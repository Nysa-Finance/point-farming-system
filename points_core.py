"""
Pure points logic: configuration, scoring, state I/O, safety guards.

Deliberately stdlib-only. Everything that talks to an RPC lives in points_farming.py, so
this module — where every rule that decides what a lender is owed lives — can be unit
tested in CI without a network, an RPC key, or the Solana dependency tree.
"""

import csv
import json
import os
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------


def _env(name, default):
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def _env_float(name, default):
    return float(_env(name, default))


def _env_int(name, default):
    return int(_env(name, default))


HERE = os.path.dirname(os.path.abspath(__file__))

# api.mainnet-beta.solana.com rate-limits and often outright refuses getProgramAccounts.
# CI must set SOLANA_RPC_URL to a keyed provider; the public node is a local-dev fallback only.
RPC_URL = _env("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")

KLEND_PROGRAM_ID = "KLend2g3cP87fffoy8q1mQqGKjrxjC8boSyAYavgmjD"

# The Kamino lending market to track. This default IS VITE_KAMINO_MARKET's default in
# nysa-dapp (src/chains/chains.ts) — the two are set in different repositories and a mismatch
# produces a leaderboard for a market the UI never shows, with no error anywhere. Keep them
# equal, and override both together if the market ever moves.
#
# Changing this invalidates an existing points_state.csv: points accrued against one market
# mean nothing against another. check_scope() refuses to run rather than blend the two.
MARKET_ADDRESS = _env("KAMINO_MARKET", "F4uLsGZT4YnHDcemtoYDz2LBZKLmwTB1wzkwS6oqygvy")

# Which reserves count toward points. Empty = every reserve in the market, which is what
# makes collateral deposits (USDY) earn alongside lent liquidity (USDC). Set to a
# comma-separated mint list to narrow it.
TRACKED_MINTS = [m.strip() for m in _env("TRACKED_MINTS", "").split(",") if m.strip()]

IDL_PATH = os.path.join(HERE, "klend_idl.json")
STATE_FILE = os.path.join(HERE, _env("STATE_FILE", "points_state.csv"))
META_FILE = os.path.join(HERE, _env("META_FILE", "points_meta.json"))
SNAPSHOT_DIR = os.path.join(HERE, _env("SNAPSHOT_DIR", "snapshots"))

SNAPSHOT_INTERVAL_SECONDS = _env_int("SNAPSHOT_INTERVAL_SECONDS", 3600)

# ── Points basis ───────────────────────────────────────────────────────────
# "min"   credit min(balance at start, balance at end) x elapsed hours.
# "start" credit balance at start x elapsed hours (the original behaviour).
#
# "min" is the default because a fixed daily snapshot is a published schedule: with "start"
# an address can deposit a minute before the snapshot and withdraw a minute after, and be
# credited a full 24h for two minutes of exposure. Taking the minimum makes that worth
# exactly zero. The trade-off is real and deliberate — an honest lender who withdraws
# mid-interval also earns nothing for that interval.
POINTS_BASIS = _env("POINTS_BASIS", "min")

# A brand-new address has no start balance, so its first interval would otherwise pay
# nothing. Assume arrival is uniform across the interval and backdate its clock by half of
# it: expected-value fair, and still snipe-proof because POINTS_BASIS="min" gates it on the
# balance still being there next cycle.
NEW_ADDRESS_BACKDATE_FRACTION = _env_float("NEW_ADDRESS_BACKDATE_FRACTION", 0.5)

# ── Write guard ────────────────────────────────────────────────────────────
# A partial or empty RPC read would zero every balance, and because points are
# balance x time, a zeroed balance never recovers — the leaderboard would die silently and
# permanently. Refuse to write when this cycle's total supply collapses against the last one.
MIN_SUPPLY_RATIO = _env_float("MIN_SUPPLY_RATIO", 0.25)

# Addresses that left without ever earning anything are dropped, so dust that appears once
# does not accumulate in the CSV forever. Anything with points is kept — it is a leaderboard.
PRUNE_POINTS_FLOOR = _env_float("PRUNE_POINTS_FLOOR", 1e-6)

# Optional random delay before the read, so the exact snapshot instant is not the one
# published in the workflow file. Costs runner minutes; off by default.
JITTER_MAX_SECONDS = _env_int("JITTER_MAX_SECONDS", 0)

SCALE = 1 << 60  # Fraction U68F60 — every "_sf" field is scaled by this
SCHEMA_VERSION = 2


def describe_exception(e: BaseException) -> str:
    """A one-line description that is never empty.

    The Solana and HTTP client stacks raise exceptions whose str() is the empty string, so
    `print(f"FAILED: {e}")` produces a bare "FAILED:" and a CI log with nothing to act on.
    Fall back through the type name, the provider-specific message attributes, and the
    underlying cause until there is something to read.
    """
    parts = [type(e).__name__]
    msg = str(e).strip()
    if msg:
        parts.append(msg)
    for attr in ("error_msg", "message", "detail"):
        value = getattr(e, attr, None)
        text = str(value).strip() if value is not None else ""
        if text and text != msg:
            parts.append(f"{attr}={text}")
    cause = e.__cause__ or e.__context__
    if cause is not None and cause is not e:
        parts.append(f"caused by {type(cause).__name__}: {str(cause).strip() or '<no message>'}")
    return " | ".join(parts)


def sf_to_float(raw_sf: int) -> float:
    return raw_sf / SCALE


# ---------------------------------------------------------------------------
# IDL LAYOUT — byte offset of a field, for server-side memcmp filtering
# ---------------------------------------------------------------------------
_PRIMITIVES = {
    "bool": 1, "u8": 1, "i8": 1, "u16": 2, "i16": 2, "u32": 4, "i32": 4,
    "f32": 4, "u64": 8, "i64": 8, "f64": 8, "u128": 16, "i128": 16,
    "publicKey": 32, "pubkey": 32,
}


def _sizeof(ty, types_by_name):
    """Borsh size of a fixed-width IDL type. Raises on anything variable-length."""
    if isinstance(ty, str):
        if ty in _PRIMITIVES:
            return _PRIMITIVES[ty]
        raise ValueError(f"variable-length or unknown IDL type: {ty}")
    if "array" in ty:
        inner, count = ty["array"]
        return _sizeof(inner, types_by_name) * count
    if "defined" in ty:
        name = ty["defined"]
        defn = types_by_name[name]["type"]
        if defn["kind"] == "struct":
            return sum(_sizeof(f["type"], types_by_name) for f in defn["fields"])
        if defn["kind"] == "enum" and all(not v.get("fields") for v in defn["variants"]):
            return 1  # fieldless enum is a single borsh discriminant byte
        raise ValueError(f"unsupported IDL type: {name}")
    raise ValueError(f"unsupported IDL type: {ty}")


def idl_field_offset(idl_dict, account_name, field_name):
    """Byte offset of `field_name` inside `account_name`, including the 8-byte discriminator."""
    types_by_name = {t["name"]: t for t in idl_dict.get("types", [])}
    account = next(a for a in idl_dict["accounts"] if a["name"] == account_name)
    offset = 8
    for f in account["type"]["fields"]:
        if f["name"] == field_name:
            return offset
        offset += _sizeof(f["type"], types_by_name)
    raise KeyError(f"{account_name} has no field {field_name}")


# ---------------------------------------------------------------------------
# STATE I/O
# ---------------------------------------------------------------------------
STATE_FIELDS = ["address", "cumulative_points", "last_supplied_usd", "last_snapshot_ts"]


def load_state(path):
    if not os.path.exists(path):
        return {}
    state = {}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            state[row["address"]] = {
                "cumulative_points": float(row["cumulative_points"]),
                "last_supplied_usd": float(row["last_supplied_usd"]),
                "last_snapshot_ts": int(row["last_snapshot_ts"]),
            }
    return state


def save_state(path, state):
    """Written sorted by points descending: the file IS the leaderboard."""
    rows = sorted(
        ({"address": a, **v} for a, v in state.items()),
        key=lambda r: r["cumulative_points"],
        reverse=True,
    )
    tmp = path + ".tmp"
    with open(tmp, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=STATE_FIELDS)
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)  # atomic: a crash mid-write cannot leave half a leaderboard
    return len(rows)


def load_meta(path):
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


def save_meta(path, meta):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(meta, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def save_raw_snapshot(directory, now_ts, rows):
    os.makedirs(directory, exist_ok=True)
    name = datetime.fromtimestamp(now_ts, tz=timezone.utc).strftime("%Y-%m-%d_%Hh%M")
    path = os.path.join(directory, f"{name}.csv")
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["owner", "mint", "supplied_usd"])
        w.writeheader()
        w.writerows(rows)
    return path


# ---------------------------------------------------------------------------
# GUARDS
# ---------------------------------------------------------------------------
def check_scope(meta, reset: bool, market=None, mints=None):
    """Points accrued against one market are meaningless against another.

    Changing the market or the tracked mints silently would blend two different leaderboards
    into one file, so it has to be an explicit decision.
    """
    market = MARKET_ADDRESS if market is None else market
    mints = TRACKED_MINTS if mints is None else mints
    if meta.get("market") is None or reset:
        return
    have = {"market": meta.get("market"), "tracked_mints": sorted(meta.get("tracked_mints") or [])}
    want = {"market": market, "tracked_mints": sorted(mints)}
    if have != want:
        raise RuntimeError(
            f"scope changed: state was built for {have}, this run is configured for {want}. "
            f"Re-run with --reset-scope to accept the change (existing points are kept but "
            f"will accrue against the new scope from now on)."
        )


def guard_supply_collapse(state, balances, force: bool, min_ratio=None):
    """Refuse to write a leaderboard built on an obviously broken read."""
    min_ratio = MIN_SUPPLY_RATIO if min_ratio is None else min_ratio
    if not balances:
        raise RuntimeError("read returned zero deposits — refusing to zero every balance")
    prev_total = sum(v["last_supplied_usd"] for v in state.values())
    now_total = sum(balances.values())
    if prev_total <= 0:
        return
    ratio = now_total / prev_total
    if ratio < min_ratio and not force:
        raise RuntimeError(
            f"total supplied collapsed {prev_total:,.2f} -> {now_total:,.2f} USD "
            f"(ratio {ratio:.3f} < MIN_SUPPLY_RATIO {min_ratio}). Refusing to write. "
            f"Re-run with --force if the drop is real."
        )


# ---------------------------------------------------------------------------
# SCORING
# ---------------------------------------------------------------------------
def aggregate_by_owner(rows):
    """One USD balance per owner, summed across every tracked reserve and obligation."""
    balances = {}
    for r in rows:
        balances[r["owner"]] = balances.get(r["owner"], 0.0) + r["supplied_usd"]
    return balances


def update_points(state, balances, now_ts, prev_run_ts, basis=None, backdate_fraction=None):
    """Credit the interval that just ended, then store the freshly read balance."""
    basis_mode = POINTS_BASIS if basis is None else basis
    frac = NEW_ADDRESS_BACKDATE_FRACTION if backdate_fraction is None else backdate_fraction

    interval = max(0, now_ts - prev_run_ts) if prev_run_ts else 0
    backdate = int(frac * interval)

    for addr in set(state) | set(balances):
        cur = balances.get(addr, 0.0)
        prev = state.get(addr)

        if prev is None:
            # First sighting. No points yet; the clock is backdated so the next cycle pays
            # for the part of this interval the address was (in expectation) already here.
            state[addr] = {
                "cumulative_points": 0.0,
                "last_supplied_usd": cur,
                "last_snapshot_ts": now_ts - backdate,
            }
            continue

        elapsed_hours = max(0.0, (now_ts - prev["last_snapshot_ts"]) / 3600)
        credited = min(prev["last_supplied_usd"], cur) if basis_mode == "min" else prev["last_supplied_usd"]
        prev["cumulative_points"] += max(0.0, credited) * elapsed_hours
        prev["last_supplied_usd"] = cur
        prev["last_snapshot_ts"] = now_ts

    return state


def prune_state(state, floor=None):
    """Drop addresses that left without ever earning. Anything with points is kept."""
    floor = PRUNE_POINTS_FLOOR if floor is None else floor
    dropped = [
        a for a, v in state.items()
        if v["last_supplied_usd"] <= 0 and v["cumulative_points"] < floor
    ]
    for a in dropped:
        del state[a]
    return len(dropped)
