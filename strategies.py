#!/usr/bin/env python3
"""
strategies.py
============================================================================
Shared strategy-detection logic for supply_demand and bos_fib_ifvg.

IMPORTANT: This is copied verbatim (not re-derived) from the already-run,
already-validated three_strategy_backtest.py, specifically the two
strategies you chose to move forward with. Using the exact same detection
functions here guarantees the scanner finds the same kind of setups your
15-year NSE500 backtest already tested - no drift between "what was
backtested" and "what the live scanner looks for".

Fidelity notes (same as the backtest file):
  - supply_demand: HIGH fidelity to @KevOfMomentum's posts.
  - bos_fib_ifvg: MEDIUM-HIGH fidelity to mr.akash_genius_trader's BOS +
    Fibonacci OTE (0.618-0.786) + Fair Value Gap confluence post. BOS and
    FVG have precise, standard definitions in SMC/ICT so little guesswork
    was needed; "POI+IFVG confluence" = any overlap between the OTE zone
    and a detected FVG.

One deliberate change from the backtest for LIVE/SCAN use: the backtest's
entry_price was the confirmation candle's Close (used purely for position
sizing/backtesting math). In live scanning, we still use that Close to size
the Stop and Target1/Target2 (since that's the price level the setup was
actually confirmed at), but the REAL fill price used by the tracker is the
NEXT trading day's Open (see tracker.py) - because by the time the scanner
runs (4:00 PM, after close), you could never have actually gotten that
day's open. This means your live stop/target distances are computed off a
slightly different reference price than your actual fill - a small,
unavoidable gap-risk caveat worth knowing about, not a bug.
============================================================================
"""

from dataclasses import dataclass
from typing import List, Optional

import numpy as np
import pandas as pd


@dataclass
class Signal:
    strategy: str
    ticker: str
    signal_date: pd.Timestamp     # the confirmation candle's date (= Scanned Date)
    confirm_close: float          # confirmation candle's close (reference price for sizing)
    stop: float
    target1: float
    target2: float                # informational only - see README
    ema50_at_signal: float


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["EMA50"] = df["Close"].ewm(span=50, adjust=False).mean()
    df["AvgRange"] = (df["High"] - df["Low"]).rolling(20).mean()
    return df


def find_swing_points(df: pd.DataFrame, k: int = 3):
    """Fractal swing high/low detector: bar i is a swing high if its High
    is the max of the window [i-k, i+k], similarly for swing lows."""
    highs = df["High"].values
    lows = df["Low"].values
    n = len(df)
    is_swing_high = np.zeros(n, dtype=bool)
    is_swing_low = np.zeros(n, dtype=bool)
    for i in range(k, n - k):
        window_h = highs[i - k:i + k + 1]
        window_l = lows[i - k:i + k + 1]
        if highs[i] == window_h.max() and np.argmax(window_h) == k:
            is_swing_high[i] = True
        if lows[i] == window_l.min() and np.argmin(window_l) == k:
            is_swing_low[i] = True
    return pd.Series(is_swing_high, index=df.index), pd.Series(is_swing_low, index=df.index)


def find_bullish_fvgs(df: pd.DataFrame, start_idx: int, end_idx: int):
    """Standard 3-candle bullish Fair Value Gap: a gap exists at candle i
    if Low[i+1] > High[i-1]."""
    fvgs = []
    for i in range(max(1, start_idx), min(end_idx, len(df) - 1)):
        prior_high = df["High"].iloc[i - 1]
        next_low = df["Low"].iloc[i + 1]
        if next_low > prior_high:
            fvgs.append((i, prior_high, next_low))
    return fvgs


# ============================================================================
# STRATEGY 1: SUPPLY / DEMAND BREAK -> PULLBACK -> CONFIRMATION
# ============================================================================
def scan_supply_demand(df: pd.DataFrame, ticker: str, rr: float,
                        rr2: float, range_len: int = 6, tightness: float = 0.035,
                        breakout_mult: float = 1.5, pullback_window: int = 6) -> Optional[Signal]:
    """Checks whether the LAST bar in df is a fresh confirmation/entry
    signal (i.e. today's candle just confirmed a supply_demand setup)."""
    n = len(df)
    target_last_idx = n - 1
    i = range_len
    while i < n - 1:
        window = df.iloc[i - range_len:i]
        range_high = window["High"].max()
        range_low = window["Low"].min()
        closes = window["Close"]
        tight = (closes.std() / closes.mean()) < tightness if closes.mean() else False

        breakout_bar = df.iloc[i]
        avg_range = df["AvgRange"].iloc[i]
        strong_candle = (breakout_bar["High"] - breakout_bar["Low"]) > breakout_mult * avg_range \
            if pd.notna(avg_range) and avg_range > 0 else False
        breaks_out = breakout_bar["Close"] > range_high

        if tight and strong_candle and breaks_out:
            zone_top, zone_bottom = range_high, range_low
            entry_signal = None
            for j in range(i + 1, min(i + 1 + pullback_window, n)):
                bar = df.iloc[j]
                touched_zone = bar["Low"] <= zone_top
                zone_holds = bar["Low"] >= zone_bottom * 0.995
                if touched_zone and zone_holds:
                    for c in range(j, min(j + 3, n)):
                        conf_bar = df.iloc[c]
                        prior_bar = df.iloc[c - 1]
                        if conf_bar["Close"] > prior_bar["High"] and conf_bar["Close"] > conf_bar["Open"]:
                            entry_signal = c
                            break
                    break
                if bar["Close"] < zone_bottom:
                    break
                if entry_signal is not None:
                    break

            if entry_signal == target_last_idx:
                entry_bar = df.iloc[entry_signal]
                entry_price = entry_bar["Close"]
                stop = zone_bottom * 0.995
                risk = entry_price - stop
                if risk > 0:
                    target1 = entry_price + rr * risk
                    target2 = entry_price + rr2 * risk
                    return Signal(
                        strategy="supply_demand", ticker=ticker,
                        signal_date=df.index[entry_signal], confirm_close=entry_price,
                        stop=stop, target1=target1, target2=target2,
                        ema50_at_signal=entry_bar["EMA50"])
        i += 1
    return None


# ============================================================================
# STRATEGY 2: BOS + FIBONACCI OTE (0.618-0.786) + POI/FVG CONFLUENCE
# ============================================================================
def scan_bos_fib_ifvg(df: pd.DataFrame, ticker: str, rr: float, rr2: float,
                       swing_k: int = 3, ifvg_lookahead: int = 15) -> Optional[Signal]:
    """Checks whether the LAST bar in df is a fresh confirmation/entry
    signal for the BOS+Fib+FVG setup."""
    n = len(df)
    target_last_idx = n - 1
    is_high, is_low = find_swing_points(df, k=swing_k)
    swing_high_idx = [i for i in range(n) if is_high.iloc[i]]
    swing_low_idx = [i for i in range(n) if is_low.iloc[i]]

    i = swing_k * 2
    while i < n - 1:
        prior_highs = [x for x in swing_high_idx if x < i]
        prior_lows = [x for x in swing_low_idx if x < i]
        if len(prior_highs) < 1 or len(prior_lows) < 1:
            i += 1
            continue
        last_swing_high_idx = prior_highs[-1]
        last_swing_high = df["High"].iloc[last_swing_high_idx]
        bar = df.iloc[i]
        bos_confirmed = bar["Close"] > last_swing_high

        if bos_confirmed:
            leg_low_candidates = [x for x in prior_lows if x < i]
            if not leg_low_candidates:
                i += 1
                continue
            leg_low_idx = leg_low_candidates[-1]
            leg_low = df["Low"].iloc[leg_low_idx]
            leg_high = bar["High"]
            if leg_high <= leg_low:
                i += 1
                continue

            ote_top = leg_high - 0.618 * (leg_high - leg_low)
            ote_bottom = leg_high - 0.786 * (leg_high - leg_low)

            fvgs = find_bullish_fvgs(df, leg_low_idx, min(i + ifvg_lookahead, n))
            confluent_fvgs = [f for f in fvgs if not (f[2] < ote_bottom or f[1] > ote_top)]
            if not confluent_fvgs:
                i += 1
                continue

            entry_signal = None
            for j in range(i + 1, min(i + 1 + ifvg_lookahead, n)):
                rbar = df.iloc[j]
                in_zone = rbar["Low"] <= ote_top and rbar["Low"] >= ote_bottom * 0.99
                if in_zone:
                    for c in range(j, min(j + 3, n)):
                        conf_bar = df.iloc[c]
                        prior_bar = df.iloc[c - 1]
                        if conf_bar["Close"] > prior_bar["High"] and conf_bar["Close"] > conf_bar["Open"]:
                            entry_signal = c
                            break
                    break
                if rbar["Close"] < ote_bottom:
                    break

            if entry_signal == target_last_idx:
                entry_bar = df.iloc[entry_signal]
                entry_price = entry_bar["Close"]
                stop = leg_low * 0.995
                risk = entry_price - stop
                if risk > 0:
                    target1 = entry_price + rr * risk
                    target2 = entry_price + rr2 * risk
                    return Signal(
                        strategy="bos_fib_ifvg", ticker=ticker,
                        signal_date=df.index[entry_signal], confirm_close=entry_price,
                        stop=stop, target1=target1, target2=target2,
                        ema50_at_signal=entry_bar["EMA50"])
        i += 1
    return None
