"""
Point farming for lenders on a Kamino (klend) lending market.

Each cycle:
  1. Read the market on-chain (reserves + obligations) via RPC.
  2. Sum every tracked deposit per owner into a USD balance.
  3. Credit points for the elapsed time.
  4. Rewrite points_state.csv (the leaderboard) and archive the raw snapshot.

Two run modes:
  python points_farming.py --once   one cycle, then exit. Exit code 0 on success,
                                    1 on any failure. This is what CI runs.
  python points_farming.py          daemon loop every SNAPSHOT_INTERVAL_SECONDS,
                                    for local development.

Scoring rules, state I/O and the safety guards live in points_core.py (stdlib only, unit
tested). This file is the RPC layer and the CLI. Configuration is by environment variable
so CI never has to edit source — see the table in README.md.
"""

import argparse
import asyncio
import hashlib
import json
import os
import random
import sys
import time
import traceback
from datetime import datetime, timezone

import base58
from anchorpy import Program, Provider, Idl, Wallet
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Finalized
from solana.rpc.types import MemcmpOpts
from solders.pubkey import Pubkey

import points_core as core

KLEND_PROGRAM_ID = Pubkey.from_string(core.KLEND_PROGRAM_ID)


def account_discriminator(name: str) -> bytes:
    return hashlib.sha256(f"account:{name}".encode()).digest()[:8]


# ---------------------------------------------------------------------------
# FETCH ON-CHAIN
# ---------------------------------------------------------------------------
async def fetch_accounts_filtered(client, program, account_name, market_offset, market_pk):
    """Every account of one type belonging to ONE market.

    The market memcmp runs on the RPC node. Without it this asks for every Reserve and every
    Obligation in the whole klend program — all Kamino markets at once, hundreds of
    megabytes, which public and most keyed RPCs simply refuse.
    """
    disc = account_discriminator(account_name)
    try:
        resp = await client.get_program_accounts(
            KLEND_PROGRAM_ID,
            encoding="base64",
            commitment=Finalized,
            filters=[
                MemcmpOpts(offset=0, bytes=base58.b58encode(disc).decode()),
                MemcmpOpts(offset=market_offset, bytes=str(market_pk)),
            ],
        )
    except Exception as e:
        # Name the call that failed. Many providers disable or heavily restrict
        # getProgramAccounts, and the client raises that as an exception with no message.
        raise RuntimeError(
            f"getProgramAccounts({account_name}) failed against {core.RPC_URL.split('?')[0]} "
            f"-- {core.describe_exception(e)}. Most providers restrict or rate-limit this "
            f"call; confirm the endpoint allows it and that the plan covers it."
        ) from e
    decoded, skipped = [], 0
    for entry in resp.value:
        try:
            decoded.append((entry.pubkey, program.coder.accounts.decode(entry.account.data)))
        except Exception:
            skipped += 1
    if skipped:
        print(f"  {account_name}: {len(decoded)} decoded, {skipped} skipped (size mismatch)")
    return decoded


async def fetch_market_deposits(market_address: str):
    """[{owner, mint, supplied_usd}] for every tracked reserve in the market."""
    client = AsyncClient(core.RPC_URL, timeout=120)
    try:
        with open(core.IDL_PATH) as f:
            raw_idl = f.read()
        idl_dict = json.loads(raw_idl)
        program = Program(Idl.from_json(raw_idl), KLEND_PROGRAM_ID, Provider(client, Wallet.dummy()))
        market_pk = Pubkey.from_string(market_address)

        reserve_offset = core.idl_field_offset(idl_dict, "Reserve", "lendingMarket")
        obligation_offset = core.idl_field_offset(idl_dict, "Obligation", "lendingMarket")

        reserves = dict(await fetch_accounts_filtered(
            client, program, "Reserve", reserve_offset, market_pk))
        if not reserves:
            raise RuntimeError(
                f"no reserves found for market {market_address} — wrong market address, or the "
                f"RPC silently truncated the getProgramAccounts response")

        # Per-reserve conversion constants, resolved once.
        tracked = {}
        for pk, acc in reserves.items():
            mint = str(acc.liquidity.mint_pubkey)
            if core.TRACKED_MINTS and mint not in core.TRACKED_MINTS:
                continue
            total_liquidity = (
                acc.liquidity.total_available_amount
                + core.sf_to_float(acc.liquidity.borrowed_amount_sf)
            )
            collateral_supply = acc.collateral.mint_total_supply
            tracked[str(pk)] = {
                "mint": mint,
                "exchange_rate": total_liquidity / collateral_supply if collateral_supply > 0 else 1.0,
                "decimals": acc.liquidity.mint_decimals,
                "price_usd": core.sf_to_float(acc.liquidity.market_price_sf),
            }
        if not tracked:
            raise RuntimeError(
                f"no reserve in {market_address} matches TRACKED_MINTS={core.TRACKED_MINTS}")

        obligations = await fetch_accounts_filtered(
            client, program, "Obligation", obligation_offset, market_pk)

        rows = []
        for _pk, obl in obligations:
            for dep in obl.deposits:
                r = tracked.get(str(dep.deposit_reserve))
                if r is None or dep.deposited_amount == 0:
                    continue
                supplied_ui = (dep.deposited_amount * r["exchange_rate"]) / (10 ** r["decimals"])
                rows.append({
                    "owner": str(obl.owner),
                    "mint": r["mint"],
                    "supplied_usd": round(supplied_ui * r["price_usd"], 6),
                })
        print(f"  reserves tracked: {len(tracked)} | obligations: {len(obligations)} | deposits: {len(rows)}")
        return rows
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# CYCLE
# ---------------------------------------------------------------------------
async def run_once(force=False, reset_scope=False):
    now_ts = int(time.time())
    stamp = datetime.fromtimestamp(now_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{stamp}] market={core.MARKET_ADDRESS} basis={core.POINTS_BASIS}", flush=True)

    meta = core.load_meta(core.META_FILE)
    core.check_scope(meta, reset_scope)

    rows = await fetch_market_deposits(core.MARKET_ADDRESS)
    balances = core.aggregate_by_owner(rows)

    state = core.load_state(core.STATE_FILE)
    # Everything that can refuse the write happens BEFORE anything is written.
    core.guard_supply_collapse(state, balances, force)

    prev_run_ts = meta.get("last_run_ts") or max(
        (v["last_snapshot_ts"] for v in state.values()), default=0)
    state = core.update_points(state, balances, now_ts, prev_run_ts)
    pruned = core.prune_state(state)

    snapshot_path = core.save_raw_snapshot(core.SNAPSHOT_DIR, now_ts, rows)
    written = core.save_state(core.STATE_FILE, state)
    core.save_meta(core.META_FILE, {
        "schema_version": core.SCHEMA_VERSION,
        "market": core.MARKET_ADDRESS,
        "tracked_mints": sorted(core.TRACKED_MINTS),
        "points_basis": core.POINTS_BASIS,
        "last_run_ts": now_ts,
        "last_run_utc": stamp,
        "addresses": written,
        "total_supplied_usd": round(sum(balances.values()), 6),
    })
    print(f"  wrote {written} addresses ({pruned} pruned) | snapshot -> {os.path.basename(snapshot_path)}")


async def main_loop():
    while True:
        try:
            await run_once()
        except Exception as e:
            print(f"snapshot failed: {core.describe_exception(e)}", file=sys.stderr, flush=True)
        await asyncio.sleep(core.SNAPSHOT_INTERVAL_SECONDS)


def main():
    p = argparse.ArgumentParser(description="Kamino points farming tracker")
    p.add_argument("--once", action="store_true",
                   help="run a single cycle and exit (non-zero on failure). Use this in CI.")
    p.add_argument("--force", action="store_true",
                   help="write even if the supply-collapse guard trips")
    p.add_argument("--reset-scope", action="store_true",
                   help="accept a changed market / tracked mints")
    args = p.parse_args()

    if not args.once:
        asyncio.run(main_loop())
        return 0

    if core.JITTER_MAX_SECONDS > 0:
        delay = random.randint(0, core.JITTER_MAX_SECONDS)
        print(f"jitter: sleeping {delay}s so the snapshot instant is not the published cron time")
        time.sleep(delay)

    try:
        asyncio.run(run_once(force=args.force, reset_scope=args.reset_scope))
    except Exception as e:
        # Fail loudly AND legibly. A job that exits 0 after a failed read would commit a
        # stale or damaged leaderboard; a job that exits 1 with no message is nearly as bad,
        # because nobody can tell which of the guards or the RPC actually refused.
        sys.stdout.flush()
        print(f"FAILED: {core.describe_exception(e)}", file=sys.stderr, flush=True)
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
