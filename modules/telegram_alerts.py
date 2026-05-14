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

# Lazily created bot singleton
_bot: Bot | None = None


# ══════════════════════════════════════════════════════════════════════════════
# Core send infrastructure
# ══════════════════════════════════════════════════════════════════════════════

def _get_bot() -> Bot:
    global _bot
    if _bot is None:
        if not cfg.TELEGRAM_BOT_TOKEN:
            raise ValueError(
                "TELEGRAM_BOT_TOKEN not set — copy .env.example → .env and fill in the token."
            )
        _bot = Bot(token=cfg.TELEGRAM_BOT_TOKEN)
    return _bot


async def _send_async(text: str) -> bool:
    """Send a single Telegram message asynchronously. Returns True on success."""
    if not cfg.TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_CHAT_ID not set — cannot send.")
        return False
    try:
        await _get_bot().send_message(
            chat_id                  = cfg.TELEGRAM_CHAT_ID,
            text                     = text,
            parse_mode               = ParseMode.HTML,
            disable_web_page_preview = True,
        )
        return True
    except Exception as e:
        log.error(f"Telegram send failed: {e}")
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
        "  <b>Signal Breakdown</b>",
        "",
        f"  {_dot(rsi_score)}  RSI ({rsi})        <b>{rsi_label}</b>  [{_fmt(rsi_score)}]",
        f"  {_dot(vol_score)}  Volume Trend      <b>{vol_label}</b>  [{_fmt(vol_score)}]",
        f"  {_dot(ema_score)}  EMA Cross         <b>{ema_label}</b>  [{_fmt(ema_score)}]",
        f"  {_dot(macd_score)}  MACD              <b>{macd_label}</b>  [{_fmt(macd_score)}]",
        "",
        f"  {bias_color}  <b>Score: {_fmt(total)} / 4  →  {bias_label}  {bias_tag}</b>",
        "",
        "  Details:",
        f"  · {rsi_detail}",
        f"  · {vol_detail}",
        f"  · {ema_detail}",
        f"  · {macd_detail}",
        "",
        f"  {context}",
        f"  {caution}",
        "",
        f"  {fg_note}",
    ]


# ══════════════════════════════════════════════════════════════════════════════
# Daily Briefing builder
# ══════════════════════════════════════════════════════════════════════════════

def build_daily_briefing(
    btc_context,                   # BTCContext from Module 1 — required
    setups:      list | None = None,      # list[CoinSetup] from Module 2 — placeholder until ready
    avoid_coins: list | None = None,      # list[AvoidCoin] from Module 2 — placeholder until ready
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

    # ── Section 6: Daily Recommendation ──────────────────────────────────────
    lines += [
        "─────────────────────────────",
        "💡  <b>DAILY RECOMMENDATION</b>",
        "─────────────────────────────",
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
        lines += [
            "",
            f"📊 <b>TECHNICAL: {tech_score}/{_PTS_MAX}</b>",
            "",
            f"4H Trend: {ck(t.h4_ema_ok)} | 4H KDJ: {ck(t.h4_kdj_ok)}",
            "",
            f"15m EMA: {ck(t.m15_ema_ok)} | RSI: {ck(t.m15_rsi6_ok)} {t.m15_rsi6:.1f}",
            "",
            f"KDJ: {ck(t.m15_kdj_ok)} J={t.m15_kdj_j:.1f} | MACD: {ck(t.m15_macd_ok)}",
            "",
            f"Volume: {ck(t.vol_ok)} {t.vol_pct:.0f}% of MA10",
        ]

    lines += [
        "",
        f"💎 <b>FUNDAMENTAL: {fund_score}/{_FUND_MAX}</b>",
        "",
        f"MCap: {_vol_human(coin.market_cap)} | Circ: {coin.circ_supply_pct:.0f}% | FDV/MCap: {coin.fdv_mcap_ratio:.1f}x",
        "",
        f"🎯 <b>TOTAL SCORE: {coin.total_score}/100</b>",
        "",
        "📋 <b>ACTION:</b>",
        "",
        f"Entry: <b>{_usd(coin.entry_price)}</b>",
        "",
        f"SL: <b>{_usd(coin.stop_loss)}</b> (-{coin.sl_pct:.0f}%) → Risk: -${coin.risk_usd:.0f}",
        "",
        f"TP1: <b>{_usd(coin.tp1)}</b> (+{cfg.MOMENTUM_TP1_PCT:.0f}%) → Reward: +${coin.reward_tp1_usd:.0f}",
        "",
        f"TP2: <b>{_usd(coin.tp2)}</b> (+{cfg.MOMENTUM_TP2_PCT:.0f}%) → Max: +${coin.reward_tp2_usd:.0f}",
        "",
        f"R/R: {coin.rr_str}",
    ]

    if coin.warnings:
        lines += ["", "⚠️ <b>RISKS:</b>"]
        for w in coin.warnings:
            lines.append(f"• {w}")

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


def send_momentum_cooling_alert(coin) -> bool:
    """Send a Module 5 cooling-down watchlist alert to Telegram."""
    t = coin.tech
    kdj_val = f"{t.h4_kdj_j:.1f}" if t is not None else "N/A"
    log.info(f"Sending COOLING alert for {coin.symbol} (KDJ J {kdj_val})…")
    return send_message(build_momentum_cooling_alert(coin))


def build_golden_cross_alert(coin) -> str:
    """
    Early-entry alert for a fresh 15m EMA6/EMA20 golden cross.
    Lower vol and 1H threshold than main pipeline — signal may be very fresh.
    """
    mexc_url = f"https://futures.mexc.com/exchange/{coin.mexc_symbol}"
    t = coin.tech

    lines = [
        f"⚡ <b>GOLDEN CROSS: {coin.symbol}</b> {coin.change_1h:+.2f}% (1H)",
        "EMA6 just crossed above EMA20 on 15m.",
        "Very early signal — move just starting.",
        "",
        f"Entry: <b>{_usd(coin.entry_price)}</b>",
        f"SL: -5% = <b>{_usd(coin.stop_loss)}</b>",
        f"TP1: +10% = <b>{_usd(coin.tp1)}</b>  |  TP2: +20% = <b>{_usd(coin.tp2)}</b>",
        "",
        "⚠️ <i>Early signal: verify chart before entry</i>",
    ]

    if t is not None and t.vol_pct < cfg.MOMENTUM_VOL_GC_WARN * 100:
        lines.append("⚠️ <i>Low volume at cross — confirm with price action before entry</i>")

    lines.append(f'<a href="{mexc_url}">{coin.mexc_symbol} on MEXC Futures</a>')
    return "\n".join(lines)


def send_golden_cross_alert(coin) -> bool:
    """Send a Module 5 Golden Cross early-entry alert to Telegram."""
    log.info(f"Sending GOLDEN CROSS alert for {coin.symbol} ({coin.change_1h:+.2f}% 1h)…")
    return send_message(build_golden_cross_alert(coin))


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
    total_alerts = stats.entry_alerts + stats.watch_alerts + stats.early_alerts + stats.gc_alerts
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
        f" | Early: {stats.early_alerts} | GC: {stats.gc_alerts})",
        best_line,
        f"Missed (4H overheated): <b>{stats.cooling_alerts}</b> coins",
    ])


def send_m5_daily_summary(stats) -> bool:
    log.info("Sending Module 5 daily summary…")
    return send_message(build_m5_daily_summary(stats))


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
