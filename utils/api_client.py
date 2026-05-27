"""
utils/api_client.py — Thin wrappers around external APIs.
All modules import from here so there is only one place to update
base URLs, headers, or retry logic.
"""

import time
import requests
import pandas as pd
from typing import Optional
from config import (
    MEXC_BASE_URL, MEXC_CONTRACT_BASE_URL,
    BINANCE_BASE_URL, BINANCE_FUTURES_BASE_URL,
    COINGECKO_BASE_URL, CMC_BASE_URL, CMC_API_KEY,
)
from utils.logger import get_logger

log = get_logger(__name__)

# ── Generic HTTP helper ───────────────────────────────────────────────────────
#
# MEXC public API (Module 1)
# ─────────────────────────────────────────────────────────────────────────────
# No API key required for market data endpoints.
# Klines endpoint: GET /api/v3/klines?symbol=BTCUSDT&interval=4h&limit=200
# Response format per candle (8+ fields, we only use the first 6):
#   [0] open_time  — Unix ms
#   [1] open
#   [2] high
#   [3] low
#   [4] close
#   [5] volume
#   (remaining fields ignored — they vary by exchange / API version)
# ─────────────────────────────────────────────────────────────────────────────

def _get(url: str, params: dict = None, retries: int = 3) -> Optional[dict | list]:
    """GET request with simple retry logic. Returns parsed JSON or None."""
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, params=params, timeout=10)
            # 4xx errors are client errors (e.g. symbol not found on MEXC) —
            # retrying won't help, so bail out immediately without sleeping.
            if 400 <= resp.status_code < 500:
                return None
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            log.warning(f"[{attempt}/{retries}] Request failed: {e}")
            if attempt < retries:
                time.sleep(2 ** attempt)  # exponential back-off: 2s, 4s
    log.error(f"All retries exhausted for {url}")
    return None


# ── MEXC ─────────────────────────────────────────────────────────────────────

def get_mexc_klines(symbol: str, interval: str, limit: int = 200) -> Optional[pd.DataFrame]:
    """
    Fetch OHLCV candles from MEXC public API — no API key needed.

    symbol   — trading pair, e.g. "BTCUSDT"
    interval — candle size; MEXC supports: 1m 5m 15m 30m 60m 4h 1d 1W 1M
    limit    — number of candles to return (max 1000)

    Returns a DataFrame indexed by open_time with columns:
      open, high, low, close, volume  (all float)
    Returns None if the request fails or the data is unusable.
    """
    url  = f"{MEXC_BASE_URL}/api/v3/klines"
    data = _get(url, params={"symbol": symbol, "interval": interval, "limit": limit})

    if not data or len(data) == 0:
        log.error(f"MEXC klines returned empty data for {symbol} {interval}")
        return None

    # Parse row-by-row; only the first 6 fields are guaranteed across API versions
    rows = []
    for candle in data:
        try:
            rows.append({
                "open_time": int(candle[0]),        # Unix timestamp in milliseconds
                "open":      float(candle[1]),
                "high":      float(candle[2]),
                "low":       float(candle[3]),
                "close":     float(candle[4]),
                "volume":    float(candle[5]),
            })
        except (IndexError, ValueError, TypeError) as e:
            # Skip malformed candles rather than crashing the whole fetch
            log.warning(f"Skipping malformed MEXC candle: {candle} — {e}")
            continue

    if len(rows) < 20:
        log.error(f"Too few valid MEXC candles ({len(rows)}) for {symbol}")
        return None

    df = pd.DataFrame(rows)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df.set_index("open_time", inplace=True)

    # Candles should already be sorted oldest→newest; enforce it just in case
    df.sort_index(inplace=True)
    return df


def get_mexc_price(symbol: str) -> Optional[float]:
    """
    Fetch the latest spot price from MEXC.
    Returns the price as a float, or None on failure.
    """
    url  = f"{MEXC_BASE_URL}/api/v3/ticker/price"
    data = _get(url, params={"symbol": symbol})
    if data and "price" in data:
        return float(data["price"])
    log.error(f"MEXC price fetch failed for {symbol}: {data}")
    return None


# ── Binance ───────────────────────────────────────────────────────────────────

def get_binance_klines(symbol: str, interval: str, limit: int = 200) -> Optional[pd.DataFrame]:
    """
    Fetch OHLCV candles from Binance.
    Returns a DataFrame with columns: open, high, low, close, volume.
    symbol   — e.g. "BTCUSDT"
    interval — e.g. "4h", "1d"
    """
    url  = f"{BINANCE_BASE_URL}/api/v3/klines"
    data = _get(url, params={"symbol": symbol, "interval": interval, "limit": limit})

    if data is None:
        return None

    df = pd.DataFrame(data, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore"
    ])

    # Keep only the columns we need and cast to float
    df = df[["open_time", "open", "high", "low", "close", "volume"]].copy()
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    df.set_index("open_time", inplace=True)
    return df


def get_binance_price(symbol: str) -> Optional[float]:
    """Return the latest price for a Binance symbol (e.g. BTCUSDT)."""
    url  = f"{BINANCE_BASE_URL}/api/v3/ticker/price"
    data = _get(url, params={"symbol": symbol})
    return float(data["price"]) if data else None


# ── CoinGecko ─────────────────────────────────────────────────────────────────

def get_coingecko_market_list(limit: int = 150) -> Optional[list[dict]]:
    """
    Fetch top `limit` coins by market cap from CoinGecko.
    Returns a list of dicts with id, symbol, current_price, total_volume, etc.
    """
    url  = f"{COINGECKO_BASE_URL}/coins/markets"
    data = _get(url, params={
        "vs_currency":           "usd",
        "order":                 "market_cap_desc",
        "per_page":              limit,
        "page":                  1,
        "sparkline":             False,
        "price_change_percentage": "24h,7d",
    })
    return data


def get_coingecko_ohlc(coin_id: str, days: int = 30) -> Optional[pd.DataFrame]:
    """
    Fetch daily OHLC for a CoinGecko coin_id (e.g. "bitcoin").
    Returns a DataFrame with columns: open, high, low, close.
    Note: CoinGecko OHLC is daily candles regardless of `days`.
    """
    url  = f"{COINGECKO_BASE_URL}/coins/{coin_id}/ohlc"
    data = _get(url, params={"vs_currency": "usd", "days": days})

    if data is None:
        return None

    df = pd.DataFrame(data, columns=["timestamp", "open", "high", "low", "close"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df.set_index("timestamp", inplace=True)
    return df


# ── CoinMarketCap (shared authenticated helper + public functions) ────────────
#
# All CMC endpoints require the API key in the X-CMC_PRO_API_KEY header.
# We use a single _cmc_get() helper so key injection happens in one place.
#
# error_code quirk: CMC returns error_code as a STRING ("0"), not an int.
# Always cast with int() before comparing.
# ─────────────────────────────────────────────────────────────────────────────

def _cmc_get(endpoint: str, params: dict = None) -> Optional[dict]:
    """
    Authenticated GET to a CoinMarketCap endpoint.
    Returns the full parsed response body (including 'data' and 'status'),
    or None if the request fails or CMC returns a non-zero error code.
    """
    if not CMC_API_KEY:
        log.error("CMC_API_KEY not set in .env")
        return None

    headers = {"X-CMC_PRO_API_KEY": CMC_API_KEY, "Accept": "application/json"}
    url     = f"{CMC_BASE_URL}{endpoint}"

    for attempt in range(1, 4):
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=15)
            resp.raise_for_status()
            body = resp.json()

            # Cast error_code to int — CMC sends it as a string
            if int(body.get("status", {}).get("error_code", -1)) != 0:
                log.error(f"CMC API error: {body['status'].get('error_message')}")
                return None

            return body

        except requests.RequestException as e:
            log.warning(f"CMC {endpoint} attempt {attempt}/3 failed: {e}")
            if attempt < 3:
                time.sleep(2 ** attempt)

    log.error(f"CMC {endpoint}: all retries exhausted.")
    return None


def get_fear_greed_index() -> Optional[dict]:
    """
    Fetch the latest Crypto Fear & Greed Index from CoinMarketCap.
    Returns { "value": 73, "value_classification": "Greed" } or None.
    """
    body = _cmc_get("/v3/fear-and-greed/latest")
    if body is None:
        return None
    data = body.get("data", {})
    return {
        "value":                int(data["value"]),
        "value_classification": data["value_classification"],
    }


def get_cmc_listings(
    limit:          int   = 500,
    mcap_min_usd:   float = 20_000_000,
    mcap_max_usd:   float = 300_000_000,
) -> Optional[list[dict]]:
    """
    Fetch cryptocurrency listings from CoinMarketCap filtered by market-cap range.

    Results are sorted by 24h volume descending so the most active coins in
    the target range appear first — ideal for the altcoin volume-spike scan.

    Parameters
    ----------
    limit        : max coins to return (1 credit per 100 results on CMC Basic)
    mcap_min_usd : lower market-cap bound in USD
    mcap_max_usd : upper market-cap bound in USD

    Returns a list of raw CMC coin dicts.  Each dict has the shape:
      {
        "symbol":              "XYZ",
        "name":                "XYZ Token",
        "circulating_supply":  50_000_000,
        "total_supply":        80_000_000,
        "quote": {
          "USD": {
            "price":              0.42,
            "volume_24h":         12_000_000,
            "market_cap":         21_000_000,
            "percent_change_24h": 3.5,
            "percent_change_7d":  12.1
          }
        }
      }
    """
    body = _cmc_get("/v1/cryptocurrency/listings/latest", params={
        "start":           1,
        "limit":           limit,
        "convert":         "USD",
        "sort":            "volume_24h",       # most active coins first
        "sort_dir":        "desc",
        "market_cap_min":  mcap_min_usd,
        "market_cap_max":  mcap_max_usd,
    })
    if body is None:
        return None
    return body.get("data", [])


# ── Binance Futures — public market-structure endpoints (no API key) ──────────
#
# All three endpoints are on fapi.binance.com and require no authentication.
# They update every 1–8 hours depending on the endpoint.
#
# Funding rate:      resets every 8h; positive = longs pay shorts (bearish pressure)
# Long/short ratio:  global account ratio; > 1 = longs dominant, < 1 = shorts dominant
# Open interest:     total open contracts in BTC; rising OI = conviction, falling = unwinding
# ─────────────────────────────────────────────────────────────────────────────

def get_coingecko_global() -> Optional[dict]:
    """
    CoinGecko /global — BTC dominance, total market cap, Total3, 24h market change.
    Returns the raw 'data' dict, or None on failure. Safe to call once per day.
    """
    data = _get(f"{COINGECKO_BASE_URL}/global")
    if data is None:
        log.warning("CoinGecko /global fetch failed — BTC.D and Total3 will show N/A")
        return None
    if isinstance(data, dict):
        return data.get("data")
    return None


def get_coingecko_ath_map(limit: int = 500) -> dict:
    """
    Fetch ATH and ATL price data for the top `limit` coins.
    Returns {SYMBOL_UPPER: {"ath": float, "ath_date": str, "atl": float, "atl_date": str}}.
    Data comes from CoinGecko /coins/markets (actual ATH/ATL, not rolling window).
    """
    result: dict = {}
    per_page = 250
    pages    = max(1, (limit + per_page - 1) // per_page)
    for page in range(1, pages + 1):
        data = _get(f"{COINGECKO_BASE_URL}/coins/markets", params={
            "vs_currency": "usd",
            "order":       "market_cap_desc",
            "per_page":    per_page,
            "page":        page,
            "sparkline":   False,
        })
        if not data:
            break
        for coin in data:
            sym      = (coin.get("symbol") or "").upper()
            ath      = coin.get("ath")
            ath_date = coin.get("ath_date") or ""
            atl      = coin.get("atl")
            atl_date = coin.get("atl_date") or ""
            if sym and ath and float(ath) > 0:
                result[sym] = {
                    "ath":      float(ath),
                    "ath_date": ath_date,
                    "atl":      float(atl) if atl and float(atl) > 0 else 0.0,
                    "atl_date": atl_date,
                }
        if len(data) < per_page:
            break
    log.info(f"CoinGecko ATH/ATL map: {len(result)} entries loaded")
    return result


def get_btc_funding_rate() -> Optional[float]:
    """
    Latest perpetual funding rate for BTCUSDT from MEXC Contract API (no key needed).
    Returns a decimal (e.g. 0.0001 = 0.01%).
    Positive  → longs paying shorts (bearish pressure on price).
    Negative  → shorts paying longs (bullish pressure on price).
    """
    data = _get(f"{MEXC_CONTRACT_BASE_URL}/api/v1/contract/funding_rate/BTC_USDT")
    if data is None:
        log.error("MEXC funding rate fetch failed — response empty")
        return None
    rate = (data.get("data") or {}).get("fundingRate")
    if rate is not None:
        return float(rate)
    log.error(f"MEXC funding rate: unexpected response structure: {data}")
    return None


def get_btc_long_short_ratio() -> Optional[float]:
    """
    Global long/short account ratio for BTCUSDT (1-hour period).
    Returns the ratio as a float (e.g. 1.35 means 57.4% long accounts).
    > 1.5  → crowded longs, squeeze risk to the downside.
    < 0.8  → crowded shorts, squeeze up possible.
    """
    url  = f"{BINANCE_FUTURES_BASE_URL}/futures/data/globalLongShortAccountRatio"
    data = _get(url, params={"symbol": "BTCUSDT", "period": "1h", "limit": 1})
    if data and isinstance(data, list) and len(data) > 0:
        try:
            return float(data[0]["longShortRatio"])
        except (KeyError, ValueError, TypeError):
            pass
    log.error("Long/short ratio fetch failed")
    return None


# ── Module 5: Momentum Scanner helpers ───────────────────────────────────────

def get_cmc_momentum_listings(
    limit:        int   = 500,
    mcap_min_usd: float = 25_000_000,
    mcap_max_usd: float = 5_000_000_000,
) -> Optional[list[dict]]:
    """
    Fetch CMC listings sorted by 1h price change descending.

    Server-side parameters:
      • market_cap_min / market_cap_max  — pre-filter by market cap
      • sort=percent_change_1h desc      — biggest 1h movers appear first,
        so the caller can break early once gains drop below the minimum threshold.

    Each coin dict includes:
      tags, circulating_supply, max_supply, total_supply, and quote.USD with
      price, volume_24h, market_cap, fully_diluted_market_cap, percent_change_1h/24h.

    Credit cost: 1 credit per 200 results  (limit=500 → 3 credits per call).
    """
    body = _cmc_get("/v1/cryptocurrency/listings/latest", params={
        "start":          1,
        "limit":          limit,
        "convert":        "USD",
        "sort":           "percent_change_1h",
        "sort_dir":       "desc",
        "market_cap_min": mcap_min_usd,
        "market_cap_max": mcap_max_usd,
    })
    if body is None:
        return None
    return body.get("data", [])


# ── Interval mapping: standard notation → MEXC contract API string ────────────
_FUTURES_INTERVAL_MAP: dict[str, str] = {
    "1m":  "Min1",
    "5m":  "Min5",
    "15m": "Min15",
    "30m": "Min30",
    "60m": "Min60",
    "1h":  "Min60",
    "4h":  "Hour4",
    "8h":  "Hour8",
    "1d":  "Day1",
}


def get_mexc_futures_klines(
    symbol:     str,
    interval:   str,
    limit:      int = 100,
    start_time: int | None = None,  # Unix seconds; if set, fetches `limit` candles FROM this time
    min_candles: int = 20,          # minimum acceptable candle count (use 1 for historical fetches)
) -> Optional[pd.DataFrame]:
    """
    Fetch OHLCV candles from the MEXC Contract (Futures) kline API.

    symbol      — MEXC futures symbol with underscore, e.g. "AKT_USDT"
    interval    — standard notation ("4h", "15m") or MEXC format ("Hour4", "Min15")
    limit       — number of candles to return
    start_time  — Unix timestamp in seconds; when set, returns `limit` candles starting
                  from this time (uses MEXC `from` param). Omit for recent candles.
    min_candles — minimum acceptable candle count; set to 1 for historical/backtesting fetches

    The MEXC contract kline response is column-oriented:
      data.time   — Unix timestamps in SECONDS (not ms, unlike spot API)
      data.open / close / high / low / vol  — parallel arrays of strings

    Returns a DataFrame indexed by open_time (UTC) with columns:
      open, high, low, close, volume  (all float)
    Returns None if the request fails or yields fewer than min_candles candles.
    """
    mexc_interval = _FUTURES_INTERVAL_MAP.get(interval, interval)
    url    = f"{MEXC_CONTRACT_BASE_URL}/api/v1/contract/kline/{symbol}"
    params: dict = {"interval": mexc_interval, "limit": limit}
    if start_time is not None:
        params["from"] = start_time

    data = _get(url, params=params)

    if not data:
        log.error(f"MEXC futures klines: empty response for {symbol} {interval}")
        return None

    body  = data if isinstance(data, dict) else {}
    kdata = body.get("data", {})

    if not kdata or not isinstance(kdata, dict) or "time" not in kdata:
        log.error(f"MEXC futures klines: unexpected shape for {symbol}: {type(kdata)}")
        return None

    times  = kdata.get("time",   [])
    opens  = kdata.get("open",   [])
    highs  = kdata.get("high",   [])
    lows   = kdata.get("low",    [])
    closes = kdata.get("close",  [])
    vols   = kdata.get("vol",    []) or kdata.get("volume", [])

    if not times:
        log.error(f"MEXC futures klines: no candles for {symbol} {interval}")
        return None

    rows = []
    for i in range(len(times)):
        try:
            rows.append({
                "open_time": int(times[i]),
                "open":      float(opens[i]),
                "high":      float(highs[i]),
                "low":       float(lows[i]),
                "close":     float(closes[i]),
                "volume":    float(vols[i]) if i < len(vols) else 0.0,
            })
        except (IndexError, ValueError, TypeError) as e:
            log.warning(f"Skipping malformed futures kline [{symbol} #{i}]: {e}")
            continue

    if len(rows) < min_candles:
        log.error(f"MEXC futures klines: too few valid candles ({len(rows)}) for {symbol}")
        return None

    df = pd.DataFrame(rows)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="s", utc=True)
    df.set_index("open_time", inplace=True)
    df.sort_index(inplace=True)
    return df


# Module-level cache so every 15-min scan doesn't re-fetch the full futures list.
_mexc_futures_cache: set[str] | None = None
_mexc_futures_cache_ts: float = 0.0
_MEXC_FUTURES_TTL: float = 600.0  # 10 minutes


def get_mexc_futures_symbols() -> set[str]:
    """
    Return the set of all MEXC perpetual futures symbols in BASE_USDT format,
    e.g. {"BTC_USDT", "ETH_USDT", "SOL_USDT", ...}.

    Cached for 10 minutes — safe to call on every 15-min scan.
    Returns the stale cache (or an empty set) on fetch failure so the caller
    can decide whether to abort the scan.

    Public endpoint — no API key required.
    """
    global _mexc_futures_cache, _mexc_futures_cache_ts

    now = time.time()
    if _mexc_futures_cache is not None and (now - _mexc_futures_cache_ts) < _MEXC_FUTURES_TTL:
        return _mexc_futures_cache

    url  = f"{MEXC_CONTRACT_BASE_URL}/api/v1/contract/detail"
    data = _get(url)

    if not data:
        log.error("MEXC futures: empty response from contract/detail")
        return _mexc_futures_cache or set()

    contracts = data.get("data", []) if isinstance(data, dict) else data
    if not isinstance(contracts, list) or not contracts:
        log.error("MEXC futures: unexpected response shape")
        return _mexc_futures_cache or set()

    symbols = {c.get("symbol", "").upper() for c in contracts if c.get("symbol")}
    log.info(f"MEXC futures: {len(symbols)} perpetual contracts loaded and cached.")

    _mexc_futures_cache    = symbols
    _mexc_futures_cache_ts = now
    return symbols


def get_btc_open_interest() -> Optional[tuple[float, float]]:
    """
    Last two hourly open-interest snapshots for BTCUSDT from Binance Futures.
    Returns (current_oi, previous_oi) in BTC units so the caller can compare.
    current_oi > previous_oi → OI is rising (more conviction / new positions).
    current_oi < previous_oi → OI is falling (positions closing / de-risking).
    """
    url  = f"{BINANCE_FUTURES_BASE_URL}/futures/data/openInterestHist"
    data = _get(url, params={"symbol": "BTCUSDT", "period": "1h", "limit": 2})
    if data and isinstance(data, list) and len(data) >= 2:
        try:
            # Binance returns oldest-first
            current  = float(data[-1]["sumOpenInterest"])
            previous = float(data[-2]["sumOpenInterest"])
            return current, previous
        except (KeyError, ValueError, TypeError):
            pass
    log.error("Open interest fetch failed")
    return None
