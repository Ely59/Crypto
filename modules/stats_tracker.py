"""
modules/stats_tracker.py
Thread-safe daily scan statistics — updated after every scan, read for
the 08:01 daily summary and the /status Telegram command.
"""

from __future__ import annotations

import copy
import threading
from collections import deque
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
    gc_alerts:      int        = 0   # GOLDEN CROSS alerts sent
    vs_alerts:      int        = 0   # VOLUME SPIKE pre-signal alerts sent
    rb_alerts:      int        = 0   # RECOVERY BOUNCE alerts sent
    cooling_alerts: int        = 0   # COOLING_DOWN (4H KDJ overheated) alerts sent
    pbw_alerts:     int        = 0   # PRE-BREAKOUT Watch alerts sent
    sc_alerts:      int        = 0   # STAIRCASE Continuation alerts sent
    sq_alerts:      int        = 0   # SQUEEZE BREAKOUT alerts sent
    best_coin:      str        = ""
    best_score:     int        = 0
    last_scan_ts:   str        = ""  # HH:MM Berlin time of most recent scan
    last_results:   list[str]  = field(default_factory=list)
    top_coins:      list       = field(default_factory=list)  # [(symbol, score, emoji), ...]
    top_results:    list       = field(default_factory=list)  # up to 3 best MomentumResult objects

    # Stage 2a block breakdown (CHANGE 2C)
    s2a_ema_bearish:   int  = 0
    s2a_sep_small:     int  = 0
    s2a_15m_gate:      int  = 0
    s2a_fear_bypassed: int  = 0
    s2a_squeeze:       int  = 0
    fear_mode_active:  bool = False
    fear_greed_value:  int  = 50


@dataclass
class ScanSnapshot:
    """One complete scan cycle — stored in rolling 3-scan history for /coins and /explain."""
    timestamp: str   # HH:MM Berlin time
    results:   list  # list[MomentumResult]
    outcomes:  list  # list[CandidateOutcome]


class StatsTracker:
    """
    Singleton stats store for Module 5. Updated by main.run_momentum_scan()
    after every successful scan. Thread-safe via Lock.
    """

    def __init__(self) -> None:
        self._lock         = threading.Lock()
        self._stats        = DailyStats()
        self._scan_history: deque = deque(maxlen=3)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _ensure_today(self) -> None:
        """Silently roll over to a fresh DailyStats at midnight Berlin time."""
        today = datetime.now(tz=_BERLIN).date()
        if self._stats.date != today:
            self._stats = DailyStats(date=today)

    # ── Public API ────────────────────────────────────────────────────────────

    def record_scan(self, results: list, m1m7_count: int, macro_blocked: int = 0, outcomes: list = None,
                    s2a_ema_bearish: int = 0, s2a_sep_small: int = 0, s2a_15m_gate: int = 0,
                    s2a_fear_bypassed: int = 0, s2a_squeeze: int = 0,
                    fear_mode_active: bool = False, fear_greed_value: int = 50) -> None:
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
            # Stage 2a block breakdown
            s.s2a_ema_bearish   += s2a_ema_bearish
            s.s2a_sep_small     += s2a_sep_small
            s.s2a_15m_gate      += s2a_15m_gate
            s.s2a_fear_bypassed += s2a_fear_bypassed
            s.s2a_squeeze       += s2a_squeeze
            s.fear_mode_active   = fear_mode_active
            s.fear_greed_value   = fear_greed_value

            snapshot: list[str] = []
            for r in results:
                rec = r.recommendation
                if rec == "STRONG ENTRY":
                    s.entry_alerts += 1
                elif rec == "WATCH":
                    s.watch_alerts += 1
                elif rec == "EARLY SIGNAL":
                    s.early_alerts += 1
                elif rec == "GOLDEN CROSS":
                    s.gc_alerts += 1
                elif rec == "VOLUME SPIKE":
                    s.vs_alerts += 1
                elif rec == "RECOVERY":
                    s.rb_alerts += 1
                elif rec == "COOLING_DOWN":
                    s.cooling_alerts += 1
                elif rec == "PRE-BREAKOUT":
                    s.pbw_alerts += 1
                elif rec == "STAIRCASE":
                    s.sc_alerts += 1
                elif rec == "SQUEEZE":
                    s.sq_alerts += 1

                _no_score = {"COOLING_DOWN", "EARLY SIGNAL", "GOLDEN CROSS", "VOLUME SPIKE", "RECOVERY"}

                # Track best total score (signal-only types have no score)
                if rec not in _no_score and r.total_score > s.best_score:
                    s.best_score = r.total_score
                    s.best_coin  = r.symbol

                # Update top-3 leaderboard (scored coins only)
                if rec not in _no_score:
                    entry = (r.symbol, r.total_score, r.rec_emoji)
                    s.top_coins.append(entry)
                    s.top_coins.sort(key=lambda x: x[1], reverse=True)
                    s.top_coins = s.top_coins[:3]

                # Build last-results snapshot for /status
                if rec == "COOLING_DOWN":
                    kdj = f"{r.tech.h4_kdj_j:.1f}" if r.tech else "?"
                    snapshot.append(f"⏳ {r.symbol} {r.change_1h:+.2f}% — KDJ {kdj} (cooling)")
                elif rec == "GOLDEN CROSS":
                    snapshot.append(f"⚡ {r.symbol} {r.change_1h:+.2f}% — GOLDEN CROSS")
                elif rec == "VOLUME SPIKE":
                    ratio = f"{r.tech.m15_vol_spike_ratio:.1f}×" if r.tech else "?"
                    snapshot.append(f"⚡ {r.symbol} {r.change_1h:+.2f}% — VOL SPIKE {ratio}")
                elif rec == "RECOVERY":
                    snapshot.append(f"♻️ {r.symbol} {r.change_1h:+.2f}% — RECOVERY")
                elif rec == "SQUEEZE":
                    ratio = f"{r.tech.m15_vol_spike_ratio:.1f}×" if r.tech else "?"
                    snapshot.append(f"💥 {r.symbol} {r.change_1h:+.2f}% — SQUEEZE {ratio}")
                else:
                    snapshot.append(
                        f"{r.rec_emoji} {r.symbol} {r.change_1h:+.2f}%"
                        f" — {rec} ({r.total_score}/100)"
                    )

            s.last_results = snapshot

            # Rolling scan history for /coins and /explain commands
            snap = ScanSnapshot(
                timestamp = now_str,
                results   = list(results),
                outcomes  = list(outcomes or []),
            )
            self._scan_history.append(snap)

            # Today's top scored coins for /top and /best commands
            _no_score_recs = {"COOLING_DOWN", "EARLY SIGNAL", "GOLDEN CROSS", "VOLUME SPIKE", "RECOVERY"}
            for r in results:
                if r.recommendation not in _no_score_recs:
                    s.top_results.append(r)
            s.top_results.sort(key=lambda r: r.total_score, reverse=True)
            s.top_results = s.top_results[:3]

    def reset(self) -> None:
        """Reset stats after the daily summary has been sent."""
        with self._lock:
            self._stats = DailyStats(date=datetime.now(tz=_BERLIN).date())
            self._scan_history.clear()

    def get_daily_summary(self) -> DailyStats:
        """Return a snapshot of today's stats (safe copy — caller may read freely)."""
        with self._lock:
            self._ensure_today()
            return copy.deepcopy(self._stats)

    def get_scan_history(self) -> list:
        """Return up to last 3 ScanSnapshot objects (oldest first)."""
        with self._lock:
            return list(self._scan_history)

    def get_top_results(self) -> list:
        """Return today's top 3 MomentumResult objects by total_score."""
        with self._lock:
            self._ensure_today()
            return list(self._stats.top_results)

    def get_status(self) -> str:
        """Build the plain-text body for the Telegram /status reply."""
        with self._lock:
            self._ensure_today()
            s = self._stats

            next_min = _minutes_to_next_scan()

            fear_banner = (
                f"😟 <b>Fear Mode ACTIVE</b> (F&G: {s.fear_greed_value}) — "
                f"Stage 2a relaxed to 0.05% sep\n"
                if s.fear_mode_active else ""
            )

            if not s.scan_count:
                return (
                    fear_banner +
                    "No scans completed today yet.\n"
                    f"Next scan in <b>{next_min} min</b>."
                )

            total_alerts = (s.entry_alerts + s.watch_alerts + s.early_alerts +
                            s.gc_alerts + s.vs_alerts + s.rb_alerts +
                            s.pbw_alerts + s.sc_alerts + s.sq_alerts)
            lines = []
            if s.fear_mode_active:
                lines.append(f"😟 <b>Fear Mode ACTIVE</b> (F&G: {s.fear_greed_value}) — Stage 2a relaxed to 0.05% sep")
            lines += [
                f"Last scan: <b>{s.last_scan_ts}</b> Berlin  |  Next in <b>{next_min} min</b>",
                f"Scans today: <b>{s.scan_count}</b>",
                "",
                f"M1–M7 passed: <b>{s.coins_analyzed}</b>  |  4H blocked: <b>{s.macro_blocked}</b>",
                f"  └ EMA bearish: {s.s2a_ema_bearish} | Sep &lt;0.2%: {s.s2a_sep_small} | "
                f"15m gate: {s.s2a_15m_gate} | Fear bypass: {s.s2a_fear_bypassed} | "
                f"Squeeze: {s.s2a_squeeze}",
                f"Alerts sent: <b>{total_alerts}</b>  "
                f"(Entry: {s.entry_alerts} | Watch: {s.watch_alerts} | Early: {s.early_alerts} "
                f"| GC: {s.gc_alerts} | VS: {s.vs_alerts} | RB: {s.rb_alerts} "
                f"| PBW: {s.pbw_alerts} | SC: {s.sc_alerts} | SQ: {s.sq_alerts} | Cooling: {s.cooling_alerts})",
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

            # Global 4H cooldown display (CHANGE 5C)
            try:
                from modules.momentum_scanner import get_global_cooldown_status
                cooldowns = get_global_cooldown_status()
                if cooldowns:
                    def _fmt_cd(secs: float) -> str:
                        m = int(secs // 60)
                        return f"{m // 60}h {m % 60:02d}m"
                    cd_parts = [f"<b>{sym}</b> ({_fmt_cd(secs)} left)" for sym, secs, _ in cooldowns]
                    lines += ["", f"⏳ On cooldown (4H): {', '.join(cd_parts)}"]
            except Exception:
                pass

            return "\n".join(lines)


# Module-level singleton imported by main.py and telegram_alerts.py
tracker = StatsTracker()
