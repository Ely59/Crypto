"""
validate_backtest.py — Run after fixes are deployed.
Backtests LPT, JTO, SKYAI, CGPT:
  1. Read last alert from alert_log.csv per coin
  2. Fetch current price + peak after alert from MEXC klines
  3. Print and Telegram-send results table
Also sends one LPT test alert in the new unified format.
"""

from __future__ import annotations

import sys
import os

_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import csv
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

import config as cfg
from utils.api_client import get_mexc_futures_klines
from modules.telegram_alerts import send_message, build_unified_alert, send_message_with_buttons, _build_coin_keyboard, _get_price_scale
from utils.logger import get_logger

log = get_logger("validate_backtest")

_BERLIN = ZoneInfo("Europe/Berlin")
_COINS  = ["LPT", "JTO", "SKYAI", "CGPT"]


def _current_price(symbol: str) -> float | None:
    """Fetch latest price from MEXC futures klines."""
    df = get_mexc_futures_klines(f"{symbol}_USDT", "1h", limit=1)
    if df is not None and not df.empty:
        return float(df["close"].iloc[-1])
    return None


def _peak_after_alert(symbol: str, alert_ts: datetime, alert_price: float) -> tuple[float, float]:
    """Return (peak_price, max_gain_pct) for the period after alert_ts."""
    df = get_mexc_futures_klines(f"{symbol}_USDT", "1h", limit=200)
    if df is None or df.empty:
        return 0.0, 0.0

    import pandas as pd
    after = df[df.index >= pd.Timestamp(alert_ts)]
    if after.empty:
        return 0.0, 0.0

    peak = float(after["high"].max())
    gain = (peak - alert_price) / alert_price * 100.0 if alert_price > 0 else 0.0
    return peak, gain


def _read_last_alerts(csv_path: str) -> dict[str, dict]:
    """Return the most recent alert row per coin."""
    last: dict[str, dict] = {}
    try:
        with open(csv_path, newline="") as f:
            for row in csv.DictReader(f):
                coin = row.get("coin", "")
                if coin in _COINS:
                    # keep latest by timestamp
                    if coin not in last:
                        last[coin] = row
                    else:
                        try:
                            t_new = datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M")
                            t_old = datetime.strptime(last[coin]["timestamp"], "%Y-%m-%d %H:%M")
                            if t_new > t_old:
                                last[coin] = row
                        except ValueError:
                            pass
    except FileNotFoundError:
        pass
    return last


def run_backtest() -> str:
    """Build and return the backtest result table string."""
    alert_log = cfg.ALERT_LOG_CSV
    last_alerts = _read_last_alerts(alert_log)

    header = (
        "📊 <b>BACKTEST RESULTS</b>\n"
        f"<code>{'Coin':<7}| {'Alert$':>8} | {'Peak$':>8} | {'Max%':>6} | TP1 | TP2 | {'Now%':>6}</code>"
    )
    rows = [header]

    for coin in _COINS:
        row_data = last_alerts.get(coin)

        if row_data is None:
            rows.append(f"<code>{coin:<7}| No alert history found in log</code>")
            continue

        try:
            ts_str      = row_data["timestamp"]
            alert_price = float(row_data.get("price_at_alert") or 0)
            alert_ts    = datetime.strptime(ts_str, "%Y-%m-%d %H:%M").replace(tzinfo=_BERLIN)
        except (ValueError, KeyError):
            rows.append(f"<code>{coin:<7}| Invalid log row</code>")
            continue

        if alert_price <= 0:
            rows.append(f"<code>{coin:<7}| Alert price missing</code>")
            continue

        current_p  = _current_price(coin)
        peak_p, max_pct = _peak_after_alert(coin, alert_ts, alert_price)

        tp1_hit = "YES" if max_pct >= 8.0  else "NO"
        tp2_hit = "YES" if max_pct >= 15.0 else "NO"
        now_pct = ((current_p - alert_price) / alert_price * 100.0
                   if (current_p and alert_price > 0) else 0.0)

        scale = _get_price_scale(f"{coin}_USDT")
        ap_s  = f"${alert_price:.{scale}f}"
        pk_s  = f"${peak_p:.{scale}f}" if peak_p > 0 else "N/A"
        np_s  = f"{now_pct:+.1f}%" if current_p else "N/A"

        rows.append(
            f"<code>{coin:<7}| {ap_s:>8} | {pk_s:>8} | {max_pct:>+5.1f}% | {tp1_hit:<3} | {tp2_hit:<3} | {np_s:>6}</code>"
        )
        log.info(f"Backtest {coin}: alert=${alert_price:.4g}  peak=${peak_p:.4g}  max={max_pct:+.1f}%  now={np_s}")
        time.sleep(0.5)  # avoid rate limiting

    return "\n".join(rows)


def send_lpt_test_alert():
    """Build and send one LPT test alert in the new unified format."""
    from modules.momentum_scanner import MomentumResult, TechResult, FundResult

    # Fetch real LPT price from MEXC
    real_price = _current_price("LPT") or 2.3130
    scale      = _get_price_scale("LPT_USDT")

    log.info(f"LPT test alert at price ${real_price:.{scale}f}")

    tech = TechResult(
        h4_ema6=real_price * 0.998,  h4_ema12=real_price * 0.995,
        h4_ema20=real_price * 0.990,
        h4_ema_ok=True,     h4_ema_sep=0.80,
        h4_kdj_j=64.0,      h4_kdj_ok=True,
        macro_ok=True,
        m15_ema6=real_price * 1.002,  m15_ema20=real_price * 0.998,
        m15_ema_ok=True,    m15_ema_pts=15,
        m15_price=real_price,
        m15_price_ok=True,  m15_price_pts=10,
        m15_rsi6=58.0,      m15_rsi6_ok=True,
        m15_rsi6_hot=False, m15_rsi6_pts=10,
        m15_kdj_j=61.0,     m15_kdj_ok=True,
        m15_kdj_hot=False,  m15_kdj_pts=10,
        m15_macd_dif=0.001, m15_macd_dea=0.0005,
        m15_macd_ok=True,   m15_macd_pts=5,
        vol_pct=175.0,      vol_ok=True,   vol_pts=10,
        m15_change=2.1,     m15_golden_cross=False,
        m15_vol_spike=True, m15_vol_spike_ratio=3.2,
        h24_high=real_price * 1.013,   h24_low=real_price * 0.95,
        h16d_high=real_price * 1.25,   ath_dist_pct=82.0,
        score=60,
        m5_ema20=real_price * 0.991,
        m5_rsi6=54.0,
        m5_ema6_gt_ema20=True,
        m5_vol_pct=135.0,
        m15_ema6_gt_ema12=True,
    )
    fund = FundResult(mcap_pts=15, circ_pts=10, fdv_pts=10, gain_pts=5, total=40)

    coin = MomentumResult(
        symbol          = "LPT",
        name            = "Livepeer",
        price           = real_price,
        change_1h       = 4.2,
        change_24h      = 8.7,
        market_cap      = 280_000_000,
        volume_24h      = 22_000_000,
        fdv             = 320_000_000,
        fdv_mcap_ratio  = 1.14,
        circ_supply_pct = 72.0,
        matched_tags    = ["layer-1"],
        mexc_symbol     = "LPT_USDT",
        tech            = tech,
        fund            = fund,
        total_score     = 78,
        recommendation  = "STRONG ENTRY",
        rec_emoji       = "🟢",
        warnings        = [],
        entry_price     = real_price,
        stop_loss       = real_price * 0.95,
        tp1             = real_price * 1.08,
        tp2             = real_price * 1.15,
        risk_usd        = 1.25,
        reward_tp1_usd  = 2.0,
        reward_tp2_usd  = 3.75,
        rr_str          = "1:1.60",
        sl_pct          = 5.0,
    )

    margin   = 5.0
    leverage = 5

    header = (
        "🔔 <b>TEST ALERT — LPT (new unified format)</b>\n"
        "<i>Verifying: sorted levels ✓ 24H High variable ✓ MEXC decimals ✓</i>\n\n"
    )
    text     = header + build_unified_alert(coin, margin, leverage)
    keyboard = _build_coin_keyboard(coin, margin, leverage)
    ok, _    = send_message_with_buttons(text, keyboard)

    if ok:
        log.info("LPT test alert sent successfully.")
    else:
        log.error("LPT test alert FAILED.")
    return ok


if __name__ == "__main__":
    print("=== VALIDATION: Backtest 4 coins + LPT test alert ===\n")

    backtest_text = run_backtest()
    print(backtest_text.replace("<b>", "").replace("</b>", "").replace("<code>", "").replace("</code>", ""))
    print()

    ok = send_message(backtest_text)
    print(f"Backtest results sent to Telegram: {'OK' if ok else 'FAILED'}")
    print()

    print("Sending LPT test alert in new format…")
    send_lpt_test_alert()
    print("Done.")
