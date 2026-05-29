"""
modules/alert_logger.py
Log every alert and compute weekly hit-rate statistics.

Storage backend:
  - PostgreSQL when DATABASE_URL env var is set (Railway)
  - CSV fallback (logs/alert_log.csv) otherwise

Schema columns:
  timestamp | coin | signal_type | price_at_alert | score |
  pattern_type | leg_number | 4h_max_price | hit | roi_percent
"""

from __future__ import annotations

import csv
import os
import time as _time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from utils.logger import get_logger
import config as cfg

log = get_logger(__name__)
_BERLIN = ZoneInfo("Europe/Berlin")

_CSV_HEADERS = [
    "timestamp", "coin", "signal_type", "price_at_alert",
    "score", "pattern_type", "leg_number",
    "4h_max_price", "hit", "roi_percent",
]

# SELECT clause listing columns in _CSV_HEADERS order (used in all DB queries)
_SELECT_COLS = (
    'timestamp, coin, signal_type, price_at_alert, score, '
    'pattern_type, leg_number, "4h_max_price", hit, roi_percent'
)

_DATABASE_URL: str = (
    os.getenv("DATABASE_URL", "").strip().replace("postgres://", "postgresql://", 1)
)
_csv_ready: bool = False
_db_ready:  bool = False


# ── Storage helpers ───────────────────────────────────────────────────────────

def _use_db() -> bool:
    return bool(_DATABASE_URL)


def _get_conn():
    import psycopg2
    return psycopg2.connect(_DATABASE_URL)


def _ensure_db() -> None:
    global _db_ready
    if _db_ready:
        return
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS alerts (
                    id             SERIAL PRIMARY KEY,
                    timestamp      TEXT NOT NULL,
                    coin           TEXT,
                    signal_type    TEXT,
                    price_at_alert TEXT,
                    score          TEXT,
                    pattern_type   TEXT,
                    leg_number     TEXT,
                    "4h_max_price" TEXT,
                    hit            TEXT,
                    roi_percent    TEXT
                )
            """)
        conn.commit()
        conn.close()
        _db_ready = True
        log.info("alert_logger: PostgreSQL alerts table ready")
    except Exception as e:
        log.error(f"alert_logger: DB setup failed: {e}")


def _ensure_csv() -> None:
    """Create the CSV with correct headers, or migrate an existing file to add new columns."""
    global _csv_ready
    if _csv_ready:
        return

    os.makedirs(os.path.dirname(cfg.ALERT_LOG_CSV), exist_ok=True)

    if not os.path.exists(cfg.ALERT_LOG_CSV):
        with open(cfg.ALERT_LOG_CSV, "w", newline="") as f:
            csv.DictWriter(f, fieldnames=_CSV_HEADERS).writeheader()
        _csv_ready = True
        return

    try:
        with open(cfg.ALERT_LOG_CSV, newline="") as f:
            reader = csv.DictReader(f)
            existing = set(reader.fieldnames or [])
            missing  = [c for c in _CSV_HEADERS if c not in existing]
            if not missing:
                _csv_ready = True
                return
            rows = list(reader)

        with open(cfg.ALERT_LOG_CSV, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=_CSV_HEADERS, restval="", extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        log.info(f"alert_logger: migrated CSV — added columns {missing} ({len(rows)} existing rows preserved)")
    except Exception as e:
        log.error(f"alert_logger: CSV setup/migration failed: {e}")

    _csv_ready = True


def _init_storage() -> None:
    if _use_db():
        _ensure_db()
    else:
        _ensure_csv()


def _rows_to_dicts(rows: list) -> list[dict]:
    """Convert raw psycopg2 rows (fetched in _SELECT_COLS order) to CSV-compatible dicts."""
    return [{k: ("" if v is None else str(v)) for k, v in zip(_CSV_HEADERS, row)} for row in rows]


# ── Public API ────────────────────────────────────────────────────────────────

def log_alert(coin, signal_type: str) -> None:
    """Write one alert row. Uses PostgreSQL when DATABASE_URL is set, CSV otherwise."""
    _init_storage()
    ts = datetime.now(tz=_BERLIN).strftime("%Y-%m-%d %H:%M")
    if _use_db():
        try:
            conn = _get_conn()
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO alerts
                       (timestamp, coin, signal_type, price_at_alert, score,
                        pattern_type, leg_number, "4h_max_price", hit, roi_percent)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        ts,
                        coin.symbol,
                        signal_type,
                        str(coin.entry_price),
                        str(coin.total_score),
                        str(getattr(coin, "pattern_type", "")),
                        str(getattr(coin, "leg_number", 1)),
                        "", "", "",
                    ),
                )
            conn.commit()
            conn.close()
        except Exception as e:
            log.error(f"alert_logger: DB write failed: {e}")
    else:
        row = {
            "timestamp":      ts,
            "coin":           coin.symbol,
            "signal_type":    signal_type,
            "price_at_alert": coin.entry_price,
            "score":          coin.total_score,
            "pattern_type":   getattr(coin, "pattern_type",  ""),
            "leg_number":     getattr(coin, "leg_number",    1),
            "4h_max_price":   "",
            "hit":            "",
            "roi_percent":    "",
        }
        try:
            with open(cfg.ALERT_LOG_CSV, "a", newline="") as f:
                csv.DictWriter(f, fieldnames=_CSV_HEADERS, extrasaction="ignore").writerow(row)
        except Exception as e:
            log.error(f"alert_logger: write failed: {e}")


def compute_hits_for_pending() -> None:
    """
    For every unprocessed row older than 4h, fetch MEXC 1h klines
    and check if price reached +5% within 4h of the alert.
    """
    import pandas as pd
    from utils.api_client import get_mexc_futures_klines

    _init_storage()
    now = datetime.now(tz=_BERLIN)

    if _use_db():
        try:
            conn = _get_conn()
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, timestamp, coin, price_at_alert FROM alerts "
                    "WHERE hit IS NULL OR hit = ''"
                )
                pending = cur.fetchall()
            conn.close()
        except Exception as e:
            log.error(f"alert_logger: DB read failed: {e}")
            return

        for row_id, ts_str, coin_sym, price_str in pending:
            try:
                alert_time = datetime.strptime(ts_str, "%Y-%m-%d %H:%M").replace(tzinfo=_BERLIN)
            except ValueError:
                continue
            elapsed_h = (now - alert_time).total_seconds() / 3600
            if elapsed_h < 4.0 or elapsed_h > 8 * 24:
                continue
            entry_price = float(price_str or 0)
            if entry_price <= 0:
                continue

            df = get_mexc_futures_klines(coin_sym + "_USDT", "1h", limit=200)
            if df is None:
                continue

            alert_ts = pd.Timestamp(alert_time)
            window   = df[(df.index >= alert_ts) & (df.index <= alert_ts + pd.Timedelta(hours=4))]
            if window.empty:
                continue

            max_price = float(window["high"].max())
            roi       = (max_price - entry_price) / entry_price * 100
            hit       = "1" if roi >= 5.0 else "0"
            log.info(f"  Hit check {coin_sym}: ROI {roi:.1f}% → {'HIT ✅' if roi >= 5.0 else 'MISS ❌'}")
            try:
                conn = _get_conn()
                with conn.cursor() as cur:
                    cur.execute(
                        'UPDATE alerts SET "4h_max_price"=%s, hit=%s, roi_percent=%s WHERE id=%s',
                        (f"{max_price:.8f}", hit, f"{roi:.2f}", row_id),
                    )
                conn.commit()
                conn.close()
            except Exception as e:
                log.error(f"alert_logger: DB update failed: {e}")
        return

    # CSV path
    try:
        with open(cfg.ALERT_LOG_CSV, newline="") as f:
            rows = list(csv.DictReader(f))
    except Exception as e:
        log.error(f"alert_logger: read failed: {e}")
        return

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
        window   = df[(df.index >= alert_ts) & (df.index <= alert_ts + pd.Timedelta(hours=4))]
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
                w = csv.DictWriter(f, fieldnames=_CSV_HEADERS, restval="", extrasaction="ignore")
                w.writeheader()
                w.writerows(rows)
        except Exception as e:
            log.error(f"alert_logger: rewrite failed: {e}")


def get_recent_alerts(hours: int = 24) -> list:
    """
    Return the best alert per coin from the last `hours` hours, sorted by score desc.
    Used by the daily briefing to show yesterday's top signals.
    """
    _init_storage()
    cutoff     = datetime.now(tz=_BERLIN) - timedelta(hours=hours)
    cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M")

    if _use_db():
        try:
            conn = _get_conn()
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {_SELECT_COLS} FROM alerts WHERE timestamp >= %s ORDER BY timestamp",
                    (cutoff_str,),
                )
                rows = _rows_to_dicts(cur.fetchall())
            conn.close()
        except Exception as e:
            log.error(f"alert_logger: DB read failed: {e}")
            return []
    else:
        try:
            with open(cfg.ALERT_LOG_CSV, newline="") as f:
                all_rows = list(csv.DictReader(f))
        except Exception:
            return []
        rows = [r for r in all_rows if r.get("timestamp", "") >= cutoff_str]

    seen: dict[str, dict] = {}
    for row in rows:
        coin = row.get("coin", "")
        if not coin:
            continue
        prev = seen.get(coin)
        if prev is None or int(row.get("score") or 0) > int(prev.get("score") or 0):
            seen[coin] = row
    return sorted(seen.values(), key=lambda r: -int(r.get("score") or 0))


def get_alerts_for_date(date_str: str) -> list[dict]:
    """
    Return all rows whose timestamp date (Berlin local) matches date_str (YYYY-MM-DD).
    Rows are returned in chronological order.
    """
    _init_storage()
    if _use_db():
        try:
            conn = _get_conn()
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {_SELECT_COLS} FROM alerts WHERE timestamp LIKE %s ORDER BY timestamp",
                    (date_str + "%",),
                )
                rows = _rows_to_dicts(cur.fetchall())
            conn.close()
            return rows
        except Exception as e:
            log.error(f"alert_logger: DB read failed: {e}")
            return []

    try:
        with open(cfg.ALERT_LOG_CSV, newline="") as f:
            rows = list(csv.DictReader(f))
    except Exception:
        return []
    return [r for r in rows if r.get("timestamp", "")[:10] == date_str]


def get_weekly_stats(days: int = 7) -> dict:
    """Aggregate hit-rate stats from the past `days` days."""
    _init_storage()
    now        = datetime.now(tz=_BERLIN)
    cutoff_str = (now - timedelta(days=days)).strftime("%Y-%m-%d %H:%M")

    if _use_db():
        try:
            conn = _get_conn()
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT timestamp, coin, signal_type, hit, roi_percent "
                    "FROM alerts WHERE (hit='0' OR hit='1') AND timestamp >= %s",
                    (cutoff_str,),
                )
                rows = [
                    {"timestamp": r[0], "coin": r[1], "signal_type": r[2],
                     "hit": r[3], "roi_percent": r[4]}
                    for r in cur.fetchall()
                ]
            conn.close()
        except Exception as e:
            log.error(f"alert_logger: DB read failed: {e}")
            return {}
    else:
        try:
            with open(cfg.ALERT_LOG_CSV, newline="") as f:
                all_rows = list(csv.DictReader(f))
        except Exception:
            return {}
        rows = []
        for row in all_rows:
            if not row.get("hit"):
                continue
            try:
                alert_time = datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M").replace(tzinfo=_BERLIN)
            except ValueError:
                continue
            if (now - alert_time).days <= days:
                rows.append(row)

    by_sig: dict[str, dict] = {}
    best   = {"coin": "", "roi": -999.0, "sig": ""}
    worst  = {"coin": "", "roi":  999.0, "sig": ""}

    for row in rows:
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


# ── Backtesting engine ────────────────────────────────────────────────────────

_VERDICT_ORDER = ("GOOD", "LATE", "FALSE", "LEG2", "NEUTRAL")


def _classify_verdict(
    pct_1h:   float | None,
    pct_4h:   float | None,
    pct_24h:  float | None,
    dip_pct:  float,
    tp1_pct:  float = 5.0,
) -> str:
    """
    Classify a single alert outcome using pattern-appropriate TP1 threshold.
      GOOD    — up >= tp1_pct within 4H  (default 5%; GRIND uses 6%)
      FALSE   — down >3% within 4H
      LEG2    — dipped >6% then recovered above entry within 24H
      LATE    — flat/weak at 1H, position already missed or reversed early
      NEUTRAL — everything else
    """
    if pct_4h is not None and pct_4h >= tp1_pct:
        return "GOOD"
    if pct_4h is not None and pct_4h <= -3.0:
        return "FALSE"
    if dip_pct >= 6.0 and pct_24h is not None and pct_24h > 0.0:
        return "LEG2"
    if pct_1h is not None and pct_1h <= 0.5 and pct_4h is not None and pct_4h < tp1_pct:
        return "LATE"
    return "NEUTRAL"


def run_backtesting(date_str: str) -> dict:
    """
    For a given date (YYYY-MM-DD Berlin local), reads all alerts logged that day
    and fetches MEXC 1H klines to compute prices at +1H, +4H, +24H.

    Returns:
      {
        "date":     str,
        "entries":  list[dict],   # one per alert, see keys below
        "error":    str | None,   # non-None on fatal error (bad date, etc.)
        "has_data": bool,
      }

    Each entry dict keys:
      symbol, time_str, sig_type, pat_type, leg_num, score,
      entry, pct_1h, pct_4h, pct_24h, dip_pct, verdict
    """
    import pandas as pd
    from utils.api_client import get_mexc_futures_klines

    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return {
            "date": date_str, "entries": [], "has_data": False,
            "error": f"Invalid date format '{date_str}' — use YYYY-MM-DD (e.g. 2026-05-26)",
        }

    alerts = get_alerts_for_date(date_str)
    if not alerts:
        return {"date": date_str, "entries": [], "has_data": False, "error": None}

    now_utc = datetime.now(timezone.utc)
    entries: list[dict] = []

    for row in alerts:
        symbol = row.get("coin", "").strip()
        try:
            sig_type = row.get("signal_type", "")
            pat_type = row.get("pattern_type", "")
            try:
                leg_num = int(row.get("leg_number") or 1)
            except (ValueError, TypeError):
                leg_num = 1
            try:
                score = int(row.get("score") or 0)
            except (ValueError, TypeError):
                score = 0
            try:
                entry_price = float(row.get("price_at_alert") or 0)
            except (ValueError, TypeError):
                continue
            if entry_price <= 0 or not symbol:
                continue

            try:
                alert_berlin = datetime.strptime(
                    row["timestamp"], "%Y-%m-%d %H:%M"
                ).replace(tzinfo=_BERLIN)
            except ValueError:
                continue

            alert_utc  = alert_berlin.astimezone(timezone.utc)
            time_str   = alert_berlin.strftime("%H:%M")
            start_unix = int(alert_utc.timestamp())
            mexc_sym   = f"{symbol}_USDT"

            df = get_mexc_futures_klines(
                mexc_sym, "1h", limit=30,
                start_time=start_unix,
                min_candles=1,
            )
            _time.sleep(0.25)

            def _price_at(offset_h: int) -> float | None:
                target_utc = alert_utc + timedelta(hours=offset_h)
                if now_utc < target_utc:
                    return None
                if df is None or df.empty:
                    return None
                target_pd = pd.Timestamp(target_utc)
                pos = df.index.searchsorted(target_pd)
                pos = min(pos, len(df) - 1)
                return float(df["close"].iloc[pos])

            def _pct(price: float | None) -> float | None:
                if price is None or entry_price <= 0:
                    return None
                return (price - entry_price) / entry_price * 100.0

            p1h  = _price_at(1)
            p4h  = _price_at(4)
            p24h = _price_at(24)
            pct1  = _pct(p1h)
            pct4  = _pct(p4h)
            pct24 = _pct(p24h)

            dip_pct = 0.0
            if df is not None and not df.empty:
                alert_pd = pd.Timestamp(alert_utc)
                end_pd   = pd.Timestamp(alert_utc + timedelta(hours=24))
                w24 = df[(df.index >= alert_pd) & (df.index <= end_pd)]
                if not w24.empty:
                    min_low = float(w24["low"].min())
                    dip_pct = max(0.0, (entry_price - min_low) / entry_price * 100.0)

            tp1_pct = 6.0 if pat_type == "GRIND" else 5.0
            verdict = _classify_verdict(pct1, pct4, pct24, dip_pct, tp1_pct=tp1_pct)

            entries.append({
                "symbol":   symbol,
                "time_str": time_str,
                "sig_type": sig_type,
                "pat_type": pat_type,
                "leg_num":  leg_num,
                "score":    score,
                "entry":    entry_price,
                "pct_1h":   round(pct1,  2) if pct1  is not None else None,
                "pct_4h":   round(pct4,  2) if pct4  is not None else None,
                "pct_24h":  round(pct24, 2) if pct24 is not None else None,
                "dip_pct":  round(dip_pct, 2),
                "verdict":  verdict,
            })
        except Exception as exc:
            log.warning(f"backtesting: skipping {symbol} — {exc}")

    return {"date": date_str, "entries": entries, "has_data": True, "error": None}
