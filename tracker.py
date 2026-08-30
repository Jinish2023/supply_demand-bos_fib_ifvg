#!/usr/bin/env python3
"""
tracker.py  -  runs daily at ~4:15 PM IST (after scanner.py)
============================================================================
Two jobs in one pass over data/trade_book.csv:

1. PENDING_ENTRY rows scanned on a PRIOR trading day get their entry
   filled using TODAY's Open (the earliest realistic fill - see README for
   why Entry Price is never the Scanned Date's own open). Same-day exit
   conditions (stop/target/EMA50) are then checked immediately using
   today's own High/Low/Close, since the entry already happened this
   morning and the day's full range is known by 4:15 PM.

   NOTE: PENDING_ENTRY rows scanned TODAY are deliberately left alone -
   today's open already happened before the scan even ran, so there's
   nothing to fill yet. They'll be picked up on the NEXT run.

2. OPEN rows get marked-to-market using today's Close as Current Price,
   Days Held +1, and are checked against Stop / Target 1 / EMA50 break /
   max hold time, in that priority order (same conservative "stop wins
   ties" priority used in the original backtest).

Exit Reason is always one of exactly: "Stoploss Hit", "Target1 Hit",
"EMA50 Breakout", "Time Exit" - Target 2 is informational only and never
triggers an exit (see README).

Skips entirely if today isn't a real NSE trading day.
============================================================================
"""

import sys

import pandas as pd

import market_utils as mu
from scanner import download_chunk, batched
from strategies import add_indicators

MAX_HOLD_DAYS = 20     # matches the backtest default
ROUND_TRIP_COST_PCT = 0.3   # percentage points, matches the 0.3% used throughout
CHUNK_SIZE = 50
# Tracker only needs enough history for EMA50 to be numerically stable, not
# the scanner's 120-bar swing/channel/FVG lookback requirement - reusing
# that threshold here was a bug that could silently drop a ticker from
# tracking (see download_chunk's docstring in scanner.py for the full story).
TRACKER_MIN_BARS = 60


def fetch_latest_bars(tickers):
    """Returns {ticker: (open, high, low, close, ema50, bar_date_str)} for
    today's bar, for the given tickers."""
    result = {}
    for chunk in batched(sorted(set(tickers)), CHUNK_SIZE):
        data = download_chunk(chunk, period="1y", min_bars=TRACKER_MIN_BARS)
        for ticker, df in data.items():
            df = add_indicators(df)
            if df.empty:
                continue
            last = df.iloc[-1]
            bar_date_str = pd.Timestamp(df.index[-1]).strftime(mu.DATE_FMT)
            result[ticker] = dict(
                open=float(last["Open"]), high=float(last["High"]),
                low=float(last["Low"]), close=float(last["Close"]),
                ema50=float(last["EMA50"]), bar_date=bar_date_str,
            )
    return result


def compute_pnl(entry_price, current_price):
    pnl = current_price - entry_price
    pnl_pct = pnl / entry_price * 100.0
    net_pnl_pct = pnl_pct - ROUND_TRIP_COST_PCT
    return pnl, pnl_pct, net_pnl_pct


def main():
    if not mu.is_fresh_trading_day():
        print("[tracker] Skipping - not a fresh trading day.")
        return

    df = mu.load_trade_book()
    if df.empty:
        print("[tracker] Trade book is empty - nothing to track.")
        return

    today_str = mu.today_ist_date_str()
    now_ts = mu.now_ist_ts_str()

    pending_to_fill = df[(df["Status"] == "PENDING_ENTRY") & (df["Scanned Date"] != today_str)]
    open_rows = df[df["Status"] == "OPEN"]

    tickers_needed = list(pending_to_fill["Ticker"]) + list(open_rows["Ticker"])
    if not tickers_needed:
        print("[tracker] No PENDING_ENTRY (from a prior day) or OPEN rows to process.")
        return

    print(f"[tracker] Filling {len(pending_to_fill)} pending entr(y/ies), "
          f"updating {len(open_rows)} open trade(s)")
    bars = fetch_latest_bars(tickers_needed)

    filled, closed, updated = 0, 0, 0

    for idx in pending_to_fill.index:
        ticker = df.at[idx, "Ticker"]
        try:
            bar = bars.get(ticker)
            if bar is None:
                print(f"[tracker] {ticker}: NO DATA returned at all this run "
                      "(see [scanner]-prefixed lines above for why - e.g. Yahoo "
                      "Finance data lag, or too few clean bars) - leaving PENDING_ENTRY, "
                      "will retry next run")
                continue
            if bar["bar_date"] != today_str:
                print(f"[tracker] {ticker}: data returned but latest bar is dated "
                      f"{bar['bar_date']}, not today ({today_str}) - Yahoo Finance "
                      "likely hasn't posted today's candle for this ticker yet - "
                      "leaving PENDING_ENTRY, will retry next run")
                continue

            entry_price = bar["open"]
            stop = float(df.at[idx, "Stop Loss"])
            target1 = float(df.at[idx, "Target 1"])

            # ROBUSTNESS FIX: rows written by an older version of scanner.py
            # (before "Signal Reference Close" existed) have this field
            # blank. float("") crashes - and since that crash happened
            # BEFORE save_trade_book() was ever reached, it was silently
            # killing the ENTIRE run, blocking every other row too, for as
            # many days as the bad row sat there. Signal Reference Close is
            # informational only (used for Entry Gap %), so a missing value
            # should degrade to "gap unknown", never crash the whole job.
            raw_ref_close = df.at[idx, "Signal Reference Close"]
            if raw_ref_close not in ("", None) and str(raw_ref_close).strip() != "":
                signal_ref_close = float(raw_ref_close)
                entry_gap_pct_str = f"{(entry_price - signal_ref_close) / signal_ref_close * 100.0:+.2f}"
            else:
                entry_gap_pct_str = "N/A (legacy row, no reference price recorded)"

            df.at[idx, "Entry Date"] = today_str
            df.at[idx, "Entry Price"] = f"{entry_price:.2f}"
            df.at[idx, "Entry Gap %"] = entry_gap_pct_str
            df.at[idx, "Status"] = "OPEN"
            df.at[idx, "Days Held"] = "0"
            df.at[idx, "Last Tracked"] = now_ts
            filled += 1

            # GAP-RISK SAFETY CHECK (added after code review): if the open
            # itself already gapped through the stop, a real stop order would
            # NOT have filled at the stop price - it fills at whatever price
            # is available, i.e. the open. Treat this as an immediate,
            # worse-than-planned loss rather than pretending you got the
            # stop price. This is separate from an ordinary same-day stop
            # hit (where price opens fine and later trades down to the stop).
            gapped_through_stop = bar["open"] <= stop

            if gapped_through_stop:
                exit_price, exit_reason = bar["open"], "Stoploss Hit (Gap)"
            else:
                # same-day exit check using today's realized High/Low/Close,
                # priority: Stop first (conservative tie-break, matches backtest)
                hit_stop = bar["low"] <= stop
                hit_target = bar["high"] >= target1
                hit_ema_break = bar["close"] < bar["ema50"]

                exit_price = None
                exit_reason = None
                if hit_stop:
                    exit_price, exit_reason = stop, "Stoploss Hit"
                elif hit_target:
                    exit_price, exit_reason = target1, "Target1 Hit"
                elif hit_ema_break:
                    exit_price, exit_reason = bar["close"], "EMA50 Breakout"

            current_price = exit_price if exit_price is not None else bar["close"]
            pnl, pnl_pct, net_pnl_pct = compute_pnl(entry_price, current_price)
            df.at[idx, "Current Price"] = f"{current_price:.2f}"
            df.at[idx, "PnL"] = f"{pnl:.2f}"
            df.at[idx, "PnL %"] = f"{pnl_pct:.2f}"
            df.at[idx, "PnL % (Net of 0.3% Cost)"] = f"{net_pnl_pct:.2f}"

            if exit_reason:
                df.at[idx, "Status"] = "CLOSED"
                df.at[idx, "Exit Date"] = today_str
                df.at[idx, "Exit Price"] = f"{exit_price:.2f}"
                df.at[idx, "Exit Reason"] = exit_reason
                closed += 1
        except Exception as e:
            # FAULT ISOLATION: one malformed/unexpected row must never be
            # able to kill the whole run and block every other row's
            # update. Log it clearly and move on - this is exactly the bug
            # that caused 3 days of silently-failed tracker runs.
            print(f"[tracker] ERROR processing PENDING_ENTRY row for {ticker}: {e} "
                  "- skipping this row, continuing with the rest")
            continue

    for idx in open_rows.index:
        ticker = df.at[idx, "Ticker"]
        try:
            bar = bars.get(ticker)
            if bar is None:
                print(f"[tracker] {ticker}: NO DATA returned at all this run - "
                      "leaving position as-is, will retry next run")
                continue
            if bar["bar_date"] != today_str:
                print(f"[tracker] {ticker}: data returned but latest bar is dated "
                      f"{bar['bar_date']}, not today ({today_str}) - leaving "
                      "position as-is, will retry next run")
                continue

            entry_price = float(df.at[idx, "Entry Price"])
            stop = float(df.at[idx, "Stop Loss"])
            target1 = float(df.at[idx, "Target 1"])
            days_held = int(float(df.at[idx, "Days Held"])) + 1

            hit_stop = bar["low"] <= stop
            hit_target = bar["high"] >= target1
            hit_ema_break = bar["close"] < bar["ema50"]
            hit_time_exit = days_held >= MAX_HOLD_DAYS

            exit_price = None
            exit_reason = None
            if hit_stop:
                exit_price, exit_reason = stop, "Stoploss Hit"
            elif hit_target:
                exit_price, exit_reason = target1, "Target1 Hit"
            elif hit_ema_break:
                exit_price, exit_reason = bar["close"], "EMA50 Breakout"
            elif hit_time_exit:
                exit_price, exit_reason = bar["close"], "Time Exit"

            current_price = exit_price if exit_price is not None else bar["close"]
            pnl, pnl_pct, net_pnl_pct = compute_pnl(entry_price, current_price)

            df.at[idx, "Days Held"] = str(days_held)
            df.at[idx, "Current Price"] = f"{current_price:.2f}"
            df.at[idx, "PnL"] = f"{pnl:.2f}"
            df.at[idx, "PnL %"] = f"{pnl_pct:.2f}"
            df.at[idx, "PnL % (Net of 0.3% Cost)"] = f"{net_pnl_pct:.2f}"
            df.at[idx, "Last Tracked"] = now_ts
            updated += 1

            if exit_reason:
                df.at[idx, "Status"] = "CLOSED"
                df.at[idx, "Exit Date"] = today_str
                df.at[idx, "Exit Price"] = f"{exit_price:.2f}"
                df.at[idx, "Exit Reason"] = exit_reason
                closed += 1
        except Exception as e:
            print(f"[tracker] ERROR processing OPEN row for {ticker}: {e} "
                  "- skipping this row, continuing with the rest")
            continue

    mu.save_trade_book(df)
    print(f"[tracker] Done. Filled {filled} new entr(y/ies), updated {updated} open "
          f"trade(s), closed {closed} trade(s) this run.")


if __name__ == "__main__":
    sys.exit(main())
