"""
Point farming per lender USDC su un market Kamino (klend).

Ogni SNAPSHOT_INTERVAL_SECONDS secondi:
  1. legge il market on-chain (stesso codice di suppliers_usdc_filtered.py)
  2. filtra i supplier del reserve USDC
  3. accredita punti = saldo_precedente_usd x ore_trascorse a ogni address
  4. aggiorna points_state.csv (stato corrente, ordinato per punti = la "classifica")
  5. archivia lo snapshot grezzo in snapshots/YYYY-MM-DD_HHhMM.csv

Per ora l'intervallo e' impostato a 2 minuti SOLO per test. In produzione
alzalo (es. 3600 = ogni ora) cambiando SNAPSHOT_INTERVAL_SECONDS qui sotto.
"""

import asyncio
import csv
import hashlib
import os
import time
from datetime import datetime, timezone

import base58
from anchorpy import Program, Provider, Idl, Wallet
from solana.rpc.async_api import AsyncClient
from solana.rpc.types import MemcmpOpts
from solders.pubkey import Pubkey

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
RPC_URL = "https://api.mainnet-beta.solana.com"
KLEND_PROGRAM_ID = Pubkey.from_string("KLend2g3cP87fffoy8q1mQqGKjrxjC8boSyAYavgmjD")
MARKET_ADDRESS = "FteaGMVCLDF4eonrTiQkRQ5kby5ohwCfaMD2mNiPkZL7"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
IDL_PATH = "klend_idl.json"

SNAPSHOT_INTERVAL_SECONDS = 120  # <-- TEST: 2 minuti. Upgradeable (es. 3600 = 1h).
STATE_FILE = "points_state.csv"
SNAPSHOT_DIR = "snapshots"

SCALE = 1 << 60  # Fraction U68F60 — tutti i campi "_sf" sono scalati cosi'


def sf_to_float(raw_sf: int) -> float:
    return raw_sf / SCALE


def account_discriminator(name: str) -> bytes:
    return hashlib.sha256(f"account:{name}".encode()).digest()[:8]


# ---------------------------------------------------------------------------
# FETCH ON-CHAIN (stessa logica di suppliers_usdc_filtered.py)
# ---------------------------------------------------------------------------
async def fetch_accounts_safe(client, program, account_name):
    disc = account_discriminator(account_name)
    resp = await client.get_program_accounts(
        KLEND_PROGRAM_ID,
        encoding="base64",
        filters=[MemcmpOpts(offset=0, bytes=base58.b58encode(disc).decode())],
    )
    decoded = []
    for entry in resp.value:
        try:
            acc = program.coder.accounts.decode(entry.account.data)
            decoded.append((entry.pubkey, acc))
        except Exception:
            pass
    return decoded


async def fetch_usdc_suppliers(market_address: str):
    """Ritorna [{owner, supplied_usd}] per il reserve USDC del market dato."""
    client = AsyncClient(RPC_URL)
    with open(IDL_PATH) as f:
        idl = Idl.from_json(f.read())
    provider = Provider(client, Wallet.dummy())
    program = Program(idl, KLEND_PROGRAM_ID, provider)
    market_pk = Pubkey.from_string(market_address)

    all_reserves = await fetch_accounts_safe(client, program, "Reserve")
    reserves = {
        str(pk): acc for pk, acc in all_reserves
        if str(acc.lending_market) == str(market_pk)
    }

    usdc_reserve_pk, usdc_reserve = None, None
    for pk, acc in reserves.items():
        if str(acc.liquidity.mint_pubkey) == USDC_MINT:
            usdc_reserve_pk, usdc_reserve = pk, acc
            break
    if usdc_reserve is None:
        await client.close()
        raise RuntimeError("Reserve USDC non trovato in questo market.")

    total_liquidity = (
        usdc_reserve.liquidity.total_available_amount
        + sf_to_float(usdc_reserve.liquidity.borrowed_amount_sf)
    )
    collateral_supply = usdc_reserve.collateral.mint_total_supply
    exchange_rate = total_liquidity / collateral_supply if collateral_supply > 0 else 1.0
    decimals = usdc_reserve.liquidity.mint_decimals
    price_usd = sf_to_float(usdc_reserve.liquidity.market_price_sf)

    all_obligations = await fetch_accounts_safe(client, program, "Obligation")

    results = []
    for pk, obl in all_obligations:
        if str(obl.lending_market) != str(market_pk):
            continue
        for dep in obl.deposits:
            if str(dep.deposit_reserve) != usdc_reserve_pk or dep.deposited_amount == 0:
                continue
            supplied_ui = (dep.deposited_amount * exchange_rate) / (10 ** decimals)
            results.append({
                "owner": str(obl.owner),
                "supplied_usd": round(supplied_ui * price_usd, 6),
            })

    await client.close()
    return results


# ---------------------------------------------------------------------------
# STATO PUNTI (points_state.csv) + ARCHIVIO SNAPSHOT
# ---------------------------------------------------------------------------
def load_state(path):
    """address -> {cumulative_points, last_supplied_usd, last_snapshot_ts}"""
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
    """Salva ordinato per punti decrescenti: il file E' la classifica."""
    rows = sorted(
        ({"address": addr, **v} for addr, v in state.items()),
        key=lambda r: r["cumulative_points"],
        reverse=True,
    )
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=["address", "cumulative_points", "last_supplied_usd", "last_snapshot_ts"]
        )
        writer.writeheader()
        writer.writerows(rows)


def save_raw_snapshot(directory, now_ts, suppliers):
    os.makedirs(directory, exist_ok=True)
    fname = datetime.fromtimestamp(now_ts, tz=timezone.utc).strftime("%Y-%m-%d_%Hh%M")
    path = os.path.join(directory, f"{fname}.csv")
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["owner", "supplied_usd"])
        writer.writeheader()
        writer.writerows(suppliers)
    return path


def update_points(state, suppliers, now_ts):
    """Accredita punti = saldo precedente x ore trascorse, poi aggiorna il saldo.

    Usa il saldo registrato ALL'INIZIO dell'intervallo per calcolare i punti
    dell'intervallo appena trascorso (approssimazione standard quando non hai
    eventi in tempo reale) — poi lo sostituisce col saldo appena letto.
    """
    snapshot_balances = {}
    for row in suppliers:
        snapshot_balances[row["owner"]] = snapshot_balances.get(row["owner"], 0.0) + row["supplied_usd"]

    all_addresses = set(state.keys()) | set(snapshot_balances.keys())

    for addr in all_addresses:
        prev = state.get(addr)
        if prev is None:
            # Primo avvistamento: nessun punto ancora, si parte da qui.
            state[addr] = {
                "cumulative_points": 0.0,
                "last_supplied_usd": snapshot_balances.get(addr, 0.0),
                "last_snapshot_ts": now_ts,
            }
            continue

        elapsed_hours = (now_ts - prev["last_snapshot_ts"]) / 3600
        points_earned = prev["last_supplied_usd"] * elapsed_hours

        prev["cumulative_points"] += points_earned
        prev["last_supplied_usd"] = snapshot_balances.get(addr, 0.0)  # 0 se ha prelevato tutto
        prev["last_snapshot_ts"] = now_ts

    return state


# ---------------------------------------------------------------------------
# LOOP PRINCIPALE
# ---------------------------------------------------------------------------
async def run_once():
    now_ts = int(time.time())
    suppliers = await fetch_usdc_suppliers(MARKET_ADDRESS)

    snapshot_path = save_raw_snapshot(SNAPSHOT_DIR, now_ts, suppliers)
    state = load_state(STATE_FILE)
    state = update_points(state, suppliers, now_ts)
    save_state(STATE_FILE, state)

    ts_label = datetime.fromtimestamp(now_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{ts_label}] {len(suppliers)} depositi USDC letti | snapshot -> {snapshot_path} | stato -> {STATE_FILE}")


async def main_loop():
    while True:
        try:
            await run_once()
        except Exception as e:
            print(f"Errore durante lo snapshot: {e}")
        await asyncio.sleep(SNAPSHOT_INTERVAL_SECONDS)


if __name__ == "__main__":
    asyncio.run(main_loop())
