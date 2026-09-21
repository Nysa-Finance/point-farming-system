import asyncio
import hashlib
import base58
from anchorpy import Program, Provider, Idl, Wallet
from solana.rpc.async_api import AsyncClient
from solana.rpc.types import MemcmpOpts
from solders.pubkey import Pubkey

RPC_URL = "https://api.mainnet-beta.solana.com"
KLEND_PROGRAM_ID = Pubkey.from_string("KLend2g3cP87fffoy8q1mQqGKjrxjC8boSyAYavgmjD")
SCALE = 1 << 60  # Fraction U68F60 — tutti i campi "_sf" sono scalati cosi'
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

def sf_to_float(raw_sf: int) -> float:
    return raw_sf / SCALE

def account_discriminator(name: str) -> bytes:
    return hashlib.sha256(f"account:{name}".encode()).digest()[:8]


async def fetch_accounts_safe(client, program, account_name):
    """Fetch raw accounts by discriminator, decode one-by-one, skip failures (size mismatch)."""
    disc = account_discriminator(account_name)
    resp = await client.get_program_accounts(
        KLEND_PROGRAM_ID,
        encoding="base64",
        filters=[MemcmpOpts(offset=0, bytes=base58.b58encode(disc).decode())],
    )
    decoded = []
    skipped = 0
    for entry in resp.value:
        raw_bytes = entry.account.data
        try:
            acc = program.coder.accounts.decode(raw_bytes)
            decoded.append((entry.pubkey, acc, len(raw_bytes)))
        except Exception:
            skipped += 1
    print(f"{account_name}: {len(decoded)} decodificati OK, {skipped} saltati (size mismatch)")
    return decoded


async def fetch_market_suppliers(market_address: str):
    client = AsyncClient(RPC_URL)
    with open("klend_idl.json") as f:
        idl = Idl.from_json(f.read())
    provider = Provider(client, Wallet.dummy())
    program = Program(idl, KLEND_PROGRAM_ID, provider)

    market_pk = Pubkey.from_string(market_address)

    all_reserves = await fetch_accounts_safe(client, program, "Reserve")
    reserves = {
        str(pk): acc for pk, acc, _ in all_reserves
        if str(acc.lending_market) == str(market_pk)
    }
    print(f"Reserve in questo market: {len(reserves)}")

    # Mappa Reserve -> Mint, cosi' sai sempre quale address corrisponde a quale token
    print("\n--- Mappa Reserve -> Mint ---")
    for reserve_pk, reserve in reserves.items():
        print(f"  {reserve_pk} -> mint: {reserve.liquidity.mint_pubkey}, decimals: {reserve.liquidity.mint_decimals}")

    all_obligations = await fetch_accounts_safe(client, program, "Obligation")
    obligations = [
        acc for pk, acc, _ in all_obligations
        if str(acc.lending_market) == str(market_pk)
    ]
    print(f"Obligation in questo market: {len(obligations)}")

    results = []
    for obl in obligations:
        owner = str(obl.owner)
        for dep in obl.deposits:
            reserve_pk = str(dep.deposit_reserve)
            if reserve_pk not in reserves or dep.deposited_amount == 0:
                continue
            reserve = reserves[reserve_pk]

            total_liquidity = (
                reserve.liquidity.total_available_amount
                + sf_to_float(reserve.liquidity.borrowed_amount_sf)
            )
            collateral_supply = reserve.collateral.mint_total_supply
            exchange_rate = (
                total_liquidity / collateral_supply if collateral_supply > 0 else 1.0
            )

            supplied_underlying = dep.deposited_amount * exchange_rate
            decimals = reserve.liquidity.mint_decimals
            supplied_ui = supplied_underlying / (10 ** decimals)
            price_usd = sf_to_float(reserve.liquidity.market_price_sf)
            supplied_usd = supplied_ui * price_usd

            results.append({
                "owner": owner,
                "reserve": reserve_pk,
                "mint": str(reserve.liquidity.mint_pubkey),
                "supplied_ctoken_raw": dep.deposited_amount,
                "supplied_underlying_ui": round(supplied_ui, 6),
                "supplied_usd": round(supplied_usd, 2),
            })

    await client.close()
    return results, reserves


if __name__ == "__main__":
    # Market comes from points_core so these debugging scripts can never drift onto a
    # different market than the tracker itself. Override with KAMINO_MARKET.
    from points_core import MARKET_ADDRESS

    data, reserves = asyncio.run(fetch_market_suppliers(MARKET_ADDRESS))

    # Trova quale reserve e' USDC confrontando il mint
    usdc_reserve_pk = None
    for reserve_pk, reserve in reserves.items():
        if str(reserve.liquidity.mint_pubkey) == USDC_MINT:
            usdc_reserve_pk = reserve_pk
            break

    if usdc_reserve_pk is None:
        print("\nUSDC non trovato tra le reserve di questo market — controlla la mappa sopra.")
    else:
        usdc_suppliers = [r for r in data if r["reserve"] == usdc_reserve_pk]
        print(f"\n--- USDC suppliers ({len(usdc_suppliers)}) ---")
        for row in usdc_suppliers:
            print(row)
