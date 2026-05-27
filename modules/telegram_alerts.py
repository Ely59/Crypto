"""
Module 4: Telegram Alert System
────────────────────────────────────────────────────────────────────────────────
Handles all outbound Telegram messages for the Crypto Ecosystem.

Three message types:

  1. Daily Briefing — sent at 08:00 Stuttgart time via the scheduler in main.py
       Sections:
         • BTC Status        (price, regime, RSI, EMA stack)
         • Fear & Greed      (CMC index value + visual bar)
         • Altcoin Watchlist (populated by Module 2 when ready; placeholder until then)
         • Avoid Today       (populated by Module 2 when ready; placeholder until then)
         • US Market Open    (15:30 Stuttgart time every trading day)
         • Daily Recommendation (BULL → Long Bias, BEAR → Short Bias, NEUTRAL → No Trade)

  2. Instant BTC Trade Alert — fires when Module 3 detects a LONG/SHORT setup
       Shows Entry, SL, TP1, TP2, Runner with % distances.

  3. Instant Altcoin Alert — fires when a coin scores 7/7 (perfect setup) or
       ≥ ALTCOIN_MIN_CRITERIA_HIT on the scout scan.

All messages use Telegram HTML parse mode.
Telegram hard limit: 4 096 chars per message — long messages are split automatically.

Run directly to send a live test of the daily briefing:
  python modules/telegram_alerts.py
"""

from __future__ import annotations

# ── Path shim (allows running this file directly) ────────────────────────────
import sys as _sys, os as _os
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
# ─────────────────────────────────────────────────────────────────────────────

import asyncio
import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo      # stdlib Python 3.9+

from telegram                  import Bot, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants        import ParseMode

from utils.logger import get_logger
import config as cfg

log = get_logger(__name__)

# Stuttgart = Central European Time, auto-switches CET↔CEST
_STUTTGART_TZ = ZoneInfo("Europe/Berlin")

# ── Pending SIGNAL orders — keyed by short ID, read by main.py callback handler ─
# Format: {short_id: {"type": "breakout"|"pullback", "symbol": ..., "price": ..., ...}}
_pending_signal_orders: dict[str, dict] = {}

# ══════════════════════════════════════════════════════════════════════════════
# Core send infrastructure
# ══════════════════════════════════════════════════════════════════════════════

import requests as _requests
import traceback as _traceback

_TG_API = "https://api.telegram.org/bot"

# Consecutive-failure throttle: track failures and last Telegram error notification
_send_fail_count:       int   = 0
_last_fail_tg_notify:   float = 0.0
_FAIL_NOTIFY_INTERVAL:  float = 1800.0   # 30 min between Telegram error messages
_FAIL_NOTIFY_THRESHOLD: int   = 3


def _tg_post(endpoint: str, payload: dict, *, log_context: str = "") -> dict | None:
    """
    Raw POST to Telegram Bot API. Handles 429 (wait + single retry).
    Returns the JSON response dict on success, None on failure.
    Logs "API FAIL: url status=X error=Y" on every failure.
    """
    if not cfg.TELEGRAM_BOT_TOKEN:
        log.error("TELEGRAM_BOT_TOKEN not set.")
        return None
    url = f"{_TG_API}{cfg.TELEGRAM_BOT_TOKEN}/{endpoint}"
    for attempt in range(2):
        try:
            resp = _requests.post(url, json=payload, timeout=15)
        except Exception as exc:
            log.error(f"API FAIL: {endpoint} status=timeout error={exc}")
            if log_context:
                log.debug(_traceback.format_exc())
            return None

        if resp.status_code == 429:
            retry_after = int((resp.json() or {}).get("parameters", {}).get("retry_after", 60))
            log.warning(f"API FAIL: {endpoint} status=429 — rate limited. Waiting {retry_after}s then retrying.")
            import time as _time; _time.sleep(retry_after)
            continue  # one retry after wait

        if resp.status_code != 200:
            log.error(f"API FAIL: {endpoint} status={resp.status_code} error={resp.text[:300]}")
            return None

        return resp.json()

    log.error(f"API FAIL: {endpoint} — all retries exhausted after 429.")
    return None


def send_message(text: str) -> bool:
    """
    Send a Telegram message via direct HTTP POST (no asyncio, no connection pool).
    Splits messages that exceed Telegram's 4 096-char hard limit.
    Throttles Telegram failure notifications after 3 consecutive failures.
    """
    global _send_fail_count, _last_fail_tg_notify
    import time as _time

    if not text:
        return False
    if not cfg.TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_CHAT_ID not set — cannot send.")
        return False

    MAX = 4000
    chunks: list[str] = []
    if len(text) <= MAX:
        chunks = [text]
    else:
        current: list[str] = []
        current_len = 0
        for line in text.splitlines(keepends=True):
            if current_len + len(line) > MAX and current:
                chunks.append("".join(current))
                current = []
                current_len = 0
            current.append(line)
            current_len += len(line)
        if current:
            chunks.append("".join(current))

    ok = True
    for chunk in chunks:
        payload = {
            "chat_id":                  cfg.TELEGRAM_CHAT_ID,
            "text":                     chunk,
            "parse_mode":               "HTML",
            "disable_web_page_preview": True,
        }
        result = _tg_post("sendMessage", payload)
        if result is None:
            ok = False
            _send_fail_count += 1
            log.error(f"Telegram send failed ({_send_fail_count} consecutive failure(s)).")
            # Only emit a Telegram error notification if under threshold or interval elapsed
            now = _time.time()
            if (_send_fail_count == _FAIL_NOTIFY_THRESHOLD or
                    (now - _last_fail_tg_notify) >= _FAIL_NOTIFY_INTERVAL):
                log.warning("Suppressing further Telegram failure notifications for 30 min.")
                _last_fail_tg_notify = now
        else:
            _send_fail_count = 0

    return ok


def send_message_with_buttons(text: str, markup: "InlineKeyboardMarkup") -> "tuple[bool, int | None]":
    """
    Send a Telegram message with inline keyboard via direct HTTP POST.
    Returns (success, message_id).
    """
    if not cfg.TELEGRAM_CHAT_ID:
        return False, None

    # Serialise InlineKeyboardMarkup to the Telegram JSON format
    keyboard_rows = []
    for row in markup.inline_keyboard:
        keyboard_rows.append([
            {"text": btn.text, "callback_data": btn.callback_data}
            for btn in row
        ])

    payload = {
        "chat_id":                  cfg.TELEGRAM_CHAT_ID,
        "text":                     text,
        "parse_mode":               "HTML",
        "disable_web_page_preview": True,
        "reply_markup":             {"inline_keyboard": keyboard_rows},
    }
    result = _tg_post("sendMessage", payload)
    if result is None:
        return False, None
    msg_id = (result.get("result") or {}).get("message_id")
    return True, msg_id


def edit_signal_message(chat_id: int, message_id: int, text: str) -> bool:
    """Edit an existing Telegram message text via direct HTTP POST."""
    payload = {
        "chat_id":    chat_id,
        "message_id": message_id,
        "text":       text,
        "parse_mode": "HTML",
    }
    return _tg_post("editMessageText", payload) is not None


# ══════════════════════════════════════════════════════════════════════════════
# Formatting helpers
# ══════════════════════════════════════════════════════════════════════════════

def _regime_emoji(regime: str) -> str:
    return {"BULL": "🟢", "BEAR": "🔴", "NEUTRAL": "🟡"}.get(regime, "⚪")


def _usd(value: float) -> str:
    """Format a USD value with appropriate precision."""
    if value >= 1_000:
        return f"${value:,.0f}"
    elif value >= 1:
        return f"${value:,.2f}"
    return f"${value:,.6f}"


def _pct(value: float, decimals: int = 1) -> str:
    """Format a percentage with sign + direction arrow. e.g. '+8.3% ▲'"""
    arrow = "▲" if value > 0 else ("▼" if value < 0 else "─")
    return f"{value:+.{decimals}f}% {arrow}"


def _pct_from_entry(level: float, entry: float) -> str:
    """Percentage distance between two price levels."""
    if entry == 0:
        return ""
    return f"{(level - entry) / entry * 100:+.1f}%"


def _vol_human(vol: float) -> str:
    """Convert raw USD volume to a compact string: '$1.2B', '$520M', etc."""
    if vol >= 1_000_000_000:
        return f"${vol / 1_000_000_000:.1f}B"
    elif vol >= 1_000_000:
        return f"${vol / 1_000_000:.0f}M"
    return f"${vol:,.0f}"


def _fmt_price(price: float) -> str:
    """Format price with MEXC-appropriate decimal precision (no mental math needed)."""
    if price >= 0.10:
        return f"{price:.4f}"
    elif price >= 0.01:
        return f"{price:.5f}"
    else:
        return f"{price:.6f}"


def _fmt_ath_line(coin) -> str:
    """Return '📅 90d High: $X' or '' if data unavailable."""
    ath_price = getattr(coin, "ath_price", 0.0)
    if not ath_price or ath_price <= 0:
        return ""
    price_str = _fmt_price(ath_price)
    return f"📅 90d High: ${price_str}"


def _signal_chain_lines(coin) -> list[str]:
    """
    Build the 5m → 15m → 4H signal chain block shown in all alerts.
    5m is displayed first (primary trigger). 1H shown as context at the end.
    Returns a list of HTML lines (empty list if tech data unavailable).
    """
    t = coin.tech
    if t is None:
        return []

    def ck(ok: bool) -> str:
        return "✅" if ok else "❌"

    # ── 5m line (PRIMARY — shown first) ──────────────────────────────────────
    if t.m5_rsi6 > 0:
        ema_cross = getattr(t, 'm5_fresh_cross', False)
        ema_above = getattr(t, 'm5_ema6_gt_ema20', t.m5_price_above_ema20)
        vol_ratio  = round(t.m5_vol_pct / 100, 1) if t.m5_vol_pct > 0 else 0.0
        m5_gate_ok = (ema_above or ema_cross) and (25 <= t.m5_rsi6 <= 75) and (t.m5_vol_pct >= 120)
        cross_tag  = " cross↑" if ema_cross else ""
        ema_sym    = "✅" if (ema_above or ema_cross) else "❌"
        m5_line = (
            f"5m:  {ck(m5_gate_ok)} EMA {ema_sym}{cross_tag}"
            f" | RSI {t.m5_rsi6:.0f}"
            f" | Vol {vol_ratio:.1f}× MA10"
        )
        if not m5_gate_ok and t.m5_note:
            m5_line += f" ← {t.m5_note.split(' — ')[0].replace('⏳ ', '').replace('⚠️ ', '')}"
    else:
        m5_line = "5m:  ⚫ n/v"

    # ── 15m line ──────────────────────────────────────────────────────────────
    ema_ok_15m = t.m15_ema6_gt_ema12 if t.m15_ema6_gt_ema12 is not None else t.m15_ema_ok
    if ema_ok_15m and t.m15_rsi6 < 78:
        m15_line = f"15m: {ck(ema_ok_15m)} EMA bullish | RSI {t.m15_rsi6:.0f} | Vol {t.vol_pct:.0f}% MA10"
    else:
        concern = ""
        if not ema_ok_15m:
            concern = " EMA6 < EMA12"
        if t.m15_rsi6 >= 78:
            concern += f" RSI {t.m15_rsi6:.0f} — overheated"
        m15_line = f"15m: {ck(ema_ok_15m and t.m15_rsi6 < 78)} {concern.strip() if concern else 'EMA bullish'} | RSI {t.m15_rsi6:.0f}"

    # ── 4H line ───────────────────────────────────────────────────────────────
    macd_arrow = "↑" if t.h4_macd_ok else "↓"
    h4_rsi_str = f"RSI {t.h4_rsi6:.0f}" if t.h4_rsi6 > 0 else f"KDJ {t.h4_kdj_j:.0f}"
    h4_rsi_ok  = t.h4_rsi6 <= 0 or t.h4_rsi6 < 80
    h4_ok      = t.h4_ema_ok and h4_rsi_ok
    h4_line    = f"4H:  {ck(h4_ok)} EMA bullish | {h4_rsi_str} | MACD {macd_arrow}"
    if not t.h4_ema_ok and getattr(t, 'h4_transitioning', False):
        h4_line = f"4H:  🔄 EMA transitioning | {h4_rsi_str} | 24H positive"
    elif not t.h4_ema_ok:
        h4_line = f"4H:  ❌ EMA bearish [{t.h4_ema6:.4g}/{t.h4_ema12:.4g}/{t.h4_ema20:.4g}]"
    elif not h4_rsi_ok:
        h4_line = f"4H:  ❌ EMA bullish | {h4_rsi_str} overheated | MACD {macd_arrow}"

    # ── Context line (1H as info only — never gates signals) ─────────────────
    c1h  = getattr(coin, 'change_1h',  0.0)
    c24h = getattr(coin, 'change_24h', 0.0)
    context_line = f"📊 Context: 1H {c1h:+.2f}% | 24H {c24h:+.2f}%"

    # ── MEXC alert levels — Breakout + Pullback ───────────────────────────────
    entry      = coin.entry_price
    sl_factor  = (coin.stop_loss / entry) if entry > 0 else (1 - coin.sl_pct / 100)
    tp1_factor = (coin.tp1      / entry) if entry > 0 else 1.05
    tp2_factor = (coin.tp2      / entry) if entry > 0 else 1.10

    bk_price = t.h24_high * 1.005 if t.h24_high > 0 else 0.0
    bk_sl    = bk_price * sl_factor
    bk_tp1   = bk_price * tp1_factor
    bk_tp2   = bk_price * tp2_factor

    pb_price = t.m5_ema20 if t.m5_ema20 > 0 else 0.0
    pb_sl    = pb_price * sl_factor
    pb_tp1   = pb_price * tp1_factor
    pb_tp2   = pb_price * tp2_factor

    # Order: 5m → 15m → 4H → context
    lines = ["", m5_line, m15_line, h4_line, context_line, ""]

    if bk_price > 0:
        lines += [
            f"🔴 MEXC Alert — Breakout: {_fmt_price(bk_price)}",
            f"   Entry {_fmt_price(bk_price)} | SL {_fmt_price(bk_sl)} | TP1 {_fmt_price(bk_tp1)} | TP2 {_fmt_price(bk_tp2)}",
        ]
    if pb_price > 0:
        lines += [
            f"🟢 MEXC Alert — Pullback: {_fmt_price(pb_price)}",
            f"   Entry {_fmt_price(pb_price)} | SL {_fmt_price(pb_sl)} | TP1 {_fmt_price(pb_tp1)} | TP2 {_fmt_price(pb_tp2)}",
            f"   (check 1m chart before placing order)",
        ]
    if bk_price == 0 and pb_price == 0:
        ez_base = entry
        ez_lo   = ez_base * (1 - cfg.MOMENTUM_ENTRY_ZONE_PCT / 100)
        ez_hi   = ez_base * (1 + cfg.MOMENTUM_ENTRY_ZONE_PCT / 100)
        lines.append(f"→ Entry-Zone: {_fmt_price(ez_lo)} – {_fmt_price(ez_hi)}")

    lines.append("")
    return lines


_SEP = "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# ── BTC bear regime flag (set by main.py after each briefing) ─────────────────
_btc_bear_regime: bool = False

def set_btc_bear(value: bool) -> None:
    """Call from main.py when btc_context.regime is known."""
    global _btc_bear_regime
    _btc_bear_regime = value


# ── MEXC tick-size cache: symbol → (price_scale, cached_at) ──────────────────
_tick_size_cache: dict[str, tuple[int, float]] = {}

def _get_price_scale(mexc_symbol: str) -> int:
    """
    Return the number of decimal places MEXC uses for this contract.
    Fetched from contract/detail API and cached for 24 h.
    Falls back to 4 decimals on any error.
    """
    cached = _tick_size_cache.get(mexc_symbol)
    if cached:
        scale, ts = cached
        if time.time() - ts < 86_400:
            return scale
    try:
        sym = mexc_symbol if mexc_symbol.endswith("USDT") else mexc_symbol.replace("_USDT", "") + "USDT"
        resp = _requests.get(
            "https://contract.mexc.com/api/v1/contract/detail",
            params={"symbol": sym},
            timeout=5,
        )
        data = resp.json()
        scale = int((data.get("data") or {}).get("priceScale", 4))
    except Exception:
        scale = 4
    _tick_size_cache[mexc_symbol] = (scale, time.time())
    return scale


def _fmt_price_dollar(price: float) -> str:
    """Format price with $ prefix and tiered decimal precision. Never strips trailing zeros."""
    if price <= 0:
        return "$0.00"
    if price >= 10:
        return f"${price:.4f}"
    elif price >= 1:
        return f"${price:.5f}"
    elif price >= 0.1:
        return f"${price:.5f}"
    elif price >= 0.01:
        return f"${price:.6f}"
    elif price >= 0.001:
        return f"${price:.7f}"
    else:
        return f"${price:.8f}"


def _score_emoji(score: int) -> str:
    if score >= 90:
        return "🔥"
    elif score >= 80:
        return "✅"
    elif score >= 65:
        return "👁️"
    return ""


def _auto_sentence(coin, sector: str) -> str:
    """Auto-generated summary sentence for line 3 of the unified alert."""
    ath_dist = getattr(coin, 'ath_dist_pct', 0.0)
    circ_pct = getattr(coin, 'circ_supply_pct', 0.0)
    rec      = coin.recommendation
    t        = coin.tech

    tf_parts: list[str] = []
    if t is not None and t.m5_rsi6 > 0:
        m5_ema = getattr(t, 'm5_ema6_gt_ema20', False) or getattr(t, 'm5_fresh_cross', False)
        if m5_ema and 25 <= t.m5_rsi6 <= 75 and t.m5_vol_pct >= 120:
            tf_parts.append("5m")
    if t is not None and t.m15_rsi6 > 0:
        if t.m15_ema6_gt_ema12 and t.m15_rsi6 < 78:
            tf_parts.append("15m")
    if t is not None:
        if t.h4_ema_ok or getattr(t, 'h4_transitioning', False) or getattr(t, 'h4_method_b', False):
            tf_parts.append("4H")

    if len(tf_parts) >= 3:
        tf_str = ", ".join(tf_parts[:-1]) + " and " + tf_parts[-1]
    elif len(tf_parts) == 2:
        tf_str = tf_parts[0] + " and " + tf_parts[1]
    elif len(tf_parts) == 1:
        tf_str = tf_parts[0]
    else:
        tf_str = "multiple timeframes"

    circ_part = f", {circ_pct:.0f}% supply in circulation" if circ_pct > 0 else ""
    ath_part  = f"{ath_dist:.0f}% below 90d high" if ath_dist > 0 else "near 90d high"

    if rec == "SPEED ALERT":
        return f"{sector} coin, {ath_part}. 5m explosive move detected — act fast."
    elif rec == "GOLDEN CROSS":
        return f"{sector} coin, {ath_part}{circ_part}. 15m EMA golden cross — {tf_str} aligned."
    elif rec == "EARLY GC":
        return f"{sector} coin, {ath_part}{circ_part}. 5m EMA cross detected — early entry signal."
    elif rec == "VOLUME SPIKE":
        return f"{sector} coin, {ath_part}{circ_part}. {tf_str} aligned — volume spike confirmed."
    elif rec == "RECOVERY":
        return f"{sector} coin, {ath_part}{circ_part}. {tf_str} aligned — recovery bounce confirmed."
    elif rec == "STAIRCASE":
        return f"{sector} coin, {ath_part}{circ_part}. {tf_str} aligned — staircase continuation confirmed."
    elif rec == "PRE-BREAKOUT":
        return f"{sector} coin, {ath_part}{circ_part}. 5m EMA compressed — breakout watch active."
    elif rec == "SQUEEZE":
        return f"{sector} coin, {ath_part}{circ_part}. EMA compression breaking out — squeeze confirmed."
    elif rec == "LEG_CONTINUATION":
        leg_num = getattr(coin, 'leg_number', 2)
        return f"{sector} coin, {ath_part}{circ_part}. {tf_str} aligned — Leg {leg_num} continuation confirmed."
    else:
        return f"{sector} coin, {ath_part}{circ_part}. {tf_str} aligned — {rec.lower()} confirmed."


def build_unified_alert(coin, margin: float | None = None, leverage: int | None = None) -> str:
    """
    Unified alert format for ALL signal types.

    LINE 1:  icon COIN — TYPE [score/100emoji]
    LINE 2:  ━━━━ separator
    LINE 3:  MCap $XM | ATH -X% | Circ X%
    LINE 4:  5m ✅/❌  15m ✅/❌  4H ✅/❌  [⚠️ BEAR if btc bear]
    LINE 5:  (⚡ FAST MOVE if SPEED ALERT)
    LINE 6:  empty
    LINE 7:  📊 LEVELS
    LINE 8+: price levels sorted HIGH → LOW
    LINE X:  empty
    """
    import config as _cfg

    t   = coin.tech
    rec = coin.recommendation
    _m  = margin   if margin   is not None else _cfg.DEFAULT_MARGIN_USDT
    _lv = leverage if leverage is not None else _cfg.DEFAULT_LEVERAGE

    # ── Line 1: Header ────────────────────────────────────────────────────────
    _NO_SCORE_RECS = {"COOLING_DOWN", "SPEED ALERT"}
    has_score = rec not in _NO_SCORE_RECS and coin.total_score > 0
    if has_score:
        s_em  = _score_emoji(coin.total_score)
        line1 = f"{coin.rec_emoji} <b>{coin.symbol}</b> — {rec} [{coin.total_score}/100{s_em}]"
    else:
        line1 = f"{coin.rec_emoji} <b>{coin.symbol}</b> — {rec}"

    # ── Line 3: Metadata ──────────────────────────────────────────────────────
    ath_dist = getattr(coin, 'ath_dist_pct', 0.0)
    circ_pct = getattr(coin, 'circ_supply_pct', 0.0)
    mcap_str = _vol_human(coin.market_cap) if coin.market_cap > 0 else "N/A"
    circ_str = f"{circ_pct:.0f}%" if circ_pct > 0 else "N/A"
    meta_line = f"MCap {mcap_str} | 90d -{ath_dist:.0f}% | Circ {circ_str}"

    # ── Line 4: TF status row ─────────────────────────────────────────────────
    def ck(ok: bool) -> str:
        return "✅" if ok else "❌"

    if t is not None and t.m5_rsi6 > 0:
        m5_ema_ok = (getattr(t, 'm5_ema6_gt_ema20', False) or
                     getattr(t, 'm5_fresh_cross', False))
        m5_ok = m5_ema_ok and (25 <= t.m5_rsi6 <= 75) and (t.m5_vol_pct >= 120)
    else:
        m5_ok = False
    m15_ok = bool(t and t.m15_ema6_gt_ema12 and t.m15_rsi6 < 78)

    # 4H/1H shown as INFO only — not a gate
    _h4_status = getattr(t, 'h4_status', '') if t else ''
    _h1_status = getattr(t, 'h1_status', '') if t else ''
    _h4_emoji  = {"FULL": "✅", "PARTIAL": "🟡", "BEARISH": "🔴"}.get(_h4_status, "⬜")
    _h1_emoji  = {"BULLISH": "✅", "NEUTRAL": "🟡", "WEAK": "🔴"}.get(_h1_status, "⬜")
    _h4_note   = " (info)" if _h4_status not in ("FULL", "") else ""

    tf_row = f"5m {ck(m5_ok)}  15m {ck(m15_ok)}  1H {_h1_emoji}  4H {_h4_emoji}{_h4_note}"
    if _btc_bear_regime:
        tf_row += "  ⚠️ BEAR"

    # ── Tick-size aware price formatter ───────────────────────────────────────
    mexc_sym = getattr(coin, 'mexc_symbol', f"{coin.symbol}_USDT")
    scale    = _get_price_scale(mexc_sym)
    def fp(p: float) -> str:
        return f"${p:.{scale}f}"

    # ── Price level calculations ───────────────────────────────────────────────
    entry    = coin.entry_price or coin.price or 0.0
    pullback = (t.m5_ema20
                if (t and t.m5_ema20 > 0 and t.m5_ema20 < entry * 0.9995)
                else entry)
    sl_pct   = getattr(coin, 'sl_pct', None) or _cfg.MOMENTUM_SL_PCT
    sl       = pullback * (1 - sl_pct / 100)

    h24h     = (t.h24_high if (t and t.h24_high > 0) else entry)
    breakout = h24h * 1.005
    tp1      = pullback * 1.08
    tp2      = pullback * 1.15

    pos_size = _m * _lv
    profit1  = pos_size * 0.08 * 0.59
    profit2  = pos_size * 0.15 * 0.41

    # ── Sort levels high → low ────────────────────────────────────────────────
    levels: list[tuple[float, str]] = [
        (tp2,      f"── TP2  +15% → +${profit2:.2f} (runner)"),
        (tp1,      f"── TP1  +8%  → +${profit1:.2f} (59% close)"),
        (h24h,     "── 24H High"),
        (breakout, "── Breakout"),
        (pullback, "── Pullback  ← best R/R"),
        (sl,       f"▁▁ SL  -{sl_pct:.0f}%"),
    ]
    levels.sort(key=lambda x: x[0], reverse=True)
    level_lines = [f"{fp(price)} {label}" for price, label in levels]

    # ── Only warning kept: Circ <40% ─────────────────────────────────────────
    circ_warn = ("⚠️ Circ <40% — unlock risk" if (circ_pct > 0 and circ_pct < 40) else "")

    # ── Assemble ──────────────────────────────────────────────────────────────
    _price_line  = f"Current price: {fp(coin.price)}" if coin.price > 0 else ""
    _s0_line     = "🔍 Pre-breakout watchlist confirmed (+10)" if getattr(coin, "stage0_breakout", False) else ""
    _leg_num     = getattr(coin, "leg_number",  1)
    _entry_valid = getattr(coin, "entry_valid", True)
    _leg_tag     = f"Leg {_leg_num}" if _leg_num > 1 else "Leg 1 (new signal)"
    _valid_tag   = "✅ Entry valid" if _entry_valid else "⏰ Entry expired — monitor only"
    _leg_line    = f"{_leg_tag}  |  {_valid_tag}"

    _pat_type  = getattr(coin, "pattern_type",  "")
    _pat_bonus = getattr(coin, "pattern_bonus", 0)
    _pat_icons = {"EXPLOSION": "💥", "BREAKOUT": "🚀", "GRIND": "📈"}
    _pat_line  = (f"{_pat_icons.get(_pat_type, '🔎')} Pattern: <b>{_pat_type}</b> (+{_pat_bonus}pts)"
                  if _pat_type else "")

    lines: list[str] = []
    if _price_line:
        lines += [_price_line, ""]
    if _s0_line:
        lines += [_s0_line, ""]
    if _pat_line:
        lines += [_pat_line, ""]
    lines += [line1, _SEP, meta_line, tf_row, _leg_line]

    if rec == "SPEED ALERT":
        lines.append("⚡ FAST MOVE — act within 10 min")

    lines.append("")
    lines.append("📊 LEVELS")
    lines.extend(level_lines)
    lines.append("")

    if circ_warn:
        lines.append(circ_warn)

    return "\n".join(lines)


def _build_warning_lines(coin, is_squeeze: bool = False) -> list[str]:
    """
    Assemble warning block in canonical order:
    supply → dilution → overbought → 5m advisory → micro-cap → squeeze-specific.

    coin.warnings already comes from _generate_warnings() in canonical order
    (supply, dilution, overbought, micro-cap). We inject the 5m note between
    overbought and micro-cap, then append the squeeze line when is_squeeze=True.
    """
    non_micro: list[str] = []
    micro:     list[str] = []

    for w in (coin.warnings or []):
        if "Micro-Cap" in w or "high-risk" in w:
            micro.append(w)
        else:
            non_micro.append(w)

    lines: list[str] = [f"⚠️ {w}" for w in non_micro]

    m5_note = getattr(coin, "m5_note", "") or (coin.tech.m5_note if coin.tech else "")
    if m5_note:
        lines.append(f"⚠️ {m5_note}")

    lines += [f"⚠️ {w}" for w in micro]

    if is_squeeze:
        lines.append("⚡ Spike-type: TP1 priority, no greed — move is short and fast")

    return lines


def _fg_bar(score: int, blocks: int = 10) -> str:
    """
    Build a simple visual bar for the Fear & Greed score.
    Uses filled (█) and empty (░) Unicode blocks.
    Example for score=63, blocks=10:  ██████░░░░  63/100
    """
    filled = round(score / 100 * blocks)
    return "█" * filled + "░" * (blocks - filled)


def _stuttgart_header() -> str:
    """Date/time line formatted in Stuttgart timezone for the briefing header."""
    now = datetime.now(tz=_STUTTGART_TZ)
    return now.strftime("%A, %d %B %Y  •  %H:%M Stuttgart")


# ── Criteria label map (used by altcoin alert builders) ──────────────────────
_CRITERIA_LABELS: dict[str, str] = {
    "C1_VolSpike":   "Vol Spike",
    "C2_RSI":        "RSI Zone",
    "C3_Breakout":   "Near Resistance",
    "C4_Uptrend":    "7d Uptrend",
    "C5_Candle":     "Strong Candle",
    "C6_EMA":        "Above EMA20",
    "C7_Liquidity":  "Liquidity OK",
}

def _format_criteria(criteria_list: list[str]) -> str:
    return "  ·  ".join(_CRITERIA_LABELS.get(c, c) for c in criteria_list)


# ══════════════════════════════════════════════════════════════════════════════
# Daily Recommendation engine
# ══════════════════════════════════════════════════════════════════════════════

def _build_recommendation(btc_context) -> list[str]:
    """
    Generate the Daily Recommendation section by scoring four independent signals.

    Each signal contributes -1 (bearish), 0 (neutral), or +1 (bullish).
    Total score range: -4 … +4.

      Signal 1 — RSI(14):        >= 55 bullish | 45–55 neutral | < 45 bearish
      Signal 2 — Volume trend:   both above MA5 & MA10 = bullish | mixed = neutral | both below = bearish
      Signal 3 — EMA cross:      BULL_CROSS / EMA6 > EMA20 = bullish |
                                  SQUEEZE = neutral | BEAR_CROSS / EMA6 < EMA20 = bearish
      Signal 4 — MACD:           DIF > DEA AND histogram growing = bullish |
                                  DIF < DEA AND histogram shrinking = bearish | mixed = neutral

    Score → bias:  +3/+4 Strong Bull | +1/+2 Mild Bull | 0 Neutral |
                   -1/-2 Mild Bear   | -3/-4 Strong Bear
    """
    ctx = btc_context
    rsi      = ctx.rsi
    fg_value = ctx.fear_greed_value

    # ── Signal 1: RSI ─────────────────────────────────────────────────────────
    if rsi >= 55:
        rsi_score  = +1
        rsi_label  = "Bullish"
        rsi_detail = f"RSI {rsi} — momentum building"
    elif rsi >= 45:
        rsi_score  = 0
        rsi_label  = "Neutral"
        rsi_detail = f"RSI {rsi} — no directional signal"
    else:
        rsi_score  = -1
        rsi_label  = "Bearish"
        rsi_detail = f"RSI {rsi} — momentum weakening"

    # ── Signal 2: Volume trend ────────────────────────────────────────────────
    above_both = ctx.vol_above_ma5 and ctx.vol_above_ma10
    below_both = not ctx.vol_above_ma5 and not ctx.vol_above_ma10
    if above_both:
        vol_score  = +1
        vol_label  = "Bullish"
        vol_detail = "Volume above MA5 & MA10 — rising interest"
    elif below_both:
        vol_score  = -1
        vol_label  = "Bearish"
        vol_detail = "Volume below MA5 & MA10 — fading activity"
    else:
        vol_score  = 0
        vol_label  = "Neutral"
        if ctx.vol_above_ma5:
            vol_detail = "Volume above MA5 but below MA10 — mixed"
        else:
            vol_detail = "Volume above MA10 but below MA5 — mixed"

    # ── Signal 3: EMA cross / position ───────────────────────────────────────
    if ctx.ema_cross == "BULL_CROSS":
        ema_score  = +1
        ema_label  = "Bullish"
        ema_detail = "EMA6 just crossed above EMA20 ↗"
    elif ctx.ema_cross == "BEAR_CROSS":
        ema_score  = -1
        ema_label  = "Bearish"
        ema_detail = "EMA6 just crossed below EMA20 ↘"
    elif ctx.ema_cross == "SQUEEZE":
        ema_score  = 0
        ema_label  = "Neutral"
        ema_detail = "EMA6/12/20 in tight squeeze — breakout pending"
    else:
        if ctx.ema6 > ctx.ema20:
            ema_score  = +1
            ema_label  = "Bullish"
            ema_detail = "EMA6 above EMA20 — short-term trend up"
        else:
            ema_score  = -1
            ema_label  = "Bearish"
            ema_detail = "EMA6 below EMA20 — short-term trend down"

    # ── Signal 4: MACD direction ──────────────────────────────────────────────
    dif_above  = ctx.macd_above_signal
    hist_grows = ctx.macd_hist_growing
    if dif_above and hist_grows:
        macd_score  = +1
        macd_label  = "Bullish"
        macd_detail = "DIF above DEA, histogram expanding"
    elif not dif_above and not hist_grows:
        macd_score  = -1
        macd_label  = "Bearish"
        macd_detail = "DIF below DEA, histogram shrinking"
    elif dif_above:
        macd_score  = 0
        macd_label  = "Neutral"
        macd_detail = "DIF above DEA but histogram contracting"
    else:
        macd_score  = 0
        macd_label  = "Neutral"
        macd_detail = "DIF below DEA but histogram recovering"

    # ── Weighted total and bias label ─────────────────────────────────────────
    total = rsi_score + vol_score + ema_score + macd_score

    if total >= 3:
        bias_label = "STRONG BULL BIAS"
        bias_tag   = "📈📈"
        bias_color = "🟢"
    elif total >= 1:
        bias_label = "MILD BULL BIAS"
        bias_tag   = "📈"
        bias_color = "🟢"
    elif total == 0:
        bias_label = "NO CLEAR BIAS"
        bias_tag   = "⚖️"
        bias_color = "🟡"
    elif total >= -2:
        bias_label = "MILD BEAR BIAS"
        bias_tag   = "📉"
        bias_color = "🔴"
    else:
        bias_label = "STRONG BEAR BIAS"
        bias_tag   = "📉📉"
        bias_color = "🔴"

    # ── Helper: turn a score into a coloured dot ──────────────────────────────
    def _dot(s: int) -> str:
        return "🟢" if s > 0 else ("🔴" if s < 0 else "⚪")

    def _fmt(s: int) -> str:
        return f"+{s}" if s > 0 else str(s)

    # ── Context lines based on bias + RSI sub-zone ────────────────────────────
    if total >= 1:       # bull
        if rsi < 60:
            context = "Momentum building with room to run. Look for longs on dips to EMA6/EMA12."
            caution = "Early phase — good risk/reward for entries."
        elif rsi < 70:
            context = "Healthy uptrend in progress. Buy pullbacks to EMA6; avoid chasing."
            caution = "Manage size — don't go all-in at current levels."
        else:
            context = "RSI approaching overbought. Reduce new long entries; protect profits."
            caution = "Wait for RSI reset below 65 before adding exposure."
    elif total <= -1:    # bear
        if rsi > 55:
            context = "Early bear signal forming. Start reducing long exposure."
            caution = "Wait for RSI confirmation below 50 before shorting."
        elif rsi > 45:
            context = "Bear trend confirmed. Favour shorts on bounces to EMA6/EMA12."
            caution = "Keep stops tight — counter-rallies can be sharp."
        else:
            context = "Strong downtrend in progress. Short bounces; no longs until regime flips."
            caution = "Oversold territory — brace for relief bounces."
    else:                # neutral
        context = "No clean directional signal. Reduce size, wait for confirmed breakout."
        caution = "Choppy conditions favour patience over action."

    # ── Fear & Greed overlay ──────────────────────────────────────────────────
    if fg_value <= 25:
        fg_note = "😱  Extreme Fear — potential contrarian long opportunity."
    elif fg_value <= 40:
        fg_note = "😟  Fear present — cautious accumulation zones may be forming."
    elif fg_value <= 60:
        fg_note = "😐  Neutral sentiment — no strong crowd signal either way."
    elif fg_value <= 75:
        fg_note = "😏  Greed rising — be disciplined, don't FOMO into positions."
    else:
        fg_note = "🤑  Extreme Greed — historically a time to reduce exposure."

    return [
        f"  {bias_color}  <b>Score: {_fmt(total)} / 4  →  {bias_label}  {bias_tag}</b>",
        "",
        f"  {_dot(rsi_score)}  RSI ({rsi})        <b>{rsi_label}</b>",
        f"  {_dot(vol_score)}  Volume Trend      <b>{vol_label}</b>",
        f"  {_dot(ema_score)}  EMA Cross         <b>{ema_label}</b>",
        f"  {_dot(macd_score)}  MACD              <b>{macd_label}</b>",
        "",
        f"  {context}",
        f"  {caution}",
        "",
        f"  {fg_note}",
    ]


def _macro_setup_line(btc_context) -> str:
    """One-sentence macro summary from regime + BTC.D + Fear & Greed."""
    regime = btc_context.regime
    fg     = btc_context.fear_greed_value
    btcd   = getattr(btc_context, "btc_dominance", None)

    if regime == "BULL":
        regime_part = "BTC is in a bullish regime"
    elif regime == "BEAR":
        regime_part = "BTC is in a bearish regime"
    else:
        regime_part = "BTC is consolidating"

    if fg <= 25:
        fg_part = "extreme fear dominating sentiment"
    elif fg <= 40:
        fg_part = "fear-driven sentiment"
    elif fg <= 60:
        fg_part = "neutral sentiment"
    elif fg <= 75:
        fg_part = "greed building in the market"
    else:
        fg_part = "extreme greed — caution warranted"

    if btcd is not None:
        if btcd > 58:
            btcd_part = "BTC.D elevated — altcoins likely underperforming"
        elif btcd < 55:
            btcd_part = "BTC.D receding — altcoin season conditions forming"
        else:
            btcd_part = "BTC.D in neutral zone"
        return f"{regime_part} with {fg_part}; {btcd_part}."
    return f"{regime_part} with {fg_part}."


# ══════════════════════════════════════════════════════════════════════════════
# Daily Briefing builder
# ══════════════════════════════════════════════════════════════════════════════

def build_daily_briefing(
    btc_context,                      # BTCContext from Module 1 — required
    setups:      list | None = None,  # list[CoinSetup] from Module 2
    avoid_coins: list | None = None,  # list[AvoidCoin] from Module 2
    top_alerts:  list | None = None,  # list[dict] from alert_logger — yesterday's top signals
) -> str:
    """
    Compose the full daily briefing message.

    Only `btc_context` is required — all other parameters are optional and
    display informative placeholder text when not provided, so the briefing
    remains useful even before Modules 2 and 3 are integrated.
    """
    lines: list[str] = []

    # ── Header ────────────────────────────────────────────────────────────────
    lines += [
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "📊  <b>CRYPTO ECOSYSTEM — Daily Briefing</b>",
        f"📅  {_stuttgart_header()}",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
    ]

    # ── Section 1: BTC Status ─────────────────────────────────────────────────
    r_emoji = _regime_emoji(btc_context.regime)

    # Human-readable EMA stack label
    stack_labels = {
        "BULL":  "Bullish 📈  (EMA6 &gt; EMA12 &gt; EMA20)",
        "BEAR":  "Bearish 📉  (EMA6 &lt; EMA12 &lt; EMA20)",
        "MIXED": "Mixed ↔️  (crossover / consolidation)",
    }
    stack_label = stack_labels.get(btc_context.ema_stack, btc_context.ema_stack)

    lines += [
        "─────────────────────────────",
        f"{r_emoji}  <b>BTC STATUS</b>",
        "─────────────────────────────",
        f"  💰  Price     <b>{_usd(btc_context.btc_price)}</b>",
        f"  🏷️  Regime    <b>{btc_context.regime}</b>",
        f"  📊  RSI (14)  <b>{btc_context.rsi}</b>",
        "",
        f"  📐  EMA6      <b>{_usd(btc_context.ema6)}</b>",
        f"  📐  EMA12     <b>{_usd(btc_context.ema12)}</b>",
        f"  📐  EMA20     <b>{_usd(btc_context.ema20)}</b>",
        f"  📈  EMA Stack  {stack_label}",
        "",
    ]

    # ── Section 1b: ETH and SOL status ───────────────────────────────────────
    lines += [
        "─────────────────────────────",
        "🔷  <b>ALT MARKETS  (ETH / SOL)</b>",
        "─────────────────────────────",
    ]
    for coin_ctx in (btc_context.eth, btc_context.sol):
        if coin_ctx is None:
            continue
        c_emoji  = _regime_emoji(coin_ctx.regime)
        s_emoji  = {"BULL": "📈", "BEAR": "📉", "MIXED": "↔️"}.get(coin_ctx.ema_stack, "─")
        # Volume signal dot
        v_dot    = "🟢" if (coin_ctx.vol_above_ma5 and coin_ctx.vol_above_ma10) else \
                   ("🔴" if not coin_ctx.vol_above_ma5 and not coin_ctx.vol_above_ma10 else "⚪")
        # MACD signal dot
        m_dot    = "🟢" if (coin_ctx.macd_above_signal and coin_ctx.macd_hist_growing) else \
                   ("🔴" if not coin_ctx.macd_above_signal and not coin_ctx.macd_hist_growing else "⚪")
        # EMA position dot
        e_dot    = "🟢" if coin_ctx.ema6 > coin_ctx.ema20 else "🔴"
        if coin_ctx.ema_cross == "SQUEEZE":
            e_dot = "⚪"

        lines += [
            f"  {c_emoji}  <b>{coin_ctx.symbol}</b>  {coin_ctx.regime}  "
            f"|  💰 {_usd(coin_ctx.price)}  |  RSI <b>{coin_ctx.rsi}</b>",
            f"     {s_emoji} EMA Stack {coin_ctx.ema_stack}"
            f"  {v_dot} Vol"
            f"  {m_dot} MACD"
            f"  {e_dot} EMA",
            "",
        ]

    # ── Section 2: Fear & Greed Index ─────────────────────────────────────────
    fg_val   = btc_context.fear_greed_value
    fg_label = btc_context.fear_greed_label
    fg_bar   = _fg_bar(fg_val)

    # Pick an emoji that reflects the sentiment bucket
    if fg_val <= 25:
        fg_emoji = "😱"
    elif fg_val <= 40:
        fg_emoji = "😟"
    elif fg_val <= 60:
        fg_emoji = "😐"
    elif fg_val <= 75:
        fg_emoji = "😏"
    else:
        fg_emoji = "🤑"

    lines += [
        "─────────────────────────────",
        "😱  <b>FEAR &amp; GREED INDEX</b>",
        "─────────────────────────────",
        f"  {fg_emoji}  <b>{fg_val} — {fg_label}</b>",
        f"  <code>{fg_bar}  {fg_val}/100</code>",
        "  <i>Source: CoinMarketCap</i>",
        "",
    ]

    # ── Section 2b: Market Structure ─────────────────────────────────────────
    lines += [
        "─────────────────────────────",
        "📐  <b>MARKET STRUCTURE</b>",
        "─────────────────────────────",
    ]

    # Funding Rate
    fr = btc_context.funding_rate
    if fr is not None:
        fr_pct  = fr * 100
        if fr < 0:
            fr_dot  = "🟢"
            fr_note = f"Shorts paying — bullish pressure"
        elif fr > 0.0003:   # > 0.03% = elevated longs-paying
            fr_dot  = "🔴"
            fr_note = f"Longs paying — bearish pressure"
        else:
            fr_dot  = "⚪"
            fr_note = f"Neutral"
        lines.append(f"  {fr_dot}  Funding Rate     <b>{fr_pct:+.4f}%</b>  <i>({fr_note})</i>")
    else:
        lines.append("  ⚫  Funding Rate     <i>N/A</i>")

    # Long/Short Ratio
    ls = btc_context.long_short_ratio
    if ls is not None:
        if ls < 0.8:
            ls_dot  = "🟢"
            ls_note = "Shorts dominant — squeeze up possible"
        elif ls < 1.0:
            ls_dot  = "🟢"
            ls_note = "Slightly short-heavy — mild bullish"
        elif ls < 1.5:
            ls_dot  = "⚪"
            ls_note = "Balanced"
        else:
            ls_dot  = "🔴"
            ls_note = "Longs crowded — squeeze risk"
        lines.append(f"  {ls_dot}  Long/Short Ratio <b>{ls:.2f}</b>  <i>({ls_note})</i>")
    else:
        lines.append("  ⚫  Long/Short Ratio <i>N/A</i>")

    # Open Interest
    oi = btc_context.oi_value
    if oi is not None:
        oi_k    = oi / 1000
        oi_arrow = "↑ Rising" if btc_context.oi_rising else "↓ Falling"
        oi_dot   = "🟢" if btc_context.oi_rising else "🔴"
        lines.append(f"  {oi_dot}  Open Interest    <b>{oi_k:,.1f}K BTC</b>  <i>({oi_arrow})</i>")
    else:
        lines.append("  ⚫  Open Interest    <i>N/A</i>")

    # BTC Dominance (7A)
    btcd = getattr(btc_context, "btc_dominance", None)
    if btcd is not None:
        if btcd > 58:
            btcd_dot  = "🔴"
            btcd_note = "⚠️ Altcoin season unlikely"
        elif btcd < 55:
            btcd_dot  = "🟢"
            btcd_note = "✅ Capital rotating to alts"
        else:
            btcd_dot  = "⚪"
            btcd_note = "Neutral zone"
        lines.append(f"  {btcd_dot}  BTC Dominance    <b>{btcd:.1f}%</b>  <i>({btcd_note})</i>")
    else:
        lines.append("  ⚫  BTC Dominance    <i>N/A</i>")

    # ETHBTC Ratio (7A)
    if btc_context.eth is not None and btc_context.btc_price and btc_context.btc_price > 0:
        ethbtc     = btc_context.eth.price / btc_context.btc_price
        eth_regime = getattr(btc_context.eth, "regime", None)
        if eth_regime == "BULL":
            ethbtc_dot  = "🟢"
            ethbtc_note = "ETH outperforming"
        elif eth_regime == "BEAR":
            ethbtc_dot  = "🔴"
            ethbtc_note = "ETH underperforming"
        else:
            ethbtc_dot  = "⚪"
            ethbtc_note = "Neutral"
        lines.append(f"  {ethbtc_dot}  ETH/BTC Ratio    <b>{ethbtc:.5f}</b>  <i>({ethbtc_note})</i>")
    else:
        lines.append("  ⚫  ETH/BTC Ratio    <i>N/A</i>")

    # Total3 (7A)
    t3    = getattr(btc_context, "total3_usd", None)
    mc24h = getattr(btc_context, "market_cap_24h_pct", None)
    if t3 is not None:
        t3_b = t3 / 1e9
        if mc24h is not None:
            t3_arrow = "↑" if mc24h > 0 else "↓"
            t3_pct   = f"  {t3_arrow} {abs(mc24h):.1f}% 24h"
        else:
            t3_pct = ""
        lines.append(f"  📊  Total3           <b>${t3_b:,.0f}B</b>{t3_pct}")
    else:
        lines.append("  ⚫  Total3           <i>N/A</i>")

    lines.append("")

    # ── Section 3: Altcoin Watchlist ──────────────────────────────────────────
    # Accepts list[ScoutResult] from the new Module 2.
    # ScoutResult has: symbol, name, price, market_cap, volume_24h, vol_mc_ratio,
    #                  circ_rate, rsi_4h, ema6, ema12, ema20, ema_spread, alert_level
    lines += [
        "─────────────────────────────",
        "🔭  <b>ALTCOIN WATCHLIST</b>",
        "─────────────────────────────",
    ]
    if setups:
        # Show Level-2 coins first (already sorted by altcoin_scout.full_scan)
        for coin in setups[:8]:
            dir_tag   = "📈 LONG" if coin.direction == "LONG" else "📉 SHORT"
            level_tag = "🚨 <b>TRADE ALERT</b>" if coin.alert_level == 2 else "👀 <b>On Radar</b>"
            lines += [
                f"  {level_tag}  {dir_tag}  <b>{coin.symbol}</b>  ({coin.name})",
                f"     💰 {_usd(coin.price)}  |  MCap {_vol_human(coin.market_cap)}",
                f"     📊 Vol/MC <b>{coin.vol_mc_ratio:.0f}%</b>  |  Circ <b>{coin.circ_rate:.0f}%</b>",
                f"     📈 RSI <b>{coin.rsi_4h}</b>  |  EMA Spread <b>{coin.ema_spread:.2f}%</b>",
                "",
            ]
    elif top_alerts:
        lines += ["  🏆  <b>YESTERDAY'S TOP SIGNALS:</b>", ""]
        for i, alert in enumerate(top_alerts[:3], 1):
            sym      = alert.get("coin", "?")
            sig_type = alert.get("signal_type", "?")
            score    = alert.get("score", "?")
            entry_p  = alert.get("price_at_alert", "")
            roi_s    = alert.get("roi_percent", "")
            hit_s    = alert.get("hit", "")
            peak_s   = alert.get("4h_max_price", "")
            try:
                entry_f = _fmt_price(float(entry_p)) if entry_p else "?"
            except (ValueError, TypeError):
                entry_f = entry_p or "?"
            lines.append(f"  {i}. <b>{sym}</b> — {score}/100 | {sig_type} | Entry: ${entry_f}")
            if roi_s and hit_s:
                try:
                    roi_f  = float(roi_s)
                    hit_ok = int(hit_s) == 1
                    hit_sym = "✅" if hit_ok else "❌"
                    try:
                        peak_f = f"Peak ${_fmt_price(float(peak_s))}  " if peak_s else ""
                    except (ValueError, TypeError):
                        peak_f = ""
                    lines.append(f"     Result: {peak_f}({roi_f:+.1f}%) {hit_sym}")
                except (ValueError, TypeError):
                    lines.append("     Result: <i>[check price]</i>")
            else:
                lines.append("     Result: <i>[check price]</i>")
            lines.append("")
    else:
        lines += [
            "  ⏳  <i>No qualifying coins found today,</i>",
            "  <i>or Module 2 not yet active.</i>",
            "",
        ]

    # ── Section 4: Avoid Today ────────────────────────────────────────────────
    lines += [
        "─────────────────────────────",
        "⚠️  <b>AVOID TODAY</b>",
        "─────────────────────────────",
    ]
    if avoid_coins:
        for coin in avoid_coins[:5]:
            lines += [
                f"  🔴  <b>{coin.symbol}</b>  ({coin.name})",
                f"       {_usd(coin.price)}  |  24h {_pct(coin.change_24h_pct)}  |  7d {_pct(coin.change_7d_pct)}",
                f"       ⛔  {coin.reason}",
                "",
            ]
    else:
        lines += [
            "  ✅  No major red-flag coins today.",
            "",
        ]

    # ── Section 5: US Market Open ─────────────────────────────────────────────
    lines += [
        "─────────────────────────────",
        "🗽  <b>US MARKETS TODAY</b>",
        "─────────────────────────────",
        "  NYSE &amp; NASDAQ open at",
        "  <b>15:30 Stuttgart time  (CET / CEST)</b>",
        "  ⚡  Watch for volatility spikes around the open!",
        "",
    ]

    # ── Section 6: Today's Macro Setup ──────────────────────────────────────
    lines += [
        "─────────────────────────────",
        "🌐  <b>TODAY'S MACRO SETUP</b>",
        "─────────────────────────────",
        f"  {_macro_setup_line(btc_context)}",
        "",
    ]
    lines += _build_recommendation(btc_context)
    lines += [""]

    # ── Footer ────────────────────────────────────────────────────────────────
    lines += [
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "⚠️  <i>Not financial advice. Always use proper risk management.</i>",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
    ]

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# Instant alert builders (for Modules 3 and 2 when they fire live signals)
# ══════════════════════════════════════════════════════════════════════════════

def build_scout_level2_alert(coin) -> str:
    """
    Instant Trade Alert for a Level-2 coin (Vol/MC ≥ 500 %).
    Sent immediately when detected — not deferred to the daily briefing.
    coin is a ScoutResult dataclass.
    """
    is_long   = coin.direction == "LONG"
    dir_emoji = "📈" if is_long else "📉"
    dir_label = "LONG" if is_long else "SHORT"
    rsi_note  = f"below 70 ✅" if is_long else f"above 70 ✅"
    ema_note  = "EMA squeeze ✅" if is_long else "Bearish stack ✅"
    return "\n".join([
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"🚨  <b>TRADE ALERT — {dir_emoji} {dir_label}  |  {coin.symbol}</b>",
        f"    {coin.name}",
        "━━━━━━━━━━━━━━━━━━━━━━",
        "",
        f"  <b>All criteria passed — Level 2  {dir_emoji}</b>",
        "",
        f"  💰  Price         <b>{_usd(coin.price)}</b>",
        f"  📊  Vol / MCap    <b>{coin.vol_mc_ratio:.0f}%</b>  🔥",
        f"  🏦  Market Cap    <b>{_vol_human(coin.market_cap)}</b>",
        f"  💧  24h Volume    <b>{_vol_human(coin.volume_24h)}</b>",
        f"  🔄  Circ. Rate    <b>{coin.circ_rate:.0f}%</b>",
        "",
        f"  📊  RSI (4H)      <b>{coin.rsi_4h}</b>  ({rsi_note})",
        f"  📐  EMA6          <b>{_usd(coin.ema6)}</b>",
        f"  📐  EMA12         <b>{_usd(coin.ema12)}</b>",
        f"  📐  EMA20         <b>{_usd(coin.ema20)}</b>",
        f"  🗜️  EMA Spread    <b>{coin.ema_spread:.2f}%</b>  ({ema_note})",
        "",
        "  ⚠️  <i>High volume spike detected. Confirm on chart before entering.</i>",
        "  ⚠️  <i>Not financial advice. Use proper risk management.</i>",
        "━━━━━━━━━━━━━━━━━━━━━━",
    ])


def build_scout_level1_alert(coin) -> str:
    """
    Watchlist notification for a Level-1 coin (Vol/MC ≥ 200 % but < 500 %).
    Less urgent than Level 2 — coin is on radar, not yet a confirmed trade.
    coin is a ScoutResult dataclass.
    """
    is_long   = coin.direction == "LONG"
    dir_emoji = "📈" if is_long else "📉"
    dir_label = "LONG" if is_long else "SHORT"
    return "\n".join([
        f"👀  <b>COIN ON RADAR — {dir_emoji} {dir_label}  |  {coin.symbol}</b>",
        f"    {coin.name}",
        "",
        f"  💰  Price         <b>{_usd(coin.price)}</b>",
        f"  📊  Vol / MCap    <b>{coin.vol_mc_ratio:.0f}%</b>",
        f"  🏦  Market Cap    <b>{_vol_human(coin.market_cap)}</b>",
        f"  🔄  Circ. Rate    <b>{coin.circ_rate:.0f}%</b>",
        f"  📊  RSI (4H)      <b>{coin.rsi_4h}</b>",
        f"  🗜️  EMA Spread    <b>{coin.ema_spread:.2f}%</b>",
        "",
        "  <i>Watch for a volume escalation to Level 2.</i>",
    ])


def build_btc_trade_alert(btc_trade) -> str:
    """
    Full BTC leverage trade alert with fixed-% risk framework.

    Levels are derived from config percentages, not ATR, so they are consistent
    across all alerts and easy to follow without recalculating.
    """
    import config as _cfg   # local import to avoid circular dependency at module level
    dir_emoji = "📈" if btc_trade.direction == "LONG" else "📉"
    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"⚡  <b>BTC TRADE ALERT  —  {btc_trade.direction}</b>  {dir_emoji}",
        "━━━━━━━━━━━━━━━━━━━━━━",
        "",
        "  <b>Levels</b>",
        f"  📍  Entry      <b>{_usd(btc_trade.entry_price)}</b>",
        f"  🛑  Stop-Loss  <b>{_usd(btc_trade.stop_loss)}</b>"
        f"  ({_pct_from_entry(btc_trade.stop_loss, btc_trade.entry_price)})  ← {_cfg.BTC_RISK_SL_PCT}% rule",
        "",
        f"  🎯  TP1        <b>{_usd(btc_trade.tp1)}</b>"
        f"  ({_pct_from_entry(btc_trade.tp1, btc_trade.entry_price)})  → close 40%",
        f"  🎯  TP2        <b>{_usd(btc_trade.tp2)}</b>"
        f"  ({_pct_from_entry(btc_trade.tp2, btc_trade.entry_price)})  → close 40%",
        f"  🚀  Runner     <b>{_usd(btc_trade.runner)}</b>"
        f"  ({_pct_from_entry(btc_trade.runner, btc_trade.entry_price)})  → let 20% ride",
        "",
        "  <b>Risk Framework</b>"
        f"  ({_cfg.BTC_RISK_LEVERAGE}× leverage / {_cfg.BTC_RISK_MARGIN_USDT} USDT margin)",
        f"  💼  Position    <b>{_cfg.BTC_RISK_MARGIN_USDT * _cfg.BTC_RISK_LEVERAGE:.0f} USDT</b>",
        f"  💸  Max Loss    <b>{btc_trade.max_loss_usdt} USDT</b>  (at SL)",
        f"  ⚖️  R:R (TP2)  <b>1 : {btc_trade.r_r_tp2}</b>",
        f"  📊  RSI  <b>{btc_trade.rsi}</b>   ATR  <b>{_usd(btc_trade.atr)}</b>   TF: 1h",
        "",
        "  <b>Trade Management</b>",
        "  ✅  After TP1 hit → move SL to entry (risk-free trade)",
        "  ✅  Let TP2 run; only move SL to TP1 once TP2 is close",
        "  ✅  Runner: trail SL manually — let the move play out",
        "",
        "  <b>Confirmation signals:</b>",
    ]
    for reason in btc_trade.reasons:
        lines.append(f"    ✅  {reason}")

    # Market structure block
    ms = btc_trade.market_structure_score
    if ms >= 2:
        ms_label = "Strong confirmation"
        ms_dot   = "🟢"
    elif ms == 1:
        ms_label = "Mild confirmation"
        ms_dot   = "🟢"
    elif ms == 0:
        ms_label = "Neutral"
        ms_dot   = "⚪"
    else:
        ms_label = "Weak structure — caution"
        ms_dot   = "🔴"

    lines += [
        "",
        f"  <b>Market Structure</b>  {ms_dot}  Score <b>{ms:+d}/3</b>  ({ms_label})",
    ]
    for sig in (btc_trade.ms_signals or []):
        lines.append(f"    · {sig}")

    lines += [
        "",
        "  ⚠️  <i>Not financial advice. Use proper position sizing.</i>",
        "━━━━━━━━━━━━━━━━━━━━━━",
    ]
    return "\n".join(lines)


def build_altcoin_perfect_alert(coin) -> str:
    """Alert for a coin that hit all 7 criteria — highest-conviction setup."""
    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"🚀  <b>PERFECT SETUP  —  {coin.symbol}</b>  ⭐",
        f"    {coin.name}",
        "━━━━━━━━━━━━━━━━━━━━━━",
        "",
        "  <b>✅  ALL 7 / 7 CRITERIA PASSED</b>",
        "",
        f"  💰  Price      <b>{_usd(coin.price)}</b>",
        f"  📊  RSI        <b>{coin.rsi}</b>",
        f"  📈  7d Change  <b>{_pct(coin.change_7d_pct)}</b>",
        f"  💧  24h Vol    <b>{_vol_human(coin.volume_24h)}</b>",
        "",
        "  Criteria:",
    ]
    for c in coin.criteria_list:
        lines.append(f"    ✅  {_CRITERIA_LABELS.get(c, c)}")
    lines += [
        "",
        "  ⚠️  <i>Always confirm on your own chart. Not financial advice.</i>",
        "━━━━━━━━━━━━━━━━━━━━━━",
    ]
    return "\n".join(lines)


def build_altcoin_setup_alert(coin) -> str:
    """Alert for a coin that passed ≥ ALTCOIN_MIN_CRITERIA_HIT but fewer than 7."""
    lines = [
        f"🔍  <b>ALTCOIN SETUP  —  {coin.symbol}</b>",
        f"    {coin.name}  |  {coin.criteria_hit}/7 criteria",
        "",
        f"  💰  Price      <b>{_usd(coin.price)}</b>",
        f"  📊  RSI        <b>{coin.rsi}</b>",
        f"  📈  7d Change  <b>{_pct(coin.change_7d_pct)}</b>",
        f"  💧  24h Vol    <b>{_vol_human(coin.volume_24h)}</b>",
        "",
        f"  ✅  {_format_criteria(coin.criteria_list)}",
    ]
    return "\n".join(lines)


def build_us_market_reminder() -> str:
    """Short reminder sent at 15:30 Stuttgart every trading day."""
    now      = datetime.now(tz=_STUTTGART_TZ)
    date_str = now.strftime("%A, %d %B")
    return (
        "🗽  <b>US MARKET OPEN</b>\n"
        f"NYSE &amp; NASDAQ just opened — {date_str}, 15:30 Stuttgart\n\n"
        "⚡  Expect potential volatility in BTC and major altcoins.\n"
        "Keep an eye on your open positions and set alerts!"
    )


# ══════════════════════════════════════════════════════════════════════════════
# Public send functions
# ══════════════════════════════════════════════════════════════════════════════

def send_daily_briefing(
    btc_context,
    setups:      list | None = None,
    avoid_coins: list | None = None,
    top_alerts:  list | None = None,
) -> bool:
    """
    Build and send the morning briefing.
    Only btc_context is required; setups, avoid_coins, and top_alerts show placeholders when absent.
    """
    log.info("Building daily briefing…")
    msg = build_daily_briefing(btc_context, setups, avoid_coins, top_alerts=top_alerts)
    ok  = send_message(msg)
    log.info("Daily briefing sent." if ok else "Daily briefing FAILED.")
    return ok


def send_btc_alert(btc_trade) -> bool:
    """Send an instant BTC leverage trade alert."""
    log.info(f"Sending BTC {btc_trade.direction} alert…")
    return send_message(build_btc_trade_alert(btc_trade))


def send_altcoin_alert(coin) -> bool:
    """Send a 7/7 perfect alert or a standard setup alert, depending on criteria count."""
    if coin.criteria_hit == 7:
        log.info(f"Sending PERFECT altcoin alert for {coin.symbol}…")
        return send_message(build_altcoin_perfect_alert(coin))
    log.info(f"Sending altcoin setup alert for {coin.symbol} ({coin.criteria_hit}/7)…")
    return send_message(build_altcoin_setup_alert(coin))


def send_us_market_reminder() -> bool:
    """Send the daily US market open reminder (scheduled at 15:30 Stuttgart)."""
    log.info("Sending US market open reminder…")
    return send_message(build_us_market_reminder())


def build_entry_alert(entry) -> str:
    """
    Phase 2 Smart Entry alert.
    Only sent when EntrySignal.ready is True (score ≥ ENTRY_MIN_SCORE).
    entry is an EntrySignal dataclass from btc_trading_support.check_entry_timing().
    """
    import config as _cfg
    dir_emoji = "📈" if entry.direction == "LONG" else "📉"
    score_bar = "█" * entry.score + "░" * (8 - entry.score)

    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"⚡  <b>SMART ENTRY — {entry.direction}</b>  {dir_emoji}",
        f"    15M timing confirmed  |  Score <b>{entry.score}/8</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
        f"  💰  Current Price   <b>{_usd(entry.entry_price)}</b>",
        f"  <code>{score_bar}  {entry.score}/8</code>",
        "",
        "  <b>Signal Breakdown</b>",
    ]
    for line in entry.signal_lines:
        lines.append(f"  {line}")

    lines += [
        "",
        "  ─────────────────────────",
        f"  <b>Levels</b>  ({_cfg.ENTRY_LEVERAGE}× / {_cfg.ENTRY_MARGIN_USDT} USDT margin)",
        f"  📍  Entry      <b>{_usd(entry.entry_price)}</b>",
        f"  🛑  Stop-Loss  <b>{_usd(entry.stop_loss)}</b>"
        f"  ({_pct_from_entry(entry.stop_loss, entry.entry_price)})  ← {_cfg.BTC_RISK_SL_PCT}% rule",
        "",
        f"  🎯  TP1        <b>{_usd(entry.tp1)}</b>"
        f"  ({_pct_from_entry(entry.tp1, entry.entry_price)})  → close 40%",
        f"  🎯  TP2        <b>{_usd(entry.tp2)}</b>"
        f"  ({_pct_from_entry(entry.tp2, entry.entry_price)})  → close 40%",
        f"  🚀  Runner     <b>{_usd(entry.runner)}</b>"
        f"  ({_pct_from_entry(entry.runner, entry.entry_price)})  → let 20% ride",
        f"  💸  Max Loss   <b>{entry.max_loss_usdt} USDT</b>  (at SL)",
        "",
        "  <b>Trade Management</b>",
        "  ✅  After TP1 hit → move SL to entry (risk-free)",
        "  ✅  At TP2 → trail SL to TP1, let runner ride",
        "",
        "  ⚠️  <i>Not financial advice. Use proper risk management.</i>",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
    ]
    return "\n".join(lines)


def send_entry_alert(entry) -> bool:
    """Send a Phase 2 Smart Entry alert to Telegram."""
    log.info(f"Sending Phase 2 entry alert — {entry.direction} score {entry.score}/8…")
    return send_message(build_entry_alert(entry))


def build_momentum_alert(coin) -> str:
    """STRONG ENTRY / WATCH alert — unified 15-line format."""
    return build_unified_alert(coin)


def send_momentum_alert(coin, margin: float = 10.0, leverage: int = 5) -> bool:
    """Send a Module 5 momentum alert (≥65/100) to Telegram with inline buttons."""
    log.info(
        f"Sending {coin.recommendation} alert for "
        f"{coin.symbol} ({coin.change_1h:+.2f}% 1h, {coin.total_score}/100)…"
    )
    return send_alert_with_buttons(coin, margin, leverage)


def build_momentum_cooling_alert(coin) -> str:
    """
    Short watchlist alert for a coin that passed M1-M7 and has a bullish
    4H EMA stack, but whose 4H KDJ J is above 90 (overheated oscillator).
    Tells the user to wait for the KDJ to cool before considering entry.
    coin is a MomentumResult with recommendation="COOLING_DOWN".
    """
    t = coin.tech
    kdj_val = f"{t.h4_kdj_j:.1f}" if t is not None else "N/A"

    return "\n".join([
        f"⏳ <b>{coin.symbol}</b> {coin.change_1h:+.2f}% — Cooling down",
        f"4H KDJ: {kdj_val} (wait for &lt;80)",
        f"Fundamentals: MCap {_vol_human(coin.market_cap)} | Circ {coin.circ_supply_pct:.0f}%",
        "Watch for entry when 4H cools.",
    ])


def _make_test_momentum_result():
    """
    Synthetic MomentumResult for /test delivery checks.
    Passes through build_momentum_alert() so the format is byte-for-byte identical
    to a real alert — if it looks right, real alerts will too.
    """
    from modules.momentum_scanner import MomentumResult, TechResult, FundResult

    tech = TechResult(
        h4_ema6=0.5124,    h4_ema12=0.5089,   h4_ema20=0.5052,
        h4_ema_ok=True,    h4_ema_sep=1.42,
        h4_kdj_j=72.3,     h4_kdj_ok=True,
        macro_ok=True,
        m15_ema6=0.5138,   m15_ema20=0.5101,
        m15_ema_ok=True,   m15_ema_pts=15,
        m15_price=0.5145,  m15_price_ok=True,  m15_price_pts=10,
        m15_rsi6=58.4,     m15_rsi6_ok=True,   m15_rsi6_hot=False, m15_rsi6_pts=10,
        m15_kdj_j=61.2,    m15_kdj_ok=True,    m15_kdj_hot=False,  m15_kdj_pts=10,
        m15_macd_dif=0.00031, m15_macd_dea=0.00018,
        m15_macd_ok=True,  m15_macd_pts=5,
        vol_pct=187.0,     vol_ok=True,        vol_pts=10,
        m15_change=1.8,    m15_golden_cross=False,
        m15_vol_spike=False, m15_vol_spike_ratio=1.1,
        h24_high=0.5280,   h24_low=0.4980,
        h16d_high=0.6500,  ath_dist_pct=20.8,
        score=60,
    )
    fund = FundResult(mcap_pts=15, circ_pts=10, fdv_pts=10, gain_pts=5, total=40)

    return MomentumResult(
        symbol          = "TEST",
        name            = "Test Coin (delivery check)",
        price           = 0.5145,
        change_1h       = 6.83,
        change_24h      = 12.40,
        market_cap      = 250_000_000,
        volume_24h      = 18_500_000,
        fdv             = 380_000_000,
        fdv_mcap_ratio  = 1.52,
        circ_supply_pct = 65.8,
        matched_tags    = ["layer-2"],
        mexc_symbol     = "TEST_USDT",
        tech            = tech,
        fund            = fund,
        total_score     = 100,
        recommendation  = "STRONG ENTRY",
        rec_emoji       = "🟢",
        warnings        = ["TEST ALERT — not a real signal"],
        entry_price     = 0.5145,
        stop_loss       = 0.4836,
        tp1             = 0.5660,
        tp2             = 0.6174,
        risk_usd        = 6.0,
        reward_tp1_usd  = 10.0,
        reward_tp2_usd  = 20.0,
        rr_str          = "1:1.67",
        sl_pct          = 6.0,
    )


def send_test_alert() -> bool:
    """
    Send a test momentum alert via /test command.
    Uses build_momentum_alert() so the format is identical to a real alert.
    """
    log.info("Sending /test delivery alert…")
    header = "🔔 <b>TEST ALERT — delivery check</b>\n<i>Format is identical to a real signal.</i>\n\n"
    body   = build_momentum_alert(_make_test_momentum_result())
    return send_message(header + body)


def send_startup_ping() -> bool:
    """
    Minimal one-line ping sent immediately on bot start.
    Confirms alert delivery is working before the first real scan fires.
    """
    log.info("Sending startup delivery ping…")
    return send_message(
        "🔔 Alert delivery test — if you see this, alerts are working ✅"
    )


def send_momentum_cooling_alert(coin) -> bool:
    """Send a Module 5 cooling-down watchlist alert to Telegram."""
    t = coin.tech
    kdj_val = f"{t.h4_kdj_j:.1f}" if t is not None else "N/A"
    log.info(f"Sending COOLING alert for {coin.symbol} (KDJ J {kdj_val})…")
    return send_message(build_momentum_cooling_alert(coin))


def build_leg_continuation_alert(coin) -> str:
    """🔄 LEG_CONTINUATION alert — unified 15-line format (Part D)."""
    return build_unified_alert(coin)


def send_leg_continuation_alert(coin, margin: float = 10.0, leverage: int = 5) -> bool:
    """Send a Leg Continuation alert to Telegram with inline buttons."""
    leg_num = getattr(coin, 'leg_number', 2)
    log.info(f"Sending LEG {leg_num} CONTINUATION alert for {coin.symbol}…")
    return send_alert_with_buttons(coin, margin, leverage)


def build_volume_spike_alert(coin) -> str:
    """VOLUME SPIKE alert — unified 15-line format."""
    return build_unified_alert(coin)


def send_volume_spike_alert(coin, margin: float = 10.0, leverage: int = 5) -> bool:
    """Send a Module 5 Volume Spike pre-signal alert to Telegram with inline buttons."""
    log.info(f"Sending VOLUME SPIKE alert for {coin.symbol} ({coin.change_1h:+.2f}% 1h)…")
    return send_alert_with_buttons(coin, margin, leverage)


def build_recovery_alert(coin) -> str:
    """RECOVERY BOUNCE alert — unified 15-line format."""
    return build_unified_alert(coin)


def send_recovery_alert(coin, margin: float = 10.0, leverage: int = 5) -> bool:
    """Send a Module 5 Recovery Bounce alert to Telegram with inline buttons."""
    log.info(
        f"Sending RECOVERY alert for {coin.symbol} "
        f"({coin.change_1h:+.2f}% 1h, pulled back from {coin.tech.h24_high if coin.tech else '?'})…"
    )
    return send_alert_with_buttons(coin, margin, leverage)


def build_golden_cross_alert(coin) -> str:
    """GOLDEN CROSS alert — unified 15-line format."""
    return build_unified_alert(coin)


def send_golden_cross_alert(coin, margin: float = 10.0, leverage: int = 5) -> bool:
    """Send a Module 5 Golden Cross early-entry alert to Telegram with inline buttons."""
    log.info(f"Sending GOLDEN CROSS alert for {coin.symbol} ({coin.change_1h:+.2f}% 1h)…")
    return send_alert_with_buttons(coin, margin, leverage)


def build_pbw_alert(coin) -> str:
    """PRE-BREAKOUT Watch alert — unified 15-line format."""
    return build_unified_alert(coin)


def send_pbw_alert(coin, margin: float = 10.0, leverage: int = 5) -> bool:
    """Send a Module 5 Pre-Breakout Watch alert to Telegram with inline buttons."""
    log.info(f"Sending PRE-BREAKOUT alert for {coin.symbol} ({coin.change_1h:+.2f}% 1h)…")
    return send_alert_with_buttons(coin, margin, leverage)


def build_staircase_alert(coin) -> str:
    """STAIRCASE Continuation alert — unified 15-line format."""
    return build_unified_alert(coin)


def send_staircase_alert(coin, margin: float = 10.0, leverage: int = 5) -> bool:
    """Send a Module 5 Staircase Continuation alert to Telegram with inline buttons."""
    log.info(f"Sending STAIRCASE alert for {coin.symbol} ({coin.change_1h:+.2f}% 1h)…")
    return send_alert_with_buttons(coin, margin, leverage)


def build_squeeze_alert(coin) -> str:
    """BB-Squeeze Breakout alert — unified 15-line format."""
    return build_unified_alert(coin)


def send_squeeze_alert(coin, margin: float = 10.0, leverage: int = 5) -> bool:
    """Send a BB-Squeeze Breakout alert to Telegram with inline buttons."""
    log.info(
        f"Sending SQUEEZE BREAKOUT alert for {coin.symbol} "
        f"({coin.change_1h:+.2f}% 1h, score {coin.total_score})…"
    )
    return send_alert_with_buttons(coin, margin, leverage)


def build_speed_alert(coin) -> str:
    """SPEED ALERT — unified 15-line format."""
    return build_unified_alert(coin)


def send_speed_alert(coin, margin: float = 10.0, leverage: int = 5) -> bool:
    """Send a Speed Alert to Telegram with inline buttons."""
    log.info(f"Sending SPEED ALERT for {coin.symbol} ({coin.change_1h:+.2f}% 1h)…")
    return send_alert_with_buttons(coin, margin, leverage)


def build_early_gc_alert(coin) -> str:
    """EARLY GC alert — unified 15-line format."""
    return build_unified_alert(coin)


def send_early_gc_alert(coin, margin: float = 10.0, leverage: int = 5) -> bool:
    """Send an Early GC (5m EMA cross) alert to Telegram with inline buttons."""
    log.info(f"Sending EARLY GC alert for {coin.symbol} ({coin.change_1h:+.2f}% 1h, score {coin.total_score})…")
    return send_alert_with_buttons(coin, margin, leverage)


def build_weekly_hitrate_report(stats: dict) -> str:
    """
    Weekly hit-rate report — sent every Sunday 09:00 Berlin.
    stats is the dict returned by modules.alert_logger.get_weekly_stats().
    """
    from datetime import datetime, timedelta
    now      = datetime.now(tz=_STUTTGART_TZ)
    end_date = now.strftime("%d.%m")
    start_date = (now - timedelta(days=stats.get("period_days", 7))).strftime("%d.%m")

    by_sig = stats.get("by_signal", {})

    _sig_labels = {
        "GOLDEN CROSS":  "⚡ Golden Cross",
        "VOLUME SPIKE":  "⚡ Volume Spike",
        "PRE-BREAKOUT":  "🔍 Pre-Breakout",
        "STAIRCASE":     "🪜 Staircase",
        "RECOVERY":      "♻️ Recovery Bounce",
        "STRONG ENTRY":  "🟢 Strong Entry",
        "WATCH":         "🟡 Watch",
        "SIGNAL":        "🔍 Signal",
        "SQUEEZE":       "💥 BB-Squeeze",
        "SPEED ALERT":   "⚡ Speed Alert",
        "EARLY GC":      "⚡ Early GC",
    }

    lines = [
        "📊 <b>WÖCHENTLICHER HIT-RATE REPORT</b>",
        f"Zeitraum: {start_date} – {end_date}",
        "",
    ]

    total_hits  = 0
    total_count = 0
    for sig_key, label in _sig_labels.items():
        if sig_key not in by_sig:
            continue
        d     = by_sig[sig_key]
        hits  = d["hits"]
        count = d["total"]
        pct   = f"{hits / count * 100:.0f}%" if count > 0 else "—"
        lines.append(f"{label}:  {hits}/{count} Hits ({pct})")
        total_hits  += hits
        total_count += count

    if not total_count:
        lines.append("Keine Daten für diese Woche.")
        return "\n".join(lines)

    total_pct = f"{total_hits / total_count * 100:.0f}%" if total_count > 0 else "—"
    lines += [
        "",
        f"<b>Gesamt: {total_hits}/{total_count} Hits ({total_pct})</b>",
    ]

    best  = stats.get("best",  {})
    worst = stats.get("worst", {})
    if best.get("coin"):
        lines.append(f"Bester Trade: <b>{best['coin']}</b> +{best['roi']:.1f}%")
    if worst.get("coin"):
        lines.append(f"Schlechtester: <b>{worst['coin']}</b> {worst['roi']:+.1f}%")

    return "\n".join(lines)


def send_weekly_hitrate_report(stats: dict) -> bool:
    """Send the weekly hit-rate report to Telegram."""
    log.info("Sending weekly hit-rate report…")
    return send_message(build_weekly_hitrate_report(stats))


# ── Startup message ────────────────────────────────────────────────────────────

def build_startup_message() -> str:
    """One-time message sent when the bot comes online in --schedule mode."""
    return "\n".join([
        "🤖 <b>Crypto Scout Pro — ONLINE ✅</b>",
        "",
        "Interval: every 15 min",
        "Signals: Zone (2-10%) | ⚡ Golden Cross (0.5-6%) | $25M+ MCap | MEXC Perp",
        "Categories: L1, L2, AI, DePIN, RWA, Gaming, DeFi",
        "",
        "Type /status for last scan results",
    ])


def send_startup_message() -> bool:
    log.info("Sending startup message…")
    return send_message(build_startup_message())


# ── Module 5 daily summary ─────────────────────────────────────────────────────

def build_m5_daily_summary(stats) -> str:
    """
    Daily Module 5 stats summary — sent at 08:01 Stuttgart, then stats reset.
    stats is a DailyStats from modules.stats_tracker.
    """
    date_str     = stats.date.strftime("%d %B %Y")
    total_alerts = (stats.entry_alerts + stats.watch_alerts + stats.early_alerts +
                    stats.gc_alerts + stats.vs_alerts + stats.rb_alerts)
    best_line    = (
        f"Best score: <b>{stats.best_coin} {stats.best_score}/100</b>"
        if stats.best_coin else "Best score: —"
    )

    return "\n".join([
        f"📊 <b>DAILY SUMMARY</b> — {date_str}",
        "",
        f"Scans today: <b>{stats.scan_count}</b>",
        f"Coins analyzed: <b>{stats.coins_analyzed}</b>",
        f"Alerts sent: <b>{total_alerts}</b>"
        f"  (Entry: {stats.entry_alerts} | Watch: {stats.watch_alerts}"
        f" | Early: {stats.early_alerts} | GC: {stats.gc_alerts}"
        f" | VS: {stats.vs_alerts} | RB: {stats.rb_alerts})",
        best_line,
        f"Missed (4H overheated): <b>{stats.cooling_alerts}</b> coins",
    ])


def send_m5_daily_summary(stats) -> bool:
    log.info("Sending Module 5 daily summary…")
    return send_message(build_m5_daily_summary(stats))


# ══════════════════════════════════════════════════════════════════════════════
# Interactive command response builders (/coins, /top, /best, /filters, etc.)
# ══════════════════════════════════════════════════════════════════════════════

def build_coins_message(scan_history: list) -> str:
    """/coins — Coins from last 3 scans in compact list format."""
    if not scan_history:
        return "No scans recorded yet today. Try again after the next scan (:02/:17/:32/:47 UTC)."

    lines = ["📋 <b>RECENT SCAN RESULTS</b>", ""]
    _no_score = {"COOLING_DOWN", "GOLDEN CROSS", "VOLUME SPIKE", "RECOVERY"}
    for snap in reversed(scan_history):  # most recent first
        lines.append(f"<b>🕐 {snap.timestamp}</b>")
        if not snap.results:
            lines.append("  No alerts this scan.")
        else:
            for r in snap.results:
                score_part = f"  ({r.total_score}/100)" if r.recommendation not in _no_score else ""
                lines.append(f"  {r.rec_emoji} <b>{r.symbol}</b> {r.change_1h:+.2f}% — {r.recommendation}{score_part}")
        lines.append("")
    return "\n".join(lines)


def build_top_message(top_results: list, alert_log: list | None = None) -> str:
    """/top — Today's top 3 scored coins with outcome from alert_log when available."""
    if not top_results:
        return "No scored coins today yet.\nBe patient — the first alert of the day usually appears by mid-morning."

    log_by_sym: dict = {}
    if alert_log:
        for entry in alert_log:
            sym = entry.get("coin", "")
            if sym and sym not in log_by_sym:
                log_by_sym[sym] = entry

    lines = ["🏆 <b>TODAY'S TOP SCORED COINS</b>", ""]
    best_roi: float | None = None
    best_coin: str = ""

    for i, r in enumerate(top_results, 1):
        lines += [
            f"{i}. {r.rec_emoji} <b>{r.symbol}</b> — {r.total_score}/100 ({r.recommendation})",
            f"   {r.change_1h:+.2f}% (1H)  |  Entry: {_usd(r.entry_price)}",
            f"   SL: {_usd(r.stop_loss)}  TP1: {_usd(r.tp1)}  TP2: {_usd(r.tp2)}",
        ]
        log_entry = log_by_sym.get(r.symbol)
        if log_entry:
            roi  = log_entry.get("roi_percent", "")
            peak = log_entry.get("4h_max_price", "")
            hit  = log_entry.get("hit", "")
            if roi:
                try:
                    roi_f    = float(roi)
                    hit_sym  = "✅" if hit and int(hit) == 1 else "❌"
                    peak_str = f" | Peak {_usd(float(peak))}" if peak else ""
                    lines.append(f"   Outcome: {roi_f:+.1f}%{peak_str} {hit_sym}")
                    if best_roi is None or roi_f > best_roi:
                        best_roi  = roi_f
                        best_coin = r.symbol
                except (ValueError, TypeError):
                    pass
        lines.append("")

    if best_coin and best_roi is not None:
        lines += [f"⭐ Best performer: <b>{best_coin}</b> ({best_roi:+.1f}%)", ""]

    return "\n".join(lines)


def build_best_message(top_results: list) -> str:
    """/best — Full alert for today's highest-scored coin."""
    if not top_results:
        return "No scored coins today yet."
    return build_momentum_alert(top_results[0])


def build_filters_message() -> str:
    """/filters — All active filter thresholds."""
    return "\n".join([
        "⚙️ <b>ACTIVE FILTERS</b>",
        "",
        "<b>Stage 1 — Fundamental Screen</b>",
        f"  1H gain:     {cfg.MOMENTUM_ZONE_MIN:.0f}% – {cfg.MOMENTUM_ZONE_MAX:.0f}%",
        f"  MCap:        ${cfg.MOMENTUM_MCAP_MIN_USD/1e6:.0f}M – ${cfg.MOMENTUM_MCAP_MAX_USD/1e9:.0f}B",
        f"  24H vol:     ≥${cfg.MOMENTUM_VOL_24H_MIN_USD/1e6:.0f}M",
        f"  Circ supply: ≥{cfg.MOMENTUM_CIRC_SUPPLY_MIN_PCT:.0f}%",
        f"  FDV/MCap:    ≤{cfg.MOMENTUM_FDV_RATIO_MAX:.1f}×",
        "  Categories:  L1, L2, AI, DePIN, RWA, Gaming, DeFi",
        "",
        "<b>Stage 2 — 4H Macro Gate</b>",
        f"  EMA stack:   EMA6 &gt; EMA12 &gt; EMA20 (sep ≥{cfg.MOMENTUM_TA_H4_EMA_SEP_MIN*100:.1f}%)",
        f"  4H KDJ J:    warning at ≥{cfg.MOMENTUM_TA_H4_KDJ_J_MAX:.0f} (info only)",
        "",
        "<b>Stage 3 — Scoring</b>",
        f"  STRONG ENTRY: ≥{cfg.MOMENTUM_TOTAL_STRONG_ENTRY}/100",
        f"  WATCH:        ≥{cfg.MOMENTUM_TOTAL_WATCH}/100",
        f"  MONITOR:      ≥{cfg.MOMENTUM_TOTAL_MONITOR}/100 (no alert)",
        f"  Cooldown:     {cfg.MOMENTUM_ALERT_COOLDOWN_MIN // 60}H per coin",
        "",
        "<b>Golden Cross (⚡)</b>",
        f"  1H range: {cfg.MOMENTUM_GC_1H_MIN:.1f}% – {cfg.MOMENTUM_GC_1H_MAX:.1f}%  |  RSI max: {cfg.MOMENTUM_GC_RSI_MAX:.0f}",
        "",
        "<b>Volume Spike (⚡)</b>",
        f"  1H range: {cfg.MOMENTUM_VS_1H_MIN:.0f}% – {cfg.MOMENTUM_VS_1H_MAX:.0f}%",
        f"  Vol mult: ≥{cfg.MOMENTUM_VS_VOL_MULT:.0f}× avg prev 3 candles  |  RSI max: {cfg.MOMENTUM_VS_RSI_MAX:.0f}",
        "",
        "<b>Recovery Bounce (♻️)</b>",
        f"  1H range: {cfg.MOMENTUM_RB_1H_MIN:.0f}% – {cfg.MOMENTUM_RB_1H_MAX:.0f}%",
        f"  24H peak: ≥{cfg.MOMENTUM_RB_PEAK_PCT:.0f}% above current  |  Pullback: ≥{cfg.MOMENTUM_RB_PULLBACK_PCT:.0f}%",
        f"  4H KDJ J: &lt;{cfg.MOMENTUM_RB_KDJ_MAX:.0f}",
    ])


def build_explain_message(symbol: str, scan_history: list) -> str:
    """/explain COIN — What happened to a specific coin in the last scan."""
    symbol = symbol.upper()

    if not scan_history:
        return f"No scan history yet. Try again after the next scan."

    _status_map = {
        "ALERTED":         "✅ Alert sent",
        "COOLDOWN":        "⏸ On cooldown (alerted recently, suppressed)",
        "MONITOR":         "👁 Monitor (below alert threshold)",
        "BELOW_THRESHOLD": "❌ Score too low",
        "MACRO_BLOCKED":   "🚫 4H EMA stack bearish",
        "DEAD_ZONE":       "💀 Dead zone (low vol + low gain)",
        "NO_DATA":         "❓ No MEXC kline data",
        "GC":              "⚡ Golden Cross alert sent",
        "VS":              "⚡ Volume Spike alert sent",
        "RB":              "♻️ Recovery alert sent",
        "COOLING":         "⏳ 4H KDJ overheated (cooling alert sent)",
    }

    for snap in reversed(scan_history):
        for oc in snap.outcomes:
            if oc.symbol == symbol:
                status = _status_map.get(oc.rec, oc.rec)
                lines = [
                    f"🔍 <b>EXPLAIN: {symbol}</b>",
                    f"Last seen at: <b>{snap.timestamp}</b>",
                    "",
                    f"Status: <b>{status}</b>",
                    f"1H change: <b>{oc.change_1h:+.2f}%</b>",
                ]
                if oc.score > 0:
                    lines.append(f"Score: <b>{oc.score}/100</b>")
                if oc.vol_pct > 0:
                    lines.append(f"4H Vol: {oc.vol_pct:.0f}% of MA10")
                if oc.h4_kdj_j > 0:
                    lines.append(f"4H KDJ J: {oc.h4_kdj_j:.1f}")
                lines += ["", f"Detail: <i>{oc.detail}</i>"]
                return "\n".join(lines)

    return (
        f"<b>{symbol}</b> was not seen in the last 3 scans.\n\n"
        "Possible reasons:\n"
        "• Didn't pass 1H gain / MCap / vol / category filter\n"
        "• No MEXC perpetual contract\n"
        "• Above 10% 1H gain (parabolic, skipped)"
    )


def build_recovery_message(rb_watchlist: list) -> str:
    """/recovery — Near-miss Recovery Bounce candidates from the last scan."""
    if not rb_watchlist:
        return (
            "♻️ <b>RECOVERY WATCH</b>\n\n"
            "No candidates in the last scan.\n\n"
            "Candidates appear when a coin drops 12%+ from its 24H high\n"
            "and starts bouncing (2-8% 1H gain)."
        )

    lines = ["♻️ <b>RECOVERY WATCH</b>", ""]
    for item in sorted(rb_watchlist, key=lambda x: x.pullback_pct, reverse=True)[:5]:
        ema_dot = "🟢" if item.h4_ema_ok else "🔴"
        kdj_dot = "🟢" if item.h4_kdj_j < cfg.MOMENTUM_RB_KDJ_MAX else "🔴"
        lines += [
            f"<b>{item.symbol}</b> {item.change_1h:+.2f}% (1H)",
            f"  24H peak: {_usd(item.h24_high)} → Now: {_usd(item.current)}",
            f"  Pullback: <b>{item.pullback_pct:.1f}%</b>",
            f"  {ema_dot} 4H EMA  {kdj_dot} KDJ {item.h4_kdj_j:.1f}",
            "",
        ]
    return "\n".join(lines)


def build_stage0_message(watchlist: list) -> str:
    """/stage0 — Current Stage 0 pre-breakout watchlist."""
    if not watchlist:
        return (
            "🔍 <b>STAGE 0 — PRE-BREAKOUT WATCH</b>\n\n"
            "No coins on pre-breakout watch.\n\n"
            "Coins appear here when they are consolidating (45m range ≤3%,\n"
            "RSI 42–68, volume building) with −0.5% ≤ 1H &lt; 0.3%.\n"
            "A +10 bonus fires when they break above consolidation high within 90 min."
        )

    lines = [
        "🔍 <b>STAGE 0 — PRE-BREAKOUT WATCH</b>",
        f"  {len(watchlist)} coin(s) on watchlist",
        "",
    ]
    for entry in watchlist:
        sym      = entry["symbol"]
        hi       = entry["consolidation_high"]
        lo       = entry["consolidation_low"]
        rng      = entry["range_pct"]
        rsi      = entry["rsi5m"]
        age      = entry["age_min"]
        exp      = entry["expires_in_min"]
        add_px   = entry["price_at_add"]

        exp_str  = f"{exp:.0f}m" if exp > 0 else "expired"
        age_str  = f"{age:.0f}m ago"

        lines += [
            f"<b>{sym}</b>  —  added {age_str}  |  expires in {exp_str}",
            f"  Range:  <b>${_fmt_price(lo)}</b> – <b>${_fmt_price(hi)}</b>  "
            f"({rng:.1f}%)  ← break above <b>${_fmt_price(hi)}</b>",
            f"  RSI5m:  <b>{rsi:.0f}</b>  |  Price at add: ${_fmt_price(add_px)}",
            "",
        ]

    lines.append("<i>Coins get +10 bonus when they break above consolidation high within 90 min.</i>")
    return "\n".join(lines)


def build_backtesting_message(result: dict) -> str:
    """/backtesting YYYY-MM-DD — per-alert outcome report for a given date."""
    date_str = result.get("date", "?")
    error    = result.get("error")
    has_data = result.get("has_data", False)
    entries  = result.get("entries", [])

    # Friendly display date
    try:
        from datetime import datetime as _dt
        display_date = _dt.strptime(date_str, "%Y-%m-%d").strftime("%d.%m.%Y")
    except ValueError:
        display_date = date_str

    header = f"📊 <b>BACKTESTING — {display_date}</b>\n"

    if error:
        return header + f"\n❌ Error: {error}"

    if not has_data:
        return (
            header
            + "\nNo alerts logged for this date.\n\n"
            + "<i>If you just deployed the bot, alert logging is now active and "
            "results will appear for future dates.</i>\n\n"
            + "<i>Tip: /backtesting YYYY-MM-DD — use today's or yesterday's date.</i>"
        )

    # Count verdicts
    counts: dict[str, int] = {"GOOD": 0, "LATE": 0, "FALSE": 0, "LEG2": 0, "NEUTRAL": 0}
    for e in entries:
        counts[e["verdict"]] = counts.get(e["verdict"], 0) + 1

    verdict_icons = {
        "GOOD": "✅", "LATE": "⚠️", "FALSE": "❌", "LEG2": "🔄", "NEUTRAL": "➡️",
    }
    verdict_labels = {
        "GOOD": "GOOD TIMING", "LATE": "TOO LATE",
        "FALSE": "FALSE SIGNAL", "LEG2": "LEG 2 SETUP", "NEUTRAL": "NEUTRAL",
    }

    total = len(entries)
    summary_parts = [f"Total: {total}"]
    for v in ("GOOD", "LATE", "FALSE", "LEG2"):
        if counts[v]:
            summary_parts.append(f"{verdict_icons[v]} {counts[v]}")
    summary_line = "  |  ".join(summary_parts)

    def _fmt_pct(v: float | None) -> str:
        if v is None:
            return "  N/A  "
        return f"{v:+.1f}%"

    def _fmt_price(p: float) -> str:
        """Format entry price with appropriate decimal places."""
        if p >= 100:
            return f"${p:.2f}"
        if p >= 1:
            return f"${p:.3f}"
        if p >= 0.01:
            return f"${p:.4f}"
        return f"${p:.6f}"

    # Build table rows
    col_w = {"SYM": 7, "TIME": 6, "ENTRY": 9, "PCT": 7}
    hdr   = (
        f"{'COIN':<{col_w['SYM']}} {'TIME':<{col_w['TIME']}} "
        f"{'ENTRY':>{col_w['ENTRY']}} {'1H':>{col_w['PCT']}} "
        f"{'4H':>{col_w['PCT']}} {'24H':>{col_w['PCT']}} VERDICT"
    )
    rows = [hdr, "─" * (col_w["SYM"] + col_w["TIME"] + col_w["ENTRY"] + col_w["PCT"] * 3 + 12)]

    for e in entries:
        sym     = e["symbol"][:col_w["SYM"]]
        time_s  = e["time_str"]
        entry_s = _fmt_price(e["entry"])
        p1h_s   = _fmt_pct(e["pct_1h"])
        p4h_s   = _fmt_pct(e["pct_4h"])
        p24h_s  = _fmt_pct(e["pct_24h"])
        icon    = verdict_icons.get(e["verdict"], "➡️")
        label   = verdict_labels.get(e["verdict"], e["verdict"])
        pat     = f" [{e['pat_type']}]" if e.get("pat_type") else ""
        rows.append(
            f"{sym:<{col_w['SYM']}} {time_s:<{col_w['TIME']}} "
            f"{entry_s:>{col_w['ENTRY']}} {p1h_s:>{col_w['PCT']}} "
            f"{p4h_s:>{col_w['PCT']}} {p24h_s:>{col_w['PCT']}} "
            f"{icon} {label}{pat}"
        )

    # Score averages
    good_scores  = [e["score"] for e in entries if e["verdict"] == "GOOD"  and e["score"] > 0]
    false_scores = [e["score"] for e in entries if e["verdict"] == "FALSE" and e["score"] > 0]
    avg_good  = sum(good_scores)  / len(good_scores)  if good_scores  else None
    avg_false = sum(false_scores) / len(false_scores) if false_scores else None

    # Most common false-signal pattern
    false_pats = [e["pat_type"] for e in entries if e["verdict"] == "FALSE" and e["pat_type"]]
    top_false_pat = max(set(false_pats), key=false_pats.count) if false_pats else None

    lines: list[str] = [
        header,
        summary_line,
        "",
        f"<code>{chr(10).join(rows)}</code>",
        "",
    ]
    if avg_good is not None:
        lines.append(f"Ø Score good signals:  <b>{avg_good:.0f}</b>")
    if avg_false is not None:
        lines.append(f"Ø Score false signals: <b>{avg_false:.0f}</b>")
    if top_false_pat:
        lines.append(f"Most common false pattern: <b>{top_false_pat}</b>")

    lines += [
        "",
        "<i>GOOD: >5% in 4H  |  FALSE: down >3% in 4H  |  LEG2: dip >6% then recovered</i>",
    ]
    return "\n".join(lines)


def build_summary_message(stats) -> str:
    """/summary — Today's full stats (same as 08:01 daily summary)."""
    return build_m5_daily_summary(stats)


def build_passed_message(passed_candidates: list, last_scan_ts: str) -> str:
    """/passed — All M1–M7 candidates from the last Tier 1 scan with gate status."""
    if not passed_candidates:
        return "📋 <b>M1–M7 PASSED</b>\n\nNo candidates in last scan data yet."

    def _ck(ok: bool) -> str:
        return "✅" if ok else "❌"

    lines = [
        f"📋 <b>M1–M7 PASSED — Last scan {last_scan_ts}</b>",
        f"{len(passed_candidates)} coins qualified:",
        "",
    ]
    for i, p in enumerate(passed_candidates, 1):
        sym       = p.get("symbol", "?")
        score     = p.get("score", 0)
        mcap      = p.get("mcap", 0.0)
        ath_dist  = p.get("ath_dist_pct", 0.0)
        m5_ok     = p.get("m5_ok", False)
        m15_ok    = p.get("m15_ok", False)
        h4_ok     = p.get("h4_ok", False)
        rec       = p.get("rec", "PENDING")
        detail    = p.get("detail", "")

        mcap_str  = f"${mcap/1e6:.0f}M" if mcap >= 1e6 else f"${mcap:.0f}"
        ath_str   = f"90d -{ath_dist:.0f}%" if ath_dist > 0 else "90d N/A"
        score_str = f"Score {score}" if score > 0 else rec

        tf_str = f"5m {_ck(m5_ok)} 15m {_ck(m15_ok)} 4H {_ck(h4_ok)}"
        detail_str = f" — {detail}" if detail and rec not in {"STRONG ENTRY", "WATCH", "MONITOR", "PENDING"} else ""

        lines.append(f"{i}. <b>{sym}</b>   {score_str} | {mcap_str} | {ath_str}")
        lines.append(f"   {tf_str}{detail_str}")

    return "\n".join(lines)


def build_tier2_message(active_watch: set, active_watch_ts: dict,
                        cmc_data_cache: dict, cmc_price_cache: dict) -> str:
    """/tier2 — Active Tier 2 watch coins (5m momentum, rescanned every 5 min)."""
    if not active_watch:
        return "👁️ <b>TIER 2 ACTIVE WATCH</b>\n\nNo coins in Tier 2 watch right now."

    import time as _time
    now = _time.time()

    lines = [
        f"👁️ <b>TIER 2 ACTIVE WATCH — {len(active_watch)} coins</b>",
        "Rescanned every 5 min",
        "",
    ]
    items = sorted(active_watch)
    for i, mexc_sym in enumerate(items, 1):
        sym   = mexc_sym.replace("_USDT", "")
        price = cmc_price_cache.get(sym, 0.0)
        ts    = active_watch_ts.get(mexc_sym, 0.0)

        price_str = f"${price:.6g}" if price > 0 else "N/A"
        if ts > 0:
            elapsed = int(now - ts)
            if elapsed < 60:
                added_str = f"added {elapsed}s ago"
            else:
                added_str = f"added {elapsed // 60}m ago"
        else:
            added_str = "active"

        lines.append(f"{i}. <b>{sym}</b>   {price_str} | 5m ✅ | {added_str}")

    return "\n".join(lines)


def build_blocked_message(scan_outcomes: list, last_scan_ts: str,
                          method_c_blocked: list) -> str:
    """/blocked — Coins blocked at 4H gate with reason, plus Method C coins below threshold."""
    blocked = [o for o in scan_outcomes if o.rec == "MACRO_BLOCKED"]

    lines = [f"🚫 <b>4H BLOCKED — Last scan {last_scan_ts}</b>"]

    if not blocked and not method_c_blocked:
        lines.append("\nNo coins were blocked in the last scan.")
        return "\n".join(lines)

    if blocked:
        lines.append(f"{len(blocked)} coins blocked:")
        lines.append("")
        for i, o in enumerate(blocked, 1):
            lines.append(f"{i}. <b>{o.symbol}</b>   {o.detail}")
    else:
        lines.append("")

    if method_c_blocked:
        lines.append("")
        lines.append("Method C (transitioning, F&amp;G &lt;40) — scored below 70:")
        for p in method_c_blocked:
            lines.append(f"• <b>{p['symbol']}</b> — {p['detail']}")

    return "\n".join(lines)


def build_help_message() -> str:
    """/help — All available bot commands."""
    return "\n".join([
        "📖 <b>BOT COMMANDS</b>",
        "",
        "/status   — Last scan time, today's alert counts, next scan ETA",
        "/coins    — Coins from the last 3 scans",
        "/top      — Today's top 3 scored coins (compact)",
        "/best     — Full alert for today's best coin",
        "/filters  — All active filter thresholds",
        "/explain COIN — What happened to a specific coin (e.g. /explain POLYX)",
        "/recovery — Near-miss Recovery Bounce candidates",
        "/stage0      — Pre-breakout watchlist (Stage 0 consolidating coins)",
        "/backtesting YYYY-MM-DD — Per-alert outcome report for a past date",
        "/summary  — Today's full stats summary",
        "/passed   — All M1–M7 candidates from last scan with gate status",
        "/tier2    — Active Tier 2 watch coins (5m momentum)",
        "/blocked  — Coins blocked at 4H gate with reason",
        "/help     — This message",
        "",
        "/test     — Send a test alert (delivery check)",
        "/chatid   — Verify your chat ID configuration",
    ])


def send_scout_alert(coin, level: int) -> bool:
    """
    Send an instant Altcoin Scout alert.
      level=2  →  Trade Alert (build_scout_level2_alert)
      level=1  →  Coin on Radar (build_scout_level1_alert)
    """
    if level == 2:
        log.info(f"Sending Level-2 Trade Alert for {coin.symbol}…")
        return send_message(build_scout_level2_alert(coin))
    log.info(f"Sending Level-1 Radar alert for {coin.symbol}…")
    return send_message(build_scout_level1_alert(coin))


# ══════════════════════════════════════════════════════════════════════════════
# RADAR alert (Part A) — simple awareness message, no buttons
# ══════════════════════════════════════════════════════════════════════════════

def build_radar_alert(info: dict) -> str:
    """
    Simple RADAR awareness message. No trade action required.
    info keys: symbol, mexc_symbol, price, conds, m3_rsi6, m5_rsi6, tech
    """
    sym   = info["symbol"]
    price = info["price"]
    conds = info.get("conds", {})

    def ck(key: str) -> str:
        return "✅" if conds.get(key) else "❌"

    fd  = _fmt_price_dollar
    return (
        f"👁️ <b>RADAR: {sym}</b> — conditions forming\n"
        f"{_SEP}\n"
        f"3m {ck('3m')} | 5m {ck('5m')} | 10m ❌ | 15m {ck('15m')} | 4H {ck('4H')}\n"
        f"\n"
        f"<b>{sym}</b> is building momentum.\n"
        f"Watching for 10m cross to confirm.\n"
        f"Current: {fd(price)}\n"
        f"{_SEP}"
    )


def send_radar_alert(info: dict) -> bool:
    """Send a RADAR awareness alert."""
    log.info(f"Sending RADAR alert for {info.get('symbol')} @ {info.get('price'):.4g}")
    return send_message(build_radar_alert(info))


# ══════════════════════════════════════════════════════════════════════════════
# Shared button builder — used by ALL alert types that have Breakout/Pullback
# ══════════════════════════════════════════════════════════════════════════════

def _short_id() -> str:
    """Generate a short unique ID for pending signal order storage."""
    return f"{int(time.time()) % 1_000_000:06d}"


def _build_coin_keyboard(coin, margin: float, leverage: int) -> "InlineKeyboardMarkup":
    """
    Register breakout + pullback pending orders for a MomentumResult coin and
    return an InlineKeyboardMarkup with the three standard buttons.
    Breakout = h24_high × 1.005, Pullback = 5m EMA20 — matches build_unified_alert.
    """
    import config as _cfg
    sym      = coin.symbol
    mexc_sym = getattr(coin, "mexc_symbol", f"{sym}_USDT")
    t        = coin.tech
    entry    = coin.entry_price or coin.price or 0.0
    sl_pct   = getattr(coin, 'sl_pct', None) or _cfg.MOMENTUM_SL_PCT

    # Breakout = 24H high × 1.005 (resistance break level)
    h24h     = t.h24_high if (t and t.h24_high > 0) else entry
    bk_price = round(h24h * 1.005, 8)

    # Pullback = 5m EMA20 if below entry, else entry
    pb_ema20 = (t.m5_ema20 if (t and t.m5_ema20 > 0 and t.m5_ema20 < entry * 0.9995) else 0.0)
    pb_price = round(pb_ema20 if pb_ema20 > 0 else entry, 8)

    sl_factor  = 1 - sl_pct / 100
    tp1_factor = 1.08
    tp2_factor = 1.15

    bk_sl    = round(bk_price * sl_factor,  8)
    bk_tp1   = round(bk_price * tp1_factor, 8)
    bk_tp2   = round(bk_price * tp2_factor, 8)
    pb_sl    = round(pb_price * sl_factor,  8)
    pb_tp1   = round(pb_price * tp1_factor, 8)
    pb_tp2   = round(pb_price * tp2_factor, 8)

    bk_id = _short_id()
    time.sleep(0.001)
    pb_id = _short_id() + "p"

    _pending_signal_orders[bk_id] = {
        "order_type": "breakout",
        "symbol":     sym,
        "mexc_symbol": mexc_sym,
        "side":       "BUY",
        "price":      bk_price,
        "sl":         bk_sl,
        "tp1":        bk_tp1,
        "tp2":        bk_tp2,
        "margin":     margin,
        "leverage":   leverage,
        "created_at": time.time(),
    }
    _pending_signal_orders[pb_id] = {
        "order_type": "pullback",
        "symbol":     sym,
        "mexc_symbol": mexc_sym,
        "side":       "BUY",
        "price":      pb_price,
        "sl":         pb_sl,
        "tp1":        pb_tp1,
        "tp2":        pb_tp2,
        "margin":     margin,
        "leverage":   leverage,
        "created_at": time.time(),
    }

    # Expire pending orders older than 24 h
    stale = time.time() - 86_400
    for k in [k for k, v in list(_pending_signal_orders.items()) if v.get("created_at", 0) < stale]:
        del _pending_signal_orders[k]

    fd = _fmt_price_dollar
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                f"✅ BREAKOUT {fd(bk_price)}",
                callback_data=json.dumps({"a": "o", "id": bk_id}, separators=(",", ":")),
            ),
            InlineKeyboardButton(
                f"✅ PULLBACK {fd(pb_price)}",
                callback_data=json.dumps({"a": "o", "id": pb_id}, separators=(",", ":")),
            ),
        ],
        [
            InlineKeyboardButton(
                "❌ Skip",
                callback_data=json.dumps({"a": "s", "sym": sym}, separators=(",", ":")),
            ),
        ],
    ])


def send_alert_with_buttons(coin, margin: float = 5.0, leverage: int = 5) -> bool:
    """Send any MomentumResult as a unified alert WITH inline keyboard buttons.

    entry_valid=False behaviour:
      - LEG_CONTINUATION, RECOVERY: exempt — sends text without buttons (leg 2+ may arrive late)
      - all other types: suppressed entirely — logged internally, never sent to Telegram
    """
    _rec   = getattr(coin, "recommendation", "")
    _valid = getattr(coin, "entry_valid", True)
    _exempt = _rec in ("LEG_CONTINUATION", "RECOVERY")

    if not _valid and not _exempt:
        log.info(
            f"Alert suppressed — entry expired: {getattr(coin, 'symbol', '?')} [{_rec}]"
        )
        return False

    text = build_unified_alert(coin, margin, leverage)
    if not _valid:
        # Exempt types (LEG_CONTINUATION / RECOVERY) — send text without order buttons
        return send_message(text)
    keyboard = _build_coin_keyboard(coin, margin, leverage)
    ok, _    = send_message_with_buttons(text, keyboard)
    return ok


def build_signal_text(info: dict) -> str:
    """
    Build the SIGNAL alert message body — unified format matching build_unified_alert.
    info keys: symbol, price, conds, cond_score, tech, bk_price, bk_sl, bk_tp1, bk_tp2,
               pb_price, pb_sl, pb_tp1, pb_tp2, margin, leverage, position_size,
               profit_tp1, profit_tp2, ath_dist_pct
    """
    sym      = info["symbol"]
    mexc_sym = info.get("mexc_symbol", f"{sym}_USDT")
    conds    = info.get("conds", {})
    score    = info.get("cond_score", 0)
    margin   = info.get("margin", 5)
    leverage = info.get("leverage", 5)
    ath_dist = info.get("ath_dist_pct", 0)

    def ck(key: str) -> str:
        return "✅" if conds.get(key) else "❌"

    s_em    = _score_emoji(min(score * 20, 100))
    line1   = f"🟢 <b>{sym}</b> — SIGNAL [{score}/5{s_em}]"

    # Meta line — MCap/Circ not available in SIGNAL dict, show 90d high only
    meta_line = f"MCap N/A | 90d -{ath_dist:.0f}% | Circ N/A"

    # TF status row
    tf_row = f"3m {ck('3m')}  5m {ck('5m')}  10m ✅  15m {ck('15m')}  4H {ck('4H')}"
    if _btc_bear_regime:
        tf_row += "  ⚠️ BEAR"

    # Tick-size aware price formatter
    scale = _get_price_scale(mexc_sym)
    def fp(p: float) -> str:
        return f"${p:.{scale}f}"

    # Levels — sorted high to low
    pb_price = info["pb_price"]
    bk_price = info["bk_price"]
    sl_pct   = 5.0  # standard SL for SIGNAL alerts
    sl       = pb_price * (1 - sl_pct / 100)
    tp1      = pb_price * 1.08
    tp2      = pb_price * 1.15
    pos_size = margin * leverage
    profit1  = pos_size * 0.08 * 0.59
    profit2  = pos_size * 0.15 * 0.41
    # 24H high from tech if available
    tech  = info.get("tech")
    h24h  = (tech.h24_high if (tech and tech.h24_high > 0) else bk_price)

    levels: list[tuple[float, str]] = [
        (tp2,      f"── TP2  +15% → +${profit2:.2f} (runner)"),
        (tp1,      f"── TP1  +8%  → +${profit1:.2f} (59% close)"),
        (h24h,     "── 24H High"),
        (bk_price, "── Breakout"),
        (pb_price, "── Pullback  ← best R/R"),
        (sl,       f"▁▁ SL  -{sl_pct:.0f}%"),
    ]
    levels.sort(key=lambda x: x[0], reverse=True)
    level_lines = [f"{fp(price)} {label}" for price, label in levels]

    lines = [line1, _SEP, meta_line, tf_row, "", "📊 LEVELS"]
    lines.extend(level_lines)
    lines.append("")
    return "\n".join(lines)


def send_signal_alert(info: dict) -> tuple[bool, int | None]:
    """
    Send SIGNAL alert with InlineKeyboard.
    Stores pending order details in _pending_signal_orders.
    Returns (success, message_id) — message_id is needed to edit after button press.
    """
    sym = info["symbol"]

    # Register pending orders (breakout and pullback variants)
    bk_id = _short_id()
    time.sleep(0.001)   # ensure unique timestamp
    pb_id = _short_id() + "p"

    _pending_signal_orders[bk_id] = {
        "order_type": "breakout",
        "symbol":     sym,
        "mexc_symbol": info.get("mexc_symbol", f"{sym}_USDT"),
        "side":       "BUY",
        "price":      info["bk_price"],
        "sl":         info["bk_sl"],
        "tp1":        info["bk_tp1"],
        "tp2":        info["bk_tp2"],
        "margin":     info["margin"],
        "leverage":   info["leverage"],
        "created_at": time.time(),
    }
    _pending_signal_orders[pb_id] = {
        "order_type": "pullback",
        "symbol":     sym,
        "mexc_symbol": info.get("mexc_symbol", f"{sym}_USDT"),
        "side":       "BUY",
        "price":      info["pb_price"],
        "sl":         info["pb_sl"],
        "tp1":        info["pb_tp1"],
        "tp2":        info["pb_tp2"],
        "margin":     info["margin"],
        "leverage":   info["leverage"],
        "created_at": time.time(),
    }

    # Expire old pending orders (>24H)
    stale = time.time() - 86_400
    for k in [k for k, v in list(_pending_signal_orders.items()) if v.get("created_at", 0) < stale]:
        del _pending_signal_orders[k]

    fd  = _fmt_price_dollar
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                f"✅ BREAKOUT {fd(info['bk_price'])}",
                callback_data=json.dumps({"a": "o", "id": bk_id}, separators=(",", ":")),
            ),
            InlineKeyboardButton(
                f"✅ PULLBACK {fd(info['pb_price'])}",
                callback_data=json.dumps({"a": "o", "id": pb_id}, separators=(",", ":")),
            ),
        ],
        [
            InlineKeyboardButton(
                "❌ Skip",
                callback_data=json.dumps({"a": "s", "sym": sym}, separators=(",", ":")),
            ),
        ],
    ])

    text = build_signal_text(info)
    log.info(f"Sending SIGNAL alert for {sym} with buttons (bk={info['bk_price']:.4g})")
    return send_message_with_buttons(text, keyboard)


# ══════════════════════════════════════════════════════════════════════════════
# Stand-alone test — run this file directly to send a live briefing to Telegram
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import modules.btc_context_analyzer as m1

    print("Fetching live BTC data…")
    ctx = m1.analyze()

    if ctx is None:
        print("ERROR: Module 1 failed. Check logs.")
        raise SystemExit(1)

    print(f"  Regime: {ctx.regime}  |  Price: ${ctx.btc_price:,.2f}  |  RSI: {ctx.rsi}")
    print(f"  F&G: {ctx.fear_greed_value} ({ctx.fear_greed_label})")
    print("\nSending daily briefing to Telegram…")

    ok = send_daily_briefing(ctx)
    print("Sent OK ✓" if ok else "FAILED — check token / chat ID in .env")
