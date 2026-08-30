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

REFERENCE_TICKERS = ["RELIANCE.NS", "TCS.NS", "HDFCBANK.NS"]  # see is_fresh_trading_day()

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
    """Auto-detect approach: today counts as a fresh NSE trading day if
    ANY of a few highly-liquid large-cap stocks has a daily bar dated
    today (IST). If none do, today is NOT a fresh trading day and the
    caller should skip the whole job.

    ORIGINALLY this checked only the raw ^NSEI index quote. That turned
    out to be a real bug, not just a theoretical risk: on a live run, the
    index symbol's data lagged a full calendar day behind actual equity
    data on Yahoo Finance - individual stocks (including the ones this
    scanner trades) had a fresh bar for a real trading day, while ^NSEI
    still showed the PRIOR day's bar. Because this check runs BEFORE
    anything else, that false negative caused the scanner to skip an
    entire real trading day - not just delay it, but silently lose any
    setups that confirmed that day, since the scanner only ever checks
    "did today's candle just confirm a setup", with no historical catch-up.
    That's a materially worse failure mode than a late/lagging individual
    ticker (which just retries next run with nothing lost), so this check
    now uses actual liquid equities - the same kind of data already proven
    reliable by every other successful run - instead of a pure index quote,
    and requires only ONE of several references to agree, so one symbol's
    data hiccup on any given day can't single-handedly cause a lost day."""
    for ticker in REFERENCE_TICKERS:
        try:
            df = yf.download(ticker, period="5d", interval="1d",
                              progress=False, auto_adjust=True)
            if df is None or df.empty:
                print(f"[market_utils] {ticker}: no data returned, trying next reference")
                continue
            last_bar_date = pd.Timestamp(df.index[-1]).strftime(DATE_FMT)
            today = today_ist_date_str()
            if last_bar_date == today:
                return True
            print(f"[market_utils] {ticker}: last available bar is {last_bar_date}, "
                  f"today (IST) is {today} - trying next reference before concluding "
                  "today isn't a fresh trading day")
        except Exception as e:
            print(f"[market_utils] {ticker}: freshness check failed ({e}), trying next reference")
            continue

    print(f"[market_utils] None of {REFERENCE_TICKERS} show a bar dated today "
          f"({today_ist_date_str()}) - treating today as NOT a fresh trading day. Skipping.")
    return False


def load_trade_book() -> pd.DataFrame:
    if os.path.exists(TRADE_BOOK_PATH):
        # keep_default_na=False: dtype=str alone does NOT stop pandas from
        # converting blank fields to NaN on read (NA-detection runs before
        # the dtype cast) - this schema treats every column as string-typed
        # where blank means "not yet set", so a blank field must stay ''
        # after reload, not silently become float NaN. Bug found while
        # testing the "Current Price populated at scan time" change, but
        # it was latent everywhere in the trade book before that too.
        df = pd.read_csv(TRADE_BOOK_PATH, dtype=str, keep_default_na=False)
        for col in COLUMNS:
            if col not in df.columns:
                df[col] = ""
        return df[COLUMNS]
    return pd.DataFrame(columns=COLUMNS)


def save_trade_book(df: pd.DataFrame):
    os.makedirs(os.path.dirname(TRADE_BOOK_PATH), exist_ok=True)
    df[COLUMNS].to_csv(TRADE_BOOK_PATH, index=False)
