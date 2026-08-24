# NSE Strategy Tracker — supply_demand & bos_fib_ifvg

A live paper-trading pipeline: a **scanner** job finds fresh setups across
the NSE-500 every trading day, and a **tracker** job fills entries, marks
positions to market daily, and closes them out, all logged to a single
CSV trade book. No manual work required once it's set up — GitHub Actions
runs both jobs automatically and commits the results back to the repo.

## What it does, in one sentence each

- **`scanner.py`** (runs ~4:00 PM IST, after NSE close): scans the current
  NSE-500 for `supply_demand` and `bos_fib_ifvg` setups whose confirmation
  candle closed *today*, and appends a `PENDING_ENTRY` row per fresh
  signal.
- **`tracker.py`** (runs ~4:15 PM IST): fills any `PENDING_ENTRY` row that
  was scanned on a *prior* day using today's Open, checks all open trades
  against Stop / Target 1 / EMA50-break / max-hold-time, and updates P&L.

## Key design decisions (read this before you rely on the numbers)

- **Entry price = next trading day's Open, not the scan day's Open.**
  The scanner runs after the market has already closed, so that day's
  Open already happened hours before the signal existed — using it would
  be an impossible fill. `Scanned Date` (immutable, the day the setup was
  confirmed) and `Entry Date` (the day the trade actually opened) are
  therefore two separate columns and will differ by one trading day.
- **Dates are ISO 8601 (`YYYY-MM-DD`) everywhere, no exceptions.** This
  format is unambiguous (year always first, always 4 digits) and sorts
  correctly as plain text — there's no way to misread `2026-05-12` as
  either May 12 or Dec 5. Never hand-format a date anywhere in this repo;
  always go through `market_utils.DATE_FMT`.
- **Holiday/weekend handling is fully automatic.** Both jobs check
  whether the Nifty 50 index actually produced a *new* daily candle today
  (`market_utils.is_fresh_trading_day()`). If not — weekend, NSE holiday,
  Yahoo Finance lag — the job logs it and exits without touching the
  trade book. No holiday calendar to maintain.
- **`Target 2` is informational only.** Neither source strategy defines a
  second profit target — it's shown as a reference level (a wider 3:1 RR
  vs. Target 1's 2:1) but it never triggers an exit on its own.
- **Exit priority when multiple conditions could fire the same day:**
  Stoploss Hit → Target1 Hit → EMA50 Breakout → Time Exit, in that order.
  This matches the conservative "assume the worst case" priority used in
  the original 15-year backtest, so live results stay comparable to what
  you already validated.
- **`max_hold_days = 20`** and the **0.3% round-trip cost** used for
  `PnL % (Net of 0.3% Cost)` both match the backtest defaults exactly.

## Trade book columns (`data/trade_book.csv`)

| Column | Meaning |
|---|---|
| Ticker | NSE symbol, e.g. `RELIANCE.NS` |
| Strategy | `supply_demand` or `bos_fib_ifvg` |
| Scanned Date | Day the setup was confirmed — **immutable, never changes** |
| Status | `PENDING_ENTRY` → `OPEN` → `CLOSED` |
| Entry Date | Day the position actually opened (next trading day after Scanned Date) |
| Entry Price | That day's Open |
| Stop Loss | Structural stop, sized off the confirmation candle |
| Target 1 | 2:1 RR target (the real exit trigger) |
| Target 2 (Informational) | 3:1 RR reference level — **not a real exit trigger** |
| EMA50 At Entry | EMA50 value at the confirmation bar |
| Current Price | Latest tracked price (Close, or the exit price once closed) |
| PnL / PnL % | Raw profit/loss in price and percent |
| PnL % (Net of 0.3% Cost) | Same, after the 0.3% round-trip cost assumption |
| Days Held | Trading days since entry |
| Exit Date / Exit Price | Set once the trade closes |
| Exit Reason | One of: `Stoploss Hit`, `Target1 Hit`, `EMA50 Breakout`, `Time Exit` |
| Last Tracked | Timestamp of the most recent update (IST) |

## Setup instructions (step by step)

### 1. Create the GitHub repo
1. Go to github.com → **New repository** → name it (e.g. `nse-strategy-tracker`)
   → keep it **Public or Private**, either works → **Create repository**.
2. Upload every file from this project into the repo root, keeping the
   folder structure intact:
   ```
   nse-strategy-tracker/
   ├── .github/workflows/scanner.yml
   ├── .github/workflows/tracker.yml
   ├── data/trade_book.csv
   ├── market_utils.py
   ├── requirements.txt
   ├── scanner.py
   ├── strategies.py
   ├── tracker.py
   └── universe.py
   ```
   Easiest way: on your machine, `git init`, `git add .`, `git commit -m
   "initial"`, then `git remote add origin <your repo URL>` and `git push
   -u origin main`. (If GitHub gives you a different default branch name
   than `main`, edit that branch name into both workflow YAML files'
   `git pull --rebase origin main` line.)

### 2. Allow the workflows to commit back to the repo
GitHub Actions can't push commits by default — you have to turn this on:
1. In your repo: **Settings** → **Actions** → **General**.
2. Scroll to **Workflow permissions**.
3. Select **"Read and write permissions"**.
4. Click **Save**.

(This is the single most common reason people's automated bots silently
stop updating their files — if you skip this step, both jobs will run
successfully but the `git push` step will fail with a permissions error.)

### 3. Confirm the schedule
Both workflows are already set to run automatically, Monday–Friday:
- **Scanner**: 10:30 UTC = 4:00 PM IST
- **Tracker**: 10:45 UTC = 4:15 PM IST

GitHub's scheduler can run a few minutes late under heavy load — that's
fine, since the actual freshness check inside the code (not the trigger
time) decides whether the job does anything.

### 4. Test it manually before waiting for the schedule
1. Go to your repo's **Actions** tab.
2. Click **"Scanner (4:00 PM IST)"** in the left sidebar → **"Run
   workflow"** button → **Run workflow** (green button).
3. Watch it run — click into the run to see live logs. It should print
   universe size, tickers processed, and any signals found.
4. Once it finishes, check `data/trade_book.csv` in your repo — you
   should see a new commit from `trading-bot` if any signal fired (or no
   change if the market didn't offer a fresh setup that day — totally
   normal, this can happen for stretches of days).
5. Do the same for **"Tracker (4:15 PM IST)"** to confirm it runs
   cleanly too (it'll just say "no PENDING_ENTRY/OPEN rows to process" if
   the scanner didn't find anything yet — that's expected on a fresh
   repo).

### 5. (Optional) Pin your own NSE500 list
The universe is fetched automatically at runtime (official NSE archive,
falling back to community GitHub mirrors), but if you want full control
or the automatic sources are unreliable, add your own file at
`data/nse500_symbols.csv` with a `Symbol` column (with or without the
`.NS` suffix) — the code checks for this file first, before anything else.

### 6. Let it run
That's it — from here it's fully automatic. Every trading day, the
scanner adds fresh signals and the tracker manages every open position
until it closes, all logged with a full audit trail in
`data/trade_book.csv`, version-controlled via git history so you can see
exactly how the trade book evolved day by day.

## Known limitations (be aware of these)

- **Entry price sizing gap**: Stop/Target1/Target2 are computed off the
  confirmation candle's Close, but the real fill is the next day's Open.
  If the stock gaps overnight, your actual risk/reward will differ
  slightly from what's shown at signal time — this is unavoidable for any
  EOD-scan system and isn't a bug.
- **NSE500 list currency**: none of the automatic sources are officially
  guaranteed to reflect the exact current index membership at every
  moment (NSE rebalances Nifty 500 roughly twice a year). Re-check/pin
  your own list periodically if this matters to you.
- **Yahoo Finance data quality**: occasional missing bars, delayed
  updates, or symbol changes can cause a ticker to be silently skipped
  for a day. The freshness check protects against acting on stale data,
  but doesn't guarantee every single ticker's data is perfect every day.
