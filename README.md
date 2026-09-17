# points_farming.py

Snapshot-based point farming tracker for USDC lenders on a Kamino (`klend`) lending market.

## What it does

Runs an infinite loop. On every cycle it:

1. Reads the on-chain state of the target Kamino market (reserves + obligations) via RPC.
2. Filters obligation deposits down to the USDC reserve, converting each lender's cToken balance into a USD-denominated supplied amount.
3. Compares each address's balance to what it held at the previous cycle, and accrues points for the elapsed time:

   ```
   points_earned = previous_balance_usd × hours_since_last_snapshot
   ```

   This rewards lenders proportionally to both how much they supply and how long they keep it supplied — a large deposit held for a short time earns the same as a small deposit held proportionally longer.
4. Writes two things to disk:
   - **`points_state.csv`** — the current standings, one row per address (`cumulative_points`, `last_supplied_usd`, `last_snapshot_ts`), sorted by points descending. This file is overwritten every cycle and doubles as the leaderboard.
   - **`snapshots/YYYY-MM-DD_HHhMM.csv`** — a raw, append-only archive of that cycle's balances, never modified after being written.
5. Sleeps for `SNAPSHOT_INTERVAL_SECONDS`, then repeats.

## Why snapshots instead of live events

The script has no way to subscribe to Kamino's on-chain events directly, so it can't award points continuously as deposits/withdrawals happen. Instead it approximates: each address's balance is assumed constant between two consecutive snapshots, and points are accrued against that assumed-constant balance for the interval that just elapsed. The shorter the interval, the closer this gets to true continuous accrual — at the cost of more RPC calls.

An address seen for the first time starts at 0 points (nothing to accrue yet) and begins earning from the next cycle onward. An address that fully withdraws still earns points for the interval it was still deposited, then drops to 0 ongoing accrual until it deposits again.

## Configuration

All at the top of the file:

| Variable | Purpose |
|---|---|
| `RPC_URL` | Solana RPC endpoint |
| `MARKET_ADDRESS` | Kamino lending market to track |
| `USDC_MINT` | Mint address used to identify the USDC reserve |
| `SNAPSHOT_INTERVAL_SECONDS` | Seconds between cycles — currently `120` for testing; raise for production (e.g. `3600` for hourly) |
| `STATE_FILE` / `SNAPSHOT_DIR` | Output paths |

## Requirements

`klend_idl.json` must be present in the working directory (generated once via `fetch_idl.py`). Python dependencies: `anchorpy`, `solana`, `solders`, `base58`.

## Known limitations

- **No anti-sniping protection at short intervals.** A fixed, predictable snapshot cadence lets someone deposit right before a snapshot and withdraw right after, farming a full interval's points with minimal real exposure. Mitigate by shortening the interval and/or adding random jitter before each run.
- **Single-market, single-asset.** Only tracks USDC deposits on one hardcoded market; extending to other assets or markets requires generalizing the reserve-lookup logic.
- **No sybil resistance.** Points are purely balance × time per address; nothing here prevents one entity from splitting capital across many wallets.
