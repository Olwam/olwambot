"""
Session detection and scoring.
Determines which trading session is active and how suitable it is
for a given symbol, influencing both confidence scoring and scanner filtering.
"""

from datetime import datetime, timezone


# UTC hour windows for each session (start inclusive, end exclusive)
SESSION_WINDOWS = {
    "asian":    (0, 8),
    "london":   (7, 16),
    "new_york": (12, 21),
    "overlap":  (12, 16),   # London + New York overlap — strongest liquidity
}

# Dead hours: after NY close, before Asian peak liquidity
DEAD_HOUR_START = 21
DEAD_HOUR_END   = 1   # wrap-around midnight

# Per-symbol ideal sessions for trading
SYMBOL_SESSION_RULES = {
    "EURUSD":  ["overlap", "london", "new_york"],
    "GBPUSD":  ["overlap", "london", "new_york"],
    "USDJPY":  ["asian", "overlap", "new_york"],
    "AUDUSD":  ["asian", "london"],
    "USDCAD":  ["overlap", "new_york"],
    "NZDUSD":  ["asian", "london"],
    "USDCHF":  ["london", "overlap"],
    "GBPJPY":  ["london", "overlap"],
    "EURJPY":  ["asian", "london", "overlap"],
    "EURGBP":  ["london"],
    "XAUUSD":  ["new_york", "overlap", "london"],
    "XAGUSD":  ["new_york", "overlap"],
    "NAS100":  ["new_york", "overlap"],
    "US30":    ["new_york", "overlap"],
}

DEFAULT_GOOD_SESSIONS = ["london", "new_york", "overlap"]


def get_current_sessions(now_utc: datetime = None) -> list:
    """Returns list of currently active session names."""
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    hour = now_utc.hour + now_utc.minute / 60.0
    active = []
    for name, (start, end) in SESSION_WINDOWS.items():
        if start <= hour < end:
            active.append(name)
    return active


def is_dead_hours(now_utc: datetime = None) -> bool:
    """True during low-liquidity dead hours (21:00 – 01:00 UTC)."""
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    hour = now_utc.hour + now_utc.minute / 60.0
    return hour >= DEAD_HOUR_START or hour < DEAD_HOUR_END


def get_session_score(symbol: str, now_utc: datetime = None) -> int:
    """
    Returns a 0–15 score reflecting how suitable the current session is
    for trading the given symbol.  Higher = better conditions.
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)

    if is_dead_hours(now_utc):
        return 0

    active   = get_current_sessions(now_utc)
    sym      = symbol.upper().replace("/", "").replace("-", "")
    ideal    = SYMBOL_SESSION_RULES.get(sym, DEFAULT_GOOD_SESSIONS)

    if not active:
        return 0

    # Overlap is highest priority
    if "overlap" in active and "overlap" in ideal:
        return 15
    if "london" in active and "london" in ideal:
        return 10
    if "new_york" in active and "new_york" in ideal:
        return 10
    if "asian" in active and "asian" in ideal:
        return 7
    # A session is open but it's not ideal for this symbol
    if active:
        return 3
    return 0


def get_session_label(now_utc: datetime = None) -> str:
    """Human-readable label for the current session."""
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    active = get_current_sessions(now_utc)
    if not active:
        return "Dead Hours"
    if "overlap" in active:
        return "London-NY Overlap"
    if "london" in active and "new_york" in active:
        return "London-NY Overlap"
    if "london" in active:
        return "London Session"
    if "new_york" in active:
        return "New York Session"
    if "asian" in active:
        return "Asian Session"
    return "Off-hours"


def session_confidence_bonus(symbol: str, now_utc: datetime = None) -> int:
    """
    Returns a confidence adjustment (-5 to +10) based on session quality.
    Used by the decision engine to adjust the final score.
    """
    score = get_session_score(symbol, now_utc)
    if score >= 15:
        return 10   # overlap — prime time
    if score >= 10:
        return 6    # good session
    if score >= 7:
        return 3    # acceptable session
    if score >= 3:
        return 0    # session open but not ideal
    return -5       # dead hours penalty
