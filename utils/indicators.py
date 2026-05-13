"""
utils/indicators.py — Reusable technical indicator helpers.
Every module imports from here; we never duplicate TA logic.
"""

import pandas as pd
import numpy as np


def compute_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """
    Classic Wilder RSI.
    series — closing prices as a pd.Series
    Returns a pd.Series of RSI values (0–100).
    """
    delta = series.diff()
    gain  = delta.clip(lower=0)
    loss  = -delta.clip(upper=0)

    # Wilder smoothing = EMA with alpha = 1/period
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()

    rs  = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi


def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """
    Average True Range.
    df must have 'high', 'low', 'close' columns.
    Returns a pd.Series of ATR values.
    """
    high, low, prev_close = df["high"], df["low"], df["close"].shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def compute_ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential Moving Average."""
    return series.ewm(span=period, adjust=False).mean()


def compute_sma(series: pd.Series, period: int) -> pd.Series:
    """Simple Moving Average."""
    return series.rolling(window=period).mean()


def compute_bollinger_bands(series: pd.Series, period: int = 20, std_dev: float = 2.0):
    """
    Bollinger Bands.
    Returns (upper_band, middle_band, lower_band) as pd.Series.
    """
    middle = compute_sma(series, period)
    std    = series.rolling(window=period).std()
    upper  = middle + std_dev * std
    lower  = middle - std_dev * std
    return upper, middle, lower


def compute_macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    """
    MACD line, Signal line, Histogram.
    Returns (macd_line, signal_line, histogram) as pd.Series.
    """
    ema_fast   = compute_ema(series, fast)
    ema_slow   = compute_ema(series, slow)
    macd_line  = ema_fast - ema_slow
    signal_line = compute_ema(macd_line, signal)
    histogram   = macd_line - signal_line
    return macd_line, signal_line, histogram


def compute_kdj(
    df: pd.DataFrame,
    period:   int = 9,
    k_smooth: int = 3,
    d_smooth: int = 3,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """
    KDJ (Stochastic Oscillator variant) — standard (9, 3, 3) parameterisation.

    Formula:
      RSV  = (close − lowest_low(period)) / (highest_high(period) − lowest_low(period)) × 100
      K[t] = (1 − 1/k_smooth) × K[t−1] + (1/k_smooth) × RSV[t]   init K₀ = 50
      D[t] = (1 − 1/d_smooth) × D[t−1] + (1/d_smooth) × K[t]     init D₀ = 50
      J    = 3K − 2D   (can exceed 0–100 range, that's normal)

    df must have 'high', 'low', 'close' columns.
    Returns (K, D, J) as pd.Series aligned to df.index.
    """
    low_min  = df["low"].rolling(period, min_periods=period).min()
    high_max = df["high"].rolling(period, min_periods=period).max()
    denom    = high_max - low_min

    with np.errstate(invalid="ignore", divide="ignore"):
        raw_rsv = (df["close"].values - low_min.values) / denom.values * 100.0

    # Replace NaN (warm-up rows) and zero-range bars with 50 (neutral initialisation)
    rsv_arr = np.where(np.isnan(raw_rsv) | (denom.values == 0), 50.0, raw_rsv)

    k_alpha = 1.0 / k_smooth
    d_alpha = 1.0 / d_smooth

    k_arr = np.full(len(rsv_arr), 50.0)
    d_arr = np.full(len(rsv_arr), 50.0)

    for i in range(1, len(rsv_arr)):
        k_arr[i] = (1 - k_alpha) * k_arr[i - 1] + k_alpha * rsv_arr[i]
        d_arr[i] = (1 - d_alpha) * d_arr[i - 1] + d_alpha * k_arr[i]

    k = pd.Series(k_arr, index=df.index)
    d = pd.Series(d_arr, index=df.index)
    j = 3.0 * k - 2.0 * d
    return k, d, j


def volume_spike(volume: pd.Series, lookback: int = 20) -> pd.Series:
    """
    Returns volume / rolling_average so you can threshold at e.g. 2.0×.
    A value of 2.5 means today's volume is 2.5× the 20-day average.
    """
    avg = volume.rolling(window=lookback).mean()
    return volume / avg.replace(0, np.nan)
