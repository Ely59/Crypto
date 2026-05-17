"""
modules/alert_logger.py
Log every alert to CSV and compute weekly hit-rate statistics.

CSV columns:
  timestamp | coin | signal_type | price_at_alert | score | 4h_max_price | hit | roi_percent

hit = True if price rises ≥5% within 4h after alert.
Hit checking uses MEXC 1h klines fetched at weekly report time.
"""

from __future__ import annotations

import csv
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from utils.logger import get_logger
import config as cfg

log = get_logger(__name__)
_BERLIN = ZoneInfo("Europe/Berlin")

_CSV_HEADERS = [
    "timestamp", "coin", "signal_type", "price_at_alert",
    "score", "4h_max_price", "hit", "roi_percent",
]


def _ensure_csv() -> None:
    os.makedirs(os.path.dirname(cfg.ALERT_LOG_CSV), exist_ok=True)
    if not os.path.exists(cfg.ALERT_LOG_CSV):
        with open(cfg.ALERT_LOG_CSV, "w", newline="") as f:
            csv.DictWriter(f, fieldnames=_CSV_HEADERS).writeheader()


def log_alert(coin, signal_type: str) -> None:
    """Write one alert row to the CSV log."""
    _ensure_csv()
    row = {
        "timestamp":      datetime.now(tz=_BERLIN).strftime("%Y-%m-%d %H:%M"),
        "coin":           coin.symbol,
        "signal_type":    signal_type,
        "price_at_alert": coin.entry_price,
        "score":          coin.total_score,
        "4h_max_price":   "",
        "hit":            "",
        "roi_percent":    "",
    }
    try:
        with open(cfg.ALERT_LOG_CSV, "a", newline="") as f:
            csv.DictWriter(f, fieldnames=_CSV_HEADERS).writerow(row)
    except Exception as e:
        log.error(f"alert_logger: write failed: {e}")


def compute_hits_for_pending() -> None:
    """
    For every unprocessed row older than 4h, fetch MEXC 1h klines
    and check if price reached +5% within 4h of the alert.
    """
    import pandas as pd
    from utils.api_client import get_mexc_futures_klines

    _ensure_csv()
    try:
        with open(cfg.ALERT_LOG_CSV, newline="") as f:
            rows = list(csv.DictReader(f))
    except Exception as e:
        log.error(f"alert_logger: read failed: {e}")
        return

    now     = datetime.now(tz=_BERLIN)
    updated = False

    for row in rows:
        if row.get("hit"):
            continue
        try:
            alert_time = datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M").replace(tzinfo=_BERLIN)
        except ValueError:
            continue

        elapsed_h = (now - alert_time).total_seconds() / 3600
        if elapsed_h < 4.0 or elapsed_h > 8 * 24:
            continue

        symbol      = row["coin"] + "_USDT"
        entry_price = float(row.get("price_at_alert") or 0)
        if entry_price <= 0:
            continue

        df = get_mexc_futures_klines(symbol, "1h", limit=200)
        if df is None:
            continue

        alert_ts = pd.Timestamp(alert_time)
        end_ts   = alert_ts + pd.Timedelta(hours=4)
        window   = df[(df.index >= alert_ts) & (df.index <= end_ts)]
        if window.empty:
            continue

        max_price = float(window["high"].max())
        roi       = (max_price - entry_price) / entry_price * 100
        row["4h_max_price"] = f"{max_price:.8f}"
        row["hit"]          = "1" if roi >= 5.0 else "0"
        row["roi_percent"]  = f"{roi:.2f}"
        updated = True
        log.info(f"  Hit check {row['coin']}: ROI {roi:.1f}% → {'HIT ✅' if roi >= 5.0 else 'MISS ❌'}")

    if updated:
        try:
            with open(cfg.ALERT_LOG_CSV, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=_CSV_HEADERS)
                w.writeheader()
                w.writerows(rows)
        except Exception as e:
            log.error(f"alert_logger: rewrite failed: {e}")


def get_weekly_stats(days: int = 7) -> dict:
    """Aggregate hit-rate stats from the CSV for the past `days` days."""
    _ensure_csv()
    try:
        with open(cfg.ALERT_LOG_CSV, newline="") as f:
            rows = list(csv.DictReader(f))
    except Exception:
        return {}

    now    = datetime.now(tz=_BERLIN)
    by_sig: dict[str, dict] = {}
    best   = {"coin": "", "roi": -999.0, "sig": ""}
    worst  = {"coin": "", "roi":  999.0, "sig": ""}

    for row in rows:
        if not row.get("hit"):
            continue
        try:
            alert_time = datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M").replace(tzinfo=_BERLIN)
        except ValueError:
            continue
        if (now - alert_time).days > days:
            continue

        sig  = row["signal_type"]
        hit  = row["hit"] == "1"
        roi  = float(row.get("roi_percent") or 0)
        coin = row["coin"]

        if sig not in by_sig:
            by_sig[sig] = {"total": 0, "hits": 0, "best_roi": -999.0, "best_coin": "", "worst_roi": 999.0, "worst_coin": ""}
        s = by_sig[sig]
        s["total"] += 1
        if hit:
            s["hits"] += 1
        if roi > s["best_roi"]:
            s["best_roi"]  = roi
            s["best_coin"] = coin
        if roi < s["worst_roi"]:
            s["worst_roi"]  = roi
            s["worst_coin"] = coin
        if roi > best["roi"]:
            best = {"coin": coin, "roi": roi, "sig": sig}
        if roi < worst["roi"]:
            worst = {"coin": coin, "roi": roi, "sig": sig}

    return {"by_signal": by_sig, "best": best, "worst": worst, "period_days": days}
