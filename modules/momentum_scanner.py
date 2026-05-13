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

    # Technical score (0-60)
    score: int


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

    # Trade levels (fixed-% framework, $100 position)
    entry_price:    float = 0.0
    stop_loss:      float = 0.0        # entry × (1 − SL_PCT/100)
    tp1:            float = 0.0        # entry × (1 + TP1_PCT/100)
    tp2:            float = 0.0        # entry × (1 + TP2_PCT/100)
    risk_usd:       float = 0.0        # POSITION_USD × SL_PCT/100
    reward_tp1_usd: float = 0.0        # POSITION_USD × TP1_PCT/100
    reward_tp2_usd: float = 0.0        # POSITION_USD × TP2_PCT/100
    rr_str:         str   = "1:1.67"   # reward_tp1 / risk


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

# Separate cooldown for cooling alerts so they never block entry alerts
_cooling_alerted: dict[str, float] = {}


def _on_cooling_cooldown(symbol: str) -> bool:
    return (time.time() - _cooling_alerted.get(symbol, 0.0)) < cfg.MOMENTUM_ALERT_COOLDOWN_MIN * 60


def _mark_cooling_alerted(symbol: str) -> None:
    _cooling_alerted[symbol] = time.time()
    stale = time.time() - 86_400
    for k in [k for k, ts in _cooling_alerted.items() if ts < stale]:
        del _cooling_alerted[k]


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
    }
    for t in tags:
        if t in _MAP:
            return _MAP[t]
    return tags[0] if tags else "Unknown"


# ══════════════════════════════════════════════════════════════════════════════
# Stage 2 + 3a: Technical Analysis
# ══════════════════════════════════════════════════════════════════════════════

def _check_technicals(mexc_symbol: str) -> TechResult | None:
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
    h4_ema_ok = h4_ema6 > h4_ema12 > h4_ema20

    _, _, j4h = compute_kdj(df_4h)
    h4_kdj_j  = float(j4h.iloc[-1])
    h4_kdj_ok = h4_kdj_j < cfg.MOMENTUM_TA_H4_KDJ_J_MAX

    macro_ok = h4_ema_ok and h4_kdj_ok

    # ── 4H Volume ────────────────────────────────────────────────────────────
    vol_last = float(df_4h["volume"].iloc[-1])
    vol_ma10 = float(df_4h["volume"].rolling(10).mean().iloc[-1])
    vol_pct  = (vol_last / vol_ma10 * 100.0) if vol_ma10 > 0 else 0.0
    vol_ok   = vol_pct >= cfg.MOMENTUM_TA_VOL_RATIO_MIN * 100
    vol_pts  = _PTS_VOL if vol_ok else 0

    # ── 15m Scoring ──────────────────────────────────────────────────────────
    close_15m = df_15m["close"]

    m15_ema6  = float(compute_ema(close_15m,  6).iloc[-1])
    m15_ema20 = float(compute_ema(close_15m, 20).iloc[-1])
    m15_ema_ok  = m15_ema6 > m15_ema20
    m15_ema_pts = _PTS_EMA if m15_ema_ok else 0

    m15_price     = float(close_15m.iloc[-1])
    m15_price_ok  = m15_price > m15_ema20
    m15_price_pts = _PTS_PRICE if m15_price_ok else 0

    m15_rsi6     = float(compute_rsi(close_15m, period=6).iloc[-1])
    m15_rsi6_ok  = cfg.MOMENTUM_TA_15M_RSI6_MIN <= m15_rsi6 <= cfg.MOMENTUM_TA_15M_RSI6_MAX
    m15_rsi6_hot = m15_rsi6 > cfg.MOMENTUM_TA_15M_RSI6_MAX
    m15_rsi6_pts = _PTS_RSI6 if m15_rsi6_ok else 0

    _, _, j15   = compute_kdj(df_15m)
    m15_kdj_j   = float(j15.iloc[-1])
    m15_kdj_ok  = m15_kdj_j < cfg.MOMENTUM_TA_15M_KDJ_J_MAX
    m15_kdj_hot = m15_kdj_j >= cfg.MOMENTUM_TA_15M_KDJ_J_MAX
    m15_kdj_pts = _PTS_KDJ if m15_kdj_ok else 0

    dif_s, dea_s, _ = compute_macd(close_15m)
    m15_macd_dif = float(dif_s.iloc[-1])
    m15_macd_dea = float(dea_s.iloc[-1])
    m15_macd_ok  = m15_macd_dif > m15_macd_dea
    m15_macd_pts = _PTS_MACD if m15_macd_ok else 0

    score = (m15_ema_pts + m15_price_pts + m15_rsi6_pts +
             m15_kdj_pts + m15_macd_pts + vol_pts)

    return TechResult(
        h4_ema6=round(h4_ema6, 8),    h4_ema12=round(h4_ema12, 8),  h4_ema20=round(h4_ema20, 8),
        h4_ema_ok=h4_ema_ok,
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
        score=score,
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
                        circ_pct: float, fdv_ratio: float) -> list[str]:
    """Return up to 3 auto-generated risk warning strings (highest priority first)."""
    warns: list[str] = []

    if tech.m15_rsi6 > cfg.MOMENTUM_WARN_RSI6_PCT:
        warns.append("RSI approaching overbought")
    if tech.m15_kdj_j > cfg.MOMENTUM_WARN_KDJ_J_PCT:
        warns.append("KDJ getting hot")
    if change_1h > cfg.MOMENTUM_WARN_GAIN_LATE_PCT:
        warns.append("Late in momentum cycle")
    if fdv_ratio > cfg.MOMENTUM_WARN_FDV_HIGH_RATIO:
        warns.append("High dilution risk")
    if circ_pct < cfg.MOMENTUM_WARN_CIRC_LOW_PCT:
        warns.append("Dilution risk present")
    if tech.vol_pct < cfg.MOMENTUM_WARN_VOL_LOW_PCT:
        warns.append("Volume not exceptional")

    return warns[:3]


# ══════════════════════════════════════════════════════════════════════════════
# Main scan
# ══════════════════════════════════════════════════════════════════════════════

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
    candidates = []

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

        if change_1h < cfg.MOMENTUM_EARLY_EXIT_PCT:
            break   # CMC sorted desc — no more Zone 2 coins below this
        if change_1h < cfg.MOMENTUM_1H_CHANGE_MIN_PCT:
            continue   # 2.5–3.0% buffer zone: below M1 minimum, keep scanning
        if change_1h > cfg.MOMENTUM_1H_CHANGE_MAX_PCT:
            continue

        if not (cfg.MOMENTUM_MCAP_MIN_USD <= mcap <= cfg.MOMENTUM_MCAP_MAX_USD):
            continue
        if vol_24h < cfg.MOMENTUM_VOL_24H_MIN_USD:
            continue

        circ_pct = _resolve_circ_pct(coin)
        if circ_pct < cfg.MOMENTUM_CIRC_SUPPLY_MIN_PCT:
            continue

        fdv = _resolve_fdv(coin, price)
        if fdv <= 0 or mcap <= 0:
            continue
        fdv_ratio = fdv / mcap
        if fdv_ratio > cfg.MOMENTUM_FDV_MCAP_MAX_RATIO:
            continue

        matched_tags = [t for t in tags if t in cfg.MOMENTUM_ALLOWED_TAGS]
        if not matched_tags:
            continue

        mexc_symbol = f"{symbol}_USDT"
        if mexc_symbol not in futures_set:
            continue

        log.info(
            f"  M1–M7 ✓  {symbol:<10}  1h {change_1h:+.2f}%  "
            f"MCap ${mcap/1e6:.0f}M  Vol ${vol_24h/1e6:.0f}M  "
            f"FDV/MC {fdv_ratio:.1f}×  [{matched_tags[0]}]"
        )
        candidates.append((
            symbol, name, matched_tags, mexc_symbol,
            price, change_1h, change_24h, mcap, vol_24h, fdv, fdv_ratio, circ_pct,
        ))

    global _last_m1m7_count, _last_macro_blocked
    _last_m1m7_count = len(candidates)
    log.info(f"Stage 1: {len(candidates)} passed M1–M7.")

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

        tech = _check_technicals(mexc_symbol)

        if tech is None:
            log.warning(f"  TA skip  {symbol}: futures kline data unavailable")
            continue

        if not tech.macro_ok:
            macro_blocked_count += 1
            reasons = []
            if not tech.h4_ema_ok:
                reasons.append(
                    f"EMA bearish  ({tech.h4_ema6:.4g} < {tech.h4_ema12:.4g} "
                    f"or {tech.h4_ema12:.4g} < {tech.h4_ema20:.4g})"
                )
            if not tech.h4_kdj_ok:
                reasons.append(f"4H KDJ J {tech.h4_kdj_j:.1f} ≥ {cfg.MOMENTUM_TA_H4_KDJ_J_MAX:.0f}")
            log.info(f"  MACRO ✗  {symbol}: {', '.join(reasons)}")

            # Cooling alert: trend is bullish (EMA stack OK) but KDJ is overheated
            if tech.h4_ema_ok and not tech.h4_kdj_ok:
                if not _on_cooling_cooldown(symbol):
                    log.info(f"  ⏳ COOLING  {symbol}: KDJ J {tech.h4_kdj_j:.1f} — queuing alert")
                    cooling_alerts.append(MomentumResult(
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
                        recommendation  = "COOLING_DOWN",
                        rec_emoji       = "⏳",
                    ))
                    _mark_cooling_alerted(symbol)
            continue

        # Fundamental bonus
        fund = _score_fundamentals(change_1h, mcap, circ_pct, fdv_ratio)

        # Total score
        total = tech.score + fund.total

        # Recommendation classification
        if total >= cfg.MOMENTUM_TOTAL_STRONG_ENTRY:
            recommendation = "STRONG ENTRY"
            rec_emoji      = "🟢"
        elif total >= cfg.MOMENTUM_TOTAL_WATCH:
            recommendation = "WATCH"
            rec_emoji      = "🟡"
        elif total >= cfg.MOMENTUM_TOTAL_MONITOR:
            log.info(f"  MONITOR  {symbol}: {total}/100 — logged only, no alert")
            continue
        else:
            log.debug(f"  SKIP     {symbol}: {total}/100 < {cfg.MOMENTUM_TOTAL_MONITOR}")
            continue

        # Risk warnings
        warnings = _generate_warnings(tech, change_1h, circ_pct, fdv_ratio)

        log.info(
            f"  {rec_emoji} {recommendation}  {symbol}  {total}/100  "
            f"[tech {tech.score}+fund {fund.total}]  "
            f"[ema {tech.m15_ema_pts}+px {tech.m15_price_pts}+"
            f"rsi {tech.m15_rsi6_pts}+kdj {tech.m15_kdj_pts}+"
            f"macd {tech.m15_macd_pts}+vol {tech.vol_pts}]"
        )

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
            entry_price     = round(price, 8),
            stop_loss       = round(price * _sl_factor, 8),
            tp1             = round(price * _tp1_factor, 8),
            tp2             = round(price * _tp2_factor, 8),
            risk_usd        = _risk_usd,
            reward_tp1_usd  = _rwd_tp1,
            reward_tp2_usd  = _rwd_tp2,
            rr_str          = _rr_str,
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

    strong  = sum(r.recommendation == "STRONG ENTRY" for r in new_alerts)
    watch   = sum(r.recommendation == "WATCH"        for r in new_alerts)
    cooling = len(cooling_alerts)
    log.info(
        f"Scan done — {len(candidates)} M1–M7, "
        f"{macro_blocked_count} macro-blocked, "
        f"{len(scored)} scored ≥{cfg.MOMENTUM_TOTAL_WATCH}, "
        f"{strong} STRONG ENTRY + {watch} WATCH + {cooling} COOLING alerts."
    )
    return new_alerts + cooling_alerts


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
        f"{t.m15_kdj_j:.1f}  [<75]{kdj_flag}   +{t.m15_kdj_pts} pts",
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
