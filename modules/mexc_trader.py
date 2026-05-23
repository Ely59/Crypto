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

_BASE        = "https://contract.mexc.com"
_RECV_WINDOW = "5000"   # milliseconds; required in MEXC futures signature

# ── Daily loss tracking (reset at UTC midnight) ───────────────────────────────
_daily_risk_usd:   float = 0.0    # cumulative worst-case risk placed today
_daily_reset_date: str   = ""     # "YYYY-MM-DD" of last reset
_last_balance:     float = 0.0    # cached balance for loss-limit comparison


# ══════════════════════════════════════════════════════════════════════════════
# Auth helpers
# ══════════════════════════════════════════════════════════════════════════════

def _sign(api_key: str, api_secret: str, timestamp: str, body: str) -> str:
    """MEXC futures signature: HMAC-SHA256(secret, api_key + timestamp + recv_window + params)."""
    message = api_key + timestamp + _RECV_WINDOW + body
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
        "RecvWindow":   _RECV_WINDOW,
        "Signature":    _sign(key, secret, timestamp, body),
    }


def _post(endpoint: str, payload: dict) -> dict | None:
    """Authenticated POST to MEXC Contract API."""
    key      = cfg.MEXC_API_KEY or ""
    ts       = str(int(time.time() * 1000))
    body_str = json.dumps(payload, separators=(",", ":"))
    sign_msg = key + ts + _RECV_WINDOW + body_str
    print(f"[MEXC DEBUG] POST {endpoint}")
    print(f"[MEXC DEBUG] payload: {body_str}")
    print(f"[MEXC DEBUG] sign-string (first 200): {sign_msg[:200]}")
    log.debug(f"MEXC sign-string: {sign_msg[:200]}")
    headers  = _auth_headers(ts, body_str)
    if headers is None:
        return None
    try:
        resp = requests.post(f"{_BASE}{endpoint}", headers=headers,
                             data=body_str, timeout=10)
        print(f"[MEXC DEBUG] STATUS: {resp.status_code}")
        print(f"[MEXC DEBUG] BODY: {resp.text[:2000]}")
        log.error(
            f"MEXC POST {endpoint} HTTP {resp.status_code} — body: {resp.text[:500]}"
        ) if resp.status_code != 200 else None
        try:
            return resp.json()
        except Exception:
            log.error(f"MEXC POST {endpoint}: response is not JSON — body: {resp.text[:500]}")
            return None
    except Exception as e:
        print(f"[MEXC DEBUG] EXCEPTION: {e}")
        log.error(f"MEXC POST {endpoint} failed: {e}")
        return None


def _get_auth(endpoint: str, params: dict | None = None) -> dict | None:
    """Authenticated GET to MEXC Contract API."""
    ts        = str(int(time.time() * 1000))
    param_str = "&".join(f"{k}={v}" for k, v in sorted((params or {}).items()))
    headers   = _auth_headers(ts, param_str)
    if headers is None:
        return None
    print(f"[MEXC DEBUG] GET {endpoint}  params={params}")
    try:
        resp = requests.get(f"{_BASE}{endpoint}", headers=headers,
                            params=params, timeout=10)
        print(f"[MEXC DEBUG] STATUS: {resp.status_code}")
        print(f"[MEXC DEBUG] BODY: {resp.text[:2000]}")
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"[MEXC DEBUG] EXCEPTION: {e}")
        log.error(f"MEXC GET {endpoint} failed: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════════════
# Contract detail cache
# ══════════════════════════════════════════════════════════════════════════════

_contract_detail_cache: dict[str, dict] = {}


def _get_contract_detail(symbol: str) -> dict:
    """
    Fetch and cache MEXC contract detail for a symbol.
    Returns dict with at least {"contractSize": 1, "priceScale": 4, "minVol": 1}.
    Falls back to safe defaults on error.
    """
    if symbol in _contract_detail_cache:
        return _contract_detail_cache[symbol]
    defaults = {"contractSize": 1, "priceScale": 4, "minVol": 1, "volUnit": 1}
    try:
        resp = requests.get(
            f"{_BASE}/api/v1/contract/detail",
            params={"symbol": symbol},
            timeout=8,
        )
        data = resp.json()
        print(f"[MEXC DEBUG] contract/detail STATUS: {resp.status_code}")
        print(f"[MEXC DEBUG] contract/detail BODY: {resp.text[:800]}")
        if data.get("success") and data.get("data"):
            detail = data["data"]
            _contract_detail_cache[symbol] = detail
            return detail
    except Exception as e:
        print(f"[MEXC DEBUG] contract/detail EXCEPTION: {e}")
        log.warning(f"Could not fetch contract detail for {symbol}: {e}")
    return defaults


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

    # Step 1a — set position mode to One-Way (required by MEXC before orders)
    pm_resp = _post("/api/v1/private/position/change_margin_mode", {
        "symbol":     sym,
        "marginMode": 1,   # 1 = One-Way (hedge = 2)
    })
    if pm_resp is not None and not pm_resp.get("success"):
        log.warning(f"Set position mode for {sym}: {pm_resp.get('message')}")

    # Step 1b — set leverage
    lev_resp = _post("/api/v1/private/position/change_leverage", {
        "symbol":   sym,
        "leverage": leverage,
        "openType": 1,   # 1 = isolated
    })
    if lev_resp and not lev_resp.get("success"):
        log.warning(f"Set leverage {leverage}x for {sym}: {lev_resp.get('message')}")

    # Step 2 — calculate quantity (contracts)
    # MEXC: vol = position_size_usdt / (contractSize × price)
    # contractSize varies per coin (e.g. SKYAI_USDT=10, BTC_USDT=0.0001)
    detail        = _get_contract_detail(sym)
    contract_size = float(detail.get("contractSize", 1) or 1)
    min_vol       = int(detail.get("minVol", 1) or 1)
    position_size = margin_usdt * leverage
    raw_qty       = position_size / (contract_size * price) if price > 0 else 1
    quantity      = max(min_vol, round(raw_qty))
    print(f"[MEXC DEBUG] qty calc: pos={position_size:.4f} contractSize={contract_size} price={price:.6g} raw={raw_qty:.3f} → vol={quantity}")

    # Step 3 — submit limit order
    # MEXC side: 1=open long, 2=close long, 3=open short, 4=close short
    api_side  = 1 if side.upper() == "BUY" else 3
    price_scale = int(detail.get("priceScale", 4) or 4)
    order_resp = _post("/api/v1/private/order/submit", {
        "symbol":   sym,
        "side":     api_side,
        "openType": 1,       # isolated
        "type":     1,       # limit
        "price":    f"{price:.{price_scale}f}",
        "vol":      str(quantity),
        "leverage": leverage,
    })

    if not order_resp or not order_resp.get("success"):
        err  = (order_resp or {}).get("message", "Unknown error")
        code = (order_resp or {}).get("code", "?")
        log.error(f"Order submit failed for {sym}: code={code} message={err}")
        try:
            from modules.telegram_alerts import send_message as _tg_send
            _tg_send(
                f"⚠️ <b>Order failed</b>: {sym}\n"
                f"Error: <code>{err}</code>  (code {code})\n"
                f"Check MEXC API key has <b>Futures Trading</b> permission enabled."
            )
        except Exception:
            pass
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
            f"⛔ Max {cfg.MAX_OPEN_POSITIONS} positions active.\n"
            "Close one before opening new trade."
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


# ══════════════════════════════════════════════════════════════════════════════
# Debug test — run directly: python modules/mexc_trader.py
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys as _sys
    import os as _os
    _ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    if _ROOT not in _sys.path:
        _sys.path.insert(0, _ROOT)

    print("=== MEXC Futures Debug Test ===")
    print(f"Base URL  : {_BASE}")
    print(f"RecvWindow: {_RECV_WINDOW}")
    print(f"API key   : {cfg.MEXC_API_KEY[:8]}…" if cfg.MEXC_API_KEY else "API key   : NOT SET")
    print(f"API secret: {'SET' if cfg.MEXC_API_SECRET else 'NOT SET'}")
    print()

    # Step 1: check contract detail for SKYAI to confirm symbol format
    print("--- Step 1: Contract detail for SKYAI_USDT ---")
    detail_resp = requests.get(
        f"{_BASE}/api/v1/contract/detail",
        params={"symbol": "SKYAI_USDT"},
        timeout=10,
    )
    print(f"STATUS: {detail_resp.status_code}")
    print(f"BODY  : {detail_resp.text[:1000]}")
    print()

    # Step 2: account balance (authenticated GET)
    print("--- Step 2: Account balance ---")
    bal = get_account_balance()
    print(f"Balance: {bal}")
    print()

    # Step 3: fetch current SKYAI price (ticker, then klines fallback)
    print("--- Step 3: SKYAI current price ---")
    skyai_price = 0.0
    try:
        ticker_resp = requests.get(
            f"{_BASE}/api/v1/contract/ticker",
            params={"symbol": "SKYAI_USDT"},
            timeout=8,
        )
        print(f"Ticker STATUS: {ticker_resp.status_code}")
        print(f"Ticker BODY  : {ticker_resp.text[:400]}")
        tdata = ticker_resp.json().get("data") or {}
        skyai_price = float(tdata.get("lastPrice", 0) or 0)
    except Exception as _e:
        print(f"Ticker error: {_e}")
    if skyai_price <= 0:
        try:
            from utils.api_client import get_mexc_futures_klines
            df = get_mexc_futures_klines("SKYAI_USDT", "1h", limit=2)
            if df is not None and not df.empty:
                skyai_price = float(df["close"].iloc[-1])
        except Exception as _e2:
            print(f"Klines fallback error: {_e2}")
    print(f"SKYAI price: {skyai_price}")
    print()

    if skyai_price <= 0:
        print("Cannot place test order — price unavailable.")
        _sys.exit(1)

    # Step 4: test order — $1 margin, 5× leverage, limit buy
    print("--- Step 4: Place test order (SKYAI $1 margin, 5× leverage) ---")
    sl_price  = round(skyai_price * 0.94, 8)
    tp1_price = round(skyai_price * 1.10, 8)
    result = place_futures_order(
        symbol      = "SKYAI_USDT",
        side        = "BUY",
        order_type  = "LIMIT",
        price       = skyai_price,
        margin_usdt = 1.0,
        leverage    = 5,
        sl_price    = sl_price,
        tp1_price   = tp1_price,
    )
    print()
    print(f"=== RESULT ===")
    print(result)
