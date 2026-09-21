# NYSA Points Farming Tracker for Kamino Pools

Snapshot-based point farming tracker for lenders on a Kamino (`klend`) lending market.

`points_state.csv` in this repository is the leaderboard. A daily GitHub Actions job rewrites
it, and the Nysa dapp reads it through its own `/api/points` proxy to render the Farm Points
table.

## Quickstart

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# one-time: fetch the program IDL, only needed if klend_idl.json isn't present yet
python fetch_idl.py

# one cycle, then exit — what CI runs
SOLANA_RPC_URL=https://your-keyed-endpoint python points_farming.py --once

# or the local daemon loop (Ctrl+C to stop)
SOLANA_RPC_URL=https://your-keyed-endpoint python points_farming.py
```

Output lands in `points_state.csv` (leaderboard), `points_meta.json` (run metadata) and
`snapshots/` (raw archive).

## What it does

On every cycle it:

1. Reads the target Kamino market on-chain. Both `getProgramAccounts` calls carry a `memcmp`
   filter on the account's `lendingMarket` field, so the RPC returns only this market's
   reserves and obligations rather than every Kamino market at once.
2. Converts each lender's cToken balance in each tracked reserve into a USD amount, and sums
   them per owner across reserves and obligations.
3. Credits points for the elapsed time:

   ```
   points_earned = credited_balance_usd × hours_since_last_snapshot
   ```

   where `credited_balance_usd` is `min(balance at start, balance at end)` by default — see
   **Points basis** below.
4. Writes:
   - **`points_state.csv`** — current standings, one row per address
     (`cumulative_points`, `last_supplied_usd`, `last_snapshot_ts`), sorted by points
     descending. Overwritten every cycle, atomically.
   - **`points_meta.json`** — market, tracked mints, basis, last run time, totals. Also the
     guard that stops two different markets' points being blended into one file.
   - **`snapshots/YYYY-MM-DD_HHhMM.csv`** — raw append-only archive of that cycle's balances.

Elapsed time is measured from each address's own `last_snapshot_ts`, so a late or skipped run
costs an honest holder nothing — the next run credits the whole gap.

## Points basis

`POINTS_BASIS` decides what a lender is credited for an interval.

| Value | Credits | Notes |
|---|---|---|
| `min` (default) | `min(balance at start, balance at end)` | Snipe-proof |
| `start` | `balance at start` | Original behaviour |

A daily snapshot is a **published schedule**. Under `start`, an address can deposit a minute
before the snapshot and withdraw a minute after, and be credited a full 24 hours for two
minutes of real exposure. `min` makes that worth exactly zero.

The trade-off is deliberate and it is not free: an honest lender who withdraws mid-interval
also earns nothing for that interval. Shortening the interval reduces the cost. Set
`JITTER_MAX_SECONDS` to add a random delay before the read if you also want the snapshot
instant to be unpredictable.

New addresses earn nothing on first sighting (there is no start balance to credit), so their
clock is backdated by `NEW_ADDRESS_BACKDATE_FRACTION` of the interval — half by default, the
expected value if arrival is uniform. The next cycle pays for that backdated period, and
`min` still gates it on the balance actually still being there.

## Configuration

Every setting is an environment variable, so CI never edits source.

| Variable | Default | Purpose |
|---|---|---|
| `SOLANA_RPC_URL` | public mainnet-beta | Solana RPC. **Set this** — see *RPC requirements*. |
| `KAMINO_MARKET` | `FteaGMVC…kZL7` | Lending market to track. Must match `VITE_KAMINO_MARKET` in the dapp. |
| `TRACKED_MINTS` | *(empty — all reserves)* | Comma-separated mints. Empty means every reserve in the market counts, so collateral earns alongside lent liquidity. |
| `POINTS_BASIS` | `min` | See above. |
| `NEW_ADDRESS_BACKDATE_FRACTION` | `0.5` | Fraction of the interval credited to a first-seen address. |
| `MIN_SUPPLY_RATIO` | `0.25` | Refuse to write if total supplied falls below this fraction of the previous run. |
| `PRUNE_POINTS_FLOOR` | `1e-6` | Drop departed addresses below this many points. |
| `JITTER_MAX_SECONDS` | `0` | Random pre-read delay, `--once` mode only. Costs runner minutes. |
| `SNAPSHOT_INTERVAL_SECONDS` | `3600` | Daemon mode only. |
| `STATE_FILE` / `META_FILE` / `SNAPSHOT_DIR` | as named | Output paths. |

### CLI

| Flag | Effect |
|---|---|
| `--once` | One cycle, then exit. Exit code 1 on any failure. |
| `--force` | Write even if the supply-collapse guard trips. |
| `--reset-scope` | Accept a changed `KAMINO_MARKET` / `TRACKED_MINTS`. |

## RPC requirements

The tracker calls `getProgramAccounts` on the klend program. Even filtered to one market this
is a heavy call, and `api.mainnet-beta.solana.com` rate-limits it and frequently refuses it
outright. **Use a keyed provider** (Helius, Triton, QuickNode, Alchemy). The public endpoint
is a local-development fallback, not a production configuration.

## Safety guards

Points are `balance × time`, which means a balance zeroed by a bad read never recovers — the
leaderboard would die silently and permanently. Three guards sit in front of every write:

- **Empty read** — a cycle that returns zero deposits is refused outright.
- **Supply collapse** — if this cycle's total supplied USD is below `MIN_SUPPLY_RATIO` of the
  previous cycle's, the write is refused. Override with `--force` when the drop is real.
- **Scope change** — if `points_meta.json` records a different market or mint set than the
  current configuration, the run aborts. Override with `--reset-scope`.

Nothing is written until all three pass, `points_state.csv` is replaced atomically, and
`--once` exits non-zero on any failure so CI never commits a damaged leaderboard.

## Automation

`.github/workflows/points-snapshot.yml` runs daily at `17 3 * * *` UTC (05:17 Europe/Amsterdam
in summer, 04:17 in winter — GitHub cron is UTC and does not follow DST), runs the unit tests
first, takes the snapshot and commits the result.

It must live on the **default branch**: GitHub only evaluates `schedule:` there, whatever
branch the job would otherwise check out.

Required once, under *Settings → Secrets and variables → Actions*:

- Secret `SOLANA_RPC_URL` — the job fails fast with instructions if it is missing.
- Variables (all optional): `KAMINO_MARKET`, `TRACKED_MINTS`, `POINTS_BASIS`,
  `MIN_SUPPLY_RATIO`, `JITTER_MAX_SECONDS`.

`workflow_dispatch` exposes `force` and `reset_scope` for manual runs.

> GitHub disables scheduled workflows after 60 days without repository activity. The job's own
> commits normally keep it alive, but it is worth confirming the schedule is still enabled
> after a quiet period.

## Tests

`points_core.py` holds every rule that decides what a lender is owed, and imports nothing
outside the standard library — so the tests need no RPC, no key and no network:

```bash
python -m unittest discover -v -p 'test_*.py'
```

They run on every push via `.github/workflows/ci.yml`, and gate the snapshot job.

## Files

| File | Role |
|---|---|
| `points_core.py` | Config, scoring, state I/O, guards. Stdlib only, tested. |
| `points_farming.py` | RPC layer + CLI. |
| `test_points_core.py` | Unit tests. |
| `summarise.py` | Job-summary helper for CI. |
| `fetch_idl.py` | One-time IDL fetch. |
| `fetch_suppliers.py`, `suppliers_usdc_filtered.py`, `inspect_idl.py` | Exploratory scripts kept for reference. |

## Known limitations

- **No sybil resistance.** Points are balance × time per address; nothing here prevents one
  entity splitting capital across many wallets.
- **Honest mid-interval withdrawals earn nothing** under `POINTS_BASIS=min`. This is the
  price of closing the snapshot snipe at a daily cadence; a shorter interval reduces it.
- **Single market.** One market per state file. Tracking a second one needs a second
  checkout with its own `STATE_FILE`, `META_FILE` and `SNAPSHOT_DIR`.
