"""
modules/mexc_trader.py — MEXC Futures API integration.
Handles authenticated order placement, balance queries, and position checks.
All functions return None on failure rather than raising, so the bot never crashes.
"""
from __future__ import annotations

import csv
import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timezone

import requests

import config as cfg
from utils.logger import get_logger

log = get_logger(__name__)

_BASE = "https://contract.mexc.com"

# ── Daily loss tracking (reset at UTC midnight) ───────────────────────────────
_daily_risk_usd:   float = 0.0    # cumulative worst-case risk placed today
_daily_reset_date: str   = ""     # "YYYY-MM-DD" of last reset
_last_balance:     float = 0.0    # cached balance for loss-limit comparison


# ══════════════════════════════════════════════════════════════════════════════
# Auth helpers
# ══════════════════════════════════════════════════════════════════════════════

def _sign(api_key: str, api_secret: str, timestamp: str, body: str) -> str:
    """MEXC signature: HMAC-SHA256(secret, api_key + timestamp + body_string)."""
    message = api_key + timestamp + body
    return hmac.new(
        api_secret.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _auth_headers(timestamp: str, body: str) -> dict | None:
    """Build authenticated request headers. Returns None if keys are missing."""
    key    = cfg.MEXC_API_KEY
    secret = cfg.MEXC_API_SECRET
    if not key or not secret:
        log.error("MEXC_API_KEY / MEXC_API_SECRET not set — cannot authenticate.")
        return None
    return {
        "Content-Type": "application/json",
        "Apikey":       key,
        "Request-Time": timestamp,
        "Signature":    _sign(key, secret, timestamp, body),
    }


def _post(endpoint: str, payload: dict) -> dict | None:
    """Authenticated POST to MEXC Contract API."""
    ts       = str(int(time.time() * 1000))
    body_str = json.dumps(payload, separators=(",", ":"))
    headers  = _auth_headers(ts, body_str)
    if headers is None:
        return None
    try:
        resp = requests.post(f"{_BASE}{endpoint}", headers=headers,
                             data=body_str, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log.error(f"MEXC POST {endpoint} failed: {e}")
        return None


def _get_auth(endpoint: str, params: dict | None = None) -> dict | None:
    """Authenticated GET to MEXC Contract API."""
    ts        = str(int(time.time() * 1000))
    param_str = "&".join(f"{k}={v}" for k, v in sorted((params or {}).items()))
    headers   = _auth_headers(ts, param_str)
    if headers is None:
        return None
    try:
        resp = requests.get(f"{_BASE}{endpoint}", headers=headers,
                            params=params, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log.error(f"MEXC GET {endpoint} failed: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════════════
# Public API
# ══════════════════════════════════════════════════════════════════════════════

def get_account_balance() -> float | None:
    """Return available USDT futures balance, or None on failure."""
    data = _get_auth("/api/v1/private/account/assets")
    if not data:
        return None
    assets = data.get("data") or []
    # Response is either a list of asset dicts or a single dict
    if isinstance(assets, list):
        for asset in assets:
            if isinstance(asset, dict) and asset.get("currency") == "USDT":
                try:
                    bal = float(asset.get("availableBalance", 0))
                    global _last_balance
                    _last_balance = bal
                    return bal
                except (TypeError, ValueError):
                    pass
    elif isinstance(assets, dict):
        try:
            bal = float(assets.get("availableBalance", 0))
            _last_balance = bal
            return bal
        except (TypeError, ValueError):
            pass
    log.error(f"Unexpected account assets shape: {type(assets)}")
    return None


def get_open_positions() -> list | None:
    """Return list of currently open positions, or None on API failure."""
    data = _get_auth("/api/v1/private/position/open_positions")
    if data is None:
        return None
    return data.get("data") or []


def place_futures_order(
    symbol:      str,
    side:        str,    # "BUY" or "SELL"
    order_type:  str,    # "LIMIT"
    price:       float,
    margin_usdt: float,
    leverage:    int,
    sl_price:    float,
    tp1_price:   float,
) -> dict | None:
    """
    Place a MEXC Futures isolated-margin limit order with a stop-loss plan order.

    Returns a result dict on success:
        {order_id, symbol, price, quantity, sl_price, tp1_price, status}
    Returns {error: str, ...} on failure (never None so callers can inspect).
    """
    # Normalise symbol
    sym = symbol.upper().replace("-", "_").replace("/", "_")
    if "_USDT" not in sym:
        sym = sym + "_USDT"

    # Sanity-clamp margin
    margin_usdt = max(cfg.MARGIN_MIN_USDT, min(cfg.MARGIN_MAX_USDT, margin_usdt))

    # Step 1 — set leverage
    lev_resp = _post("/api/v1/private/position/change_leverage", {
        "symbol":   sym,
        "leverage": leverage,
        "openType": 1,   # 1 = isolated
    })
    if lev_resp and not lev_resp.get("success"):
        log.warning(f"Set leverage {leverage}x for {sym}: {lev_resp.get('message')}")

    # Step 2 — calculate quantity (contracts)
    position_size = margin_usdt * leverage
    quantity      = max(1, round(position_size / price))

    # Step 3 — submit limit order
    # MEXC side: 1=open long, 2=close long, 3=open short, 4=close short
    api_side  = 1 if side.upper() == "BUY" else 3
    order_resp = _post("/api/v1/private/order/submit", {
        "symbol":   sym,
        "side":     api_side,
        "openType": 1,       # isolated
        "type":     1,       # limit
        "price":    str(round(price, 8)),
        "vol":      str(quantity),
        "leverage": leverage,
    })

    if not order_resp or not order_resp.get("success"):
        err = (order_resp or {}).get("message", "Unknown error")
        log.error(f"Order submit failed for {sym}: {err}")
        return {"error": err, "symbol": sym, "price": price, "quantity": quantity}

    order_id = str(order_resp.get("data", ""))
    log.info(f"Order placed: {sym} {side} {quantity} @ {price:.6g}  ID={order_id}")

    # Step 4 — set stop-loss via plan (trigger) order
    sl_side = 2 if api_side == 1 else 4   # close long = 2, close short = 4
    sl_exec = round(sl_price * 0.99, 8)
    sl_resp = _post("/api/v1/private/planorder/place", {
        "symbol":         sym,
        "side":           sl_side,
        "orderType":      2,
        "triggerPrice":   str(round(sl_price, 8)),
        "executionPrice": str(sl_exec),
        "vol":            str(quantity),
        "openType":       1,
        "leverage":       leverage,
    })
    if not sl_resp or not sl_resp.get("success"):
        log.warning(f"SL plan order failed for {sym}: {(sl_resp or {}).get('message')}")

    # Track daily risk
    _record_risk(margin_usdt, cfg.MOMENTUM_SL_PCT)

    return {
        "order_id":  order_id,
        "symbol":    sym,
        "price":     price,
        "quantity":  quantity,
        "sl_price":  sl_price,
        "tp1_price": tp1_price,
        "status":    "placed",
    }


def cancel_order(symbol: str, order_id: str) -> bool:
    """Cancel an open order. Returns True on success."""
    sym  = symbol.upper().replace("-", "_").replace("/", "_")
    resp = _post("/api/v1/private/order/cancel", {"orderId": order_id, "symbol": sym})
    if resp and resp.get("success"):
        log.info(f"Order {order_id} cancelled.")
        return True
    log.warning(f"Cancel failed for {order_id}: {(resp or {}).get('message')}")
    return False


# ══════════════════════════════════════════════════════════════════════════════
# Safety helpers
# ══════════════════════════════════════════════════════════════════════════════

def _record_risk(margin: float, sl_pct: float) -> None:
    """Track cumulative worst-case daily risk after placing an order."""
    global _daily_risk_usd, _daily_reset_date
    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    if _daily_reset_date != today:
        _daily_risk_usd   = 0.0
        _daily_reset_date = today
    _daily_risk_usd += margin * (sl_pct / 100)


def daily_loss_exceeded(balance: float | None = None) -> bool:
    """Return True if cumulative daily risk exceeds the configured 15% limit."""
    bal = balance or _last_balance
    if bal <= 0:
        return False
    return _daily_risk_usd > bal * (cfg.DAILY_LOSS_LIMIT_PCT / 100)


def check_safety(margin: float) -> tuple[bool, str]:
    """
    Run all pre-order safety checks.
    Returns (ok, error_message). ok=True means safe to proceed.
    """
    # Rule 1: open position count
    positions = get_open_positions()
    if positions is not None and len(positions) >= cfg.MAX_OPEN_POSITIONS:
        return False, (
            f"⛔ Max {cfg.MAX_OPEN_POSITIONS} positions active. "
            "Close one before opening a new trade."
        )

    # Rule 2: daily loss limit
    balance = get_account_balance()
    if daily_loss_exceeded(balance):
        return False, "⛔ Daily loss limit reached. No new orders today."

    # Rule 3: margin range
    if margin < cfg.MARGIN_MIN_USDT or margin > cfg.MARGIN_MAX_USDT:
        margin = 10.0   # silent fallback — caller should re-check

    # Rule 4: insufficient balance
    if balance is not None and balance < margin:
        return False, f"⛔ Insufficient balance: ${balance:.2f} available (need ${margin:.0f})."

    return True, ""


# ══════════════════════════════════════════════════════════════════════════════
# Trade log
# ══════════════════════════════════════════════════════════════════════════════

def log_trade(
    symbol:   str,
    action:   str,
    price:    float,
    margin:   float,
    leverage: int,
    order_id: str,
    status:   str,
) -> None:
    """Append a row to the trade log CSV."""
    os.makedirs("logs", exist_ok=True)
    try:
        write_header = not os.path.exists(cfg.TRADE_LOG_CSV)
        with open(cfg.TRADE_LOG_CSV, "a", newline="") as f:
            writer = csv.writer(f)
            if write_header:
                writer.writerow(["timestamp", "symbol", "action", "price",
                                 "margin", "leverage", "order_id", "status"])
            writer.writerow([
                datetime.utcnow().isoformat(),
                symbol, action, price, margin, leverage, order_id, status,
            ])
    except Exception as e:
        log.error(f"Trade log write failed: {e}")
