"""
Live market data via Twelve Data.
Provides quotes, candle fetching, and higher-timeframe context helpers.
Includes a lightweight API-call counter so /health can show daily usage.
"""

import re
import threading
from datetime import datetime, timezone, timedelta

import requests

from config import TWELVEDATA_API_KEY, REQUEST_TIMEOUT

http = requests.Session()

# ── API usage counter ──────────────────────────────────────────────────────────
# Counts HTTP calls to Twelve Data for the current calendar day (SAST).
# Resets at midnight SAST.  Used purely for informational display in /health.

_SAST = timezone(timedelta(hours=2))
_api_lock         = threading.Lock()
_api_call_date    = ""     # SAST date string YYYY-MM-DD
_api_call_count   = 0


def _record_api_call():
    global _api_call_date, _api_call_count
    today = datetime.now(_SAST).strftime("%Y-%m-%d")
    with _api_lock:
        if _api_call_date != today:
            _api_call_date  = today
            _api_call_count = 0
        _api_call_count += 1


def get_api_usage_today() -> int:
    """Returns number of Twelve Data API calls made today (SAST day)."""
    with _api_lock:
        return _api_call_count


SYMBOL_MAP = {
    "XAUUSD": "XAU/USD",
    "XAGUSD": "XAG/USD",
    "EURUSD": "EUR/USD",
    "GBPUSD": "GBP/USD",
    "USDJPY": "USD/JPY",
    "AUDUSD": "AUD/USD",
    "USDCAD": "USD/CAD",
    "NZDUSD": "NZD/USD",
    "USDCHF": "USD/CHF",
    "GBPJPY": "GBP/JPY",
    "EURJPY": "EUR/JPY",
    "EURGBP": "EUR/GBP",
    "NAS100": "IXIC",
    "USTEC":  "IXIC",
    "US30":   "DJI",
}

TIMEFRAME_MAP = {
    "M1":  "1min",
    "M5":  "5min",
    "M15": "15min",
    "M30": "30min",
    "H1":  "1h",
    "H4":  "4h",
    "D1":  "1day",
    "W1":  "1week",
}

HIGHER_TIMEFRAME_MAP = {
    "M1":  "M15",
    "M5":  "M30",
    "M15": "H1",
    "M30": "H4",
    "H1":  "H4",
    "H4":  "D1",
    "D1":  "W1",
}


def normalize_symbol(symbol: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", symbol.upper())


def normalize_symbol_for_api(symbol: str) -> str:
    sym = normalize_symbol(symbol)
    if sym in SYMBOL_MAP:
        return SYMBOL_MAP[sym]
    if len(sym) == 6:
        return f"{sym[:3]}/{sym[3:]}"
    return sym


def timeframe_to_interval(tf: str) -> str:
    tf = tf.strip().upper()
    return TIMEFRAME_MAP.get(tf, "15min")


def get_higher_timeframe(tf: str) -> str:
    tf = tf.strip().upper()
    return HIGHER_TIMEFRAME_MAP.get(tf, "H1")


def has_market_data() -> bool:
    return bool(TWELVEDATA_API_KEY)


def get_quote(symbol: str) -> dict:
    if not TWELVEDATA_API_KEY:
        print("  [MarketData] No Twelve Data key — skipping live quote.", flush=True)
        return {}
    api_sym = normalize_symbol_for_api(symbol)
    try:
        url    = "https://api.twelvedata.com/price"
        params = {"symbol": api_sym, "apikey": TWELVEDATA_API_KEY}
        print(f"  [MarketData] Fetching live quote: {api_sym}", flush=True)
        r = http.get(url, params=params, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        _record_api_call()
        data = r.json()
        if "price" in data:
            price = float(data["price"])
            print(f"  [MarketData] Live price for {api_sym}: {price}", flush=True)
            return {"price": price, "symbol": api_sym}
        msg = data.get("message", data.get("code", "unknown error"))
        print(f"  [MarketData] Quote error for {api_sym}: {msg}", flush=True)
        return {}
    except Exception as e:
        print(f"  [MarketData] Quote fetch exception for {symbol}: {e}", flush=True)
        return {}


def get_candles(symbol: str, interval: str = "15min", outputsize: int = 100) -> list:
    if not TWELVEDATA_API_KEY:
        print("  [MarketData] No Twelve Data key — skipping candles.", flush=True)
        return []
    api_sym = normalize_symbol_for_api(symbol)
    try:
        url    = "https://api.twelvedata.com/time_series"
        params = {
            "symbol":     api_sym,
            "interval":   interval,
            "outputsize": min(outputsize, 200),
            "apikey":     TWELVEDATA_API_KEY,
        }
        print(f"  [MarketData] Fetching {outputsize} candles: {api_sym} @ {interval}", flush=True)
        r = http.get(url, params=params, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        _record_api_call()
        data = r.json()
        if data.get("status") == "error":
            print(f"  [MarketData] Candle API error for {api_sym}: {data.get('message','')}", flush=True)
            return []
        values  = data.get("values", [])
        candles = []
        for v in values:
            try:
                candles.append({
                    "open":     float(v["open"]),
                    "high":     float(v["high"]),
                    "low":      float(v["low"]),
                    "close":    float(v["close"]),
                    "datetime": v.get("datetime", ""),
                })
            except (ValueError, KeyError):
                continue
        candles.reverse()   # oldest → newest
        print(f"  [MarketData] Got {len(candles)} candles for {api_sym}", flush=True)
        return candles
    except Exception as e:
        print(f"  [MarketData] Candle fetch exception for {symbol}: {e}", flush=True)
        return []


def get_higher_timeframe_context(symbol: str, timeframe: str) -> dict | None:
    """
    Fetches candles on the higher timeframe and computes market context.
    Returns None if data is insufficient or unavailable.
    """
    if not TWELVEDATA_API_KEY:
        return None

    from indicators import compute_market_context

    htf      = get_higher_timeframe(timeframe)
    interval = timeframe_to_interval(htf)
    candles  = get_candles(symbol, interval, outputsize=60)

    if len(candles) < 20:
        print(f"  [MarketData] Insufficient HTF candles for {symbol} @ {htf}", flush=True)
        return None

    ctx = compute_market_context(candles)
    ctx["timeframe"] = htf
    print(f"  [MarketData] HTF ({htf}) context for {symbol}: "
          f"bias={ctx['trend_bias']} regime={ctx['regime']}", flush=True)
    return ctx


def get_multi_timeframe_data(symbol: str) -> dict:
    """
    Fetches OHLC candles across four timeframes: D1, H4, H1, M15.
    Returns a dict keyed by timeframe — any level that fails returns [].

    API calls: up to 4 (one per timeframe).
    Only called after the trade passes early gates to save API quota.
    """
    result = {}
    for tf, outputsize in [("D1", 60), ("H4", 60), ("H1", 60), ("M15", 100)]:
        interval = timeframe_to_interval(tf)
        candles  = get_candles(symbol, interval, outputsize=outputsize)
        result[tf] = candles
    return result


def compute_mtf_alignment(symbol: str, entry_direction: str) -> dict:
    """
    Fetches D1, H4, H1 context and scores directional alignment for
    an intended trade direction ("BUY" or "SELL").

    Scoring:
      +4  per fully-aligned timeframe (bias matches direction)
      -4  per timeframe that directly conflicts
      ± 0 for neutral

    Total alignment bucket:
      >= 10  → "full"    (all or near-all aligned)
      >= 4   → "partial" (majority aligned)
      <  0   → "conflict"
      else   → "neutral"

    Returns:
      {
        "score":              int,
        "bucket":             "full" | "partial" | "neutral" | "conflict",
        "aligned_timeframes": ["H4", "D1", ...],
        "bias_by_tf":         {"D1": "bullish", "H4": "neutral", "H1": "bearish"},
      }
    """
    if not TWELVEDATA_API_KEY:
        return {"score": 0, "bucket": "neutral", "aligned_timeframes": [], "bias_by_tf": {}}

    from indicators import compute_market_context

    direction_bias = "bullish" if entry_direction == "BUY" else "bearish"
    opposite_bias  = "bearish" if entry_direction == "BUY" else "bullish"

    score         = 0
    bias_by_tf    = {}
    aligned_tfs   = []

    for tf, outputsize in [("D1", 60), ("H4", 60), ("H1", 60)]:
        interval = timeframe_to_interval(tf)
        candles  = get_candles(symbol, interval, outputsize=outputsize)
        if len(candles) < 20:
            bias_by_tf[tf] = "unknown"
            continue
        ctx = compute_market_context(candles)
        bias = ctx.get("trend_bias", "neutral")
        bias_by_tf[tf] = bias
        if bias == direction_bias:
            score += 4
            aligned_tfs.append(tf)
        elif bias == opposite_bias:
            score -= 4

    if score >= 10:
        bucket = "full"
    elif score >= 4:
        bucket = "partial"
    elif score < 0:
        bucket = "conflict"
    else:
        bucket = "neutral"

    print(f"  [MTF] {symbol} {entry_direction} alignment: score={score} ({bucket}) "
          f"biases={bias_by_tf}", flush=True)

    return {
        "score":              score,
        "bucket":             bucket,
        "aligned_timeframes": aligned_tfs,
        "bias_by_tf":         bias_by_tf,
    }
