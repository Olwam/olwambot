"""
Outcome checker — background task that resolves pending scanner alerts.

After a set time window based on the alert's timeframe, it fetches the
current price and determines whether TP or SL was hit first (using
high/low candle data where possible, falling back to current price).

Timeframe resolution windows:
  M5  → check after 30 min
  M15 → check after 60 min
  M30 → check after 120 min
  H1  → check after 240 min
  H4  → check after 720 min
"""

import time
from datetime import datetime, timezone, timedelta

from config import ADMIN_IDS, OUTCOME_FOLLOWUP_ENABLED
from storage import (
    get_pending_scanner_alerts,
    update_alert_outcome,
    get_pending_missed_setups,
    update_missed_setup_outcome,
)
from market_data import get_candles, get_quote, timeframe_to_interval, normalize_symbol


# How many minutes after alert was sent to attempt resolution
RESOLUTION_WINDOWS = {
    "M5":  30,
    "M15": 60,
    "M30": 120,
    "H1":  240,
    "H4":  720,
    "D1":  1440,
}
DEFAULT_RESOLUTION_MINUTES = 90

# Maximum age before marking as expired (even if unresolved)
EXPIRY_HOURS = 48

# ── Health tracking ────────────────────────────────────────────────────────────
# Module-level timestamp so /health and /healthreport can report last run time.
_LAST_OUTCOME_CHECK_TS: float = 0.0
_LAST_OUTCOME_CHECK_SAST: str = "—"


def get_last_outcome_check_sast() -> str:
    """Returns a human-readable SAST timestamp of the last outcome check."""
    return _LAST_OUTCOME_CHECK_SAST


def _minutes_since(iso_ts: str) -> float:
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).total_seconds() / 60.0
    except Exception:
        return 0.0


def _resolve_with_candles(alert: dict) -> tuple:
    """
    Try to determine outcome by scanning candle high/low data since the alert.
    Returns ('win'|'loss'|None, price).
    A None outcome means the trade is still open.
    """
    sym       = normalize_symbol(alert["pair"])
    tf        = alert.get("timeframe", "M15")
    direction = alert["direction"]
    sl        = alert["stop_loss"]
    tp        = alert["take_profit"]
    ts        = alert.get("timestamp", "")

    interval = timeframe_to_interval(tf)
    candles  = get_candles(sym, interval, outputsize=30)
    if not candles:
        return None, None

    # Only look at candles after the alert timestamp
    try:
        alert_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        alert_dt = None

    relevant = []
    for c in candles:
        if alert_dt:
            try:
                raw_dt = c.get("datetime", "")
                if "T" not in raw_dt:
                    raw_dt = raw_dt.replace(" ", "T")
                c_dt = datetime.fromisoformat(raw_dt + "+00:00")
                if c_dt < alert_dt:
                    continue
            except Exception:
                pass
        relevant.append(c)

    if not relevant:
        # Fall back to current quote
        quote = get_quote(sym)
        if not quote:
            return None, None
        price = quote.get("price")
        if not price:
            return None, None
        if direction == "BUY":
            if price >= tp:
                return "win", price
            if price <= sl:
                return "loss", price
        else:
            if price <= tp:
                return "win", price
            if price >= sl:
                return "loss", price
        return None, price

    # Walk through candles chronologically
    for c in relevant:
        high  = c.get("high", 0)
        low   = c.get("low",  0)
        open_ = c.get("open", 0)
        close = c.get("close", 0)

        if direction == "BUY":
            sl_hit = low  <= sl
            tp_hit = high >= tp
            if sl_hit and tp_hit:
                # Both wicked in same candle — use candle body direction.
                # Bullish candle (close > open) → price more likely ran to TP first.
                # Bearish candle              → price more likely ran to SL first.
                if close > open_:
                    return "win", tp
                else:
                    return "loss", sl
            if sl_hit:
                return "loss", sl
            if tp_hit:
                return "win", tp
        else:
            sl_hit = high >= sl
            tp_hit = low  <= tp
            if sl_hit and tp_hit:
                # Bearish candle → TP more likely hit first; bullish → SL first.
                if close < open_:
                    return "win", tp
                else:
                    return "loss", sl
            if sl_hit:
                return "loss", sl
            if tp_hit:
                return "win", tp

    # Trade still open
    quote   = get_quote(sym)
    current = quote.get("price") if quote else None
    return None, current


def check_pending_outcomes(notify_callback=None):
    """
    Checks all pending scanner alerts and resolves those that are ready.
    notify_callback(chat_id, text) is called for admin notifications.
    Returns number of resolved alerts.
    """
    global _LAST_OUTCOME_CHECK_TS, _LAST_OUTCOME_CHECK_SAST

    pending  = get_pending_scanner_alerts()
    resolved = 0
    SAST     = timezone(timedelta(hours=2))

    for alert in pending:
        try:
            tf          = alert.get("timeframe", "M15")
            window_min  = RESOLUTION_WINDOWS.get(tf, DEFAULT_RESOLUTION_MINUTES)
            elapsed_min = _minutes_since(alert.get("timestamp", ""))
            alert_id    = alert["alert_id"]

            # Not ready yet
            if elapsed_min < window_min:
                continue

            # Too old — expire without resolution
            if elapsed_min > EXPIRY_HOURS * 60:
                update_alert_outcome(alert_id, "expired", notes="Expired without resolution.")
                resolved += 1
                print(f"  [OutcomeChecker] {alert['pair']} {alert_id[:6]} "
                      f"→ EXPIRED (too old)", flush=True)
                continue

            # Try to resolve via candles
            outcome, price = _resolve_with_candles(alert)

            if outcome in ("win", "loss"):
                update_alert_outcome(alert_id, outcome, outcome_price=price)
                resolved += 1
                emoji = "✅" if outcome == "win" else "❌"
                print(f"  [OutcomeChecker] {alert['pair']} {alert_id[:6]} "
                      f"→ {emoji} {outcome.upper()} @ {price}", flush=True)

                # ── Circuit breaker update ─────────────────────────────────────
                try:
                    import circuit_breaker
                    circuit_breaker.update_from_outcome(outcome)
                except Exception as _cbe:
                    print(f"  [OutcomeChecker] Circuit breaker update error: {_cbe}", flush=True)

                # ── Adaptive brain update ──────────────────────────────────────
                try:
                    import adaptive_brain
                    if outcome == "loss":
                        adaptive_brain.on_loss(alert, notify_callback=notify_callback)
                    elif outcome == "win":
                        adaptive_brain.on_win(alert)
                except Exception as _abe:
                    print(f"  [OutcomeChecker] Brain update error: {_abe}", flush=True)

                if notify_callback:
                    conf = alert.get("confidence", 0)
                    rr   = alert.get("rr", 0)

                    # ── Admin detailed resolution message ──────────────────────
                    admin_msg = (
                        f"{emoji} ALERT RESOLVED\n\n"
                        f"{alert['pair']} {alert['direction']} {tf}\n"
                        f"Entry: {alert['entry']} | SL: {alert['stop_loss']} "
                        f"| TP: {alert['take_profit']}\n"
                        f"Confidence: {conf}% | RR: 1:{rr}\n"
                        f"Outcome: {outcome.upper()} @ {price}\n"
                        f"Regime: {alert.get('market_regime','')} "
                        f"| Session: {alert.get('session','')}"
                    )
                    for admin_id in ADMIN_IDS:
                        try:
                            notify_callback(admin_id, admin_msg)
                        except Exception:
                            pass

                    # ── Follow-up to original alert recipients ─────────────────
                    if OUTCOME_FOLLOWUP_ENABLED:
                        from plans import user_has_feature
                        r_delta      = f"+{rr}R" if outcome == "win" else "-1R"
                        followup_msg = (
                            f"{emoji} TRADE UPDATE — {alert['pair']} {alert['direction']}\n\n"
                            f"{'TP Hit' if outcome == 'win' else 'SL Hit'} @ {price}\n"
                            f"Result: {r_delta} | Conf was {conf}%\n\n"
                            f"Use /stats to see your overall record."
                        )
                        stored_recipients = alert.get("recipients", [])
                        notified          = set(ADMIN_IDS)   # admins got detailed msg already
                        for uid in stored_recipients:
                            if uid in notified:
                                continue
                            # Gate: user's plan must allow outcome followups
                            if not user_has_feature(uid, "outcome_followups"):
                                continue
                            try:
                                notify_callback(uid, followup_msg)
                                notified.add(uid)
                            except Exception:
                                pass

            elif outcome is None and price is None:
                pass   # No data — leave pending
            else:
                pass   # Still open — leave pending until next check

        except Exception as e:
            print(f"  [OutcomeChecker] Error checking {alert.get('alert_id')}: {e}",
                  flush=True)

    # Update last-check health tracking
    _LAST_OUTCOME_CHECK_TS   = time.time()
    _LAST_OUTCOME_CHECK_SAST = datetime.now(SAST).strftime("%d %b %Y %H:%M SAST")

    return resolved


def check_missed_setups() -> int:
    """
    Resolve virtual trades stored as 'missed setups' (rejected near-misses).
    Uses the same candle-walk logic as real alerts. Returns # resolved.
    """
    pending = get_pending_missed_setups()
    resolved = 0
    for ms in pending:
        try:
            tf          = ms.get("timeframe", "M15")
            window_min  = RESOLUTION_WINDOWS.get(tf, DEFAULT_RESOLUTION_MINUTES)
            elapsed_min = _minutes_since(ms.get("timestamp", ""))
            msid        = ms["id"]

            if elapsed_min < window_min:
                continue
            if elapsed_min > EXPIRY_HOURS * 60:
                update_missed_setup_outcome(msid, "expired")
                resolved += 1
                continue

            # Reuse alert resolver (same shape: pair/timeframe/direction/sl/tp/timestamp)
            outcome, _price = _resolve_with_candles({
                "pair":        ms["pair"],
                "timeframe":   tf,
                "direction":   ms["direction"],
                "stop_loss":   ms["stop_loss"],
                "take_profit": ms["take_profit"],
                "timestamp":   ms.get("timestamp", ""),
            })
            if outcome in ("win", "loss"):
                update_missed_setup_outcome(msid, outcome)
                resolved += 1
        except Exception as e:
            print(f"  [OutcomeChecker] missed_setup error: {e}", flush=True)
    return resolved


def run_outcome_checker_loop(notify_callback=None, interval_seconds: int = 1800):
    """
    Continuous loop that checks outcomes on a schedule.
    Designed to run in a background thread.
    interval_seconds: how often to run (default 30 min).
    """
    while True:
        try:
            time.sleep(interval_seconds)
            print("[OutcomeChecker] Running outcome check...", flush=True)
            count = check_pending_outcomes(notify_callback)
            if count:
                print(f"[OutcomeChecker] Resolved {count} alert(s).", flush=True)
            missed = check_missed_setups()
            if missed:
                print(f"[OutcomeChecker] Resolved {missed} missed-setup(s).", flush=True)
        except Exception as e:
            print(f"[OutcomeChecker] Loop error: {e}", flush=True)
