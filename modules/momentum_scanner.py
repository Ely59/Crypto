"""
Module 5: Momentum Scanner
────────────────────────────────────────────────────────────────────────────────
Three-stage pipeline per coin:

  Stage 1 — Fundamental screen (M1–M7)
    M1  1h change    +3 % to +12 %
    M2  Market cap   $25M – $5B
    M3  24h volume   > $10M
    M4  Circ. supply ≥ 40 %
    M5  FDV/MCap     ≤ 4×
    M6  Category     Layer 1/2, AI, DePIN, RWA, Gaming
    M7  MEXC perp futures available

  Stage 2 — 4H Macro Filter (binary hard gate)
    BOTH must pass; one failure = coin rejected immediately.
    • EMA6 > EMA12 > EMA20 on 4H
    • KDJ(9,3,3) J < 90 on 4H

  Stage 3 — Scoring (max 100 pts)
    Technical (0-60):
      EMA6 > EMA20    on 15m  → +15 pts
      Price > EMA20   on 15m  → +10 pts
      RSI6 [40, 72]   on 15m  → +10 pts
      KDJ J < 75      on 15m  → +10 pts
      MACD DIF > DEA  on 15m  →  +5 pts
      Vol > 120% MA10 on 4H   → +10 pts
    Fundamental (0-40):
      MCap $50M – $2B         → +15 pts
      Circ supply > 60%       → +10 pts
      FDV/MCap < 2×           → +10 pts
      1h gain 5-10%           →  +5 pts  (3-5% → +2 pts)

Recommendation thresholds (total = tech + fund):
    80-100  🟢 STRONG ENTRY  — full Telegram alert
    65-79   🟡 WATCH         — Telegram alert with limit order note
    50-64   🟠 MONITOR       — logged, no alert
    < 50        SKIP          — silent

Trade levels (fixed-%, $100 position):
    SL  = entry × 0.94  (−6%)   → risk $6
    TP1 = entry × 1.10  (+10%)  → reward $10
    TP2 = entry × 1.20  (+20%)  → max reward $20
    R/R = 1 : 1.67

Klines source: MEXC Contract API (futures klines, no spot data needed)
  GET https://contract.mexc.com/api/v1/contract/kline/{symbol}
  Symbol format: AKT_USDT  (underscore — same as MEXC futures symbol list)

Run directly:
  python modules/momentum_scanner.py
"""

from __future__ import annotations

import sys as _sys, os as _os
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)

import time
import requests as _req
from dataclasses import dataclass, field

from utils.api_client import (
    get_cmc_momentum_listings,
    get_mexc_futures_symbols,
    get_mexc_futures_klines,
    get_coingecko_ath_map,
)
from utils.indicators import compute_ema, compute_rsi, compute_macd, compute_kdj
from utils.logger     import get_logger
import config as cfg

log = get_logger(__name__)

# ── Market context cache (Fear & Greed + BTC 24H) ────────────────────────────
_fg_value:      int   = 50     # Fear & Greed index (0-100); default = neutral
_fg_fetched_at: float = 0.0   # Unix timestamp of last successful fetch
_fear_mode:     bool  = False  # True when F&G < MOMENTUM_FEAR_FG_THRESHOLD
_btc_24h_change: float = 0.0  # BTC 24H % change (for relative-strength bypass)

# Stage 2a block counters — reset per scan, exposed for stats_tracker
_last_s2a_ema_bearish:   int = 0
_last_s2a_sep_small:     int = 0
_last_s2a_15m_gate:      int = 0
_last_s2a_fear_bypassed: int = 0
_last_s2a_squeeze:       int = 0


def _refresh_market_context() -> None:
    """
    Fetch Fear & Greed index (alternative.me) + BTC 24H change (MEXC daily kline).
    Results cached for 1 hour; safe to call before every scan.
    """
    global _fg_value, _fg_fetched_at, _fear_mode, _btc_24h_change
    if time.time() - _fg_fetched_at < 3600:
        return

    try:
        resp = _req.get("https://api.alternative.me/fng/?limit=1", timeout=8)
        _fg_value = int(resp.json()["data"][0]["value"])
    except Exception as e:
        log.warning(f"F&G fetch failed: {e}")

    try:
        df_btc = get_mexc_futures_klines("BTC_USDT", "1d", limit=2)
        if df_btc is not None and len(df_btc) >= 2:
            prev = float(df_btc["close"].iloc[-2])
            last = float(df_btc["close"].iloc[-1])
            _btc_24h_change = (last - prev) / prev * 100.0 if prev > 0 else 0.0
    except Exception as e:
        log.warning(f"BTC 24H change fetch failed: {e}")

    _fear_mode = _fg_value < cfg.MOMENTUM_FEAR_FG_THRESHOLD
    _fg_fetched_at = time.time()
    mode_str = f"😟 FEAR MODE (F&G {_fg_value})" if _fear_mode else f"Normal (F&G {_fg_value})"
    log.info(f"Market context: {mode_str} | BTC 24H {_btc_24h_change:+.1f}%")


# Scoring point values — technical layer
_PTS_EMA   = 15
_PTS_PRICE = 10
_PTS_RSI6  = 10
_PTS_KDJ   = 10
_PTS_MACD  =  5
_PTS_VOL   = 10
_PTS_MAX   = 60   # technical maximum

_FUND_MAX  = 40   # fundamental maximum


# ══════════════════════════════════════════════════════════════════════════════
# Dataclasses
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class TechResult:
    """Full TA result — 4H macro gate + 15m scoring breakdown."""

    # 4H Macro Filter
    h4_ema6:    float
    h4_ema12:   float
    h4_ema20:   float
    h4_ema_ok:  bool        # EMA6 > EMA12 > EMA20

    h4_kdj_j:   float
    h4_kdj_ok:  bool        # J < 90

    macro_ok:   bool        # h4_ema_ok AND h4_kdj_ok

    # 15m Scoring
    m15_ema6:       float
    m15_ema20:      float
    m15_ema_ok:     bool
    m15_ema_pts:    int     # 15 or 0

    m15_price:      float
    m15_price_ok:   bool
    m15_price_pts:  int     # 10 or 0

    m15_rsi6:       float
    m15_rsi6_ok:    bool
    m15_rsi6_hot:   bool    # RSI6 > 72 — risk flag
    m15_rsi6_pts:   int     # 10 or 0

    m15_kdj_j:      float
    m15_kdj_ok:     bool
    m15_kdj_hot:    bool    # J ≥ 75 — overbought warning
    m15_kdj_pts:    int     # 10 or 0

    m15_macd_dif:   float
    m15_macd_dea:   float
    m15_macd_ok:    bool
    m15_macd_pts:   int     # 5 or 0

    # Volume (4H)
    vol_pct:    float       # last 4H candle vol / MA10 × 100
    vol_ok:     bool
    vol_pts:    int         # 10 or 0

    # 4H EMA separation (for logging / EARLY gate)
    h4_ema_sep: float       # (ema6 - ema20) / ema20

    # Last 15m candle % change (used for EARLY SIGNAL detection)
    m15_change: float

    # True if EMA6 just crossed above EMA20 on 15m within the last 2 candles
    m15_golden_cross: bool

    # 15m Volume Spike — current candle vol vs avg of prior 3
    m15_vol_spike:       bool    # vol_last ≥ VS_VOL_MULT × avg_prev3
    m15_vol_spike_ratio: float   # actual ratio for alert display

    # 4H price range over last 24H (6 × 4H candles) — for Recovery Bounce
    h24_high:     float   # max 4H candle high over last 6 candles
    h24_low:      float   # min 4H candle low  over last 6 candles

    # 16-day peak (all fetched 4H candles) — for ATH distance bonus
    h16d_high:    float   # max 4H candle high over all fetched candles
    ath_dist_pct: float   # % below h16d_high  (higher = farther from peak)

    # Technical score (0-60)
    score: int

    # 4H additional indicators (for signal chain display)
    h4_rsi6:             float = 0.0    # RSI6 on 4H
    h4_macd_ok:          bool  = False  # 4H MACD DIF > DEA

    # 15m EMA12 — for GATE 2a hard gate
    m15_ema12:           float = 0.0
    m15_ema6_gt_ema12:   bool  = False

    # 5m Precision Layer (soft gate — populated after _check_5m())
    m5_rsi6:              float = 0.0
    m5_kdj_j:             float = 0.0
    m5_kdj_rising:        bool  = False
    m5_price_above_ema20: bool  = False
    m5_ema6_gt_ema12:     bool  = False
    m5_ema6_gt_ema20:     bool  = False   # EMA6 above EMA20 on 5m
    m5_fresh_cross:       bool  = False   # fresh 5m EMA6/EMA20 cross (was below 3 candles ago)
    m5_first_green:       bool  = False
    m5_vol_pct:           float = 0.0
    m5_vol_recent_pct:    float = 0.0    # avg of prior 9 5m candles / MA10 (SC consolidation)
    m5_ema20:             float = 0.0    # 5m EMA20 price — used for entry zone
    m5_ok:                bool  = False
    m5_note:              str   = ""

    # Squeeze / Fear Mode support
    h4_ema20_slope:       float = 0.0   # % change of 4H EMA20 over last 5 candles (CHANGE 2B)
    m15_price_gt_h4_ema20: bool = False  # 15m price > 4H EMA20 (breakout confirmation)
    h4_compression_days:  int   = 0     # calendar days 4H EMA spread has been compressed < 3%

    # 4H Transition Mode: EMA6 > EMA20 + EMA6 > EMA12 + 24H positive — partial bullish
    h4_transitioning:     bool  = False

    # 4H Momentum Method B: price > 8H ago + DIF rising + RSI > 42 + green candle
    h4_method_b:          bool  = False

    # 4H Fear Mode Method C: EMA6 within 1.5% below EMA20, 4H rising, RSI > 35
    h4_method_c:          bool  = False

    # 4H score factor (replaces hard gate): +20 full / +8 partial / -10 bearish / 0 neutral
    h4_score:   int  = 0
    h4_status:  str  = ""   # "FULL" | "PARTIAL" | "BEARISH" | "NEUTRAL"

    # 1H directional score: +10 bullish / +5 neutral-bullish / 0 weak
    h1_score:   int  = 0
    h1_status:  str  = ""   # "BULLISH" | "NEUTRAL" | "WEAK" | "UNKNOWN"


@dataclass
class FundResult:
    """Fundamental bonus scoring breakdown (0-40 pts total)."""
    mcap_pts: int    # 0 or 15
    circ_pts: int    # 0 or 10
    fdv_pts:  int    # 0 or 10
    gain_pts: int    # 0, 2, or 5
    total:    int    # sum of above


@dataclass
class MomentumResult:
    """
    A coin that passed Stage 1 (M1-M7), Stage 2 (4H macro),
    and reached total score ≥ 50 (MONITOR or above).
    scan() returns only STRONG ENTRY (≥80) and WATCH (≥65) coins.
    """
    symbol:          str
    name:            str
    price:           float
    change_1h:       float
    change_24h:      float
    market_cap:      float
    volume_24h:      float
    fdv:             float
    fdv_mcap_ratio:  float
    circ_supply_pct: float
    matched_tags:    list[str]         = field(default_factory=list)
    mexc_symbol:     str               = ""

    tech:            TechResult | None = None
    fund:            FundResult | None = None

    total_score:     int  = 0          # tech.score + fund.total (0-100)
    recommendation:  str  = ""         # "STRONG ENTRY", "WATCH", "MONITOR"
    rec_emoji:       str  = ""         # "🟢", "🟡", "🟠"

    warnings:        list[str] = field(default_factory=list)

    # ATH distance bonus (computed from tech.ath_dist_pct, added to total_score)
    ath_pts:         int  = 0

    # Trade levels (fixed-% framework, $100 position)
    entry_price:    float = 0.0
    stop_loss:      float = 0.0        # entry × (1 − SL_PCT/100)
    tp1:            float = 0.0        # entry × (1 + TP1_PCT/100)
    tp2:            float = 0.0        # entry × (1 + TP2_PCT/100)
    risk_usd:       float = 0.0        # POSITION_USD × SL_PCT/100
    reward_tp1_usd: float = 0.0        # POSITION_USD × TP1_PCT/100
    reward_tp2_usd: float = 0.0        # POSITION_USD × TP2_PCT/100
    rr_str:         str   = "1:1.67"   # reward_tp1 / risk
    sl_pct:         float = 6.0        # actual SL % applied (4.0 for EARLY SIGNAL)

    # Supplementary fields for new signal types
    infinite_supply:  bool  = False  # True when max_supply is None (inflationary)
    m1_ema_spread:    float = 0.0    # 1m EMA6/EMA20 spread % (Pre-Breakout Watch)
    m1_rsi_streak:    int   = 0      # consecutive 1m candles with RSI < 45 (PBW)
    m1_vol_ratio:     float = 0.0    # 1m trigger vol / MA10 ratio (PBW)
    sc_prior_move:    float = 0.0    # 24H price range % (Staircase prior leg)
    ath_dist_pct:     float = 0.0    # % below real ATH (from CoinGecko; fallback to 16D peak)
    ath_price:        float = 0.0    # true all-time high price (USD, from CoinGecko)
    ath_date:         str   = ""     # ATH date ISO string from CoinGecko

    # 5m layer data and advisory note (for signal chain display in alerts)
    m5_rsi6:   float = 0.0
    m5_kdj_j:  float = 0.0
    m5_note:   str   = ""

    # Gate 2g — BB-Squeeze bypass flag (CHANGE 2B)
    squeeze_bypass: bool = False

    # Stage 0 pre-breakout watchlist confirmation
    stage0_breakout: bool = False   # True if coin was on S0 watchlist and broke above consolidation high

    # Change 4: leg number + entry validity
    leg_number:   int  = 1      # 1 = first detection, 2+ = continuation leg
    entry_valid:  bool = True   # False if price moved too far or alert is too old

    # Change 5: pattern type
    pattern_type:  str = ""   # "EXPLOSION" | "BREAKOUT" | "GRIND" | ""
    pattern_bonus: int = 0    # score pts added for detected pattern


# ══════════════════════════════════════════════════════════════════════════════
# Cooldown — keyed by symbol, 2-hour window
# ══════════════════════════════════════════════════════════════════════════════

_alerted: dict[str, float] = {}


def _on_cooldown(symbol: str) -> bool:
    return (time.time() - _alerted.get(symbol, 0.0)) < cfg.MOMENTUM_ALERT_COOLDOWN_MIN * 60


def _mark_alerted(symbol: str) -> None:
    _alerted[symbol] = time.time()
    stale = time.time() - 86_400
    for k in [k for k, ts in _alerted.items() if ts < stale]:
        del _alerted[k]


# Stage-1 and Stage-2 counters — read by main.py for stats tracking
_last_m1m7_count:    int = 0
_last_macro_blocked: int = 0   # coins rejected at 4H macro gate per scan


# ── Per-scan outcome tracking — read by main.py for command responses ─────────

@dataclass
class CandidateOutcome:
    """What happened to each M1–M7 candidate in the last scan."""
    symbol:    str
    change_1h: float
    score:     int    # 0 if blocked before scoring
    rec:       str    # "ALERTED" | "MONITOR" | "BELOW_THRESHOLD" | "MACRO_BLOCKED"
                      # | "DEAD_ZONE" | "NO_DATA" | "COOLDOWN" | "GC" | "VS" | "RB" | "COOLING"
    detail:    str    # human-readable description
    vol_pct:   float  # 4H vol% vs MA10 (0 if unavailable)
    h4_kdj_j:  float  # 4H KDJ J value (0 if unavailable)


@dataclass
class RBWatchItem:
    """Coin that has a recent 24H peak — near-miss recovery bounce candidate."""
    symbol:       str
    change_1h:    float
    current:      float
    h24_high:     float
    pullback_pct: float   # % below h24_high
    h4_ema_ok:    bool
    h4_kdj_j:     float


_last_scan_outcomes: list[CandidateOutcome] = []
_last_rb_watchlist:  list[RBWatchItem]      = []

# Separate cooldown for cooling alerts so they never block entry alerts
_cooling_alerted: dict[str, float] = {}


def _on_cooling_cooldown(symbol: str) -> bool:
    return (time.time() - _cooling_alerted.get(symbol, 0.0)) < cfg.MOMENTUM_ALERT_COOLDOWN_MIN * 60


def _mark_cooling_alerted(symbol: str) -> None:
    _cooling_alerted[symbol] = time.time()
    stale = time.time() - 86_400
    for k in [k for k, ts in _cooling_alerted.items() if ts < stale]:
        del _cooling_alerted[k]


_gc_alerted: dict[str, float] = {}


def _on_gc_cooldown(symbol: str) -> bool:
    return (time.time() - _gc_alerted.get(symbol, 0.0)) < cfg.MOMENTUM_ALERT_COOLDOWN_MIN * 60


def _mark_gc_alerted(symbol: str) -> None:
    _gc_alerted[symbol] = time.time()
    stale = time.time() - 86_400
    for k in [k for k, ts in _gc_alerted.items() if ts < stale]:
        del _gc_alerted[k]


_vs_alerted: dict[str, float] = {}


def _on_vs_cooldown(symbol: str) -> bool:
    return (time.time() - _vs_alerted.get(symbol, 0.0)) < cfg.MOMENTUM_ALERT_COOLDOWN_MIN * 60


def _mark_vs_alerted(symbol: str) -> None:
    _vs_alerted[symbol] = time.time()
    stale = time.time() - 86_400
    for k in [k for k, ts in _vs_alerted.items() if ts < stale]:
        del _vs_alerted[k]


_rb_alerted: dict[str, float] = {}


def _on_rb_cooldown(symbol: str) -> bool:
    return (time.time() - _rb_alerted.get(symbol, 0.0)) < cfg.MOMENTUM_RB_COOLDOWN_MIN * 60


def _mark_rb_alerted(symbol: str) -> None:
    _rb_alerted[symbol] = time.time()
    stale = time.time() - 86_400
    for k in [k for k, ts in _rb_alerted.items() if ts < stale]:
        del _rb_alerted[k]


_pbw_alerted: dict[str, float] = {}


def _on_pbw_cooldown(symbol: str) -> bool:
    return (time.time() - _pbw_alerted.get(symbol, 0.0)) < cfg.MOMENTUM_PBW_COOLDOWN_MIN * 60


def _mark_pbw_alerted(symbol: str) -> None:
    _pbw_alerted[symbol] = time.time()
    stale = time.time() - 86_400
    for k in [k for k, ts in _pbw_alerted.items() if ts < stale]:
        del _pbw_alerted[k]


_sc_alerted: dict[str, float] = {}


def _on_sc_cooldown(symbol: str) -> bool:
    return (time.time() - _sc_alerted.get(symbol, 0.0)) < cfg.MOMENTUM_SC_COOLDOWN_MIN * 60


def _mark_sc_alerted(symbol: str) -> None:
    _sc_alerted[symbol] = time.time()
    stale = time.time() - 86_400
    for k in [k for k, ts in _sc_alerted.items() if ts < stale]:
        del _sc_alerted[k]


_sq_alerted: dict[str, float] = {}


def _on_sq_cooldown(symbol: str) -> bool:
    return (time.time() - _sq_alerted.get(symbol, 0.0)) < cfg.MOMENTUM_SQ_COOLDOWN_MIN * 60


def _mark_sq_alerted(symbol: str) -> None:
    _sq_alerted[symbol] = time.time()
    stale = time.time() - 172_800   # clean entries older than 48H
    for k in [k for k, ts in _sq_alerted.items() if ts < stale]:
        del _sq_alerted[k]


# ── Speed Alert cooldown (⚡ SPEED track) ────────────────────────────────────
_speed_alerted: dict[str, float] = {}
_last_speed_count: int = 0


def _on_speed_cooldown(symbol: str) -> bool:
    return (time.time() - _speed_alerted.get(symbol, 0.0)) < 120 * 60  # 2H cooldown


def _mark_speed_alerted(symbol: str) -> None:
    _speed_alerted[symbol] = time.time()
    stale = time.time() - 86_400
    for k in [k for k, ts in _speed_alerted.items() if ts < stale]:
        del _speed_alerted[k]


# ── Early GC cooldown (5m EMA cross signal) ───────────────────────────────────
_early_gc_alerted: dict[str, float] = {}


def _on_early_gc_cooldown(symbol: str) -> bool:
    return (time.time() - _early_gc_alerted.get(symbol, 0.0)) < cfg.MOMENTUM_EARLY_GC_COOLDOWN_H * 3600


def _mark_early_gc_alerted(symbol: str) -> None:
    _early_gc_alerted[symbol] = time.time()
    stale = time.time() - 86_400
    for k in [k for k, ts in _early_gc_alerted.items() if ts < stale]:
        del _early_gc_alerted[k]


# ── Tiered scan state — MASTER PROMPT Part A ─────────────────────────────────
# Tier 2: coins with active 5m momentum (populated during Tier 1 scan)
_active_watch: set[str] = set()           # mexc_symbols with 5m EMA cross / spike
# Tier 3: coins that received an alert (for leg continuation tracking)
_alert_watchlist: dict[str, dict] = {}    # symbol → {leg_high, entry_price, signal_type, leg_number, alert_time}

# CMC data cache — populated by Tier 1 scan so Tier 2 can show real name/mcap/circ
# symbol → (name, matched_tags, mcap, vol_24h, fdv, fdv_ratio, circ_pct)
_cmc_data_cache: dict[str, tuple] = {}

# Timestamps for /status reporting
_tier1_last_run: float = 0.0
_tier2_last_run: float = 0.0
_tier3_last_run: float = 0.0

# /passed — per-candidate data from last Tier1 scan (symbol, score, mcap, m5/m15/4H status)
_last_passed_candidates: list[dict] = []

# /tier2 — timestamps when each mexc_symbol was added to active_watch
_active_watch_ts: dict[str, float] = {}

# /tier2 — last known CMC price per symbol (from last Tier1)
_cmc_price_cache: dict[str, float] = {}

# /blocked — Method C coins that scored below 70 in last scan
_last_method_c_blocked: list[dict] = []

# Stage 0 pre-breakout watchlist
# symbol → {mexc_symbol, consolidation_high, ma10_vol, added_ts, name, mcap, price_at_add}
_stage0_watchlist: dict[str, dict] = {}

# ── Global per-coin cooldown — CHANGE 5A ─────────────────────────────────────
# Stores: symbol → (timestamp, rec_type, price_at_alert)
_global_alerted: dict[str, tuple] = {}


def _on_global_cooldown(symbol: str, new_rec: str, new_price: float) -> bool:
    """Return True (block) if same coin was alerted within the global 4H window."""
    entry = _global_alerted.get(symbol)
    if entry is None:
        return False
    ts, prev_rec, prev_price = entry
    if time.time() - ts >= cfg.MOMENTUM_GLOBAL_COOLDOWN_MIN * 60:
        return False
    # SQUEEZE exception: prior signal was a different type AND price moved > 10%
    if (new_rec == "SQUEEZE" and prev_rec != "SQUEEZE"
            and prev_price > 0
            and abs(new_price - prev_price) / prev_price
                > cfg.MOMENTUM_GLOBAL_SQ_EXCEPTION_PCT / 100.0):
        return False
    return True


def _mark_global_alerted(symbol: str, rec_type: str, price: float) -> None:
    _global_alerted[symbol] = (time.time(), rec_type, price)
    stale = time.time() - 86_400
    for k in [k for k, (ts, _r, _p) in list(_global_alerted.items()) if ts < stale]:
        del _global_alerted[k]


def _add_to_watchlist(symbol: str, price: float, signal_type: str, leg_number: int = 1) -> None:
    """Add or update a coin in the alert_watchlist after an alert fires."""
    global _alert_watchlist
    existing = _alert_watchlist.get(symbol)
    if existing and existing.get("leg_number", 1) >= leg_number:
        # Only update if this is a new leg or same leg with higher price
        if price > existing.get("leg_high", 0):
            existing["leg_high"] = price
        return
    _alert_watchlist[symbol] = {
        "leg_high":    price,
        "entry_price": price,
        "signal_type": signal_type,
        "leg_number":  leg_number,
        "alert_time":  time.time(),
    }
    # Expire entries older than 72H
    stale = time.time() - 72 * 3600
    for k in [k for k, v in list(_alert_watchlist.items()) if v.get("alert_time", 0) < stale]:
        del _alert_watchlist[k]


def _get_leg_info(symbol: str, price: float, recommendation: str) -> tuple[int, bool]:
    """
    Return (leg_number, entry_valid) for a coin about to be alerted.
    Looks up _alert_watchlist for prior alert state.
    entry_valid=False if price moved too far or prior alert is too old.
    """
    existing = _alert_watchlist.get(symbol)
    if not existing:
        return 1, True

    leg = existing.get("leg_number", 1)
    alert_ts     = existing.get("alert_time", 0.0)
    prior_price  = existing.get("entry_price", price)

    age_min  = (time.time() - alert_ts) / 60.0
    moved_pct = abs(price - prior_price) / prior_price * 100.0 if prior_price > 0 else 0.0

    if recommendation == "STRONG ENTRY":
        threshold = cfg.MOMENTUM_ENTRY_VALID_STRONG_PCT
    elif recommendation == "WATCH":
        threshold = cfg.MOMENTUM_ENTRY_VALID_WATCH_PCT
    else:
        threshold = cfg.MOMENTUM_ENTRY_VALID_OTHER_PCT

    valid = age_min <= cfg.MOMENTUM_ENTRY_VALID_WINDOW_MIN and moved_pct <= threshold
    return leg, valid


def _detect_pattern(mexc_symbol: str, stage0_breakout: bool) -> tuple[str, int]:
    """
    Detect one of three pattern types from 5m kline data.
    Returns (pattern_type, bonus_pts).

    Priority: EXPLOSION > BREAKOUT > GRIND (first match wins).
    """
    df5m = get_mexc_futures_klines(mexc_symbol, "5m", limit=20)
    if df5m is None or len(df5m) < 10:
        return "", 0

    closes = df5m["close"].astype(float)
    opens  = df5m["open"].astype(float)
    highs  = df5m["high"].astype(float)
    vols   = df5m["volume"].astype(float)

    # EXPLOSION: last candle vol ≥ 3.5× avg prior 4 AND candle body ≥ 2%
    vol_last  = float(vols.iloc[-1])
    vol_prev4 = float(vols.iloc[-5:-1].mean()) if len(vols) >= 5 else vol_last
    candle_move = (float(closes.iloc[-1]) - float(opens.iloc[-1])) / float(opens.iloc[-1]) * 100.0 \
                  if float(opens.iloc[-1]) > 0 else 0.0
    if (vol_prev4 > 0 and vol_last >= cfg.MOMENTUM_PAT_EXPLOSION_VOL_MULT * vol_prev4
            and candle_move >= cfg.MOMENTUM_PAT_EXPLOSION_MOVE_PCT):
        return "EXPLOSION", cfg.MOMENTUM_PAT_EXPLOSION_BONUS

    # BREAKOUT: prior 9-candle range ≤ 4% then last close above prior range high
    prior_high = float(highs.iloc[-10:-1].max())
    prior_low  = float(df5m["low"].astype(float).iloc[-10:-1].min())
    if prior_low > 0:
        prior_range = (prior_high - prior_low) / prior_low * 100.0
        last_close  = float(closes.iloc[-1])
        if prior_range <= cfg.MOMENTUM_PAT_BREAKOUT_RANGE_MAX and last_close > prior_high:
            bonus = (cfg.MOMENTUM_PAT_BREAKOUT_S0_BONUS if stage0_breakout
                     else cfg.MOMENTUM_PAT_BREAKOUT_BONUS)
            return "BREAKOUT", bonus

    # GRIND: N+ consecutive green 5m candles above EMA20
    n = cfg.MOMENTUM_PAT_GRIND_CANDLES
    if len(closes) >= n + 5:
        ema20 = float(closes.ewm(span=20, adjust=False).mean().iloc[-1])
        consecutive = 0
        for i in range(1, n + 2):
            c = float(closes.iloc[-i])
            o = float(opens.iloc[-i])
            if c > o and c > ema20:
                consecutive += 1
            else:
                break
        if consecutive >= n:
            return "GRIND", cfg.MOMENTUM_PAT_GRIND_BONUS

    return "", 0


# ── RADAR / SIGNAL cooldowns ──────────────────────────────────────────────────
_radar_alerted:  dict[str, float] = {}
_signal_alerted: dict[str, float] = {}


def _on_radar_cooldown(symbol: str) -> bool:
    return (time.time() - _radar_alerted.get(symbol, 0.0)) < cfg.RADAR_COOLDOWN_MIN * 60


def _mark_radar_alerted(symbol: str) -> None:
    _radar_alerted[symbol] = time.time()
    stale = time.time() - 86_400
    for k in [k for k, ts in list(_radar_alerted.items()) if ts < stale]:
        del _radar_alerted[k]


def _on_signal_cooldown(symbol: str) -> bool:
    return (time.time() - _signal_alerted.get(symbol, 0.0)) < cfg.SIGNAL_COOLDOWN_MIN * 60


def _mark_signal_alerted(symbol: str) -> None:
    _signal_alerted[symbol] = time.time()
    stale = time.time() - 86_400
    for k in [k for k, ts in list(_signal_alerted.items()) if ts < stale]:
        del _signal_alerted[k]


def _fetch_aggregated_klines(mexc_symbol: str, minutes: int) -> "pd.DataFrame | None":
    """Fetch 1m futures klines and resample to `minutes`-minute bars."""
    import pandas as pd
    raw_limit = min(200, 15 * minutes)
    df_1m = get_mexc_futures_klines(mexc_symbol, "1m", limit=raw_limit)
    if df_1m is None or len(df_1m) < 6:
        return None
    try:
        resampled = (
            df_1m.resample(f"{minutes}min")
            .agg({"open": "first", "high": "max", "low": "min",
                  "close": "last", "volume": "sum"})
            .dropna(subset=["close"])
        )
        return resampled if len(resampled) >= 5 else None
    except Exception as e:
        log.debug(f"Resample {mexc_symbol} {minutes}m failed: {e}")
        return None


def _check_3m(mexc_symbol: str) -> "dict | None":
    """
    3m precision check for RADAR conditions.
    Returns dict with ok=True when: price > EMA20 AND RSI6 > 42 AND RSI6 rising.
    """
    df = _fetch_aggregated_klines(mexc_symbol, 3)
    if df is None:
        return None
    close   = df["close"]
    ema20_s = compute_ema(close, 20)
    rsi_s   = compute_rsi(close, period=6)
    if len(rsi_s) < 3:
        return None
    rsi_now  = float(rsi_s.iloc[-1])
    rsi_prev = float(rsi_s.iloc[-3])   # 2 candles ago
    price    = float(close.iloc[-1])
    ema20_v  = float(ema20_s.iloc[-1])
    price_gt = price > ema20_v
    rsi_ok   = rsi_now > cfg.RADAR_3M_RSI_MIN
    rsi_rise = rsi_now > rsi_prev
    return {
        "price_gt_ema20": price_gt,
        "rsi6":           round(rsi_now, 1),
        "rsi_rising":     rsi_rise,
        "ok":             price_gt and rsi_ok and rsi_rise,
    }


def _check_10m(mexc_symbol: str) -> "dict | None":
    """
    10m EMA6/EMA20 cross detection.
    Returns {"ema6_gt_ema20": bool, "just_crossed": bool, ...}.
    """
    df = _fetch_aggregated_klines(mexc_symbol, 10)
    if df is None:
        return None
    close  = df["close"]
    ema6_s = compute_ema(close, 6)
    ema20_s = compute_ema(close, 20)
    if len(ema6_s) < 3:
        return None
    ema6_now   = float(ema6_s.iloc[-1])
    ema20_now  = float(ema20_s.iloc[-1])
    ema6_prev  = float(ema6_s.iloc[-2])
    ema20_prev = float(ema20_s.iloc[-2])
    crossed      = ema6_now > ema20_now
    just_crossed = (ema6_prev <= ema20_prev) and crossed
    return {
        "ema6_gt_ema20": crossed,
        "just_crossed":  just_crossed,
        "ema6":          round(ema6_now, 8),
        "ema20":         round(ema20_now, 8),
    }


def scan_radar_and_signal(
    margin:   float | None = None,
    leverage: int   | None = None,
) -> "tuple[list[dict], list[dict]]":
    """
    Scan _active_watch for RADAR (pre-signal) and SIGNAL (10m cross confirmed) alerts.

    RADAR fires when: 3m ok + 5m approaching/crossed + 4H ok + 10m NOT yet crossed.
    SIGNAL fires when: same but 10m crossed AND ≥3/5 TF conditions pass.

    Returns (radar_list, signal_list) — each element is a plain dict for the alert builder.
    margin / leverage default to config values when not supplied.
    """
    if not _active_watch:
        return [], []

    import config as _cfg
    _margin   = margin   if margin   is not None else _cfg.DEFAULT_MARGIN_USDT
    _leverage = leverage if leverage is not None else _cfg.DEFAULT_LEVERAGE

    # Clamp margin to safe range
    _margin = max(_cfg.MARGIN_MIN_USDT, min(_cfg.MARGIN_MAX_USDT, _margin))

    _ath_map = get_coingecko_ath_map(limit=500)
    radar_results:  list[dict] = []
    signal_results: list[dict] = []

    for mexc_symbol in list(_active_watch):
        symbol = mexc_symbol.replace("_USDT", "")

        on_radar  = _on_radar_cooldown(symbol)
        on_signal = _on_signal_cooldown(symbol)
        if on_radar and on_signal:
            continue

        # ── Fast 3m check (cheap: 1m fetch + resample) ───────────────────────
        m3 = _check_3m(mexc_symbol)
        if not m3 or not m3["ok"]:
            continue

        # ── 5m check (use cache if available) ────────────────────────────────
        m5 = _m5_cache.get(mexc_symbol) or _check_5m(mexc_symbol)
        if not m5:
            continue

        ema6_v  = m5.get("ema6",  0.0)
        ema20_v = m5.get("ema20", 0.0)
        if ema20_v > 0:
            spread_pct = abs(ema6_v - ema20_v) / ema20_v * 100.0
            approaching = spread_pct < _cfg.RADAR_5M_EMA_APPROACH_PCT and ema6_v < ema20_v
        else:
            approaching = False
        crossed_5m  = m5.get("fresh_cross", False) or m5.get("ema6_gt_ema20", False)
        rsi5_ok     = _cfg.RADAR_5M_RSI_MIN <= m5.get("rsi6", 0) <= _cfg.RADAR_5M_RSI_MAX
        m5_ok       = (approaching or crossed_5m) and rsi5_ok
        if not m5_ok:
            continue

        # ── 10m cross check (resamples from 1m — one shared API call) ────────
        m10 = _check_10m(mexc_symbol)
        m10_crossed = bool(m10 and m10.get("ema6_gt_ema20"))

        # ── 4H + 15m technicals (most expensive — only fetch when pre-checks pass) ─
        tech = _check_technicals(mexc_symbol)
        if tech is None:
            continue

        h4_ok  = bool(tech.h4_ema_ok or tech.h4_method_b or tech.h4_transitioning)
        m15_ok = bool(tech.m15_ema6_gt_ema12 and tech.m15_rsi6 < 78)

        if not h4_ok:
            continue

        conds = {
            "3m":  True,
            "5m":  True,
            "10m": m10_crossed,
            "15m": m15_ok,
            "4H":  h4_ok,
        }
        cond_score = sum(conds.values())
        price      = tech.m15_price
        if price <= 0:
            continue

        _ath_dist, _ath_px, _ath_date = _lookup_ath(_ath_map, symbol, price, tech.ath_dist_pct)

        if m10_crossed and cond_score >= _cfg.SIGNAL_MIN_CONDITIONS and not on_signal:
            # ── SIGNAL ────────────────────────────────────────────────────────
            if _on_global_cooldown(symbol, "SIGNAL", price):
                continue

            # Trade levels: SL -5%, TP1 +8%, TP2 +15%
            bk_price = tech.h24_high * 1.005 if tech.h24_high > 0 else price * 1.005
            pb_price = tech.m5_ema20 if tech.m5_ema20 > 0 and tech.m5_ema20 < price else price * 0.97

            def _levels(p: float) -> tuple:
                sl  = p * (1 - _cfg.MOMENTUM_SL_PCT / 100)
                tp1 = p * (1 + _cfg.MOMENTUM_TP1_PCT / 100)
                tp2 = p * (1 + _cfg.MOMENTUM_TP2_PCT / 100)
                return sl, tp1, tp2

            bk_sl, bk_tp1, bk_tp2 = _levels(bk_price)
            pb_sl, pb_tp1, pb_tp2 = _levels(pb_price)

            pos_size     = _margin * _leverage
            profit_tp1   = pos_size * (_cfg.MOMENTUM_TP1_PCT / 100) * 0.60
            profit_tp2   = pos_size * (_cfg.MOMENTUM_TP2_PCT / 100) * 0.40

            result = {
                "type":        "SIGNAL",
                "symbol":      symbol,
                "mexc_symbol": mexc_symbol,
                "price":       round(price, 8),
                "conds":       conds,
                "cond_score":  cond_score,
                "tech":        tech,
                "bk_price":   round(bk_price, 8),
                "bk_sl":      round(bk_sl,    8),
                "bk_tp1":     round(bk_tp1,   8),
                "bk_tp2":     round(bk_tp2,   8),
                "pb_price":   round(pb_price, 8),
                "pb_sl":      round(pb_sl,    8),
                "pb_tp1":     round(pb_tp1,   8),
                "pb_tp2":     round(pb_tp2,   8),
                "margin":      _margin,
                "leverage":    _leverage,
                "position_size": round(pos_size, 2),
                "profit_tp1":  round(profit_tp1, 2),
                "profit_tp2":  round(profit_tp2, 2),
                "ath_dist_pct": round(_ath_dist, 1),
                "ath_price":   _ath_px,
                "ath_date":    _ath_date,
            }
            signal_results.append(result)
            _mark_signal_alerted(symbol)
            _mark_global_alerted(symbol, "SIGNAL", price)
            log.info(
                f"  🟢 SIGNAL  {symbol}  {cond_score}/5 conds  "
                f"bk={bk_price:.4g}  pb={pb_price:.4g}"
            )

        elif not m10_crossed and not on_radar:
            # ── RADAR ─────────────────────────────────────────────────────────
            result = {
                "type":        "RADAR",
                "symbol":      symbol,
                "mexc_symbol": mexc_symbol,
                "price":       round(price, 8),
                "conds":       conds,
                "m3_rsi6":     m3["rsi6"],
                "m5_rsi6":     round(m5.get("rsi6", 0), 1),
                "tech":        tech,
                "ath_dist_pct": round(_ath_dist, 1),
            }
            radar_results.append(result)
            _mark_radar_alerted(symbol)
            log.info(
                f"  👁️ RADAR  {symbol}  3m RSI {m3['rsi6']:.0f}  "
                f"5m {'cross' if crossed_5m else 'approach'}"
            )

    log.info(
        f"RADAR/SIGNAL scan done — "
        f"{len(radar_results)} RADAR, {len(signal_results)} SIGNAL."
    )
    return radar_results, signal_results


def get_global_cooldown_status() -> list:
    """Return [(symbol, seconds_remaining, rec_type), ...] sorted longest-remaining first."""
    now    = time.time()
    window = cfg.MOMENTUM_GLOBAL_COOLDOWN_MIN * 60
    result = []
    for sym, (ts, rec, _) in _global_alerted.items():
        remaining = window - (now - ts)
        if remaining > 0:
            result.append((sym, remaining, rec))
    result.sort(key=lambda x: x[1], reverse=True)
    return result


# ══════════════════════════════════════════════════════════════════════════════
# Stage 1 helpers
# ══════════════════════════════════════════════════════════════════════════════

def _resolve_fdv(coin: dict, price: float) -> float:
    q   = coin.get("quote", {}).get("USD", {})
    fdv = q.get("fully_diluted_market_cap") or 0.0
    if fdv > 0:
        return fdv
    supply = coin.get("max_supply") or coin.get("total_supply") or 0.0
    return price * supply if supply > 0 else 0.0


def _resolve_circ_pct(coin: dict) -> float:
    circ  = coin.get("circulating_supply") or 0.0
    denom = coin.get("max_supply") or coin.get("total_supply") or 0.0
    return (circ / denom * 100.0) if denom > 0 and circ > 0 else 0.0


def _best_category_label(tags: list[str]) -> str:
    _MAP = {
        "layer-1":               "Layer 1",
        "layer-2":               "Layer 2",
        "ai-big-data":           "AI & Big Data",
        "artificial-intelligence": "AI & Big Data",
        "depin":                 "DePIN",
        "real-world-assets":     "RWA",
        "rwa":                   "RWA",
        "tokenized-assets":      "RWA",
        "gaming":                "Gaming",
        "gamefi":                "Gaming",
        "play-to-earn":          "Gaming",
        "metaverse":             "Gaming",
        "fan-token":             "Fan Token",
        "privacy":               "Privacy",
    }
    for t in tags:
        if t in _MAP:
            return _MAP[t]
    return tags[0] if tags else "Unknown"


# ══════════════════════════════════════════════════════════════════════════════
# Stage 2 + 3a: Technical Analysis
# ══════════════════════════════════════════════════════════════════════════════

def _check_technicals(mexc_symbol: str, vol_threshold: float = cfg.MOMENTUM_TA_VOL_RATIO_MIN) -> TechResult | None:
    """
    Fetch MEXC futures klines and evaluate the two-layer TA gate.
    Returns None only when kline data is genuinely unavailable.
    """
    df_4h  = get_mexc_futures_klines(mexc_symbol, "4h",  limit=cfg.MOMENTUM_TA_4H_LIMIT)
    df_15m = get_mexc_futures_klines(mexc_symbol, "15m", limit=cfg.MOMENTUM_TA_15M_LIMIT)

    if df_4h is None or len(df_4h) < 30:
        log.debug(f"  TA skip {mexc_symbol}: 4H futures data unavailable")
        return None
    if df_15m is None or len(df_15m) < 20:
        log.debug(f"  TA skip {mexc_symbol}: 15m futures data unavailable")
        return None

    close_4h = df_4h["close"]

    # ── 4H Macro Filter ──────────────────────────────────────────────────────
    ema6_4h_s  = compute_ema(close_4h, 6)
    ema20_4h_s = compute_ema(close_4h, 20)
    h4_ema6    = float(ema6_4h_s.iloc[-1])
    h4_ema12   = float(compute_ema(close_4h, 12).iloc[-1])
    h4_ema20   = float(ema20_4h_s.iloc[-1])
    h4_ema_sep = (h4_ema6 - h4_ema20) / h4_ema20 if h4_ema20 > 0 else 0.0
    h4_ema_ok  = (h4_ema6 > h4_ema12 > h4_ema20 and
                  h4_ema_sep >= cfg.MOMENTUM_TA_H4_EMA_SEP_MIN)

    # EMA20 slope over last 5 candles — used by gate 2g squeeze detection
    h4_ema20_slope = 0.0
    if len(ema20_4h_s) >= 6:
        prev5 = float(ema20_4h_s.iloc[-6])
        h4_ema20_slope = (h4_ema20 - prev5) / prev5 * 100.0 if prev5 > 0 else 0.0

    # Compression days: consecutive 4H candles where EMA6/EMA20 spread < 3% (PART 3)
    h4_compression_days = 0
    for _ci in range(1, min(len(ema6_4h_s), 121)):   # max 120 candles ≈ 20 days
        _e6  = float(ema6_4h_s.iloc[-_ci])
        _e20 = float(ema20_4h_s.iloc[-_ci])
        _sp  = abs(_e6 - _e20) / _e20 * 100.0 if _e20 > 0 else 999.0
        if _sp < cfg.MOMENTUM_SQUEEZE_EMA_SPREAD_MAX:
            h4_compression_days += 1
        else:
            break
    h4_compression_days = max(1, h4_compression_days // 4)  # 4H candles → calendar days

    h4_rsi6    = float(compute_rsi(close_4h, period=6).iloc[-1])
    h4_dif, h4_dea, _ = compute_macd(close_4h)
    h4_macd_ok = float(h4_dif.iloc[-1]) > float(h4_dea.iloc[-1])

    # Method B: 4H momentum check (alternative to full EMA stack)
    h4_method_b = False
    if len(df_4h) >= 3:
        _c0      = float(close_4h.iloc[-1])
        _c2      = float(close_4h.iloc[-3])
        _o0      = float(df_4h["open"].iloc[-1])
        _dif_now = float(h4_dif.iloc[-1])
        _dif_prv = float(h4_dif.iloc[-2]) if len(h4_dif) >= 2 else _dif_now
        h4_method_b = (
            _c0 > _c2 and        # price higher than 8H ago
            _dif_now > _dif_prv and   # MACD DIF rising
            h4_rsi6 > 42 and     # RSI above oversold
            _c0 > _o0            # green current candle
        )

    _, _, j4h = compute_kdj(df_4h)
    h4_kdj_j  = float(j4h.iloc[-1])
    h4_kdj_ok = h4_kdj_j < cfg.MOMENTUM_TA_H4_KDJ_J_MAX   # informational only — no longer a hard gate

    # KDJ is a warning, not a blocker — only EMA stack scores
    macro_ok = h4_ema_ok   # kept for legacy display compatibility

    # Method C: kept for backwards-compat with alert display fields
    _mc_ema_gap = (h4_ema20 - h4_ema6) / h4_ema20 if h4_ema20 > 0 else 1.0
    _mc_rising  = len(close_4h) >= 3 and float(close_4h.iloc[-1]) > float(close_4h.iloc[-3])
    h4_method_c = (
        not h4_ema_ok and
        _mc_ema_gap < 0.015 and
        _mc_rising and
        h4_rsi6 > 35
    )

    # ── 4H Score factor (replaces hard gate) ─────────────────────────────────
    if h4_ema_ok:                    # EMA6 > EMA12 > EMA20 AND sep ≥ 0.2%
        h4_score, h4_status = 20, "FULL"
    elif h4_ema6 > h4_ema20:        # EMA6 above EMA20 but not fully stacked
        h4_score, h4_status = 8, "PARTIAL"
    elif h4_ema6 < h4_ema20:        # EMA6 below EMA20 — bearish
        h4_score, h4_status = -10, "BEARISH"
    else:
        h4_score, h4_status = 0, "NEUTRAL"

    # ── 1H directional score ─────────────────────────────────────────────────
    h1_score, h1_status = 0, "UNKNOWN"
    df_1h = get_mexc_futures_klines(mexc_symbol, "1h", limit=30)
    if df_1h is not None and len(df_1h) >= 10:
        close_1h = df_1h["close"]
        h1_ema6  = float(compute_ema(close_1h, 6).iloc[-1])
        h1_ema20 = float(compute_ema(close_1h, 20).iloc[-1])
        h1_rsi6  = float(compute_rsi(close_1h, period=6).iloc[-1])
        if h1_ema6 > h1_ema20 and h1_rsi6 > 50:
            h1_score, h1_status = 10, "BULLISH"
        elif h1_rsi6 > 48:
            h1_score, h1_status = 5, "NEUTRAL"
        else:
            h1_score, h1_status = 0, "WEAK"

    # ── 4H Volume ────────────────────────────────────────────────────────────
    vol_last = float(df_4h["volume"].iloc[-1])
    vol_ma10 = float(df_4h["volume"].rolling(10).mean().iloc[-1])
    vol_pct  = (vol_last / vol_ma10 * 100.0) if vol_ma10 > 0 else 0.0
    vol_ok   = vol_pct >= vol_threshold * 100
    vol_pts  = _PTS_VOL if vol_ok else 0

    # ── 15m Scoring ──────────────────────────────────────────────────────────
    close_15m = df_15m["close"]

    ema6_15m  = compute_ema(close_15m,  6)
    ema20_15m = compute_ema(close_15m, 20)
    m15_ema6  = float(ema6_15m.iloc[-1])
    m15_ema20 = float(ema20_15m.iloc[-1])
    m15_ema_ok  = m15_ema6 > m15_ema20
    m15_ema_pts = _PTS_EMA if m15_ema_ok else 0

    # 15m EMA12 — for GATE 2a confirmation
    ema12_15m           = compute_ema(close_15m, 12)
    m15_ema12           = float(ema12_15m.iloc[-1])
    m15_ema6_gt_ema12   = m15_ema6 > m15_ema12

    # Golden cross: EMA6 was below EMA20 two candles ago, now above (fresh cross)
    m15_golden_cross = (
        len(ema6_15m) >= 3 and
        float(ema6_15m.iloc[-3]) < float(ema20_15m.iloc[-3]) and
        m15_ema6 > m15_ema20
    )

    m15_price              = float(close_15m.iloc[-1])
    m15_price_ok           = m15_price > m15_ema20
    m15_price_pts          = _PTS_PRICE if m15_price_ok else 0
    m15_price_gt_h4_ema20  = m15_price > h4_ema20   # breakout above 4H EMA20 (squeeze check)

    m15_rsi6     = float(compute_rsi(close_15m, period=6).iloc[-1])
    m15_rsi6_ok  = cfg.MOMENTUM_TA_15M_RSI6_MIN <= m15_rsi6 <= cfg.MOMENTUM_RSI_MAX
    m15_rsi6_hot = m15_rsi6 > cfg.MOMENTUM_RSI_MAX
    m15_rsi6_pts = _PTS_RSI6 if m15_rsi6_ok else 0

    _, _, j15   = compute_kdj(df_15m)
    m15_kdj_j   = float(j15.iloc[-1])
    m15_kdj_ok  = m15_kdj_j < cfg.MOMENTUM_KDJ_MAX
    m15_kdj_hot = m15_kdj_j >= cfg.MOMENTUM_KDJ_MAX
    m15_kdj_pts = _PTS_KDJ if m15_kdj_ok else 0

    dif_s, dea_s, _ = compute_macd(close_15m)
    m15_macd_dif = float(dif_s.iloc[-1])
    m15_macd_dea = float(dea_s.iloc[-1])
    m15_macd_ok  = m15_macd_dif > m15_macd_dea
    m15_macd_pts = _PTS_MACD if m15_macd_ok else 0

    score = (m15_ema_pts + m15_price_pts + m15_rsi6_pts +
             m15_kdj_pts + m15_macd_pts + vol_pts)

    # Last 15m candle % change — used for EARLY SIGNAL detection in scan()
    m15_change = ((float(close_15m.iloc[-1]) - float(close_15m.iloc[-2])) /
                   float(close_15m.iloc[-2]) * 100.0) if len(close_15m) >= 2 else 0.0

    # ── 15m Volume Spike ─────────────────────────────────────────────────────
    vol_15m         = df_15m["volume"]
    m15_vol_last    = float(vol_15m.iloc[-1])
    m15_vol_prev3   = float(vol_15m.iloc[-4:-1].mean()) if len(vol_15m) >= 4 else m15_vol_last
    m15_vol_spike_ratio = m15_vol_last / m15_vol_prev3 if m15_vol_prev3 > 0 else 0.0
    m15_vol_spike   = m15_vol_spike_ratio >= cfg.MOMENTUM_VS_VOL_MULT

    # ── 4H price ranges ──────────────────────────────────────────────────────
    h4_highs  = df_4h["high"]
    h4_lows   = df_4h["low"]
    h24_high  = float(h4_highs.iloc[-6:].max())   # 6 × 4H = 24H
    h24_low   = float(h4_lows.iloc[-6:].min())
    h16d_high = float(h4_highs.max())             # full kline window ≈ 16 days
    ath_dist_pct = ((h16d_high - float(close_4h.iloc[-1])) / h16d_high * 100.0
                    if h16d_high > 0 else 0.0)

    return TechResult(
        h4_ema6=round(h4_ema6, 8),    h4_ema12=round(h4_ema12, 8),  h4_ema20=round(h4_ema20, 8),
        h4_ema_ok=h4_ema_ok,          h4_ema_sep=round(h4_ema_sep * 100, 3),
        h4_kdj_j=round(h4_kdj_j, 2),  h4_kdj_ok=h4_kdj_ok,
        macro_ok=macro_ok,
        m15_ema6=round(m15_ema6, 8),   m15_ema20=round(m15_ema20, 8),
        m15_ema_ok=m15_ema_ok,         m15_ema_pts=m15_ema_pts,
        m15_price=round(m15_price, 8), m15_price_ok=m15_price_ok,    m15_price_pts=m15_price_pts,
        m15_rsi6=round(m15_rsi6, 2),   m15_rsi6_ok=m15_rsi6_ok,
        m15_rsi6_hot=m15_rsi6_hot,     m15_rsi6_pts=m15_rsi6_pts,
        m15_kdj_j=round(m15_kdj_j, 2), m15_kdj_ok=m15_kdj_ok,
        m15_kdj_hot=m15_kdj_hot,        m15_kdj_pts=m15_kdj_pts,
        m15_macd_dif=round(m15_macd_dif, 8), m15_macd_dea=round(m15_macd_dea, 8),
        m15_macd_ok=m15_macd_ok,        m15_macd_pts=m15_macd_pts,
        vol_pct=round(vol_pct, 1),     vol_ok=vol_ok,  vol_pts=vol_pts,
        m15_change=round(m15_change, 2),
        m15_golden_cross=m15_golden_cross,
        m15_vol_spike=m15_vol_spike,
        m15_vol_spike_ratio=round(m15_vol_spike_ratio, 2),
        h24_high=round(h24_high, 8),
        h24_low=round(h24_low, 8),
        h16d_high=round(h16d_high, 8),
        ath_dist_pct=round(ath_dist_pct, 1),
        score=score,
        h4_rsi6=round(h4_rsi6, 2),
        h4_macd_ok=h4_macd_ok,
        m15_ema12=round(m15_ema12, 8),
        m15_ema6_gt_ema12=m15_ema6_gt_ema12,
        h4_ema20_slope=round(h4_ema20_slope, 3),
        m15_price_gt_h4_ema20=m15_price_gt_h4_ema20,
        h4_compression_days=h4_compression_days,
        h4_method_b=h4_method_b,
        h4_method_c=h4_method_c,
        h4_score=h4_score,
        h4_status=h4_status,
        h1_score=h1_score,
        h1_status=h1_status,
    )


# ══════════════════════════════════════════════════════════════════════════════
# Stage 3b: Fundamental bonus scoring
# ══════════════════════════════════════════════════════════════════════════════

def _score_fundamentals(change_1h: float, mcap: float,
                         circ_pct: float, fdv_ratio: float) -> FundResult:
    """Compute the four fundamental bonus dimensions (0-40 pts total)."""

    mcap_pts = (cfg.MOMENTUM_FUND_MCAP_PTS
                if cfg.MOMENTUM_FUND_MCAP_L1_MIN_USD <= mcap <= cfg.MOMENTUM_FUND_MCAP_L1_MAX_USD
                else 0)

    circ_pts = cfg.MOMENTUM_FUND_CIRC_PTS if circ_pct > cfg.MOMENTUM_FUND_CIRC_MIN_PCT else 0

    fdv_pts  = cfg.MOMENTUM_FUND_FDV_PTS  if fdv_ratio < cfg.MOMENTUM_FUND_FDV_RATIO_MAX else 0

    if cfg.MOMENTUM_FUND_1H_SWEET_MIN_PCT <= change_1h <= cfg.MOMENTUM_FUND_1H_SWEET_MAX_PCT:
        gain_pts = cfg.MOMENTUM_FUND_1H_SWEET_PTS   # 5-10% → +5
    elif change_1h < cfg.MOMENTUM_FUND_1H_SWEET_MIN_PCT:
        gain_pts = cfg.MOMENTUM_FUND_1H_OK_PTS       # 3-5%  → +2
    else:
        gain_pts = 0                                  # >10%  → +0

    total = mcap_pts + circ_pts + fdv_pts + gain_pts
    return FundResult(mcap_pts=mcap_pts, circ_pts=circ_pts,
                      fdv_pts=fdv_pts,   gain_pts=gain_pts, total=total)


# ══════════════════════════════════════════════════════════════════════════════
# Risk warning generator
# ══════════════════════════════════════════════════════════════════════════════

def _generate_warnings(tech: TechResult, change_1h: float,
                        circ_pct: float, fdv_ratio: float,
                        has_inf_supply: bool = False,
                        is_micro_cap: bool = False,
                        mcap: float = 0.0) -> list[str]:
    """Generate warnings in canonical order: supply → dilution → overbought → micro-cap."""
    supply:    list[str] = []
    dilution:  list[str] = []
    optional:  list[str] = []
    micro_cap: list[str] = []

    # 1. Supply
    if has_inf_supply:
        supply.append("Max Supply unknown — inflation risk")
    # 2. Dilution
    if circ_pct > 0 and circ_pct < cfg.MOMENTUM_WARN_CIRC_ALERT_PCT:
        dilution.append(f"Circ Rate: {circ_pct:.0f}% — dilution risk")
    if fdv_ratio > cfg.MOMENTUM_WARN_FDV_ALERT_RATIO:
        dilution.append(f"FDV/MCap: {fdv_ratio:.1f}×")
    # 3. Overbought / technical risk
    if tech.h4_kdj_j >= cfg.MOMENTUM_TA_H4_KDJ_J_MAX:
        optional.append(f"4H KDJ {tech.h4_kdj_j:.0f} — overheated, watch for reversal")
    if tech.m15_rsi6 > cfg.MOMENTUM_WARN_RSI6_PCT:
        optional.append("RSI approaching overbought")
    if tech.m15_kdj_j > cfg.MOMENTUM_WARN_KDJ_J_PCT:
        optional.append("KDJ getting hot")
    if change_1h > cfg.MOMENTUM_WARN_GAIN_LATE_PCT:
        optional.append("Late in momentum cycle")
    if tech.vol_pct < cfg.MOMENTUM_WARN_VOL_LOW_PCT:
        optional.append("Volume not exceptional")
    # 4. Micro-cap (always last)
    if is_micro_cap:
        micro_cap.append(f"Micro-Cap: ${mcap/1e6:.0f}M — half position size")
        micro_cap.append("Treat as high-risk, max 50% normal margin")

    return supply + dilution + optional[:2] + micro_cap


# ══════════════════════════════════════════════════════════════════════════════
# Main scan
# ══════════════════════════════════════════════════════════════════════════════

def _check_5m_pbw(mexc_symbol: str, fear_mode: bool = False) -> "dict | None":
    """
    Check 5m candles for Pre-Breakout Watch conditions.
    Returns a dict with computed values, or None if data unavailable.
    """
    df_5m = get_mexc_futures_klines(mexc_symbol, "5m", limit=100)
    if df_5m is None or len(df_5m) < 20:
        return None

    close_5m = df_5m["close"]
    ema6_s   = compute_ema(close_5m, 6)
    ema20_s  = compute_ema(close_5m, 20)
    m5_ema6  = float(ema6_s.iloc[-1])
    m5_ema20 = float(ema20_s.iloc[-1])
    ema_spread = (abs(m5_ema6 - m5_ema20) / m5_ema20 * 100.0) if m5_ema20 > 0 else 999.0

    rsi_s = compute_rsi(close_5m, period=6)
    if len(rsi_s) < cfg.MOMENTUM_PBW_RSI_CANDLES:
        return None

    rsi_threshold = cfg.MOMENTUM_PBW_RSI_MAX_FEAR if fear_mode else cfg.MOMENTUM_PBW_RSI_MAX
    latest_rsi    = float(rsi_s.iloc[-1])

    # Count consecutive recent candles with RSI < streak threshold
    streak = 0
    for i in range(1, len(rsi_s) + 1):
        if float(rsi_s.iloc[-i]) < rsi_threshold:
            streak += 1
        else:
            break

    vol_s    = df_5m["volume"]
    vol_last = float(vol_s.iloc[-1])
    vol_ma10 = float(vol_s.rolling(10).mean().iloc[-1])
    vol_ratio = vol_last / vol_ma10 if vol_ma10 > 0 else 0.0

    return {
        "ema_spread":   round(ema_spread, 3),
        "compressed":   ema_spread < cfg.MOMENTUM_PBW_EMA_SPREAD_MAX,
        "rsi_streak":   streak,
        "rsi_ok":       streak >= cfg.MOMENTUM_PBW_RSI_CANDLES,
        "latest_rsi":   round(latest_rsi, 1),
        "trigger_rsi":  latest_rsi < cfg.MOMENTUM_PBW_TRIGGER_RSI_MAX,
        "vol_ratio":    round(vol_ratio, 2),
        "trigger_vol":  vol_ratio >= cfg.MOMENTUM_PBW_VOL_MULT,
        "trigger_px":   float(close_5m.iloc[-1]) > m5_ema20,
    }


def _check_5m(mexc_symbol: str) -> "dict | None":
    """
    Fetch 5m candles and compute precision-layer indicators.
    Returns a plain dict used both for soft gate display and the unified 5m primary gate.
    """
    df_5m = get_mexc_futures_klines(mexc_symbol, "5m", limit=cfg.MOMENTUM_TA_5M_LIMIT)
    if df_5m is None or len(df_5m) < 20:
        return None

    close_5m = df_5m["close"]
    ema6_s   = compute_ema(close_5m,  6)
    ema12_s  = compute_ema(close_5m, 12)
    ema20_s  = compute_ema(close_5m, 20)
    m5_ema6  = float(ema6_s.iloc[-1])
    m5_ema12 = float(ema12_s.iloc[-1])
    m5_ema20 = float(ema20_s.iloc[-1])

    rsi_s   = compute_rsi(close_5m, period=6)
    m5_rsi6 = float(rsi_s.iloc[-1])

    _, _, j_s = compute_kdj(df_5m)
    m5_kdj_j     = float(j_s.iloc[-1])
    m5_kdj_prev  = float(j_s.iloc[-2]) if len(j_s) >= 2 else m5_kdj_j
    m5_kdj_rising = m5_kdj_j > m5_kdj_prev

    m5_price     = float(close_5m.iloc[-1])
    m5_open_last = float(df_5m["open"].iloc[-1])

    vol_s    = df_5m["volume"]
    vol_last = float(vol_s.iloc[-1])
    vol_ma10 = float(vol_s.rolling(10).mean().iloc[-1])
    m5_vol_pct = (vol_last / vol_ma10 * 100.0) if vol_ma10 > 0 else 0.0

    # Fresh EMA6/EMA20 cross: was below 3 candles ago, now above
    m5_fresh_cross = (
        len(ema6_s) >= 3 and
        float(ema6_s.iloc[-3]) < float(ema20_s.iloc[-3]) and
        m5_ema6 > m5_ema20
    )

    # Background volume level: avg of prior 9 candles (excludes trigger candle)
    vol_prior9     = float(vol_s.iloc[-10:-1].mean()) if len(vol_s) >= 10 else vol_last
    vol_recent_pct = (vol_prior9 / vol_ma10 * 100.0) if vol_ma10 > 0 else 100.0

    # 2-candle wick filter (PART C): confirm cross on the NEXT candle, not the cross candle itself
    m5_low          = float(df_5m["low"].iloc[-1])
    candle_gain_pct = ((m5_price - m5_open_last) / m5_open_last * 100.0) if m5_open_last > 0 else 0.0
    is_spike        = m5_vol_pct >= 500.0 or candle_gain_pct >= 5.0  # VS / Speed exception

    # Cross happened at candle[-2] (was below at [-3], above at [-2]) → current[-1] is confirmation
    cross_was_prev_candle = (
        len(ema6_s) >= 3 and
        float(ema6_s.iloc[-3]) < float(ema20_s.iloc[-3]) and
        float(ema6_s.iloc[-2]) > float(ema20_s.iloc[-2])
    )
    candle_confirmed = (
        cross_was_prev_candle and
        m5_price > m5_ema20 and         # close above EMA20
        m5_low > m5_ema20 * 0.998 and   # low didn't wick below EMA20
        vol_last > vol_ma10 * 0.8       # vol at least 80% of MA10
    )

    return {
        "rsi6":               round(m5_rsi6, 2),
        "kdj_j":              round(m5_kdj_j, 2),
        "kdj_rising":         m5_kdj_rising,
        "price_above_ema20":  m5_price > m5_ema20,
        "ema6_gt_ema12":      m5_ema6 > m5_ema12,
        "ema6_gt_ema20":      m5_ema6 > m5_ema20,
        "fresh_cross":        m5_fresh_cross,
        "first_green":        m5_price > m5_open_last,
        "vol_pct":            round(m5_vol_pct, 1),
        "vol_recent_pct":     round(vol_recent_pct, 1),
        "ema6":               m5_ema6,    # raw float — needed for RADAR approach check
        "ema20":              m5_ema20,   # raw float for entry zone
        "candle_confirmed":   candle_confirmed,
        "is_spike":           is_spike,
    }


def _price_decimals(price: float) -> int:
    """Return decimal places for MEXC-ready price display."""
    if price >= 0.10:
        return 4
    elif price >= 0.01:
        return 5
    elif price >= 0.001:
        return 6
    else:
        return 7


def _base_5m_gate_ok(m5: "dict | None") -> bool:
    """
    Unified 5m primary gate — replaces per-signal 1H gates.
    Passes if: (EMA6 > EMA20 OR fresh cross) AND RSI 25-75 AND vol >= 1.2× MA10.
    Fresh crosses additionally require 2-candle wick confirmation (PART C),
    unless the signal is a spike (vol ≥5× or candle gain ≥5%).
    """
    if m5 is None:
        return False
    ema_ok = m5["ema6_gt_ema20"] or m5["fresh_cross"]
    rsi_ok = cfg.MOMENTUM_5M_GATE_RSI_MIN <= m5["rsi6"] <= cfg.MOMENTUM_5M_GATE_RSI_MAX
    vol_ok = m5["vol_pct"] >= cfg.MOMENTUM_5M_GATE_VOL_MIN * 100

    # 2-candle wick filter: fresh crosses must be confirmed on the NEXT candle
    if m5.get("fresh_cross") and not (m5.get("candle_confirmed", False) or m5.get("is_spike", False)):
        return False

    return ema_ok and rsi_ok and vol_ok


def _apply_5m_to_tech(tech: TechResult, m5: "dict | None") -> None:
    """Populate TechResult 5m fields from _check_5m() result (mutates in place)."""
    if m5 is None:
        return
    tech.m5_rsi6              = m5["rsi6"]
    tech.m5_kdj_j             = m5["kdj_j"]
    tech.m5_kdj_rising        = m5["kdj_rising"]
    tech.m5_price_above_ema20 = m5["price_above_ema20"]
    tech.m5_ema6_gt_ema12     = m5["ema6_gt_ema12"]
    tech.m5_ema6_gt_ema20     = m5.get("ema6_gt_ema20", False)
    tech.m5_fresh_cross       = m5.get("fresh_cross", False)
    tech.m5_first_green       = m5["first_green"]
    tech.m5_vol_pct           = m5["vol_pct"]
    tech.m5_vol_recent_pct    = m5.get("vol_recent_pct", 100.0)
    tech.m5_ema20             = m5.get("ema20", 0.0)
    # Evaluate 5m overall health — all three required for ✅
    if m5["rsi6"] >= cfg.MOMENTUM_5M_RSI_HOT:
        tech.m5_ok   = False
        tech.m5_note = f"⏳ 5m overheated (RSI {m5['rsi6']:.0f}) — wait for pullback"
    elif not m5["kdj_rising"]:
        tech.m5_ok   = False
        tech.m5_note = "5m KDJ not rising — wait for entry zone"
    elif not m5["price_above_ema20"]:
        tech.m5_ok   = False
        tech.m5_note = "5m price below EMA20 — wait for entry zone"
    else:
        tech.m5_ok   = True
        tech.m5_note = "✅ 2-candle confirmed — not a wick" if m5.get("candle_confirmed") else ""


def _ath_score(dist_pct: float) -> int:
    """Bonus (+) or penalty (-) based on real ATH distance. Hard block handled by caller."""
    if dist_pct > cfg.MOMENTUM_ATH_DIST_L2:   # > 90% → +15
        return cfg.MOMENTUM_ATH_DIST_L2_PTS
    if dist_pct > cfg.MOMENTUM_ATH_DIST_L1:   # 80–90% → +10
        return cfg.MOMENTUM_ATH_DIST_L1_PTS
    if dist_pct > cfg.MOMENTUM_ATH_DIST_L3:   # 60–80% → +3
        return cfg.MOMENTUM_ATH_DIST_L3_PTS
    if dist_pct > cfg.MOMENTUM_ATH_DIST_P1:   # 40–60% → 0
        return 0
    if dist_pct > cfg.MOMENTUM_ATH_DIST_P2:   # 20–40% → -5
        return cfg.MOMENTUM_ATH_DIST_P2_PTS
    return cfg.MOMENTUM_ATH_DIST_P3_PTS        # ≤ 20% → -12


def _lookup_ath(ath_map: dict, symbol: str, price: float,
                fallback_dist: float) -> tuple[float, float, str]:
    """Return (dist_pct, ath_price, ath_date) from CoinGecko map or fallback."""
    info      = ath_map.get(symbol, {})
    ath_price = float(info.get("ath") or 0)
    ath_date  = info.get("ath_date") or ""
    if ath_price > 0 and price > 0:
        dist = round((ath_price - price) / ath_price * 100.0, 1)
        return dist, round(ath_price, 8), ath_date
    return fallback_dist, 0.0, ""


def _lookup_atl(ath_map: dict, symbol: str) -> float:
    """Return ATL price from CoinGecko map (0.0 if not available)."""
    info = ath_map.get(symbol, {})
    return float(info.get("atl") or 0)


def _score_squeeze(tech: TechResult, mcap: float, circ_pct: float, has_inf_supply: bool) -> int:
    """
    Compute BB-Squeeze Breakout quality score (PART 3).
    Base 70; bonuses for extreme vol, ATH distance, circ%, MCap; penalties for unknown supply / low circ.
    """
    score = cfg.MOMENTUM_SQ_BASE_SCORE
    if tech.m15_vol_spike_ratio >= cfg.MOMENTUM_SQ_VOL_EXTREME:
        score += cfg.MOMENTUM_SQ_VOL_EXTREME_PTS
    # ATH bonus/penalty applied externally via _ath_score() in the caller
    if circ_pct >= cfg.MOMENTUM_SQ_CIRC_BONUS_PCT:
        score += cfg.MOMENTUM_SQ_CIRC_BONUS_PTS
    if mcap > cfg.MOMENTUM_SQ_MCAP_BONUS_USD:
        score += cfg.MOMENTUM_SQ_MCAP_BONUS_PTS
    if has_inf_supply:
        score -= cfg.MOMENTUM_SQ_INF_SUPPLY_PEN
    if 0 < circ_pct < cfg.MOMENTUM_SQ_CIRC_LOW_PCT:
        score -= cfg.MOMENTUM_SQ_CIRC_LOW_PEN
    return score


def _check_15m_fasttrack(mexc_symbol: str) -> bool:
    """
    Fast-track bypass (CHANGE 1C): check if the last closed 15m candle is a spike.
    Qualifies if: candle gain ≥ 3% AND candle vol ≥ 5× avg of prior 3 candles.
    Used to admit coins that pass M2-M7 but sit below the M1 1H gain floor.
    """
    df = get_mexc_futures_klines(mexc_symbol, "15m", limit=5)
    if df is None or len(df) < 4:
        return False
    last_open  = float(df["open"].iloc[-1])
    last_close = float(df["close"].iloc[-1])
    candle_gain = (last_close - last_open) / last_open * 100.0 if last_open > 0 else 0.0
    if candle_gain < cfg.MOMENTUM_FT_15M_GAIN_MIN:
        return False
    vol = df["volume"]
    vol_last  = float(vol.iloc[-1])
    vol_prev3 = float(vol.iloc[-4:-1].mean()) if len(vol) >= 4 else vol_last
    return vol_prev3 > 0 and vol_last >= cfg.MOMENTUM_FT_15M_VOL_MULT * vol_prev3


def _check_5m_gc(mexc_symbol: str) -> "dict | None":
    """
    Detect a fresh 5m EMA6/EMA20 golden cross for the Early GC signal.
    Cross is fresh if EMA6 was below EMA20 three candles ago and is now above.
    Returns dict with cross data, or None if conditions not met.
    """
    df = get_mexc_futures_klines(mexc_symbol, "5m", limit=30)
    if df is None or len(df) < 15:
        return None

    closes = df["close"].astype(float)
    vols   = df["volume"].astype(float)

    ema6_s  = closes.ewm(span=6,  adjust=False).mean()
    ema20_s = closes.ewm(span=20, adjust=False).mean()

    m5_ema6  = float(ema6_s.iloc[-1])
    m5_ema20 = float(ema20_s.iloc[-1])

    # Fresh cross: was below EMA20 three candles ago, now above
    if not (len(ema6_s) >= 3 and
            float(ema6_s.iloc[-3]) < float(ema20_s.iloc[-3]) and
            m5_ema6 > m5_ema20):
        return None

    # RSI6 on 5m
    delta    = closes.diff()
    gain     = delta.clip(lower=0)
    loss     = (-delta).clip(lower=0)
    avg_gain = gain.rolling(6).mean()
    avg_loss = loss.rolling(6).mean()
    rs       = avg_gain / avg_loss.replace(0, 1e-9)
    rsi_s    = 100 - 100 / (1 + rs)
    m5_rsi6  = float(rsi_s.iloc[-1]) if len(rsi_s) >= 6 else 50.0

    if not (cfg.MOMENTUM_EARLY_GC_RSI_MIN <= m5_rsi6 <= cfg.MOMENTUM_EARLY_GC_RSI_MAX):
        return None

    # Volume: last candle vs MA10 of prior candles
    vol_last  = float(vols.iloc[-1])
    vol_ma10  = float(vols.iloc[-11:-1].mean()) if len(vols) >= 11 else vol_last
    vol_ratio = vol_last / vol_ma10 if vol_ma10 > 0 else 0.0

    if vol_ratio < cfg.MOMENTUM_EARLY_GC_VOL_MIN:
        return None

    return {
        "m5_ema6":   round(m5_ema6, 8),
        "m5_ema20":  round(m5_ema20, 8),
        "m5_rsi6":   round(m5_rsi6, 1),
        "vol_ratio": round(vol_ratio, 2),
        "price":     float(closes.iloc[-1]),
    }


def _score_early_gc(tech: "TechResult | None", m5gc: dict, ath_dist_pct: float) -> int:
    """Score an Early GC signal. Base 65; bonuses/penalties applied."""
    score = 65

    if tech is not None:
        if tech.m15_ema6_gt_ema12 and tech.m15_ema_ok:
            score += 5            # +5 if 15m fully bullish (EMA6 > EMA12 > EMA20)
        if tech.h4_kdj_j < 60:
            score += 3            # +3 if H4 KDJ < 60 (not overheated)
        if tech.h4_kdj_j > 90:
            score -= 5            # -5 if H4 KDJ > 90 (overheated)

    if ath_dist_pct >= 80.0:
        score += 5                # +5 if far from ATH (more room to run)

    if m5gc["vol_ratio"] >= 2.0:
        score += 5                # +5 if vol > 2× MA10

    if m5gc["m5_rsi6"] > 65.0:
        score -= 5                # -5 if RSI already elevated

    return score


def _check_speed_alert(mexc_symbol: str) -> "dict | None":
    """
    Detect explosive 15m candle moves for the Speed Alert Track.
    Returns dict with spike data or None if conditions not met.
    Fields: candle_gain, vol_ratio, pre_rsi (second-to-last 15m RSI6 proxy), h4_ema_spread_pct
    """
    df = get_mexc_futures_klines(mexc_symbol, "15m", limit=25)
    if df is None or len(df) < 10:
        return None

    closes = df["close"].astype(float)
    opens  = df["open"].astype(float)
    vols   = df["volume"].astype(float)

    last_open  = float(opens.iloc[-1])
    last_close = float(closes.iloc[-1])
    candle_gain = (last_close - last_open) / last_open * 100.0 if last_open > 0 else 0.0
    if candle_gain < cfg.MOMENTUM_SPEED_15M_GAIN_MIN:
        return None

    vol_last  = float(vols.iloc[-1])
    vol_prev3 = float(vols.iloc[-4:-1].mean()) if len(vols) >= 4 else vol_last
    if vol_prev3 <= 0:
        return None
    vol_ratio = vol_last / vol_prev3
    if vol_ratio < cfg.MOMENTUM_SPEED_VOL_MULT:
        return None

    # Pre-spike RSI6 approximation: use RSI on second-to-last candle window
    delta = closes.iloc[:-1].diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.rolling(6).mean()
    avg_loss = loss.rolling(6).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-9)
    rsi_series = 100 - 100 / (1 + rs)
    pre_rsi = float(rsi_series.iloc[-1]) if len(rsi_series) >= 6 else 50.0
    if pre_rsi > cfg.MOMENTUM_SPEED_PRE_RSI_MAX:
        return None

    # 4H EMA spread check (not deeply bearish)
    df4h = get_mexc_futures_klines(mexc_symbol, "4h", limit=30)
    h4_ema_spread_pct = 0.0
    if df4h is not None and len(df4h) >= 20:
        c4 = df4h["close"].astype(float)
        ema6  = c4.ewm(span=6,  adjust=False).mean().iloc[-1]
        ema20 = c4.ewm(span=20, adjust=False).mean().iloc[-1]
        if ema20 > 0:
            h4_ema_spread_pct = (ema6 - ema20) / ema20 * 100.0
    if h4_ema_spread_pct < cfg.MOMENTUM_SPEED_H4_EMA_MAX_NEG:
        return None

    return {
        "candle_gain":      round(candle_gain, 2),
        "vol_ratio":        round(vol_ratio, 1),
        "pre_rsi":          round(pre_rsi, 1),
        "h4_ema_spread_pct": round(h4_ema_spread_pct, 3),
        "price":            last_close,
    }


def _check_stage0(mexc_symbol: str, price: float) -> dict | None:
    """
    Stage 0 pre-breakout qualification check.
    Returns {consolidation_high, ma10_vol, range_pct, rsi5m} or None.

    Conditions (all must pass):
      - 45m price range ≤ MOMENTUM_S0_RANGE_MAX_PCT (consolidation)
      - 5m RSI6 in [MOMENTUM_S0_RSI_MIN, MOMENTUM_S0_RSI_MAX] (not overbought/oversold)
      - Volume building: last 5m vol ≥ MOMENTUM_S0_VOL_BUILD_MIN × avg prior 4
      - No new 1H low: last candle low > low 12 candles ago
    """
    df5m = get_mexc_futures_klines(mexc_symbol, "5m", limit=25)
    if df5m is None or len(df5m) < 15:
        return None

    closes = df5m["close"].astype(float)
    highs  = df5m["high"].astype(float)
    lows   = df5m["low"].astype(float)
    vols   = df5m["volume"].astype(float)

    # 45m range: last 9 × 5m candles
    window_high = float(highs.iloc[-9:].max())
    window_low  = float(lows.iloc[-9:].min())
    if window_low <= 0:
        return None
    range_pct = (window_high - window_low) / window_low * 100.0
    if range_pct > cfg.MOMENTUM_S0_RANGE_MAX_PCT:
        return None

    # 5m RSI6
    rsi5m = float(compute_rsi(closes, period=6).iloc[-1])
    if not (cfg.MOMENTUM_S0_RSI_MIN <= rsi5m <= cfg.MOMENTUM_S0_RSI_MAX):
        return None

    # Volume building: last candle vol vs avg of prior 4
    if len(vols) < 5:
        return None
    vol_last  = float(vols.iloc[-1])
    vol_prev4 = float(vols.iloc[-5:-1].mean())
    if vol_prev4 <= 0 or vol_last < cfg.MOMENTUM_S0_VOL_BUILD_MIN * vol_prev4:
        return None

    # No new 1H low: last low must be above the low 12 candles ago
    if float(lows.iloc[-1]) <= float(lows.iloc[-13]):
        return None

    # MA10 volume (baseline for later reference)
    ma10_vol = float(vols.iloc[-10:].mean()) if len(vols) >= 10 else float(vols.mean())

    return {
        "consolidation_high": round(window_high, 8),
        "ma10_vol":           round(ma10_vol, 2),
        "range_pct":          round(range_pct, 2),
        "rsi5m":              round(rsi5m, 1),
    }


def scan() -> list[MomentumResult]:
    """
    Run one full momentum scan.

    Returns STRONG ENTRY (≥80) and WATCH (≥65) coins only,
    sorted by total score descending, capped at MOMENTUM_MAX_ALERTS_PER_SCAN,
    and filtered by cooldown.
    """
    _refresh_market_context()   # update F&G + BTC 24H once per hour

    _ath_map    = get_coingecko_ath_map(limit=500)   # real ATH from CoinGecko
    futures_set = get_mexc_futures_symbols()
    if not futures_set:
        log.error("Momentum scan aborted — MEXC futures list unavailable.")
        return []

    coins = get_cmc_momentum_listings(
        limit        = cfg.MOMENTUM_CMC_LIMIT,
        mcap_min_usd = cfg.MOMENTUM_MCAP_MIN_USD,
        mcap_max_usd = cfg.MOMENTUM_MCAP_MAX_USD,
    )
    if not coins:
        log.error("Momentum scan aborted — CMC listings unavailable.")
        return []

    log.info(
        f"Momentum scan: {len(coins)} CMC coins, "
        f"{len(futures_set)} MEXC perps cached."
    )

    # ── Stage 1: M1–M7 (unified candidates — 5m primary architecture) ───────────
    candidates: list = []        # coins with 1H ≥ 0.3% passing M2–M7 (standard pipeline)
    stage0_candidates: list = [] # coins with -0.5% ≤ 1H < 0.3% passing M2–M7 (pre-breakout)
    _inf_supply_map: dict[str, bool] = {}   # symbol → has infinite supply
    _micro_cap_set:  set[str] = set()       # symbols that bypassed via micro-cap Vol/MC rule

    # Stage 1 block counters — logged at end of Stage 1 for filter analysis
    _s1_mcap_blocked    = 0
    _s1_vol_blocked     = 0
    _s1_supply_blocked  = 0
    _s1_fdv_blocked     = 0
    _s1_tags_blocked    = 0
    _s1_mexc_blocked    = 0

    for coin in coins:
        symbol = coin.get("symbol", "").upper()
        name   = coin.get("name", "")
        tags   = [t.lower() for t in (coin.get("tags") or [])]
        q      = coin.get("quote", {}).get("USD", {})

        change_1h  = q.get("percent_change_1h")  or 0.0
        change_24h = q.get("percent_change_24h") or 0.0
        price      = q.get("price")              or 0.0
        mcap       = q.get("market_cap")         or 0.0
        vol_24h    = q.get("volume_24h")         or 0.0

        # CMC sorted descending by 1H gain; stop entirely once below Stage 0 floor
        if change_1h < cfg.MOMENTUM_S0_1H_MIN:
            break

        # M2: Market cap (micro-cap bypass: $10M-$25M allowed if Vol/MC > 150%)
        if mcap > cfg.MOMENTUM_MCAP_MAX_USD:
            _s1_mcap_blocked += 1
            continue
        vol_mc_ratio = vol_24h / mcap if mcap > 0 else 0.0
        is_micro_cap = cfg.MOMENTUM_MCAP_MICRO_MIN_USD <= mcap < cfg.MOMENTUM_MCAP_MIN_USD
        if mcap < cfg.MOMENTUM_MCAP_MICRO_MIN_USD:          # hard floor $10M — no exceptions
            _s1_mcap_blocked += 1
            continue
        if is_micro_cap and vol_mc_ratio < cfg.MOMENTUM_MCAP_MICRO_VOLMC_MIN:
            _s1_mcap_blocked += 1
            continue

        # M3: Volume (bypass $5M absolute floor if Vol/MC > 150%; block dead coins < 20%)
        if vol_mc_ratio < cfg.MOMENTUM_VOLMC_DEAD_MAX:      # dead coin — no interest
            _s1_vol_blocked += 1
            continue
        if vol_24h < cfg.MOMENTUM_VOL_24H_MIN_USD and vol_mc_ratio < cfg.MOMENTUM_VOLMC_BYPASS_MIN:
            _s1_vol_blocked += 1
            continue

        # M4: Supply (inf-supply coins bypass circ% gate, get mandatory warning instead)
        has_inf_supply = coin.get("max_supply") is None
        circ_pct = _resolve_circ_pct(coin)
        if not has_inf_supply and circ_pct < cfg.MOMENTUM_CIRC_SUPPLY_MIN_PCT:
            _s1_supply_blocked += 1
            continue

        fdv = _resolve_fdv(coin, price)
        if fdv <= 0 or mcap <= 0:
            continue
        fdv_ratio = fdv / mcap
        if fdv_ratio > cfg.MOMENTUM_FDV_RATIO_MAX:
            _s1_fdv_blocked += 1
            continue

        matched_tags = [t for t in tags if t in cfg.MOMENTUM_ALLOWED_TAGS]
        if not matched_tags:
            _s1_tags_blocked += 1
            continue

        mexc_symbol = f"{symbol}_USDT"
        if mexc_symbol not in futures_set:
            _s1_mexc_blocked += 1
            continue

        _inf_supply_map[symbol] = has_inf_supply
        if is_micro_cap:
            _micro_cap_set.add(symbol)

        entry_tuple = (
            symbol, name, matched_tags, mexc_symbol,
            price, change_1h, change_24h, mcap, vol_24h, fdv, fdv_ratio, circ_pct,
        )

        if change_1h >= cfg.MOMENTUM_1H_MIN_MOVEMENT:
            log.info(
                f"  M1–M7 ✓  {symbol:<10}  1h {change_1h:+.2f}%  24h {change_24h:+.1f}%  "
                f"MCap ${mcap/1e6:.0f}M  Vol ${vol_24h/1e6:.0f}M  "
                f"FDV/MC {fdv_ratio:.1f}×  [{matched_tags[0]}]"
            )
            candidates.append(entry_tuple)
        else:
            # Stage 0: coin in consolidation (-0.5% ≤ 1H < 0.3%)
            log.debug(
                f"  S0 cand  {symbol:<10}  1h {change_1h:+.2f}%  — pre-breakout watchlist candidate"
            )
            stage0_candidates.append(entry_tuple)

    # Cache CMC data keyed by symbol so Tier 2 can show real name/mcap/circ
    global _cmc_data_cache, _cmc_price_cache
    _cmc_data_cache = {
        sym: (n, tags, mc, vol, fv, fr, cp)
        for sym, n, tags, _, _, _, _, mc, vol, fv, fr, cp in candidates
    }
    _cmc_price_cache = {
        sym: price
        for sym, _, _, _, price, _, _, _, _, _, _, _ in candidates
    }

    # Stage 1 filter breakdown — logged every scan for pipeline analysis
    log.info(
        f"Stage 1 filter stats: "
        f"MCap-block={_s1_mcap_blocked} | Vol-block={_s1_vol_blocked} | "
        f"Supply-block={_s1_supply_blocked} | FDV-block={_s1_fdv_blocked} | "
        f"Tags-block={_s1_tags_blocked} | MEXC-block={_s1_mexc_blocked} | "
        f"MicroCap-allowed={len(_micro_cap_set)}"
    )

    global _last_m1m7_count, _last_macro_blocked, _last_scan_outcomes, _last_rb_watchlist
    _last_scan_outcomes = []
    _last_rb_watchlist  = []
    _last_m1m7_count = len(candidates)
    log.info(f"Stage 1: {len(candidates)} unified candidates (1H ≥ {cfg.MOMENTUM_1H_MIN_MOVEMENT}%).")

    # ── Stage 0: pre-breakout watchlist update ────────────────────────────────
    global _stage0_watchlist
    _now_ts = time.time()

    # Expire old Stage 0 entries
    _expired = [sym for sym, entry in _stage0_watchlist.items()
                if _now_ts - entry["added_ts"] > cfg.MOMENTUM_S0_BREAKOUT_WINDOW * 60]
    for sym in _expired:
        del _stage0_watchlist[sym]
        log.info(f"  S0 expire  {sym}: watchlist entry expired after {cfg.MOMENTUM_S0_BREAKOUT_WINDOW}m")

    # Check new Stage 0 candidates — skip if already in standard pipeline or watchlist
    _s0_added = 0
    for entry in stage0_candidates:
        (symbol, name, matched_tags, mexc_symbol,
         price, change_1h, change_24h, mcap, vol_24h, fdv, fdv_ratio, circ_pct) = entry
        if symbol in _stage0_watchlist:
            continue   # already watching
        s0 = _check_stage0(mexc_symbol, price)
        if s0 is None:
            continue
        _stage0_watchlist[symbol] = {
            "mexc_symbol":        mexc_symbol,
            "consolidation_high": s0["consolidation_high"],
            "ma10_vol":           s0["ma10_vol"],
            "added_ts":           _now_ts,
            "name":               name,
            "mcap":               mcap,
            "price_at_add":       price,
            "range_pct":          s0["range_pct"],
            "rsi5m":              s0["rsi5m"],
        }
        _s0_added += 1
        log.info(
            f"  S0 watch   {symbol}: range {s0['range_pct']:.1f}%  RSI {s0['rsi5m']:.0f}  "
            f"high ${s0['consolidation_high']:.6g}  MA10vol {s0['ma10_vol']:.0f}"
        )

    if _s0_added or _expired:
        log.info(f"Stage 0: +{_s0_added} added, {len(_expired)} expired → {len(_stage0_watchlist)} on watchlist.")

    # ── 5m pre-fetch: cache for all candidates before Stage 2 ─────────────────
    _m5_cache: dict[str, "dict | None"] = {}
    for _cand in candidates:
        _msym = _cand[3]   # mexc_symbol at index 3
        if _msym not in _m5_cache:
            _m5_cache[_msym] = _check_5m(_msym)
    log.info(f"5m pre-fetch: {len(_m5_cache)} symbols, {sum(1 for v in _m5_cache.values() if v is not None)} with data.")

    # Populate active_watch with coins showing 5m momentum (for Tier 2 rescans)
    global _active_watch, _active_watch_ts, _tier1_last_run
    _new_watch = {
        cand[3] for cand in candidates
        if _m5_cache.get(cand[3]) and (
            _m5_cache[cand[3]].get("ema6_gt_ema20") or
            _m5_cache[cand[3]].get("fresh_cross")
        )
    }
    _now_ts = time.time()
    for _sym in _new_watch - _active_watch:   # newly added
        _active_watch_ts[_sym] = _now_ts
    for _sym in list(_active_watch_ts.keys()):  # removed from watch
        if _sym not in _new_watch:
            del _active_watch_ts[_sym]
    _active_watch = _new_watch
    _tier1_last_run = _now_ts
    log.info(f"Tier 1: {len(_active_watch)} symbols added to active_watch.")

    # ── Stages 2 + 3: TA gate + full scoring ─────────────────────────────────
    scored:              list[MomentumResult] = []
    cooling_alerts:      list[MomentumResult] = []
    macro_blocked_count: int = 0
    sq_pending:          list[tuple] = []   # [(entry, tech, sq_score), ...] for Stage 2g
    _passed_info:        dict[str, dict] = {}   # symbol → per-candidate gate status for /passed
    _mc_blocked_scan:    list[dict] = []        # Method C coins scored < 70 for /blocked

    # Stage 2a block counters (CHANGE 2C)
    _s2a_ema_bearish:   int = 0   # EMA6 not > EMA12 > EMA20
    _s2a_sep_small:     int = 0   # EMA order ok but sep < 0.2%
    _s2a_15m_gate:      int = 0   # 15m EMA6 < EMA12
    _s2a_fear_bypassed: int = 0   # allowed via Fear Mode relaxation
    _s2a_squeeze:       int = 0   # bypassed via gate 2g BB-Squeeze

    # Pre-compute fixed trade-level constants (same for every coin)
    _sl_factor  = 1.0 - cfg.MOMENTUM_SL_PCT  / 100.0
    _tp1_factor = 1.0 + cfg.MOMENTUM_TP1_PCT / 100.0
    _tp2_factor = 1.0 + cfg.MOMENTUM_TP2_PCT / 100.0
    _risk_usd   = round(cfg.MOMENTUM_POSITION_USD * cfg.MOMENTUM_SL_PCT  / 100.0, 2)
    _rwd_tp1    = round(cfg.MOMENTUM_POSITION_USD * cfg.MOMENTUM_TP1_PCT / 100.0, 2)
    _rwd_tp2    = round(cfg.MOMENTUM_POSITION_USD * cfg.MOMENTUM_TP2_PCT / 100.0, 2)
    _rr_str     = f"1:{_rwd_tp1 / _risk_usd:.2f}"

    for entry in candidates:
        (symbol, name, matched_tags, mexc_symbol,
         price, change_1h, change_24h, mcap, vol_24h, fdv, fdv_ratio, circ_pct) = entry

        # Per-candidate gate tracking for /passed command
        _pi = {"symbol": symbol, "score": 0, "mcap": mcap, "ath_dist_pct": 0.0,
               "m5_ok": False, "m15_ok": False, "h4_ok": False, "rec": "PENDING", "detail": ""}
        _passed_info[symbol] = _pi

        # Dynamic vol threshold: fast move (>5%) vs slow trend (2-5%)
        vol_threshold = (cfg.MOMENTUM_VOL_FAST_MIN if change_1h > cfg.MOMENTUM_EARLY_1H_MAX
                         else cfg.MOMENTUM_VOL_SLOW_MIN)

        tech = _check_technicals(mexc_symbol, vol_threshold)

        if tech is None:
            log.warning(f"  TA skip  {symbol}: futures kline data unavailable")
            _last_scan_outcomes.append(CandidateOutcome(symbol, change_1h, 0, "NO_DATA", "MEXC kline unavailable", 0.0, 0.0))
            _pi.update({"rec": "NO_DATA", "detail": "MEXC kline unavailable"})
            continue

        squeeze_bypass = False

        # ── Squeeze Breakout detection (4H gate removed — runs unconditionally) ─
        ema_spread_pct = abs(tech.h4_ema_sep)
        ema6_gt_ema20  = tech.h4_ema6 > tech.h4_ema20
        slope_flat     = abs(tech.h4_ema20_slope) < cfg.MOMENTUM_SQUEEZE_EMA_SLOPE_MAX
        vol_breakout   = tech.m15_vol_spike_ratio >= cfg.MOMENTUM_SQUEEZE_VOL_MULT
        px_breakout    = tech.m15_price_gt_h4_ema20
        compressed     = ema_spread_pct < cfg.MOMENTUM_SQUEEZE_EMA_SPREAD_MAX
        if compressed and slope_flat and vol_breakout and px_breakout and ema6_gt_ema20:
            squeeze_bypass = True
            _s2a_squeeze  += 1
            log.info(
                f"  💥 SQUEEZE  {symbol}: spread {ema_spread_pct:.2f}%  "
                f"slope {tech.h4_ema20_slope:+.2f}%  vol {tech.m15_vol_spike_ratio:.1f}×  "
                f"px_above_h4ema20={px_breakout}"
            )
            sq_rsi_ok = cfg.MOMENTUM_SQ_15M_RSI_MIN <= tech.m15_rsi6 <= cfg.MOMENTUM_SQ_15M_RSI_MAX
            sq_1h_ok  = cfg.MOMENTUM_SQ_1H_MIN <= change_1h <= cfg.MOMENTUM_SQ_1H_MAX
            if sq_rsi_ok and sq_1h_ok:
                has_inf_now  = _inf_supply_map.get(symbol, False)
                _sq_ath_dist, _sq_ath_price, _sq_ath_date = _lookup_ath(
                    _ath_map, symbol, price, tech.ath_dist_pct)
                sq_score = (_score_squeeze(tech, mcap, circ_pct, has_inf_now)
                            + _ath_score(_sq_ath_dist))
                if sq_score >= cfg.MOMENTUM_SQ_MIN_SCORE:
                    sq_pending.append((entry, tech, sq_score,
                                       _sq_ath_dist, _sq_ath_price, _sq_ath_date))
                    log.debug(f"  💥 SQ stash  {symbol}: score {sq_score} RSI {tech.m15_rsi6:.1f}")

        # 4H is now a score factor — log status and track for /passed display
        _pi["h4_ok"] = tech.h4_status in ("FULL", "PARTIAL")
        log.info(
            f"  4H [{tech.h4_status}] {symbol}: score {tech.h4_score:+d}  "
            f"1H [{tech.h1_status}]: score {tech.h1_score:+d}"
        )

        # ── GATE 2a: 15m EMA6 > EMA12 (hard gate) ─────────────────────────────
        if not tech.m15_ema6_gt_ema12 and not squeeze_bypass:
            _s2a_15m_gate += 1
            macro_blocked_count += 1
            log.info(f"  15m EMA ✗  {symbol}: EMA6 {tech.m15_ema6:.4g} < EMA12 {tech.m15_ema12:.4g}")
            _last_scan_outcomes.append(CandidateOutcome(
                symbol, change_1h, 0, "MACRO_BLOCKED", "15m EMA6 < EMA12",
                tech.vol_pct, tech.h4_kdj_j))
            _pi.update({"rec": "MACRO_BLOCKED", "detail": "15m EMA6 < EMA12"})
            continue

        _pi["m15_ok"] = True

        # FIX 4: dead-zone blocker — very low volume AND low momentum = skip
        if (tech.vol_pct < cfg.MOMENTUM_DEAD_VOL_PCT * 100 and
                change_1h < cfg.MOMENTUM_DEAD_1H_MAX):
            log.info(
                f"  DEAD ZONE {symbol}: vol {tech.vol_pct:.0f}% + 1H {change_1h:+.1f}% "
                f"— no momentum"
            )
            _dz_detail = f"Vol {tech.vol_pct:.0f}% of MA10 + only {change_1h:.1f}% 1H"
            _last_scan_outcomes.append(CandidateOutcome(symbol, change_1h, 0, "DEAD_ZONE", _dz_detail, tech.vol_pct, tech.h4_kdj_j))
            _pi.update({"rec": "DEAD_ZONE", "detail": _dz_detail})
            continue

        # 5m primary gate — use pre-fetched cache (replaces 1H gate)
        _m5 = _m5_cache.get(mexc_symbol)
        if not _base_5m_gate_ok(_m5):
            log.debug(f"  5m gate ✗  {symbol}: gate failed (EMA/RSI/vol)")
            _last_scan_outcomes.append(CandidateOutcome(symbol, change_1h, 0, "MACRO_BLOCKED", "5m gate: EMA/RSI/vol not aligned", tech.vol_pct, tech.h4_kdj_j))
            _pi.update({"rec": "5M_BLOCKED", "detail": "5m gate: EMA/RSI/vol not aligned"})
            continue

        _pi["m5_ok"] = True

        # Populate tech.m5_* from cache
        _apply_5m_to_tech(tech, _m5)

        # Fundamental bonus
        fund = _score_fundamentals(change_1h, mcap, circ_pct, fdv_ratio)

        # ATH distance — real ATH from CoinGecko (fallback to 16D candle peak)
        real_ath_dist, real_ath_price, real_ath_date = _lookup_ath(
            _ath_map, symbol, price, tech.ath_dist_pct)

        # Hard block: coin within 10% of ATH — risk/reward too poor
        if real_ath_dist < cfg.MOMENTUM_ATH_DIST_HARD_BLOCK:
            log.info(f"  ATH BLOCK  {symbol}: only {real_ath_dist:.1f}% below ATH")
            _last_scan_outcomes.append(CandidateOutcome(symbol, change_1h, 0, "ATH_BLOCK",
                f"Only {real_ath_dist:.1f}% below ATH — hard block", tech.vol_pct, tech.h4_kdj_j))
            _pi.update({"ath_dist_pct": real_ath_dist, "rec": "ATH_BLOCK",
                        "detail": f"Only {real_ath_dist:.1f}% below ATH — hard block"})
            continue

        _pi["ath_dist_pct"] = real_ath_dist
        ath_pts = _ath_score(real_ath_dist)

        # Stage 0 breakout bonus: coin was pre-identified in consolidation watchlist
        _s0_entry = _stage0_watchlist.get(symbol)
        s0_bonus = 0
        if _s0_entry and price > _s0_entry["consolidation_high"]:
            age_min = (_now_ts - _s0_entry["added_ts"]) / 60.0
            if age_min <= cfg.MOMENTUM_S0_BREAKOUT_WINDOW:
                s0_bonus = cfg.MOMENTUM_S0_BREAKOUT_BONUS
                log.info(
                    f"  S0 BREAK   {symbol}: +{s0_bonus}pts  "
                    f"(above ${_s0_entry['consolidation_high']:.6g}, {age_min:.0f}m ago)"
                )

        # Pattern detection (Change 5): EXPLOSION / BREAKOUT / GRIND
        _pat_type, _pat_bonus = _detect_pattern(mexc_symbol, s0_bonus > 0)
        if _pat_type:
            log.info(f"  🔎 PATTERN  {symbol}: {_pat_type} +{_pat_bonus}pts")

        # Total score: 15m tech + fundamentals + ATH bonus + 4H score factor + 1H score + S0 bonus + pattern
        # 4H: +20 fully bullish / +8 partial / -10 bearish / 0 neutral (no hard gate)
        # 1H: +10 bullish / +5 neutral-bullish / 0 weak
        # S0: +10 if coin was on pre-breakout watchlist and just broke above consolidation high
        # Pattern: +5 GRIND / +10-20 BREAKOUT / +15 EXPLOSION
        total = tech.score + fund.total + ath_pts + tech.h4_score + tech.h1_score + s0_bonus + _pat_bonus
        total = max(0, min(100, total))

        # Recommendation classification
        if total >= cfg.MOMENTUM_TOTAL_STRONG_ENTRY:
            recommendation = "STRONG ENTRY"
            rec_emoji      = "🟢"
        elif total >= cfg.MOMENTUM_TOTAL_WATCH:
            recommendation = "WATCH"
            rec_emoji      = "🟡"
        else:
            if total >= cfg.MOMENTUM_TOTAL_MONITOR:
                log.info(f"  MONITOR  {symbol}: {total}/100 — logged only, no alert")
                _last_scan_outcomes.append(CandidateOutcome(symbol, change_1h, total, "MONITOR", f"Score {total}/100 — below alert threshold ({cfg.MOMENTUM_TOTAL_WATCH})", tech.vol_pct, tech.h4_kdj_j))
            else:
                log.debug(f"  SKIP     {symbol}: {total}/100 < {cfg.MOMENTUM_TOTAL_MONITOR}")
                _last_scan_outcomes.append(CandidateOutcome(symbol, change_1h, total, "BELOW_THRESHOLD", f"Score {total}/100 — too low", tech.vol_pct, tech.h4_kdj_j))
            continue

        # 5m soft gate: downgrade STRONG → WATCH if overheated
        if recommendation == "STRONG ENTRY" and tech.m5_note.startswith("⏳"):
            recommendation = "WATCH"
            rec_emoji      = "🟡"

        # SL parameters — standard for all remaining recommendations
        coin_sl_factor = _sl_factor
        coin_risk_usd  = _risk_usd
        coin_rr_str    = _rr_str
        coin_sl_pct    = cfg.MOMENTUM_SL_PCT

        # Risk warnings
        warnings = _generate_warnings(tech, change_1h, circ_pct, fdv_ratio,
                                       _inf_supply_map.get(symbol, False),
                                       is_micro_cap=(symbol in _micro_cap_set),
                                       mcap=mcap)

        _pi.update({"score": total, "rec": recommendation})

        # Leg number + entry validity (Change 4)
        _leg_num, _entry_valid = _get_leg_info(symbol, price, recommendation)
        if not _entry_valid:
            log.info(f"  ⏰ ENTRY EXPIRED  {symbol}: leg {_leg_num} — price moved too far or >90m")

        log.info(
            f"  {rec_emoji} {recommendation}  {symbol}  {total}/100  "
            f"[tech {tech.score}+fund {fund.total}]  "
            f"[ema {tech.m15_ema_pts}+px {tech.m15_price_pts}+"
            f"rsi {tech.m15_rsi6_pts}+kdj {tech.m15_kdj_pts}+"
            f"macd {tech.m15_macd_pts}+vol {tech.vol_pts}]"
        )

        _last_scan_outcomes.append(CandidateOutcome(symbol, change_1h, total, "ALERTED", recommendation, tech.vol_pct, tech.h4_kdj_j))

        scored.append(MomentumResult(
            symbol          = symbol,
            name            = name,
            price           = round(price, 8),
            change_1h       = round(change_1h, 2),
            change_24h      = round(change_24h, 2),
            market_cap      = mcap,
            volume_24h      = vol_24h,
            fdv             = fdv,
            fdv_mcap_ratio  = round(fdv_ratio, 2),
            circ_supply_pct = round(circ_pct, 1),
            matched_tags    = matched_tags,
            mexc_symbol     = mexc_symbol,
            tech            = tech,
            fund            = fund,
            total_score     = total,
            recommendation  = recommendation,
            rec_emoji       = rec_emoji,
            warnings        = warnings,
            ath_pts         = ath_pts,
            entry_price     = round(price * (1 + cfg.MOMENTUM_ENTRY_LIMIT_OFFSET), _price_decimals(price)),
            stop_loss       = round(price * (1 + cfg.MOMENTUM_ENTRY_LIMIT_OFFSET) * (1 - cfg.MOMENTUM_SL_PCT / 100), _price_decimals(price)),
            tp1             = round(price * (1 + cfg.MOMENTUM_ENTRY_LIMIT_OFFSET) * (1 + cfg.MOMENTUM_TP1_PCT / 100), _price_decimals(price)),
            tp2             = round(price * (1 + cfg.MOMENTUM_ENTRY_LIMIT_OFFSET) * (1 + cfg.MOMENTUM_TP2_PCT / 100), _price_decimals(price)),
            risk_usd        = coin_risk_usd,
            reward_tp1_usd  = _rwd_tp1,
            reward_tp2_usd  = _rwd_tp2,
            rr_str          = coin_rr_str,
            sl_pct          = coin_sl_pct,
            infinite_supply = _inf_supply_map.get(symbol, False),
            ath_dist_pct    = real_ath_dist,
            ath_price       = real_ath_price,
            ath_date        = real_ath_date,
            m5_note          = tech.m5_note,
            squeeze_bypass   = squeeze_bypass,
            stage0_breakout  = (s0_bonus > 0),
            leg_number       = _leg_num,
            entry_valid      = _entry_valid,
            pattern_type     = _pat_type,
            pattern_bonus    = _pat_bonus,
        ))

    # Export per-candidate pass data for /passed and /blocked commands
    global _last_passed_candidates, _last_method_c_blocked
    _last_passed_candidates = list(_passed_info.values())
    _last_method_c_blocked  = _mc_blocked_scan

    # Stage 2a block breakdown log (CHANGE 2C)
    log.info(
        f"Stage 2a stats: "
        f"EMA-bearish={_s2a_ema_bearish} | Sep-small={_s2a_sep_small} | "
        f"15m-gate={_s2a_15m_gate} | FearMode-bypass={_s2a_fear_bypassed} | "
        f"Squeeze-bypass={_s2a_squeeze}"
        + (f" | 😟 FEAR MODE ACTIVE (F&G {_fg_value})" if _fear_mode else "")
    )

    # Expose Stage 2a counters for stats_tracker (CHANGE 2C)
    global _last_s2a_ema_bearish, _last_s2a_sep_small, _last_s2a_15m_gate
    global _last_s2a_fear_bypassed, _last_s2a_squeeze
    _last_s2a_ema_bearish   = _s2a_ema_bearish
    _last_s2a_sep_small     = _s2a_sep_small
    _last_s2a_15m_gate      = _s2a_15m_gate
    _last_s2a_fear_bypassed = _s2a_fear_bypassed
    _last_s2a_squeeze       = _s2a_squeeze

    # Sort best-first
    scored.sort(key=lambda r: r.total_score, reverse=True)

    # Cooldown deduplication + cap at max alerts per scan
    new_alerts: list[MomentumResult] = []
    for r in scored:
        if _on_cooldown(r.symbol):
            log.debug(f"  COOLDOWN: {r.symbol}")
            continue
        if _on_global_cooldown(r.symbol, r.recommendation, r.price):
            log.info(f"  GLOBAL COOLDOWN (4H): {r.symbol} — {r.recommendation} blocked")
            continue
        if len(new_alerts) >= cfg.MOMENTUM_MAX_ALERTS_PER_SCAN:
            log.debug(f"  CAP: {r.symbol} (max {cfg.MOMENTUM_MAX_ALERTS_PER_SCAN} per scan)")
            break
        _mark_alerted(r.symbol)
        _mark_global_alerted(r.symbol, r.recommendation, r.price)
        new_alerts.append(r)

    _last_macro_blocked = macro_blocked_count

    # Mark alerted outcomes that were suppressed by cooldown
    _alerted_syms = {r.symbol for r in new_alerts}
    for oc in _last_scan_outcomes:
        if oc.rec == "ALERTED" and oc.symbol not in _alerted_syms:
            oc.rec    = "COOLDOWN"
            oc.detail = f"{oc.detail} — on 2H alert cooldown"

    # ── Stage 2b: Golden Cross pipeline ──────────────────────────────────────
    gc_alerts: list[MomentumResult] = []
    alerted_this_scan = {r.symbol for r in new_alerts}

    for entry in candidates:
        (symbol, name, matched_tags, mexc_symbol,
         price, change_1h, change_24h, mcap, vol_24h, fdv, fdv_ratio, circ_pct) = entry

        if symbol in alerted_this_scan:
            continue  # main pipeline already alerted this coin this scan
        if _on_gc_cooldown(symbol):
            log.debug(f"  GC COOLDOWN: {symbol}")
            continue
        if _on_global_cooldown(symbol, "GOLDEN CROSS", price):
            log.info(f"  GLOBAL COOLDOWN (4H): {symbol} — GC blocked")
            continue

        # Base 5m gate (primary trigger — replaces 1H gate)
        _m5_gc = _m5_cache.get(mexc_symbol)
        if not _base_5m_gate_ok(_m5_gc):
            continue

        tech = _check_technicals(mexc_symbol, cfg.MOMENTUM_VOL_GC_MIN)
        if tech is None:
            continue

        if not tech.m15_golden_cross:
            continue
        # 4H EMA gate removed — 5m gate is primary
        if tech.m15_rsi6 >= cfg.MOMENTUM_GC_RSI_MAX:
            log.debug(f"  GC skip {symbol}: 15m RSI {tech.m15_rsi6:.1f} >= {cfg.MOMENTUM_GC_RSI_MAX}")
            continue

        # Populate 5m fields from cache
        _apply_5m_to_tech(tech, _m5_gc)
        gc_5m_warn = []
        if tech.m5_rsi6 > 0:
            if not (cfg.MOMENTUM_5M_RSI_LOW <= tech.m5_rsi6 <= cfg.MOMENTUM_5M_RSI_HOT):
                gc_5m_warn.append("5m RSI out of range — wait for entry zone or reduce position size")

        log.info(
            f"  ⚡ GOLDEN CROSS  {symbol}  1h {change_1h:+.2f}%  "
            f"RSI {tech.m15_rsi6:.1f}  Vol {tech.vol_pct:.0f}%"
        )
        _mark_gc_alerted(symbol)
        _mark_global_alerted(symbol, "GOLDEN CROSS", price)
        _last_scan_outcomes.append(CandidateOutcome(symbol, change_1h, 0, "GC", "GOLDEN CROSS", tech.vol_pct, tech.h4_kdj_j))

        gc_sl_factor = 1.0 - cfg.MOMENTUM_GC_SL_PCT / 100.0
        gc_risk_usd  = round(cfg.MOMENTUM_POSITION_USD * cfg.MOMENTUM_GC_SL_PCT / 100.0, 2)
        gc_rr_str    = f"1:{_rwd_tp1 / gc_risk_usd:.2f}"
        _gc_ath_dist, _gc_ath_price, _gc_ath_date = _lookup_ath(_ath_map, symbol, price, tech.ath_dist_pct)

        gc_alerts.append(MomentumResult(
            symbol          = symbol,
            name            = name,
            price           = round(price, 8),
            change_1h       = round(change_1h, 2),
            change_24h      = round(change_24h, 2),
            market_cap      = mcap,
            volume_24h      = vol_24h,
            fdv             = fdv,
            fdv_mcap_ratio  = round(fdv_ratio, 2),
            circ_supply_pct = round(circ_pct, 1),
            matched_tags    = matched_tags,
            mexc_symbol     = mexc_symbol,
            tech            = tech,
            recommendation  = "GOLDEN CROSS",
            rec_emoji       = "⚡",
            warnings        = gc_5m_warn,
            m5_note         = tech.m5_note,
            entry_price     = round(price * (1 + cfg.MOMENTUM_ENTRY_LIMIT_OFFSET), _price_decimals(price)),
            stop_loss       = round(price * (1 + cfg.MOMENTUM_ENTRY_LIMIT_OFFSET) * (1 - cfg.MOMENTUM_GC_SL_PCT / 100), _price_decimals(price)),
            tp1             = round(price * (1 + cfg.MOMENTUM_ENTRY_LIMIT_OFFSET) * (1 + cfg.MOMENTUM_TP1_PCT / 100), _price_decimals(price)),
            tp2             = round(price * (1 + cfg.MOMENTUM_ENTRY_LIMIT_OFFSET) * (1 + cfg.MOMENTUM_TP2_PCT / 100), _price_decimals(price)),
            risk_usd        = gc_risk_usd,
            reward_tp1_usd  = _rwd_tp1,
            reward_tp2_usd  = _rwd_tp2,
            rr_str          = gc_rr_str,
            sl_pct          = cfg.MOMENTUM_GC_SL_PCT,
            ath_dist_pct    = _gc_ath_dist,
            ath_price       = _gc_ath_price,
            ath_date        = _gc_ath_date,
        ))

    # ── Stage 2c: Volume Spike pipeline ──────────────────────────────────────
    vs_alerts: list[MomentumResult] = []
    alerted_symbols = {r.symbol for r in new_alerts + gc_alerts}

    for entry in candidates:
        (symbol, name, matched_tags, mexc_symbol,
         price, change_1h, change_24h, mcap, vol_24h, fdv, fdv_ratio, circ_pct) = entry

        if symbol in alerted_symbols:
            continue
        if _on_vs_cooldown(symbol):
            log.debug(f"  VS COOLDOWN: {symbol}")
            continue
        if _on_global_cooldown(symbol, "VOLUME SPIKE", price):
            log.info(f"  GLOBAL COOLDOWN (4H): {symbol} — VS blocked")
            continue

        # Base 5m gate (primary trigger)
        _m5_vs = _m5_cache.get(mexc_symbol)
        if not _base_5m_gate_ok(_m5_vs):
            continue

        tech = _check_technicals(mexc_symbol, cfg.MOMENTUM_VOL_GC_MIN)
        if tech is None:
            continue
        # P&D pre-filter: near ATL → require doubled vol + mandatory warning
        _vs_atl      = _lookup_atl(_ath_map, symbol)
        _vs_near_atl = (_vs_atl > 0 and price <= _vs_atl * (1 + cfg.MOMENTUM_PD_ATL_NEAR_PCT / 100.0))
        _vs_pd_active = _vs_near_atl  # no 4H EMA requirement in new arch
        _vs_pd_vol_mult = (cfg.MOMENTUM_FEAR_PD_VOL_MULT if _fear_mode else cfg.MOMENTUM_PD_VOL_MULT)

        if not tech.m15_vol_spike:
            log.debug(f"  VS skip {symbol}: vol ratio {tech.m15_vol_spike_ratio:.1f}× < {cfg.MOMENTUM_VS_VOL_MULT}×")
            continue
        if _vs_pd_active and tech.m15_vol_spike_ratio < _vs_pd_vol_mult:
            log.info(f"  VS skip {symbol}: near ATL, vol {tech.m15_vol_spike_ratio:.1f}× < {_vs_pd_vol_mult}× P&D threshold")
            continue
        # 4H EMA gate removed — 5m gate is primary
        if tech.m15_rsi6 >= cfg.MOMENTUM_VS_RSI_MAX:
            log.debug(f"  VS skip {symbol}: 15m RSI {tech.m15_rsi6:.1f} ≥ {cfg.MOMENTUM_VS_RSI_MAX}")
            continue

        # Populate 5m fields from cache
        _apply_5m_to_tech(tech, _m5_vs)
        vs_5m_warn = []
        if _vs_near_atl:
            vs_5m_warn.append("Near ATL in downtrend — pump-and-dump risk. Use 30% of normal margin only.")

        log.info(
            f"  ⚡ VOL SPIKE  {symbol}  1h {change_1h:+.2f}%  "
            f"vol {tech.m15_vol_spike_ratio:.1f}×  RSI {tech.m15_rsi6:.1f}"
        )
        _mark_vs_alerted(symbol)
        _mark_global_alerted(symbol, "VOLUME SPIKE", price)
        _last_scan_outcomes.append(CandidateOutcome(symbol, change_1h, 0, "VS", "VOLUME SPIKE", tech.vol_pct, tech.h4_kdj_j))

        vs_sl_factor = 1.0 - cfg.MOMENTUM_VS_SL_PCT / 100.0
        vs_risk_usd  = round(cfg.MOMENTUM_POSITION_USD * cfg.MOMENTUM_VS_SL_PCT / 100.0, 2)
        _vs_ath_dist, _vs_ath_price, _vs_ath_date = _lookup_ath(_ath_map, symbol, price, tech.ath_dist_pct)

        vs_alerts.append(MomentumResult(
            symbol          = symbol,
            name            = name,
            price           = round(price, 8),
            change_1h       = round(change_1h, 2),
            change_24h      = round(change_24h, 2),
            market_cap      = mcap,
            volume_24h      = vol_24h,
            fdv             = fdv,
            fdv_mcap_ratio  = round(fdv_ratio, 2),
            circ_supply_pct = round(circ_pct, 1),
            matched_tags    = matched_tags,
            mexc_symbol     = mexc_symbol,
            tech            = tech,
            recommendation  = "VOLUME SPIKE",
            rec_emoji       = "⚡",
            warnings        = vs_5m_warn,
            m5_note         = tech.m5_note,
            entry_price     = round(price * (1 + cfg.MOMENTUM_ENTRY_LIMIT_OFFSET), _price_decimals(price)),
            stop_loss       = round(price * (1 + cfg.MOMENTUM_ENTRY_LIMIT_OFFSET) * (1 - cfg.MOMENTUM_VS_SL_PCT / 100), _price_decimals(price)),
            tp1             = round(price * (1 + cfg.MOMENTUM_ENTRY_LIMIT_OFFSET) * (1 + cfg.MOMENTUM_TP1_PCT / 100), _price_decimals(price)),
            tp2             = round(price * (1 + cfg.MOMENTUM_ENTRY_LIMIT_OFFSET) * (1 + cfg.MOMENTUM_TP2_PCT / 100), _price_decimals(price)),
            risk_usd        = vs_risk_usd,
            reward_tp1_usd  = _rwd_tp1,
            reward_tp2_usd  = _rwd_tp2,
            rr_str          = f"1:{_rwd_tp1 / vs_risk_usd:.2f}",
            sl_pct          = cfg.MOMENTUM_VS_SL_PCT,
            ath_dist_pct    = _vs_ath_dist,
            ath_price       = _vs_ath_price,
            ath_date        = _vs_ath_date,
        ))

    # ── Stage 2d: Recovery Bounce pipeline ───────────────────────────────────
    rb_alerts: list[MomentumResult] = []
    alerted_symbols = {r.symbol for r in new_alerts + gc_alerts + vs_alerts}

    for entry in candidates:
        (symbol, name, matched_tags, mexc_symbol,
         price, change_1h, change_24h, mcap, vol_24h, fdv, fdv_ratio, circ_pct) = entry

        if symbol in alerted_symbols:
            continue
        # RB requires recent 24H decline (moved from Stage 1 into pipeline)
        if change_24h >= -3.0:
            continue
        if _on_rb_cooldown(symbol):
            log.debug(f"  RB COOLDOWN: {symbol}")
            continue
        if _on_global_cooldown(symbol, "RECOVERY", price):
            log.info(f"  GLOBAL COOLDOWN (4H): {symbol} — RB blocked")
            continue

        # Base 5m gate (primary trigger)
        _m5_rb = _m5_cache.get(mexc_symbol)
        if not _base_5m_gate_ok(_m5_rb):
            continue

        tech = _check_technicals(mexc_symbol, cfg.MOMENTUM_VOL_SLOW_MIN)
        if tech is None:
            continue

        current       = tech.m15_price
        h24_high      = tech.h24_high
        h24_low       = tech.h24_low
        peak_pct      = cfg.MOMENTUM_RB_PEAK_PCT / 100.0
        pullback_pct  = cfg.MOMENTUM_RB_PULLBACK_PCT / 100.0

        peak_above_current = h24_high >= current * (1.0 + peak_pct)
        peak_above_low     = h24_high >= h24_low  * (1.0 + peak_pct)
        real_pullback      = (h24_high - current) / h24_high >= pullback_pct if h24_high > 0 else False

        if not (peak_above_current or peak_above_low):
            log.debug(f"  RB skip {symbol}: h24_high {h24_high:.4g} not 12%+ above current {current:.4g}")
            continue

        # Track near-miss for /recovery command
        _last_rb_watchlist.append(RBWatchItem(
            symbol       = symbol,
            change_1h    = round(change_1h, 2),
            current      = round(current, 8),
            h24_high     = round(h24_high, 8),
            pullback_pct = round((h24_high - current) / h24_high * 100, 1) if h24_high > 0 else 0.0,
            h4_ema_ok    = tech.h4_ema_ok,
            h4_kdj_j     = tech.h4_kdj_j,
        ))

        if not real_pullback:
            log.debug(f"  RB skip {symbol}: pullback only {(h24_high - current)/h24_high*100:.1f}% < 8%")
            continue
        # 4H EMA gate removed — 5m gate is primary; keep KDJ check (spec: 4H KDJ < 110)
        if tech.h4_kdj_j >= cfg.MOMENTUM_RB_KDJ_MAX:
            log.debug(f"  RB skip {symbol}: 4H KDJ {tech.h4_kdj_j:.1f} ≥ {cfg.MOMENTUM_RB_KDJ_MAX} (not cooled)")
            continue

        # Populate 5m fields + 15m EMA diagnostic
        _apply_5m_to_tech(tech, _m5_rb)
        rb_5m_warn = []
        if not tech.m15_ema6_gt_ema12:
            rb_5m_warn.append("15m EMA6 < EMA12 — momentum not confirmed")
        if tech.m5_rsi6 > 0 and not tech.m5_first_green:
            rb_5m_warn.append("5m first green candle not confirmed — wait for entry zone")

        pullback_shown = (h24_high - current) / h24_high * 100
        log.info(
            f"  ♻️ RECOVERY  {symbol}  1h {change_1h:+.2f}%  "
            f"peak {h24_high:.4g}  pullback {pullback_shown:.1f}%  KDJ {tech.h4_kdj_j:.1f}"
        )
        _mark_rb_alerted(symbol)
        _mark_global_alerted(symbol, "RECOVERY", price)
        _last_scan_outcomes.append(CandidateOutcome(symbol, change_1h, 0, "RB", "RECOVERY", tech.vol_pct, tech.h4_kdj_j))

        rb_sl_factor  = 1.0 - cfg.MOMENTUM_RB_SL_PCT / 100.0
        rb_tp1_factor = 1.0 + cfg.MOMENTUM_RB_TP1_PCT / 100.0
        rb_tp2_factor = 1.0 + cfg.MOMENTUM_RB_TP2_PCT / 100.0
        rb_risk_usd   = round(cfg.MOMENTUM_POSITION_USD * cfg.MOMENTUM_RB_SL_PCT / 100.0, 2)
        rb_rwd_tp1    = round(cfg.MOMENTUM_POSITION_USD * cfg.MOMENTUM_RB_TP1_PCT / 100.0, 2)
        rb_rwd_tp2    = round(cfg.MOMENTUM_POSITION_USD * cfg.MOMENTUM_RB_TP2_PCT / 100.0, 2)
        _rb_ath_dist, _rb_ath_price, _rb_ath_date = _lookup_ath(_ath_map, symbol, price, tech.ath_dist_pct)

        rb_alerts.append(MomentumResult(
            symbol          = symbol,
            name            = name,
            price           = round(price, 8),
            change_1h       = round(change_1h, 2),
            change_24h      = round(change_24h, 2),
            market_cap      = mcap,
            volume_24h      = vol_24h,
            fdv             = fdv,
            fdv_mcap_ratio  = round(fdv_ratio, 2),
            circ_supply_pct = round(circ_pct, 1),
            matched_tags    = matched_tags,
            mexc_symbol     = mexc_symbol,
            tech            = tech,
            recommendation  = "RECOVERY",
            rec_emoji       = "♻️",
            warnings        = rb_5m_warn,
            m5_note         = tech.m5_note,
            entry_price     = round(price * (1 + cfg.MOMENTUM_ENTRY_LIMIT_OFFSET), _price_decimals(price)),
            stop_loss       = round(price * (1 + cfg.MOMENTUM_ENTRY_LIMIT_OFFSET) * (1 - cfg.MOMENTUM_RB_SL_PCT / 100), _price_decimals(price)),
            tp1             = round(price * (1 + cfg.MOMENTUM_ENTRY_LIMIT_OFFSET) * (1 + cfg.MOMENTUM_RB_TP1_PCT / 100), _price_decimals(price)),
            tp2             = round(price * (1 + cfg.MOMENTUM_ENTRY_LIMIT_OFFSET) * (1 + cfg.MOMENTUM_RB_TP2_PCT / 100), _price_decimals(price)),
            risk_usd        = rb_risk_usd,
            reward_tp1_usd  = rb_rwd_tp1,
            reward_tp2_usd  = rb_rwd_tp2,
            rr_str          = f"1:{rb_rwd_tp1 / rb_risk_usd:.2f}",
            sl_pct          = cfg.MOMENTUM_RB_SL_PCT,
            ath_dist_pct    = _rb_ath_dist,
            ath_price       = _rb_ath_price,
            ath_date        = _rb_ath_date,
        ))

    # ── Stage 2e: Pre-Breakout Watch pipeline ────────────────────────────────
    pbw_alerts: list[MomentumResult] = []
    alerted_symbols = {r.symbol for r in new_alerts + cooling_alerts + gc_alerts + vs_alerts + rb_alerts}

    for entry in candidates:
        (symbol, name, matched_tags, mexc_symbol,
         price, change_1h, change_24h, mcap, vol_24h, fdv, fdv_ratio, circ_pct) = entry

        if symbol in alerted_symbols:
            continue
        if _on_pbw_cooldown(symbol):
            continue
        if _on_global_cooldown(symbol, "PRE-BREAKOUT", price):
            log.info(f"  GLOBAL COOLDOWN (4H): {symbol} — PBW blocked")
            continue

        # 4H EMA stack check — no separation requirement (bypass sep check)
        df_4h = get_mexc_futures_klines(mexc_symbol, "4h", limit=40)
        if df_4h is None or len(df_4h) < 30:
            continue
        close_4h = df_4h["close"]
        h4_ema6  = float(compute_ema(close_4h,  6).iloc[-1])
        h4_ema12 = float(compute_ema(close_4h, 12).iloc[-1])
        h4_ema20 = float(compute_ema(close_4h, 20).iloc[-1])
        if not (h4_ema6 > h4_ema12 > h4_ema20):
            log.debug(f"  PBW skip {symbol}: 4H EMA not bullish")
            continue

        # ATH distance — real ATH from CoinGecko, fallback to 16D candle high
        h16d_high        = float(df_4h["high"].max())
        _h16d_dist       = (h16d_high - price) / h16d_high * 100 if h16d_high > 0 else 0.0
        ath_dist_pct, _pbw_ath_price, _pbw_ath_date = _lookup_ath(_ath_map, symbol, price, _h16d_dist)

        pbw = _check_5m_pbw(mexc_symbol, fear_mode=_fear_mode)
        if pbw is None:
            log.info(f"  PBW diag {symbol}: 5m data unavailable")
            continue
        log.info(
            f"  PBW diag {symbol} — "
            f"RSI consec count: {pbw['rsi_streak']}/{cfg.MOMENTUM_PBW_RSI_CANDLES} | "
            f"EMA spread: {pbw['ema_spread']:.3f}% (need <{cfg.MOMENTUM_PBW_EMA_SPREAD_MAX}%) | "
            f"vol trigger: {pbw['vol_ratio']:.2f}× (need >{cfg.MOMENTUM_PBW_VOL_MULT}×) | "
            f"RSI trigger: {pbw['latest_rsi']:.1f} (need <{cfg.MOMENTUM_PBW_TRIGGER_RSI_MAX}) | "
            f"px_above_ema: {pbw['trigger_px']}"
        )
        if not pbw["compressed"]:
            continue
        if not pbw["rsi_ok"]:
            continue
        if not pbw["trigger_rsi"]:
            continue
        if not (pbw["trigger_vol"] and pbw["trigger_px"]):
            continue

        fund     = _score_fundamentals(change_1h, mcap, circ_pct, fdv_ratio)
        if fund.total < cfg.MOMENTUM_PBW_MIN_SCORE:
            log.debug(f"  PBW skip {symbol}: score {fund.total} < {cfg.MOMENTUM_PBW_MIN_SCORE}")
            continue
        has_inf  = _inf_supply_map.get(symbol, False)
        warnings = []
        if symbol in _micro_cap_set:
            warnings.append(f"Micro-Cap: ${mcap/1e6:.0f}M — half position size")
            warnings.append("Treat as high-risk, max 50% normal margin")
        if has_inf:
            warnings.append("Max Supply unknown — inflation risk")
        if circ_pct > 0 and circ_pct < cfg.MOMENTUM_WARN_CIRC_ALERT_PCT:
            warnings.append(f"Circ Rate: {circ_pct:.0f}% — Dilution risk")
        if fdv_ratio > cfg.MOMENTUM_WARN_FDV_ALERT_RATIO:
            warnings.append(f"FDV/MCap: {fdv_ratio:.1f}×")

        # 5m soft gate for PBW
        m5_pbw = _check_5m(mexc_symbol)
        pbw_m5_note = ""
        if m5_pbw is not None:
            if m5_pbw["kdj_j"] >= cfg.MOMENTUM_5M_KDJ_MAX_PBW or m5_pbw["rsi6"] >= cfg.MOMENTUM_5M_RSI_MAX_PBW:
                pbw_m5_note = "5m not ideal — wait for entry zone or reduce position size"

        log.info(
            f"  🔍 PRE-BREAKOUT  {symbol}  1h {change_1h:+.2f}%  "
            f"EMA {pbw['ema_spread']:.3f}%  RSI streak {pbw['rsi_streak']}  "
            f"Vol {pbw['vol_ratio']:.1f}×"
        )
        _mark_pbw_alerted(symbol)
        _mark_global_alerted(symbol, "PRE-BREAKOUT", price)
        _last_scan_outcomes.append(CandidateOutcome(symbol, change_1h, fund.total, "PBW", "PRE-BREAKOUT", 0.0, 0.0))

        sl_f   = 1.0 - cfg.MOMENTUM_PBW_SL_PCT  / 100.0
        tp1_f  = 1.0 + cfg.MOMENTUM_PBW_TP1_PCT / 100.0
        tp2_f  = 1.0 + cfg.MOMENTUM_PBW_TP2_PCT / 100.0
        risk   = round(cfg.MOMENTUM_POSITION_USD * cfg.MOMENTUM_PBW_SL_PCT  / 100.0, 2)
        rwd1   = round(cfg.MOMENTUM_POSITION_USD * cfg.MOMENTUM_PBW_TP1_PCT / 100.0, 2)
        rwd2   = round(cfg.MOMENTUM_POSITION_USD * cfg.MOMENTUM_PBW_TP2_PCT / 100.0, 2)

        pbw_alerts.append(MomentumResult(
            symbol=symbol, name=name, price=round(price, 8),
            change_1h=round(change_1h, 2), change_24h=round(change_24h, 2),
            market_cap=mcap, volume_24h=vol_24h, fdv=fdv,
            fdv_mcap_ratio=round(fdv_ratio, 2), circ_supply_pct=round(circ_pct, 1),
            matched_tags=matched_tags, mexc_symbol=mexc_symbol,
            fund=fund, total_score=fund.total,
            recommendation="PRE-BREAKOUT", rec_emoji="🔍",
            warnings=warnings, ath_pts=0,
            m5_note=pbw_m5_note,
            entry_price=round(price * (1 + cfg.MOMENTUM_ENTRY_LIMIT_OFFSET), _price_decimals(price)),
            stop_loss=round(price * (1 + cfg.MOMENTUM_ENTRY_LIMIT_OFFSET) * (1 - cfg.MOMENTUM_PBW_SL_PCT / 100), _price_decimals(price)),
            tp1=round(price * (1 + cfg.MOMENTUM_ENTRY_LIMIT_OFFSET) * (1 + cfg.MOMENTUM_PBW_TP1_PCT / 100), _price_decimals(price)),
            tp2=round(price * (1 + cfg.MOMENTUM_ENTRY_LIMIT_OFFSET) * (1 + cfg.MOMENTUM_PBW_TP2_PCT / 100), _price_decimals(price)),
            risk_usd=risk, reward_tp1_usd=rwd1, reward_tp2_usd=rwd2,
            rr_str=f"1:{rwd1/risk:.2f}", sl_pct=cfg.MOMENTUM_PBW_SL_PCT,
            infinite_supply=has_inf,
            m1_ema_spread=pbw["ema_spread"],
            m1_rsi_streak=pbw["rsi_streak"],
            m1_vol_ratio=pbw["vol_ratio"],
            ath_dist_pct=round(ath_dist_pct, 1),
            ath_price=_pbw_ath_price,
            ath_date=_pbw_ath_date,
        ))

    # ── Stage 2f: Staircase Continuation pipeline ─────────────────────────────
    sc_alerts: list[MomentumResult] = []
    alerted_symbols = {r.symbol for r in new_alerts + cooling_alerts + gc_alerts + vs_alerts + rb_alerts + pbw_alerts}

    for entry in candidates:
        (symbol, name, matched_tags, mexc_symbol,
         price, change_1h, change_24h, mcap, vol_24h, fdv, fdv_ratio, circ_pct) = entry

        if symbol in alerted_symbols:
            continue
        # SC requires positive 24H trend (moved from Stage 1 into pipeline)
        if change_24h <= 3.0:
            continue
        if _on_sc_cooldown(symbol):
            continue
        if _on_global_cooldown(symbol, "STAIRCASE", price):
            log.info(f"  GLOBAL COOLDOWN (4H): {symbol} — SC blocked")
            continue

        # Base 5m gate — fresh 5m cross signals the new leg starting
        _m5_sc = _m5_cache.get(mexc_symbol)
        if not _base_5m_gate_ok(_m5_sc):
            continue

        tech = _check_technicals(mexc_symbol, cfg.MOMENTUM_VOL_SLOW_MIN)
        if tech is None:
            continue
        # SC requires 4H EMA bullish OR transitioning (per spec)
        h4_transition_ok_sc = (tech.h4_ema6 > tech.h4_ema12 and
                               tech.h4_ema6 > tech.h4_ema20 and change_24h > 0)
        if not (tech.h4_ema_ok or h4_transition_ok_sc):
            log.debug(f"  SC skip {symbol}: 4H EMA bearish, no transition")
            continue

        # Consolidation checks — 5m replaces 15m vol filter
        current        = tech.m15_price
        prior_move_pct = (tech.h24_high - current) / current * 100 if current > 0 else 0.0
        sc_rsi_limit   = cfg.MOMENTUM_SC_RSI_MAX_FEAR if _fear_mode else cfg.MOMENTUM_SC_RSI_MAX

        # 5m background vol consolidation check (replaces 15m vol < 35%)
        _m5_sc_vol_recent = _m5_sc.get("vol_recent_pct", 100.0) if _m5_sc else 100.0
        _sc_vol_consol_limit = cfg.MOMENTUM_SC_5M_VOL_MAX_CONSOL * 100

        log.info(
            f"  SC diag {symbol} — "
            f"5m-vol-prior: {_m5_sc_vol_recent:.0f}% (need <{_sc_vol_consol_limit:.0f}%) | "
            f"15m RSI6: {tech.m15_rsi6:.1f} (need <{sc_rsi_limit}) | "
            f"KDJ J: {tech.m15_kdj_j:.1f} (need <{cfg.MOMENTUM_SC_KDJ_MAX}, strict<{cfg.MOMENTUM_SC_KDJ_STRICT}) | "
            f"prior move: {prior_move_pct:.1f}% (need ≥{cfg.MOMENTUM_SC_PRIOR_MOVE_MIN}%) | "
            f"24H: {change_24h:+.1f}%"
        )

        # 5m background vol must be low (consolidation); current candle was the trigger (handled by base gate)
        if _m5_sc_vol_recent >= _sc_vol_consol_limit:
            log.debug(f"  SC skip {symbol}: 5m background vol {_m5_sc_vol_recent:.0f}% >= {_sc_vol_consol_limit:.0f}% (not consolidating)")
            continue
        if tech.m15_rsi6 >= sc_rsi_limit:
            continue
        if tech.m15_kdj_j >= cfg.MOMENTUM_SC_KDJ_MAX:
            continue

        # EITHER gate: RSI < strict threshold OR KDJ J < strict threshold
        either_ok = (tech.m15_rsi6 < cfg.MOMENTUM_SC_RSI_STRICT) or (tech.m15_kdj_j < cfg.MOMENTUM_SC_KDJ_STRICT)
        if not either_ok:
            log.debug(
                f"  SC skip {symbol}: neither RSI {tech.m15_rsi6:.1f}<{cfg.MOMENTUM_SC_RSI_STRICT}"
                f" nor KDJ {tech.m15_kdj_j:.1f}<{cfg.MOMENTUM_SC_KDJ_STRICT}"
            )
            continue

        # Prior move: 24H high must be ≥4% above current price (confirms previous leg)
        prior_move_ok = tech.h24_high >= current * (1.0 + cfg.MOMENTUM_SC_PRIOR_MOVE_MIN / 100.0)
        if not prior_move_ok:
            log.debug(f"  SC skip {symbol}: 24H high only {prior_move_pct:.1f}% above current (need {cfg.MOMENTUM_SC_PRIOR_MOVE_MIN}%)")
            continue

        fund    = _score_fundamentals(change_1h, mcap, circ_pct, fdv_ratio)
        if fund.total < cfg.MOMENTUM_SC_MIN_SCORE:
            log.debug(f"  SC skip {symbol}: score {fund.total} < {cfg.MOMENTUM_SC_MIN_SCORE}")
            continue
        has_inf = _inf_supply_map.get(symbol, False)
        warnings = []
        if symbol in _micro_cap_set:
            warnings.append(f"Micro-Cap: ${mcap/1e6:.0f}M — half position size")
            warnings.append("Treat as high-risk, max 50% normal margin")
        if has_inf:
            warnings.append("Max Supply unknown — inflation risk")
        if circ_pct > 0 and circ_pct < cfg.MOMENTUM_WARN_CIRC_ALERT_PCT:
            warnings.append(f"Circ Rate: {circ_pct:.0f}% — Dilution risk")
        if fdv_ratio > cfg.MOMENTUM_WARN_FDV_ALERT_RATIO:
            warnings.append(f"FDV/MCap: {fdv_ratio:.1f}×")

        # Populate 5m fields from cache
        _apply_5m_to_tech(tech, _m5_sc)
        if tech.m5_kdj_j >= cfg.MOMENTUM_5M_KDJ_MAX_SC:
            warnings.append("5m KDJ J overheated — wait for entry zone")

        log.info(
            f"  🪜 STAIRCASE  {symbol}  1h {change_1h:+.2f}%  "
            f"Vol {tech.vol_pct:.0f}%  RSI {tech.m15_rsi6:.1f}  KDJ {tech.m15_kdj_j:.1f}  "
            f"prior +{prior_move_pct:.1f}%"
            + (" [fear-mode]" if _fear_mode else "")
        )
        _mark_sc_alerted(symbol)
        _mark_global_alerted(symbol, "STAIRCASE", price)
        _last_scan_outcomes.append(CandidateOutcome(symbol, change_1h, fund.total, "SC", "STAIRCASE", tech.vol_pct, tech.h4_kdj_j))

        sl_f   = 1.0 - cfg.MOMENTUM_SC_SL_PCT  / 100.0
        tp1_f  = 1.0 + cfg.MOMENTUM_SC_TP1_PCT / 100.0
        tp2_f  = 1.0 + cfg.MOMENTUM_SC_TP2_PCT / 100.0
        risk   = round(cfg.MOMENTUM_POSITION_USD * cfg.MOMENTUM_SC_SL_PCT  / 100.0, 2)
        rwd1   = round(cfg.MOMENTUM_POSITION_USD * cfg.MOMENTUM_SC_TP1_PCT / 100.0, 2)
        rwd2   = round(cfg.MOMENTUM_POSITION_USD * cfg.MOMENTUM_SC_TP2_PCT / 100.0, 2)
        _sc_ath_dist, _sc_ath_price, _sc_ath_date = _lookup_ath(_ath_map, symbol, price, tech.ath_dist_pct)

        sc_alerts.append(MomentumResult(
            symbol=symbol, name=name, price=round(price, 8),
            change_1h=round(change_1h, 2), change_24h=round(change_24h, 2),
            market_cap=mcap, volume_24h=vol_24h, fdv=fdv,
            fdv_mcap_ratio=round(fdv_ratio, 2), circ_supply_pct=round(circ_pct, 1),
            matched_tags=matched_tags, mexc_symbol=mexc_symbol,
            tech=tech, fund=fund, total_score=fund.total,
            recommendation="STAIRCASE", rec_emoji="🪜",
            warnings=warnings, ath_pts=0,
            m5_note=tech.m5_note,
            entry_price=round(price * (1 + cfg.MOMENTUM_ENTRY_LIMIT_OFFSET), _price_decimals(price)),
            stop_loss=round(price * (1 + cfg.MOMENTUM_ENTRY_LIMIT_OFFSET) * (1 - cfg.MOMENTUM_SC_SL_PCT / 100), _price_decimals(price)),
            tp1=round(price * (1 + cfg.MOMENTUM_ENTRY_LIMIT_OFFSET) * (1 + cfg.MOMENTUM_SC_TP1_PCT / 100), _price_decimals(price)),
            tp2=round(price * (1 + cfg.MOMENTUM_ENTRY_LIMIT_OFFSET) * (1 + cfg.MOMENTUM_SC_TP2_PCT / 100), _price_decimals(price)),
            risk_usd=risk, reward_tp1_usd=rwd1, reward_tp2_usd=rwd2,
            rr_str=f"1:{rwd1/risk:.2f}", sl_pct=cfg.MOMENTUM_SC_SL_PCT,
            infinite_supply=has_inf,
            sc_prior_move=round(prior_move_pct, 1),
            ath_dist_pct=_sc_ath_dist,
            ath_price=_sc_ath_price,
            ath_date=_sc_ath_date,
        ))

    # ── Stage 2g: BB-Squeeze Breakout pipeline (PART 3) ──────────────────────
    sq_alerts: list[MomentumResult] = []
    alerted_symbols = {r.symbol for r in new_alerts + cooling_alerts + gc_alerts + vs_alerts + rb_alerts + pbw_alerts + sc_alerts}
    sq_risk_usd  = round(cfg.MOMENTUM_POSITION_USD * cfg.MOMENTUM_SQ_SL_PCT  / 100.0, 2)
    sq_rwd_tp1   = round(cfg.MOMENTUM_POSITION_USD * cfg.MOMENTUM_SQ_TP1_PCT / 100.0, 2)
    sq_rwd_tp2   = round(cfg.MOMENTUM_POSITION_USD * cfg.MOMENTUM_SQ_TP2_PCT / 100.0, 2)

    for (sq_entry, sq_tech, sq_score,
         _sq_ath_dist, _sq_ath_price, _sq_ath_date) in sq_pending:
        (symbol, name, matched_tags, mexc_symbol,
         price, change_1h, change_24h, mcap, vol_24h, fdv, fdv_ratio, circ_pct) = sq_entry

        if symbol in alerted_symbols:
            continue
        if _on_sq_cooldown(symbol):
            log.debug(f"  SQ COOLDOWN: {symbol}")
            continue
        if _on_global_cooldown(symbol, "SQUEEZE", price):
            log.info(f"  GLOBAL COOLDOWN (4H): {symbol} — SQ blocked (exception not met)")
            continue

        # Base 5m gate for SQ (use cache, fall back to fresh fetch if not pre-fetched)
        _m5_sq = _m5_cache.get(mexc_symbol) or _check_5m(mexc_symbol)
        if not _base_5m_gate_ok(_m5_sq):
            log.debug(f"  SQ skip {symbol}: 5m gate not met")
            continue
        _apply_5m_to_tech(sq_tech, _m5_sq)

        has_inf = _inf_supply_map.get(symbol, False)
        sq_warnings: list[str] = []
        if sq_tech.m5_kdj_j > 0 and sq_tech.m5_kdj_j < 50:
            sq_warnings.append(
                f"5m KDJ J {sq_tech.m5_kdj_j:.0f} < 50 — momentum building, "
                "wait for 5m confirmation before entry"
            )
        if symbol in _micro_cap_set:
            sq_warnings.append(f"Micro-Cap: ${mcap/1e6:.0f}M — half position size")
            sq_warnings.append("Treat as high-risk, max 50% normal margin")
        if has_inf:
            sq_warnings.append("Max Supply unknown — inflation risk")
        if circ_pct > 0 and circ_pct < cfg.MOMENTUM_WARN_CIRC_ALERT_PCT:
            sq_warnings.append(f"Circ Rate: {circ_pct:.0f}% — Dilution risk")
        if fdv_ratio > cfg.MOMENTUM_WARN_FDV_ALERT_RATIO:
            sq_warnings.append(f"FDV/MCap: {fdv_ratio:.1f}×")

        log.info(
            f"  💥 SQUEEZE BREAKOUT  {symbol}  1h {change_1h:+.2f}%  "
            f"vol {sq_tech.m15_vol_spike_ratio:.1f}×  RSI {sq_tech.m15_rsi6:.1f}  "
            f"compress {sq_tech.h4_compression_days}d  score {sq_score}"
        )
        _mark_sq_alerted(symbol)
        _mark_global_alerted(symbol, "SQUEEZE", price)
        alerted_symbols.add(symbol)
        _last_scan_outcomes.append(CandidateOutcome(
            symbol, change_1h, sq_score, "SQ", "SQUEEZE BREAKOUT",
            sq_tech.vol_pct, sq_tech.h4_kdj_j))

        sq_alerts.append(MomentumResult(
            symbol=symbol, name=name, price=round(price, 8),
            change_1h=round(change_1h, 2), change_24h=round(change_24h, 2),
            market_cap=mcap, volume_24h=vol_24h, fdv=fdv,
            fdv_mcap_ratio=round(fdv_ratio, 2), circ_supply_pct=round(circ_pct, 1),
            matched_tags=matched_tags, mexc_symbol=mexc_symbol,
            tech=sq_tech, total_score=sq_score,
            recommendation="SQUEEZE", rec_emoji="💥",
            warnings=sq_warnings, m5_note=sq_tech.m5_note,
            entry_price=round(price * (1 + cfg.MOMENTUM_ENTRY_LIMIT_OFFSET), _price_decimals(price)),
            stop_loss=round(price * (1 + cfg.MOMENTUM_ENTRY_LIMIT_OFFSET) * (1 - cfg.MOMENTUM_SQ_SL_PCT / 100), _price_decimals(price)),
            tp1=round(price * (1 + cfg.MOMENTUM_ENTRY_LIMIT_OFFSET) * (1 + cfg.MOMENTUM_SQ_TP1_PCT / 100), _price_decimals(price)),
            tp2=round(price * (1 + cfg.MOMENTUM_ENTRY_LIMIT_OFFSET) * (1 + cfg.MOMENTUM_SQ_TP2_PCT / 100), _price_decimals(price)),
            risk_usd=sq_risk_usd, reward_tp1_usd=sq_rwd_tp1, reward_tp2_usd=sq_rwd_tp2,
            rr_str=f"1:{sq_rwd_tp1/sq_risk_usd:.2f}", sl_pct=cfg.MOMENTUM_SQ_SL_PCT,
            infinite_supply=has_inf,
            ath_dist_pct=_sq_ath_dist,
            ath_price=_sq_ath_price,
            ath_date=_sq_ath_date,
            squeeze_bypass=True,
        ))

    # ── Stage 2h: Speed Alert Track ⚡ ────────────────────────────────────────
    speed_alerts: list[MomentumResult] = []
    alerted_symbols_all = {r.symbol for r in new_alerts + cooling_alerts + gc_alerts +
                           vs_alerts + rb_alerts + pbw_alerts + sc_alerts + sq_alerts}
    atl_buffer_pct = cfg.MOMENTUM_FEAR_ATL_BUFFER_PCT if _fear_mode else cfg.MOMENTUM_SPEED_ATL_BUFFER_PCT

    for coin_data in coins:
        sym    = (coin_data.get("symbol") or "").upper()
        name   = coin_data.get("name") or sym
        _q     = coin_data.get("quote", {}).get("USD", {})
        mcap   = float(_q.get("market_cap")         or 0)
        price  = float(_q.get("price")              or 0)
        c24h   = float(_q.get("percent_change_24h") or 0)
        c1h    = float(_q.get("percent_change_1h")  or 0)
        vol24h = float(_q.get("volume_24h")         or 0)
        mexc_sym = sym + "_USDT"

        if sym in alerted_symbols_all:
            continue
        if mexc_sym not in futures_set:
            continue
        if mcap < cfg.MOMENTUM_SPEED_MCAP_MIN:
            continue
        if _on_speed_cooldown(sym):
            continue
        if _on_global_cooldown(sym, "SPEED ALERT", price):
            continue

        atl = _lookup_atl(_ath_map, sym)
        if atl > 0 and price <= atl * (1 + atl_buffer_pct / 100.0):
            log.debug(f"  SPEED skip {sym}: price within {atl_buffer_pct}% of ATL")
            continue

        spike = _check_speed_alert(mexc_sym)
        if spike is None:
            continue

        log.info(
            f"  ⚡ SPEED ALERT  {sym}  gain {spike['candle_gain']:+.1f}%  "
            f"vol {spike['vol_ratio']:.1f}×  pre-RSI {spike['pre_rsi']:.0f}"
        )

        sp_entry = round(price * (1 + cfg.MOMENTUM_ENTRY_LIMIT_OFFSET), _price_decimals(price))
        sp_sl    = round(sp_entry * (1 - cfg.MOMENTUM_SPEED_SL_PCT  / 100), _price_decimals(price))
        sp_tp1   = round(sp_entry * (1 + cfg.MOMENTUM_SPEED_TP1_PCT / 100), _price_decimals(price))
        sp_tp2   = round(sp_entry * (1 + cfg.MOMENTUM_SPEED_TP2_PCT / 100), _price_decimals(price))
        sp_risk  = round(cfg.MOMENTUM_POSITION_USD * cfg.MOMENTUM_SPEED_SL_PCT  / 100, 2)
        sp_rwd1  = round(cfg.MOMENTUM_POSITION_USD * cfg.MOMENTUM_SPEED_TP1_PCT / 100, 2)
        sp_rwd2  = round(cfg.MOMENTUM_POSITION_USD * cfg.MOMENTUM_SPEED_TP2_PCT / 100, 2)

        _sp_ath_dist, _sp_ath_price, _sp_ath_date = _lookup_ath(_ath_map, sym, price, 0.0)

        speed_alerts.append(MomentumResult(
            symbol=sym, name=name, price=round(price, 8),
            change_1h=round(c1h, 2), change_24h=round(c24h, 2),
            market_cap=mcap, volume_24h=vol24h,
            fdv=0.0, fdv_mcap_ratio=0.0, circ_supply_pct=0.0,
            mexc_symbol=mexc_sym,
            recommendation="SPEED ALERT", rec_emoji="⚡",
            warnings=["Spike-type — TP1 priority, no greed. Move is short and fast."],
            entry_price=sp_entry, stop_loss=sp_sl, tp1=sp_tp1, tp2=sp_tp2,
            sl_pct=cfg.MOMENTUM_SPEED_SL_PCT,
            risk_usd=sp_risk, reward_tp1_usd=sp_rwd1, reward_tp2_usd=sp_rwd2,
            rr_str=f"1:{sp_rwd1/sp_risk:.2f}",
            ath_dist_pct=_sp_ath_dist, ath_price=_sp_ath_price, ath_date=_sp_ath_date,
        ))
        _mark_speed_alerted(sym)
        _mark_global_alerted(sym, "SPEED ALERT", price)
        alerted_symbols_all.add(sym)

    global _last_speed_count
    _last_speed_count = len(speed_alerts)

    # ── Stage 2i: Early GC (5m EMA cross) pipeline ───────────────────────────
    early_gc_alerts: list[MomentumResult] = []
    _all_alerted_syms = {r.symbol for r in new_alerts + cooling_alerts + gc_alerts +
                         vs_alerts + rb_alerts + pbw_alerts + sc_alerts + sq_alerts + speed_alerts}

    for entry in candidates:
        (symbol, name, matched_tags, mexc_symbol,
         price, change_1h, change_24h, mcap, vol_24h, fdv, fdv_ratio, circ_pct) = entry

        if symbol in _all_alerted_syms:
            continue
        if mcap < cfg.MOMENTUM_EARLY_GC_MCAP_MIN:
            continue
        if _on_early_gc_cooldown(symbol):
            continue
        if _on_global_cooldown(symbol, "EARLY GC", price):
            continue

        # Check 5m cross first (cheap — skip if no fresh cross)
        m5gc = _check_5m_gc(mexc_symbol)
        if m5gc is None:
            continue

        # Fetch technicals for 15m EMA check + 4H indicators
        egc_tech = _check_technicals(mexc_symbol, cfg.MOMENTUM_VOL_GC_MIN)
        if egc_tech is None:
            continue

        # 15m EMA6 > EMA20 required
        if not egc_tech.m15_ema_ok:
            log.debug(f"  EARLY_GC skip {symbol}: 15m EMA6 < EMA20")
            continue

        # 4H gate: accept if bullish OR transitioning (EMA6 > EMA12 AND EMA6 > EMA20 + 24H > 0)
        h4_transition_ok = (egc_tech.h4_ema6 > egc_tech.h4_ema12 and
                            egc_tech.h4_ema6 > egc_tech.h4_ema20 and
                            change_24h > 0)
        if not (egc_tech.h4_ema_ok or h4_transition_ok):
            log.debug(f"  EARLY_GC skip {symbol}: 4H EMA bearish, no transition")
            continue

        if h4_transition_ok and not egc_tech.h4_ema_ok:
            egc_tech.h4_transitioning = True

        _egc_ath_dist, _egc_ath_price, _egc_ath_date = _lookup_ath(
            _ath_map, symbol, price, egc_tech.ath_dist_pct)
        egc_score = _score_early_gc(egc_tech, m5gc, _egc_ath_dist)

        if egc_score < cfg.MOMENTUM_EARLY_GC_MIN_SCORE:
            log.debug(f"  EARLY_GC skip {symbol}: score {egc_score} < {cfg.MOMENTUM_EARLY_GC_MIN_SCORE}")
            continue

        log.info(
            f"  ⚡ EARLY GC  {symbol}  1h {change_1h:+.2f}%  "
            f"5m RSI {m5gc['m5_rsi6']:.1f}  vol {m5gc['vol_ratio']:.1f}×  score {egc_score}"
        )

        egc_risk_usd = round(cfg.MOMENTUM_POSITION_USD * cfg.MOMENTUM_EARLY_GC_SL_PCT / 100.0, 2)
        egc_rr_str   = f"1:{_rwd_tp1 / egc_risk_usd:.2f}"

        # Store m5gc data in tech for alert builder access
        egc_tech.m5_rsi6  = m5gc["m5_rsi6"]
        egc_tech.m5_ema20 = m5gc["m5_ema20"]

        early_gc_alerts.append(MomentumResult(
            symbol          = symbol,
            name            = name,
            price           = round(price, 8),
            change_1h       = round(change_1h, 2),
            change_24h      = round(change_24h, 2),
            market_cap      = mcap,
            volume_24h      = vol_24h,
            fdv             = fdv,
            fdv_mcap_ratio  = round(fdv_ratio, 2),
            circ_supply_pct = round(circ_pct, 1),
            matched_tags    = matched_tags,
            mexc_symbol     = mexc_symbol,
            tech            = egc_tech,
            total_score     = egc_score,
            recommendation  = "EARLY GC",
            rec_emoji       = "⚡",
            warnings        = ["Early signal — reduced confirmation",
                               "Use 50% of normal position size"],
            m5_note         = "",
            entry_price     = round(price * (1 + cfg.MOMENTUM_ENTRY_LIMIT_OFFSET), _price_decimals(price)),
            stop_loss       = round(price * (1 + cfg.MOMENTUM_ENTRY_LIMIT_OFFSET) * (1 - cfg.MOMENTUM_EARLY_GC_SL_PCT / 100), _price_decimals(price)),
            tp1             = round(price * (1 + cfg.MOMENTUM_ENTRY_LIMIT_OFFSET) * (1 + cfg.MOMENTUM_TP1_PCT / 100), _price_decimals(price)),
            tp2             = round(price * (1 + cfg.MOMENTUM_ENTRY_LIMIT_OFFSET) * (1 + cfg.MOMENTUM_TP2_PCT / 100), _price_decimals(price)),
            risk_usd        = egc_risk_usd,
            reward_tp1_usd  = _rwd_tp1,
            reward_tp2_usd  = _rwd_tp2,
            rr_str          = egc_rr_str,
            sl_pct          = cfg.MOMENTUM_EARLY_GC_SL_PCT,
            ath_dist_pct    = _egc_ath_dist,
            ath_price       = _egc_ath_price,
            ath_date        = _egc_ath_date,
        ))
        _mark_early_gc_alerted(symbol)
        _mark_global_alerted(symbol, "EARLY GC", price)
        _all_alerted_syms.add(symbol)

    # ── CHANGE 5B: Session-level dedup — one alert per coin per scan ────────────
    _DEDUP_PRIORITY = {
        "SQUEEZE": 8, "STRONG ENTRY": 7, "WATCH": 6, "RECOVERY": 5,
        "GOLDEN CROSS": 4, "STAIRCASE": 4, "VOLUME SPIKE": 3,
        "PRE-BREAKOUT": 3, "SPEED ALERT": 3,
        "EARLY GC": 2, "COOLING_DOWN": 1,
    }
    _all_this_scan = (new_alerts + cooling_alerts + gc_alerts + vs_alerts +
                      rb_alerts + pbw_alerts + sc_alerts + sq_alerts + speed_alerts +
                      early_gc_alerts)
    _best_per_sym: dict[str, MomentumResult] = {}
    for _r in _all_this_scan:
        _prev = _best_per_sym.get(_r.symbol)
        if _prev is None:
            _best_per_sym[_r.symbol] = _r
        else:
            _r_pri   = _DEDUP_PRIORITY.get(_r.recommendation, 0)
            _pre_pri = _DEDUP_PRIORITY.get(_prev.recommendation, 0)
            if _r_pri > _pre_pri or (_r_pri == _pre_pri and _r.total_score > _prev.total_score):
                log.info(
                    f"  Dedup: {_r.symbol} — kept {_r.recommendation} ({_r.total_score}), "
                    f"dropped {_prev.recommendation} ({_prev.total_score})"
                )
                _best_per_sym[_r.symbol] = _r
            else:
                log.info(
                    f"  Dedup: {_r.symbol} — kept {_prev.recommendation} ({_prev.total_score}), "
                    f"dropped {_r.recommendation} ({_r.total_score})"
                )
    _keep_ids = {id(r) for r in _best_per_sym.values()}
    new_alerts      = [r for r in new_alerts      if id(r) in _keep_ids]
    cooling_alerts  = [r for r in cooling_alerts  if id(r) in _keep_ids]
    gc_alerts       = [r for r in gc_alerts       if id(r) in _keep_ids]
    vs_alerts       = [r for r in vs_alerts       if id(r) in _keep_ids]
    rb_alerts       = [r for r in rb_alerts       if id(r) in _keep_ids]
    pbw_alerts      = [r for r in pbw_alerts      if id(r) in _keep_ids]
    sc_alerts       = [r for r in sc_alerts       if id(r) in _keep_ids]
    sq_alerts       = [r for r in sq_alerts       if id(r) in _keep_ids]
    speed_alerts    = [r for r in speed_alerts    if id(r) in _keep_ids]
    early_gc_alerts = [r for r in early_gc_alerts if id(r) in _keep_ids]

    # Populate alert_watchlist after dedup (Tier 3 leg tracking, Part D)
    _all_deduped = (new_alerts + gc_alerts + vs_alerts + rb_alerts +
                    pbw_alerts + sc_alerts + sq_alerts + speed_alerts + early_gc_alerts)
    for _r in _all_deduped:
        _add_to_watchlist(_r.symbol, _r.price, _r.recommendation)

    strong  = sum(r.recommendation == "STRONG ENTRY" for r in new_alerts)
    watch   = sum(r.recommendation == "WATCH"        for r in new_alerts)
    gc      = len(gc_alerts)
    vs      = len(vs_alerts)
    rb      = len(rb_alerts)
    pbw     = len(pbw_alerts)
    sc_n    = len(sc_alerts)
    sq_n    = len(sq_alerts)
    sp_n    = len(speed_alerts)
    egc_n   = len(early_gc_alerts)
    cooling = len(cooling_alerts)
    log.info(
        f"Scan done — {len(candidates)} M1–M7, "
        f"{macro_blocked_count} macro-blocked, "
        f"{len(scored)} scored ≥{cfg.MOMENTUM_TOTAL_WATCH}, "
        f"{strong} STRONG + {watch} WATCH + "
        f"{gc} GC + {vs} VS + {rb} RB + {pbw} PBW + {sc_n} SC + {sq_n} SQ + "
        f"{sp_n} SPEED + {egc_n} EARLY_GC + {cooling} COOLING alerts."
    )
    return (new_alerts + cooling_alerts + gc_alerts + vs_alerts + rb_alerts +
            pbw_alerts + sc_alerts + sq_alerts + speed_alerts + early_gc_alerts)


# ══════════════════════════════════════════════════════════════════════════════
# Tier 2: 5-min rescan of active_watch coins
# ══════════════════════════════════════════════════════════════════════════════

def scan_tier2() -> list:
    """
    Tier 2 scan — every 5 minutes.
    Re-scans coins in _active_watch (those showing 5m EMA cross / spike in last Tier 1).
    Skips full Stage 1 CMC fetch. Applies Stage 2 momentum checks only.
    Returns new alerts (same MomentumResult type as scan()).
    """
    global _tier2_last_run
    _tier2_last_run = time.time()

    if not _active_watch:
        log.debug("Tier 2: active_watch empty — skipping.")
        return []

    log.info(f"Tier 2 scan: {len(_active_watch)} coins in active_watch.")
    _refresh_market_context()
    _ath_map    = get_coingecko_ath_map(limit=500)
    futures_set = get_mexc_futures_symbols()
    if not futures_set:
        log.error("Tier 2 aborted — MEXC futures list unavailable.")
        return []

    results: list = []
    alerted_syms  = set(_global_alerted.keys())

    for mexc_symbol in list(_active_watch):
        symbol = mexc_symbol.replace("_USDT", "")
        if _on_global_cooldown(symbol, "TIER2", 0.0):
            continue
        if mexc_symbol not in futures_set:
            continue

        m5 = _check_5m(mexc_symbol)
        if not _base_5m_gate_ok(m5):
            continue

        tech = _check_technicals(mexc_symbol)
        if tech is None:
            continue

        price = tech.m15_price
        if price <= 0:
            continue

        _apply_5m_to_tech(tech, m5)

        # 4H gate: Method A, B, or C
        h4_trans_ok = tech.h4_ema6 > tech.h4_ema12 and tech.h4_ema6 > tech.h4_ema20
        if not (tech.h4_ema_ok or tech.h4_method_b or h4_trans_ok):
            continue

        # Simple scoring for Tier 2 (fundamental data pulled from CMC cache)
        tier2_score = tech.score + (5 if tech.h4_ema_ok else 0) + (3 if tech.h4_method_b else 0)

        if tier2_score < 55:
            continue

        decimals = _price_decimals(price)
        _ath_dist, _ath_px, _ath_date = _lookup_ath(_ath_map, symbol, price, tech.ath_dist_pct)

        # Pull real CMC data from cache so name/mcap/circ show correctly in alert
        cached = _cmc_data_cache.get(symbol)
        if cached:
            t2_name, t2_tags, t2_mcap, t2_vol, t2_fdv, t2_fdr, t2_circ = cached
        else:
            t2_name, t2_tags, t2_mcap, t2_vol, t2_fdv, t2_fdr, t2_circ = (
                symbol, [], 0.0, 0.0, 0.0, 0.0, 0.0
            )

        log.info(f"  Tier2 ✓  {symbol}  score {tier2_score}  → WATCH  MCap ${t2_mcap/1e6:.0f}M")
        _mark_alerted(symbol)
        _mark_global_alerted(symbol, "WATCH", price)
        _add_to_watchlist(symbol, price, "WATCH")

        results.append(MomentumResult(
            symbol=symbol, name=t2_name, price=round(price, 8),
            change_1h=0.0, change_24h=0.0,
            market_cap=t2_mcap, volume_24h=t2_vol, fdv=t2_fdv,
            fdv_mcap_ratio=round(t2_fdr, 2), circ_supply_pct=round(t2_circ, 1),
            matched_tags=t2_tags,
            mexc_symbol=mexc_symbol,
            tech=tech, total_score=tier2_score,
            recommendation="WATCH", rec_emoji="🟡",
            warnings=["Tier 2 rescan — verify chart before entry"],
            entry_price=round(price * (1 + cfg.MOMENTUM_ENTRY_LIMIT_OFFSET), decimals),
            stop_loss=round(price * (1 + cfg.MOMENTUM_ENTRY_LIMIT_OFFSET) * (1 - cfg.MOMENTUM_SL_PCT / 100), decimals),
            tp1=round(price * (1 + cfg.MOMENTUM_ENTRY_LIMIT_OFFSET) * (1 + cfg.MOMENTUM_TP1_PCT / 100), decimals),
            tp2=round(price * (1 + cfg.MOMENTUM_ENTRY_LIMIT_OFFSET) * (1 + cfg.MOMENTUM_TP2_PCT / 100), decimals),
            sl_pct=cfg.MOMENTUM_SL_PCT,
            ath_dist_pct=_ath_dist, ath_price=_ath_px, ath_date=_ath_date,
        ))

    log.info(f"Tier 2 done — {len(results)} alert(s).")
    return results


# ══════════════════════════════════════════════════════════════════════════════
# Tier 3: 3-min leg continuation scan of alert_watchlist
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class LegContinuationResult:
    """Returned by scan_tier3() when a leg continuation is detected."""
    symbol:      str
    mexc_symbol: str
    leg_number:  int
    price:       float
    leg_high:    float
    pullback_pct: float
    signal_type: str    # original signal type
    matched_tags: list  = field(default_factory=list)


def scan_tier3() -> list[MomentumResult]:
    """
    Tier 3 scan — every 3 minutes.
    Scans _alert_watchlist coins for leg continuation signals.
    Returns MomentumResult objects with recommendation="LEG_CONTINUATION".
    """
    global _tier3_last_run, _alert_watchlist
    _tier3_last_run = time.time()

    if not _alert_watchlist:
        log.debug("Tier 3: alert_watchlist empty — skipping.")
        return []

    log.info(f"Tier 3 scan: {len(_alert_watchlist)} coins on watchlist.")
    _ath_map    = get_coingecko_ath_map(limit=500)
    futures_set = get_mexc_futures_symbols()
    if not futures_set:
        return []

    results: list[MomentumResult] = []
    to_expire: list[str] = []

    for symbol, wl_entry in list(_alert_watchlist.items()):
        mexc_symbol  = f"{symbol}_USDT"
        leg_high     = wl_entry.get("leg_high",    0.0)
        entry_price  = wl_entry.get("entry_price", 0.0)
        leg_number   = wl_entry.get("leg_number",  1)
        alert_time   = wl_entry.get("alert_time",  0.0)
        signal_type  = wl_entry.get("signal_type", "")

        if mexc_symbol not in futures_set:
            continue
        if _on_global_cooldown(symbol, "LEG_CONTINUATION", 0.0):
            continue
        if time.time() - alert_time > 72 * 3600:
            to_expire.append(symbol)
            continue

        m5 = _check_5m(mexc_symbol)
        if not _base_5m_gate_ok(m5):
            continue

        if m5 is None:
            continue

        price = float(m5.get("ema20", 0.0)) or 0.0
        tech  = _check_technicals(mexc_symbol)
        if tech is None:
            continue
        price = tech.m15_price
        if price <= 0:
            continue

        # Leg detection criteria
        if leg_high <= 0:
            leg_high = price

        pullback_pct = (leg_high - price) / leg_high * 100 if leg_high > 0 else 0.0

        # (1) Price pulled back ≥6% from leg_high
        if pullback_pct < 6.0:
            # Update leg_high if price has moved up
            if price > leg_high:
                wl_entry["leg_high"] = price
                leg_high = price
            continue

        # (2) Volume dried up: 5m vol < 70% of MA10 (consolidation, not chasing)
        if m5.get("vol_pct", 100.0) >= 70.0:
            continue

        # (3) Fresh 5m EMA cross: EMA6 just crossed above EMA20
        if not m5.get("fresh_cross", False):
            continue

        # (4) RSI reset from overbought: was >70 recently AND now rising from below 58
        _df5_leg = get_mexc_futures_klines(mexc_symbol, "5m", limit=15)
        _rsi_ok = False
        if _df5_leg is not None and len(_df5_leg) >= 10:
            _rsi_s     = compute_rsi(_df5_leg["close"].astype(float), period=6)
            _rsi_now   = float(_rsi_s.iloc[-1])
            _rsi_prev  = float(_rsi_s.iloc[-2]) if len(_rsi_s) >= 2 else _rsi_now
            _rsi_max8  = float(_rsi_s.iloc[-8:].max()) if len(_rsi_s) >= 8 else _rsi_now
            _was_overbought = _rsi_max8 > 70.0
            _rsi_ok = _was_overbought and _rsi_now < 58.0 and _rsi_now > _rsi_prev
        if not _rsi_ok:
            continue

        _apply_5m_to_tech(tech, m5)

        new_leg = leg_number + 1
        log.info(
            f"  🔄 LEG {new_leg}  {symbol}  pullback {pullback_pct:.1f}%  "
            f"price {price:.4g}  leg_high {leg_high:.4g}"
        )

        decimals = _price_decimals(price)
        _ath_dist, _ath_px, _ath_date = _lookup_ath(_ath_map, symbol, price, tech.ath_dist_pct)

        _mark_alerted(symbol)
        _mark_global_alerted(symbol, "LEG_CONTINUATION", price)
        wl_entry["leg_number"] = new_leg
        wl_entry["leg_high"]   = price
        wl_entry["alert_time"] = time.time()

        result = MomentumResult(
            symbol=symbol, name=symbol, price=round(price, 8),
            change_1h=0.0, change_24h=0.0,
            market_cap=0.0, volume_24h=0.0, fdv=0.0,
            fdv_mcap_ratio=0.0, circ_supply_pct=0.0,
            mexc_symbol=mexc_symbol,
            tech=tech, total_score=tech.score,
            recommendation="LEG_CONTINUATION", rec_emoji="🔄",
            warnings=[f"Leg {new_leg} — confirm 1m chart before entry"],
            entry_price=round(price * (1 + cfg.MOMENTUM_ENTRY_LIMIT_OFFSET), decimals),
            stop_loss=round(price * (1 + cfg.MOMENTUM_ENTRY_LIMIT_OFFSET) * (1 - cfg.MOMENTUM_SL_PCT / 100), decimals),
            tp1=round(price * (1 + cfg.MOMENTUM_ENTRY_LIMIT_OFFSET) * (1 + cfg.MOMENTUM_TP1_PCT / 100), decimals),
            tp2=round(price * (1 + cfg.MOMENTUM_ENTRY_LIMIT_OFFSET) * (1 + cfg.MOMENTUM_TP2_PCT / 100), decimals),
            sl_pct=cfg.MOMENTUM_SL_PCT,
            ath_dist_pct=_ath_dist, ath_price=_ath_px, ath_date=_ath_date,
        )
        # Attach leg metadata for the alert builder
        result.__dict__["leg_number"] = new_leg
        result.__dict__["leg_high"]   = round(leg_high, 8)
        results.append(result)

    for sym in to_expire:
        del _alert_watchlist[sym]

    log.info(f"Tier 3 done — {len(results)} leg continuation(s).")
    return results


def get_tier_status() -> dict:
    """Return tier status info for /status command display."""
    now = time.time()
    return {
        "tier1_ago":   int(now - _tier1_last_run)   if _tier1_last_run > 0 else -1,
        "tier2_coins": len(_active_watch),
        "tier2_ago":   int(now - _tier2_last_run)   if _tier2_last_run > 0 else -1,
        "tier3_coins": len(_alert_watchlist),
        "tier3_ago":   int(now - _tier3_last_run)   if _tier3_last_run > 0 else -1,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Public helpers used by telegram_alerts.py and standalone test
# ══════════════════════════════════════════════════════════════════════════════

def category_label(result: MomentumResult) -> str:
    return _best_category_label(result.matched_tags)


def tech_summary_lines(result: MomentumResult) -> list[str]:
    """Plain-text breakdown for the terminal smoke test."""
    t = result.tech
    f = result.fund
    if t is None:
        return ["  ⚫  Technical data unavailable"]

    def ck(ok: bool) -> str:
        return "✅" if ok else "❌"

    rsi_flag = "  ⚠️  hot"        if t.m15_rsi6_hot else ""
    kdj_flag = "  ⚠️  overbought" if t.m15_kdj_hot  else ""

    lines = [
        "  ── 4H Macro Filter ─────────────────────────────────────────────",
        f"  {ck(t.h4_ema_ok)}  EMA Stack (4H)    "
        f"{t.h4_ema6:.5g} {'>' if t.h4_ema6 > t.h4_ema12 else '<'} "
        f"{t.h4_ema12:.5g} {'>' if t.h4_ema12 > t.h4_ema20 else '<'} "
        f"{t.h4_ema20:.5g}",
        f"  {ck(t.h4_kdj_ok)}  KDJ J     (4H)    {t.h4_kdj_j:.1f}  [<{cfg.MOMENTUM_TA_H4_KDJ_J_MAX:.0f}]",
        "",
        "  ── 15m Scoring ──────────────────────────────────────────────────",
        f"  {ck(t.m15_ema_ok)}  EMA6 > EMA20      "
        f"{t.m15_ema6:.5g} vs {t.m15_ema20:.5g}   +{t.m15_ema_pts} pts",
        f"  {ck(t.m15_price_ok)}  Price > EMA20     "
        f"{t.m15_price:.5g} vs {t.m15_ema20:.5g}   +{t.m15_price_pts} pts",
        f"  {ck(t.m15_rsi6_ok)}  RSI6              "
        f"{t.m15_rsi6:.1f}  [40–72]{rsi_flag}   +{t.m15_rsi6_pts} pts",
        f"  {ck(t.m15_kdj_ok)}  KDJ J             "
        f"{t.m15_kdj_j:.1f}  [<{cfg.MOMENTUM_KDJ_MAX:.0f}]{kdj_flag}   +{t.m15_kdj_pts} pts",
        f"  {ck(t.m15_macd_ok)}  MACD DIF > DEA    "
        f"DIF {t.m15_macd_dif:.5g}  DEA {t.m15_macd_dea:.5g}   +{t.m15_macd_pts} pts",
        "",
        "  ── Volume (4H) ──────────────────────────────────────────────────",
        f"  {ck(t.vol_ok)}  Vol vs MA10       "
        f"{t.vol_pct:.0f}%  [≥120%]   +{t.vol_pts} pts",
        "",
        f"  Technical:   {t.score}/{_PTS_MAX} pts",
    ]

    if f is not None:
        lines += [
            "",
            "  ── Fundamental Bonus ────────────────────────────────────────────",
            f"  {ck(f.mcap_pts > 0)}  MCap $50M–$2B      +{f.mcap_pts} pts",
            f"  {ck(f.circ_pts > 0)}  Circ > 60%         +{f.circ_pts} pts",
            f"  {ck(f.fdv_pts  > 0)}  FDV/MCap < 2×      +{f.fdv_pts} pts",
            f"  {'✅' if f.gain_pts == 5 else ('⚡' if f.gain_pts == 2 else '❌')}  "
            f"1h gain sweet spot   +{f.gain_pts} pts",
            f"  Fundamental: {f.total}/{_FUND_MAX} pts",
        ]

    lines += [
        "",
        f"  {'═' * 52}",
        f"  TOTAL:  {result.total_score}/100  {result.rec_emoji}  {result.recommendation}",
    ]

    if result.entry_price > 0:
        lines += [
            "",
            f"  📍  Entry   {result.entry_price:.6g}",
            f"  🛑  SL      {result.stop_loss:.6g}  (-{cfg.MOMENTUM_SL_PCT:.0f}%)  → risk ${result.risk_usd:.0f}",
            f"  🎯  TP1     {result.tp1:.6g}  (+{cfg.MOMENTUM_TP1_PCT:.0f}%)  → reward ${result.reward_tp1_usd:.0f}",
            f"  🚀  TP2     {result.tp2:.6g}  (+{cfg.MOMENTUM_TP2_PCT:.0f}%)  → max ${result.reward_tp2_usd:.0f}",
            f"  R/R    {result.rr_str}",
        ]

    if result.warnings:
        lines.append("")
        lines.append("  ⚠️  Warnings:")
        for w in result.warnings:
            lines.append(f"    · {w}")

    return lines


# ══════════════════════════════════════════════════════════════════════════════
# Stand-alone test
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("Running Momentum Scanner…\n")

    results = scan()

    if not results:
        print("No alerts this run (total score < 65 or cooldown).")
    else:
        SEP = "─" * 72
        for r in results:
            cat = category_label(r)
            print(SEP)
            print(
                f"  {r.rec_emoji}  {r.recommendation}  |  {r.symbol}  "
                f"{r.change_1h:+.2f}%  │  ${r.market_cap/1e6:.0f}M MCap  │  "
                f"{cat}  │  {r.mexc_symbol}  │  Score {r.total_score}/100"
            )
            print()
            for line in tech_summary_lines(r):
                print(line)
            print()

        print(SEP)
        strong = sum(r.recommendation == "STRONG ENTRY" for r in results)
        watch  = sum(r.recommendation == "WATCH"        for r in results)
        print(f"\nSTRONG ENTRY: {strong}  │  WATCH: {watch}")
