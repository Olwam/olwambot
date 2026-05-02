"""
circuit_breaker.py — Drawdown protection layer.

Monitors recent scanner alert outcomes and temporarily raises confidence
thresholds when a losing streak is detected. This is a soft, transparent
protective layer — not hidden self-modification.

Behaviour:
  - Checks resolved scanner alerts for consecutive losses
  - If consecutive losses >= CIRCUIT_BREAKER_LOSS_STREAK:
      → Active: confidence bar is raised by CIRCUIT_BREAKER_CONFIDENCE_BUMP
      → Scanner is NOT stopped, just made stricter
  - Resets after a win is observed, or after CIRCUIT_BREAKER_RESET_HOURS hours
  - Status is fully visible in /health and /healthreport

State is held in memory (resets on bot restart) but also persisted
to storage so it survives restarts.

Design rules:
  - Transparent: status always visible in /health
  - Conservative: only raises bar, never lowers it below normal
  - Not autonomous: does not auto-apply tuning changes
"""

import time
from datetime import datetime, timezone, timedelta

from config import (
    CIRCUIT_BREAKER_LOSS_STREAK,
    CIRCUIT_BREAKER_CONFIDENCE_BUMP,
    CIRCUIT_BREAKER_RESET_HOURS,
)

# ── Module-level state ─────────────────────────────────────────────────────────
_active:             bool  = False
_trigger_ts:         float = 0.0    # when the breaker was last triggered
_consecutive_losses: int   = 0      # current consecutive loss count
_trigger_reason:     str   = ""     # human-readable explanation for /health


def is_active() -> bool:
    """Returns True if the circuit breaker is currently engaged."""
    _check_expiry()
    return _active


def get_confidence_bump() -> int:
    """Returns the confidence adjustment to add when breaker is active (positive int)."""
    return CIRCUIT_BREAKER_CONFIDENCE_BUMP if is_active() else 0


def get_status() -> dict:
    """Returns a status dict for /health and /healthreport display."""
    _check_expiry()
    reset_at = "—"
    if _active and _trigger_ts:
        reset_utc = datetime.fromtimestamp(_trigger_ts, tz=timezone.utc) + \
                    timedelta(hours=CIRCUIT_BREAKER_RESET_HOURS)
        reset_sast = reset_utc.astimezone(timezone(timedelta(hours=2)))
        reset_at = reset_sast.strftime("%d %b %H:%M SAST")
    return {
        "active":               _active,
        "consecutive_losses":   _consecutive_losses,
        "streak_threshold":     CIRCUIT_BREAKER_LOSS_STREAK,
        "confidence_bump":      CIRCUIT_BREAKER_CONFIDENCE_BUMP if _active else 0,
        "trigger_reason":       _trigger_reason,
        "auto_reset_at":        reset_at,
    }


def update_from_outcome(outcome: str):
    """
    Call this whenever a scanner alert is resolved.
    outcome: 'win' | 'loss' | 'expired'
    Expired does not count as a win or loss for the breaker.
    """
    global _active, _trigger_ts, _consecutive_losses, _trigger_reason

    if outcome == "win":
        # A win resets the streak
        _consecutive_losses = 0
        if _active:
            _active         = False
            _trigger_reason = ""
            print("[CircuitBreaker] Reset — win observed.", flush=True)

    elif outcome == "loss":
        _consecutive_losses += 1
        if _consecutive_losses >= CIRCUIT_BREAKER_LOSS_STREAK and not _active:
            _active         = True
            _trigger_ts     = time.time()
            _trigger_reason = (
                f"{_consecutive_losses} consecutive losses — "
                f"confidence threshold raised by +{CIRCUIT_BREAKER_CONFIDENCE_BUMP}pp. "
                f"Auto-resets in {CIRCUIT_BREAKER_RESET_HOURS}h or on next win."
            )
            print(f"[CircuitBreaker] TRIGGERED — {_trigger_reason}", flush=True)


def refresh_from_storage():
    """
    Recomputes circuit breaker state from the last N resolved scanner alerts.
    Call once on bot startup so state survives restarts.
    """
    global _active, _trigger_ts, _consecutive_losses, _trigger_reason

    try:
        from storage import load_data
        data   = load_data()
        alerts = data.get("scanner_alerts", [])

        # Walk backwards through resolved alerts, counting streak
        consecutive = 0
        for a in reversed(alerts):
            oc = a.get("outcome")
            if oc == "pending" or oc == "expired":
                continue
            if oc == "loss":
                consecutive += 1
            elif oc == "win":
                break

        _consecutive_losses = consecutive
        if consecutive >= CIRCUIT_BREAKER_LOSS_STREAK:
            if not _active:
                _active         = True
                _trigger_ts     = time.time()
                _trigger_reason = (
                    f"Recovered from storage: {consecutive} consecutive losses "
                    f"— threshold raised by +{CIRCUIT_BREAKER_CONFIDENCE_BUMP}pp."
                )
                print(f"[CircuitBreaker] Restored from storage — {_trigger_reason}", flush=True)
        else:
            _active = False

    except Exception as e:
        print(f"[CircuitBreaker] refresh_from_storage error: {e}", flush=True)


def _check_expiry():
    """Auto-reset if the breaker has been active longer than CIRCUIT_BREAKER_RESET_HOURS."""
    global _active, _trigger_reason
    if _active and _trigger_ts:
        elapsed_hours = (time.time() - _trigger_ts) / 3600.0
        if elapsed_hours >= CIRCUIT_BREAKER_RESET_HOURS:
            _active         = False
            _trigger_reason = ""
            print("[CircuitBreaker] Auto-reset after time limit.", flush=True)
