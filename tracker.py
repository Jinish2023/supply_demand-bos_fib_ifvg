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


def fetch_latest_bars(tickers):
    """Returns {ticker: (open, high, low, close, ema50, bar_date_str)} for
    today's bar, for the given tickers."""
    result = {}
    for chunk in batched(sorted(set(tickers)), CHUNK_SIZE):
        data = download_chunk(chunk, period="1y")
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
        bar = bars.get(ticker)
        if bar is None or bar["bar_date"] != today_str:
            print(f"[tracker] {ticker}: no fresh bar today, leaving PENDING_ENTRY")
            continue

        entry_price = bar["open"]
        stop = float(df.at[idx, "Stop Loss"])
        target1 = float(df.at[idx, "Target 1"])

        df.at[idx, "Entry Date"] = today_str
        df.at[idx, "Entry Price"] = f"{entry_price:.2f}"
        df.at[idx, "Status"] = "OPEN"
        df.at[idx, "Days Held"] = "0"
        df.at[idx, "Last Tracked"] = now_ts
        filled += 1

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

    for idx in open_rows.index:
        ticker = df.at[idx, "Ticker"]
        bar = bars.get(ticker)
        if bar is None or bar["bar_date"] != today_str:
            print(f"[tracker] {ticker}: no fresh bar today, leaving as-is")
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

    mu.save_trade_book(df)
    print(f"[tracker] Done. Filled {filled} new entr(y/ies), updated {updated} open "
          f"trade(s), closed {closed} trade(s) this run.")


if __name__ == "__main__":
    sys.exit(main())
