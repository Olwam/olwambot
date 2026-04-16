"""
execution_engine.py — Broker-agnostic safety validator + order dispatcher.

Every trade goes through validate_trade() before reaching the broker.
If validation passes, place_trade() routes to the correct broker connector.

Safety rules enforced here (not in the broker connector):
  1. max open trades per user (default 3)
  2. max daily risk consumed
  3. spread too high
  4. ATR too low (dead market)
  5. news risk = high
  6. broker not connected
"""

import time
from datetime import datetime, timezone

from indicators import estimate_atr
from news_data import get_relevant_news_block


# Spread limits per pair (price, not pips)
_SPREAD_LIMITS = {
    "EURUSD": 0.00035,
    "GBPUSD": 0.00050,
    "USDJPY": 0.050,
    "XAUUSD": 0.80,
    "DEFAULT": 0.00120,
}

# Minimum ATR (as fraction of expected) before rejecting for dead market
_MIN_ATR_RATIO = 0.30


def validate_trade(signal: dict, user_settings: dict, broker,
                   open_trades: list) -> dict:
    """
    Run all safety checks before any order is placed.

    Returns:
      {"ok": True/False, "reason": str}
    """
    pair      = signal.get("pair", "")
    direction = signal.get("direction", "")
    entry     = signal.get("entry", 0)
    sl        = signal.get("stop_loss", 0)
    tp        = signal.get("take_profit", 0)

    # ── 1. Broker connected ────────────────────────────────────────────────────
    if not broker or not broker.is_connected():
        return {"ok": False, "reason": "broker_not_connected"}

    # ── 2. Valid signal fields ─────────────────────────────────────────────────
    if not pair or not direction or not entry or not sl or not tp:
        return {"ok": False, "reason": "invalid_signal_fields"}
    if direction not in ("BUY", "SELL"):
        return {"ok": False, "reason": "invalid_direction"}

    # ── 3. Max open trades ────────────────────────────────────────────────────
    max_trades = int(user_settings.get("max_trades", 3))
    if len(open_trades) >= max_trades:
        return {"ok": False,
                "reason": f"max_trades_reached ({len(open_trades)}/{max_trades})"}

    # ── 4. Spread check ───────────────────────────────────────────────────────
    price_data = broker.get_price(pair)
    if price_data.get("error"):
        return {"ok": False, "reason": f"price_fetch_error: {price_data['error']}"}

    spread       = price_data.get("spread", 0)
    spread_limit = _SPREAD_LIMITS.get(pair.upper(), _SPREAD_LIMITS["DEFAULT"])
    if spread > spread_limit * 2:
        return {"ok": False,
                "reason": f"spread_too_high ({round(spread, 6)} > {spread_limit * 2})"}

    # ── 5. Dead market (ATR too low) ──────────────────────────────────────────
    current_price = price_data.get("ask", entry) or entry
    expected_atr  = estimate_atr(pair, current_price)
    sl_dist       = abs(current_price - sl)
    if expected_atr > 0 and sl_dist < expected_atr * _MIN_ATR_RATIO:
        return {"ok": False,
                "reason": f"dead_market (sl_dist {round(sl_dist, 5)} < {_MIN_ATR_RATIO}x ATR)"}

    # ── 6. News risk ──────────────────────────────────────────────────────────
    news = get_relevant_news_block(pair)
    if news.get("risk") == "high":
        return {"ok": False, "reason": f"news_risk_high: {news.get('message','')}"}

    # ── 7. Daily risk limit ───────────────────────────────────────────────────
    max_daily_risk = float(user_settings.get("daily_risk_limit_pct", 5.0))
    daily_consumed = float(user_settings.get("daily_risk_consumed_pct", 0.0))
    risk_pct       = float(user_settings.get("risk_pct", 1.0))
    if daily_consumed + risk_pct > max_daily_risk:
        return {"ok": False,
                "reason": f"daily_risk_limit_reached "
                          f"({round(daily_consumed + risk_pct, 1)}% > {max_daily_risk}%)"}

    # ── 8. Drawdown protection ────────────────────────────────────────────────
    max_drawdown = float(user_settings.get("max_drawdown_pct", 10.0))
    account_info = broker.get_account_info()
    balance      = account_info.get("balance", 0)
    equity       = account_info.get("equity", balance)
    if balance > 0:
        drawdown = (balance - equity) / balance * 100
        if drawdown >= max_drawdown:
            return {"ok": False,
                    "reason": f"drawdown_limit ({round(drawdown, 1)}% >= {max_drawdown}%)"}

    return {"ok": True, "reason": ""}


def calculate_lot_size(entry: float, sl: float, balance: float,
                       risk_pct: float, pair: str, broker_name: str) -> float:
    """
    Calculate position size based on fixed-risk money management.

    Returns lot size in:
      - OANDA: units (integer, e.g. 10000)
      - cTrader: lots (float, e.g. 0.10)
    """
    if balance <= 0 or entry <= 0 or sl <= 0:
        return 0

    risk_amount  = balance * risk_pct / 100.0
    sl_distance  = abs(entry - sl)
    if sl_distance <= 0:
        return 0

    pair_upper = pair.upper().replace("/", "")

    if broker_name == "OANDA":
        # OANDA units — 1 standard lot = 100,000 units
        # pip_value ≈ 10 USD per lot for majors (simplified)
        if "JPY" in pair_upper:
            pip       = 0.01
            pip_value = 9.0   # USD per lot (approx)
        elif "XAU" in pair_upper:
            pip       = 0.01
            pip_value = 1.0
        else:
            pip       = 0.0001
            pip_value = 10.0

        sl_pips       = sl_distance / pip
        risk_per_lot  = sl_pips * pip_value
        lots          = risk_amount / risk_per_lot if risk_per_lot > 0 else 0
        units         = lots * 100_000
        # Clamp: min 1000, max 1,000,000; round to nearest 100
        units = max(1000, min(1_000_000, round(units / 100) * 100))
        return float(units)

    else:
        # cTrader lots (0.01 minimum, 2 decimal places)
        if "JPY" in pair_upper:
            pip       = 0.01
            pip_value = 9.0
        elif "XAU" in pair_upper:
            pip       = 0.01
            pip_value = 1.0
        else:
            pip       = 0.0001
            pip_value = 10.0

        sl_pips      = sl_distance / pip
        risk_per_lot = sl_pips * pip_value
        lots         = risk_amount / risk_per_lot if risk_per_lot > 0 else 0
        lots         = max(0.01, min(50.0, round(lots, 2)))
        return float(lots)


def place_trade(signal: dict, user_settings: dict, broker) -> dict:
    """
    Full pipeline: validate → calculate lot size → place order → log.

    Returns:
      {
        "ok": bool, "order_id": str, "fill_price": float,
        "lots": float, "error": str, "rejection_reason": str
      }
    """
    pair      = signal.get("pair", "")
    direction = signal.get("direction", "")
    entry     = float(signal.get("entry", 0))
    sl        = float(signal.get("stop_loss", 0))
    tp        = float(signal.get("take_profit", 0))

    # Fetch open trades for limit check
    try:
        open_trades = broker.get_open_trades()
    except Exception:
        open_trades = []

    # Safety validation
    val = validate_trade(signal, user_settings, broker, open_trades)
    if not val["ok"]:
        print(f"[Execution] REJECTED {pair} {direction}: {val['reason']}", flush=True)
        return {
            "ok": False, "order_id": "", "fill_price": 0,
            "lots": 0, "error": val["reason"], "rejection_reason": val["reason"],
        }

    # Get account balance for lot size
    acct    = broker.get_account_info()
    balance = acct.get("balance", 0)
    if balance <= 0:
        return {
            "ok": False, "order_id": "", "fill_price": 0,
            "lots": 0, "error": "Could not fetch account balance",
            "rejection_reason": "no_balance",
        }

    risk_pct = float(user_settings.get("risk_pct", 1.0))
    lots     = calculate_lot_size(entry, sl, balance, risk_pct, pair, broker.broker_name)
    if lots <= 0:
        return {
            "ok": False, "order_id": "", "fill_price": 0,
            "lots": 0, "error": "Lot size calculated as zero",
            "rejection_reason": "zero_lot_size",
        }

    print(f"[Execution] Placing {direction} {pair} "
          f"| lots={lots} sl={sl} tp={tp} "
          f"| broker={broker.broker_name}", flush=True)

    # Attempt to place — retry once on failure
    result = broker.place_market_order(pair, direction, lots, sl, tp)
    if not result["ok"] and "requote" in result.get("error", "").lower():
        print(f"[Execution] Requote on {pair} — retrying once...", flush=True)
        time.sleep(1)
        result = broker.place_market_order(pair, direction, lots, sl, tp)

    result["lots"]             = lots
    result["rejection_reason"] = "" if result["ok"] else result.get("error", "")
    if result["ok"]:
        print(f"[Execution] {pair} order filled: id={result['order_id']} "
              f"price={result['fill_price']}", flush=True)
    else:
        print(f"[Execution] {pair} order FAILED: {result['error']}", flush=True)

    return result
