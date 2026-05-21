"""
config.py — Central configuration for every module.
Edit this file to tune thresholds without touching business logic.
"""

import os
from dotenv import load_dotenv

load_dotenv()  # reads .env file into os.environ

# ─── Telegram ────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "").strip()

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
MOMENTUM_ZONE_MIN            = 1.0    # kept for scoring reference only — no longer a Stage 1 gate
MOMENTUM_ZONE_MAX            = 15.0   # kept for scoring reference only — no longer a Stage 1 gate
MOMENTUM_1H_MIN_MOVEMENT     = 0.3    # Stage 1 break: CMC loop stops when 1H < 0.3%
MOMENTUM_EARLY_EXIT_PCT      = 0.3    # alias — same as MOMENTUM_1H_MIN_MOVEMENT (legacy compat)

# Market-cap & liquidity filters
MOMENTUM_MCAP_MIN_USD        = 25_000_000      # $25M standard minimum — micro-cap bypass below
MOMENTUM_MCAP_MAX_USD        = 5_000_000_000   # $5B maximum market cap
MOMENTUM_MCAP_MICRO_MIN_USD  = 10_000_000      # $10M absolute hard floor (no exceptions)
MOMENTUM_MCAP_MICRO_VOLMC_MIN = 1.50           # Vol/MC ratio (150%) required to bypass $25M floor
MOMENTUM_VOL_24H_MIN_USD     = 5_000_000       # $5M minimum 24h volume — bypassed if Vol/MC > 150%
MOMENTUM_VOLMC_BYPASS_MIN    = 1.50            # Vol/MC > 150% → bypass $5M absolute vol floor
MOMENTUM_VOLMC_DEAD_MAX      = 0.20            # Vol/MC < 20% → dead coin, block regardless of abs vol
MOMENTUM_VOL_SPIKE_MIN       = 1.30            # vol_change_24h must be ≥ this ratio (130% of prior 24h)

# 15m candle fast-track bypass (CHANGE 1C)
MOMENTUM_FT_15M_GAIN_MIN     = 3.0    # fast-track: 15m last candle must show ≥ 3% gain
MOMENTUM_FT_15M_VOL_MULT     = 5.0    # fast-track: 15m last candle vol ≥ 5× avg of prior 3 candles

# Supply / dilution filters
MOMENTUM_CIRC_SUPPLY_MIN_PCT = 40.0   # circulating / max_supply >= 40 %
MOMENTUM_FDV_RATIO_MAX       = 3.0    # FDV / market_cap ≤ 3×  (was 4×; renamed from MOMENTUM_FDV_MCAP_MAX_RATIO)

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
MOMENTUM_TA_H4_KDJ_J_MAX  = 95.0   # 4H KDJ J ceiling (fast move ≥5%)
MOMENTUM_SLOW_RSI_MAX     = 65.0   # slow-trend (1H <5%): 15m RSI ceiling when KDJ 95-100 is allowed
#
# Layer 2: 15m Scoring (50 pts max)
#   EMA6 > EMA20      → 15 pts
#   Price > EMA20     → 10 pts
#   RSI6 in [40, 72]  → 10 pts   (above 72 = flagged as hot)
#   KDJ J < 75        → 10 pts   (above 75 = overbought warning)
#   MACD DIF > DEA    →  5 pts
MOMENTUM_TA_15M_RSI6_MIN  = 40.0   # RSI6 lower bound
MOMENTUM_RSI_MAX          = 72.0   # RSI6 upper bound — above = risk flag  (renamed from MOMENTUM_TA_15M_RSI6_MAX)
MOMENTUM_KDJ_MAX          = 72.0   # KDJ J upper bound — above = overbought  (was 75; renamed from MOMENTUM_TA_15M_KDJ_J_MAX)
MOMENTUM_TA_H4_EMA_SEP_MIN = 0.002 # EMA6 must be ≥ 0.2% above EMA20 on 4H (min separation)
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
MOMENTUM_EARLY_SL_PCT          = 4.0    # SL for EARLY SIGNAL — tighter than normal
MOMENTUM_EARLY_15M_MIN         = 0.8    # min 15m candle gain % to qualify as early mover (was 1.0)
MOMENTUM_EARLY_15M_MAX         = 3.5    # max 15m candle gain % (above = too extended)
MOMENTUM_EARLY_1H_MAX          = 5.0    # 1H must still be below this for EARLY classification
MOMENTUM_SL_PCT                = 5.0    # SL  = entry × (1 − 5%)  unified for all signals
MOMENTUM_TP1_PCT               = 8.0    # TP1 = entry × (1 + 8%)  unified
MOMENTUM_TP2_PCT               = 15.0   # TP2 = entry × (1 + 15%) unified
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
#
# Kline fetch limits
MOMENTUM_TA_4H_LIMIT      = 100    # 4H candles (covers EMA20 + KDJ + MACD warm-up)
MOMENTUM_TA_15M_LIMIT     = 60     # 15m candles (≈ 15 hours of data)
MOMENTUM_TA_5M_LIMIT          = 60     # 60 × 5m = 5 hours of data for precision layer

# ─── 5m precision layer thresholds ──────────────────────────────────────────
MOMENTUM_5M_RSI_LOW           = 30.0   # 5m RSI below → not in entry range (GC gate)
MOMENTUM_5M_RSI_HOT           = 75.0   # 5m RSI above → overheated (soft gate, downgrade STRONG→WATCH)
MOMENTUM_5M_KDJ_MAX_PBW       = 30.0   # PBW: 5m KDJ J < 30 during accumulation
MOMENTUM_5M_RSI_MAX_PBW       = 45.0   # PBW: 5m RSI6 < 45 confirmed
MOMENTUM_5M_VOL_MAX_SC        = 0.25   # SC: 5m vol < 25% of MA10 (legacy; replaced by 5M_VOL_MAX_CONSOL)
MOMENTUM_5M_KDJ_MAX_SC        = 35.0   # SC: 5m KDJ J < 35

# ─── Unified 5m Gate (primary trigger — replaces per-signal 1H gates) ────────
MOMENTUM_5M_GATE_RSI_MIN      = 25.0   # 5m RSI6 lower bound (not deeply oversold)
MOMENTUM_5M_GATE_RSI_MAX      = 75.0   # 5m RSI6 upper bound (not overbought)
MOMENTUM_5M_GATE_VOL_MIN      = 1.2    # 5m vol must be ≥ 1.2× MA10 (active interest)
MOMENTUM_SC_5M_VOL_MAX_CONSOL = 0.40   # SC: prior 9-candle avg vol < 40% MA10 (consolidation check)

# ─── Per-signal cooldowns (CHANGE B) ──────────────────────────────────────────
MOMENTUM_RB_COOLDOWN_MIN      = 180    # Recovery Bounce: 3h (was 2h — avoid chasing noise)
MOMENTUM_PBW_COOLDOWN_MIN     = 60     # Pre-Breakout Watch: 1h (was 2h — accumulation needs fast re-check)
MOMENTUM_SC_COOLDOWN_MIN      = 60     # Staircase: 1h (was 2h — leg pauses are short)

# ─── New-signal min scores (CHANGE D) ─────────────────────────────────────────
MOMENTUM_PBW_MIN_SCORE        = 60     # PBW fires if fund quality ≥ this
MOMENTUM_SC_MIN_SCORE         = 60     # SC fires if fund quality ≥ this

# ─── Entry precision (ADDITION 2) ──────────────────────────────────────────────
MOMENTUM_ENTRY_LIMIT_OFFSET   = 0.001  # entry = last_price × (1 + 0.1%) — direct MEXC limit
MOMENTUM_ENTRY_ZONE_PCT       = 0.3    # signal chain: Entry-Zone ± 0.3%

# ─── Golden Cross signal ──────────────────────────────────────────────────────
MOMENTUM_GC_1H_MIN      = 0.5    # minimum 1H gain for GC candidates
MOMENTUM_GC_1H_MAX      = 8.0    # maximum 1H gain (raised from 5.0 — fast pumps hit GC at 6-7%)
MOMENTUM_GC_RSI_MAX     = 72.0   # 15m RSI ceiling for GC (was 65.0 — 72 still below overbought)
MOMENTUM_GC_SL_PCT      = 5.0    # stop-loss % for GOLDEN CROSS alerts
MOMENTUM_GC_EARLY_1H_MIN = 0.8   # boundary: below = pure GC vol, above = early-detection vol (was 1.0)

# Dynamic 4H volume thresholds (vs 4H MA10) — graduated by signal speed
MOMENTUM_VOL_FAST_MIN  = 1.30   # fast move (1H > 5%): vol > 130% MA10
MOMENTUM_VOL_SLOW_MIN  = 0.55   # slow trend (1H 2-5%): vol > 55% MA10 (was 80%)
MOMENTUM_VOL_EARLY_MIN = 0.30   # early detection (1H 0.8-5%): vol > 30% MA10 (was 0.45)
MOMENTUM_VOL_GC_MIN    = 0.30   # pure GC zone (1H 0.5-0.8%): vol > 30% MA10 (was 0.40)
MOMENTUM_VOL_GC_WARN   = 0.40   # below this at cross → "low volume" warning in Telegram

# ─── Volume Spike signal (FIX 2) ─────────────────────────────────────────────
MOMENTUM_VS_1H_MIN   = 1.0    # min 1H gain to be a VS candidate
MOMENTUM_VS_1H_MAX   = 8.0    # max 1H gain for VS (keeps it pre-breakout)
MOMENTUM_VS_VOL_MULT = 3.0    # current 15m vol must be ≥ 3× avg of prior 3 candles
MOMENTUM_VS_RSI_MAX  = 75.0   # 15m RSI ceiling for VS alerts
MOMENTUM_VS_SL_PCT   = 5.0    # SL % for Volume Spike alerts

# ─── Recovery Bounce signal (FIX 3) ──────────────────────────────────────────
MOMENTUM_RB_1H_MIN       = 2.0    # min 1H gain (coin is actively bouncing)
MOMENTUM_RB_1H_MAX       = 8.0    # max 1H gain (not yet overextended)
MOMENTUM_RB_PEAK_PCT     = 12.0   # h24_high must be ≥ 12% above current price or low
MOMENTUM_RB_PULLBACK_PCT = 8.0    # coin must be ≥ 8% below h24_high (real pullback happened)
MOMENTUM_RB_KDJ_MAX      = 110.0  # 4H KDJ must be < 110 (cooled after the pump)
MOMENTUM_RB_TP1_PCT      = 8.0    # conservative TP1 (below old peak)
MOMENTUM_RB_TP2_PCT      = 15.0   # optimistic TP2 (still below old peak for most cases)
MOMENTUM_RB_SL_PCT       = 5.0    # tight SL — old peak is overhead resistance

# ─── FIX 4: KDJ as warning + dead zone + ATH distance bonus ──────────────────
MOMENTUM_DEAD_VOL_PCT    = 0.15   # vol < 15% of MA10 = dead volume
MOMENTUM_DEAD_1H_MAX     = 3.0    # block only if dead vol AND 1H < 3% (low momentum confirmed)
MOMENTUM_ATH_DIST_L1     = 80.0   # > 80% below real ATH → +3 pts (high upside)
MOMENTUM_ATH_DIST_L2     = 90.0   # > 90% below real ATH → +5 pts (massive upside)
MOMENTUM_ATH_DIST_L1_PTS = 3
MOMENTUM_ATH_DIST_L2_PTS = 5
MOMENTUM_ATH_DIST_P1     = 60.0   # 40–60% below ATH → -3 pts (limited upside)
MOMENTUM_ATH_DIST_P2     = 40.0   # < 40% below ATH → -8 pts (near ATH, low ROI)
MOMENTUM_ATH_DIST_P1_PTS = -3
MOMENTUM_ATH_DIST_P2_PTS = -8

# ─── Pre-Breakout Watch signal (FIX 5) ───────────────────────────────────────
MOMENTUM_PBW_1H_MIN            = 1.0    # min 1H gain for PBW candidates
MOMENTUM_PBW_1H_MAX            = 8.0    # max 1H gain for PBW candidates
MOMENTUM_PBW_EMA_SPREAD_MAX    = 0.15   # 5m EMA6/EMA20 spread must be < 0.15% (compression)
MOMENTUM_PBW_RSI_MAX           = 52.0   # 5m RSI6 must be < 52 (base streak threshold)
MOMENTUM_PBW_RSI_CANDLES       = 5      # consecutive 5m candles with RSI < threshold
MOMENTUM_PBW_VOL_MULT          = 1.5    # trigger: 5m vol > 1.5× MA10
MOMENTUM_PBW_TRIGGER_RSI_MAX   = 68.0   # trigger candle RSI6 must be < 68 (not overbought)
MOMENTUM_PBW_RSI_MAX_FEAR      = 58.0   # Fear Mode: relax streak RSI threshold to 58
MOMENTUM_PBW_SL_PCT            = 5.0
MOMENTUM_PBW_TP1_PCT           = 8.0
MOMENTUM_PBW_TP2_PCT           = 15.0

# ─── Staircase Continuation signal (FIX 7) ───────────────────────────────────
MOMENTUM_SC_1H_MIN          = -2.0   # min 1H gain (flat/slight dip = consolidation)
MOMENTUM_SC_1H_MAX          = 4.0    # max 1H gain
MOMENTUM_SC_VOL_MAX         = 0.35   # 15m vol must be < 35% of MA10
MOMENTUM_SC_RSI_MAX         = 55.0   # 15m RSI6 must be < 55
MOMENTUM_SC_RSI_STRICT      = 45.0   # EITHER gate: RSI < 45 alone satisfies consolidation
MOMENTUM_SC_KDJ_MAX         = 50.0   # 15m KDJ J must be < 50
MOMENTUM_SC_KDJ_STRICT      = 35.0   # EITHER gate: KDJ J < 35 alone satisfies consolidation
MOMENTUM_SC_PRIOR_MOVE_MIN  = 4.0    # 24H high must be ≥4% above current (prior leg)
MOMENTUM_SC_RSI_MAX_FEAR    = 62.0   # Fear Mode: relax RSI threshold to 62
MOMENTUM_SC_VOL_MAX_FEAR    = 0.45   # Fear Mode: relax vol threshold to 45%
MOMENTUM_SC_SL_PCT          = 5.0
MOMENTUM_SC_TP1_PCT         = 8.0
MOMENTUM_SC_TP2_PCT         = 15.0

# ─── Fear Mode — Bear Market Relief (CHANGE 2A) ──────────────────────────────
MOMENTUM_FEAR_FG_THRESHOLD    = 45     # F&G below this activates Fear Mode
MOMENTUM_FEAR_EMA_SEP_MIN     = 0.05   # relaxed sep % (normal = 0.2%) compared vs tech.h4_ema_sep
MOMENTUM_FEAR_RS_PCT          = 5.0    # relative strength vs BTC needed for EMA6>EMA20 bypass (was 3.0)
MOMENTUM_FEAR_ATL_BUFFER_PCT  = 15.0  # Fear Mode: coin must be >15% above ATL to pass (normal = 5%)
MOMENTUM_ATL_BUFFER_PCT       = 5.0   # Normal mode: coin must be >5% above ATL to pass
MOMENTUM_FEAR_PD_VOL_MULT     = 8.0   # Fear Mode: P&D pre-filter vol multiplier (normal = 5×)
MOMENTUM_PD_VOL_MULT          = 5.0   # Normal mode: P&D pre-filter vol multiplier
MOMENTUM_PD_ATL_NEAR_PCT      = 20.0  # "near ATL" = price < ATL × (1 + this%) → pump-and-dump risk

# ─── Speed Alert Track (⚡) ────────────────────────────────────────────────────
MOMENTUM_SPEED_15M_GAIN_MIN   = 5.0   # 15m candle ≥ 5% to qualify
MOMENTUM_SPEED_VOL_MULT       = 5.0   # 15m vol ≥ 5× avg prev-3
MOMENTUM_SPEED_PRE_RSI_MAX    = 55.0  # RSI candle-before spike ≤ 55 (not already hot)
MOMENTUM_SPEED_MCAP_MIN       = 10_000_000  # MCap ≥ $10M
MOMENTUM_SPEED_ATL_BUFFER_PCT = 15.0  # price must be >15% above ATL
MOMENTUM_SPEED_H4_EMA_MAX_NEG = -8.0  # 4H EMA spread (ema6-ema20)/ema20 must be > -8% (not deeply bearish)
MOMENTUM_SPEED_SL_PCT         = 4.0   # Speed Alert stop-loss
MOMENTUM_SPEED_TP1_PCT        = 5.0   # Speed Alert TP1
MOMENTUM_SPEED_TP2_PCT        = 10.0  # Speed Alert TP2

# ─── Early GC (5m EMA cross signal) — Stage 2i ────────────────────────────────
MOMENTUM_EARLY_GC_1H_MIN     = 0.5    # minimum 1H gain for Early GC candidates
MOMENTUM_EARLY_GC_1H_MAX     = 12.0   # maximum 1H gain
MOMENTUM_EARLY_GC_MCAP_MIN   = 10_000_000  # $10M minimum MCap
MOMENTUM_EARLY_GC_RSI_MIN    = 30.0   # 5m RSI lower bound (not oversold)
MOMENTUM_EARLY_GC_RSI_MAX    = 72.0   # 5m RSI upper bound (not overbought)
MOMENTUM_EARLY_GC_VOL_MIN    = 1.5    # 5m vol ≥ 1.5× MA10 required
MOMENTUM_EARLY_GC_MIN_SCORE  = 60     # minimum score to fire alert
MOMENTUM_EARLY_GC_SL_PCT     = 5.0    # SL% for Early GC (tighter than main)
MOMENTUM_EARLY_GC_COOLDOWN_H = 4.0    # hours between Early GC alerts per coin

# ─── BB-Squeeze Bypass — Gate 2g (CHANGE 2B) ─────────────────────────────────
MOMENTUM_SQUEEZE_EMA_SPREAD_MAX = 3.0  # abs spread between 4H EMA6 and EMA20 < 3% (compressed)
MOMENTUM_SQUEEZE_EMA_SLOPE_MAX  = 0.5  # 4H EMA20 slope over 5 candles < 0.5% (flat)
MOMENTUM_SQUEEZE_VOL_MULT       = 5.0  # 15m vol ≥ 5× avg prev-3 (breakout vol)

# ─── BB-Squeeze Breakout signal (PART 3) ──────────────────────────────────────
MOMENTUM_SQ_15M_RSI_MIN     = 30.0        # 15m RSI6 ≥ 30 (not oversold when signal fires)
MOMENTUM_SQ_15M_RSI_MAX     = 75.0        # 15m RSI6 ≤ 75 (not yet overbought)
MOMENTUM_SQ_1H_MIN          = 1.0         # min 1H gain for SQ candidates
MOMENTUM_SQ_1H_MAX          = 20.0        # max 1H gain (wider — squeeze moves fast)
MOMENTUM_SQ_COOLDOWN_MIN    = 1440        # 24H cooldown — one alert per move
MOMENTUM_SQ_MIN_SCORE       = 65          # minimum score to fire alert
MOMENTUM_SQ_BASE_SCORE      = 70          # starting score
MOMENTUM_SQ_VOL_EXTREME     = 10.0        # 15m vol ≥ 10× → extreme vol bonus
MOMENTUM_SQ_VOL_EXTREME_PTS = 10          # +10 pts for extreme vol spike
MOMENTUM_SQ_CIRC_BONUS_PCT  = 99.9        # circ ≥ ~100% → fully circulating bonus
MOMENTUM_SQ_CIRC_BONUS_PTS  = 5           # +5 pts
MOMENTUM_SQ_MCAP_BONUS_USD  = 50_000_000  # MCap > $50M → size-credibility bonus
MOMENTUM_SQ_MCAP_BONUS_PTS  = 5           # +5 pts
MOMENTUM_SQ_INF_SUPPLY_PEN  = 10          # unknown max supply penalty magnitude (subtracted)
MOMENTUM_SQ_CIRC_LOW_PCT    = 60.0        # circ < 60% → low circ penalty
MOMENTUM_SQ_CIRC_LOW_PEN    = 5           # penalty magnitude (subtracted)
MOMENTUM_SQ_SL_PCT          = 5.0
MOMENTUM_SQ_TP1_PCT         = 8.0
MOMENTUM_SQ_TP2_PCT         = 15.0

# ─── Global per-coin cooldown (CHANGE 5A) ────────────────────────────────────
MOMENTUM_GLOBAL_COOLDOWN_MIN       = 240     # 4H global cooldown per coin, across all signal types
MOMENTUM_GLOBAL_SQ_EXCEPTION_PCT   = 10.0   # SQ exception fires if price moved > 10% since last alert

# ─── Alert logging (FIX 6) ───────────────────────────────────────────────────
ALERT_LOG_CSV = "logs/alert_log.csv"

# ─── Unified warning thresholds ──────────────────────────────────────────────
MOMENTUM_WARN_CIRC_ALERT_PCT  = 60.0   # circ rate warning if below this
MOMENTUM_WARN_FDV_ALERT_RATIO = 2.5    # FDV/MCap warning if above this

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
    # DeFi & DEX
    "decentralized-exchange",
    "defi",
    # Modular
    "modular-blockchain",
    "fan-token",
    "privacy",
})
