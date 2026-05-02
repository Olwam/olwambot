"""
structure_signals.py — Rule-based structural confluence layer.

Provides conservative, measurable SMC-lite signal detectors:
  - Liquidity sweep (swing high/low taken then reclaimed)
  - Fair Value Gap / imbalance (3-candle displacement)
  - Order Block zone (last opposing candle before impulse)
  - Breaker context (broken OB now re-tested from opposite side)
  - Fibonacci confluence (0.50–0.618 retracement zone)
  - Volatility health scoring (dead / low / healthy / elevated / chaotic)

All detectors return structured dicts so their findings flow into score
breakdowns and are visible in analytics.

Design rules:
  - Conservative: bonuses are small; false positives just add 0 points
  - Measurable: every finding is logged in score_breakdown
  - Additive: these signals supplement existing scoring, never replace it
  - No magic: only rule-based logic derived directly from candle data
"""

from config import (
    LIQUIDITY_SWEEP_BONUS,
    FVG_BONUS,
    ORDER_BLOCK_BONUS,
    BREAKER_CONTEXT_BONUS,
    FIB_CONFLUENCE_BONUS,
    VOLATILITY_DEAD_PENALTY,
    VOLATILITY_SPIKE_PENALTY,
)


# ── BOS / CHOCH Detection ─────────────────────────────────────────────────────

def detect_bos_choch(candles: list, current_trend: str = "neutral") -> dict:
    """
    Detects Break of Structure (BOS) and Change of Character (CHOCH).

    BOS = price breaks a swing level IN the direction of the current trend:
      - Bullish BOS: price breaks above a previous swing high
      - Bearish BOS: price breaks below a previous swing low

    CHOCH = price breaks a swing level AGAINST the current trend (first reversal sign):
      - Bullish CHOCH: price breaks above swing high while trend was bearish
      - Bearish CHOCH: price breaks below swing low while trend was bullish

    Returns dict with bos/choch detected, type, and broken level.
    """
    empty = {
        "bos_detected": False, "bos_type": None, "bos_level": None,
        "choch_detected": False, "choch_type": None, "choch_level": None,
        "description": "",
    }

    if len(candles) < 15:
        return empty

    look = candles[-50:] if len(candles) >= 50 else candles[:]

    swing_highs = []
    swing_lows  = []
    left = right = 3
    for i in range(left, len(look) - right - 3):
        h = look[i]["high"]
        l = look[i]["low"]
        is_sh = all(h > look[i - j]["high"] for j in range(1, left + 1)) and \
                all(h > look[i + j]["high"] for j in range(1, right + 1))
        is_sl = all(l < look[i - j]["low"] for j in range(1, left + 1)) and \
                all(l < look[i + j]["low"] for j in range(1, right + 1))
        if is_sh:
            swing_highs.append((i, h))
        if is_sl:
            swing_lows.append((i, l))

    if not swing_highs and not swing_lows:
        return empty

    recent = look[-3:]
    result = dict(empty)

    if swing_highs:
        last_sh_idx, last_sh = swing_highs[-1]
        for c in recent:
            if c["close"] > last_sh:
                if current_trend in ("bearish", "neutral"):
                    result["choch_detected"] = True
                    result["choch_type"]     = "bullish_choch"
                    result["choch_level"]    = round(last_sh, 5)
                    result["description"]    = f"Bullish CHOCH — broke above {round(last_sh, 5)} against bearish structure"
                else:
                    result["bos_detected"] = True
                    result["bos_type"]     = "bullish_bos"
                    result["bos_level"]    = round(last_sh, 5)
                    result["description"]  = f"Bullish BOS — continuation break above {round(last_sh, 5)}"
                break

    if swing_lows and not result["choch_detected"] and not result["bos_detected"]:
        last_sl_idx, last_sl = swing_lows[-1]
        for c in recent:
            if c["close"] < last_sl:
                if current_trend in ("bullish", "neutral"):
                    result["choch_detected"] = True
                    result["choch_type"]     = "bearish_choch"
                    result["choch_level"]    = round(last_sl, 5)
                    result["description"]    = f"Bearish CHOCH — broke below {round(last_sl, 5)} against bullish structure"
                else:
                    result["bos_detected"] = True
                    result["bos_type"]     = "bearish_bos"
                    result["bos_level"]    = round(last_sl, 5)
                    result["description"]  = f"Bearish BOS — continuation break below {round(last_sl, 5)}"
                break

    return result


# ── Liquidity Quality Score ───────────────────────────────────────────────────

def score_liquidity_quality(sweep: dict, candles: list, atr: float) -> dict:
    """
    Grades the quality of a liquidity sweep:
      Strong: takes equal highs/lows, large wick + strong rejection → +3
      Moderate: decent wick → +2
      Weak:   small wick, marginal sweep → +1
      None:   no sweep detected → 0 (neutral, not penalized)
    """
    if not sweep.get("detected") or atr <= 0:
        return {"quality": "none", "score": 0, "description": ""}

    sweep_type = sweep.get("type", "")

    best_c = None
    best_wick = 0
    for c in candles[-3:]:
        body = abs(c["close"] - c["open"])
        if "bullish" in sweep_type:
            wick = min(c["open"], c["close"]) - c["low"]
        else:
            wick = c["high"] - max(c["open"], c["close"])
        if wick > best_wick:
            best_wick = wick
            best_c = c

    if not best_c:
        return {"quality": "weak", "score": 1, "description": "Sweep detected — minimal rejection"}

    body = abs(best_c["close"] - best_c["open"])
    wick_body_ratio = best_wick / body if body > 0 else 0
    wick_atr_ratio  = best_wick / atr if atr > 0 else 0

    if wick_body_ratio >= 2.0 and wick_atr_ratio >= 0.4:
        return {
            "quality": "strong",
            "score": 3,
            "description": f"Strong sweep — large rejection wick ({wick_body_ratio:.1f}x body, {wick_atr_ratio:.1f}x ATR)",
        }
    elif wick_body_ratio >= 1.0 or wick_atr_ratio >= 0.2:
        return {
            "quality": "moderate",
            "score": 2,
            "description": f"Moderate sweep — decent wick ({wick_body_ratio:.1f}x body)",
        }
    else:
        return {
            "quality": "weak",
            "score": 1,
            "description": "Weak sweep — small wick, marginal rejection",
        }


# ── Equal Highs / Equal Lows (Liquidity Pools) ───────────────────────────────

def detect_equal_levels(candles: list, atr: float = 0) -> dict:
    """
    Detects equal highs or equal lows — areas where price has tested the
    same level multiple times, forming a liquidity pool that institutions
    target for stop hunts.

    Tolerance is ATR-based (0.3× ATR) to avoid false clustering on
    high-priced instruments like Gold.

    Returns detected pools with level and touch count.
    """
    empty = {"equal_highs": [], "equal_lows": [], "detected": False}

    if len(candles) < 15:
        return empty

    look = candles[-50:] if len(candles) >= 50 else candles[:]

    if atr <= 0:
        ranges = [c["high"] - c["low"] for c in look if c["high"] > c["low"]]
        atr = sum(ranges) / len(ranges) if ranges else 0
    if atr <= 0:
        return empty

    tol = atr * 0.3

    highs = [c["high"] for c in look]
    lows  = [c["low"]  for c in look]

    def _find_clusters(values, label):
        clusters = []
        used = set()
        for i in range(len(values)):
            if i in used:
                continue
            level = values[i]
            touches = [i]
            for j in range(i + 1, len(values)):
                if j in used:
                    continue
                if abs(values[j] - level) <= tol:
                    touches.append(j)
            if len(touches) >= 3:
                avg_level = sum(values[t] for t in touches) / len(touches)
                clusters.append({
                    "level": round(avg_level, 5),
                    "touches": len(touches),
                    "type": label,
                })
                for t in touches:
                    used.add(t)
        return clusters

    eq_highs = _find_clusters(highs, "equal_highs")
    eq_lows  = _find_clusters(lows,  "equal_lows")

    return {
        "equal_highs": sorted(eq_highs, key=lambda x: -x["touches"]),
        "equal_lows":  sorted(eq_lows,  key=lambda x: -x["touches"]),
        "detected": bool(eq_highs or eq_lows),
    }


# ── Market Intent ─────────────────────────────────────────────────────────────

def detect_market_intent(candles: list, sweep: dict, direction: str, atr: float) -> dict:
    """
    Classifies market intent as reversal, continuation, or consolidation.

    - Liquidity grab + reversal candle → REVERSAL
    - Strong breakout + continuation → CONTINUATION
    - Tight range / no momentum → CONSOLIDATION (NO TRADE)
    """
    empty = {"intent": "unclear", "description": "", "tradeable": False}

    if len(candles) < 10 or atr <= 0:
        return empty

    last_3 = candles[-3:]
    last_5 = candles[-5:]

    body_avg = sum(abs(c["close"] - c["open"]) for c in last_5) / len(last_5)
    range_avg = sum(c["high"] - c["low"] for c in last_5) / len(last_5)

    is_tight_range = range_avg < atr * 0.5
    if is_tight_range:
        return {
            "intent": "consolidation",
            "description": "Price in tight consolidation — no clear intent",
            "tradeable": False,
        }

    if sweep.get("detected"):
        last_c = candles[-1]
        sweep_type = sweep.get("type", "")
        if sweep_type == "bullish_sweep" and last_c["close"] > last_c["open"]:
            return {
                "intent": "reversal",
                "description": f"Liquidity sweep below {sweep['swept_level']} + bullish reversal — trapping sellers",
                "tradeable": True,
            }
        elif sweep_type == "bearish_sweep" and last_c["close"] < last_c["open"]:
            return {
                "intent": "reversal",
                "description": f"Liquidity sweep above {sweep['swept_level']} + bearish reversal — trapping buyers",
                "tradeable": True,
            }

    momentum_candles = 0
    for c in last_3:
        body = abs(c["close"] - c["open"])
        if body > atr * 0.6:
            if direction == "BUY" and c["close"] > c["open"]:
                momentum_candles += 1
            elif direction == "SELL" and c["close"] < c["open"]:
                momentum_candles += 1

    if momentum_candles >= 2:
        return {
            "intent": "continuation",
            "description": f"Strong momentum candles aligned with {direction} — continuation bias",
            "tradeable": True,
        }

    if body_avg < atr * 0.25:
        return {
            "intent": "consolidation",
            "description": "Small candle bodies with no directional momentum — ranging",
            "tradeable": False,
        }

    return {
        "intent": "unclear",
        "description": "Mixed candles — intent not yet confirmed",
        "tradeable": False,
    }


# ── Liquidity Sweep ────────────────────────────────────────────────────────────

def detect_liquidity_sweep(candles: list) -> dict:
    """
    Detects a liquidity sweep: a wick beyond a recent swing high/low that
    is immediately rejected, suggesting trapped orders were cleared.

    Bullish sweep: wick below recent swing low → closes back above it.
    Bearish sweep: wick above recent swing high → closes back below it.

    Only counts when wick depth is meaningful relative to candle body.
    """
    empty = {"detected": False, "type": None, "swept_level": None, "description": ""}

    if len(candles) < 10:
        return empty

    # Reference window for swing levels: exclude last 3 so they can be the reaction
    ref_end   = max(len(candles) - 3, 5)
    ref_start = max(ref_end - 22, 0)
    ref       = candles[ref_start:ref_end]

    if len(ref) < 5:
        return empty

    swing_high = max(c["high"] for c in ref)
    swing_low  = min(c["low"]  for c in ref)

    # Check the 3 most recent candles for the sweep+reclaim pattern
    for c in candles[-3:]:
        body = abs(c["close"] - c["open"])

        # Bullish sweep: wick below swing low, close reclaims above it
        if c["low"] < swing_low and c["close"] > swing_low:
            wick_depth = swing_low - c["low"]
            # Require wick to be at least 30% of body size (rules out tiny spikes)
            if body > 0 and wick_depth > body * 0.3:
                return {
                    "detected": True,
                    "type": "bullish_sweep",
                    "swept_level": round(swing_low, 5),
                    "description": f"Liquidity sweep below {round(swing_low, 5)} — bullish reclaim",
                }

        # Bearish sweep: wick above swing high, close rejects below it
        if c["high"] > swing_high and c["close"] < swing_high:
            wick_depth = c["high"] - swing_high
            if body > 0 and wick_depth > body * 0.3:
                return {
                    "detected": True,
                    "type": "bearish_sweep",
                    "swept_level": round(swing_high, 5),
                    "description": f"Liquidity sweep above {round(swing_high, 5)} — bearish rejection",
                }

    return empty


# ── Fair Value Gap (FVG / Imbalance) ──────────────────────────────────────────

def detect_fvg(candles: list) -> dict:
    """
    Detects a Fair Value Gap: a 3-candle formation where candle[i-2] and
    candle[i] do not overlap, leaving a price imbalance the market may fill.

    Bullish FVG: candle[i-2].high < candle[i].low  (gap to the upside)
    Bearish FVG: candle[i-2].low  > candle[i].high  (gap to the downside)

    Returns the most recent qualifying FVG and whether current price is inside it.
    """
    empty = {"detected": False, "type": None, "zone_high": None,
             "zone_low": None, "description": "", "price_inside": False}

    if len(candles) < 5:
        return empty

    current_price = candles[-1]["close"]
    look = candles[-16:]
    result = empty

    for i in range(2, len(look)):
        c0 = look[i - 2]
        c2 = look[i]

        # Bullish FVG: gap between c0.high and c2.low
        if c0["high"] < c2["low"]:
            z_low, z_high = c0["high"], c2["low"]
            if z_high > z_low:
                result = {
                    "detected":    True,
                    "type":        "bullish_fvg",
                    "zone_high":   round(z_high, 5),
                    "zone_low":    round(z_low, 5),
                    "description": f"Bullish FVG {round(z_low, 5)}–{round(z_high, 5)}",
                    "price_inside": z_low <= current_price <= z_high,
                }

        # Bearish FVG: gap between c2.high and c0.low
        elif c0["low"] > c2["high"]:
            z_low, z_high = c2["high"], c0["low"]
            if z_high > z_low:
                result = {
                    "detected":    True,
                    "type":        "bearish_fvg",
                    "zone_high":   round(z_high, 5),
                    "zone_low":    round(z_low, 5),
                    "description": f"Bearish FVG {round(z_low, 5)}–{round(z_high, 5)}",
                    "price_inside": z_low <= current_price <= z_high,
                }

    return result


# ── Order Block Zone ──────────────────────────────────────────────────────────

def detect_order_block_zone(candles: list, direction: str, atr: float) -> dict:
    """
    Detects the last opposing candle before a strong directional impulse move.

    Bullish OB: last bearish candle before a bullish impulse ≥ 1.2× ATR.
    Bearish OB: last bullish candle before a bearish impulse ≥ 1.2× ATR.

    Returns zone bounds and whether current price is entering the zone.
    """
    empty = {"detected": False, "zone_high": None, "zone_low": None,
             "description": "", "price_in_zone": False}

    if len(candles) < 10 or atr <= 0:
        return empty

    current_price = candles[-1]["close"]
    look = candles[-28:] if len(candles) >= 28 else candles[:]
    impulse_min = atr * 1.2

    if direction == "BUY":
        for i in range(len(look) - 3, 1, -1):
            c = look[i]
            if c["close"] >= c["open"]:     # skip non-bearish candles
                continue
            # Impulse: the leg from c's low to the next candle's high
            if i + 1 >= len(look):
                continue
            impulse = look[i + 1]["high"] - c["low"]
            if impulse >= impulse_min:
                z_low, z_high  = c["low"], c["high"]
                return {
                    "detected":     True,
                    "zone_high":    round(z_high, 5),
                    "zone_low":     round(z_low, 5),
                    "description":  f"Bullish OB {round(z_low, 5)}–{round(z_high, 5)}",
                    "price_in_zone": z_low <= current_price <= z_high,
                }

    elif direction == "SELL":
        for i in range(len(look) - 3, 1, -1):
            c = look[i]
            if c["close"] <= c["open"]:     # skip non-bullish candles
                continue
            if i + 1 >= len(look):
                continue
            impulse = c["high"] - look[i + 1]["low"]
            if impulse >= impulse_min:
                z_low, z_high  = c["low"], c["high"]
                return {
                    "detected":     True,
                    "zone_high":    round(z_high, 5),
                    "zone_low":     round(z_low, 5),
                    "description":  f"Bearish OB {round(z_low, 5)}–{round(z_high, 5)}",
                    "price_in_zone": z_low <= current_price <= z_high,
                }

    return empty


# ── Breaker Context ────────────────────────────────────────────────────────────

def detect_breaker_context(candles: list, direction: str, atr: float) -> dict:
    """
    Simplified breaker detection: a prior swing level has been broken with
    conviction and price is now holding on the correct side of it.

    For BUY:  a prior swing high was broken upward strongly → now acting as support.
    For SELL: a prior swing low  was broken downward strongly → now acting as resistance.

    Conviction threshold: breaking candle body ≥ 0.8× ATR.
    """
    empty = {"detected": False, "description": ""}

    if len(candles) < 20 or atr <= 0:
        return empty

    look = candles[-30:] if len(candles) >= 30 else candles[:]
    current_price = look[-1]["close"]

    if direction == "BUY":
        for i in range(8, len(look) - 2):
            ref_high = max(c["high"] for c in look[max(0, i - 5):i])
            c_next   = look[i + 1]
            body     = abs(c_next["close"] - c_next["open"])
            if c_next["close"] > ref_high and body >= atr * 0.8:
                # Broke up with conviction — check price still holds above
                if current_price >= ref_high * 0.9985:
                    return {
                        "detected":    True,
                        "description": f"Bullish structure break above {round(ref_high, 5)} — breaker support",
                    }

    elif direction == "SELL":
        for i in range(8, len(look) - 2):
            ref_low = min(c["low"] for c in look[max(0, i - 5):i])
            c_next  = look[i + 1]
            body    = abs(c_next["close"] - c_next["open"])
            if c_next["close"] < ref_low and body >= atr * 0.8:
                if current_price <= ref_low * 1.0015:
                    return {
                        "detected":    True,
                        "description": f"Bearish structure break below {round(ref_low, 5)} — breaker resistance",
                    }

    return empty


# ── Fibonacci Confluence ───────────────────────────────────────────────────────

def detect_fib_confluence(candles: list, direction: str,
                          current_price: float) -> dict:
    """
    Detects whether price is in a Fibonacci retracement confluence zone.

    For BUY setups: checks if price is at the 0.50–0.618 pullback of the
    most recent bullish leg (swing low → swing high).
    For SELL setups: checks if price is at the 0.382–0.618 pullback of the
    most recent bearish leg (swing high → swing low).

    Only applied when there is a clear directional leg (swing range > 2× ATR).
    """
    empty = {"detected": False, "fib_level": None, "zone": None, "description": ""}

    if len(candles) < 20 or not current_price:
        return empty

    look  = candles[-40:] if len(candles) >= 40 else candles[:]
    s_high = max(c["high"] for c in look)
    s_low  = min(c["low"]  for c in look)
    leg    = s_high - s_low

    if leg <= 0:
        return empty

    if direction == "BUY":
        # Expect price pulled back from swing high toward swing low
        # Key zones: 0.50 to 0.618 retracement of the bullish leg
        f618 = s_high - leg * 0.618
        f500 = s_high - leg * 0.500
        f382 = s_high - leg * 0.382

        if f618 <= current_price <= f382:
            nearest = 0.618 if current_price <= f500 else 0.5
            return {
                "detected":    True,
                "fib_level":   nearest,
                "zone":        (round(f618, 5), round(f382, 5)),
                "description": f"Fib {nearest} retracement — bullish confluence zone",
            }

    elif direction == "SELL":
        # Price pulled back from swing low toward swing high
        f382 = s_low + leg * 0.382
        f500 = s_low + leg * 0.500
        f618 = s_low + leg * 0.618

        if f382 <= current_price <= f618:
            nearest = 0.618 if current_price >= f500 else 0.5
            return {
                "detected":    True,
                "fib_level":   nearest,
                "zone":        (round(f382, 5), round(f618, 5)),
                "description": f"Fib {nearest} retracement — bearish confluence zone",
            }

    return empty


# ── Volatility Health Score ────────────────────────────────────────────────────

def score_volatility(atr: float, expected_atr: float) -> dict:
    """
    Classifies market volatility as dead / low / healthy / elevated / chaotic
    by comparing current ATR against the expected ATR for this instrument.

    Returns a score adjustment (negative = penalty, 0 = neutral) and label.
    """
    if expected_atr <= 0 or atr <= 0:
        return {"label": "unknown", "score_adjustment": 0, "ratio": 0.0, "description": ""}

    ratio = atr / expected_atr

    if ratio < 0.25:
        label, pts = "dead",     VOLATILITY_DEAD_PENALTY
    elif ratio < 0.50:
        label, pts = "low",      max(VOLATILITY_DEAD_PENALTY // 2, -3)
    elif ratio < 2.50:
        label, pts = "healthy",  0
    elif ratio < 4.00:
        label, pts = "elevated", max(VOLATILITY_SPIKE_PENALTY // 2, -3)
    else:
        label, pts = "chaotic",  VOLATILITY_SPIKE_PENALTY

    return {
        "label":           label,
        "score_adjustment": pts,
        "ratio":           round(ratio, 2),
        "description":     f"Volatility {label} — ATR {round(ratio*100)}% of expected",
    }


# ── Master wrapper ─────────────────────────────────────────────────────────────

def compute_structure_signals(candles: list, direction: str,
                              current_price: float, symbol: str,
                              atr: float, expected_atr: float) -> dict:
    """
    Runs all structure signal detectors and returns a combined result.

    Returns a dict containing:
      - Individual detector results (sweep, fvg, order_block, breaker, fib, volatility)
      - Per-detector score adjustments
      - 'total_bonus': net score adjustment to apply to confidence
      - 'narrative_parts': list of human-readable explanation fragments

    All values are conservative. A non-detection contributes 0, not a penalty.
    Opposing signals apply a mild negative adjustment only for liquidity sweeps.
    """
    sweep   = detect_liquidity_sweep(candles)
    fvg     = detect_fvg(candles)
    ob      = detect_order_block_zone(candles, direction, atr)
    breaker = detect_breaker_context(candles, direction, atr)
    fib     = detect_fib_confluence(candles, direction, current_price)
    vol     = score_volatility(atr, expected_atr)
    eq_levels = detect_equal_levels(candles, atr=atr)
    intent    = detect_market_intent(candles, sweep, direction, atr)

    current_trend = "bullish" if direction == "BUY" else "bearish"
    bos_choch = detect_bos_choch(candles, current_trend=current_trend)
    liq_quality = score_liquidity_quality(sweep, candles, atr)

    narrative_parts = []

    # ── Liquidity sweep points ─────────────────────────────────────────────────
    sweep_pts = 0
    if sweep["detected"]:
        aligned = (direction == "BUY"  and sweep["type"] == "bullish_sweep") or \
                  (direction == "SELL" and sweep["type"] == "bearish_sweep")
        if aligned:
            sweep_pts = LIQUIDITY_SWEEP_BONUS
            narrative_parts.append(sweep["description"])
        else:
            sweep_pts = -2   # opposing sweep — mild warning

    # ── FVG points — only when price is inside the aligned gap ────────────────
    fvg_pts = 0
    if fvg["detected"] and fvg.get("price_inside"):
        aligned = (direction == "BUY"  and fvg["type"] == "bullish_fvg") or \
                  (direction == "SELL" and fvg["type"] == "bearish_fvg")
        if aligned:
            fvg_pts = FVG_BONUS
            narrative_parts.append(fvg["description"])

    # ── Order block points — only when price is entering the zone ─────────────
    ob_pts = 0
    if ob["detected"] and ob.get("price_in_zone"):
        ob_pts = ORDER_BLOCK_BONUS
        narrative_parts.append(ob["description"])

    # ── Breaker context ────────────────────────────────────────────────────────
    breaker_pts = 0
    if breaker["detected"]:
        breaker_pts = BREAKER_CONTEXT_BONUS
        narrative_parts.append(breaker["description"])

    # ── Fibonacci confluence ───────────────────────────────────────────────────
    fib_pts = 0
    if fib["detected"]:
        fib_pts = FIB_CONFLUENCE_BONUS
        narrative_parts.append(fib["description"])

    # ── Volatility adjustment ──────────────────────────────────────────────────
    vol_pts = vol["score_adjustment"]
    if vol["label"] in ("dead", "chaotic", "low", "elevated"):
        narrative_parts.append(vol["description"])

    eq_pts = 0
    if eq_levels["detected"]:
        eq_pts = 3
        best_pool = None
        if eq_levels["equal_highs"]:
            best_pool = eq_levels["equal_highs"][0]
        if eq_levels["equal_lows"]:
            low_pool = eq_levels["equal_lows"][0]
            if not best_pool or low_pool["touches"] > best_pool["touches"]:
                best_pool = low_pool
        if best_pool:
            narrative_parts.append(
                f"{best_pool['type'].replace('_', ' ').title()} at {best_pool['level']} "
                f"({best_pool['touches']} touches) — liquidity pool"
            )

    intent_pts = 0
    if intent["intent"] == "reversal":
        intent_pts = 4
        narrative_parts.append(intent["description"])
    elif intent["intent"] == "continuation":
        intent_pts = 2
        narrative_parts.append(intent["description"])
    elif intent["intent"] == "consolidation":
        intent_pts = -5

    bos_choch_pts = 0
    if bos_choch["choch_detected"]:
        bos_choch_pts = 4
        narrative_parts.append(bos_choch["description"])
    elif bos_choch["bos_detected"]:
        bos_choch_pts = 3
        narrative_parts.append(bos_choch["description"])

    liq_qual_pts = liq_quality["score"]
    if liq_quality["quality"] != "none":
        narrative_parts.append(liq_quality["description"])

    total_bonus = (sweep_pts + fvg_pts + ob_pts + breaker_pts + fib_pts
                   + vol_pts + eq_pts + intent_pts + bos_choch_pts + liq_qual_pts)

    return {
        "sweep":         sweep,
        "fvg":           fvg,
        "order_block":   ob,
        "breaker":       breaker,
        "fib":           fib,
        "volatility":    vol,
        "equal_levels":  eq_levels,
        "market_intent": intent,
        "bos_choch":     bos_choch,
        "liq_quality":   liq_quality,
        "sweep_pts":     sweep_pts,
        "fvg_pts":       fvg_pts,
        "ob_pts":        ob_pts,
        "breaker_pts":   breaker_pts,
        "fib_pts":       fib_pts,
        "vol_pts":       vol_pts,
        "eq_pts":        eq_pts,
        "intent_pts":    intent_pts,
        "bos_choch_pts": bos_choch_pts,
        "liq_qual_pts":  liq_qual_pts,
        "total_bonus":   total_bonus,
        "narrative_parts": narrative_parts,
    }
