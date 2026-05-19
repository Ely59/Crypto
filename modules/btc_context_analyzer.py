"""
Module 1: BTC Context Analyzer
────────────────────────────────────────────────────────────────────────────────
Determines whether the current Bitcoin market is in a BULL, BEAR, or NEUTRAL
regime by combining three independent data sources:

  Source A — MEXC 4H candles (free public API, no key needed)
             Used to calculate: RSI(14), EMA6, EMA12, EMA20, current price

  Source B — Alternative.me Fear & Greed Index (free public API)
             Gives a 0–100 sentiment score updated daily

Regime logic (ALL conditions in a group must be true):
┌─────────┬──────────────────────────────────────────────────────────────────┐
│ BULL    │ price > EMA20   AND   RSI in [50, 75]   AND   EMA6>EMA12>EMA20  │
│ BEAR    │ price < EMA20   AND   RSI < 45           AND   EMA6<EMA12<EMA20  │
│ NEUTRAL │ anything that doesn't cleanly satisfy BULL or BEAR              │
└─────────┴──────────────────────────────────────────────────────────────────┘

Why these three indicators together?
  • EMA stack (6/12/20) shows whether short-term momentum is aligned with
    the medium-term trend. All three pointing the same way = strong regime.
  • RSI filters out "EMA crossovers that stall" — we only want regimes where
    momentum is actually building, not exhausted.
  • Price vs EMA20 is the single most robust trend-filter at the 4H level.

Run directly to send a test Telegram message:
  python -m modules.btc_context_analyzer
  — or —
  python modules/btc_context_analyzer.py
"""

from __future__ import annotations

# ── Path shim ─────────────────────────────────────────────────────────────────
# When this file is run directly (`python modules/btc_context_analyzer.py`),
# Python's working directory may not include the project root, so relative
# imports like `from utils.xxx` would fail.  Adding the root once here fixes
# direct execution without breaking the normal `import` path used by main.py.
import sys as _sys, os as _os
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
# ── End path shim ─────────────────────────────────────────────────────────────

from dataclasses import dataclass, field

from utils.api_client import (
    get_mexc_klines, get_mexc_price, get_fear_greed_index,
    get_btc_funding_rate, get_btc_long_short_ratio, get_btc_open_interest,
    get_coingecko_global,
)
from utils.indicators import compute_rsi, compute_ema, compute_macd
from utils.logger     import get_logger
import config as cfg

log = get_logger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# Result dataclasses
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class CoinContext:
    """
    Full technical snapshot for a single coin (ETH, SOL, …).
    Shares the same indicator set as BTC so the recommendation engine can
    render any coin in the daily briefing without special-casing.
    """
    symbol:            str
    regime:            str    # "BULL" | "BEAR" | "NEUTRAL"
    price:             float
    rsi:               float
    ema6:              float
    ema12:             float
    ema20:             float
    ema_stack:         str    # "BULL" | "BEAR" | "MIXED"
    vol_above_ma5:     bool
    vol_above_ma10:    bool
    ema_cross:         str    # "BULL_CROSS" | "BEAR_CROSS" | "SQUEEZE" | "NONE"
    macd_above_signal: bool
    macd_hist_growing: bool


@dataclass
class BTCContext:
    """
    Everything the rest of the system needs to know about the current BTC regime.
    All price / indicator values are rounded to 2 decimal places.
    """
    # ── Core output ───────────────────────────────────────────────────────────
    regime:           str    # "BULL" | "BEAR" | "NEUTRAL"

    # ── Live market data ──────────────────────────────────────────────────────
    btc_price:        float  # latest BTC/USDT spot price from MEXC

    # ── Technical indicators (computed on 4H candles) ─────────────────────────
    rsi:              float  # RSI(14) — 0 to 100
    ema6:             float  # EMA of last 6 closed 4H candles
    ema12:            float  # EMA of last 12 closed 4H candles
    ema20:            float  # EMA of last 20 closed 4H candles  ← main trend filter

    # ── Sentiment ─────────────────────────────────────────────────────────────
    fear_greed_value: int    # 0 (Extreme Fear) … 100 (Extreme Greed)
    fear_greed_label: str    # e.g. "Fear", "Greed", "Extreme Greed"

    # ── Diagnostics ───────────────────────────────────────────────────────────
    ema_stack:        str    # "BULL" | "BEAR" | "MIXED" — EMA alignment alone
    summary:          str    # one-line human-readable description

    # ── Multi-signal indicators (used by recommendation engine in Module 4) ────
    vol_above_ma5:      bool  # current 4H volume > 5-bar MA of volume
    vol_above_ma10:     bool  # current 4H volume > 10-bar MA of volume
    ema_cross:          str   # "BULL_CROSS" | "BEAR_CROSS" | "SQUEEZE" | "NONE"
    macd_above_signal:  bool  # MACD DIF line > DEA signal line
    macd_hist_growing:  bool  # histogram is expanding (current bar > previous bar)

    # ── Binance Futures market structure (None if fetch fails) ───────────────
    # These are populated by analyze() and used by the MARKET STRUCTURE section
    # of the daily briefing and by Module 3's conviction scoring.
    funding_rate:      float | None = field(default=None)  # decimal; e.g. 0.0001 = 0.01%
    long_short_ratio:  float | None = field(default=None)  # >1 longs dominant, <1 shorts
    oi_value:          float | None = field(default=None)  # open interest in BTC
    oi_rising:         bool  | None = field(default=None)  # True = OI growing vs prev hour

    # ── Companion coins (populated by analyze(); None if fetch fails) ─────────
    eth: CoinContext | None = field(default=None)
    sol: CoinContext | None = field(default=None)

    # ── Global macro (CoinGecko /global — CHANGE 7A) ──────────────────────────
    btc_dominance:      float | None = field(default=None)  # BTC % of total market cap
    total3_usd:         float | None = field(default=None)  # Total market ex BTC+ETH (USD)
    market_cap_24h_pct: float | None = field(default=None)  # 24h change % in total market cap


# ══════════════════════════════════════════════════════════════════════════════
# Internal helpers
# ══════════════════════════════════════════════════════════════════════════════

def _classify_ema_stack(ema6: float, ema12: float, ema20: float) -> str:
    """
    Determine whether the three EMAs are in a bullish, bearish, or mixed stack.

    Bullish stack: EMA6 > EMA12 > EMA20
      → short-term average is above mid-term, which is above long-term.
      → price has been consistently rising across all three horizons.

    Bearish stack: EMA6 < EMA12 < EMA20
      → the reverse: short-term has fallen furthest, confirming downtrend.

    Mixed: anything else (crossover in progress, consolidation, etc.)
    """
    if ema6 > ema12 > ema20:
        return "BULL"
    if ema6 < ema12 < ema20:
        return "BEAR"
    return "MIXED"


def _determine_regime(
    price: float,
    rsi: float,
    ema20: float,
    ema_stack: str,
) -> str:
    """
    Apply the three-condition regime rules.

    BULL — price is above its trend line, momentum is building (RSI 50–75),
           and all three EMAs confirm the uptrend.

    BEAR — price is below its trend line, momentum is weak (RSI < 45),
           and all three EMAs confirm the downtrend.

    NEUTRAL — anything in between: trending but RSI doesn't confirm,
              or RSI is right but EMAs are mixed, etc.
    """
    # ── BULL: price above EMA20 + RSI in momentum zone + aligned EMA stack ──
    is_bull = (
        price > ema20
        and cfg.BTC_REGIME_RSI_BULL_MIN <= rsi <= cfg.BTC_REGIME_RSI_BULL_MAX
        and ema_stack == "BULL"
    )

    # ── BEAR: price below EMA20 + RSI shows weak momentum + aligned EMA stack ─
    is_bear = (
        price < ema20
        and rsi < cfg.BTC_REGIME_RSI_BEAR_MAX
        and ema_stack == "BEAR"
    )

    if is_bull:
        return "BULL"
    if is_bear:
        return "BEAR"
    return "NEUTRAL"


def _analyze_coin(symbol: str) -> CoinContext | None:
    """
    Fetch MEXC 4H candles for any coin and compute the same indicator set
    used for BTC — RSI, EMA 6/12/20 stack + cross, volume vs MA, MACD.

    symbol — coin ticker without USDT, e.g. "ETH" or "SOL"
    Returns None if the exchange has no data for this pair.
    """
    df = get_mexc_klines(f"{symbol}USDT", "4h", limit=cfg.BTC_4H_CANDLE_LIMIT)
    if df is None or len(df) < 30:
        log.warning(f"Not enough MEXC 4H data for {symbol} — skipping.")
        return None

    close = df["close"]

    rsi_series   = compute_rsi(close, period=14)
    ema6_series  = compute_ema(close, period=6)
    ema12_series = compute_ema(close, period=12)
    ema20_series = compute_ema(close, period=20)

    rsi   = round(float(rsi_series.iloc[-1]),  2)
    ema6  = round(float(ema6_series.iloc[-1]),  2)
    ema12 = round(float(ema12_series.iloc[-1]), 2)
    ema20 = round(float(ema20_series.iloc[-1]), 2)
    price = round(float(close.iloc[-1]), 2)

    ema_stack = _classify_ema_stack(ema6, ema12, ema20)
    regime    = _determine_regime(price, rsi, ema20, ema_stack)

    # EMA cross detection (last two bars)
    ema6_prev  = float(ema6_series.iloc[-2])
    ema20_prev = float(ema20_series.iloc[-2])
    ema_spread = (max(ema6, ema12, ema20) - min(ema6, ema12, ema20)) / min(ema6, ema12, ema20) * 100

    if ema6_prev <= ema20_prev and ema6 > ema20:
        ema_cross = "BULL_CROSS"
    elif ema6_prev >= ema20_prev and ema6 < ema20:
        ema_cross = "BEAR_CROSS"
    elif ema_spread < cfg.SCOUT_EMA_SQUEEZE_PCT:
        ema_cross = "SQUEEZE"
    else:
        ema_cross = "NONE"

    # Volume trend
    volume      = df["volume"]
    vol_ma5     = float(volume.rolling(5).mean().iloc[-1])
    vol_ma10    = float(volume.rolling(10).mean().iloc[-1])
    current_vol = float(volume.iloc[-1])

    # MACD
    macd_line, signal_line, histogram = compute_macd(close)

    return CoinContext(
        symbol            = symbol,
        regime            = regime,
        price             = price,
        rsi               = rsi,
        ema6              = ema6,
        ema12             = ema12,
        ema20             = ema20,
        ema_stack         = ema_stack,
        vol_above_ma5     = current_vol > vol_ma5,
        vol_above_ma10    = current_vol > vol_ma10,
        ema_cross         = ema_cross,
        macd_above_signal = bool(macd_line.iloc[-1] > signal_line.iloc[-1]),
        macd_hist_growing = bool(histogram.iloc[-1] > histogram.iloc[-2]),
    )


def _fetch_fear_greed() -> tuple[int, str]:
    """
    Fetch the current Fear & Greed index from alternative.me.
    Returns (score, label). Falls back to (50, "Neutral") if the API is down.

    The index is published once per day. Scores:
      0–24  → Extreme Fear
      25–44 → Fear
      45–55 → Neutral
      56–74 → Greed
      75–100 → Extreme Greed
    """
    data = get_fear_greed_index()
    if data is None:
        log.warning("Fear & Greed API unavailable — defaulting to 50 / Neutral")
        return 50, "Neutral"
    return int(data["value"]), data["value_classification"]


# ══════════════════════════════════════════════════════════════════════════════
# Main public function
# ══════════════════════════════════════════════════════════════════════════════

def analyze() -> BTCContext | None:
    """
    Fetch all data, compute indicators, apply regime logic, and return a
    BTCContext dataclass.  Returns None only if the OHLCV fetch fails
    (Fear & Greed failure is handled gracefully with a fallback).

    Steps:
      1. Pull 200 × 4H candles from MEXC  →  gives ~33 days of history
      2. Compute RSI(14) on the closing prices
      3. Compute EMA6, EMA12, EMA20 on the closing prices
      4. Read the latest close as the "current price"
         (alternative: fetch the live ticker for a slightly fresher number)
      5. Fetch Fear & Greed index from alternative.me
      6. Classify EMA stack → BULL / BEAR / MIXED
      7. Apply three-condition regime rule → BULL / BEAR / NEUTRAL
      8. Build and return BTCContext
    """
    log.info("BTC Context Analyzer: fetching MEXC 4H candles…")

    # ── Step 1: Fetch candles ─────────────────────────────────────────────────
    df = get_mexc_klines("BTCUSDT", "4h", limit=cfg.BTC_4H_CANDLE_LIMIT)
    if df is None or len(df) < 30:
        # 30 is the minimum we need for EMA20 + a few warm-up bars
        log.error("Not enough 4H candle data from MEXC — aborting analysis.")
        return None

    close = df["close"]   # pd.Series of closing prices, oldest first

    # ── Step 2: RSI(14) ──────────────────────────────────────────────────────
    # RSI uses 14 periods of gains/losses.
    # We call .iloc[-1] to get the value for the most recently closed candle.
    rsi_series = compute_rsi(close, period=14)
    rsi        = round(rsi_series.iloc[-1], 2)

    # ── Step 3: EMA6, EMA12, EMA20 ───────────────────────────────────────────
    # EMA (Exponential Moving Average) gives more weight to recent candles.
    # span=N means the smoothing factor α = 2/(N+1).
    # EMA6  responds fastest to price changes (most sensitive).
    # EMA20 responds slowest (most stable, best trend filter).
    ema6_series  = compute_ema(close, period=6)
    ema12_series = compute_ema(close, period=12)
    ema20_series = compute_ema(close, period=20)

    ema6  = round(ema6_series.iloc[-1],  2)
    ema12 = round(ema12_series.iloc[-1], 2)
    ema20 = round(ema20_series.iloc[-1], 2)

    # ── Step 3b: EMA cross signal ────────────────────────────────────────────
    # A "cross" is detected by comparing the current and previous bar's relative
    # positions of EMA6 and EMA20. This fires at most once per candle.
    ema6_prev  = ema6_series.iloc[-2]
    ema20_prev = ema20_series.iloc[-2]
    ema_spread = (max(ema6, ema12, ema20) - min(ema6, ema12, ema20)) / min(ema6, ema12, ema20) * 100

    if ema6_prev <= ema20_prev and ema6 > ema20:
        ema_cross = "BULL_CROSS"      # EMA6 just crossed above EMA20
    elif ema6_prev >= ema20_prev and ema6 < ema20:
        ema_cross = "BEAR_CROSS"      # EMA6 just crossed below EMA20
    elif ema_spread < cfg.SCOUT_EMA_SQUEEZE_PCT:
        ema_cross = "SQUEEZE"         # all three EMAs tightly clustered
    else:
        ema_cross = "NONE"            # no recent cross; relative position via ema6 vs ema20

    # ── Step 3c: Volume trend ────────────────────────────────────────────────
    volume     = df["volume"]
    vol_ma5    = float(volume.rolling(5).mean().iloc[-1])
    vol_ma10   = float(volume.rolling(10).mean().iloc[-1])
    current_vol = float(volume.iloc[-1])
    vol_above_ma5  = current_vol > vol_ma5
    vol_above_ma10 = current_vol > vol_ma10

    # ── Step 3d: MACD direction ──────────────────────────────────────────────
    # DIF = fast EMA − slow EMA;  DEA = signal line (EMA of DIF);
    # histogram = DIF − DEA.  We look at the two most recent bars to detect
    # whether the histogram is expanding (growing conviction) or contracting.
    macd_line, signal_line, histogram = compute_macd(close)
    macd_above_signal = bool(macd_line.iloc[-1] > signal_line.iloc[-1])
    macd_hist_growing = bool(histogram.iloc[-1] > histogram.iloc[-2])

    # ── Step 4: Current price ─────────────────────────────────────────────────
    # Use the last closed candle's close price.
    # This is slightly older than the live spot price but matches the indicators
    # (which are also based on closed candles), keeping everything consistent.
    btc_price = round(float(close.iloc[-1]), 2)

    # ── Step 5: Binance Futures market structure ──────────────────────────────
    log.info("BTC Context Analyzer: fetching market structure (funding / L/S / OI)…")
    funding_rate     = get_btc_funding_rate()
    long_short_ratio = get_btc_long_short_ratio()
    oi_result        = get_btc_open_interest()
    if oi_result is not None:
        oi_current, oi_prev = oi_result
        oi_value  = oi_current
        oi_rising = oi_current > oi_prev
    else:
        oi_value  = None
        oi_rising = None

    # ── Step 6: ETH and SOL companion snapshots ───────────────────────────────
    log.info("BTC Context Analyzer: fetching ETH and SOL snapshots…")
    eth = _analyze_coin("ETH")
    sol = _analyze_coin("SOL")

    # ── Step 6b: CoinGecko global macro (BTC.D, Total3) ──────────────────────
    log.info("BTC Context Analyzer: fetching CoinGecko global macro…")
    cg = get_coingecko_global()
    if cg:
        btc_pct        = cg.get("market_cap_percentage", {}).get("btc", 0.0)
        eth_pct        = cg.get("market_cap_percentage", {}).get("eth", 0.0)
        total_cap      = cg.get("total_market_cap", {}).get("usd", 0.0)
        btc_dominance  = round(float(btc_pct), 2)
        total3_usd     = round(float(total_cap) * (1.0 - btc_pct / 100.0 - eth_pct / 100.0))
        market_cap_24h = cg.get("market_cap_change_percentage_24h_usd")
        market_cap_24h = round(float(market_cap_24h), 2) if market_cap_24h is not None else None
    else:
        btc_dominance  = None
        total3_usd     = None
        market_cap_24h = None

    # ── Step 6: Fear & Greed ─────────────────────────────────────────────────
    fg_value, fg_label = _fetch_fear_greed()

    # ── Step 7: EMA stack classification ─────────────────────────────────────
    ema_stack = _classify_ema_stack(ema6, ema12, ema20)

    # ── Step 8: Regime decision ───────────────────────────────────────────────
    regime = _determine_regime(btc_price, rsi, ema20, ema_stack)

    # ── Step 9: Build result ──────────────────────────────────────────────────
    summary = (
        f"{regime} | BTC ${btc_price:,.0f} | "
        f"RSI {rsi} | EMA6 {ema6:,.0f} / EMA12 {ema12:,.0f} / EMA20 {ema20:,.0f} | "
        f"F&G {fg_value} ({fg_label})"
    )
    log.info(summary)

    return BTCContext(
        regime             = regime,
        btc_price          = btc_price,
        rsi                = rsi,
        ema6               = ema6,
        ema12              = ema12,
        ema20              = ema20,
        fear_greed_value   = fg_value,
        fear_greed_label   = fg_label,
        ema_stack          = ema_stack,
        summary            = summary,
        vol_above_ma5      = vol_above_ma5,
        vol_above_ma10     = vol_above_ma10,
        ema_cross          = ema_cross,
        macd_above_signal  = macd_above_signal,
        macd_hist_growing  = macd_hist_growing,
        funding_rate       = funding_rate,
        long_short_ratio   = long_short_ratio,
        oi_value           = oi_value,
        oi_rising          = oi_rising,
        eth                = eth,
        sol                = sol,
        btc_dominance      = btc_dominance,
        total3_usd         = total3_usd,
        market_cap_24h_pct = market_cap_24h,
    )


# ══════════════════════════════════════════════════════════════════════════════
# Stand-alone test — run this file directly to get a live result + Telegram msg
# ══════════════════════════════════════════════════════════════════════════════

def _build_test_message(ctx: BTCContext) -> str:
    """Format a concise Telegram test message from a BTCContext."""
    regime_emoji = {"BULL": "🟢", "BEAR": "🔴", "NEUTRAL": "🟡"}.get(ctx.regime, "⚪")
    stack_emoji  = {"BULL": "📈", "BEAR": "📉", "MIXED": "↔️"}.get(ctx.ema_stack, "─")

    return (
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🧪  <b>Module 1 — Live Test</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "\n"
        f"{regime_emoji}  <b>BTC Regime: {ctx.regime}</b>\n"
        "\n"
        f"  💰  Price       <b>${ctx.btc_price:,.2f}</b>\n"
        f"  📊  RSI (14)    <b>{ctx.rsi}</b>\n"
        "\n"
        f"  📐  EMA6        <b>${ctx.ema6:,.2f}</b>\n"
        f"  📐  EMA12       <b>${ctx.ema12:,.2f}</b>\n"
        f"  📐  EMA20       <b>${ctx.ema20:,.2f}</b>\n"
        f"  {stack_emoji}  EMA Stack   <b>{ctx.ema_stack}</b>\n"
        "\n"
        f"  😱  Fear &amp; Greed  <b>{ctx.fear_greed_value} — {ctx.fear_greed_label}</b>\n"
        "\n"
        f"  📝  {ctx.summary}\n"
        "━━━━━━━━━━━━━━━━━━━━━━"
    )


if __name__ == "__main__":
    # Running this file directly:
    #   python modules/btc_context_analyzer.py
    # Will print the result to the console AND send a Telegram test message.
    import sys
    import os

    # Make sure project root is on the path when running this file directly
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    # Import here (not at top) to avoid circular imports when used as a module
    from modules.telegram_alerts import send_message

    print("Running BTC Context Analyzer…\n")
    ctx = analyze()

    if ctx is None:
        print("ERROR: Analysis failed. Check your internet connection and logs.")
        sys.exit(1)

    # ── Print to console ──────────────────────────────────────────────────────
    print(f"  Regime:     {ctx.regime}")
    print(f"  BTC Price:  ${ctx.btc_price:,.2f}")
    print(f"  RSI (14):   {ctx.rsi}")
    print(f"  EMA6:       ${ctx.ema6:,.2f}")
    print(f"  EMA12:      ${ctx.ema12:,.2f}")
    print(f"  EMA20:      ${ctx.ema20:,.2f}")
    print(f"  EMA Stack:  {ctx.ema_stack}")
    print(f"  F&G:        {ctx.fear_greed_value} ({ctx.fear_greed_label})")
    print(f"\nSummary: {ctx.summary}")

    # ── Send to Telegram ──────────────────────────────────────────────────────
    print("\nSending Telegram test message…")
    ok = send_message(_build_test_message(ctx))
    print("Telegram: OK ✓" if ok else "Telegram: FAILED — check token/chat ID in .env")
