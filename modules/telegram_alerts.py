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
from datetime import datetime
from zoneinfo import ZoneInfo      # stdlib Python 3.9+

from telegram           import Bot
from telegram.constants import ParseMode

from utils.logger import get_logger
import config as cfg

log = get_logger(__name__)

# Stuttgart = Central European Time, auto-switches CET↔CEST
_STUTTGART_TZ = ZoneInfo("Europe/Berlin")

# ══════════════════════════════════════════════════════════════════════════════
# Core send infrastructure
# ══════════════════════════════════════════════════════════════════════════════

async def _send_async(text: str) -> bool:
    """Send a single Telegram message asynchronously. Returns True on success."""
    if not cfg.TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_CHAT_ID not set — cannot send.")
        return False
    if not cfg.TELEGRAM_BOT_TOKEN:
        log.error("TELEGRAM_BOT_TOKEN not set — cannot send.")
        return False
    try:
        log.debug(f"Sending to chat_id={cfg.TELEGRAM_CHAT_ID!r}")
        async with Bot(token=cfg.TELEGRAM_BOT_TOKEN) as bot:
            await bot.send_message(
                chat_id                  = int(cfg.TELEGRAM_CHAT_ID),
                text                     = text,
                parse_mode               = ParseMode.HTML,
                disable_web_page_preview = True,
            )
        return True
    except Exception as e:
        log.error(f"Telegram send failed (chat_id={cfg.TELEGRAM_CHAT_ID!r}): {e}")
        return False


def send_message(text: str) -> bool:
    """
    Synchronous entry point — safe to call from any non-async code.
    Splits messages that exceed Telegram's 4 096-char hard limit,
    breaking on newline boundaries to keep formatting intact.
    """
    if not text:
        return False

    MAX = 4000   # conservative limit below Telegram's 4096 to leave room for HTML tags

    if len(text) <= MAX:
        return asyncio.run(_send_async(text))

    # Build chunks by accumulating lines until we approach the limit
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for line in text.splitlines(keepends=True):
        if current_len + len(line) > MAX and current:
            chunks.append("".join(current))
            current     = []
            current_len = 0
        current.append(line)
        current_len += len(line)

    if current:
        chunks.append("".join(current))

    return all(asyncio.run(_send_async(chunk)) for chunk in chunks)


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


def _signal_chain_lines(coin) -> list[str]:
    """
    Build the 4H/15m/5m signal chain block shown above the entry in all alerts.
    Returns a list of HTML lines (empty list if tech data unavailable).
    """
    t = coin.tech
    if t is None:
        return []

    def ck(ok: bool) -> str:
        return "✅" if ok else "❌"

    # 4H line
    macd_arrow = "↑" if t.h4_macd_ok else "↓"
    h4_rsi_str = f"RSI {t.h4_rsi6:.0f}" if t.h4_rsi6 > 0 else f"KDJ {t.h4_kdj_j:.0f}"
    h4_line = f"4H:  {ck(t.h4_ema_ok)} EMA bullish | {h4_rsi_str} | MACD {macd_arrow}"
    if not t.h4_ema_ok:
        h4_line = f"4H:  ❌ EMA bearish [{t.h4_ema6:.4g}/{t.h4_ema12:.4g}/{t.h4_ema20:.4g}]"

    # 15m line
    ema_ok_15m = t.m15_ema6_gt_ema12 if t.m15_ema6_gt_ema12 is not None else t.m15_ema_ok
    if ema_ok_15m and t.m15_rsi6 <= 72:
        m15_line = f"15m: {ck(ema_ok_15m)} EMA bullish | RSI {t.m15_rsi6:.0f} | Vol {t.vol_pct:.0f}% MA10"
    else:
        concern = ""
        if not ema_ok_15m:
            concern = f" EMA6 < EMA12"
        if t.m15_rsi6 > 72:
            concern += f" RSI {t.m15_rsi6:.0f} — überhitzt"
        m15_line = f"15m: {ck(ema_ok_15m)} {concern.strip() if concern else 'EMA bullish'} | RSI {t.m15_rsi6:.0f}"

    # 5m line
    if t.m5_rsi6 > 0:
        kdj_arrow = "↑" if t.m5_kdj_rising else "↓"
        px_sym    = "&gt;" if t.m5_price_above_ema20 else "&lt;"
        m5_overall_ok = t.m5_ok
        m5_line = (f"5m:  {ck(m5_overall_ok)} KDJ J: {t.m5_kdj_j:.0f}{kdj_arrow}"
                   f" | RSI {t.m5_rsi6:.0f} | Price {px_sym} EMA20")
        if not m5_overall_ok and t.m5_note:
            m5_line += f" ← {t.m5_note.split(' — ')[0].replace('⏳ ', '').replace('⚠️ ', '')}"
    else:
        m5_line = "5m:  ⚫ n/v"

    # Entry zone — use 5m EMA20 when available (CHANGE 6C), else fall back to entry_price
    ez_base = t.m5_ema20 if t.m5_ema20 > 0 else coin.entry_price
    ez_lo   = ez_base * (1 - cfg.MOMENTUM_ENTRY_ZONE_PCT / 100)
    ez_hi   = ez_base * (1 + cfg.MOMENTUM_ENTRY_ZONE_PCT / 100)
    if t.m5_ema20 > 0:
        zone_line = (
            f"→ Entry-Zone: {_fmt_price(ez_lo)} – {_fmt_price(ez_hi)}"
            f" (5m EMA20 ± {cfg.MOMENTUM_ENTRY_ZONE_PCT:.1f}% — verify on 1m before entry)"
        )
    else:
        zone_line = f"→ Entry-Zone: {_fmt_price(ez_lo)} – {_fmt_price(ez_hi)}"

    return ["", h4_line, m15_line, m5_line, zone_line, ""]


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
) -> bool:
    """
    Build and send the morning briefing.
    Only btc_context is required; setups and avoid_coins show placeholders when absent.
    """
    log.info("Building daily briefing…")
    msg = build_daily_briefing(btc_context, setups, avoid_coins)
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
    """
    Telegram alert for a coin that scored ≥ 65/100 (STRONG ENTRY or WATCH).

    Exact format specified by user:
      [emoji] [COIN] +X% (1H) — [RECOMMENDATION]
      📊 TECHNICAL: X/60
      4H Trend / KDJ / 15m EMA / RSI / KDJ / MACD / Volume
      💎 FUNDAMENTAL: X/40
      🎯 TOTAL SCORE: X/100
      📋 ACTION: Entry / SL / TP1 / TP2 / R/R
      ⚠️ RISKS (auto-generated, max 3)
      MEXC link
    """
    from modules.momentum_scanner import _PTS_MAX, _FUND_MAX
    t = coin.tech
    f = coin.fund

    def ck(ok: bool) -> str:
        return "✅" if ok else "❌"

    tech_score = t.score if t is not None else 0
    fund_score = f.total if f is not None else 0

    lines = [
        f"{coin.rec_emoji} <b>{coin.symbol}</b> {coin.change_1h:+.2f}% (1H) — <b>{coin.recommendation}</b>",
    ]
    if coin.recommendation == "EARLY SIGNAL":
        lines.append("<i>Move just starting — tighter SL advised</i>")

    if t is not None:
        kdj_note = (f" ⚠️ {t.h4_kdj_j:.0f}" if not t.h4_kdj_ok else f" {t.h4_kdj_j:.0f}")
        lines += [
            "",
            f"📊 <b>TECHNICAL: {tech_score}/{_PTS_MAX}</b>",
            "",
            f"4H Trend: {ck(t.h4_ema_ok)} | 4H KDJ:{kdj_note}",
            "",
            f"15m EMA: {ck(t.m15_ema_ok)} | RSI: {ck(t.m15_rsi6_ok)} {t.m15_rsi6:.1f}",
            "",
            f"KDJ: {ck(t.m15_kdj_ok)} J={t.m15_kdj_j:.1f} | MACD: {ck(t.m15_macd_ok)}",
            "",
            f"Volume: {ck(t.vol_ok)} {t.vol_pct:.0f}% of MA10",
        ]

    if coin.ath_pts > 0 and coin.tech is not None:
        lines.append(f"📊 ATH distance: -{coin.tech.ath_dist_pct:.0f}% from 16D peak (+{coin.ath_pts} pts)")

    lines += [
        "",
        f"💎 <b>FUNDAMENTAL: {fund_score}/{_FUND_MAX}</b>",
        "",
        f"MCap: {_vol_human(coin.market_cap)} | Circ: {coin.circ_supply_pct:.0f}% | FDV/MCap: {coin.fdv_mcap_ratio:.1f}x",
        "",
        f"🎯 <b>TOTAL SCORE: {coin.total_score}/100</b>",
    ]
    lines += _signal_chain_lines(coin)
    lines += [
        "📋 <b>ACTION:</b>",
        "",
        f"Entry: <b>{_fmt_price(coin.entry_price)}</b>",
        "",
        f"SL (-{coin.sl_pct:.0f}%): <b>{_fmt_price(coin.stop_loss)}</b>",
        "",
        f"TP1 (+{cfg.MOMENTUM_TP1_PCT:.0f}%): <b>{_fmt_price(coin.tp1)}</b> → 60% close",
        "",
        f"TP2 (+{cfg.MOMENTUM_TP2_PCT:.0f}%): <b>{_fmt_price(coin.tp2)}</b>",
        "",
    ]

    ath_dist = coin.ath_dist_pct if coin.ath_dist_pct > 0 else (coin.tech.ath_dist_pct if coin.tech else 0.0)
    h4_status = "✅ Bullish" if (coin.tech and coin.tech.h4_ema_ok) else "❌ Bearish"
    lines += [
        f"ATH-Dist: -{ath_dist:.0f}% 📊 | Score: {coin.total_score}/100",
        f"MCap: {_vol_human(coin.market_cap)} | 4H: {h4_status}",
        "",
        f"R/R: {coin.rr_str}",
    ]

    all_warnings = list(coin.warnings)
    if coin.m5_note and coin.m5_note not in all_warnings:
        all_warnings.append(coin.m5_note)
    if all_warnings:
        lines += ["", "⚠️ <b>RISKS:</b>"]
        for w in all_warnings:
            lines.append(f"⚠️ {w}")

    mexc_url = f"https://futures.mexc.com/exchange/{coin.mexc_symbol}"
    lines += ["", f'<a href="{mexc_url}">{coin.mexc_symbol} on MEXC Futures</a>']

    return "\n".join(lines)


def send_momentum_alert(coin) -> bool:
    """Send a Module 5 momentum alert (≥65/100) to Telegram."""
    log.info(
        f"Sending {coin.recommendation} alert for "
        f"{coin.symbol} ({coin.change_1h:+.2f}% 1h, {coin.total_score}/100)…"
    )
    return send_message(build_momentum_alert(coin))


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


def build_volume_spike_alert(coin) -> str:
    """
    Pre-signal alert: current 15m candle volume ≥ 3× avg of prior 3 candles.
    Fires before a GC or main breakout — early warning, not a confirmed entry.
    """
    mexc_url = f"https://futures.mexc.com/exchange/{coin.mexc_symbol}"
    t = coin.tech
    ratio = f"{t.m15_vol_spike_ratio:.1f}" if t is not None else "?"

    ath_dist  = coin.ath_dist_pct if coin.ath_dist_pct > 0 else (t.ath_dist_pct if t else 0.0)
    h4_status = "✅ Bullish" if (t and t.h4_ema_ok) else "❌ Bearish"

    lines = [
        f"⚡ <b>VOLUME SPIKE: {coin.symbol}</b> {coin.change_1h:+.2f}% (1H)",
        f"Volume <b>{ratio}×</b> normal — move may be starting.",
        "Watch for entry. Not confirmed yet.",
    ]
    lines += _signal_chain_lines(coin)
    lines += [
        f"Entry: <b>{_fmt_price(coin.entry_price)}</b>",
        f"SL (-{cfg.MOMENTUM_VS_SL_PCT:.0f}%): <b>{_fmt_price(coin.stop_loss)}</b>",
        f"TP1 (+{cfg.MOMENTUM_TP1_PCT:.0f}%): <b>{_fmt_price(coin.tp1)}</b> → 60% close",
        f"TP2 (+{cfg.MOMENTUM_TP2_PCT:.0f}%): <b>{_fmt_price(coin.tp2)}</b>",
        "",
        f"ATH-Dist: -{ath_dist:.0f}% 📊 | Score: {coin.total_score}/100",
        f"MCap: {_vol_human(coin.market_cap)} | 4H: {h4_status}",
    ]

    if coin.warnings:
        for w in coin.warnings:
            lines.append(f"⚠️ {w}")

    lines += [
        "",
        "⚠️ <i>Pre-signal only — wait for GC or breakout confirmation before entry</i>",
        f'<a href="{mexc_url}">{coin.mexc_symbol} on MEXC Futures</a>',
    ]
    return "\n".join(lines)


def send_volume_spike_alert(coin) -> bool:
    """Send a Module 5 Volume Spike pre-signal alert to Telegram."""
    log.info(f"Sending VOLUME SPIKE alert for {coin.symbol} ({coin.change_1h:+.2f}% 1h)…")
    return send_message(build_volume_spike_alert(coin))


def build_recovery_alert(coin) -> str:
    """
    Recovery Bounce alert: coin pulled back ≥8% from its 24H high
    and is now rising again with bullish 4H EMA.
    """
    mexc_url = f"https://futures.mexc.com/exchange/{coin.mexc_symbol}"
    t = coin.tech
    prev_peak = _usd(t.h24_high) if t is not None else "?"
    pullback_pct = ((t.h24_high - coin.entry_price) / t.h24_high * 100) if t is not None and t.h24_high > 0 else 0.0

    ath_dist  = coin.ath_dist_pct if coin.ath_dist_pct > 0 else (t.ath_dist_pct if t else 0.0)
    h4_status = "✅ Bullish" if (t and t.h4_ema_ok) else "❌ Bearish"

    lines = [
        f"♻️ <b>RECOVERY: {coin.symbol}</b> {coin.change_1h:+.2f}% (1H)",
        f"Post-pump bounce — previous 24H peak: <b>{prev_peak}</b>",
        f"Pulled back <b>{pullback_pct:.1f}%</b> — now recovering.",
    ]
    lines += _signal_chain_lines(coin)
    lines += [
        f"Entry: <b>{_fmt_price(coin.entry_price)}</b>",
        f"SL (-{cfg.MOMENTUM_RB_SL_PCT:.0f}%): <b>{_fmt_price(coin.stop_loss)}</b>",
        f"TP1 (+{cfg.MOMENTUM_RB_TP1_PCT:.0f}%): <b>{_fmt_price(coin.tp1)}</b> → 60% close",
        f"TP2 (+{cfg.MOMENTUM_RB_TP2_PCT:.0f}%): <b>{_fmt_price(coin.tp2)}</b>",
        "",
        f"ATH-Dist: -{ath_dist:.0f}% 📊 | Score: {coin.total_score}/100",
        f"MCap: {_vol_human(coin.market_cap)} | 4H: {h4_status}",
    ]

    if coin.warnings:
        for w in coin.warnings:
            lines.append(f"⚠️ {w}")

    lines += [
        "",
        "⚠️ <i>Old peak acts as resistance — take partial profits early</i>",
        f'<a href="{mexc_url}">{coin.mexc_symbol} on MEXC Futures</a>',
    ]
    return "\n".join(lines)


def send_recovery_alert(coin) -> bool:
    """Send a Module 5 Recovery Bounce alert to Telegram."""
    log.info(
        f"Sending RECOVERY alert for {coin.symbol} "
        f"({coin.change_1h:+.2f}% 1h, pulled back from {coin.tech.h24_high if coin.tech else '?'})…"
    )
    return send_message(build_recovery_alert(coin))


def build_golden_cross_alert(coin) -> str:
    """
    Early-entry alert for a fresh 15m EMA6/EMA20 golden cross.
    Lower vol and 1H threshold than main pipeline — signal may be very fresh.
    """
    mexc_url = f"https://futures.mexc.com/exchange/{coin.mexc_symbol}"
    t = coin.tech

    ath_dist  = coin.ath_dist_pct if coin.ath_dist_pct > 0 else (t.ath_dist_pct if t else 0.0)
    h4_status = "✅ Bullish" if (t and t.h4_ema_ok) else "❌ Bearish"

    lines = [
        f"⚡ <b>GOLDEN CROSS: {coin.symbol}</b> {coin.change_1h:+.2f}% (1H)",
        "EMA6 just crossed above EMA20 on 15m.",
        "Very early signal — move just starting.",
    ]
    lines += _signal_chain_lines(coin)
    lines += [
        f"Entry: <b>{_fmt_price(coin.entry_price)}</b>",
        f"SL (-{cfg.MOMENTUM_GC_SL_PCT:.0f}%): <b>{_fmt_price(coin.stop_loss)}</b>",
        f"TP1 (+{cfg.MOMENTUM_TP1_PCT:.0f}%): <b>{_fmt_price(coin.tp1)}</b> → 60% close",
        f"TP2 (+{cfg.MOMENTUM_TP2_PCT:.0f}%): <b>{_fmt_price(coin.tp2)}</b>",
        "",
        f"ATH-Dist: -{ath_dist:.0f}% 📊 | Score: {coin.total_score}/100",
        f"MCap: {_vol_human(coin.market_cap)} | 4H: {h4_status}",
    ]

    if coin.warnings:
        for w in coin.warnings:
            lines.append(f"⚠️ {w}")

    if t is not None and t.vol_pct < cfg.MOMENTUM_VOL_GC_WARN * 100:
        lines.append("⚠️ Low volume at cross — confirm with price action before entry")

    lines += ["", "⚠️ <i>Early signal: verify chart before entry</i>",
              f'<a href="{mexc_url}">{coin.mexc_symbol} on MEXC Futures</a>']
    return "\n".join(lines)


def send_golden_cross_alert(coin) -> bool:
    """Send a Module 5 Golden Cross early-entry alert to Telegram."""
    log.info(f"Sending GOLDEN CROSS alert for {coin.symbol} ({coin.change_1h:+.2f}% 1h)…")
    return send_message(build_golden_cross_alert(coin))


def build_pbw_alert(coin) -> str:
    """🔍 PRE-BREAKOUT Watch alert — slow-grind accumulation before breakout."""
    mexc_url  = f"https://futures.mexc.com/exchange/{coin.mexc_symbol}"
    h4_status = "✅ Bullish"   # PBW only fires when 4H EMA stack is bullish
    score_str = str(coin.total_score) if coin.total_score > 0 else "—"

    lines = [
        f"🔍 <b>PRE-BREAKOUT: {coin.symbol}</b> {coin.change_1h:+.2f}% (1H)",
        f"RSI&lt;45 seit {coin.m1_rsi_streak} Kerzen | EMAs komprimiert ({coin.m1_ema_spread:.3f}%)",
        f"Erste Volumen-Kerze bestätigt ({coin.m1_vol_ratio:.1f}×).",
    ]
    lines += _signal_chain_lines(coin)
    lines += [
        f"Entry: <b>{_fmt_price(coin.entry_price)}</b>",
        f"SL (-{cfg.MOMENTUM_PBW_SL_PCT:.0f}%): <b>{_fmt_price(coin.stop_loss)}</b>",
        f"TP1 (+{cfg.MOMENTUM_PBW_TP1_PCT:.0f}%): <b>{_fmt_price(coin.tp1)}</b> → 60% close",
        f"TP2 (+{cfg.MOMENTUM_PBW_TP2_PCT:.0f}%): <b>{_fmt_price(coin.tp2)}</b>",
        "",
        f"ATH-Dist: -{coin.ath_dist_pct:.0f}% 📊 | Score: {score_str}/100",
        f"MCap: {_vol_human(coin.market_cap)} | 4H: {h4_status}",
    ]

    if coin.warnings:
        for w in coin.warnings:
            lines.append(f"⚠️ {w}")

    lines.append(f'<a href="{mexc_url}">{coin.mexc_symbol} on MEXC Futures</a>')
    return "\n".join(lines)


def send_pbw_alert(coin) -> bool:
    """Send a Module 5 Pre-Breakout Watch alert to Telegram."""
    log.info(f"Sending PRE-BREAKOUT alert for {coin.symbol} ({coin.change_1h:+.2f}% 1h)…")
    return send_message(build_pbw_alert(coin))


def build_staircase_alert(coin) -> str:
    """🪜 STAIRCASE Continuation alert — consolidation pause before leg 2."""
    mexc_url  = f"https://futures.mexc.com/exchange/{coin.mexc_symbol}"
    t         = coin.tech
    h4_status = "✅ Bullish" if (t and t.h4_ema_ok) else "❌ Bearish"
    vol_pct   = f"{t.vol_pct:.0f}%" if t else "?"
    rsi_val   = f"{t.m15_rsi6:.1f}" if t else "?"
    kdj_val   = f"{t.m15_kdj_j:.1f}" if t else "?"
    ath_dist  = coin.ath_dist_pct if coin.ath_dist_pct > 0 else (t.ath_dist_pct if t else 0.0)
    score_str = str(coin.total_score) if coin.total_score > 0 else "—"

    lines = [
        f"🪜 <b>STAIRCASE: {coin.symbol}</b> — Konsolidierung vor Leg 2",
        f"4H Trend intakt | 15m Vol: {vol_pct} von MA10",
        f"RSI abgekühlt: {rsi_val} | KDJ J: {kdj_val}",
        f"Vorheriger Leg: +{coin.sc_prior_move:.1f}% ✅",
    ]
    lines += _signal_chain_lines(coin)
    lines += [
        f"Entry: <b>{_fmt_price(coin.entry_price)}</b>",
        f"SL (-{cfg.MOMENTUM_SC_SL_PCT:.0f}%): <b>{_fmt_price(coin.stop_loss)}</b>",
        f"TP1 (+{cfg.MOMENTUM_SC_TP1_PCT:.0f}%): <b>{_fmt_price(coin.tp1)}</b> → 60% close",
        f"TP2 (+{cfg.MOMENTUM_SC_TP2_PCT:.0f}%): <b>{_fmt_price(coin.tp2)}</b>",
        "",
        f"ATH-Dist: -{ath_dist:.0f}% 📊 | Score: {score_str}/100",
        f"MCap: {_vol_human(coin.market_cap)} | 4H: {h4_status}",
    ]

    if coin.warnings:
        for w in coin.warnings:
            lines.append(f"⚠️ {w}")

    lines.append(f'<a href="{mexc_url}">{coin.mexc_symbol} on MEXC Futures</a>')
    return "\n".join(lines)


def send_staircase_alert(coin) -> bool:
    """Send a Module 5 Staircase Continuation alert to Telegram."""
    log.info(f"Sending STAIRCASE alert for {coin.symbol} ({coin.change_1h:+.2f}% 1h)…")
    return send_message(build_staircase_alert(coin))


def build_squeeze_alert(coin) -> str:
    """
    💥 BB-Squeeze Breakout alert.
    Fires when a coin breaks out of weeks of EMA compression on extreme 15m volume.
    """
    mexc_url   = f"https://futures.mexc.com/exchange/{coin.mexc_symbol}"
    t          = coin.tech
    vol_ratio  = f"{t.m15_vol_spike_ratio:.1f}" if t else "?"
    rsi_val    = f"{t.m15_rsi6:.0f}"            if t else "?"
    compress_d = t.h4_compression_days           if t else 0
    ath_dist   = coin.ath_dist_pct if coin.ath_dist_pct > 0 else (t.ath_dist_pct if t else 0.0)

    lines = [
        f"💥 <b>SQUEEZE BREAKOUT: {coin.symbol}</b> {coin.change_1h:+.2f}% (1H)",
        "Weeks of compression → exploding NOW.",
        f"Vol <b>{vol_ratio}×</b> normal — move starting.",
        "⚡ Fast signal — act within 5 minutes.",
    ]

    if t is not None:
        kdj_arrow  = "↑" if t.m5_kdj_rising else "↓"
        m5_kdj_str = f"{t.m5_kdj_j:.0f}{kdj_arrow}" if t.m5_rsi6 > 0 else "n/v"
        m5_rsi_str = f"{t.m5_rsi6:.0f}"              if t.m5_rsi6 > 0 else "n/v"
        lines += [
            "",
            f"4H:  EMA compressed <b>{compress_d} days</b> | Now breaking out",
            f"15m: Vol <b>{vol_ratio}×</b> | RSI <b>{rsi_val}</b> | Price &gt; EMA20",
            f"5m:  KDJ J: <b>{m5_kdj_str}</b> | RSI <b>{m5_rsi_str}</b>",
        ]

    ep = coin.entry_price
    lines += [
        "",
        f"Entry:  <b>{_fmt_price(ep)}</b>",
        f"SL (-{cfg.MOMENTUM_SQ_SL_PCT:.0f}%): <b>{_fmt_price(coin.stop_loss)}</b>",
        f"TP1 (+{cfg.MOMENTUM_SQ_TP1_PCT:.0f}%): <b>{_fmt_price(coin.tp1)}</b> → 60% close, SL to entry",
        f"TP2 (+{cfg.MOMENTUM_SQ_TP2_PCT:.0f}%): <b>{_fmt_price(coin.tp2)}</b> → trailing SL",
        "",
        f"ATH-Dist: -{ath_dist:.0f}% 📊 | Score: {coin.total_score}/100",
        f"MCap: {_vol_human(coin.market_cap)} | Compression: {compress_d} days",
    ]

    if coin.warnings:
        for w in coin.warnings:
            lines.append(f"⚠️ {w}")

    lines.append(f'<a href="{mexc_url}">{coin.mexc_symbol} on MEXC Futures</a>')
    return "\n".join(lines)


def send_squeeze_alert(coin) -> bool:
    """Send a BB-Squeeze Breakout alert to Telegram."""
    log.info(
        f"Sending SQUEEZE BREAKOUT alert for {coin.symbol} "
        f"({coin.change_1h:+.2f}% 1h, score {coin.total_score})…"
    )
    return send_message(build_squeeze_alert(coin))


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
        "EARLY SIGNAL":  "🔍 Early Signal",
        "SQUEEZE":       "💥 BB-Squeeze",
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
    _no_score = {"COOLING_DOWN", "GOLDEN CROSS", "VOLUME SPIKE", "RECOVERY", "EARLY SIGNAL"}
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


def build_top_message(top_results: list) -> str:
    """/top — Today's top 3 scored coins (compact summary)."""
    if not top_results:
        return "No scored coins today yet.\nBe patient — the first alert of the day usually appears by mid-morning."

    lines = ["🏆 <b>TODAY'S TOP SCORED COINS</b>", ""]
    for i, r in enumerate(top_results, 1):
        lines += [
            f"{i}. {r.rec_emoji} <b>{r.symbol}</b> — {r.total_score}/100 ({r.recommendation})",
            f"   {r.change_1h:+.2f}% (1H)  |  Entry: {_usd(r.entry_price)}",
            f"   SL: {_usd(r.stop_loss)}  TP1: {_usd(r.tp1)}  TP2: {_usd(r.tp2)}",
            "",
        ]
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


def build_summary_message(stats) -> str:
    """/summary — Today's full stats (same as 08:01 daily summary)."""
    return build_m5_daily_summary(stats)


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
        "/summary  — Today's full stats summary",
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
