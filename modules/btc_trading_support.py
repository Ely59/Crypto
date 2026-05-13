"""
Module 3: BTC Trading Support
──────────────────────────────
Analyzes BTC on the 4-hour chart to find high-probability leverage trade setups.

A LONG setup requires all of:
  • RSI ≥ BTC_LEVERAGE_RSI_LONG
  • Price above 20 EMA (short-term trend up)
  • MACD line crossed above signal line (momentum shift)
  • Price bounced off lower Bollinger Band (oversold reversal)

A SHORT setup requires all of:
  • RSI ≤ BTC_LEVERAGE_RSI_SHORT
  • Price below 20 EMA
  • MACD line crossed below signal line
  • Price rejected at upper Bollinger Band

Stop-Loss = entry ± ATR × BTC_ATR_MULTIPLIER_SL
TP1       = entry ± ATR × BTC_ATR_MULTIPLIER_TP1    (conservative — take 40%)
TP2       = entry ± ATR × BTC_ATR_MULTIPLIER_TP2    (main target  — take 40%)
Runner    = entry ± ATR × BTC_ATR_MULTIPLIER_RUNNER (let 20% ride)
"""

# ── Path shim (allows running this file directly) ────────────────────────────
import sys as _sys, os as _os
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
# ─────────────────────────────────────────────────────────────────────────────

from dataclasses import dataclass, field
from typing import Literal, Optional
from utils.api_client  import (
    get_binance_klines, get_mexc_klines,
    get_btc_funding_rate, get_btc_long_short_ratio, get_btc_open_interest,
)
from utils.indicators  import compute_rsi, compute_atr, compute_ema, compute_macd, compute_bollinger_bands
from utils.logger      import get_logger
import config as cfg

log = get_logger(__name__)


@dataclass
class TradeSetup:
    direction:   Literal["LONG", "SHORT", "NO_SETUP"]
    entry_price: float
    stop_loss:   float
    tp1:         float   # conservative target — take 40% here
    tp2:         float   # main target        — take 40% here
    runner:      float   # let 20% ride to this level
    rsi:                    float
    atr:                    float
    r_r_tp2:                float    # reward/risk to TP2
    max_loss_usdt:          float    # max loss in USDT (50× leverage, 10 USDT margin, at SL)
    reasons:                list[str]

    # Market structure signals (computed from Binance Futures public API)
    market_structure_score: int            = 0     # -1 … +3; higher = more conviction
    ms_funding_rate:        float | None   = None
    ms_long_short_ratio:    float | None   = None
    ms_oi_value:            float | None   = None
    ms_oi_rising:           bool  | None   = None
    ms_signals:             list[str]      = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.ms_signals is None:
            self.ms_signals = []

    def short_summary(self) -> str:
        if self.direction == "NO_SETUP":
            return "BTC: No clear leverage setup right now."
        return (
            f"BTC {self.direction} | Entry ${self.entry_price:,.0f} | "
            f"SL ${self.stop_loss:,.0f} | TP1 ${self.tp1:,.0f} | "
            f"TP2 ${self.tp2:,.0f} | R:R 1:{self.r_r_tp2:.1f} | "
            f"RSI {self.rsi:.1f} | MaxLoss ${self.max_loss_usdt} | "
            f"Structure {self.market_structure_score:+d}/3"
        )


def analyze() -> TradeSetup | None:
    """
    Main entry point. Returns a TradeSetup dataclass.
    Returns None if data cannot be fetched.
    """
    log.info(f"BTC Trading Support: analyzing {cfg.BTC_CANDLE_INTERVAL} chart…")

    df = get_binance_klines("BTCUSDT", cfg.BTC_CANDLE_INTERVAL, limit=200)
    if df is None or len(df) < 50:
        log.error("Not enough BTC candle data for trade analysis.")
        return None

    close = df["close"]
    entry = close.iloc[-1]

    # ── Indicators ────────────────────────────────────────────────────────────
    rsi              = compute_rsi(close).iloc[-1]
    atr              = compute_atr(df).iloc[-1]
    ema20            = compute_ema(close, 20).iloc[-1]
    macd, signal, _  = compute_macd(close)
    bb_upper, _, bb_lower = compute_bollinger_bands(close)

    # MACD crossover: compare last two bars to detect the cross
    macd_crossed_up   = (macd.iloc[-2] < signal.iloc[-2]) and (macd.iloc[-1] >= signal.iloc[-1])
    macd_crossed_down = (macd.iloc[-2] > signal.iloc[-2]) and (macd.iloc[-1] <= signal.iloc[-1])

    # Bollinger Band touch on the previous candle (entry on this candle)
    prev_close = close.iloc[-2]
    bb_bounce_up   = prev_close <= bb_lower.iloc[-2]   # touched or pierced lower band
    bb_reject_down = prev_close >= bb_upper.iloc[-2]   # touched or pierced upper band

    # ── LONG conditions ───────────────────────────────────────────────────────
    long_conditions = {
        f"RSI {rsi:.1f} ≥ {cfg.BTC_LEVERAGE_RSI_LONG}":  rsi >= cfg.BTC_LEVERAGE_RSI_LONG,
        "Price above 20 EMA":                              entry > ema20,
        "MACD crossed up":                                 macd_crossed_up,
        "Bounced off lower BB":                            bb_bounce_up,
    }
    long_reasons  = [k for k, v in long_conditions.items() if v]
    long_triggers = len(long_reasons)

    # ── SHORT conditions ──────────────────────────────────────────────────────
    short_conditions = {
        f"RSI {rsi:.1f} ≤ {cfg.BTC_LEVERAGE_RSI_SHORT}":  rsi <= cfg.BTC_LEVERAGE_RSI_SHORT,
        "Price below 20 EMA":                               entry < ema20,
        "MACD crossed down":                                macd_crossed_down,
        "Rejected at upper BB":                             bb_reject_down,
    }
    short_reasons  = [k for k, v in short_conditions.items() if v]
    short_triggers = len(short_reasons)

    # ── Market structure signals (Binance Futures public API) ────────────────
    # Fetched fresh each call so the hourly check always has current data.
    # Score: each signal contributes -1, 0, or +1 (total range -1 … +3).
    log.info("BTC Trading Support: fetching market structure signals…")

    ms_funding     = get_btc_funding_rate()
    ms_ls_ratio    = get_btc_long_short_ratio()
    ms_oi_result   = get_btc_open_interest()
    ms_oi_current  = ms_oi_result[0] if ms_oi_result else None
    ms_oi_rising   = (ms_oi_result[0] > ms_oi_result[1]) if ms_oi_result else None

    ms_score   = 0
    ms_signals: list[str] = []

    # Signal 1: Funding rate
    if ms_funding is not None:
        pct = ms_funding * 100
        if ms_funding < 0:
            ms_score += 1
            ms_signals.append(f"Funding {pct:+.4f}% — shorts paying (bullish) [+1]")
        else:
            ms_signals.append(f"Funding {pct:+.4f}% — longs paying (bearish) [0]")

    # Signal 2: Long/Short ratio
    if ms_ls_ratio is not None:
        if ms_ls_ratio < 1.0:
            ms_score += 1
            ms_signals.append(f"L/S ratio {ms_ls_ratio:.2f} — shorts dominant, squeeze up possible [+1]")
        elif ms_ls_ratio > 1.5:
            ms_signals.append(f"L/S ratio {ms_ls_ratio:.2f} — longs crowded, squeeze risk [0]")
        else:
            ms_signals.append(f"L/S ratio {ms_ls_ratio:.2f} — balanced [0]")

    # Signal 3: Open Interest vs price direction
    # "price rising" = last close above close 4 bars ago (4 hours on 1H candles)
    if ms_oi_rising is not None:
        price_rising = float(close.iloc[-1]) > float(close.iloc[-5])
        if ms_oi_rising and price_rising:
            ms_score += 1
            ms_signals.append(f"OI rising + price rising — trend confirmed [+1]")
        elif not ms_oi_rising and price_rising:
            ms_score -= 1
            ms_signals.append(f"OI falling + price rising — weak move, no conviction [-1]")
        elif ms_oi_rising and not price_rising:
            ms_signals.append(f"OI rising + price falling — possible short buildup [0]")
        else:
            ms_signals.append(f"OI falling + price falling — de-risking in progress [0]")

    log.info(f"Market structure score: {ms_score:+d}/3  |  {'; '.join(ms_signals)}")

    # ── Decide direction: need ALL 4 technical conditions ────────────────────
    if long_triggers == 4:
        direction = "LONG"
        sign      = +1
        reasons   = long_reasons
    elif short_triggers == 4:
        direction = "SHORT"
        sign      = -1
        reasons   = short_reasons
    else:
        log.info("BTC Trading Support: no clean setup at this time.")
        return TradeSetup(
            direction              = "NO_SETUP",
            entry_price            = entry,
            stop_loss              = 0,
            tp1                    = 0,
            tp2                    = 0,
            runner                 = 0,
            rsi                    = round(rsi, 2),
            atr                    = round(atr, 2),
            r_r_tp2                = 0,
            max_loss_usdt          = 0,
            reasons                = [],
            market_structure_score = ms_score,
            ms_funding_rate        = ms_funding,
            ms_long_short_ratio    = ms_ls_ratio,
            ms_oi_value            = ms_oi_current,
            ms_oi_rising           = ms_oi_rising,
            ms_signals             = ms_signals,
        )

    # Fixed-percentage risk framework (config-driven)
    sl_pct     = cfg.BTC_RISK_SL_PCT     / 100
    tp1_pct    = cfg.BTC_RISK_TP1_PCT    / 100
    tp2_pct    = cfg.BTC_RISK_TP2_PCT    / 100
    runner_pct = cfg.BTC_RISK_RUNNER_PCT / 100

    sl     = entry * (1 - sign * sl_pct)
    tp1    = entry * (1 + sign * tp1_pct)
    tp2    = entry * (1 + sign * tp2_pct)
    runner = entry * (1 + sign * runner_pct)

    # R:R and max loss are constant for a given config (not entry-dependent)
    r_r_tp2       = round(cfg.BTC_RISK_TP2_PCT / cfg.BTC_RISK_SL_PCT, 2)
    position_usdt = cfg.BTC_RISK_MARGIN_USDT * cfg.BTC_RISK_LEVERAGE
    max_loss_usdt = round(position_usdt * sl_pct, 2)

    setup = TradeSetup(
        direction              = direction,
        entry_price            = round(entry, 2),
        stop_loss              = round(sl, 2),
        tp1                    = round(tp1, 2),
        tp2                    = round(tp2, 2),
        runner                 = round(runner, 2),
        rsi                    = round(rsi, 2),
        atr                    = round(atr, 2),
        r_r_tp2                = r_r_tp2,
        max_loss_usdt          = max_loss_usdt,
        reasons                = reasons,
        market_structure_score = ms_score,
        ms_funding_rate        = ms_funding,
        ms_long_short_ratio    = ms_ls_ratio,
        ms_oi_value            = ms_oi_current,
        ms_oi_rising           = ms_oi_rising,
        ms_signals             = ms_signals,
    )
    log.info(setup.short_summary())
    return setup


# ══════════════════════════════════════════════════════════════════════════════
# Phase 2 — Smart Entry Timing
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class EntrySignal:
    """
    Result of Phase 2 entry timing analysis.
    Built from 15M MEXC candles + market structure signals already fetched
    by Phase 1 (TradeSetup.ms_* fields).

    Scoring (max 8):
      EMA squeeze breakout  +2
      RSI ideal  50–65      +2   RSI acceptable 65–72  +1
      Volume above avg      +1
      Funding negative      +1
      OI rising             +1
      Long/Short < 1.0      +1
    Fire when score ≥ ENTRY_MIN_SCORE (5).
    """
    ready:             bool
    score:             int
    direction:         str          # "LONG" or "SHORT"
    wait_reason:       str          # non-empty when not ready

    # Current price and derived levels
    entry_price:       float
    stop_loss:         float
    tp1:               float
    tp2:               float
    runner:            float
    max_loss_usdt:     float        # at 50× with ENTRY_MARGIN_USDT

    # 15M technical signals
    squeeze_confirmed: bool         # last N candles inside EMA squeeze
    breakout_confirmed:bool         # price above/below EMA6 + volume spike
    ema6_15m:          float        # EMA6 on 15M (= breakout reference level)
    rsi_15m:           float
    vol_above_avg:     bool
    price_vs_ema6_pct: float        # % distance from EMA6 (+ = above, - = below)
    in_pullback_wait:  bool         # price ran > 0.8%, waiting for retrace

    # Individual scores
    ema_score:         int          # 0 or 2
    rsi_score:         int          # 0, 1, or 2
    vol_score:         int          # 0 or 1
    funding_score:     int          # 0 or 1
    oi_score:          int          # 0 or 1
    ls_score:          int          # 0 or 1

    # Human-readable breakdown for the alert
    signal_lines:      list[str]    # one line per signal with score label


def check_entry_timing(direction: str, setup: TradeSetup) -> EntrySignal:
    """
    Phase 2: Smart Entry Timing on 15M MEXC candles.

    Called after Phase 1 has confirmed a LONG or SHORT direction.
    Returns an EntrySignal; only send an alert when EntrySignal.ready is True.

    Parameters
    ----------
    direction : "LONG" or "SHORT"
    setup     : TradeSetup from Phase 1 — reuses its ms_* market structure fields
                so we don't re-fetch the Binance Futures endpoints.
    """
    log.info(f"Phase 2: checking 15M entry timing for {direction}…")

    # ── Fetch 15M MEXC candles ────────────────────────────────────────────────
    df = get_mexc_klines("BTCUSDT", "15m", limit=cfg.ENTRY_15M_LIMIT)
    if df is None or len(df) < 20:
        log.error("Phase 2: insufficient 15M candle data.")
        return _no_entry(direction, "Insufficient 15M data", setup)

    close  = df["close"]
    volume = df["volume"]

    ema6_s  = compute_ema(close,  6)
    ema12_s = compute_ema(close, 12)
    ema20_s = compute_ema(close, 20)
    rsi_s   = compute_rsi(close, 14)

    price        = float(close.iloc[-1])
    ema6         = float(ema6_s.iloc[-1])
    rsi_15m      = float(rsi_s.iloc[-1])
    vol_avg_10   = float(volume.rolling(10).mean().iloc[-1])
    current_vol  = float(volume.iloc[-1])

    # ── 1. EMA Squeeze detection ──────────────────────────────────────────────
    # Squeeze = last ENTRY_SQUEEZE_CANDLES consecutive bars all had EMA spread < 2 %
    squeeze_confirmed = True
    for i in range(-cfg.ENTRY_SQUEEZE_CANDLES, 0):
        e6  = float(ema6_s.iloc[i])
        e12 = float(ema12_s.iloc[i])
        e20 = float(ema20_s.iloc[i])
        mn  = min(e6, e12, e20)
        spread = (max(e6, e12, e20) - mn) / mn * 100 if mn > 0 else 999.0
        if spread >= cfg.ENTRY_SQUEEZE_PCT:
            squeeze_confirmed = False
            break

    # Breakout = price exits the squeeze in the right direction + volume confirms
    vol_spike     = current_vol > vol_avg_10
    if direction == "LONG":
        breakout_confirmed = squeeze_confirmed and price > ema6 and vol_spike
    else:
        breakout_confirmed = squeeze_confirmed and price < ema6 and vol_spike

    # ── 2. RSI Gate ───────────────────────────────────────────────────────────
    rsi_ok = cfg.ENTRY_RSI_MIN <= rsi_15m <= cfg.ENTRY_RSI_MAX

    # ── 3. Pullback / extension check ────────────────────────────────────────
    if direction == "LONG":
        price_vs_ema6_pct = (price - ema6) / ema6 * 100     # + = above EMA6
    else:
        price_vs_ema6_pct = (ema6 - price) / ema6 * 100     # + = below EMA6

    extended         = price_vs_ema6_pct > cfg.ENTRY_PULLBACK_MAX_PCT
    pullback_arrived = price_vs_ema6_pct <= cfg.ENTRY_PULLBACK_NEAR_PCT
    in_pullback_wait = extended and not pullback_arrived

    # ── 4. Score each signal ──────────────────────────────────────────────────
    signal_lines: list[str] = []

    # EMA breakout (+2)
    ema_score = 0
    if breakout_confirmed:
        ema_score = 2
        signal_lines.append(
            f"🟢  EMA Squeeze Breakout (15M)  Confirmed            [+2]"
        )
    elif squeeze_confirmed:
        signal_lines.append(
            f"⚪  EMA Squeeze Breakout (15M)  Squeeze but no break [ 0]"
        )
    else:
        signal_lines.append(
            f"🔴  EMA Squeeze Breakout (15M)  No squeeze detected  [ 0]"
        )

    # RSI (+2 ideal / +1 acceptable / 0 outside gate)
    rsi_score = 0
    if cfg.ENTRY_RSI_IDEAL_MIN <= rsi_15m <= cfg.ENTRY_RSI_IDEAL_MAX:
        rsi_score = 2
        signal_lines.append(
            f"🟢  RSI 15M ({rsi_15m:.1f})            Ideal zone 50–65     [+2]"
        )
    elif cfg.ENTRY_RSI_IDEAL_MAX < rsi_15m <= cfg.ENTRY_RSI_OK_MAX:
        rsi_score = 1
        signal_lines.append(
            f"🟡  RSI 15M ({rsi_15m:.1f})            Acceptable 65–72     [+1]"
        )
    elif rsi_15m > cfg.ENTRY_RSI_MAX:
        signal_lines.append(
            f"🔴  RSI 15M ({rsi_15m:.1f})            Overheated >72       [ 0]"
        )
    else:
        signal_lines.append(
            f"🔴  RSI 15M ({rsi_15m:.1f})            Momentum low <45     [ 0]"
        )

    # Volume (+1)
    vol_score = 0
    if vol_spike:
        vol_score = 1
        signal_lines.append(
            f"🟢  Volume                      Above 10-candle avg  [+1]"
        )
    else:
        signal_lines.append(
            f"⚪  Volume                      Below average        [ 0]"
        )

    # Funding (+1)
    funding_score = 0
    fr = setup.ms_funding_rate
    if fr is not None and fr < 0:
        funding_score = 1
        signal_lines.append(
            f"🟢  Funding ({fr*100:+.4f}%)         Negative — bullish   [+1]"
        )
    else:
        fr_str = f"{fr*100:+.4f}%" if fr is not None else "N/A"
        signal_lines.append(
            f"⚪  Funding ({fr_str})         Neutral/positive     [ 0]"
        )

    # OI (+1)
    oi_score = 0
    if setup.ms_oi_rising:
        oi_score = 1
        signal_lines.append(
            f"🟢  Open Interest               Rising               [+1]"
        )
    else:
        signal_lines.append(
            f"⚪  Open Interest               Flat/falling         [ 0]"
        )

    # Long/Short ratio (+1)
    ls_score = 0
    ls = setup.ms_long_short_ratio
    if ls is not None and ls < 1.0:
        ls_score = 1
        signal_lines.append(
            f"🟢  Long/Short ({ls:.2f})          Shorts dominant      [+1]"
        )
    else:
        ls_str = f"{ls:.2f}" if ls is not None else "N/A"
        signal_lines.append(
            f"⚪  Long/Short ({ls_str})          Balanced/long-heavy  [ 0]"
        )

    total_score = ema_score + rsi_score + vol_score + funding_score + oi_score + ls_score

    # ── 5. Readiness gate ────────────────────────────────────────────────────
    wait_reason = ""
    if not rsi_ok:
        if rsi_15m > cfg.ENTRY_RSI_MAX:
            wait_reason = f"RSI 15M {rsi_15m:.1f} — overheated (>{cfg.ENTRY_RSI_MAX}), wait for cooldown"
        else:
            wait_reason = f"RSI 15M {rsi_15m:.1f} — momentum not confirmed yet (<{cfg.ENTRY_RSI_MIN})"
    elif in_pullback_wait:
        wait_reason = (
            f"Price {price_vs_ema6_pct:.2f}% beyond EMA6 breakout level — "
            f"waiting for pullback to EMA6 ({ema6:,.2f})"
        )
    elif total_score < cfg.ENTRY_MIN_SCORE:
        wait_reason = (
            f"Score {total_score}/{cfg.ENTRY_MIN_SCORE} required "
            f"(need {cfg.ENTRY_MIN_SCORE - total_score} more points)"
        )

    ready = rsi_ok and not in_pullback_wait and total_score >= cfg.ENTRY_MIN_SCORE

    # ── 6. Calculate entry levels (current price, 50× / ENTRY_MARGIN_USDT) ──
    sign          = +1 if direction == "LONG" else -1
    sl_pct        = cfg.BTC_RISK_SL_PCT    / 100
    tp1_pct       = cfg.BTC_RISK_TP1_PCT   / 100
    tp2_pct       = cfg.BTC_RISK_TP2_PCT   / 100
    runner_pct    = cfg.BTC_RISK_RUNNER_PCT / 100
    position_usdt = cfg.ENTRY_MARGIN_USDT * cfg.ENTRY_LEVERAGE
    max_loss_usdt = round(position_usdt * sl_pct, 2)

    log.info(
        f"Phase 2: score={total_score}/8  ready={ready}  "
        f"squeeze={squeeze_confirmed}  breakout={breakout_confirmed}  "
        f"RSI={rsi_15m:.1f}  pullback_wait={in_pullback_wait}"
        + (f"  WAIT: {wait_reason}" if not ready else "")
    )

    return EntrySignal(
        ready              = ready,
        score              = total_score,
        direction          = direction,
        wait_reason        = wait_reason,
        entry_price        = round(price, 2),
        stop_loss          = round(price * (1 - sign * sl_pct), 2),
        tp1                = round(price * (1 + sign * tp1_pct), 2),
        tp2                = round(price * (1 + sign * tp2_pct), 2),
        runner             = round(price * (1 + sign * runner_pct), 2),
        max_loss_usdt      = max_loss_usdt,
        squeeze_confirmed  = squeeze_confirmed,
        breakout_confirmed = breakout_confirmed,
        ema6_15m           = round(ema6, 2),
        rsi_15m            = round(rsi_15m, 2),
        vol_above_avg      = vol_spike,
        price_vs_ema6_pct  = round(price_vs_ema6_pct, 3),
        in_pullback_wait   = in_pullback_wait,
        ema_score          = ema_score,
        rsi_score          = rsi_score,
        vol_score          = vol_score,
        funding_score      = funding_score,
        oi_score           = oi_score,
        ls_score           = ls_score,
        signal_lines       = signal_lines,
    )


def _no_entry(direction: str, reason: str, setup: TradeSetup) -> EntrySignal:
    """Return a not-ready EntrySignal when data is unavailable."""
    price = setup.entry_price or 0.0
    sign  = +1 if direction == "LONG" else -1
    sl_pct = cfg.BTC_RISK_SL_PCT / 100
    return EntrySignal(
        ready=False, score=0, direction=direction, wait_reason=reason,
        entry_price=price,
        stop_loss  = round(price * (1 - sign * sl_pct), 2),
        tp1        = round(price * (1 + sign * cfg.BTC_RISK_TP1_PCT    / 100), 2),
        tp2        = round(price * (1 + sign * cfg.BTC_RISK_TP2_PCT    / 100), 2),
        runner     = round(price * (1 + sign * cfg.BTC_RISK_RUNNER_PCT / 100), 2),
        max_loss_usdt      = round(cfg.ENTRY_MARGIN_USDT * cfg.ENTRY_LEVERAGE * sl_pct, 2),
        squeeze_confirmed  = False, breakout_confirmed = False,
        ema6_15m           = 0.0,  rsi_15m            = 0.0,
        vol_above_avg      = False, price_vs_ema6_pct  = 0.0,
        in_pullback_wait   = False,
        ema_score=0, rsi_score=0, vol_score=0,
        funding_score=0, oi_score=0, ls_score=0,
        signal_lines=[],
    )


# ══════════════════════════════════════════════════════════════════════════════
# Stand-alone test — run this file directly to check for a live BTC setup
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import modules.telegram_alerts as m4

    print("Fetching live BTC 4H data from Binance…")
    result = analyze()

    if result is None:
        print("ERROR: Could not fetch BTC data. Check logs.")
        raise SystemExit(1)

    print(f"\n  Direction : {result.direction}")
    print(f"  Entry     : ${result.entry_price:,.2f}")
    if result.direction != "NO_SETUP":
        print(f"  Stop-Loss : ${result.stop_loss:,.2f}")
        print(f"  TP1       : ${result.tp1:,.2f}")
        print(f"  TP2       : ${result.tp2:,.2f}")
        print(f"  Runner    : ${result.runner:,.2f}")
        print(f"  R:R (TP2) : 1:{result.r_r_tp2}")
        print(f"  Signals   : {', '.join(result.reasons)}")
        print("\nSending BTC trade alert to Telegram…")
        ok = m4.send_btc_alert(result)
        print("Sent OK ✓" if ok else "FAILED — check token / chat ID in .env")
    else:
        print("  RSI       :", result.rsi)
        print("  ATR       :", result.atr)
        print("\nNo active setup — nothing to send.")
