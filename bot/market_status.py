"""
Market status, stale-data, and symbol-classification helpers.

Provides hard gates that must pass before the scanner scores any setup:
  - is_market_open(symbol, now_utc)
  - is_candle_stale(timeframe, candle_ts, now_utc)
  - is_quote_stale(symbol, quote_data, now_utc)
  - get_symbol_type(symbol)

All thresholds are sourced from config.py so they can be tuned without
touching this file.
"""

from datetime import datetime, timezone

from config import (
    STALE_CANDLE_THRESHOLDS,
    MARKET_OPEN_RULES,
)


# ── Symbol classification ─────────────────────────────────────────────────────

FOREX_PAIRS = {
    "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD",
    "NZDUSD", "USDCHF", "GBPJPY", "EURJPY", "EURGBP",
    "EURCAD", "GBPCAD", "AUDCAD", "AUDNZD", "AUDCHF",
    "CADJPY", "GBPCHF", "CHFJPY", "NZDCAD", "NZDCHF",
    "EURCHF", "EURAUD", "EURNZD",
}

METALS = {
    "XAUUSD", "XAGUSD", "XPTUSD", "XPDUSD",
    "GOLD", "SILVER",
}

INDICES = {
    "NAS100", "US30", "SPX500", "UK100", "GER40",
    "FRA40", "JPN225", "AUS200", "USTEC", "US500",
}

CRYPTO = {
    "BTCUSD", "ETHUSD", "LTCUSD", "XRPUSD",
}


def get_symbol_type(symbol: str) -> str:
    """
    Returns one of: 'forex', 'metals', 'indices', 'crypto', 'unknown'.
    """
    sym = symbol.upper().replace("/", "").replace("-", "").strip()
    if sym in FOREX_PAIRS:
        return "forex"
    if sym in METALS:
        return "metals"
    if sym in INDICES:
        return "indices"
    if sym in CRYPTO:
        return "crypto"
    # Heuristic: 6-char alphabetic pairs are likely forex
    if len(sym) == 6 and sym.isalpha():
        return "forex"
    return "unknown"


# ── Market-open check ─────────────────────────────────────────────────────────

def is_market_open(symbol: str, now_utc: datetime = None) -> tuple:
    """
    Returns (is_open: bool, reason: str).

    Rules per symbol type (sourced from MARKET_OPEN_RULES in config.py):
      forex   — closed Saturday all day and Sunday until ~21:00 UTC
      metals  — follows forex hours (gold/silver trade on forex session)
      indices — narrower hours; configurable
      crypto  — always open
      unknown — treated as forex (conservative default)

    Session labels such as "London" or "New York" are NOT used as proof
    that the market is open — only the weekday/hour schedule is checked.
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)

    sym_type = get_symbol_type(symbol)

    # Crypto: always open
    if sym_type == "crypto":
        return True, "crypto_always_open"

    weekday = now_utc.weekday()  # 0=Mon … 6=Sun
    hour    = now_utc.hour
    minute  = now_utc.minute
    hour_f  = hour + minute / 60.0

    if sym_type in ("forex", "metals", "unknown"):
        rules = MARKET_OPEN_RULES.get("forex", {})
        # Saturday — always closed for forex/metals
        if weekday == 5:
            return False, f"{symbol} market closed — Saturday (forex weekend schedule)"
        # Sunday — closed until NY Sunday open (~21:00 UTC)
        if weekday == 6:
            open_hour = rules.get("sunday_open_utc", 21)
            if hour_f < open_hour:
                return False, (
                    f"{symbol} market closed — Sunday before {open_hour:02.0f}:00 UTC "
                    f"(forex Sunday open)"
                )
        # Friday — closes ~21:00–22:00 UTC
        if weekday == 4:
            close_hour = rules.get("friday_close_utc", 21)
            if hour_f >= close_hour:
                return False, (
                    f"{symbol} market closed — Friday after {close_hour:02.0f}:00 UTC "
                    f"(weekly close)"
                )
        return True, "forex_market_open"

    if sym_type == "indices":
        rules    = MARKET_OPEN_RULES.get("indices", {})
        open_h   = rules.get("open_utc", 13)
        close_h  = rules.get("close_utc", 21)
        # Indices closed on weekends
        if weekday >= 5:
            return False, f"{symbol} market closed — weekend (indices schedule)"
        if not (open_h <= hour_f < close_h):
            return False, (
                f"{symbol} market closed — outside indices hours "
                f"({open_h:02.0f}:00–{close_h:02.0f}:00 UTC)"
            )
        return True, "indices_market_open"

    return True, "unknown_symbol_type_open_assumed"


# ── Candle staleness check ────────────────────────────────────────────────────

def is_candle_stale(timeframe: str, candle_timestamp: str,
                    now_utc: datetime = None) -> tuple:
    """
    Returns (is_stale: bool, reason: str).

    candle_timestamp: ISO string from Twelve Data, e.g. '2024-04-10 18:45:00'
    Thresholds are sourced from STALE_CANDLE_THRESHOLDS in config.py.

    If the timestamp cannot be parsed, returns (True, reason) — fail safe.
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)

    tf = timeframe.strip().upper()
    threshold_minutes = STALE_CANDLE_THRESHOLDS.get(tf, 120)  # default 2h

    if not candle_timestamp:
        return True, f"Candle timestamp missing for {tf} — treating as stale"

    # Twelve Data returns datetimes without timezone; they are UTC
    ts_clean = candle_timestamp.replace("T", " ").strip()
    # Drop sub-second precision if present
    if "." in ts_clean:
        ts_clean = ts_clean.split(".")[0]
    # Append UTC marker if not present
    if not ts_clean.endswith("+00:00") and not ts_clean.endswith("Z"):
        ts_clean += "+00:00"

    try:
        candle_dt = datetime.fromisoformat(ts_clean)
    except Exception:
        return True, f"Cannot parse candle timestamp '{candle_timestamp}' for {tf}"

    age_minutes = (now_utc - candle_dt).total_seconds() / 60.0

    if age_minutes > threshold_minutes:
        return True, (
            f"Latest {tf} candle is {int(age_minutes)} minutes old, "
            f"threshold is {threshold_minutes} minutes"
        )

    return False, f"{tf} candle is fresh ({int(age_minutes)}m old, limit {threshold_minutes}m)"


# ── Quote staleness check ─────────────────────────────────────────────────────

def is_quote_stale(symbol: str, quote_data: dict,
                   now_utc: datetime = None) -> tuple:
    """
    Returns (is_stale: bool, reason: str).

    Twelve Data's /price endpoint does not return a timestamp, so we cannot
    directly measure quote age.  Instead we fall back to truthful rules:
      - If quote_data is empty or has no price → stale
      - If quote_data has an explicit 'timestamp' field → use it
      - Otherwise → assume fresh (let candle check do the heavy lifting)

    This function intentionally avoids inventing certainty it does not have.
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)

    if not quote_data or not quote_data.get("price"):
        return True, f"Quote missing or empty for {symbol} — scanner aborted"

    # If the quote carries a timestamp field (some endpoints do), check it
    ts = quote_data.get("timestamp") or quote_data.get("datetime")
    if ts:
        try:
            ts_clean = str(ts).replace("T", " ").strip()
            if not ts_clean.endswith("+00:00") and not ts_clean.endswith("Z"):
                ts_clean += "+00:00"
            quote_dt     = datetime.fromisoformat(ts_clean)
            age_minutes  = (now_utc - quote_dt).total_seconds() / 60.0
            stale_thresh = STALE_CANDLE_THRESHOLDS.get("QUOTE", 30)
            if age_minutes > stale_thresh:
                return True, (
                    f"Quote timestamp is {int(age_minutes)} minutes old "
                    f"for {symbol} — scanner aborted"
                )
        except Exception:
            pass  # timestamp unparseable — fall through to assumed fresh

    # No timestamp available — we cannot confirm freshness; trust candle check
    return False, "quote_freshness_unverifiable_assumed_fresh"
