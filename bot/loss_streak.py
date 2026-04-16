"""
loss_streak.py — Per-pair loss streak protection and global drawdown guard.

Per-pair protection:
  - 2 consecutive losses on same pair → block that pair for LOSS_STREAK_BLOCK_2_MINUTES
  - 3 consecutive losses on same pair → block that pair for LOSS_STREAK_BLOCK_3_MINUTES

Global drawdown protection:
  - GLOBAL_DRAWDOWN_LOSS_COUNT losses across all pairs within GLOBAL_DRAWDOWN_WINDOW_MINUTES
    → pause ALL scanning for GLOBAL_DRAWDOWN_PAUSE_MINUTES

State is held in memory and optionally restored from storage on startup.
"""

import time
from datetime import datetime, timezone

from config import (
    LOSS_STREAK_BLOCK_2_MINUTES,
    LOSS_STREAK_BLOCK_3_MINUTES,
    GLOBAL_DRAWDOWN_LOSS_COUNT,
    GLOBAL_DRAWDOWN_WINDOW_MINUTES,
    GLOBAL_DRAWDOWN_PAUSE_MINUTES,
)

# Per-pair state: {PAIR: {"streak": int, "blocked_until": float}}
_pair_state: dict = {}

# Global drawdown: sorted list of recent loss epoch timestamps
_global_loss_times: list = []
_global_pause_until: float = 0.0


# ── Public API ─────────────────────────────────────────────────────────────────

def record_outcome(pair: str, outcome: str):
    """
    Call whenever a scanner alert is resolved.
    outcome: 'win' | 'loss' | 'expired'
    'expired' does not count toward streak.
    """
    global _global_pause_until

    if outcome not in ("win", "loss"):
        return

    pair = pair.upper()
    state = _pair_state.setdefault(pair, {"streak": 0, "blocked_until": 0.0})

    if outcome == "win":
        state["streak"] = 0
        return

    # outcome == "loss"
    state["streak"] += 1
    streak = state["streak"]
    now    = time.time()

    if streak >= 3:
        state["blocked_until"] = now + LOSS_STREAK_BLOCK_3_MINUTES * 60
        print(f"[LossStreak] {pair}: {streak} consecutive losses — "
              f"blocked for {LOSS_STREAK_BLOCK_3_MINUTES}min", flush=True)
    elif streak >= 2:
        state["blocked_until"] = now + LOSS_STREAK_BLOCK_2_MINUTES * 60
        print(f"[LossStreak] {pair}: {streak} consecutive losses — "
              f"blocked for {LOSS_STREAK_BLOCK_2_MINUTES}min", flush=True)

    # Global drawdown tracking
    window = GLOBAL_DRAWDOWN_WINDOW_MINUTES * 60
    _global_loss_times.append(now)
    cutoff = now - window
    while _global_loss_times and _global_loss_times[0] < cutoff:
        _global_loss_times.pop(0)

    if len(_global_loss_times) >= GLOBAL_DRAWDOWN_LOSS_COUNT:
        _global_pause_until = now + GLOBAL_DRAWDOWN_PAUSE_MINUTES * 60
        print(f"[GlobalDrawdown] {len(_global_loss_times)} losses in "
              f"{GLOBAL_DRAWDOWN_WINDOW_MINUTES}min — scanner paused "
              f"for {GLOBAL_DRAWDOWN_PAUSE_MINUTES}min", flush=True)


def is_pair_blocked(pair: str) -> tuple:
    """Returns (blocked: bool, reason: str)."""
    pair  = pair.upper()
    state = _pair_state.get(pair)
    if not state:
        return False, ""
    blocked_until = state.get("blocked_until", 0.0)
    if time.time() < blocked_until:
        streak      = state.get("streak", 0)
        mins_left   = max(1, int((blocked_until - time.time()) / 60))
        return True, f"loss_streak_block ({streak} consecutive losses, {mins_left}min remaining)"
    return False, ""


def is_globally_paused() -> tuple:
    """Returns (paused: bool, reason: str)."""
    if time.time() < _global_pause_until:
        mins_left = max(1, int((_global_pause_until - time.time()) / 60))
        return True, f"global_drawdown_protection ({mins_left}min remaining)"
    return False, ""


def get_status() -> dict:
    now = time.time()
    blocked_pairs = {}
    for pair, state in _pair_state.items():
        if now < state.get("blocked_until", 0):
            blocked_pairs[pair] = {
                "streak":     state["streak"],
                "mins_left":  max(1, int((state["blocked_until"] - now) / 60)),
            }
    return {
        "globally_paused":           now < _global_pause_until,
        "global_pause_mins_left":    max(0, int((_global_pause_until - now) / 60)) if now < _global_pause_until else 0,
        "recent_losses_in_window":   len(_global_loss_times),
        "blocked_pairs":             blocked_pairs,
    }


def refresh_from_storage():
    """
    Recomputes streak and global drawdown from stored scanner alerts on startup.
    Conservative: only restores streak count; does not re-apply block times
    (old blocks would have expired anyway).
    """
    global _global_pause_until

    try:
        from storage import load_data
        data   = load_data()
        alerts = data.get("scanner_alerts", [])

        # Per-pair streak: walk backwards, count consecutive losses until a win
        pair_streaks: dict = {}
        for a in reversed(alerts):
            oc   = a.get("outcome")
            pair = a.get("pair", "").upper()
            if not pair or oc not in ("win", "loss"):
                continue
            if pair in pair_streaks and pair_streaks[pair] < 0:
                continue   # already found a win for this pair — stop counting
            if oc == "loss":
                pair_streaks[pair] = pair_streaks.get(pair, 0) + 1
            elif oc == "win":
                pair_streaks[pair] = -1   # sentinel: stop at first win

        for pair, streak in pair_streaks.items():
            if streak > 0:
                _pair_state[pair] = {"streak": streak, "blocked_until": 0.0}

        # Global loss window: look at outcome timestamps from recent alerts
        now    = time.time()
        window = GLOBAL_DRAWDOWN_WINDOW_MINUTES * 60
        for a in alerts:
            if a.get("outcome") != "loss":
                continue
            ts_str = a.get("outcome_time") or a.get("timestamp", "")
            if not ts_str:
                continue
            try:
                ts_epoch = datetime.fromisoformat(
                    ts_str.replace("Z", "+00:00")
                ).timestamp()
                if now - ts_epoch <= window:
                    _global_loss_times.append(ts_epoch)
            except Exception:
                pass

        _global_loss_times.sort()

        if len(_global_loss_times) >= GLOBAL_DRAWDOWN_LOSS_COUNT:
            _global_pause_until = now + GLOBAL_DRAWDOWN_PAUSE_MINUTES * 60
            print(f"[GlobalDrawdown] Restored from storage — "
                  f"scanner paused for {GLOBAL_DRAWDOWN_PAUSE_MINUTES}min", flush=True)

        if pair_streaks:
            print(f"[LossStreak] State restored — "
                  f"{sum(1 for s in pair_streaks.values() if s > 0)} pair(s) with active streaks",
                  flush=True)

    except Exception as e:
        print(f"[LossStreak] refresh_from_storage error: {e}", flush=True)
