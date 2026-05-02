"""
copy_trading_store.py — Persistence helpers for copy trading data.

Stores everything inside the existing JSON data file under two keys:
  data["copy_trading"]   — per-user settings + credential (encrypted)
  data["copy_trade_log"] — history of every executed / rejected copy trade

Credentials are encrypted with Fernet using a key derived from the bot token.
"""

import base64
import hashlib
import json
import os
from datetime import datetime, timezone

from cryptography.fernet import Fernet

from storage import load_data, save_data


# ── Encryption ─────────────────────────────────────────────────────────────────

def _get_cipher() -> Fernet:
    token = os.getenv("FOREX_BOT_TOKEN", "default_insecure_key_change_me")
    raw   = hashlib.sha256(token.encode()).digest()
    key   = base64.urlsafe_b64encode(raw)
    return Fernet(key)


def encrypt_credentials(creds: dict) -> str:
    """Encrypt a credentials dict → base64 string."""
    raw = json.dumps(creds).encode()
    return _get_cipher().encrypt(raw).decode()


def decrypt_credentials(enc_str: str) -> dict:
    """Decrypt → credentials dict."""
    raw = _get_cipher().decrypt(enc_str.encode())
    return json.loads(raw)


# ── User copy settings ─────────────────────────────────────────────────────────

def get_user_copy_settings(uid_str: str) -> dict:
    data = load_data()
    return data.get("copy_trading", {}).get(uid_str, {})


def save_user_copy_settings(uid_str: str, settings: dict):
    data = load_data()
    data.setdefault("copy_trading", {})[uid_str] = settings
    save_data(data)


def get_all_copy_users() -> list:
    data = load_data()
    return list(data.get("copy_trading", {}).keys())


def link_broker(uid_str: str, broker_type: str, credentials: dict) -> dict:
    """
    Link a broker account for a user.
    broker_type: "oanda" | "ctrader"
    credentials: raw dict with api keys / tokens
    Returns existing settings merged with new credentials.
    """
    existing = get_user_copy_settings(uid_str) or {}
    existing.update({
        "broker":               broker_type.lower(),
        "credentials_enc":      encrypt_credentials(credentials),
        "enabled":              False,   # must be explicitly turned on
        "risk_pct":             existing.get("risk_pct", 1.0),
        "max_trades":           existing.get("max_trades", 3),
        "daily_risk_limit_pct": existing.get("daily_risk_limit_pct", 5.0),
        "max_drawdown_pct":     existing.get("max_drawdown_pct", 10.0),
        "daily_risk_consumed_pct": existing.get("daily_risk_consumed_pct", 0.0),
        "linked_at":            datetime.now(timezone.utc).isoformat(),
    })
    save_user_copy_settings(uid_str, existing)
    return existing


def set_copy_enabled(uid_str: str, enabled: bool) -> bool:
    """Enable or disable copy trading for a user. Returns False if not linked."""
    settings = get_user_copy_settings(uid_str)
    if not settings:
        return False
    settings["enabled"] = enabled
    save_user_copy_settings(uid_str, settings)
    return True


def set_risk_pct(uid_str: str, pct: float) -> bool:
    settings = get_user_copy_settings(uid_str)
    if not settings:
        return False
    settings["risk_pct"] = max(0.1, min(10.0, pct))
    save_user_copy_settings(uid_str, settings)
    return True


# ── Copy trade log ─────────────────────────────────────────────────────────────

def log_copy_trade(uid_str: str, signal: dict, ok: bool,
                   order_id: str = "", fill_price: float = 0,
                   lots: float = 0, error: str = ""):
    data = load_data()
    data.setdefault("copy_trade_log", []).append({
        "uid":              uid_str,
        "timestamp":        datetime.now(timezone.utc).isoformat(),
        "pair":             signal.get("pair", ""),
        "direction":        signal.get("direction", ""),
        "entry":            signal.get("entry", 0),
        "stop_loss":        signal.get("stop_loss", 0),
        "take_profit":      signal.get("take_profit", 0),
        "rr":               signal.get("rr", 0),
        "confidence":       signal.get("confidence", 0),
        "lots":             lots,
        "ok":               ok,
        "order_id":         order_id,
        "fill_price":       fill_price,
        "error":            error,
        "source":           "scanner",
        "outcome":          "open" if ok else "rejected",
        "close_price":      None,
        "pnl":              None,
        "closed_at":        None,
    })
    # Keep last 500 records
    if len(data["copy_trade_log"]) > 500:
        data["copy_trade_log"] = data["copy_trade_log"][-500:]
    save_data(data)


def update_copy_trade_outcome(uid_str: str, order_id: str,
                               outcome: str, pnl: float):
    data   = load_data()
    log    = data.get("copy_trade_log", [])
    now    = datetime.now(timezone.utc).isoformat()
    for record in reversed(log):
        if record.get("uid") == uid_str and record.get("order_id") == order_id:
            record["outcome"]    = outcome
            record["pnl"]        = pnl
            record["closed_at"]  = now
            break
    save_data(data)


def log_open_positions_snapshot(uid_str: str, trades: list):
    """Lightweight snapshot for monitoring — stored in memory only."""
    pass   # extend later if persistent snapshots are needed


def get_user_trade_history(uid_str: str, limit: int = 10) -> list:
    data = load_data()
    log  = data.get("copy_trade_log", [])
    user = [r for r in log if r.get("uid") == uid_str]
    return user[-limit:]
