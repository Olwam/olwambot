import os

# Telegram — uses the dedicated forex bot token
TELEGRAM_BOT_TOKEN = os.environ["FOREX_BOT_TOKEN"]

# OpenAI
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")

# Admin IDs (comma-separated Telegram numeric user IDs)
ADMIN_IDS = [
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
]

# Market data
TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY", "")
FINNHUB_API_KEY    = os.getenv("FINNHUB_API_KEY", "")

# Server / runtime
PORT                  = int(os.getenv("PORT", "5000"))
DATA_FILE             = os.getenv("DATA_FILE", "bot_data.json")
USER_COOLDOWN_SECONDS = int(os.getenv("USER_COOLDOWN_SECONDS", "20"))
MAX_SIGNALS_HISTORY   = int(os.getenv("MAX_SIGNALS_HISTORY", "500"))
REQUEST_TIMEOUT       = int(os.getenv("REQUEST_TIMEOUT", "20"))

# ── Auto-scanner / alert engine ───────────────────────────────────────────────
SCAN_INTERVAL_SECONDS       = int(os.getenv("SCAN_INTERVAL_SECONDS", "900"))
ALERT_MIN_CONFIDENCE        = int(os.getenv("ALERT_MIN_CONFIDENCE", "75"))
ALERT_PAIR_COOLDOWN_MINUTES = int(os.getenv("ALERT_PAIR_COOLDOWN_MINUTES", "60"))

# Watch (pre-alert) system
WATCH_ALERT_MIN_CONFIDENCE   = int(os.getenv("WATCH_ALERT_MIN_CONFIDENCE", "60"))
WATCH_ALERT_COOLDOWN_MINUTES = int(os.getenv("WATCH_ALERT_COOLDOWN_MINUTES", "90"))

# Analytics / rejection logging
REJECTION_LOG_LIMIT    = int(os.getenv("REJECTION_LOG_LIMIT", "1000"))
ANALYTICS_SAMPLE_LIMIT = int(os.getenv("ANALYTICS_SAMPLE_LIMIT", "2000"))

# ── Market-open schedule rules ────────────────────────────────────────────────
# Controls hard market-status gate in market_status.py. All hours are UTC.
MARKET_OPEN_RULES: dict = {
    "forex": {
        "sunday_open_utc":  21,    # forex re-opens ~21:00 UTC Sunday (Sydney open)
        "friday_close_utc": 21,    # forex closes ~21:00 UTC Friday
    },
    "indices": {
        "open_utc":  13,           # approx US open (08:00 ET = 13:00 UTC)
        "close_utc": 21,           # approx US close (16:00 ET = 21:00 UTC)
    },
}

# ── Stale-candle / stale-quote thresholds ─────────────────────────────────────
STALE_CANDLE_THRESHOLDS: dict = {
    "M1":    10,
    "M5":    15,
    "M15":   40,
    "M30":   75,
    "H1":    120,
    "H4":    360,
    "D1":    1440,
    "QUOTE": 30,
}

# ── Scanner timeframes (each pair is scanned on all listed timeframes) ─────────
SCAN_TIMEFRAMES: list = os.getenv("SCAN_TIMEFRAMES", "M15,H1,H4").split(",")

# ── Correlated pair groups ─────────────────────────────────────────────────────
CORRELATED_PAIR_GROUPS: list = [
    ["EURUSD", "GBPUSD", "AUDUSD", "NZDUSD", "EURGBP", "EURAUD", "GBPAUD"],
    ["USDJPY", "USDCHF", "USDCAD", "EURJPY", "GBPJPY", "AUDJPY"],
    ["XAUUSD", "XAGUSD"],
]
CORRELATED_ALERT_WINDOW_MINUTES: int = int(
    os.getenv("CORRELATED_ALERT_WINDOW_MINUTES", "60")
)

REPLAY_PREVENTION_MINUTES: int = ALERT_PAIR_COOLDOWN_MINUTES

# ── Session-adaptive confidence thresholds ────────────────────────────────────
SESSION_THRESHOLD_OFFSETS: dict = {
    "overlap": -2,
    "london":   0,
    "new_york": 0,
    "asian":   +6,
    "dead":    +8,
    "default": +2,
}

# ── Dynamic scan intervals by session (seconds) ───────────────────────────────
SESSION_SCAN_INTERVALS: dict = {
    "overlap":  int(os.getenv("SCAN_INTERVAL_OVERLAP",  "600")),
    "london":   int(os.getenv("SCAN_INTERVAL_LONDON",   "720")),
    "new_york": int(os.getenv("SCAN_INTERVAL_NEWYORK",  "720")),
    "asian":    int(os.getenv("SCAN_INTERVAL_ASIAN",   "1200")),
    "dead":     int(os.getenv("SCAN_INTERVAL_DEAD",    "1800")),
    "default":  SCAN_INTERVAL_SECONDS,
}

# ── Health / daily report ─────────────────────────────────────────────────────
# 05:30 UTC = 07:30 SAST
DAILY_HEALTH_REPORT_UTC_HOUR:   int = int(os.getenv("DAILY_REPORT_UTC_HOUR",   "5"))
DAILY_HEALTH_REPORT_UTC_MINUTE: int = int(os.getenv("DAILY_REPORT_UTC_MINUTE", "30"))

# Drift detection: warn if short-run win rate is this many pp below all-time
HEALTH_DRIFT_THRESHOLD_PP: int = int(os.getenv("HEALTH_DRIFT_THRESHOLD_PP", "15"))

# ── Outcome follow-up messaging ────────────────────────────────────────────────
OUTCOME_FOLLOWUP_ENABLED: bool = (
    os.getenv("OUTCOME_FOLLOWUP", "true").lower() == "true"
)

# ── Score tuning overrides ────────────────────────────────────────────────────
# Set any key here to override the corresponding default weight in the engine.
# Leave empty ({}) to use all defaults.
#
# Available override keys:
#   htf_aligned, htf_conflict
#   pullback, trending, reversal, range, mixed
#   ema_slope_aligned, ema_slope_misaligned
#   momentum_strong, momentum_pullback
#   rr_3plus, rr_25plus, rr_2plus
#   session_cap
#   news_medium, news_high
#   bias_aligned, bias_conflict
#   chart_quality_clean, chart_quality_dirty
#
TUNING_OVERRIDES: dict = {}

# ── Phase 2: Structure signal bonuses ────────────────────────────────────────
# These are intentionally conservative so they supplement but never dominate
# the core scoring logic.

# Liquidity sweep (swing taken and reclaimed in the trade direction)
LIQUIDITY_SWEEP_BONUS: int  = int(os.getenv("LIQUIDITY_SWEEP_BONUS",  "5"))

# Fair Value Gap / imbalance (price currently inside an aligned gap)
FVG_BONUS: int               = int(os.getenv("FVG_BONUS",             "4"))

# Order Block (price entering a confirmed OB zone in the trade direction)
ORDER_BLOCK_BONUS: int       = int(os.getenv("ORDER_BLOCK_BONUS",     "4"))

# Breaker context (broken structure level now acting as support/resistance)
BREAKER_CONTEXT_BONUS: int   = int(os.getenv("BREAKER_CONTEXT_BONUS", "3"))

# Fibonacci confluence (price in 0.50–0.618 retracement zone with structure)
FIB_CONFLUENCE_BONUS: int    = int(os.getenv("FIB_CONFLUENCE_BONUS",  "3"))

# Volatility scoring
# Applied when market is frozen (ratio < 0.25× expected ATR)
VOLATILITY_DEAD_PENALTY: int  = int(os.getenv("VOLATILITY_DEAD_PENALTY",  "-8"))
# Applied when market is spiking (ratio > 4.0× expected ATR)
VOLATILITY_SPIKE_PENALTY: int = int(os.getenv("VOLATILITY_SPIKE_PENALTY", "-6"))

# Hard gate: if volatility label is "dead" or "chaotic" the scanner aborts
# regardless of score.  Set to 0 to keep as penalty-only (no hard abort).
VOLATILITY_HARD_ABORT: bool   = (
    os.getenv("VOLATILITY_HARD_ABORT", "true").lower() == "true"
)

# ── Phase 2: Circuit breaker settings ─────────────────────────────────────────
# Number of consecutive scanner losses that arm the breaker
CIRCUIT_BREAKER_LOSS_STREAK: int       = int(
    os.getenv("CIRCUIT_BREAKER_LOSS_STREAK", "4")
)
# Points added to ALERT_MIN_CONFIDENCE when breaker is active
CIRCUIT_BREAKER_CONFIDENCE_BUMP: int   = int(
    os.getenv("CIRCUIT_BREAKER_CONFIDENCE_BUMP", "5")
)
# Hours after which the breaker auto-resets (regardless of outcome)
CIRCUIT_BREAKER_RESET_HOURS: float     = float(
    os.getenv("CIRCUIT_BREAKER_RESET_HOURS", "24")
)

# ── Plan limits ───────────────────────────────────────────────────────────────
# ── Loss-streak per-pair protection ─────────────────────────────────────────
LOSS_STREAK_BLOCK_2_MINUTES: int = int(os.getenv("LOSS_STREAK_BLOCK_2_MINUTES", "60"))
LOSS_STREAK_BLOCK_3_MINUTES: int = int(os.getenv("LOSS_STREAK_BLOCK_3_MINUTES", "120"))

# ── Global drawdown protection ─────────────────────────────────────────────────
GLOBAL_DRAWDOWN_LOSS_COUNT:     int = int(os.getenv("GLOBAL_DRAWDOWN_LOSS_COUNT",     "4"))
GLOBAL_DRAWDOWN_WINDOW_MINUTES: int = int(os.getenv("GLOBAL_DRAWDOWN_WINDOW_MINUTES", "60"))
GLOBAL_DRAWDOWN_PAUSE_MINUTES:  int = int(os.getenv("GLOBAL_DRAWDOWN_PAUSE_MINUTES",  "45"))

# ── ATR-based SL & entry quality ───────────────────────────────────────────────
ATR_SL_MULTIPLIER:               float = float(os.getenv("ATR_SL_MULTIPLIER",               "2.0"))
ENTRY_CANDLE_BODY_ATR_MAX_RATIO: float = float(os.getenv("ENTRY_CANDLE_BODY_ATR_MAX_RATIO", "0.70"))
EMA_DISTANCE_ATR_MAX:            float = float(os.getenv("EMA_DISTANCE_ATR_MAX",            "2.5"))
LOW_VOLATILITY_ATR_RATIO:        float = float(os.getenv("LOW_VOLATILITY_ATR_RATIO",        "0.40"))

# ── Direction flip delay ────────────────────────────────────────────────────────
DIRECTION_FLIP_DELAY_MINUTES: int = int(os.getenv("DIRECTION_FLIP_DELAY_MINUTES", "12"))

PLAN_LIMITS = {
    "trial":   5,
    "weekly":  20,
    "monthly": 60,
    "vip":     9999,
}

PLAN_DURATIONS = {
    "trial":   3,
    "weekly":  7,
    "monthly": 30,
    "vip":     365,
}

# ── cTrader developer credentials (set once by admin in Railway env vars) ─────
CTRADER_CLIENT_ID:     str = os.getenv("CTRADER_CLIENT_ID",     "")
CTRADER_CLIENT_SECRET: str = os.getenv("CTRADER_CLIENT_SECRET", "")

# ── Copy trading defaults ──────────────────────────────────────────────────────
COPY_DEFAULT_RISK_PCT:        float = float(os.getenv("COPY_DEFAULT_RISK_PCT",        "1.0"))
COPY_MAX_TRADES_DEFAULT:      int   = int(  os.getenv("COPY_MAX_TRADES_DEFAULT",      "3"))
COPY_DAILY_RISK_LIMIT_PCT:    float = float(os.getenv("COPY_DAILY_RISK_LIMIT_PCT",    "5.0"))
COPY_MAX_DRAWDOWN_PCT:        float = float(os.getenv("COPY_MAX_DRAWDOWN_PCT",        "10.0"))
COPY_MONITOR_INTERVAL_SECS:   int   = int(  os.getenv("COPY_MONITOR_INTERVAL_SECS",   "30"))
