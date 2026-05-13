"""
modules/stats_tracker.py
Thread-safe daily scan statistics — updated after every scan, read for
the 08:01 daily summary and the /status Telegram command.
"""

from __future__ import annotations

import copy
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

_BERLIN = ZoneInfo("Europe/Berlin")
_UTC    = timezone.utc

# Scan schedule: :02/:17/:32/:47 each hour UTC
_SCAN_MINUTES = [2, 17, 32, 47]


def _minutes_to_next_scan() -> int:
    """Return whole minutes until the next scheduled momentum scan."""
    now  = datetime.now(tz=_UTC)
    mins = now.minute
    for m in _SCAN_MINUTES:
        if m > mins:
            return m - mins
    return 60 - mins + _SCAN_MINUTES[0]


@dataclass
class DailyStats:
    date:           date       = field(default_factory=date.today)
    scan_count:     int        = 0
    coins_analyzed: int        = 0   # cumulative M1-M7 passes across all scans
    macro_blocked:  int        = 0   # cumulative 4H macro gate rejections
    entry_alerts:   int        = 0   # STRONG ENTRY alerts sent
    watch_alerts:   int        = 0   # WATCH alerts sent
    early_alerts:   int        = 0   # EARLY SIGNAL alerts sent
    cooling_alerts: int        = 0   # COOLING_DOWN (4H KDJ overheated) alerts sent
    best_coin:      str        = ""
    best_score:     int        = 0
    last_scan_ts:   str        = ""  # HH:MM Berlin time of most recent scan
    last_results:   list[str]  = field(default_factory=list)
    top_coins:      list       = field(default_factory=list)  # [(symbol, score, emoji), ...]


class StatsTracker:
    """
    Singleton stats store for Module 5. Updated by main.run_momentum_scan()
    after every successful scan. Thread-safe via Lock.
    """

    def __init__(self) -> None:
        self._lock  = threading.Lock()
        self._stats = DailyStats()

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _ensure_today(self) -> None:
        """Silently roll over to a fresh DailyStats at midnight Berlin time."""
        today = datetime.now(tz=_BERLIN).date()
        if self._stats.date != today:
            self._stats = DailyStats(date=today)

    # ── Public API ────────────────────────────────────────────────────────────

    def record_scan(self, results: list, m1m7_count: int, macro_blocked: int = 0) -> None:
        """
        Call after every successful m5.scan().
        results       — list[MomentumResult] returned by scan()
        m1m7_count    — Stage-1 candidates (from m5._last_m1m7_count)
        macro_blocked — coins blocked at 4H macro gate (from m5._last_macro_blocked)
        """
        now_str = datetime.now(tz=_BERLIN).strftime("%H:%M")
        with self._lock:
            self._ensure_today()
            s = self._stats
            s.scan_count     += 1
            s.coins_analyzed += m1m7_count
            s.macro_blocked  += macro_blocked
            s.last_scan_ts    = now_str

            snapshot: list[str] = []
            for r in results:
                rec = r.recommendation
                if rec == "STRONG ENTRY":
                    s.entry_alerts += 1
                elif rec == "WATCH":
                    s.watch_alerts += 1
                elif rec == "EARLY SIGNAL":
                    s.early_alerts += 1
                elif rec == "COOLING_DOWN":
                    s.cooling_alerts += 1

                # Track best total score (ignore COOLING which has score 0)
                if rec not in ("COOLING_DOWN", "EARLY SIGNAL") and r.total_score > s.best_score:
                    s.best_score = r.total_score
                    s.best_coin  = r.symbol

                # Update top-3 leaderboard (non-COOLING only)
                if rec not in ("COOLING_DOWN", "EARLY SIGNAL"):
                    entry = (r.symbol, r.total_score, r.rec_emoji)
                    s.top_coins.append(entry)
                    s.top_coins.sort(key=lambda x: x[1], reverse=True)
                    s.top_coins = s.top_coins[:3]

                # Build last-results snapshot for /status
                if rec == "COOLING_DOWN":
                    kdj = f"{r.tech.h4_kdj_j:.1f}" if r.tech else "?"
                    snapshot.append(
                        f"⏳ {r.symbol} {r.change_1h:+.2f}% — KDJ {kdj} (cooling)"
                    )
                else:
                    snapshot.append(
                        f"{r.rec_emoji} {r.symbol} {r.change_1h:+.2f}%"
                        f" — {rec} ({r.total_score}/100)"
                    )

            s.last_results = snapshot

    def reset(self) -> None:
        """Reset stats after the daily summary has been sent."""
        with self._lock:
            self._stats = DailyStats(date=datetime.now(tz=_BERLIN).date())

    def get_daily_summary(self) -> DailyStats:
        """Return a snapshot of today's stats (safe copy — caller may read freely)."""
        with self._lock:
            self._ensure_today()
            return copy.deepcopy(self._stats)

    def get_status(self) -> str:
        """Build the plain-text body for the Telegram /status reply."""
        with self._lock:
            self._ensure_today()
            s = self._stats

            next_min = _minutes_to_next_scan()

            if not s.scan_count:
                return (
                    "No scans completed today yet.\n"
                    f"Next scan in <b>{next_min} min</b>."
                )

            total_alerts = s.entry_alerts + s.watch_alerts + s.early_alerts
            lines = [
                f"Last scan: <b>{s.last_scan_ts}</b> Berlin  |  Next in <b>{next_min} min</b>",
                f"Scans today: <b>{s.scan_count}</b>",
                "",
                f"M1–M7 passed: <b>{s.coins_analyzed}</b>  |  4H blocked: <b>{s.macro_blocked}</b>",
                f"Alerts sent: <b>{total_alerts}</b>"
                f"  (Entry: {s.entry_alerts} | Watch: {s.watch_alerts} | Early: {s.early_alerts} | Cooling: {s.cooling_alerts})",
            ]

            if s.top_coins:
                lines += ["", "🏆 Top coins today:"]
                for i, (sym, score, emoji) in enumerate(s.top_coins, 1):
                    lines.append(f"  {i}. {emoji} <b>{sym}</b> — {score}/100")

            if s.best_coin:
                lines.append(f"\nBest score: <b>{s.best_coin} {s.best_score}/100</b>")

            if s.last_results:
                lines += ["", "Last scan:"]
                lines.extend(f"  {r}" for r in s.last_results)
            else:
                lines += ["", "Last scan: no qualifying coins."]

            return "\n".join(lines)


# Module-level singleton imported by main.py and telegram_alerts.py
tracker = StatsTracker()
