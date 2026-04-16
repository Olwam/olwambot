"""
Chart vision analysis via OpenAI GPT-4o.
Includes screenshot quality scoring — poor-quality charts are flagged
and rejected before the decision engine runs.
"""

import json
import time
import base64

from openai import OpenAI
from config import OPENAI_API_KEY, OPENAI_MODEL

client = OpenAI(api_key=OPENAI_API_KEY)

VISION_PROMPT = """
You are CHEFBUNTSA SNIPER AI — an elite institutional-grade Forex trading assistant.
Your job is to analyze chart images and ONLY identify high-probability trade setups.
You must be extremely strict. If conditions are not perfect → NO TRADE.

CORE PHILOSOPHY:
Do NOT trade based on indicators alone. Trade based on:
- Market structure (HH/HL for bullish, LH/LL for bearish)
- Liquidity behavior (stop hunts, equal highs/lows)
- Price intent (reversal after sweep, continuation after breakout)
- Confirmation candles at key levels
If intent is unclear → NO TRADE.

STEP-BY-STEP ANALYSIS (apply ALL):

1. TREND & STRUCTURE
   - Determine trend using EMA 9/21 alignment AND structure (HH/HL or LH/LL)
   - If EMA and structure disagree → trend_bias = neutral

2. KEY LEVELS
   - Identify support/resistance (swing-based), demand/supply zones
   - BUY near resistance → BLOCK. SELL near support → BLOCK. Mid-range → NO TRADE.
   - Only trade AT key levels.

3. LIQUIDITY ENGINE
   - Look for equal highs / equal lows (liquidity pools)
   - Detect liquidity sweeps: price breaks level then closes back inside (stop hunt)
   - Detect fake breakouts: wick beyond level with rejection
   - Sweep + rejection → strong confirmation. No liquidity event → weak setup.
   - Prefer trades AFTER liquidity is taken.

4. MARKET INTENT
   - Liquidity grab + reversal → "reversal" bias
   - Strong breakout + continuation → "continuation" bias
   - Consolidation → NO TRADE
   - Only trade when intent is CLEAR.

5. CANDLE CONFIRMATION
   Bullish: engulfing, hammer (long lower wick), strong rejection candle
   Bearish: engulfing, shooting star (long upper wick), strong rejection candle
   - Must occur at key level. Must align with liquidity + intent.
   - If no confirmation → NO TRADE.

6. PULLBACK VALIDATION
   - Price must retrace into EMA zone or key level
   - Overextended moves → BLOCK

FAILSAFE: If ANY condition is not met → entry_readiness = "no_setup"

Return ONLY JSON with these exact fields:
{
  "readable": true,
  "pair": "XAUUSD",
  "timeframe": "M15",
  "visible_price": 3245.5,
  "trend_bias": "bullish",
  "structure_summary": "HH/HL structure with liquidity sweep below demand zone.",
  "clean_chart": true,
  "key_levels": [3240.0, 3250.0, 3260.0],
  "support_zone": 3238.0,
  "resistance_zone": 3262.0,
  "notes": "Sweep of equal lows at 3238, bullish engulfing from demand.",

  "price_location": "at_support",
  "entry_readiness": "pullback_confirmed",
  "confirmation_candle": true,
  "sniper_score": 8,
  "sniper_reasoning": "Trend aligned, sweep of lows + engulfing confirmation at demand.",

  "market_intent": "reversal",
  "market_intent_reasoning": "Liquidity swept below equal lows, strong rejection back inside range.",
  "liquidity_event": "Sweep below 3238 (equal lows, 3 touches) — bullish reclaim",
  "equal_levels_detected": true,

  "quality_score": 82,
  "quality_issues": [],
  "pair_visible": true,
  "timeframe_visible": true,
  "candle_clarity": "good",
  "drawing_overload": false,
  "zoom_acceptable": true,
  "is_cropped": false
}

Field definitions:
- readable: true if chart is clear enough to analyze
- pair: instrument shown (EURUSD, XAUUSD etc.), "UNKNOWN" if not visible
- timeframe: chart timeframe, "UNKNOWN" if not visible
- visible_price: current/last price visible on chart
- trend_bias: "bullish" (HH+HL), "bearish" (LH+LL), or "neutral" (unclear/ranging)
- structure_summary: one sentence about market structure, liquidity, and intent
- clean_chart: true if chart is tradeable
- key_levels: 2–5 important visible price levels
- support_zone: nearest support (null if none)
- resistance_zone: nearest resistance (null if none)
- notes: observations — FVG, OB, CHoCH, BOS, sweep, equal levels etc.

Sniper fields:
- price_location: "at_support", "at_resistance", "mid_range", or "breakout"
- entry_readiness: "pullback_confirmed", "breakout_confirmed", "awaiting_confirmation", or "no_setup"
- confirmation_candle: true only if engulfing/pin bar/rejection wick at key level
- sniper_score: 0–10 strict scoring:
  * Trend alignment: 0–3 (clear trend = 3, weak = 1, none = 0)
  * Liquidity confirmation: 0–3 (sweep + rejection = 3, sweep only = 2, equal levels = 1, none = 0)
  * Candle confirmation: 0–2 (confirmed = 2, forming = 1, none = 0)
  * Structure/location: 0–1 (at key level = 1, mid-range = 0)
  * Session timing: 0–1 (London/NY = 1, off-hours = 0)
  Only score >= 7 is tradeable.
- sniper_reasoning: one sentence explaining the score

Intent & Liquidity fields:
- market_intent: "reversal", "continuation", or "consolidation"
- market_intent_reasoning: one sentence explaining the intent
- liquidity_event: describe any sweep, fake breakout, or equal levels taken (empty string if none)
- equal_levels_detected: true if equal highs or equal lows are visible

Quality fields:
- quality_score: 0–100 (85+ excellent, 70–84 good, 50–69 mediocre, 30–49 poor, <30 unusable)
- quality_issues: list of problems
- pair_visible, timeframe_visible, candle_clarity, drawing_overload, zoom_acceptable, is_cropped

If chart is unreadable: readable=false, quality_score<=25, sniper_score=0.
If no setup: entry_readiness="no_setup", sniper_score = actual value (usually < 4).
""".strip()


def analyze_chart_vision(image_bytes: bytes) -> dict:
    image_b64 = base64.b64encode(image_bytes).decode("utf-8")

    last_error = None
    for attempt in range(2):
        try:
            response = client.chat.completions.create(
                model=OPENAI_MODEL,
                temperature=0.1,
                max_tokens=800,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": VISION_PROMPT},
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": "Analyze this trading chart. Score its quality. Return only JSON."
                            },
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}
                            }
                        ]
                    }
                ],
            )
            content = response.choices[0].message.content
            result  = json.loads(content)
            validated = _validate_vision(result)
            print(f"  [Vision] quality={validated['quality_score']} "
                  f"sniper={validated['sniper_score']}/10 "
                  f"readiness={validated['entry_readiness']} "
                  f"location={validated['price_location']} "
                  f"confirm={validated['confirmation_candle']}",
                  flush=True)
            if validated.get("quality_issues"):
                print(f"  [Vision] quality issues: {validated['quality_issues']}", flush=True)
            return validated
        except Exception as e:
            last_error = e
            if attempt < 1:
                time.sleep(0.8)

    print(f"  [Vision] FAILED: {last_error}", flush=True)
    return _failed_vision(str(last_error))


def get_quality_confidence_adjustment(vision: dict) -> int:
    """
    Returns a confidence point adjustment based on chart quality.
    Applied on top of the normal decision engine scoring.

      quality_score 85+ → +0  (no change; engine runs normally)
      quality_score 70–84 → -5  (minor penalty)
      quality_score 50–69 → -12 (moderate penalty)
      quality_score 30–49 → -20 (serious penalty, borderline reject)
      quality_score <30   → force reject (handled by decision engine)
    """
    qs = vision.get("quality_score", 80)
    if qs >= 85:
        return 0
    if qs >= 70:
        return -5
    if qs >= 50:
        return -12
    if qs >= 30:
        return -20
    return -40   # effective reject


def is_chart_quality_acceptable(vision: dict) -> tuple:
    """
    Returns (acceptable: bool, rejection_reason: str | None).
    Charts with quality_score < 30 are hard-rejected.
    """
    qs     = vision.get("quality_score", 80)
    issues = vision.get("quality_issues", [])

    if qs < 30:
        issue_txt = "; ".join(issues[:3]) if issues else "chart quality too poor"
        return False, f"Chart quality score {qs}/100 — {issue_txt}."

    if not vision.get("pair_visible", True) and vision.get("pair", "UNKNOWN") == "UNKNOWN":
        return False, "Pair not visible on chart. Please include the symbol label."

    if vision.get("drawing_overload", False) and qs < 50:
        return False, "Too many indicators/drawings obscure price. Please clean up the chart."

    if vision.get("is_cropped", False) and qs < 45:
        return False, "Screenshot appears badly cropped. Please capture the full chart including price and time axes."

    return True, None


# ── Internal helpers ──────────────────────────────────────────────────────────

def _validate_vision(raw: dict) -> dict:
    return {
        "readable":           bool(raw.get("readable", False)),
        "pair":               str(raw.get("pair", "UNKNOWN")).strip().upper().replace("/", ""),
        "timeframe":          str(raw.get("timeframe", "UNKNOWN")).strip().upper(),
        "visible_price":      _safe_float(raw.get("visible_price")),
        "trend_bias":         str(raw.get("trend_bias", "neutral")).strip().lower(),
        "structure_summary":  str(raw.get("structure_summary", "")),
        "clean_chart":        bool(raw.get("clean_chart", False)),
        "key_levels":         _safe_float_list(raw.get("key_levels", [])),
        "support_zone":       _safe_float(raw.get("support_zone")),
        "resistance_zone":    _safe_float(raw.get("resistance_zone")),
        "notes":              str(raw.get("notes", "")),
        # Sniper analysis fields
        "price_location":     str(raw.get("price_location", "mid_range")).strip().lower(),
        "entry_readiness":    str(raw.get("entry_readiness", "no_setup")).strip().lower(),
        "confirmation_candle": bool(raw.get("confirmation_candle", False)),
        "sniper_score":       min(10, max(0, int(raw.get("sniper_score", 0) or 0))),
        "sniper_reasoning":   str(raw.get("sniper_reasoning", "")),
        # Market intent & liquidity fields
        "market_intent":      str(raw.get("market_intent", "")).strip().lower(),
        "market_intent_reasoning": str(raw.get("market_intent_reasoning", "")),
        "liquidity_event":    str(raw.get("liquidity_event", "")),
        "equal_levels_detected": bool(raw.get("equal_levels_detected", False)),
        # Quality fields
        "quality_score":      int(raw.get("quality_score", 70) or 70),
        "quality_issues":     list(raw.get("quality_issues", [])),
        "pair_visible":       bool(raw.get("pair_visible", True)),
        "timeframe_visible":  bool(raw.get("timeframe_visible", True)),
        "candle_clarity":     str(raw.get("candle_clarity", "good")).lower(),
        "drawing_overload":   bool(raw.get("drawing_overload", False)),
        "zoom_acceptable":    bool(raw.get("zoom_acceptable", True)),
        "is_cropped":         bool(raw.get("is_cropped", False)),
    }


def _failed_vision(error_msg: str) -> dict:
    return {
        "readable":          False,
        "pair":              "UNKNOWN",
        "timeframe":         "UNKNOWN",
        "visible_price":     None,
        "trend_bias":        "neutral",
        "structure_summary": f"Vision analysis failed: {error_msg}",
        "clean_chart":       False,
        "key_levels":        [],
        "support_zone":      None,
        "resistance_zone":   None,
        "notes":             "",
        "price_location":    "mid_range",
        "entry_readiness":   "no_setup",
        "confirmation_candle": False,
        "sniper_score":      0,
        "sniper_reasoning":  "",
        "market_intent":     "",
        "market_intent_reasoning": "",
        "liquidity_event":   "",
        "equal_levels_detected": False,
        "quality_score":     0,
        "quality_issues":    ["vision API failure"],
        "pair_visible":      False,
        "timeframe_visible": False,
        "candle_clarity":    "poor",
        "drawing_overload":  False,
        "zoom_acceptable":   False,
        "is_cropped":        False,
    }


def _safe_float(val):
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _safe_float_list(lst):
    if not isinstance(lst, list):
        return []
    result = []
    for v in lst:
        f = _safe_float(v)
        if f is not None:
            result.append(f)
    return result
