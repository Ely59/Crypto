"""
main.py — Entry point for the Crypto Ecosystem.

Usage:
  python main.py              # send one briefing immediately (testing)
  python main.py --schedule   # start the persistent scheduler

Scheduled jobs:
  08:00 Europe/Berlin  — daily briefing (Module 1 + 2)
  08:01 Europe/Berlin  — Module 5 daily summary, then stats reset
  15:30 Europe/Berlin  — US market open reminder
  Every 1H at :05 UTC  — BTC Phase 1 direction detection
  Every 15M :00/:15/:30/:45 UTC  — BTC Phase 2 entry timing
  Every 15M :02/:17/:32/:47 UTC  — Module 5 momentum scanner

Stuttgart = Europe/Berlin timezone.
APScheduler uses this directly, so CET↔CEST transitions are handled automatically.
"""

import argparse
import asyncio
import threading
from datetime import datetime, timedelta

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron       import CronTrigger
from apscheduler.triggers.date       import DateTrigger

import modules.btc_context_analyzer as m1
import modules.altcoin_scout        as m2
import modules.btc_trading_support  as m3
import modules.telegram_alerts      as m4
import modules.momentum_scanner     as m5
from modules.stats_tracker import tracker
import config as cfg
from utils.logger import get_logger

log = get_logger("main")

_BERLIN_TZ = "Europe/Berlin"

# ── Shared state — BTC two-phase pipeline ─────────────────────────────────────
_current_btc_setup: "m3.TradeSetup | None" = None

# ── Module 5 resilience ───────────────────────────────────────────────────────
_consecutive_failures: int = 0
_scheduler: BlockingScheduler | None = None   # set by start_scheduler(); used for retry jobs


# ══════════════════════════════════════════════════════════════════════════════
# Job functions — daily briefing
# ══════════════════════════════════════════════════════════════════════════════

def run_daily_briefing():
    """
    Morning briefing job — runs every day at 08:00 Stuttgart time.

    Execution order:
      1. Module 1 — BTC context (regime, price, RSI, EMAs, Fear & Greed)
      2. Module 2 — Altcoin Scout (skipped automatically if BTC regime is BEAR)
         • Level-2 coins fire an instant Trade Alert immediately
         • Level-1 coins appear only in the briefing watchlist
      3. Send the combined daily briefing message
    """
    log.info("═══ Daily Briefing: starting ═══")

    btc_context = m1.analyze()
    if btc_context is None:
        log.error("Module 1 failed — skipping today's briefing.")
        return

    scout_results, avoid_coins = m2.full_scan(btc_context)

    for coin in scout_results:
        if coin.alert_level == 2:
            m4.send_scout_alert(coin, level=2)

    m4.send_daily_briefing(btc_context, scout_results, avoid_coins)

    log.info("═══ Daily Briefing: complete ═══")


def run_full_analysis():
    """
    Full analysis run — used when all modules are active.
    Currently wired up for manual / test use via `python main.py`.
    The daily scheduler uses run_daily_briefing() which is lighter.
    """
    log.info("═══ Full analysis: starting ═══")

    btc_context                  = m1.analyze()
    scout_results, avoid_coins   = m2.full_scan(btc_context)
    btc_trade                    = m3.analyze()

    if btc_context is None:
        log.error("Module 1 failed — aborting.")
        return

    for coin in scout_results:
        if coin.alert_level == 2:
            m4.send_scout_alert(coin, level=2)

    m4.send_daily_briefing(btc_context, scout_results, avoid_coins)

    if btc_trade and btc_trade.direction != "NO_SETUP":
        m4.send_btc_alert(btc_trade)

    log.info("═══ Full analysis: complete ═══")


# ══════════════════════════════════════════════════════════════════════════════
# Job functions — BTC two-phase pipeline
# ══════════════════════════════════════════════════════════════════════════════

def run_btc_phase1():
    """
    Phase 1 BTC direction detection — runs every 1H at :05 UTC.
    Stores result in _current_btc_setup for Phase 2 to consume.
    """
    global _current_btc_setup

    log.info("Phase 1: running 1H direction detection…")
    setup = m3.analyze()
    _current_btc_setup = setup

    if setup is None:
        log.error("Phase 1 failed — setup cleared.")
        return

    if setup.direction == "NO_SETUP":
        log.info(
            f"Phase 1: no setup.  "
            f"RSI {setup.rsi}  |  Market structure {setup.market_structure_score:+d}/3"
        )
    else:
        log.info(
            f"Phase 1: {setup.direction} detected  "
            f"(RSI {setup.rsi}, structure {setup.market_structure_score:+d}/3) — "
            f"Phase 2 will now monitor 15M candles."
        )


def run_btc_phase2():
    """
    Phase 2 entry timing — runs every 15M at :00/:15/:30/:45 UTC.
    Fires a Telegram alert when entry readiness score ≥ ENTRY_MIN_SCORE.
    """
    global _current_btc_setup

    setup = _current_btc_setup
    if setup is None or setup.direction == "NO_SETUP":
        log.debug("Phase 2: no active Phase 1 direction — skipping.")
        return

    log.info(f"Phase 2: checking 15M entry timing for {setup.direction}…")
    entry = m3.check_entry_timing(setup.direction, setup)

    if entry.ready:
        log.info(f"Phase 2: READY — score {entry.score}/8.  Sending entry alert.")
        m4.send_entry_alert(entry)
    else:
        log.info(f"Phase 2: waiting — {entry.wait_reason}  (score {entry.score}/8)")


# ══════════════════════════════════════════════════════════════════════════════
# Job functions — Module 5 momentum scanner
# ══════════════════════════════════════════════════════════════════════════════

def run_momentum_scan():
    """
    Module 5 — runs every 15 minutes at :02/:17/:32/:47 UTC.

    Fetches CMC listings sorted by 1h gain, applies all seven momentum filters,
    and fires an individual Telegram alert for each new qualifying coin.
    Coins already alerted within the cooldown window are suppressed automatically.

    Error handling:
      • Any exception increments _consecutive_failures.
      • After 3 consecutive failures, a warning alert is sent to Telegram.
      • A one-shot retry is scheduled 5 minutes after any failure.
    """
    global _consecutive_failures, _scheduler

    log.info("Momentum scan: starting…")

    try:
        results = m5.scan()
    except Exception as exc:
        _consecutive_failures += 1
        log.error(f"Momentum scan FAILED (failure #{_consecutive_failures}): {exc}")

        if _consecutive_failures >= 3:
            m4.send_message(
                "⚠️ <b>Scanner paused</b> — API issue detected.\n"
                f"Failure #{_consecutive_failures}. Retrying in 5 min…"
            )

        # Schedule a one-shot retry 5 minutes from now
        if _scheduler is not None:
            retry_time = datetime.utcnow() + timedelta(minutes=5)
            try:
                _scheduler.add_job(
                    func             = run_momentum_scan,
                    trigger          = DateTrigger(run_date=retry_time, timezone="UTC"),
                    id               = f"momentum_retry_{retry_time.strftime('%H%M%S')}",
                    name             = "Momentum Scanner 5-min retry",
                    replace_existing = False,
                )
                log.info(f"Retry scheduled at {retry_time.strftime('%H:%M:%S')} UTC.")
            except Exception as sched_err:
                log.error(f"Could not schedule retry: {sched_err}")
        return

    _consecutive_failures = 0

    # Record stats regardless of whether results were returned
    tracker.record_scan(results, m5._last_m1m7_count, m5._last_macro_blocked)

    if not results:
        log.info("Momentum scan: no new alerts this cycle.")
        return

    sent = 0
    for coin in results:
        if coin.recommendation == "COOLING_DOWN":
            m4.send_momentum_cooling_alert(coin)
        elif coin.recommendation == "GOLDEN CROSS":
            m4.send_golden_cross_alert(coin)
        else:
            m4.send_momentum_alert(coin)
        sent += 1

    log.info(f"Momentum scan: {sent} alert(s) sent.")


def run_m5_daily_summary():
    """
    Module 5 daily summary — runs at 08:01 Stuttgart, right after the briefing.
    Sends yesterday's scan stats, then resets the tracker for the new day.
    """
    log.info("Sending Module 5 daily summary…")
    stats = tracker.get_daily_summary()
    m4.send_m5_daily_summary(stats)
    tracker.reset()
    log.info("Daily summary sent; stats reset.")


# ══════════════════════════════════════════════════════════════════════════════
# Telegram /status command listener (background daemon thread)
# ══════════════════════════════════════════════════════════════════════════════

def _command_poll_loop() -> None:
    """Entry point for the background daemon thread."""
    asyncio.run(_command_poll_async())


async def _command_poll_async() -> None:
    """
    Long-poll the Telegram bot for incoming messages.
    Responds to /status from the authorised chat ID.
    Runs indefinitely in its own event loop on a daemon thread.
    """
    from telegram import Bot
    from telegram.constants import ParseMode

    if not cfg.TELEGRAM_BOT_TOKEN:
        log.warning("TELEGRAM_BOT_TOKEN not set — /status command disabled.")
        return

    bot        = Bot(token=cfg.TELEGRAM_BOT_TOKEN)
    offset     = 0
    authorized = str(cfg.TELEGRAM_CHAT_ID).strip()

    log.info("Telegram command listener started (polling for /status).")

    while True:
        try:
            async with bot:
                updates = await bot.get_updates(offset=offset, timeout=20)
            for update in updates:
                offset = update.update_id + 1
                msg = update.message
                if msg is None:
                    continue
                chat_id = str(msg.chat_id)
                text    = (msg.text or "").strip()
                if chat_id != authorized:
                    continue
                if text.startswith("/status"):
                    reply = tracker.get_status()
                    async with bot:
                        await bot.send_message(
                            chat_id    = chat_id,
                            text       = reply,
                            parse_mode = ParseMode.HTML,
                        )
                    log.info("/status replied.")
        except Exception as exc:
            log.warning(f"Command poll error (will retry): {exc}")
            await asyncio.sleep(10)


def _start_command_listener() -> None:
    """Spawn the Telegram command listener as a background daemon thread."""
    t = threading.Thread(target=_command_poll_loop, name="tg-cmd-listener", daemon=True)
    t.start()
    log.info("Command listener thread started.")


# ══════════════════════════════════════════════════════════════════════════════
# Scheduler
# ══════════════════════════════════════════════════════════════════════════════

def start_scheduler():
    """Register all scheduled jobs and block until Ctrl+C."""
    global _scheduler

    scheduler  = BlockingScheduler()
    _scheduler = scheduler

    # ── Job 1: Daily briefing at 08:00 Stuttgart ──────────────────────────────
    scheduler.add_job(
        func             = run_daily_briefing,
        trigger          = CronTrigger(
                               hour     = cfg.DAILY_BRIEFING_HOUR,
                               minute   = cfg.DAILY_BRIEFING_MINUTE,
                               timezone = _BERLIN_TZ,
                           ),
        id               = "daily_briefing",
        name             = "Daily 8am Briefing (Stuttgart)",
        replace_existing = True,
    )

    # ── Job 2: Module 5 daily summary at 08:01 Stuttgart ─────────────────────
    scheduler.add_job(
        func             = run_m5_daily_summary,
        trigger          = CronTrigger(
                               hour     = cfg.DAILY_BRIEFING_HOUR,
                               minute   = cfg.DAILY_BRIEFING_MINUTE + 1,
                               timezone = _BERLIN_TZ,
                           ),
        id               = "m5_daily_summary",
        name             = "Module 5 Daily Summary (08:01 Stuttgart)",
        replace_existing = True,
    )

    # ── Job 3: US market open reminder at 15:30 Stuttgart ─────────────────────
    scheduler.add_job(
        func             = m4.send_us_market_reminder,
        trigger          = CronTrigger(
                               hour     = cfg.US_MARKET_OPEN_HOUR,
                               minute   = cfg.US_MARKET_OPEN_MINUTE,
                               timezone = _BERLIN_TZ,
                           ),
        id               = "us_market_reminder",
        name             = "US Market Open Reminder (15:30 Stuttgart)",
        replace_existing = True,
    )

    # ── Job 4: BTC Phase 1 — direction detection every 1H at :05 UTC ─────────
    scheduler.add_job(
        func             = run_btc_phase1,
        trigger          = CronTrigger(minute="5", timezone="UTC"),
        id               = "btc_phase1_1h",
        name             = "1H BTC Phase 1 — Direction Detection",
        replace_existing = True,
    )

    # ── Job 5: BTC Phase 2 — entry timing every 15M ───────────────────────────
    scheduler.add_job(
        func             = run_btc_phase2,
        trigger          = CronTrigger(minute="0,15,30,45", timezone="UTC"),
        id               = "btc_phase2_15m",
        name             = "15M BTC Phase 2 — Entry Timing",
        replace_existing = True,
    )

    # ── Job 6: Momentum Scanner — every 15M at :02/:17/:32/:47 UTC ───────────
    # Offset by 2 minutes from Phase 2 so both jobs don't hit APIs simultaneously.
    scheduler.add_job(
        func             = run_momentum_scan,
        trigger          = CronTrigger(minute="2,17,32,47", timezone="UTC"),
        id               = "momentum_scanner_15m",
        name             = "15M Momentum Scanner (Module 5)",
        replace_existing = True,
    )

    log.info("Scheduler started:")
    log.info(f"  • Daily briefing     — 08:00 Stuttgart (Europe/Berlin)")
    log.info(f"  • M5 daily summary   — 08:01 Stuttgart (Europe/Berlin)")
    log.info(f"  • US market reminder — 15:30 Stuttgart (Europe/Berlin)")
    log.info(f"  • BTC Phase 1        — every 1H at :05 UTC  (direction detection)")
    log.info(f"  • BTC Phase 2        — every 15M at :00/:15/:30/:45 UTC  (entry timing)")
    log.info(f"  • Momentum Scanner   — every 15M at :02/:17/:32/:47 UTC  (Module 5)")
    log.info("Press Ctrl+C to stop.")

    # Start the /status command listener before blocking
    _start_command_listener()

    # Announce that the bot is online
    m4.send_startup_message()

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Scheduler stopped.")


# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Crypto Ecosystem")
    parser.add_argument(
        "--schedule",
        action = "store_true",
        help   = "Start the persistent scheduler",
    )
    args = parser.parse_args()

    if args.schedule:
        start_scheduler()
    else:
        # Default: send one briefing right now — useful for manual testing
        run_daily_briefing()
