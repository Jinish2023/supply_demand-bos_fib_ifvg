#!/usr/bin/env python3
"""
market_utils.py
============================================================================
Shared helpers: (1) "is today actually a trading day" freshness check
(auto-detect approach you chose - no static holiday calendar to maintain),
(2) the trade book CSV schema, read/write with a STRICT, LOCKED date format
so the scanner and tracker can never disagree on what a date string means.

DATE FORMAT: every date column in the trade book uses ISO 8601 (YYYY-MM-DD),
e.g. "2026-05-12" -- never "05-12-2026" or "12-05-2026". ISO 8601 is used
specifically because it has exactly one possible reading (year first, always
4 digits; month always 2 digits; day always 2 digits) and sorts correctly
as plain text. Timestamp columns (Last Tracked) use "YYYY-MM-DD HH:MM:SS"
in IST. This format is enforced everywhere via the DATE_FMT / TS_FMT
constants below - never build a date string by hand elsewhere in this repo.
============================================================================
"""

import os
from datetime import datetime, timedelta, timezone

import pandas as pd
import yfinance as yf

IST = timezone(timedelta(hours=5, minutes=30))
DATE_FMT = "%Y-%m-%d"
TS_FMT = "%Y-%m-%d %H:%M:%S"

REFERENCE_INDEX = "^NSEI"  # Nifty 50 index, used only for the freshness check

TRADE_BOOK_PATH = os.path.join(os.path.dirname(__file__), "data", "trade_book.csv")

COLUMNS = [
    "Ticker",
    "Strategy",
    "Scanned Date",              # immutable, ISO date, set once by scanner
    "Status",                    # PENDING_ENTRY | OPEN | CLOSED
    "Entry Date",                # ISO date, set once tracker fills the entry
    "Entry Price",
    "Signal Reference Close",    # confirmation candle's close - what Stop/Target1/Target2 were sized off
    "Entry Gap %",               # (Entry Price - Signal Reference Close) / Signal Reference Close * 100
    "Stop Loss",
    "Target 1",
    "Target 2 (Informational)",  # NOT a real exit trigger - see README
    "EMA50 At Entry",
    "Current Price",
    "PnL",
    "PnL %",
    "PnL % (Net of 0.3% Cost)",
    "Days Held",
    "Exit Date",                 # ISO date
    "Exit Price",
    "Exit Reason",               # EMA50 Breakout | Time Exit | Stoploss Hit | Target1 Hit
    "Last Tracked",              # ISO timestamp
]

EXIT_REASONS = ["EMA50 Breakout", "Time Exit", "Stoploss Hit", "Stoploss Hit (Gap)", "Target1 Hit"]


def today_ist() -> datetime:
    return datetime.now(IST)


def today_ist_date_str() -> str:
    return today_ist().strftime(DATE_FMT)


def now_ist_ts_str() -> str:
    return today_ist().strftime(TS_FMT)


def is_fresh_trading_day() -> bool:
    """Auto-detect approach: download the Nifty 50 index's latest daily
    bar and check whether its date matches today's real IST calendar
    date. If it doesn't (weekend, NSE holiday, or data lag), today is NOT
    a fresh trading day and the caller should skip the whole job."""
    try:
        df = yf.download(REFERENCE_INDEX, period="5d", interval="1d",
                          progress=False, auto_adjust=True)
        if df is None or df.empty:
            print("[market_utils] Could not fetch reference index - skipping run as a precaution")
            return False
        last_bar_date = pd.Timestamp(df.index[-1]).strftime(DATE_FMT)
        today = today_ist_date_str()
        if last_bar_date == today:
            return True
        print(f"[market_utils] Not a fresh trading day: last available bar is "
              f"{last_bar_date}, today (IST) is {today}. Skipping.")
        return False
    except Exception as e:
        print(f"[market_utils] Freshness check failed ({e}) - skipping run as a precaution")
        return False


def load_trade_book() -> pd.DataFrame:
    if os.path.exists(TRADE_BOOK_PATH):
        df = pd.read_csv(TRADE_BOOK_PATH, dtype=str)
        for col in COLUMNS:
            if col not in df.columns:
                df[col] = ""
        return df[COLUMNS]
    return pd.DataFrame(columns=COLUMNS)


def save_trade_book(df: pd.DataFrame):
    os.makedirs(os.path.dirname(TRADE_BOOK_PATH), exist_ok=True)
    df[COLUMNS].to_csv(TRADE_BOOK_PATH, index=False)
