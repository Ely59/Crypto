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
  Every 15M :02/:17/:32/:47 UTC  — Tier 1: full CMC scan (Module 5)
  Every 5M  */5 UTC    — Tier 2: active_watch rescan
  Every 3M  */3 UTC    — Tier 3: leg continuation scan

Stuttgart = Europe/Berlin timezone.
APScheduler uses this directly, so CET↔CEST transitions are handled automatically.
"""

import argparse
import asyncio
import json
import os
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
import modules.mexc_trader          as trader
import modules.alert_logger         as m_log
from modules.stats_tracker import tracker
import config as cfg
from utils.logger import get_logger

log = get_logger("main")

_BERLIN_TZ = "Europe/Berlin"

# ── Maintenance mode — set True to pause all jobs while keeping Telegram alive ─
MAINTENANCE_MODE: bool = False

# ── Session trading settings (overridable via /setmargin and /setleverage) ────
_session_margin:   float = cfg.DEFAULT_MARGIN_USDT
_session_leverage: int   = cfg.DEFAULT_LEVERAGE

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

    # Update BTC bear flag so alert builders can show ⚠️ BEAR on line 4
    m4.set_btc_bear(btc_context.regime == "BEAR")

    scout_results, avoid_coins = m2.full_scan(btc_context)

    for coin in scout_results:
        if coin.alert_level == 2:
            m4.send_scout_alert(coin, level=2)

    # Update hit data then collect yesterday's top signals for the briefing (CHANGE 7C)
    m_log.compute_hits_for_pending()
    top_alerts = m_log.get_recent_alerts(hours=24)

    m4.send_daily_briefing(btc_context, scout_results, avoid_coins, top_alerts=top_alerts)

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
        import traceback as _tb
        _consecutive_failures += 1
        tb_lines = _tb.format_exc().splitlines()
        tb_short = "\n".join(tb_lines[:8])
        log.error(
            f"Momentum scan FAILED (failure #{_consecutive_failures}): {exc}\n"
            f"Traceback (first 8 lines):\n{tb_short}"
        )

        # Telegram notification: only on the 3rd failure; afterwards, throttled by
        # send_message's own _send_fail_count guard to avoid spamming.
        if _consecutive_failures == 3:
            m4.send_message(
                "⚠️ <b>Scanner</b> — 3 consecutive failures.\n"
                f"Error: <code>{str(exc)[:200]}</code>\n"
                "Logging only until resolved. Retrying in 5 min…"
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
    tracker.record_scan(
        results, m5._last_m1m7_count, m5._last_macro_blocked, m5._last_scan_outcomes,
        s2a_ema_bearish   = m5._last_s2a_ema_bearish,
        s2a_sep_small     = m5._last_s2a_sep_small,
        s2a_15m_gate      = m5._last_s2a_15m_gate,
        s2a_fear_bypassed = m5._last_s2a_fear_bypassed,
        s2a_squeeze       = m5._last_s2a_squeeze,
        fear_mode_active  = m5._fear_mode,
        fear_greed_value  = m5._fg_value,
    )

    if not results:
        log.info("Momentum scan: no new alerts this cycle.")
        return

    mg = _session_margin
    lv = _session_leverage
    sent = 0
    for coin in results:
        if coin.recommendation == "COOLING_DOWN":
            m4.send_momentum_cooling_alert(coin)
        elif coin.recommendation == "GOLDEN CROSS":
            m4.send_golden_cross_alert(coin, mg, lv)
        elif coin.recommendation == "VOLUME SPIKE":
            m4.send_volume_spike_alert(coin, mg, lv)
        elif coin.recommendation == "RECOVERY":
            m4.send_recovery_alert(coin, mg, lv)
        elif coin.recommendation == "PRE-BREAKOUT":
            m4.send_pbw_alert(coin, mg, lv)
        elif coin.recommendation == "STAIRCASE":
            m4.send_staircase_alert(coin, mg, lv)
        elif coin.recommendation == "SQUEEZE":
            m4.send_squeeze_alert(coin, mg, lv)
        elif coin.recommendation == "SPEED ALERT":
            m4.send_speed_alert(coin, mg, lv)
        elif coin.recommendation == "EARLY GC":
            m4.send_early_gc_alert(coin, mg, lv)
        else:
            m4.send_momentum_alert(coin, mg, lv)
        m_log.log_alert(coin, coin.recommendation)
        sent += 1

    log.info(f"Momentum scan: {sent} alert(s) sent.")


def run_tier2_scan():
    """
    Tier 2 — every 5 minutes.
    Re-scans active_watch coins (those with 5m EMA momentum from last Tier 1).
    Sends alerts via the same send_* functions as run_momentum_scan().
    """
    log.info("Tier 2 scan: starting…")
    try:
        results = m5.scan_tier2()
    except Exception as exc:
        log.error(f"Tier 2 scan FAILED: {exc}")
        return

    if not results:
        log.debug("Tier 2: no new alerts.")
        return

    mg = _session_margin
    lv = _session_leverage
    for coin in results:
        m4.send_momentum_alert(coin, mg, lv)
        m_log.log_alert(coin, coin.recommendation)

    log.info(f"Tier 2 scan: {len(results)} alert(s) sent.")


def run_tier3_scan():
    """
    Tier 3 — every 3 minutes.
    Scans alert_watchlist for leg continuation signals (6% pullback + fresh EMA cross).
    Sends LEG_CONTINUATION alerts via m4.send_leg_continuation_alert().
    """
    log.info("Tier 3 scan: starting…")
    try:
        results = m5.scan_tier3()
    except Exception as exc:
        log.error(f"Tier 3 scan FAILED: {exc}")
        return

    if not results:
        log.debug("Tier 3: no leg continuations detected.")
        return

    mg = _session_margin
    lv = _session_leverage
    for coin in results:
        m4.send_leg_continuation_alert(coin, mg, lv)
        m_log.log_alert(coin, coin.recommendation)

    log.info(f"Tier 3 scan: {len(results)} leg continuation(s) sent.")


def run_grind_scanner():
    """
    GRIND scanner — every 5 minutes.
    Independent of the main pipeline; detects slow-grind 5m momentum builds.
    Stage A: silent tracking (added to _grind_candidates).
    Stage B: EARLY GRIND alert when 4+ consecutive green candles + quality checks pass.
    """
    log.debug("GRIND scan: starting…")
    try:
        results = m5.scan_grind()
    except Exception as exc:
        log.error(f"GRIND scan FAILED: {exc}")
        return

    if not results:
        log.debug("GRIND scan: no new alerts.")
        return

    mg = _session_margin
    lv = _session_leverage
    for grind in results:
        m4.send_grind_alert(grind, mg, lv)
        m_log.log_alert(grind, grind.recommendation)

    log.info(f"GRIND scan: {len(results)} alert(s) sent.")


def run_radar_signal_scan():
    """
    RADAR / SIGNAL scan — every 5 minutes.
    Checks active_watch coins for 3m+5m+4H momentum forming (RADAR) or
    10m EMA cross confirmed (SIGNAL with Telegram buttons).
    """
    global _session_margin, _session_leverage
    log.info("RADAR/SIGNAL scan: starting…")
    try:
        radar_list, signal_list = m5.scan_radar_and_signal(
            margin=_session_margin, leverage=_session_leverage
        )
    except Exception as exc:
        log.error(f"RADAR/SIGNAL scan FAILED: {exc}")
        return

    for info in radar_list:
        m4.send_radar_alert(info)

    for info in signal_list:
        ok, msg_id = m4.send_signal_alert(info)
        if ok:
            log.info(f"SIGNAL alert sent for {info['symbol']}, msg_id={msg_id}")

    if radar_list or signal_list:
        log.info(
            f"RADAR/SIGNAL scan: {len(radar_list)} RADAR, {len(signal_list)} SIGNAL sent."
        )
    else:
        log.debug("RADAR/SIGNAL scan: no new alerts.")


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


def run_weekly_hitrate_report():
    """
    Weekly hit-rate report — runs every Sunday 09:00 Berlin.
    Computes hit results for all pending alerts and sends a summary.
    """
    log.info("Running weekly hit-rate report…")
    m_log.compute_hits_for_pending()
    stats = m_log.get_weekly_stats(days=7)
    m4.send_weekly_hitrate_report(stats)
    log.info("Weekly hit-rate report sent.")


# ══════════════════════════════════════════════════════════════════════════════
# Telegram /status command listener (background daemon thread)
# ══════════════════════════════════════════════════════════════════════════════

def _command_poll_loop() -> None:
    """Entry point for the background daemon thread."""
    asyncio.run(_command_poll_async())


async def _handle_callback(cq, bot) -> None:
    """
    Handle Telegram inline keyboard button presses (Part D).
    cq = telegram.CallbackQuery object.
    """
    global _session_margin, _session_leverage
    from telegram import InlineKeyboardMarkup, InlineKeyboardButton
    from telegram.constants import ParseMode

    authorized = str(cfg.TELEGRAM_CHAT_ID).strip()
    if str(cq.from_user.id) != authorized and str(cq.message.chat_id) != authorized:
        await cq.answer("Unauthorized.", show_alert=True)
        return

    try:
        data = json.loads(cq.data)
    except (json.JSONDecodeError, TypeError):
        await cq.answer("Invalid callback data.")
        return

    action = data.get("a", "")

    # ── SKIP ──────────────────────────────────────────────────────────────────
    if action == "s":
        sym = data.get("sym", "?")
        await cq.answer("Skipped ✓")
        try:
            await cq.message.edit_text(
                cq.message.text_html + "\n\n❌ <b>Skipped</b>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
        trader.log_trade(sym, "skip", 0.0, 0.0, 0, "", "skipped")
        log.info(f"SIGNAL skipped: {sym}")
        return

    # ── PLACE ORDER ───────────────────────────────────────────────────────────
    if action == "o":
        order_id = data.get("id", "")
        order_info = m4._pending_signal_orders.get(order_id)
        if not order_info:
            await cq.answer("Order expired. Re-scan for a fresh signal.", show_alert=True)
            return

        margin   = order_info.get("margin",   _session_margin)
        leverage = order_info.get("leverage", _session_leverage)
        sym      = order_info["symbol"]
        price    = order_info["price"]
        otype    = order_info.get("order_type", "breakout")

        # Rule 4: large order confirmation
        if margin > cfg.LARGE_ORDER_THRESHOLD:
            confirm_id = f"c{order_id}"
            m4._pending_signal_orders[confirm_id] = order_info
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Confirm", callback_data=json.dumps({"a": "c", "id": order_id}, separators=(",", ":"))),
                InlineKeyboardButton("❌ Cancel",  callback_data=json.dumps({"a": "x", "id": order_id}, separators=(",", ":"))),
            ]])
            await cq.answer()
            await bot.send_message(
                chat_id      = cq.message.chat_id,
                text         = f"⚠️ Large order: ${margin:.0f}. Confirm?",
                reply_markup = keyboard,
                parse_mode   = ParseMode.HTML,
            )
            return

        # Fall through to actual order placement (same as action "c")
        data["a"] = "c"

    # ── CONFIRM ORDER (after large-order check) ───────────────────────────────
    if action in ("c", "o"):
        order_id   = data.get("id", "")
        order_info = m4._pending_signal_orders.get(order_id)
        if not order_info:
            await cq.answer("Order expired.", show_alert=True)
            return

        margin   = order_info.get("margin",   _session_margin)
        leverage = order_info.get("leverage", _session_leverage)
        sym      = order_info["symbol"]
        price    = order_info["price"]
        sl       = order_info["sl"]
        tp1      = order_info["tp1"]
        otype    = order_info.get("order_type", "breakout")

        await cq.answer("Checking safety…")

        # Safety checks (Part E)
        ok, err_msg = trader.check_safety(margin)
        if not ok:
            await bot.send_message(
                chat_id    = cq.message.chat_id,
                text       = err_msg,
                parse_mode = ParseMode.HTML,
            )
            return

        # Place the order
        result = trader.place_futures_order(
            symbol      = order_info.get("mexc_symbol", f"{sym}_USDT"),
            side        = order_info.get("side", "BUY"),
            order_type  = "LIMIT",
            price       = price,
            margin_usdt = margin,
            leverage    = leverage,
            sl_price    = sl,
            tp1_price   = tp1,
        )

        if result and "error" not in result:
            # Success
            oid      = result.get("order_id", "?")
            qty      = result.get("quantity", "?")
            conf_msg = (
                f"✅ ORDER PLACED — <b>{sym}</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"Type:  Long {leverage}x Isolated\n"
                f"Entry: ${price:.6g}\n"
                f"Qty:   {qty} {sym}\n"
                f"SL:    ${sl:.6g} (-{cfg.MOMENTUM_SL_PCT:.0f}%)\n"
                f"TP1:   ${tp1:.6g} (+{cfg.MOMENTUM_TP1_PCT:.0f}%)\n"
                f"Margin: ${margin:.0f}\n"
                f"Order ID: <code>{oid}</code>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"Monitor position in MEXC Futures"
            )
            trader.log_trade(sym, otype, price, margin, leverage, str(oid), "placed")
            try:
                await cq.message.edit_text(
                    cq.message.text_html + f"\n\n✅ <b>{otype.upper()} ORDER PLACED</b>",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass
        else:
            err = (result or {}).get("error", "Unknown error")
            conf_msg = (
                f"⚠️ Order failed: {err}\n"
                f"Place manually:\n"
                f"Entry: ${price:.6g} | SL: ${sl:.6g}"
            )
            trader.log_trade(sym, otype, price, margin, leverage, "", "failed")

        await bot.send_message(
            chat_id    = cq.message.chat_id,
            text       = conf_msg,
            parse_mode = ParseMode.HTML,
        )
        return

    # ── CANCEL large-order confirmation ───────────────────────────────────────
    if action == "x":
        await cq.answer("Cancelled.")
        try:
            await cq.message.edit_text(cq.message.text_html + "\n\n❌ <b>Cancelled</b>",
                                       parse_mode=ParseMode.HTML)
        except Exception:
            pass


async def _command_poll_async() -> None:
    """
    Long-poll the Telegram bot for incoming messages and callback queries.
    Handles text commands AND inline button presses.
    Runs indefinitely in its own event loop on a daemon thread.
    """
    from telegram import Bot
    from telegram.constants import ParseMode

    if not cfg.TELEGRAM_BOT_TOKEN:
        log.warning("TELEGRAM_BOT_TOKEN not set — command listener disabled.")
        return

    bot        = Bot(token=cfg.TELEGRAM_BOT_TOKEN)
    offset     = 0
    authorized = str(cfg.TELEGRAM_CHAT_ID).strip()

    # _reply uses the plain requests-based sender so it doesn't share the
    # polling Bot's connection pool, eliminating pool-timeout conflicts.
    def _reply(to_chat: str, text: str) -> None:
        m4.send_message(text)   # already targets TELEGRAM_CHAT_ID; ignore to_chat arg

    log.info("Telegram command listener started.")

    # Enter Bot context ONCE — not on every loop iteration — to avoid repeatedly
    # creating/destroying the httpx connection pool (root cause of "Pool timeout"
    # and "Conflict: terminated by other getUpdates request" errors).
    await bot.initialize()
    try:
      while True:
        try:
            updates = await bot.get_updates(offset=offset, timeout=20)

            for update in updates:
                offset = update.update_id + 1

                # ── Inline button callback ─────────────────────────────────
                if update.callback_query:
                    try:
                        await _handle_callback(update.callback_query, bot)
                    except Exception as cb_exc:
                        log.warning(f"Callback handler error: {cb_exc}")
                    continue

                # ── Text command ────────────────────────────────────────────
                msg = update.message
                if msg is None:
                    continue
                chat_id = str(msg.chat_id)
                text    = (msg.text or "").strip()
                if chat_id != authorized:
                    continue

                # Declare globals before assignment
                global _session_margin, _session_leverage

                if text.startswith("/status"):
                    reply = tracker.get_status()
                    await bot.send_message(
                        chat_id    = chat_id,
                        text       = reply,
                        parse_mode = ParseMode.HTML,
                    )
                    log.info("/status replied.")
                elif text.startswith("/test"):
                    loop = asyncio.get_event_loop()
                    await loop.run_in_executor(None, m4.send_test_alert)
                    log.info("/test replied.")
                elif text.startswith("/chatid"):
                    reply = (
                        f"Your chat ID: <code>{chat_id}</code>\n"
                        f"Configured TELEGRAM_CHAT_ID: <code>{authorized}</code>\n"
                        + ("✅ They match — alerts should reach you."
                           if chat_id == authorized else
                           "❌ MISMATCH — alerts are going to the wrong chat!\n"
                           f"Set TELEGRAM_CHAT_ID=<code>{chat_id}</code> in Railway env vars.")
                    )
                    await bot.send_message(
                        chat_id=chat_id, text=reply, parse_mode=ParseMode.HTML,
                    )
                    log.info(f"/chatid: incoming={chat_id} configured={authorized}")
                elif text.startswith("/setmargin"):
                    parts = text.split()
                    if len(parts) < 2:
                        _reply(chat_id, "Usage: /setmargin 10")
                    else:
                        try:
                            val = float(parts[1])
                            val = max(cfg.MARGIN_MIN_USDT, min(cfg.MARGIN_MAX_USDT, val))
                            _session_margin = val
                            _reply(chat_id, f"✅ Margin set to <b>${val:.0f}</b> per trade")
                            log.info(f"/setmargin → ${val:.0f}")
                        except ValueError:
                            _reply(chat_id, "⚠️ Invalid amount. Example: /setmargin 10")
                elif text.startswith("/setleverage"):
                    parts = text.split()
                    if len(parts) < 2:
                        _reply(chat_id, "Usage: /setleverage 5")
                    else:
                        try:
                            val = max(1, min(20, int(parts[1])))
                            _session_leverage = val
                            _reply(chat_id, f"✅ Leverage set to <b>{val}x</b>")
                            log.info(f"/setleverage → {val}x")
                        except ValueError:
                            _reply(chat_id, "⚠️ Invalid value. Example: /setleverage 5")
                elif text.startswith("/balance"):
                    bal = trader.get_account_balance()
                    if bal is not None:
                        _reply(chat_id, f"💰 MEXC Balance: <b>${bal:.2f} USDT</b>")
                    else:
                        _reply(chat_id, "⚠️ Could not fetch balance. Check API keys.")
                    log.info("/balance replied.")
                elif text.startswith("/coins"):
                    history = tracker.get_scan_history()
                    _reply(chat_id, m4.build_coins_message(history))
                    log.info("/coins replied.")
                elif text.startswith("/top"):
                    top       = tracker.get_top_results()
                    alert_log = m_log.get_recent_alerts(hours=24)
                    _reply(chat_id, m4.build_top_message(top, alert_log=alert_log))
                    log.info("/top replied.")
                elif text.startswith("/best"):
                    top = tracker.get_top_results()
                    _reply(chat_id, m4.build_best_message(top))
                    log.info("/best replied.")
                elif text.startswith("/filters"):
                    _reply(chat_id, m4.build_filters_message())
                    log.info("/filters replied.")
                elif text.startswith("/explain"):
                    parts = text.split()
                    if len(parts) < 2:
                        _reply(chat_id, "Usage: /explain COIN\nExample: /explain POLYX")
                    else:
                        history = tracker.get_scan_history()
                        _reply(chat_id, m4.build_explain_message(parts[1], history))
                    log.info("/explain replied.")
                elif text.startswith("/recovery"):
                    _reply(chat_id, m4.build_recovery_message(m5._last_rb_watchlist))
                    log.info("/recovery replied.")
                elif text.startswith("/stage0"):
                    _reply(chat_id, m4.build_stage0_message(m5.get_stage0_watchlist()))
                    log.info("/stage0 replied.")
                elif text.startswith("/grind"):
                    _reply(chat_id, m4.build_grind_watchlist_message(m5.get_grind_candidates()))
                    log.info("/grind replied.")
                elif text.startswith("/backtesting"):
                    parts    = text.split()
                    date_arg = parts[1].strip() if len(parts) > 1 else ""
                    if not date_arg:
                        _reply(chat_id, "Usage: /backtesting YYYY-MM-DD\nExample: /backtesting 2026-05-26")
                    else:
                        _reply(chat_id, "⏳ Fetching klines… this takes ~10–30s.")
                        bt_result = m_log.run_backtesting(date_arg)
                        _reply(chat_id, m4.build_backtesting_message(bt_result))
                    log.info(f"/backtesting {date_arg} replied.")
                elif text.startswith("/summary"):
                    stats = tracker.get_daily_summary()
                    _reply(chat_id, m4.build_summary_message(stats))
                    log.info("/summary replied.")
                elif text.startswith("/passed"):
                    stats = tracker.get_daily_summary()
                    _reply(chat_id, m4.build_passed_message(m5._last_passed_candidates, stats.last_scan_ts))
                    log.info("/passed replied.")
                elif text.startswith("/tier2"):
                    _reply(chat_id, m4.build_tier2_message(
                        m5._active_watch, m5._active_watch_ts,
                        m5._cmc_data_cache, m5._cmc_price_cache))
                    log.info("/tier2 replied.")
                elif text.startswith("/blocked"):
                    stats = tracker.get_daily_summary()
                    _reply(chat_id, m4.build_blocked_message(
                        m5._last_scan_outcomes, stats.last_scan_ts, m5._last_method_c_blocked))
                    log.info("/blocked replied.")
                elif text.startswith("/help"):
                    _reply(chat_id, m4.build_help_message())
                    log.info("/help replied.")

        except Exception as exc:
            import traceback as _tb
            log.warning(f"Command poll error (will retry): {exc}")
            log.debug(_tb.format_exc())
            await asyncio.sleep(10)
    finally:
        await bot.shutdown()


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

    # ── Job 7: Weekly Hit-Rate Report — every Sunday 09:00 Berlin ─────────────
    scheduler.add_job(
        func             = run_weekly_hitrate_report,
        trigger          = CronTrigger(day_of_week="sun", hour=9, minute=0, timezone=_BERLIN_TZ),
        id               = "weekly_hitrate_report",
        name             = "Weekly Hit-Rate Report (Sunday 09:00 Berlin)",
        replace_existing = True,
    )

    # ── Job 8: Tier 2 — re-scan active_watch every 5 minutes ─────────────────
    scheduler.add_job(
        func             = run_tier2_scan,
        trigger          = CronTrigger(minute="*/5", timezone="UTC"),
        id               = "momentum_tier2_5m",
        name             = "5M Tier 2 — Active Watch Rescan",
        replace_existing = True,
    )

    # ── Job 9: Tier 3 — leg continuation every 3 minutes ─────────────────────
    scheduler.add_job(
        func             = run_tier3_scan,
        trigger          = CronTrigger(minute="*/3", timezone="UTC"),
        id               = "momentum_tier3_3m",
        name             = "3M Tier 3 — Leg Continuation Scan",
        replace_existing = True,
    )

    # ── Job 10: RADAR/SIGNAL scan — every 5 minutes ───────────────────────────
    scheduler.add_job(
        func             = run_radar_signal_scan,
        trigger          = CronTrigger(minute="*/5", timezone="UTC"),
        id               = "radar_signal_5m",
        name             = "5M RADAR/SIGNAL Scan",
        replace_existing = True,
    )

    # ── Job 11: GRIND scanner — every 5 minutes ───────────────────────────────
    scheduler.add_job(
        func             = run_grind_scanner,
        trigger          = CronTrigger(minute="*/5", timezone="UTC"),
        id               = "grind_scanner_5m",
        name             = "5M GRIND Scanner",
        replace_existing = True,
    )

    log.info("Scheduler registered:")
    log.info(f"  • Daily briefing     — 08:00 Stuttgart (Europe/Berlin)")
    log.info(f"  • M5 daily summary   — 08:01 Stuttgart (Europe/Berlin)")
    log.info(f"  • US market reminder — 15:30 Stuttgart (Europe/Berlin)")
    log.info(f"  • BTC Phase 1        — every 1H at :05 UTC  (direction detection)")
    log.info(f"  • BTC Phase 2        — every 15M at :00/:15/:30/:45 UTC  (entry timing)")
    log.info(f"  • Momentum Scanner   — every 15M at :02/:17/:32/:47 UTC  (Tier 1)")
    log.info(f"  • Tier 2             — every 5M  (active watch rescan)")
    log.info(f"  • Tier 3             — every 3M  (leg continuation)")
    log.info(f"  • RADAR/SIGNAL       — every 5M  (10m cross detection)")
    log.info(f"  • GRIND Scanner      — every 5M  (slow-grind 5m momentum)")
    log.info(f"  • Weekly Hit-Rate    — every Sunday 09:00 Berlin")

    # Start the /status command listener (always — keeps Telegram connection alive)
    _start_command_listener()

    if MAINTENANCE_MODE:
        log.warning("MAINTENANCE MODE: all scheduler jobs will be paused after start.")
        m4.send_message("🔧 Bot in maintenance mode. Back soon.")
        print("All jobs paused successfully")
    else:
        m4.send_startup_message()
        m4.send_startup_ping()

    log.info("Press Ctrl+C to stop.")
    try:
        scheduler.start()   # blocks here — must come before pause()
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
