#!/usr/bin/env python3
"""
catchup_run.py  -  ONE-OFF recovery tool, not part of the regular pipeline
============================================================================
Use this ONLY when a real trading day's scheduled scanner/tracker runs
were both missed entirely (not the normal "one ticker had stale data"
case, which already self-heals via retries - this is for "the whole day's
runs never happened"). It re-plays scanner.py and tracker.py exactly as
they would have run on the last available trading day, by determining
that day's date from real market data and temporarily making
market_utils.today_ist_date_str() / now_ist_ts_str() / is_fresh_trading_day()
report that date instead of the real current date - for the duration of
this script's process only. scanner.py and tracker.py are NOT modified
and are NOT run with any different logic; they call mu.today_ist_date_str()
etc via the module object at call time, so this override applies cleanly
without touching either file.

WHY THIS IS SAFE:
  - Determines the "as of" date from real closed-market data (a stock's
    last available bar), not a guess or a hardcoded date.
  - scanner.py's own dedup (active_pairs) means re-scanning a day that
    was ALREADY partially scanned just finds what's left, not duplicates.
  - tracker.py's own PENDING_ENTRY/OPEN logic is unchanged - it fills
    entries and updates positions exactly as it would have on the real day.
  - The underlying price data for a closed trading day doesn't change
    (short of rare data-provider corrections), so running this today
    produces the same result as if the real Friday-evening runs had
    succeeded.

AFTER RUNNING THIS ONCE: delete this file (or just don't schedule it) and
go back to the regular scheduled scanner.py / tracker.py - this script is
a manual recovery tool, not a replacement for the normal pipeline.

USAGE
  python catchup_run.py
============================================================================
"""

import sys
from unittest.mock import patch

import pandas as pd

import market_utils as mu

REFERENCE_TICKERS = mu.REFERENCE_TICKERS


def determine_last_trading_day() -> str:
    """Same logic as market_utils.is_fresh_trading_day(), but instead of
    comparing to real-today and returning True/False, it just returns the
    actual last available trading date - the day we're catching up on."""
    import yfinance as yf
    for ticker in REFERENCE_TICKERS:
        try:
            df = yf.download(ticker, period="5d", interval="1d",
                              progress=False, auto_adjust=True)
            if df is None or df.empty:
                continue
            last_bar_date = pd.Timestamp(df.index[-1]).strftime(mu.DATE_FMT)
            print(f"[catchup] {ticker}: last available trading day is {last_bar_date}")
            return last_bar_date
        except Exception as e:
            print(f"[catchup] {ticker}: failed to check ({e}), trying next reference")
            continue
    raise RuntimeError("Could not determine the last trading day from any reference ticker - "
                        "check your network/yfinance access and try again.")


def main():
    as_of_date = determine_last_trading_day()
    real_today = mu.today_ist_date_str()
    print()
    print(f"[catchup] Real today (IST) is {real_today}. This run will pretend "
          f"today is {as_of_date} (the last real trading day) and re-play "
          "scanner.py then tracker.py exactly as they'd have run that evening.")
    print(f"[catchup] This is a ONE-OFF recovery run - after this, go back to the "
          "normal scheduled scanner.py / tracker.py.")
    print()

    fake_now_ts = f"{as_of_date} 20:00:00"

    with patch("market_utils.is_fresh_trading_day", return_value=True), \
         patch("market_utils.today_ist_date_str", return_value=as_of_date), \
         patch("market_utils.now_ist_ts_str", return_value=fake_now_ts):

        print("=" * 80)
        print(f"[catchup] Running SCANNER as-of {as_of_date}")
        print("=" * 80)
        import scanner
        scanner.main()

        print()
        print("=" * 80)
        print(f"[catchup] Running TRACKER as-of {as_of_date}")
        print("=" * 80)
        import tracker
        tracker.main()

    print()
    print("[catchup] Done. Review data/trade_book.csv for any new signals and "
          "updated positions from the missed day. Commit/push as usual, then "
          "delete this script and resume the normal schedule.")


if __name__ == "__main__":
    sys.exit(main())
