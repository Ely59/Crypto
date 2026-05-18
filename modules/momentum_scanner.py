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
from dataclasses import dataclass, field

from utils.api_client import (
    get_cmc_momentum_listings,
    get_mexc_futures_symbols,
    get_mexc_futures_klines,
)
from utils.indicators import compute_ema, compute_rsi, compute_macd, compute_kdj
from utils.logger     import get_logger
import config as cfg

log = get_logger(__name__)

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
    m5_first_green:       bool  = False
    m5_vol_pct:           float = 0.0
    m5_ok:                bool  = False
    m5_note:              str   = ""


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
    ath_dist_pct:     float = 0.0    # % below 16D peak (direct field for PBW/SC without tech)

    # 5m layer data and advisory note (for signal chain display in alerts)
    m5_rsi6:   float = 0.0
    m5_kdj_j:  float = 0.0
    m5_note:   str   = ""


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
    h4_ema6  = float(compute_ema(close_4h,  6).iloc[-1])
    h4_ema12 = float(compute_ema(close_4h, 12).iloc[-1])
    h4_ema20 = float(compute_ema(close_4h, 20).iloc[-1])
    h4_ema_sep = (h4_ema6 - h4_ema20) / h4_ema20 if h4_ema20 > 0 else 0.0
    h4_ema_ok  = (h4_ema6 > h4_ema12 > h4_ema20 and
                  h4_ema_sep >= cfg.MOMENTUM_TA_H4_EMA_SEP_MIN)

    h4_rsi6    = float(compute_rsi(close_4h, period=6).iloc[-1])
    h4_dif, h4_dea, _ = compute_macd(close_4h)
    h4_macd_ok = float(h4_dif.iloc[-1]) > float(h4_dea.iloc[-1])

    _, _, j4h = compute_kdj(df_4h)
    h4_kdj_j  = float(j4h.iloc[-1])
    h4_kdj_ok = h4_kdj_j < cfg.MOMENTUM_TA_H4_KDJ_J_MAX   # informational only — no longer a hard gate

    # FIX 4: KDJ is a warning, not a blocker — only EMA stack gates entry
    macro_ok = h4_ema_ok

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

    m15_price     = float(close_15m.iloc[-1])
    m15_price_ok  = m15_price > m15_ema20
    m15_price_pts = _PTS_PRICE if m15_price_ok else 0

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
    """Generate up to 5 warnings (mandatory checks + optional risk flags)."""
    mandatory: list[str] = []
    optional:  list[str] = []

    # Mandatory: always shown when condition met
    if is_micro_cap:
        mandatory.append(f"⚠️ Micro-Cap: ${mcap/1e6:.0f}M — half position size")
        mandatory.append("⚠️ Treat as high-risk, max 50% normal margin")
    if has_inf_supply:
        mandatory.append("⚠️ Max Supply unknown — inflation risk")
    if circ_pct > 0 and circ_pct < cfg.MOMENTUM_WARN_CIRC_ALERT_PCT:
        mandatory.append(f"Circ Rate: {circ_pct:.0f}% — Dilution-Risiko")
    if fdv_ratio > cfg.MOMENTUM_WARN_FDV_ALERT_RATIO:
        mandatory.append(f"FDV/MCap: {fdv_ratio:.1f}×")

    # Optional: technical risk flags
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

    return mandatory + optional[:2]


# ══════════════════════════════════════════════════════════════════════════════
# Main scan
# ══════════════════════════════════════════════════════════════════════════════

def _check_1m_pbw(mexc_symbol: str) -> "dict | None":
    """
    Check 1m candles for Pre-Breakout Watch conditions.
    Returns a dict with computed values, or None if data unavailable.
    """
    df_1m = get_mexc_futures_klines(mexc_symbol, "1m", limit=100)
    if df_1m is None or len(df_1m) < 20:
        return None

    close_1m = df_1m["close"]
    ema6_s   = compute_ema(close_1m, 6)
    ema20_s  = compute_ema(close_1m, 20)
    m1_ema6  = float(ema6_s.iloc[-1])
    m1_ema20 = float(ema20_s.iloc[-1])
    ema_spread = (abs(m1_ema6 - m1_ema20) / m1_ema20 * 100.0) if m1_ema20 > 0 else 999.0

    rsi_s = compute_rsi(close_1m, period=6)
    if len(rsi_s) < cfg.MOMENTUM_PBW_RSI_CANDLES:
        return None

    # Count consecutive recent candles with RSI < threshold
    streak = 0
    for i in range(1, len(rsi_s) + 1):
        if float(rsi_s.iloc[-i]) < cfg.MOMENTUM_PBW_RSI_MAX:
            streak += 1
        else:
            break

    vol_s    = df_1m["volume"]
    vol_last = float(vol_s.iloc[-1])
    vol_ma10 = float(vol_s.rolling(10).mean().iloc[-1])
    vol_ratio = vol_last / vol_ma10 if vol_ma10 > 0 else 0.0

    return {
        "ema_spread":  round(ema_spread, 3),
        "compressed":  ema_spread < cfg.MOMENTUM_PBW_EMA_SPREAD_MAX,
        "rsi_streak":  streak,
        "rsi_ok":      streak >= cfg.MOMENTUM_PBW_RSI_CANDLES,
        "vol_ratio":   round(vol_ratio, 2),
        "trigger_vol": vol_ratio >= cfg.MOMENTUM_PBW_VOL_MULT,
        "trigger_px":  float(close_1m.iloc[-1]) > m1_ema20,
    }


def _check_5m(mexc_symbol: str) -> "dict | None":
    """
    Fetch 5m candles and compute precision-layer indicators.
    Returns a plain dict (soft gate data only — never used to block alerts).
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

    return {
        "rsi6":               round(m5_rsi6, 2),
        "kdj_j":              round(m5_kdj_j, 2),
        "kdj_rising":         m5_kdj_rising,
        "price_above_ema20":  m5_price > m5_ema20,
        "ema6_gt_ema12":      m5_ema6 > m5_ema12,
        "first_green":        m5_price > m5_open_last,
        "vol_pct":            round(m5_vol_pct, 1),
    }


def _price_decimals(price: float) -> int:
    """Return decimal places for MEXC-ready price display."""
    if price >= 0.10:
        return 4
    elif price >= 0.01:
        return 5
    else:
        return 6


def _apply_5m_to_tech(tech: TechResult, m5: "dict | None") -> None:
    """Populate TechResult 5m fields from _check_5m() result (mutates in place)."""
    if m5 is None:
        return
    tech.m5_rsi6              = m5["rsi6"]
    tech.m5_kdj_j             = m5["kdj_j"]
    tech.m5_kdj_rising        = m5["kdj_rising"]
    tech.m5_price_above_ema20 = m5["price_above_ema20"]
    tech.m5_ema6_gt_ema12     = m5["ema6_gt_ema12"]
    tech.m5_first_green       = m5["first_green"]
    tech.m5_vol_pct           = m5["vol_pct"]
    # Evaluate 5m overall health
    if m5["rsi6"] > cfg.MOMENTUM_5M_RSI_HOT:
        tech.m5_ok   = False
        tech.m5_note = f"⏳ 5m überhitzt (RSI {m5['rsi6']:.0f}) — auf Pullback warten"
    elif not m5["price_above_ema20"]:
        tech.m5_ok   = False
        tech.m5_note = "⚠️ 5m noch nicht ideal — Entry-Zone abwarten oder kleinere Position"
    else:
        tech.m5_ok   = True
        tech.m5_note = ""


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


def scan() -> list[MomentumResult]:
    """
    Run one full momentum scan.

    Returns STRONG ENTRY (≥80) and WATCH (≥65) coins only,
    sorted by total score descending, capped at MOMENTUM_MAX_ALERTS_PER_SCAN,
    and filtered by cooldown.
    """
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

    # ── Stage 1: M1–M7 ───────────────────────────────────────────────────────
    candidates     = []   # 1-15% 1H gain — main scoring pipeline
    gc_candidates  = []   # 0.5-8% 1H gain — Golden Cross pipeline
    vs_candidates  = []   # 1-8%  1H gain — Volume Spike pipeline
    rb_candidates  = []   # 2-8%  1H gain + 24H negative — Recovery Bounce pipeline
    pbw_candidates = []   # 1-8%  1H gain — Pre-Breakout Watch pipeline
    sc_candidates  = []   # -2% to +4% 1H gain — Staircase Continuation pipeline
    ft_candidates  = []   # below ZONE_MIN — pending 15m fast-track check
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

        change_1h      = q.get("percent_change_1h")  or 0.0
        change_24h     = q.get("percent_change_24h") or 0.0
        vol_change_24h = q.get("volume_change_24h")  or 0.0
        price          = q.get("price")              or 0.0
        mcap           = q.get("market_cap")         or 0.0
        vol_24h        = q.get("volume_24h")         or 0.0

        if change_1h < cfg.MOMENTUM_EARLY_EXIT_PCT:
            break   # CMC sorted desc — break once below SC minimum
        if change_1h > cfg.MOMENTUM_ZONE_MAX:
            continue   # parabolic — skip all pipelines

        # M2: Market cap (1A — micro-cap bypass: $10M-$25M allowed if Vol/MC > 150%)
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

        # M3: Volume (1B — bypass $5M absolute floor if Vol/MC > 150%; block dead coins < 20%)
        if vol_mc_ratio < cfg.MOMENTUM_VOLMC_DEAD_MAX:      # dead coin — no interest
            _s1_vol_blocked += 1
            continue
        if vol_24h < cfg.MOMENTUM_VOL_24H_MIN_USD and vol_mc_ratio < cfg.MOMENTUM_VOLMC_BYPASS_MIN:
            _s1_vol_blocked += 1
            continue

        # M4: Supply (1D — inf-supply coins bypass circ% gate, get mandatory warning instead)
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

        # Pipeline assignment (before vol_change_24h filter — SC skips it)
        in_main = cfg.MOMENTUM_ZONE_MIN <= change_1h <= cfg.MOMENTUM_ZONE_MAX          # 1.5-12%
        in_gc   = cfg.MOMENTUM_GC_1H_MIN <= change_1h <= cfg.MOMENTUM_GC_1H_MAX          # 0.5-8%
        in_vs   = cfg.MOMENTUM_VS_1H_MIN <= change_1h <= cfg.MOMENTUM_VS_1H_MAX          # 1-8%
        in_rb   = (cfg.MOMENTUM_RB_1H_MIN <= change_1h <= cfg.MOMENTUM_RB_1H_MAX         # 2-8%
                   and change_24h < -3.0)
        in_pbw  = cfg.MOMENTUM_PBW_1H_MIN <= change_1h <= cfg.MOMENTUM_PBW_1H_MAX        # 1-8%
        in_sc   = (cfg.MOMENTUM_SC_1H_MIN <= change_1h <= cfg.MOMENTUM_SC_1H_MAX         # -2% to +4%
                   and change_24h > 3.0)                                                  # had positive 24H

        # Fast-track collection (1C): coin below ZONE_MIN that passes M2-M7
        # 15m candle spike check runs after Stage 1 completes (avoid per-coin API calls in loop)
        if change_1h < cfg.MOMENTUM_ZONE_MIN and not in_sc:
            ft_candidates.append((
                symbol, name, matched_tags, mexc_symbol,
                price, change_1h, change_24h, mcap, vol_24h, fdv, fdv_ratio, circ_pct,
            ))

        # M3 24H volume spike filter — required for all pipelines EXCEPT Staircase
        # (SC candidates consolidate with low volume after the spike)
        if not in_sc:
            if vol_change_24h < (cfg.MOMENTUM_VOL_SPIKE_MIN - 1.0) * 100:
                continue

        entry_tuple = (
            symbol, name, matched_tags, mexc_symbol,
            price, change_1h, change_24h, mcap, vol_24h, fdv, fdv_ratio, circ_pct,
        )
        if in_main:
            log.info(
                f"  M1–M7 ✓  {symbol:<10}  1h {change_1h:+.2f}%  "
                f"MCap ${mcap/1e6:.0f}M  Vol ${vol_24h/1e6:.0f}M  "
                f"FDV/MC {fdv_ratio:.1f}×  [{matched_tags[0]}]"
            )
            candidates.append(entry_tuple)
        if in_gc:
            if not in_main:
                log.info(f"  GC cand  {symbol:<10}  1h {change_1h:+.2f}%  [{matched_tags[0]}]")
            gc_candidates.append(entry_tuple)
        if in_vs and not in_main:
            log.info(f"  VS cand  {symbol:<10}  1h {change_1h:+.2f}%  [{matched_tags[0]}]")
            vs_candidates.append(entry_tuple)
        if in_rb:
            rb_candidates.append(entry_tuple)
        if in_pbw and not in_main and not in_vs:
            log.debug(f"  PBW cand {symbol:<10}  1h {change_1h:+.2f}%  [{matched_tags[0]}]")
            pbw_candidates.append(entry_tuple)
        if in_sc:
            log.debug(f"  SC cand  {symbol:<10}  1h {change_1h:+.2f}%  24h {change_24h:+.1f}%")
            sc_candidates.append(entry_tuple)

    # ── Fast-track: check 15m spike for below-ZONE_MIN coins (CHANGE 1C) ────────
    _fast_track_count = 0
    _candidates_syms  = {e[0] for e in candidates}
    for ft_entry in ft_candidates:
        sym_ft = ft_entry[0]
        if sym_ft in _candidates_syms:
            continue   # already in main pipeline via another path
        mex_ft = ft_entry[3]
        chg_ft = ft_entry[5]
        mc_ft  = ft_entry[7]
        if _check_15m_fasttrack(mex_ft):
            _fast_track_count += 1
            _candidates_syms.add(sym_ft)
            candidates.append(ft_entry)
            log.info(
                f"  ⚡ FAST-TRACK  {sym_ft:<10}  1h {chg_ft:+.2f}%  "
                f"MCap ${mc_ft/1e6:.0f}M — 15m spike bypasses 1H filter"
            )

    # Stage 1 filter breakdown — logged every scan for pipeline analysis
    log.info(
        f"Stage 1 filter stats: "
        f"MCap-block={_s1_mcap_blocked} | Vol-block={_s1_vol_blocked} | "
        f"Supply-block={_s1_supply_blocked} | FDV-block={_s1_fdv_blocked} | "
        f"Tags-block={_s1_tags_blocked} | MEXC-block={_s1_mexc_blocked} | "
        f"MicroCap-allowed={len(_micro_cap_set)} | "
        f"FastTrack={_fast_track_count}/{len(ft_candidates)}"
    )

    global _last_m1m7_count, _last_macro_blocked, _last_scan_outcomes, _last_rb_watchlist
    _last_scan_outcomes = []
    _last_rb_watchlist  = []
    _last_m1m7_count = len(candidates)
    log.info(
        f"Stage 1: {len(candidates)} M1–M7, {len(gc_candidates)} GC, "
        f"{len(vs_candidates)} VS, {len(rb_candidates)} RB, "
        f"{len(pbw_candidates)} PBW, {len(sc_candidates)} SC candidates."
    )

    # ── Stages 2 + 3: TA gate + full scoring ─────────────────────────────────
    scored:              list[MomentumResult] = []
    cooling_alerts:      list[MomentumResult] = []
    macro_blocked_count: int = 0

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

        # Dynamic vol threshold: fast move (>5%) vs slow trend (2-5%)
        vol_threshold = (cfg.MOMENTUM_VOL_FAST_MIN if change_1h > cfg.MOMENTUM_EARLY_1H_MAX
                         else cfg.MOMENTUM_VOL_SLOW_MIN)

        tech = _check_technicals(mexc_symbol, vol_threshold)

        if tech is None:
            log.warning(f"  TA skip  {symbol}: futures kline data unavailable")
            _last_scan_outcomes.append(CandidateOutcome(symbol, change_1h, 0, "NO_DATA", "MEXC kline unavailable", 0.0, 0.0))
            continue

        # FIX 4: macro gate = EMA stack only (KDJ no longer hard-blocks)
        if not tech.macro_ok:
            macro_blocked_count += 1
            log.info(
                f"  MACRO ✗  {symbol}: EMA bearish  "
                f"({tech.h4_ema6:.4g}/{tech.h4_ema12:.4g}/{tech.h4_ema20:.4g})"
            )
            _last_scan_outcomes.append(CandidateOutcome(symbol, change_1h, 0, "MACRO_BLOCKED", "4H EMA bearish", tech.vol_pct, tech.h4_kdj_j))
            continue

        # GATE 2a: 15m EMA6 > EMA12 (new hard gate)
        if not tech.m15_ema6_gt_ema12:
            macro_blocked_count += 1
            log.info(f"  15m EMA ✗  {symbol}: EMA6 {tech.m15_ema6:.4g} < EMA12 {tech.m15_ema12:.4g}")
            _last_scan_outcomes.append(CandidateOutcome(symbol, change_1h, 0, "MACRO_BLOCKED", "15m EMA6 < EMA12", tech.vol_pct, tech.h4_kdj_j))
            continue

        # FIX 4: dead-zone blocker — very low volume AND low momentum = skip
        if (tech.vol_pct < cfg.MOMENTUM_DEAD_VOL_PCT * 100 and
                change_1h < cfg.MOMENTUM_DEAD_1H_MAX):
            log.info(
                f"  DEAD ZONE {symbol}: vol {tech.vol_pct:.0f}% + 1H {change_1h:+.1f}% "
                f"— no momentum"
            )
            _last_scan_outcomes.append(CandidateOutcome(symbol, change_1h, 0, "DEAD_ZONE", f"Vol {tech.vol_pct:.0f}% of MA10 + only {change_1h:.1f}% 1H", tech.vol_pct, tech.h4_kdj_j))
            continue

        # 5m soft gate — fetch and populate tech.m5_* (never blocks)
        _apply_5m_to_tech(tech, _check_5m(mexc_symbol))

        # Fundamental bonus
        fund = _score_fundamentals(change_1h, mcap, circ_pct, fdv_ratio)

        # ATH distance bonus (FIX 4)
        if tech.ath_dist_pct > cfg.MOMENTUM_ATH_DIST_L2:
            ath_pts = cfg.MOMENTUM_ATH_DIST_L2_PTS   # > 90% below 16D peak → +5
        elif tech.ath_dist_pct > cfg.MOMENTUM_ATH_DIST_L1:
            ath_pts = cfg.MOMENTUM_ATH_DIST_L1_PTS   # > 80% below 16D peak → +3
        else:
            ath_pts = 0

        # Total score
        total = tech.score + fund.total + ath_pts

        # Recommendation classification
        if total >= cfg.MOMENTUM_TOTAL_STRONG_ENTRY:
            recommendation = "STRONG ENTRY"
            rec_emoji      = "🟢"
        elif total >= cfg.MOMENTUM_TOTAL_WATCH:
            recommendation = "WATCH"
            rec_emoji      = "🟡"
        else:
            # Promote MONITOR-level coins that show early-move characteristics
            is_early = (
                total >= cfg.MOMENTUM_TOTAL_MONITOR and
                change_1h < cfg.MOMENTUM_EARLY_1H_MAX and
                cfg.MOMENTUM_EARLY_15M_MIN <= tech.m15_change <= cfg.MOMENTUM_EARLY_15M_MAX and
                tech.vol_pct >= cfg.MOMENTUM_VOL_SPIKE_MIN * 100
            )
            if is_early:
                recommendation = "EARLY SIGNAL"
                rec_emoji      = "🔍"
            elif total >= cfg.MOMENTUM_TOTAL_MONITOR:
                log.info(f"  MONITOR  {symbol}: {total}/100 — logged only, no alert")
                _last_scan_outcomes.append(CandidateOutcome(symbol, change_1h, total, "MONITOR", f"Score {total}/100 — below alert threshold ({cfg.MOMENTUM_TOTAL_WATCH})", tech.vol_pct, tech.h4_kdj_j))
                continue
            else:
                log.debug(f"  SKIP     {symbol}: {total}/100 < {cfg.MOMENTUM_TOTAL_MONITOR}")
                _last_scan_outcomes.append(CandidateOutcome(symbol, change_1h, total, "BELOW_THRESHOLD", f"Score {total}/100 — too low", tech.vol_pct, tech.h4_kdj_j))
                continue

        # 5m soft gate: downgrade STRONG → WATCH if overheated
        if recommendation == "STRONG ENTRY" and tech.m5_note.startswith("⏳"):
            recommendation = "WATCH"
            rec_emoji      = "🟡"

        # SL parameters — tighter for EARLY SIGNAL (−4% vs standard −6%)
        if recommendation == "EARLY SIGNAL":
            coin_sl_factor = 1.0 - cfg.MOMENTUM_EARLY_SL_PCT / 100.0
            coin_risk_usd  = round(cfg.MOMENTUM_POSITION_USD * cfg.MOMENTUM_EARLY_SL_PCT / 100.0, 2)
            coin_rr_str    = f"1:{_rwd_tp1 / coin_risk_usd:.2f}"
            coin_sl_pct    = cfg.MOMENTUM_EARLY_SL_PCT
        else:
            coin_sl_factor = _sl_factor
            coin_risk_usd  = _risk_usd
            coin_rr_str    = _rr_str
            coin_sl_pct    = cfg.MOMENTUM_SL_PCT

        # Risk warnings
        warnings = _generate_warnings(tech, change_1h, circ_pct, fdv_ratio,
                                       _inf_supply_map.get(symbol, False),
                                       is_micro_cap=(symbol in _micro_cap_set),
                                       mcap=mcap)

        early_note = f"  [15m {tech.m15_change:+.1f}%]" if recommendation == "EARLY SIGNAL" else ""
        log.info(
            f"  {rec_emoji} {recommendation}  {symbol}  {total}/100{early_note}  "
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
            ath_dist_pct    = round(tech.ath_dist_pct, 1),
            m5_note         = tech.m5_note,
        ))

    # Sort best-first
    scored.sort(key=lambda r: r.total_score, reverse=True)

    # Cooldown deduplication + cap at max alerts per scan
    new_alerts: list[MomentumResult] = []
    for r in scored:
        if _on_cooldown(r.symbol):
            log.debug(f"  COOLDOWN: {r.symbol}")
            continue
        if len(new_alerts) >= cfg.MOMENTUM_MAX_ALERTS_PER_SCAN:
            log.debug(f"  CAP: {r.symbol} (max {cfg.MOMENTUM_MAX_ALERTS_PER_SCAN} per scan)")
            break
        _mark_alerted(r.symbol)
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

    for entry in gc_candidates:
        (symbol, name, matched_tags, mexc_symbol,
         price, change_1h, change_24h, mcap, vol_24h, fdv, fdv_ratio, circ_pct) = entry

        if symbol in alerted_this_scan:
            continue  # main pipeline already alerted this coin this scan
        if _on_gc_cooldown(symbol):
            log.debug(f"  GC COOLDOWN: {symbol}")
            continue

        # Graduated vol threshold: 0.5-0.8% uses GC floor, 0.8-5% uses early-detection floor
        gc_vol_threshold = (cfg.MOMENTUM_VOL_GC_MIN if change_1h < cfg.MOMENTUM_GC_EARLY_1H_MIN
                            else cfg.MOMENTUM_VOL_EARLY_MIN)
        tech = _check_technicals(mexc_symbol, gc_vol_threshold)
        if tech is None:
            continue

        if not tech.m15_golden_cross:
            continue
        if not tech.h4_ema_ok:
            log.debug(f"  GC skip {symbol}: 4H EMA bearish")
            continue
        if tech.m15_rsi6 >= cfg.MOMENTUM_GC_RSI_MAX:
            log.debug(f"  GC skip {symbol}: RSI {tech.m15_rsi6:.1f} >= {cfg.MOMENTUM_GC_RSI_MAX}")
            continue
        if not tech.vol_ok:
            log.debug(f"  GC skip {symbol}: vol {tech.vol_pct:.0f}% < {gc_vol_threshold*100:.0f}%")
            continue

        # 5m soft gate for GC
        _apply_5m_to_tech(tech, _check_5m(mexc_symbol))
        gc_5m_warn = []
        if tech.m5_rsi6 > 0:
            if not (40 <= tech.m5_rsi6 <= 72):
                gc_5m_warn.append(f"⚠️ 5m noch nicht ideal — Entry-Zone abwarten oder kleinere Position")
            if not tech.m5_price_above_ema20:
                gc_5m_warn.append("⚠️ 5m noch nicht ideal — Entry-Zone abwarten oder kleinere Position")

        log.info(
            f"  ⚡ GOLDEN CROSS  {symbol}  1h {change_1h:+.2f}%  "
            f"RSI {tech.m15_rsi6:.1f}  Vol {tech.vol_pct:.0f}%"
        )
        _mark_gc_alerted(symbol)
        _last_scan_outcomes.append(CandidateOutcome(symbol, change_1h, 0, "GC", "GOLDEN CROSS", tech.vol_pct, tech.h4_kdj_j))

        gc_sl_factor = 1.0 - cfg.MOMENTUM_GC_SL_PCT / 100.0
        gc_risk_usd  = round(cfg.MOMENTUM_POSITION_USD * cfg.MOMENTUM_GC_SL_PCT / 100.0, 2)
        gc_rr_str    = f"1:{_rwd_tp1 / gc_risk_usd:.2f}"

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
        ))

    # ── Stage 2c: Volume Spike pipeline ──────────────────────────────────────
    vs_alerts: list[MomentumResult] = []
    alerted_symbols = {r.symbol for r in new_alerts + gc_alerts}

    for entry in vs_candidates:
        (symbol, name, matched_tags, mexc_symbol,
         price, change_1h, change_24h, mcap, vol_24h, fdv, fdv_ratio, circ_pct) = entry

        if symbol in alerted_symbols:
            continue
        if _on_vs_cooldown(symbol):
            log.debug(f"  VS COOLDOWN: {symbol}")
            continue

        tech = _check_technicals(mexc_symbol, cfg.MOMENTUM_VOL_GC_MIN)
        if tech is None:
            continue
        if not tech.m15_vol_spike:
            log.debug(f"  VS skip {symbol}: vol ratio {tech.m15_vol_spike_ratio:.1f}× < {cfg.MOMENTUM_VS_VOL_MULT}×")
            continue
        if not tech.h4_ema_ok:
            log.debug(f"  VS skip {symbol}: 4H EMA bearish")
            continue
        if tech.m15_rsi6 >= cfg.MOMENTUM_VS_RSI_MAX:
            log.debug(f"  VS skip {symbol}: RSI {tech.m15_rsi6:.1f} ≥ {cfg.MOMENTUM_VS_RSI_MAX}")
            continue

        # 5m soft gate for VS
        _apply_5m_to_tech(tech, _check_5m(mexc_symbol))
        vs_5m_warn = []
        if tech.m5_rsi6 > 0:
            if not tech.m5_kdj_rising:
                vs_5m_warn.append("⚠️ 5m noch nicht ideal — Entry-Zone abwarten oder kleinere Position")
            if tech.m5_rsi6 >= 75:
                vs_5m_warn.append(f"⚠️ 5m noch nicht ideal — Entry-Zone abwarten oder kleinere Position")

        log.info(
            f"  ⚡ VOL SPIKE  {symbol}  1h {change_1h:+.2f}%  "
            f"vol {tech.m15_vol_spike_ratio:.1f}×  RSI {tech.m15_rsi6:.1f}"
        )
        _mark_vs_alerted(symbol)
        _last_scan_outcomes.append(CandidateOutcome(symbol, change_1h, 0, "VS", "VOLUME SPIKE", tech.vol_pct, tech.h4_kdj_j))

        vs_sl_factor = 1.0 - cfg.MOMENTUM_VS_SL_PCT / 100.0
        vs_risk_usd  = round(cfg.MOMENTUM_POSITION_USD * cfg.MOMENTUM_VS_SL_PCT / 100.0, 2)

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
        ))

    # ── Stage 2d: Recovery Bounce pipeline ───────────────────────────────────
    rb_alerts: list[MomentumResult] = []
    alerted_symbols = {r.symbol for r in new_alerts + gc_alerts + vs_alerts}

    for entry in rb_candidates:
        (symbol, name, matched_tags, mexc_symbol,
         price, change_1h, change_24h, mcap, vol_24h, fdv, fdv_ratio, circ_pct) = entry

        if symbol in alerted_symbols:
            continue
        if _on_rb_cooldown(symbol):
            log.debug(f"  RB COOLDOWN: {symbol}")
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

        # Track near-miss for /recovery command (peak confirmed, may still fail pullback/KDJ)
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
        if not tech.h4_ema_ok:
            log.debug(f"  RB skip {symbol}: 4H EMA bearish")
            continue
        if tech.h4_kdj_j >= cfg.MOMENTUM_RB_KDJ_MAX:
            log.debug(f"  RB skip {symbol}: 4H KDJ {tech.h4_kdj_j:.1f} ≥ {cfg.MOMENTUM_RB_KDJ_MAX} (not cooled)")
            continue

        # 5m soft gate for RB + 15m EMA6>EMA12 check
        _apply_5m_to_tech(tech, _check_5m(mexc_symbol))
        rb_5m_warn = []
        if not tech.m15_ema6_gt_ema12:
            rb_5m_warn.append("⚠️ 5m noch nicht ideal — Entry-Zone abwarten oder kleinere Position")
        if tech.m5_rsi6 > 0 and not tech.m5_first_green:
            rb_5m_warn.append("⚠️ 5m noch nicht ideal — Entry-Zone abwarten oder kleinere Position")

        pullback_shown = (h24_high - current) / h24_high * 100
        log.info(
            f"  ♻️ RECOVERY  {symbol}  1h {change_1h:+.2f}%  "
            f"peak {h24_high:.4g}  pullback {pullback_shown:.1f}%  KDJ {tech.h4_kdj_j:.1f}"
        )
        _mark_rb_alerted(symbol)
        _last_scan_outcomes.append(CandidateOutcome(symbol, change_1h, 0, "RB", "RECOVERY", tech.vol_pct, tech.h4_kdj_j))

        rb_sl_factor  = 1.0 - cfg.MOMENTUM_RB_SL_PCT / 100.0
        rb_tp1_factor = 1.0 + cfg.MOMENTUM_RB_TP1_PCT / 100.0
        rb_tp2_factor = 1.0 + cfg.MOMENTUM_RB_TP2_PCT / 100.0
        rb_risk_usd   = round(cfg.MOMENTUM_POSITION_USD * cfg.MOMENTUM_RB_SL_PCT / 100.0, 2)
        rb_rwd_tp1    = round(cfg.MOMENTUM_POSITION_USD * cfg.MOMENTUM_RB_TP1_PCT / 100.0, 2)
        rb_rwd_tp2    = round(cfg.MOMENTUM_POSITION_USD * cfg.MOMENTUM_RB_TP2_PCT / 100.0, 2)

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
        ))

    # ── Stage 2e: Pre-Breakout Watch pipeline ────────────────────────────────
    pbw_alerts: list[MomentumResult] = []
    alerted_symbols = {r.symbol for r in new_alerts + cooling_alerts + gc_alerts + vs_alerts + rb_alerts}

    for entry in pbw_candidates:
        (symbol, name, matched_tags, mexc_symbol,
         price, change_1h, change_24h, mcap, vol_24h, fdv, fdv_ratio, circ_pct) = entry

        if symbol in alerted_symbols:
            continue
        if _on_pbw_cooldown(symbol):
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

        # ATH distance from 4H data
        h16d_high    = float(df_4h["high"].max())
        ath_dist_pct = (h16d_high - price) / h16d_high * 100 if h16d_high > 0 else 0.0

        pbw = _check_1m_pbw(mexc_symbol)
        if pbw is None:
            continue
        if not pbw["compressed"]:
            log.debug(f"  PBW skip {symbol}: EMA spread {pbw['ema_spread']:.3f}% ≥ {cfg.MOMENTUM_PBW_EMA_SPREAD_MAX}%")
            continue
        if not pbw["rsi_ok"]:
            log.debug(f"  PBW skip {symbol}: RSI streak {pbw['rsi_streak']} < {cfg.MOMENTUM_PBW_RSI_CANDLES} candles")
            continue
        if not (pbw["trigger_vol"] and pbw["trigger_px"]):
            log.debug(f"  PBW skip {symbol}: trigger not met (vol {pbw['vol_ratio']:.1f}×, px_above_ema={pbw['trigger_px']})")
            continue

        fund     = _score_fundamentals(change_1h, mcap, circ_pct, fdv_ratio)
        has_inf  = _inf_supply_map.get(symbol, False)
        warnings = []
        if symbol in _micro_cap_set:
            warnings.append(f"⚠️ Micro-Cap: ${mcap/1e6:.0f}M — half position size")
            warnings.append("⚠️ Treat as high-risk, max 50% normal margin")
        if has_inf:
            warnings.append("⚠️ Max Supply unknown — inflation risk")
        if circ_pct > 0 and circ_pct < cfg.MOMENTUM_WARN_CIRC_ALERT_PCT:
            warnings.append(f"Circ Rate: {circ_pct:.0f}% — Dilution-Risiko")
        if fdv_ratio > cfg.MOMENTUM_WARN_FDV_ALERT_RATIO:
            warnings.append(f"FDV/MCap: {fdv_ratio:.1f}×")

        # 5m soft gate for PBW
        m5_pbw = _check_5m(mexc_symbol)
        pbw_m5_note = ""
        if m5_pbw is not None:
            if m5_pbw["kdj_j"] >= cfg.MOMENTUM_5M_KDJ_MAX_PBW or m5_pbw["rsi6"] >= cfg.MOMENTUM_5M_RSI_MAX_PBW:
                pbw_m5_note = "⚠️ 5m noch nicht ideal — Entry-Zone abwarten oder kleinere Position"
                warnings.append(pbw_m5_note)

        log.info(
            f"  🔍 PRE-BREAKOUT  {symbol}  1h {change_1h:+.2f}%  "
            f"EMA {pbw['ema_spread']:.3f}%  RSI streak {pbw['rsi_streak']}  "
            f"Vol {pbw['vol_ratio']:.1f}×"
        )
        _mark_pbw_alerted(symbol)
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
        ))

    # ── Stage 2f: Staircase Continuation pipeline ─────────────────────────────
    sc_alerts: list[MomentumResult] = []
    alerted_symbols = {r.symbol for r in new_alerts + cooling_alerts + gc_alerts + vs_alerts + rb_alerts + pbw_alerts}

    for entry in sc_candidates:
        (symbol, name, matched_tags, mexc_symbol,
         price, change_1h, change_24h, mcap, vol_24h, fdv, fdv_ratio, circ_pct) = entry

        if symbol in alerted_symbols:
            continue
        if _on_sc_cooldown(symbol):
            continue

        tech = _check_technicals(mexc_symbol, cfg.MOMENTUM_VOL_SLOW_MIN)
        if tech is None:
            continue
        if not tech.h4_ema_ok:
            log.debug(f"  SC skip {symbol}: 4H EMA bearish")
            continue

        # Consolidation checks
        if tech.vol_pct >= cfg.MOMENTUM_SC_VOL_MAX * 100:
            log.debug(f"  SC skip {symbol}: vol {tech.vol_pct:.0f}% ≥ {cfg.MOMENTUM_SC_VOL_MAX*100:.0f}% (not low enough)")
            continue
        if tech.m15_rsi6 >= cfg.MOMENTUM_SC_RSI_MAX:
            log.debug(f"  SC skip {symbol}: RSI {tech.m15_rsi6:.1f} ≥ {cfg.MOMENTUM_SC_RSI_MAX} (not cooled)")
            continue
        if tech.m15_kdj_j >= cfg.MOMENTUM_SC_KDJ_MAX:
            log.debug(f"  SC skip {symbol}: KDJ J {tech.m15_kdj_j:.1f} ≥ {cfg.MOMENTUM_SC_KDJ_MAX} (not reset)")
            continue

        # Prior move: 24H high must be ≥8% above current price (confirms previous leg)
        current       = tech.m15_price
        prior_move_ok = tech.h24_high >= current * (1.0 + cfg.MOMENTUM_SC_PRIOR_MOVE_MIN / 100.0)
        prior_move_pct = (tech.h24_high - current) / current * 100 if current > 0 else 0.0

        if not prior_move_ok:
            log.debug(f"  SC skip {symbol}: 24H high only {prior_move_pct:.1f}% above current (need {cfg.MOMENTUM_SC_PRIOR_MOVE_MIN}%)")
            continue

        fund    = _score_fundamentals(change_1h, mcap, circ_pct, fdv_ratio)
        has_inf = _inf_supply_map.get(symbol, False)
        warnings = []
        if symbol in _micro_cap_set:
            warnings.append(f"⚠️ Micro-Cap: ${mcap/1e6:.0f}M — half position size")
            warnings.append("⚠️ Treat as high-risk, max 50% normal margin")
        if has_inf:
            warnings.append("⚠️ Max Supply unknown — inflation risk")
        if circ_pct > 0 and circ_pct < cfg.MOMENTUM_WARN_CIRC_ALERT_PCT:
            warnings.append(f"Circ Rate: {circ_pct:.0f}% — Dilution-Risiko")
        if fdv_ratio > cfg.MOMENTUM_WARN_FDV_ALERT_RATIO:
            warnings.append(f"FDV/MCap: {fdv_ratio:.1f}×")

        # 5m soft gate for SC
        _apply_5m_to_tech(tech, _check_5m(mexc_symbol))
        if tech.m5_rsi6 > 0:
            if tech.m5_vol_pct >= cfg.MOMENTUM_5M_VOL_MAX_SC * 100:
                warnings.append("⚠️ 5m noch nicht ideal — Entry-Zone abwarten oder kleinere Position")
            if tech.m5_kdj_j >= cfg.MOMENTUM_5M_KDJ_MAX_SC:
                warnings.append("⚠️ 5m noch nicht ideal — Entry-Zone abwarten oder kleinere Position")

        log.info(
            f"  🪜 STAIRCASE  {symbol}  1h {change_1h:+.2f}%  "
            f"Vol {tech.vol_pct:.0f}%  RSI {tech.m15_rsi6:.1f}  KDJ {tech.m15_kdj_j:.1f}  "
            f"prior +{prior_move_pct:.1f}%"
        )
        _mark_sc_alerted(symbol)
        _last_scan_outcomes.append(CandidateOutcome(symbol, change_1h, fund.total, "SC", "STAIRCASE", tech.vol_pct, tech.h4_kdj_j))

        sl_f   = 1.0 - cfg.MOMENTUM_SC_SL_PCT  / 100.0
        tp1_f  = 1.0 + cfg.MOMENTUM_SC_TP1_PCT / 100.0
        tp2_f  = 1.0 + cfg.MOMENTUM_SC_TP2_PCT / 100.0
        risk   = round(cfg.MOMENTUM_POSITION_USD * cfg.MOMENTUM_SC_SL_PCT  / 100.0, 2)
        rwd1   = round(cfg.MOMENTUM_POSITION_USD * cfg.MOMENTUM_SC_TP1_PCT / 100.0, 2)
        rwd2   = round(cfg.MOMENTUM_POSITION_USD * cfg.MOMENTUM_SC_TP2_PCT / 100.0, 2)

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
            ath_dist_pct=round(tech.ath_dist_pct, 1),
        ))

    strong  = sum(r.recommendation == "STRONG ENTRY" for r in new_alerts)
    watch   = sum(r.recommendation == "WATCH"        for r in new_alerts)
    early   = sum(r.recommendation == "EARLY SIGNAL" for r in new_alerts)
    gc      = len(gc_alerts)
    vs      = len(vs_alerts)
    rb      = len(rb_alerts)
    pbw     = len(pbw_alerts)
    sc_n    = len(sc_alerts)
    cooling = len(cooling_alerts)
    log.info(
        f"Scan done — {len(candidates)} M1–M7, "
        f"{macro_blocked_count} macro-blocked, "
        f"{len(scored)} scored ≥{cfg.MOMENTUM_TOTAL_WATCH}, "
        f"{strong} STRONG + {watch} WATCH + {early} EARLY + "
        f"{gc} GC + {vs} VS + {rb} RB + {pbw} PBW + {sc_n} SC + {cooling} COOLING alerts."
    )
    return new_alerts + cooling_alerts + gc_alerts + vs_alerts + rb_alerts + pbw_alerts + sc_alerts


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
