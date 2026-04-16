"""
copy_engine.py — Signal → trade execution pipeline.

When the scanner generates an entry alert, `route_signal()` is called.
For each recipient who has copy trading enabled, it:
  1. Loads their broker connection (OANDA or cTrader)
  2. Runs safety validation
  3. Executes the trade
  4. Logs the result to storage

User copy settings are stored in data["copy_trading"][uid_str]:
  {
    "enabled":              bool,
    "broker":               "oanda" | "ctrader",
    "credentials_enc":      str,          # Fernet-encrypted JSON string
    "risk_pct":             float,        # % of balance per trade (default 1)
    "max_trades":           int,          # concurrent trades allowed (default 3)
    "daily_risk_limit_pct": float,        # daily loss limit (default 5)
    "max_drawdown_pct":     float,        # account drawdown limit (default 10)
    "daily_risk_consumed_pct": float,     # updated after each loss (reset daily)
  }
"""

import json
import threading
from datetime import datetime, timezone

from broker_ctrader import CTraderConnector
from execution_engine import place_trade
from copy_trading_store import (
    get_user_copy_settings,
    log_copy_trade,
    decrypt_credentials,
)

# Active broker connections: {uid_str: BrokerBase instance}
_connections: dict = {}
_conn_lock = threading.Lock()


def _get_or_create_connector(uid_str: str, settings: dict):
    """
    Return a live broker connector for this user.
    Creates and connects if needed; returns None on failure.
    """
    with _conn_lock:
        broker = _connections.get(uid_str)
        if broker and broker.is_connected():
            return broker

        broker_type = settings.get("broker", "oanda").lower()
        try:
            creds_enc = settings.get("credentials_enc", "")
            creds     = decrypt_credentials(creds_enc)
        except Exception as e:
            print(f"[CopyEngine] Credential decrypt failed for {uid_str}: {e}", flush=True)
            return None

        broker = CTraderConnector()
        result = broker.connect(creds)
        if not result["ok"]:
            print(f"[CopyEngine] Broker connect failed for {uid_str}: {result['error']}",
                  flush=True)
            return None

        _connections[uid_str] = broker
        return broker


def route_signal(signal: dict, recipients: list):
    """
    Called after a scanner entry alert is sent.
    Routes the signal to all recipients who have copy trading enabled.
    Runs each user in a separate thread so they don't block each other.
    """
    if not recipients:
        return

    threads = []
    for uid in recipients:
        uid_str  = str(uid)
        settings = get_user_copy_settings(uid_str)
        if not settings or not settings.get("enabled", False):
            continue

        t = threading.Thread(
            target=_execute_for_user,
            args=(signal, uid_str, settings),
            daemon=True,
        )
        t.start()
        threads.append(t)

    # Let threads run without waiting — fire-and-forget is intentional
    # (the bot cannot block its main loop for trade execution)


def _execute_for_user(signal: dict, uid_str: str, settings: dict):
    """Execute a single trade for one user and log the outcome."""
    pair      = signal.get("pair", "?")
    direction = signal.get("direction", "?")
    print(f"[CopyEngine] Executing {pair} {direction} for user {uid_str}...", flush=True)

    broker = _get_or_create_connector(uid_str, settings)
    if not broker:
        log_copy_trade(uid_str, signal, ok=False,
                       error="broker_connection_failed", lots=0)
        return

    result = place_trade(signal, settings, broker)
    log_copy_trade(uid_str, signal, ok=result["ok"],
                   order_id=result.get("order_id", ""),
                   fill_price=result.get("fill_price", 0),
                   lots=result.get("lots", 0),
                   error=result.get("error", ""))

    if result["ok"]:
        print(f"[CopyEngine] ✅ {pair} {direction} filled for {uid_str} "
              f"| lots={result['lots']} price={result['fill_price']}", flush=True)
    else:
        print(f"[CopyEngine] ❌ {pair} {direction} FAILED for {uid_str}: "
              f"{result['error']}", flush=True)


def disconnect_user(uid_str: str):
    """Disconnect and remove a user's broker connection."""
    with _conn_lock:
        broker = _connections.pop(uid_str, None)
        if broker:
            broker.disconnect()


def get_user_broker_status(uid_str: str) -> dict:
    """Return connection status for display in /copystatus command."""
    settings = get_user_copy_settings(uid_str)
    if not settings:
        return {"linked": False, "enabled": False, "broker": ""}

    with _conn_lock:
        broker = _connections.get(uid_str)
        live   = broker.is_connected() if broker else False

    open_trades = []
    if live and broker:
        try:
            open_trades = broker.get_open_trades()
        except Exception:
            pass

    acct = {}
    if live and broker:
        try:
            acct = broker.get_account_info()
        except Exception:
            pass

    return {
        "linked":       True,
        "enabled":      settings.get("enabled", False),
        "broker":       settings.get("broker", ""),
        "connected":    live,
        "risk_pct":     settings.get("risk_pct", 1.0),
        "max_trades":   settings.get("max_trades", 3),
        "open_trades":  open_trades,
        "balance":      acct.get("balance", 0),
        "currency":     acct.get("currency", "USD"),
    }
