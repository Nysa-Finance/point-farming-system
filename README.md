# Nysa Tracker — Kamino Market Supplier Extraction

## Process

1. **Fetch IDL** (`fetch_idl.py`)
   Fetches Kamino's Anchor program IDL from the on-chain IDL account and saves it to `klend_idl.json`.

2. **Fetch accounts** (`fetch_suppliers.py`)
   Queries `getProgramAccounts` on the Kamino program, filtered by the 8-byte account discriminator, for two account types:
   - `Reserve` — one per asset in a market
   - `Obligation` — one per user position in a market

   Each account is decoded with `anchorpy` using `klend_idl.json`. Accounts that fail to decode are skipped and counted.

3. **Filter by market**
   Both account types include a `lending_market` field; results are filtered to the target market address.

4. **Compute supplied amount**
   ```
   exchange_rate = (total_available_amount + borrowed_amount_sf) / collateral_mint_total_supply
   supplied_underlying = deposited_amount * exchange_rate
   ```
   `_sf` fields are fixed-point `U68F60`: `raw_sf / (1 << 60)`.

5. **Compute USD value**
   ```
   supplied_usd = supplied_underlying_ui * market_price_sf (scaled)
   ```

6. **Filter by asset** (optional)
   Match `reserve.liquidity.mint_pubkey` against a known mint address (e.g. USDC: `EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v`) to isolate one asset within a multi-asset market.

## Output format

| Field | Meaning |
|---|---|
| `owner` | Supplier wallet address |
| `reserve` | Reserve account address |
| `mint` | Token mint address |
| `supplied_ctoken_raw` | Raw cToken amount |
| `supplied_underlying_ui` | Supplied amount, human-readable |
| `supplied_usd` | Supplied amount in USD |
