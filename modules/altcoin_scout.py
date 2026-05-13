"""
Module 2: Altcoin Scout
────────────────────────────────────────────────────────────────────────────────
Scans small-cap altcoins ($20 M–$300 M market cap) for explosive-move setups
using 7 criteria.  Two data sources — both free:

  • CoinMarketCap API   → market cap, volume, supply data  (criteria 1–4)
  • MEXC public API     → 4H OHLCV candles for RSI + EMA   (criteria 5–6)

─────────────────────────────────────────────────────────────────────────────
The 7 Criteria
─────────────────────────────────────────────────────────────────────────────
C1  Market cap between $20 M and $300 M
      Targets coins small enough to move fast but large enough to have liquidity.

C2  Vol / market-cap ratio ≥ 200 %  →  Level 1 Watchlist Alert
      Unusual volume relative to the coin's size signals fresh interest.
      Formula: (24h_volume / market_cap) × 100

C3  Vol / market-cap ratio ≥ 500 %  →  Level 2 Trade Alert  (replaces C2 level)
      Extreme volume relative to size — potential breakout or manipulation event.

C4  Circulation rate ≥ 50 %
      circulating_supply / total_supply ≥ 0.50
      Avoids coins where most supply is locked and could dump at any moment.

C5  RSI(14) on 4H < 70
      Not already overbought on the trading timeframe.
      Computed from MEXC 4H klines.

C6  EMA squeeze on 4H: EMA6, EMA12, EMA20 all within 3 % of each other
      A tight EMA cluster means price has been consolidating — energy is
      building up and a directional move is likely coming soon.
      squeeze_pct = (max_ema − min_ema) / min_ema × 100 < 3 %

C7  BTC regime is BULL or NEUTRAL
      The macro environment must not be actively bearish.
      Passed in from Module 1 before calling full_scan().

─────────────────────────────────────────────────────────────────────────────
Alert Levels
─────────────────────────────────────────────────────────────────────────────
  Level 1 (C1 + C2 + C4 + C5 + C6 + C7)  →  "Coin on Radar"
  Level 2 (C1 + C3 + C4 + C5 + C6 + C7)  →  "Trade Alert"  (instant Telegram)

A coin gets Level 2 if vol/mc ≥ 500 %; Level 1 if vol/mc ≥ 200 % but < 500 %.
Both levels require C4–C7 to pass.

─────────────────────────────────────────────────────────────────────────────
Output
─────────────────────────────────────────────────────────────────────────────
full_scan(btc_context) → (results: list[ScoutResult], avoid: list[AvoidCoin])

  results  — all qualifying coins, Level 2 first, then Level 1, sorted by vol/mc ratio
  avoid    — coins in the same market-cap range showing weakness (for the daily briefing)

Run directly to test:
  python modules/altcoin_scout.py
"""

from __future__ import annotations

# ── Path shim ─────────────────────────────────────────────────────────────────
import sys as _sys, os as _os
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
# ─────────────────────────────────────────────────────────────────────────────

import time
from dataclasses import dataclass

from utils.api_client import get_cmc_listings, get_mexc_klines
from utils.indicators import compute_rsi, compute_ema
from utils.logger     import get_logger
import config as cfg

log = get_logger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# Result dataclasses
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class ScoutResult:
    """
    A coin that passed all 7 criteria and qualifies for an alert.
    All price values in USD; ratios in percent.
    """
    symbol:       str
    name:         str
    price:        float

    # Market data (from CMC)
    market_cap:   float
    volume_24h:   float
    vol_mc_ratio: float   # (volume_24h / market_cap) × 100
    circ_rate:    float   # (circulating_supply / total_supply) × 100

    # Technical data (from MEXC 4H)
    rsi_4h:       float   # RSI(14) on 4H
    ema6:         float
    ema12:        float
    ema20:        float
    ema_spread:   float   # % spread between min and max of the three EMAs

    # Classification
    alert_level:  int          # 1 = Watchlist, 2 = Trade Alert
    direction:    str = "LONG" # "LONG" or "SHORT"


@dataclass
class AvoidCoin:
    """
    A coin in the same market-cap range showing clear weakness.
    Populated from CMC data only — no MEXC call needed.
    """
    symbol:         str
    name:           str
    price:          float
    market_cap:     float
    volume_24h:     float
    change_24h_pct: float
    change_7d_pct:  float
    reason:         str


# ══════════════════════════════════════════════════════════════════════════════
# Internal helpers
# ══════════════════════════════════════════════════════════════════════════════

def _extract_quote(coin: dict) -> dict:
    """Pull the USD quote sub-dict out of a CMC listing entry."""
    return coin.get("quote", {}).get("USD", {})


def _check_market_data(coin: dict) -> tuple[bool, float, float, float]:
    """
    Evaluate criteria C1, C2/C3, C4 from CMC data alone — no API call.

    Returns:
      (passes_gate, vol_mc_ratio, circ_rate, market_cap)

    passes_gate is True only if C1 and C4 both pass AND vol/mc ≥ 200 %.
    The caller uses vol_mc_ratio to decide the alert level (C2 vs C3).
    """
    q           = _extract_quote(coin)
    market_cap  = q.get("market_cap")  or 0.0
    volume_24h  = q.get("volume_24h")  or 0.0
    circ        = coin.get("circulating_supply") or 0.0
    total       = coin.get("total_supply")       or 0.0

    # C1: market cap in target range
    if not (cfg.SCOUT_MCAP_MIN_USD <= market_cap <= cfg.SCOUT_MCAP_MAX_USD):
        return False, 0.0, 0.0, market_cap

    # C2 / C3: vol / market-cap ratio — must be at least Level 1 (≥ 200 %)
    if market_cap == 0:
        return False, 0.0, 0.0, market_cap
    vol_mc_ratio = volume_24h / market_cap * 100.0
    if vol_mc_ratio < cfg.SCOUT_VOL_MC_L1_PCT:
        return False, vol_mc_ratio, 0.0, market_cap

    # C4: circulation rate
    circ_rate = (circ / total * 100.0) if total > 0 else 0.0
    if circ_rate < cfg.SCOUT_CIRC_RATE_MIN_PCT:
        return False, vol_mc_ratio, circ_rate, market_cap

    return True, vol_mc_ratio, circ_rate, market_cap


def _check_technicals(symbol_usdt: str) -> tuple[bool, float, float, float, float, float]:
    """
    Evaluate C5 (RSI < 70) and C6 (EMA squeeze < 3 %) from MEXC 4H klines.

    Returns:
      (passes, rsi_4h, ema6, ema12, ema20, ema_spread_pct)

    Returns (False, 0, 0, 0, 0, 0) when klines are unavailable.
    """
    df = get_mexc_klines(symbol_usdt, "4h", limit=cfg.SCOUT_4H_CANDLE_LIMIT)
    if df is None or len(df) < 25:
        # Need at least 25 bars for EMA20 to have warmed up meaningfully
        return False, 0.0, 0.0, 0.0, 0.0, 0.0

    close = df["close"]

    # C5: RSI(14) on 4H
    rsi_val = compute_rsi(close, period=14).iloc[-1]
    if rsi_val >= cfg.SCOUT_RSI_MAX_4H:
        return False, round(rsi_val, 2), 0.0, 0.0, 0.0, 0.0

    # C6: EMA squeeze — all three EMAs within SCOUT_EMA_SQUEEZE_PCT of each other
    ema6  = compute_ema(close, 6).iloc[-1]
    ema12 = compute_ema(close, 12).iloc[-1]
    ema20 = compute_ema(close, 20).iloc[-1]

    ema_min      = min(ema6, ema12, ema20)
    ema_max      = max(ema6, ema12, ema20)
    ema_spread   = (ema_max - ema_min) / ema_min * 100.0 if ema_min > 0 else 999.0

    if ema_spread >= cfg.SCOUT_EMA_SQUEEZE_PCT:
        return False, round(rsi_val, 2), round(ema6, 6), round(ema12, 6), round(ema20, 6), round(ema_spread, 2)

    return (
        True,
        round(rsi_val, 2),
        round(ema6,  6),
        round(ema12, 6),
        round(ema20, 6),
        round(ema_spread, 2),
    )


def _check_technicals_short(symbol_usdt: str) -> tuple[bool, float, float, float, float, float]:
    """
    Evaluate SHORT criteria on MEXC 4H klines:

      S5  RSI(14) > SCOUT_RSI_MIN_4H_SHORT  (overbought — above 70)
      S6  EMA bearish stack: EMA6 < EMA12 < EMA20
      S7  Price < EMA20  (broke below mid-term trend)
      S8  Last candle is red (close < open) AND volume > MA20 × spike multiplier

    Returns (passes, rsi_4h, ema6, ema12, ema20, ema_spread_pct).
    All values are 0.0 on data failure.
    """
    df = get_mexc_klines(symbol_usdt, "4h", limit=cfg.SCOUT_4H_CANDLE_LIMIT)
    if df is None or len(df) < 25:
        return False, 0.0, 0.0, 0.0, 0.0, 0.0

    close  = df["close"]
    open_  = df["open"]
    volume = df["volume"]

    rsi_val = float(compute_rsi(close, period=14).iloc[-1])
    ema6  = float(compute_ema(close,  6).iloc[-1])
    ema12 = float(compute_ema(close, 12).iloc[-1])
    ema20 = float(compute_ema(close, 20).iloc[-1])
    price = float(close.iloc[-1])

    ema_spread = (max(ema6, ema12, ema20) - min(ema6, ema12, ema20)) / min(ema6, ema12, ema20) * 100.0

    # S5: RSI overbought
    if rsi_val <= cfg.SCOUT_RSI_MIN_4H_SHORT:
        return False, round(rsi_val, 2), round(ema6, 6), round(ema12, 6), round(ema20, 6), round(ema_spread, 2)

    # S6: bearish EMA stack
    if not (ema6 < ema12 < ema20):
        return False, round(rsi_val, 2), round(ema6, 6), round(ema12, 6), round(ema20, 6), round(ema_spread, 2)

    # S7: price below EMA20
    if price >= ema20:
        return False, round(rsi_val, 2), round(ema6, 6), round(ema12, 6), round(ema20, 6), round(ema_spread, 2)

    # S8: last candle is bearish AND volume spike
    last_red   = float(close.iloc[-1]) < float(open_.iloc[-1])
    vol_ma20   = float(volume.rolling(20).mean().iloc[-1])
    vol_spiked = float(volume.iloc[-1]) > vol_ma20 * cfg.SCOUT_SHORT_VOL_SPIKE_MULT

    if not (last_red and vol_spiked):
        return False, round(rsi_val, 2), round(ema6, 6), round(ema12, 6), round(ema20, 6), round(ema_spread, 2)

    return (
        True,
        round(rsi_val, 2),
        round(ema6,   6),
        round(ema12,  6),
        round(ema20,  6),
        round(ema_spread, 2),
    )


def _build_avoid_list(coins: list[dict]) -> list[AvoidCoin]:
    """
    From the same CMC listing, identify coins showing quiet weakness:
    significant 7d price decline with very low volume interest.

    No extra API calls — pure CMC data.
    """
    avoid: list[AvoidCoin] = []

    for coin in coins:
        q       = _extract_quote(coin)
        mcap    = q.get("market_cap")         or 0.0
        vol     = q.get("volume_24h")         or 0.0
        ch24    = q.get("percent_change_24h") or 0.0
        ch7d    = q.get("percent_change_7d")  or 0.0

        # Only flag coins in the target market-cap range
        if not (cfg.SCOUT_MCAP_MIN_USD <= mcap <= cfg.SCOUT_MCAP_MAX_USD):
            continue

        # Avoid criteria: falling price AND very little volume interest
        vol_mc = vol / mcap * 100.0 if mcap > 0 else 0.0
        if ch7d < -8.0 and vol_mc < 30.0:
            reasons = []
            if ch7d < -15:
                reasons.append(f"Heavy 7d dump ({ch7d:+.1f}%)")
            else:
                reasons.append(f"7d downtrend ({ch7d:+.1f}%)")
            if ch24 < -3:
                reasons.append(f"24h red ({ch24:+.1f}%)")
            reasons.append(f"Vol/MC only {vol_mc:.0f}% — no buying interest")

            avoid.append(AvoidCoin(
                symbol         = coin.get("symbol", "").upper() + "USDT",
                name           = coin.get("name", ""),
                price          = round(q.get("price") or 0.0, 8),
                market_cap     = mcap,
                volume_24h     = vol,
                change_24h_pct = round(ch24, 2),
                change_7d_pct  = round(ch7d, 2),
                reason         = "  ·  ".join(reasons),
            ))

    # Worst performers first; cap to avoid flooding the briefing
    avoid.sort(key=lambda x: x.change_7d_pct)
    return avoid[:8]


# ══════════════════════════════════════════════════════════════════════════════
# Main scan function
# ══════════════════════════════════════════════════════════════════════════════

def full_scan(btc_context) -> tuple[list[ScoutResult], list[AvoidCoin]]:
    """
    Run the full altcoin scan.

    Parameters
    ----------
    btc_context : BTCContext from Module 1
        Must not be None.  If BTC regime is BEAR the scan is skipped entirely.

    Returns
    -------
    results : list[ScoutResult]
        Qualifying coins sorted by alert level (Level 2 first) then vol/mc ratio.
    avoid   : list[AvoidCoin]
        Coins in the same market-cap range showing weakness.
    """
    # ── C7: BTC regime gate (per direction) ──────────────────────────────────
    if btc_context is None:
        log.warning("Altcoin Scout skipped — btc_context is None.")
        return [], []

    regime          = btc_context.regime
    run_long_scan   = regime in cfg.SCOUT_ALLOWED_REGIMES
    run_short_scan  = regime in cfg.SCOUT_SHORT_ALLOWED_REGIMES

    if not run_long_scan and not run_short_scan:
        log.warning(
            f"Altcoin Scout skipped — BTC regime '{regime}' does not enable "
            f"LONG {cfg.SCOUT_ALLOWED_REGIMES} or SHORT {cfg.SCOUT_SHORT_ALLOWED_REGIMES}."
        )
        return [], []

    log.info(
        f"Altcoin Scout starting (BTC={regime}, "
        f"LONG={'✓' if run_long_scan else '✗'}, "
        f"SHORT={'✓' if run_short_scan else '✗'}, "
        f"mcap ${cfg.SCOUT_MCAP_MIN_USD/1e6:.0f}M–${cfg.SCOUT_MCAP_MAX_USD/1e6:.0f}M)…"
    )

    # ── Fetch CMC market listings ─────────────────────────────────────────────
    coins = get_cmc_listings(
        limit        = cfg.SCOUT_CMC_LIMIT,
        mcap_min_usd = cfg.SCOUT_MCAP_MIN_USD,
        mcap_max_usd = cfg.SCOUT_MCAP_MAX_USD,
    )
    if not coins:
        log.error("CMC listings returned nothing — aborting scan.")
        return [], []

    log.info(f"CMC returned {len(coins)} coins in the target market-cap range.")

    results: list[ScoutResult] = []

    for i, coin in enumerate(coins):
        symbol_raw  = coin.get("symbol", "").upper()
        symbol_usdt = symbol_raw + "USDT"
        name        = coin.get("name", "")
        q           = _extract_quote(coin)
        price       = round(q.get("price") or 0.0, 8)

        # ── C1 + C2/C3 + C4: cheap market-data checks first ──────────────────
        # Same gate for both LONG and SHORT — we need liquidity either way.
        passes_mkt, vol_mc_ratio, circ_rate, market_cap = _check_market_data(coin)
        if not passes_mkt:
            continue

        candidate_level = 2 if vol_mc_ratio >= cfg.SCOUT_VOL_MC_L2_PCT else 1
        base_kwargs = dict(
            symbol       = symbol_usdt,
            name         = name,
            price        = price,
            market_cap   = market_cap,
            volume_24h   = q.get("volume_24h") or 0.0,
            vol_mc_ratio = round(vol_mc_ratio, 1),
            circ_rate    = round(circ_rate, 1),
            alert_level  = candidate_level,
        )

        added = False

        # ── LONG path (C5 + C6): RSI < 70, EMA squeeze ───────────────────────
        if run_long_scan:
            tech_ok, rsi_4h, ema6, ema12, ema20, ema_spread = _check_technicals(symbol_usdt)
            if tech_ok:
                results.append(ScoutResult(
                    **base_kwargs,
                    rsi_4h     = rsi_4h,
                    ema6       = ema6,
                    ema12      = ema12,
                    ema20      = ema20,
                    ema_spread = ema_spread,
                    direction  = "LONG",
                ))
                log.info(
                    f"  {'🚨 L2' if candidate_level == 2 else '👀 L1'} LONG  {symbol_usdt}  "
                    f"Vol/MC {vol_mc_ratio:.0f}%  RSI {rsi_4h}  Spread {ema_spread:.2f}%"
                )
                added = True
            else:
                log.debug(f"  SKIP LONG {symbol_usdt}: RSI={rsi_4h}, spread={ema_spread:.1f}%")

        # ── SHORT path (S5–S8): RSI > 70, bearish stack, red candle + vol spike
        # Only check if this coin didn't already qualify as a LONG (mutually exclusive
        # in practice since RSI can't be both < 70 and > 70 simultaneously).
        if run_short_scan and not added:
            s_ok, rsi_4h, ema6, ema12, ema20, ema_spread = _check_technicals_short(symbol_usdt)
            if s_ok:
                results.append(ScoutResult(
                    **base_kwargs,
                    rsi_4h     = rsi_4h,
                    ema6       = ema6,
                    ema12      = ema12,
                    ema20      = ema20,
                    ema_spread = ema_spread,
                    direction  = "SHORT",
                ))
                log.info(
                    f"  {'🚨 L2' if candidate_level == 2 else '👀 L1'} SHORT  {symbol_usdt}  "
                    f"Vol/MC {vol_mc_ratio:.0f}%  RSI {rsi_4h}  Stack BEAR"
                )
            else:
                log.debug(f"  SKIP SHORT {symbol_usdt}: RSI={rsi_4h}, ema_spread={ema_spread:.1f}%")

        # Polite pause every 20 MEXC calls to avoid hammering the free API
        if i % 20 == 19:
            time.sleep(0.3)

        if len(results) >= cfg.SCOUT_MAX_RESULTS:
            log.info(f"Reached max results cap ({cfg.SCOUT_MAX_RESULTS}), stopping early.")
            break

    # Sort: Level 2 first; within each level LONG before SHORT; then vol/mc desc
    results.sort(key=lambda r: (-r.alert_level, 0 if r.direction == "LONG" else 1, -r.vol_mc_ratio))

    # Build avoid list from full CMC dataset (no extra API calls)
    avoid = _build_avoid_list(coins)

    log.info(
        f"Altcoin Scout complete — "
        f"{sum(r.alert_level == 2 for r in results)} Level-2 alerts, "
        f"{sum(r.alert_level == 1 for r in results)} Level-1 watchlist, "
        f"{len(avoid)} coins to avoid."
    )
    return results, avoid


# ══════════════════════════════════════════════════════════════════════════════
# Stand-alone test
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import modules.btc_context_analyzer as m1

    print("Fetching BTC context (needed for C7 regime gate)…")
    ctx = m1.analyze()
    if ctx is None:
        print("ERROR: Module 1 failed.")
        raise SystemExit(1)
    print(f"  BTC regime: {ctx.regime}\n")

    print("Running Altcoin Scout…")
    results, avoid = full_scan(ctx)

    print(f"\n{'─'*50}")
    print(f"Level-2 Trade Alerts:  {sum(r.alert_level == 2 for r in results)}")
    print(f"Level-1 Watchlist:     {sum(r.alert_level == 1 for r in results)}")
    print(f"Coins to avoid:        {len(avoid)}")
    print(f"{'─'*50}")

    for r in results:
        label = "🚨 L2 TRADE" if r.alert_level == 2 else "👀 L1 RADAR"
        print(
            f"{label}  {r.symbol:<14}  "
            f"Vol/MC {r.vol_mc_ratio:>6.0f}%  "
            f"RSI {r.rsi_4h:>5.1f}  "
            f"Spread {r.ema_spread:.2f}%  "
            f"MCap ${r.market_cap/1e6:.0f}M"
        )

    if avoid:
        print(f"\n{'─'*50}  AVOID")
        for a in avoid[:5]:
            print(f"  ⛔  {a.symbol:<14}  7d {a.change_7d_pct:+.1f}%  {a.reason}")
