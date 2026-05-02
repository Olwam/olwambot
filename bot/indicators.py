"""
Technical indicator calculations.
Provides EMA, ATR, swing detection, and composite market context
used by the decision engine, scanner, and structure signals.
"""


def compute_ema(prices: list, period: int) -> list:
    """Exponential Moving Average."""
    if not prices or period <= 0:
        return []
    ema        = [prices[0]]
    multiplier = 2.0 / (period + 1)
    for i in range(1, len(prices)):
        val = prices[i] * multiplier + ema[-1] * (1 - multiplier)
        ema.append(val)
    return ema


def compute_atr(candles: list, period: int = 14) -> float:
    """Average True Range."""
    if len(candles) < 2:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        high       = candles[i]["high"]
        low        = candles[i]["low"]
        prev_close = candles[i - 1]["close"]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    if not trs:
        return 0.0
    if len(trs) <= period:
        return sum(trs) / len(trs)
    return sum(trs[-period:]) / period


def _find_swing_points(candles: list, left: int = 3, right: int = 3):
    """
    Detect proper swing highs and lows using left/right bar comparison.
    A swing high is a bar whose high is higher than the `left` bars before
    AND the `right` bars after it.  Vice-versa for swing low.
    Returns (list_of_swing_highs, list_of_swing_lows) as price floats,
    sorted chronologically.
    """
    highs = []
    lows  = []
    if len(candles) < left + right + 1:
        return highs, lows
    for i in range(left, len(candles) - right):
        h = candles[i]["high"]
        l = candles[i]["low"]
        is_sh = all(h > candles[i - j]["high"] for j in range(1, left + 1)) and \
                all(h > candles[i + j]["high"] for j in range(1, right + 1))
        is_sl = all(l < candles[i - j]["low"] for j in range(1, left + 1)) and \
                all(l < candles[i + j]["low"] for j in range(1, right + 1))
        if is_sh:
            highs.append(h)
        if is_sl:
            lows.append(l)
    return highs, lows


def find_swing_high(candles: list, lookback: int = 50) -> float:
    recent = candles[-lookback:] if len(candles) >= lookback else candles
    highs, _ = _find_swing_points(recent)
    if highs:
        return highs[-1]
    if not recent:
        return 0.0
    return max(c["high"] for c in recent[-10:])


def find_swing_low(candles: list, lookback: int = 50) -> float:
    recent = candles[-lookback:] if len(candles) >= lookback else candles
    _, lows = _find_swing_points(recent)
    if lows:
        return lows[-1]
    if not recent:
        return 0.0
    return min(c["low"] for c in recent[-10:])


def find_support_resistance(candles: list, lookback: int = 80, current_price: float = 0):
    """
    Returns (nearest_support, nearest_resistance) based on swing point clusters.
    Support = nearest swing low below current price.
    Resistance = nearest swing high above current price.
    """
    recent = candles[-lookback:] if len(candles) >= lookback else candles
    highs, lows = _find_swing_points(recent)
    if not current_price and recent:
        current_price = recent[-1]["close"]

    support    = None
    resistance = None

    below = [l for l in lows if l < current_price]
    above = [h for h in highs if h > current_price]

    if below:
        support = max(below)
    if above:
        resistance = min(above)

    return support, resistance


def detect_structure_trend(candles: list, lookback: int = 50) -> str:
    """
    Detects trend using actual swing structure (HH/HL for bullish, LH/LL for bearish).
    Uses the last 3 swing highs and 3 swing lows for more reliable sequencing.
    Returns 'bullish', 'bearish', or 'neutral'.
    """
    recent = candles[-lookback:] if len(candles) >= lookback else candles
    highs, lows = _find_swing_points(recent)

    if len(highs) < 3 or len(lows) < 3:
        if len(highs) >= 2 and len(lows) >= 2:
            hh = highs[-1] > highs[-2]
            hl = lows[-1] > lows[-2]
            lh = highs[-1] < highs[-2]
            ll = lows[-1] < lows[-2]
            if hh and hl:
                return "bullish"
            if lh and ll:
                return "bearish"
        return "neutral"

    # Check last 3 swings for consistent sequence
    hh_count = sum(1 for i in range(1, min(3, len(highs))) if highs[-i] > highs[-i-1])
    hl_count = sum(1 for i in range(1, min(3, len(lows)))  if lows[-i] > lows[-i-1])
    lh_count = sum(1 for i in range(1, min(3, len(highs))) if highs[-i] < highs[-i-1])
    ll_count = sum(1 for i in range(1, min(3, len(lows)))  if lows[-i] < lows[-i-1])

    if hh_count >= 2 and hl_count >= 2:
        return "bullish"
    if lh_count >= 2 and ll_count >= 2:
        return "bearish"
    return "neutral"


def compute_ema_slope(ema_values: list, lookback: int = 5) -> str:
    """
    Returns 'rising', 'falling', or 'flat' based on recent EMA direction.
    A rising EMA slope in a bearish context hints at potential reversal and
    vice-versa, so this is used to strengthen or weaken the regime call.
    """
    if len(ema_values) < lookback + 1:
        return "flat"
    recent    = ema_values[-(lookback + 1):]
    delta     = recent[-1] - recent[0]
    threshold = abs(recent[0]) * 0.0001 if recent[0] else 0.0001
    if delta > threshold:
        return "rising"
    if delta < -threshold:
        return "falling"
    return "flat"


def detect_market_regime(candles: list, trend_bias: str, pullback: bool,
                          range_ratio: float, ema_slope: str) -> str:
    """
    Classifies the market regime using price structure, EMA slope,
    swing behaviour, and ATR-relative range.

    Returns one of: trending, pullback, reversal, range, choppy, mixed
    - "range"  : clearly bounded, low range_ratio (<3) — price oscillating
    - "choppy" : moderate range_ratio (3-5) with flat EMA — no directional edge
    - "mixed"  : trend_bias neutral but range_ratio >= 5 — indeterminate
    """
    if trend_bias in ("bullish", "bearish"):
        if pullback:
            return "pullback"
        if ema_slope in ("rising" if trend_bias == "bullish" else "falling",):
            return "trending"
        if ema_slope in ("falling" if trend_bias == "bullish" else "rising",):
            return "reversal"
        return "trending"
    # Neutral trend bias paths
    if range_ratio < 3:
        return "range"
    if range_ratio < 5 or ema_slope == "flat":
        return "choppy"
    return "mixed"


def estimate_atr(symbol: str, price: float) -> float:
    """
    Returns an approximate expected ATR for a symbol when live candle data
    is unavailable.  Used as the baseline for volatility scoring.

    Moved here from decision_engine.py so it can be imported by both
    decision_engine and structure_signals without a circular dependency.
    """
    sym = symbol.upper().replace("/", "").replace("-", "")
    if sym == "XAUUSD":
        return price * 0.003
    if sym == "XAGUSD":
        return price * 0.005
    if "JPY" in sym:
        return 0.15
    if sym in ("NAS100", "USTEC"):
        return price * 0.003
    if sym in ("US30", "DJI"):
        return price * 0.002
    if len(sym) == 6:
        return 0.0015
    return price * 0.002


def detect_pullback(candles: list, ema_fast_val: float, ema_slow_val: float,
                    atr: float) -> dict:
    """
    Determines whether price has pulled back from an impulse move into the EMA
    zone or key structure level — creating a valid, high-probability entry.

    Returns:
        is_pullback      — True if a valid pullback into the zone is detected
        is_overextended  — True if price is stretched > 1.5 ATR from EMA in
                           trend direction (anti-FOMO flag)
        pullback_depth   — How far price retraced from the impulse high/low
                           expressed in ATR units
        zone             — Suggested entry price (EMA or structure level)
        pullback_low     — Lowest close of recent pullback candles (BUY SL ref)
        pullback_high    — Highest close of recent pullback candles (SELL SL ref)
        ema_distance_atr — Current price distance from EMA in ATR units
    """
    empty = {
        "is_pullback":      False,
        "is_overextended":  False,
        "pullback_depth":   0.0,
        "zone":             0.0,
        "pullback_low":     0.0,
        "pullback_high":    0.0,
        "ema_distance_atr": 0.0,
    }
    if not candles or atr <= 0 or ema_fast_val is None or ema_slow_val is None:
        return empty

    current_price = candles[-1]["close"]
    trend_bullish = ema_fast_val > ema_slow_val
    trend_bearish = ema_fast_val < ema_slow_val

    if not trend_bullish and not trend_bearish:
        return empty   # flat — no trend to pull back from

    ema_distance     = abs(current_price - ema_fast_val)
    ema_distance_atr = round(ema_distance / atr, 2)
    result           = dict(empty)
    result["ema_distance_atr"] = ema_distance_atr

    # ── Overextended: price stretched > 1.5 ATR beyond EMA in trend direction ──
    if trend_bullish and current_price > ema_fast_val + atr * 1.5:
        result["is_overextended"] = True
        return result
    if trend_bearish and current_price < ema_fast_val - atr * 1.5:
        result["is_overextended"] = True
        return result

    # ── Recent structure bounds (last 8 candles) ───────────────────────────────
    recent_n      = min(8, len(candles))
    recent        = candles[-recent_n:]
    pullback_low  = min(c["low"]  for c in recent)
    pullback_high = max(c["high"] for c in recent)
    result["pullback_low"]  = round(pullback_low,  5)
    result["pullback_high"] = round(pullback_high, 5)

    # ── Impulse region: the 20 candles before the recent 8 ─────────────────────
    impulse_n   = min(20, max(1, len(candles) - recent_n))
    impulse_reg = candles[-(recent_n + impulse_n):-recent_n]
    if not impulse_reg:
        impulse_reg = candles

    if trend_bullish:
        impulse_high   = max(c["high"] for c in impulse_reg)
        pullback_depth = impulse_high - current_price
        depth_in_atr   = pullback_depth / atr

        # Valid pullback conditions:
        #  1. Price has retraced at least 0.5 ATR from the impulse high
        #  2. Price is now within 1 ATR of the fast EMA (near the zone)
        near_ema = current_price <= ema_fast_val + atr
        if depth_in_atr >= 0.5 and near_ema:
            result["is_pullback"]    = True
            result["pullback_depth"] = round(depth_in_atr, 2)
            result["zone"]           = round(
                min(ema_fast_val, current_price + atr * 0.1), 5
            )
    else:   # bearish
        impulse_low    = min(c["low"] for c in impulse_reg)
        pullback_depth = current_price - impulse_low
        depth_in_atr   = pullback_depth / atr

        near_ema = current_price >= ema_fast_val - atr
        if depth_in_atr >= 0.5 and near_ema:
            result["is_pullback"]    = True
            result["pullback_depth"] = round(depth_in_atr, 2)
            result["zone"]           = round(
                max(ema_fast_val, current_price - atr * 0.1), 5
            )

    return result


def compute_market_context(candles: list) -> dict:
    """
    Full market context from a candle list.
    Returns trend bias, regime, momentum, EMAs, ATR, swing levels,
    pullback flag, and EMA slope.
    """
    empty = {
        "ema_fast":   None,
        "ema_slow":   None,
        "trend_bias": "neutral",
        "swing_high": None,
        "swing_low":  None,
        "atr":        0.0,
        "momentum":   "neutral",
        "regime":     "mixed",
        "pullback":   False,
        "ema_slope":  "flat",
    }
    if len(candles) < 20:
        return empty

    closes = [c["close"] for c in candles]

    ema_fast_series = compute_ema(closes, 9)
    ema_slow_series = compute_ema(closes, 21)
    fast_now        = ema_fast_series[-1]
    slow_now        = ema_slow_series[-1]
    current_price   = closes[-1]

    # EMA-based bias
    if fast_now > slow_now and current_price > fast_now:
        ema_bias = "bullish"
    elif fast_now < slow_now and current_price < fast_now:
        ema_bias = "bearish"
    else:
        ema_bias = "neutral"

    # Structure-based bias (higher highs/lows or lower highs/lows)
    struct_bias = detect_structure_trend(candles)

    # Both must agree — if either is neutral or they conflict, no trade
    if ema_bias == struct_bias and ema_bias != "neutral":
        trend_bias = ema_bias
    else:
        trend_bias = "neutral"

    swing_high = find_swing_high(candles)
    swing_low  = find_swing_low(candles)

    atr         = compute_atr(candles)
    recent_range = swing_high - swing_low
    range_ratio  = (recent_range / atr) if atr > 0 else 0

    ema_slope = compute_ema_slope(ema_fast_series, lookback=5)

    pullback = False
    if trend_bias == "bullish":
        if current_price < fast_now and current_price > slow_now:
            pullback = True
        momentum = "pullback" if pullback else "strong"
    elif trend_bias == "bearish":
        if current_price > fast_now and current_price < slow_now:
            pullback = True
        momentum = "pullback" if pullback else "strong"
    else:
        momentum = "neutral"

    regime = detect_market_regime(candles, trend_bias, pullback, range_ratio, ema_slope)

    # ── Detailed pullback analysis ─────────────────────────────────────────────
    pb_data = detect_pullback(candles, fast_now, slow_now, atr)

    return {
        "ema_fast":      round(fast_now, 5),
        "ema_slow":      round(slow_now, 5),
        "trend_bias":    trend_bias,
        "swing_high":    round(swing_high, 5),
        "swing_low":     round(swing_low, 5),
        "atr":           round(atr, 5),
        "momentum":      momentum,
        "regime":        regime,
        "pullback":      pullback or pb_data["is_pullback"],
        "ema_slope":     ema_slope,
        "pullback_data": pb_data,
    }
