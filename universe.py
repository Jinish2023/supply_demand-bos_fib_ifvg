#!/usr/bin/env python3
"""
universe.py
============================================================================
Fetches the CURRENT NSE-500 constituent list (not delisted/stale names).

Priority order:
  1. data/nse500_symbols.csv in this repo, IF you've added/pinned one
     yourself - this is the most reliable option since you control it
     directly and can update it whenever NSE rebalances the index
     (NSE typically rebalances Nifty 500 semi-annually, March/September).
  2. NSE's own official archive CSV (most authoritative when reachable -
     NSE sometimes blocks non-browser requests, so this can fail
     intermittently even though it's the best source when it works).
  3. Community-maintained GitHub mirrors (updated periodically by third
     parties, not officially guaranteed current).
  4. Hardcoded fallback sample (~20 large, liquid, unlikely-to-be-delisted
     names) so the pipeline never dies with zero tickers.

NOTE: None of these guarantee 100% current membership at every moment.
NSE rebalances the index periodically; a stock added/removed between your
last list refresh and today won't be reflected until you re-fetch. For a
production system you'd want to re-validate this list on some cadence
(e.g. monthly) rather than assume it's always perfectly current.
============================================================================
"""

import io
import os

import pandas as pd
import requests

NSE_ARCHIVE_URL = "https://archives.nseindia.com/content/indices/ind_nifty500list.csv"
GITHUB_MIRRORS = [
    "https://raw.githubusercontent.com/Hpareek07/NSEData/master/ind_nifty500list.csv",
    "https://raw.githubusercontent.com/kprohith/nse-stock-analysis/master/ind_nifty500list.csv",
]
LOCAL_PINNED_LIST = os.path.join(os.path.dirname(__file__), "data", "nse500_symbols.csv")

FALLBACK_SAMPLE = [
    "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "INFY.NS", "ICICIBANK.NS",
    "HINDUNILVR.NS", "ITC.NS", "SBIN.NS", "BHARTIARTL.NS", "KOTAKBANK.NS",
    "LT.NS", "AXISBANK.NS", "BAJFINANCE.NS", "ASIANPAINT.NS", "MARUTI.NS",
    "TITAN.NS", "SUNPHARMA.NS", "ULTRACEMCO.NS", "WIPRO.NS", "NESTLEIND.NS",
]

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


def _parse_symbol_df(df: pd.DataFrame):
    for col in ["Symbol", "symbol", "SYMBOL", "Ticker", "ticker"]:
        if col in df.columns:
            syms = df[col].dropna().astype(str).str.strip().str.upper().tolist()
            syms = [s if s.endswith(".NS") else s + ".NS" for s in syms]
            return sorted(set(syms))
    return []


def get_nse500_tickers() -> list:
    # 1. locally pinned list, if present
    if os.path.exists(LOCAL_PINNED_LIST):
        try:
            df = pd.read_csv(LOCAL_PINNED_LIST)
            syms = _parse_symbol_df(df)
            if len(syms) >= 100:
                print(f"[universe] Loaded {len(syms)} symbols from pinned {LOCAL_PINNED_LIST}")
                return syms
        except Exception as e:
            print(f"[universe] Failed to read pinned list: {e}")

    # 2. official NSE archive
    try:
        r = requests.get(NSE_ARCHIVE_URL, headers=HEADERS, timeout=15)
        r.raise_for_status()
        df = pd.read_csv(io.StringIO(r.text))
        syms = _parse_symbol_df(df)
        if len(syms) >= 400:
            print(f"[universe] Loaded {len(syms)} symbols from official NSE archive")
            return syms
    except Exception as e:
        print(f"[universe] NSE archive fetch failed: {e}")

    # 3. community mirrors
    for url in GITHUB_MIRRORS:
        try:
            df = pd.read_csv(url)
            syms = _parse_symbol_df(df)
            if len(syms) >= 400:
                print(f"[universe] Loaded {len(syms)} symbols from mirror {url}")
                return syms
        except Exception as e:
            print(f"[universe] Mirror fetch failed ({url}): {e}")

    # 4. fallback
    print(f"[universe] All sources failed - using {len(FALLBACK_SAMPLE)}-ticker fallback sample")
    return FALLBACK_SAMPLE
