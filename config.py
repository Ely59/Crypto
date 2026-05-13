"""
config.py — Central configuration for every module.
Edit this file to tune thresholds without touching business logic.
"""

import os
from dotenv import load_dotenv

load_dotenv()  # reads .env file into os.environ

# ─── Telegram ────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

# ─── API endpoints ────────────────────────────────────────────────────────────
MEXC_BASE_URL              = "https://api.mexc.com"              # Module 1 — no API key needed
BINANCE_BASE_URL           = "https://api.binance.com"           # Module 2 / 3 spot
BINANCE_FUTURES_BASE_URL   = "https://fapi.binance.com"          # Module 3 futures (no key needed)
COINGECKO_BASE_URL         = "https://api.coingecko.com/api/v3"  # Module 2
CMC_BASE_URL               = "https://pro-api.coinmarketcap.com" # Fear & Greed index
CMC_API_KEY                = os.getenv("CMC_API_KEY", "")        # set in .env

# ─── Module 1: BTC Context Analyzer — EMA-based regime logic ─────────────────
# BULL requires ALL three conditions:
#   1. price > EMA20                  (price is above the mid-term trend line)
#   2. RSI between 50 and 75          (momentum building, not yet overbought)
#   3. EMA6 > EMA12 > EMA20           (short > mid > long = bullish EMA stack)
BTC_REGIME_RSI_BULL_MIN = 50    # RSI lower bound for BULL confirmation
BTC_REGIME_RSI_BULL_MAX = 75    # RSI upper bound — above 75 = overbought, skip

# BEAR requires ALL three conditions:
#   1. price < EMA20                  (price is below the mid-term trend line)
#   2. RSI < 45                       (momentum is weak / declining)
#   3. EMA6 < EMA12 < EMA20           (short < mid < long = bearish EMA stack)
BTC_REGIME_RSI_BEAR_MAX = 45    # RSI upper bound for BEAR confirmation

# NEUTRAL = anything that doesn't cleanly satisfy BULL or BEAR
BTC_4H_CANDLE_LIMIT = 200       # 200 × 4 h ≈ 33 days; gives EMA20 and RSI14 room to warm up

# ─── Legacy thresholds (used by Module 2 altcoin logic) ──────────────────────
BTC_RSI_OVERSOLD         = 40
BTC_RSI_OVERBOUGHT       = 60
FEAR_GREED_EXTREME_FEAR  = 25
FEAR_GREED_FEAR          = 40
FEAR_GREED_GREED         = 60
FEAR_GREED_EXTREME_GREED = 75
BTC_TREND_DAYS           = 7
BTC_BULL_THRESHOLD       = 5.0
BTC_BEAR_THRESHOLD       = -5.0

# ─── Module 2: Altcoin Scout — LONG criteria (7) ─────────────────────────────
# C1  Market cap gate
SCOUT_MCAP_MIN_USD      = 20_000_000    # $20 M minimum market cap
SCOUT_MCAP_MAX_USD      = 300_000_000   # $300 M maximum market cap

# C2 / C3  Volume / market-cap ratio → determines alert level
SCOUT_VOL_MC_L1_PCT     = 200.0   # ≥ 200 % → Level 1 Watchlist Alert
SCOUT_VOL_MC_L2_PCT     = 500.0   # ≥ 500 % → Level 2 Trade Alert  (overrides L1)

# C4  Circulation rate
SCOUT_CIRC_RATE_MIN_PCT = 50.0    # circulating_supply / total_supply ≥ 50 %

# C5  RSI gate — LONG (4H candles, MEXC)
SCOUT_RSI_MAX_4H        = 70.0    # RSI(14) on 4H must be BELOW this for LONG

# C6  EMA squeeze (4H candles, MEXC)
# All three EMAs (6, 12, 20) must be within SCOUT_EMA_SQUEEZE_PCT of each other.
SCOUT_EMA_SQUEEZE_PCT   = 3.0     # max spread between EMA6 / EMA12 / EMA20 < 3 %

# C7  BTC regime gate — LONG scan only runs when regime is BULL or NEUTRAL
SCOUT_ALLOWED_REGIMES   = {"BULL", "NEUTRAL"}

# ─── Module 2: Altcoin Scout — SHORT criteria ────────────────────────────────
# Mirror of the LONG criteria with reversed technical conditions.
# C5-SHORT  RSI overbought on 4H
SCOUT_RSI_MIN_4H_SHORT        = 70.0    # RSI(14) must be ABOVE this for SHORT

# Volume spike on bearish candle — last 4H candle must be red with elevated volume
SCOUT_SHORT_VOL_SPIKE_MULT    = 1.5     # volume > this × MA20 of volume

# C7-SHORT  BTC regime gate for SHORT: only scan when BEAR or NEUTRAL
SCOUT_SHORT_ALLOWED_REGIMES   = {"BEAR", "NEUTRAL"}

# Fetch / performance settings
SCOUT_CMC_LIMIT         = 500    # coins to pull from CMC per scan (sorted by vol_24h)
SCOUT_4H_CANDLE_LIMIT   = 100    # 100 × 4H ≈ 17 days; enough for EMA20 + RSI14 warm-up
SCOUT_MAX_RESULTS       = 20     # cap results list to avoid Telegram message overflow

# ─── Module 3: BTC Trading Support ───────────────────────────────────────────
BTC_LEVERAGE_RSI_LONG    = 45
BTC_LEVERAGE_RSI_SHORT   = 55
BTC_CANDLE_INTERVAL      = "1h"   # 1H candles for faster signal detection

# Fixed-percentage risk framework (replaces ATR-based levels)
BTC_RISK_SL_PCT       = 1.5    # stop-loss distance from entry (%)
BTC_RISK_TP1_PCT      = 0.8    # TP1 distance — close 40 % of position
BTC_RISK_TP2_PCT      = 1.5    # TP2 distance — close 40 % of position
BTC_RISK_RUNNER_PCT   = 2.5    # Runner target — let 20 % ride
BTC_RISK_LEVERAGE     = 50     # leverage for margin loss calculation
BTC_RISK_MARGIN_USDT  = 10.0   # margin per trade in USDT (Phase 1)

# ─── Module 3 Phase 2: Smart Entry Timing (15M MEXC candles) ─────────────────
# Squeeze detection
ENTRY_15M_LIMIT           = 50      # candles to fetch (50 × 15m = 12.5 h; gives RSI warm-up)
ENTRY_SQUEEZE_CANDLES     = 3       # consecutive candles that must be inside the squeeze
ENTRY_SQUEEZE_PCT         = 2.0     # max EMA6/12/20 spread (%) to be "in squeeze"

# RSI gate on 15M
ENTRY_RSI_MIN             = 45.0   # below this = momentum not confirmed, wait
ENTRY_RSI_MAX             = 72.0   # above this = overheated, wait
ENTRY_RSI_IDEAL_MIN       = 50.0   # RSI 50-65 → +2 (ideal zone)
ENTRY_RSI_IDEAL_MAX       = 65.0
ENTRY_RSI_OK_MAX          = 72.0   # RSI 65-72 → +1 (acceptable zone)

# Pullback gate
ENTRY_PULLBACK_MAX_PCT    = 0.8    # if price is > this % above EMA6, wait for pullback
ENTRY_PULLBACK_NEAR_PCT   = 0.3    # price within this % of EMA6 = "at EMA" (pullback arrived)

# Entry readiness
ENTRY_MIN_SCORE           = 5      # minimum score (out of 8) to fire the alert

# Risk levels for Phase 2 alerts (same SL/TP % as Phase 1, larger margin)
ENTRY_LEVERAGE            = 50     # same leverage
ENTRY_MARGIN_USDT         = 15.0   # margin per Phase 2 trade (15 USDT)

# ─── Scheduling ──────────────────────────────────────────────────────────────
DAILY_BRIEFING_HOUR   = 8    # 08:00 UTC
DAILY_BRIEFING_MINUTE = 0

# NYSE/NASDAQ open = 09:30 ET = always 15:30 Europe/Berlin (CET↔CEST auto-adjusts)
US_MARKET_OPEN_HOUR   = 15
US_MARKET_OPEN_MINUTE = 30

# ─── Logging ─────────────────────────────────────────────────────────────────
LOG_LEVEL = "INFO"
LOG_FILE  = "logs/crypto_ecosystem.log"

# ─── Module 5: Momentum Scanner ──────────────────────────────────────────────
MEXC_CONTRACT_BASE_URL = "https://contract.mexc.com"  # Futures/perp API (no key needed)

# 1h momentum gate
MOMENTUM_1H_CHANGE_MIN_PCT   = 3.0    # minimum 1h gain % to qualify
MOMENTUM_1H_CHANGE_MAX_PCT   = 12.0   # maximum 1h gain % (avoid parabolic pumps)

# Market-cap & liquidity filters
MOMENTUM_MCAP_MIN_USD        = 25_000_000      # $25M minimum market cap
MOMENTUM_MCAP_MAX_USD        = 5_000_000_000   # $5B maximum market cap
MOMENTUM_VOL_24H_MIN_USD     = 10_000_000      # $10M minimum 24h volume

# Supply / dilution filters
MOMENTUM_CIRC_SUPPLY_MIN_PCT = 40.0   # circulating / max_supply >= 40 %
MOMENTUM_FDV_MCAP_MAX_RATIO  = 4.0    # FDV / market_cap <= 4

# CMC fetch settings
# Credit cost: 1 credit per 200 results  →  500 results = 3 credits per call
# At 15-min intervals: 4 × 24 × 30 = 2,880 calls/month × 3 ≈ 8,640 credits/month
# Basic plan limit: 10,000 credits/month  →  fits with ~1,360 credits headroom
MOMENTUM_CMC_LIMIT           = 500    # coins to fetch per scan
MOMENTUM_MAX_RESULTS         = 20     # cap on qualifying coins per scan
MOMENTUM_ALERT_COOLDOWN_MIN  = 120    # minutes before the same coin can re-alert

# Technical Analysis gate — two-layer approach
#
# Layer 1: 4H Macro Filter (binary — BOTH must pass or coin is rejected immediately)
#   • EMA6 > EMA12 > EMA20 on 4H   (bearish trend = instant reject)
#   • KDJ J < 90 on 4H             (overheated on higher TF = instant reject)
MOMENTUM_TA_H4_KDJ_J_MAX  = 90.0   # 4H KDJ J ceiling
#
# Layer 2: 15m Scoring (50 pts max)
#   EMA6 > EMA20      → 15 pts
#   Price > EMA20     → 10 pts
#   RSI6 in [40, 72]  → 10 pts   (above 72 = flagged as hot)
#   KDJ J < 75        → 10 pts   (above 75 = overbought warning)
#   MACD DIF > DEA    →  5 pts
MOMENTUM_TA_15M_RSI6_MIN  = 40.0   # RSI6 lower bound
MOMENTUM_TA_15M_RSI6_MAX  = 72.0   # RSI6 upper bound (above = hot, risk flag)
MOMENTUM_TA_15M_KDJ_J_MAX = 75.0   # KDJ J upper bound (above = overbought warning)
#
# Volume check (4H): 10 pts
MOMENTUM_TA_VOL_RATIO_MIN = 1.20   # current 4H candle vol must exceed 120% of MA10
#
# Technical maximum: 60 pts  (50 pts from 15m + 10 pts from volume)
#
# ─── Fundamental bonus scoring (0-40 pts) ────────────────────────────────────
MOMENTUM_FUND_MCAP_L1_MIN_USD  = 50_000_000      # $50M — lower bound for MCap bonus
MOMENTUM_FUND_MCAP_L1_MAX_USD  = 2_000_000_000   # $2B  — upper bound for MCap bonus
MOMENTUM_FUND_MCAP_PTS         = 15              # +15 pts if MCap in [$50M, $2B]
MOMENTUM_FUND_CIRC_MIN_PCT     = 60.0            # circ supply above 60 % → +10 pts
MOMENTUM_FUND_CIRC_PTS         = 10
MOMENTUM_FUND_FDV_RATIO_MAX    = 2.0             # FDV/MCap below 2× → +10 pts
MOMENTUM_FUND_FDV_PTS          = 10
MOMENTUM_FUND_1H_SWEET_MIN_PCT = 5.0             # 1h gain 5-10 % → +5 pts (sweet spot)
MOMENTUM_FUND_1H_SWEET_MAX_PCT = 10.0
MOMENTUM_FUND_1H_SWEET_PTS     = 5
MOMENTUM_FUND_1H_OK_PTS        = 2              # 1h gain 3-5 % → +2 pts  (ok zone)
#
# Total score = Technical (0-60) + Fundamental (0-40) = max 100
MOMENTUM_TOTAL_STRONG_ENTRY    = 80    # 🟢 STRONG ENTRY — full Telegram alert
MOMENTUM_TOTAL_WATCH           = 65    # 🟡 WATCH — set limit order  (alert threshold)
MOMENTUM_TOTAL_MONITOR         = 50    # 🟠 MONITOR — silent (logged only, no alert)
#                                         SKIP — total < 50  (debug log only)
#
# ─── Fixed-% risk framework (replaces EMA20-based SL) ────────────────────────
MOMENTUM_SL_PCT                = 6.0    # SL  = entry × (1 − 6%)
MOMENTUM_TP1_PCT               = 10.0   # TP1 = entry × (1 + 10%)
MOMENTUM_TP2_PCT               = 20.0   # TP2 = entry × (1 + 20%)
MOMENTUM_POSITION_USD          = 100.0  # notional position size for R/R display ($)
#
# Max Telegram alerts dispatched per scan cycle (best score first)
MOMENTUM_MAX_ALERTS_PER_SCAN   = 3
#
# ─── Auto risk-warning thresholds ─────────────────────────────────────────────
MOMENTUM_WARN_RSI6_PCT         = 70.0   # RSI6 15m above this  → "RSI approaching overbought"
MOMENTUM_WARN_KDJ_J_PCT        = 70.0   # KDJ J 15m above this → "KDJ getting hot"
MOMENTUM_WARN_VOL_LOW_PCT      = 150.0  # 4H vol% below this   → "Volume not exceptional"
MOMENTUM_WARN_GAIN_LATE_PCT    = 10.0   # 1h gain above this   → "Late in momentum cycle"
MOMENTUM_WARN_CIRC_LOW_PCT     = 50.0   # circ% below this     → "Dilution risk present"
MOMENTUM_WARN_FDV_HIGH_RATIO   = 3.0    # FDV/MCap above this  → "High dilution risk"
#
# Kline fetch limits
MOMENTUM_TA_4H_LIMIT      = 100    # 4H candles (covers EMA20 + KDJ + MACD warm-up)
MOMENTUM_TA_15M_LIMIT     = 60     # 15m candles (≈ 15 hours of data)

# CMC tag slugs for allowed categories (used for set membership check)
MOMENTUM_ALLOWED_TAGS = frozenset({
    # Layer 1 / Layer 2
    "layer-1",
    "layer-2",
    # AI & Big Data
    "ai-big-data",
    "artificial-intelligence",
    # DePIN
    "depin",
    # RWA
    "real-world-assets",
    "rwa",
    "tokenized-assets",
    # Gaming
    "gaming",
    "gamefi",
    "play-to-earn",
    "metaverse",
})
