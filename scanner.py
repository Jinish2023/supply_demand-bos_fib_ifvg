#!/usr/bin/env python3
"""
scanner.py  -  runs daily at ~4:00 PM IST (after NSE close)
============================================================================
Scans the current NSE-500 universe for fresh supply_demand / bos_fib_ifvg
signals (the confirmation candle just closed TODAY) and appends new
PENDING_ENTRY rows to data/trade_book.csv.

The scanner NEVER sets Entry Price / Entry Date - it only records that a
setup was confirmed today. tracker.py fills the entry using the NEXT
trading day's Open (see README for why).

Skips entirely if today isn't a real NSE trading day (auto-detected via
market_utils.is_fresh_trading_day()).
============================================================================
"""

import sys
import time

import pandas as pd
import yfinance as yf

import market_utils as mu
import universe as uni
from strategies import add_indicators, scan_supply_demand, scan_bos_fib_ifvg

RR1 = 2.0        # Target 1 reward:risk, matches the backtest default
RR2 = 3.0        # Target 2 (informational only) reward:risk
LOOKBACK_PERIOD = "2y"   # plenty of context for swing/channel/FVG lookbacks
CHUNK_SIZE = 50
MIN_BARS_REQUIRED = 120


def batched(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def download_chunk(tickers, period=LOOKBACK_PERIOD):
    """Batch-download a chunk of tickers in one yfinance call, returning
    {ticker: DataFrame}. Falls back to skipping tickers that fail."""
    out = {}
    try:
        raw = yf.download(tickers, period=period, interval="1d",
                           group_by="ticker", threads=True, progress=False,
                           auto_adjust=True)
    except Exception as e:
        print(f"[scanner] Chunk download failed entirely ({e}), skipping chunk")
        return out

    for t in tickers:
        try:
            if len(tickers) == 1:
                df = raw
            else:
                df = raw[t] if t in raw.columns.get_level_values(0) else None
            if df is None or df.empty:
                continue
            df = df.rename(columns=str.title)
            df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
            if len(df) >= MIN_BARS_REQUIRED:
                out[t] = df
        except Exception:
            continue
    return out


def main():
    if not mu.is_fresh_trading_day():
        print("[scanner] Skipping - not a fresh trading day.")
        return

    tickers = uni.get_nse500_tickers()
    print(f"[scanner] Universe: {len(tickers)} tickers")

    trade_book = mu.load_trade_book()
    active_pairs = set(
        zip(trade_book.loc[trade_book["Status"].isin(["PENDING_ENTRY", "OPEN"]), "Ticker"],
            trade_book.loc[trade_book["Status"].isin(["PENDING_ENTRY", "OPEN"]), "Strategy"])
    )
    print(f"[scanner] {len(active_pairs)} ticker/strategy pairs already active - will skip those")

    today_str = mu.today_ist_date_str()
    new_rows = []
    processed = 0
    t0 = time.time()

    for chunk in batched(tickers, CHUNK_SIZE):
        data = download_chunk(chunk)
        for ticker, df in data.items():
            processed += 1
            df = add_indicators(df)
            if len(df) < MIN_BARS_REQUIRED:
                continue

            for scan_fn, strategy_name in [(scan_supply_demand, "supply_demand"),
                                            (scan_bos_fib_ifvg, "bos_fib_ifvg")]:
                if (ticker, strategy_name) in active_pairs:
                    continue
                try:
                    if strategy_name == "supply_demand":
                        sig = scan_fn(df, ticker, RR1, RR2)
                    else:
                        sig = scan_fn(df, ticker, RR1, RR2)
                except Exception as e:
                    print(f"[scanner] {ticker}/{strategy_name} error: {e}")
                    continue
                if sig is None:
                    continue

                sig_date_str = pd.Timestamp(sig.signal_date).strftime(mu.DATE_FMT)
                if sig_date_str != today_str:
                    continue  # not a fresh-today signal, ignore

                new_rows.append({
                    "Ticker": ticker,
                    "Strategy": strategy_name,
                    "Scanned Date": today_str,
                    "Status": "PENDING_ENTRY",
                    "Entry Date": "",
                    "Entry Price": "",
                    "Stop Loss": f"{sig.stop:.2f}",
                    "Target 1": f"{sig.target1:.2f}",
                    "Target 2 (Informational)": f"{sig.target2:.2f}",
                    "EMA50 At Entry": f"{sig.ema50_at_signal:.2f}",
                    "Current Price": "",
                    "PnL": "",
                    "PnL %": "",
                    "PnL % (Net of 0.3% Cost)": "",
                    "Days Held": "0",
                    "Exit Date": "",
                    "Exit Price": "",
                    "Exit Reason": "",
                    "Last Tracked": "",
                })
                active_pairs.add((ticker, strategy_name))  # avoid double-adding within this same run

        if processed % 100 == 0:
            print(f"[scanner] processed {processed}/{len(tickers)} ({time.time()-t0:.0f}s)")

    print(f"[scanner] Done scanning {processed} tickers in {time.time()-t0:.0f}s. "
          f"{len(new_rows)} new signal(s) found.")

    if new_rows:
        trade_book = pd.concat([trade_book, pd.DataFrame(new_rows)], ignore_index=True)
        mu.save_trade_book(trade_book)
        print(f"[scanner] Trade book updated: {mu.TRADE_BOOK_PATH}")
    else:
        print("[scanner] No new signals - trade book unchanged.")


if __name__ == "__main__":
    sys.exit(main())
