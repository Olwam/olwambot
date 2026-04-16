"""
trade_monitor.py — Background thread that syncs open positions from all
connected brokers every 30 seconds and logs outcomes.

Detects TP / SL hits by checking whether a previously tracked trade
is no longer in the broker's open positions.  When a trade closes,
the P&L is recorded and copy_trade_log is updated.
"""

import threading
import time
from datetime import datetime, timezone

from copy_trading_store import (
    get_all_copy_users,
    get_user_copy_settings,
    update_copy_trade_outcome,
    log_open_positions_snapshot,
)


_monitor_thread = None
_stop_event     = threading.Event()

# Per-user snapshot: {uid_str: {order_id: trade_dict}}
_snapshots: dict = {}
_snap_lock       = threading.Lock()


def start_monitor(notify_callback=None, interval_seconds: int = 30):
    """
    Start the trade monitor background thread.
    notify_callback(uid, text) — optional function to send Telegram messages.
    """
    global _monitor_thread
    if _monitor_thread and _monitor_thread.is_alive():
        return

    _stop_event.clear()
    _monitor_thread = threading.Thread(
        target=_monitor_loop,
        kwargs={"notify": notify_callback, "interval": interval_seconds},
        daemon=True,
    )
    _monitor_thread.start()
    print(f"[TradeMonitor] Started (interval={interval_seconds}s).", flush=True)


def stop_monitor():
    _stop_event.set()


def _monitor_loop(notify, interval: int):
    while not _stop_event.is_set():
        try:
            _run_sync_cycle(notify)
        except Exception as e:
            print(f"[TradeMonitor] Error in sync cycle: {e}", flush=True)
        _stop_event.wait(interval)


def _run_sync_cycle(notify):
    """Sync all connected users' positions once."""
    from copy_engine import _connections, _conn_lock

    with _conn_lock:
        active = dict(_connections)   # snapshot to avoid holding lock

    if not active:
        return

    for uid_str, broker in active.items():
        if not broker.is_connected():
            continue
        try:
            _sync_user(uid_str, broker, notify)
        except Exception as e:
            print(f"[TradeMonitor] sync error for {uid_str}: {e}", flush=True)


def _sync_user(uid_str: str, broker, notify):
    """Check one user's positions against the previous snapshot."""
    current_trades = broker.get_open_trades()
    current_ids    = {t["order_id"] for t in current_trades}
    current_map    = {t["order_id"]: t for t in current_trades}

    with _snap_lock:
        previous = _snapshots.get(uid_str, {})
        _snapshots[uid_str] = current_map

    # Detect closed trades
    for order_id, old_trade in previous.items():
        if order_id not in current_ids:
            _handle_closed_trade(uid_str, old_trade, broker, notify)

    # Save open positions snapshot for analytics
    log_open_positions_snapshot(uid_str, current_trades)


def _handle_closed_trade(uid_str: str, trade: dict, broker, notify):
    """
    A previously tracked trade is no longer open — it hit TP, SL, or was closed.
    Try to determine the outcome and log it.
    """
    pair      = trade.get("symbol", "")
    direction = trade.get("direction", "")
    entry     = trade.get("open_price", 0)
    sl        = trade.get("sl", 0)
    tp        = trade.get("tp", 0)
    order_id  = trade.get("order_id", "")

    # Best-effort outcome detection from current price
    try:
        price_data    = broker.get_price(pair)
        current_price = price_data.get("bid", 0) if direction == "BUY" else price_data.get("ask", 0)
    except Exception:
        current_price = 0

    if tp and current_price:
        if direction == "BUY" and current_price >= tp:
            outcome = "tp_hit"
        elif direction == "SELL" and current_price <= tp:
            outcome = "tp_hit"
        elif direction == "BUY" and current_price <= sl:
            outcome = "sl_hit"
        elif direction == "SELL" and current_price >= sl:
            outcome = "sl_hit"
        else:
            outcome = "manually_closed"
    else:
        outcome = "closed"

    pnl = trade.get("unrealized_pnl", 0)

    update_copy_trade_outcome(
        uid_str  = uid_str,
        order_id = order_id,
        outcome  = outcome,
        pnl      = pnl,
    )

    print(f"[TradeMonitor] {uid_str} — {pair} {direction} closed: {outcome} "
          f"pnl≈{pnl}", flush=True)

    # Notify user via Telegram if callback provided
    if notify:
        icon = "✅" if outcome == "tp_hit" else ("🛑" if outcome == "sl_hit" else "📤")
        msg  = (
            f"{icon} Copy Trade Closed\n\n"
            f"Pair: {pair} {direction}\n"
            f"Result: {outcome.replace('_', ' ').title()}\n"
            f"Est. P&L: {'+' if pnl >= 0 else ''}{round(pnl, 2)} USD\n\n"
            f"Order: {order_id}"
        )
        try:
            notify(int(uid_str), msg)
        except Exception:
            pass
