"""
Decision engine — combines vision, live data, HTF confirmation,
session context, symbol-specific rules, and structure signals into
a single trade decision.

Confidence tiers:
  35–49  → hard abort
  50–64  → weak / no signal
  65–74  → okay
  75–84  → good
  85–91  → strong
  92     → elite cap (requires every factor aligned)
"""

import re

from config import TUNING_OVERRIDES, VOLATILITY_HARD_ABORT
from market_data import (
    has_market_data, get_quote, get_candles,
    timeframe_to_interval, normalize_symbol,
    get_higher_timeframe, get_higher_timeframe_context,
)
from indicators import compute_market_context, estimate_atr
from news_data import get_relevant_news_block
from sessions import session_confidence_bonus, get_session_label
from vision import get_quality_confidence_adjustment, is_chart_quality_acceptable
from structure_signals import compute_structure_signals


def _w(key: str, default: int) -> int:
    """Return the tuning-override value for a weight key, or the default."""
    return TUNING_OVERRIDES.get(key, default)


# ── Per-symbol rules ──────────────────────────────────────────────────────────

SYMBOL_RULES = {
    "EURUSD":  {"min_atr": 0.0003, "min_rr": 1.5, "news_sensitive": True,  "stop_buffer_mult": 1.5},
    "GBPUSD":  {"min_atr": 0.0004, "min_rr": 1.5, "news_sensitive": True,  "stop_buffer_mult": 1.6},
    "USDJPY":  {"min_atr": 0.10,   "min_rr": 1.5, "news_sensitive": True,  "stop_buffer_mult": 1.5},
    "AUDUSD":  {"min_atr": 0.0003, "min_rr": 1.5, "news_sensitive": False, "stop_buffer_mult": 1.5},
    "USDCAD":  {"min_atr": 0.0003, "min_rr": 1.5, "news_sensitive": True,  "stop_buffer_mult": 1.5},
    "NZDUSD":  {"min_atr": 0.0003, "min_rr": 1.5, "news_sensitive": False, "stop_buffer_mult": 1.5},
    "USDCHF":  {"min_atr": 0.0003, "min_rr": 1.5, "news_sensitive": False, "stop_buffer_mult": 1.5},
    "GBPJPY":  {"min_atr": 0.15,   "min_rr": 1.8, "news_sensitive": True,  "stop_buffer_mult": 1.8},
    "EURJPY":  {"min_atr": 0.12,   "min_rr": 1.5, "news_sensitive": True,  "stop_buffer_mult": 1.6},
    "EURGBP":  {"min_atr": 0.0002, "min_rr": 1.5, "news_sensitive": True,  "stop_buffer_mult": 1.5},
    "XAUUSD":  {"min_atr": 1.0,    "min_rr": 1.8, "news_sensitive": True,  "stop_buffer_mult": 2.0},
    "XAGUSD":  {"min_atr": 0.05,   "min_rr": 1.8, "news_sensitive": True,  "stop_buffer_mult": 2.0},
    "NAS100":  {"min_atr": 10.0,   "min_rr": 1.8, "news_sensitive": True,  "stop_buffer_mult": 1.8},
    "US30":    {"min_atr": 30.0,   "min_rr": 1.8, "news_sensitive": True,  "stop_buffer_mult": 1.8},
}


def _categorise_rejection(reason: str) -> str:
    r = reason.lower()
    if "quality" in r or "quality too low" in r:
        return "low_quality"
    if "unreadable" in r or "unclear" in r:
        return "unreadable_chart"
    if "pair" in r and ("identify" in r or "unknown" in r):
        return "pair_unknown"
    if "range" in r and "regime" in r:
        return "range_regime"
    if "ranging" in r:
        return "range_regime"
    if "mixed" in r and "regime" in r:
        return "mixed_regime"
    if "news" in r or "release" in r:
        return "news_risk"
    if "higher timeframe" in r or "htf" in r or "opposes" in r:
        return "htf_conflict"
    if "risk/reward" in r or "rr" in r or "reward" in r:
        return "poor_rr"
    if "session" in r or "dead hours" in r:
        return "weak_session"
    if "confidence too low" in r or "confluence" in r:
        return "low_confluence"
    if "price" in r and "cannot" in r:
        return "no_price_data"
    if "bias" in r or "directional" in r:
        return "no_bias"
    if "messy" in r or "no clear" in r:
        return "no_bias"
    if "volatility" in r and ("dead" in r or "chaotic" in r):
        return "volatility_blocked"
    if "atr spike" in r or "atr_spike" in r:
        return "atr_spike"
    if "smc" in r or "no_smc" in r or "sweep" in r or "order block" in r:
        return "no_smc_setup"
    if "mtf conflict" in r or "mtf_conflict" in r:
        return "htf_conflict"
    if "no_pullback" in r or "no pullback" in r:
        return "no_pullback"
    if "overextended" in r:
        return "overextended"
    return "other"


# ── Main entry point ──────────────────────────────────────────────────────────

def run_decision(vision: dict, pair: str = None, timeframe: str = None) -> dict:
    pair      = pair or vision.get("pair", "UNKNOWN")
    timeframe = timeframe or vision.get("timeframe", "UNKNOWN")
    pair      = pair.upper().replace("/", "")

    print(f"\n{'='*50}", flush=True)
    print(f"[Engine] Starting decision for {pair} {timeframe}", flush=True)

    # ── Hard filter: unreadable chart ─────────────────────────────────────────
    if not vision.get("readable", False):
        return _no_signal(pair, timeframe, "Chart is unreadable or too noisy to analyze.",
                          rejection_category="unreadable_chart", log_rejection=True)

    if pair == "UNKNOWN":
        return _no_signal(pair, timeframe, "Could not identify the trading pair from the chart.",
                          rejection_category="pair_unknown", log_rejection=True)

    if not vision.get("clean_chart", False) and vision.get("trend_bias") == "neutral":
        return _no_signal(pair, timeframe, "Chart is messy with no clear directional bias.",
                          rejection_category="no_bias", log_rejection=(pair != "UNKNOWN"))

    # ── Hard filter: chart quality score ──────────────────────────────────────
    quality_ok, quality_reason = is_chart_quality_acceptable(vision)
    if not quality_ok:
        return _no_signal(pair, timeframe, f"Chart quality too low — {quality_reason}",
                          rejection_category="low_quality", log_rejection=True)

    quality_adjustment = get_quality_confidence_adjustment(vision)
    qs = vision.get("quality_score", 80)
    print(f"[Engine] Chart quality score={qs} confidence_adj={quality_adjustment}", flush=True)

    # ── Sniper mode filters ────────────────────────────────────────────────────
    sniper_score    = vision.get("sniper_score", 0)
    entry_readiness = vision.get("entry_readiness", "no_setup")
    price_location  = vision.get("price_location", "mid_range")
    sniper_reason   = vision.get("sniper_reasoning", "")

    print(f"[Engine] Sniper — score={sniper_score}/10 readiness={entry_readiness} "
          f"location={price_location}", flush=True)

    if entry_readiness == "no_setup":
        return _no_signal(pair, timeframe,
                          "NO TRADE — no valid setup on this chart. Wait for price to reach a key level.",
                          rejection_category="sniper_no_setup", log_rejection=True)

    if price_location == "mid_range":
        return _no_signal(pair, timeframe,
                          "NO TRADE — price is in the middle of a move. Wait for pullback to support/demand "
                          "or breakout of resistance/supply.",
                          rejection_category="sniper_mid_range", log_rejection=True)

    if sniper_score < 7:
        return _no_signal(pair, timeframe,
                          f"Setup too weak (sniper score {sniper_score}/10). "
                          f"{sniper_reason or 'Wait for a cleaner setup.'}",
                          rejection_category="sniper_low_score", log_rejection=True)

    if entry_readiness == "awaiting_confirmation" and not vision.get("confirmation_candle", False):
        return _no_signal(pair, timeframe,
                          "Setup forming but NO confirmation candle yet. "
                          "Wait for a rejection wick, engulfing, or pin bar before entering.",
                          rejection_category="sniper_no_confirmation", log_rejection=True)

    gpt_intent = vision.get("market_intent", "").lower()
    if gpt_intent == "consolidation":
        return _no_signal(pair, timeframe,
                          "NO TRADE — price is consolidating with no clear market intent. "
                          "Wait for a breakout or liquidity sweep.",
                          rejection_category="sniper_consolidation", log_rejection=True)

    # ── Step 1: News check ─────────────────────────────────────────────────────
    news_block = get_relevant_news_block(pair)
    news_risk  = news_block.get("risk", "low")
    print(f"[Engine] News risk: {news_risk.upper()}", flush=True)

    if news_risk == "high":
        return _no_signal(pair, timeframe, news_block["message"], news_risk=news_risk,
                          rejection_category="news_risk", log_rejection=True)

    if news_risk == "medium" and news_block.get("minutes_away", 999) <= 15:
        return _no_signal(pair, timeframe,
                          f"News release in ~{news_block.get('minutes_away',15)} min — avoiding entry.",
                          news_risk=news_risk,
                          rejection_category="news_risk", log_rejection=True)

    # ── Step 2: Live market data ───────────────────────────────────────────────
    live_price = None
    market_ctx = None
    htf_ctx    = None
    candles    = []

    if has_market_data():
        quote = get_quote(pair)
        if quote:
            live_price = quote.get("price")

        interval = timeframe_to_interval(timeframe)
        candles  = get_candles(pair, interval, outputsize=100)
        if len(candles) >= 20:
            market_ctx = compute_market_context(candles)
            print(f"[Engine] Market context — bias={market_ctx['trend_bias']} "
                  f"regime={market_ctx['regime']} slope={market_ctx['ema_slope']} "
                  f"atr={market_ctx['atr']}", flush=True)

        htf_ctx = get_higher_timeframe_context(pair, timeframe)
    else:
        print("[Engine] No Twelve Data key — skipping live data.", flush=True)

    # ── Step 3: Bias combination + HTF check ───────────────────────────────────
    vision_bias   = vision.get("trend_bias", "neutral")
    support       = vision.get("support_zone")
    resistance    = vision.get("resistance_zone")
    visible_price = vision.get("visible_price")
    current_price = live_price or visible_price

    if not current_price:
        return _no_signal(pair, timeframe, "Cannot determine current price.",
                          rejection_category="no_price_data", log_rejection=True)

    combined_bias, bias_conflict = _combine_bias(vision_bias, market_ctx)
    live_bias = market_ctx.get("trend_bias", "N/A") if market_ctx else "N/A"
    print(f"[Engine] Bias — vision={vision_bias} live={live_bias} "
          f"combined={combined_bias} conflict={bias_conflict}", flush=True)

    if combined_bias == "neutral":
        return _no_signal(pair, timeframe, "No clear directional bias from chart or live data.",
                          news_risk=news_risk,
                          rejection_category="no_bias", log_rejection=True)

    htf_bias     = htf_ctx.get("trend_bias", "neutral") if htf_ctx else "neutral"
    htf_conflict = False
    if htf_bias != "neutral" and htf_bias != combined_bias:
        htf_conflict = True
        print(f"[Engine] HTF CONFLICT: LTF={combined_bias} HTF={htf_bias}", flush=True)

    if htf_conflict and market_ctx and market_ctx.get("regime") in ("range", "mixed"):
        return _no_signal(pair, timeframe,
                          f"Higher timeframe ({htf_ctx.get('timeframe','HTF')}) "
                          f"opposes this setup in a choppy market.",
                          news_risk=news_risk,
                          rejection_category="htf_conflict", log_rejection=True)

    if market_ctx and market_ctx.get("regime") == "range":
        return _no_signal(pair, timeframe,
                          "Market is ranging — no directional trade. Wait for a breakout.",
                          news_risk=news_risk,
                          rejection_category="range_regime", log_rejection=True)

    direction = "BUY" if combined_bias == "bullish" else "SELL"
    print(f"[Engine] Direction: {direction}", flush=True)

    # ── Step 3b: Pullback / anti-FOMO gate (live candles required) ────────────
    pb_data = market_ctx.get("pullback_data") if market_ctx else None
    if pb_data:
        if pb_data["is_overextended"]:
            print(f"[Engine] ANTI-FOMO: price overextended "
                  f"{pb_data['ema_distance_atr']:.2f} ATR from EMA — no entry", flush=True)
            return _no_signal(
                pair, timeframe,
                f"Price is overextended {pb_data['ema_distance_atr']:.1f}× ATR from EMA — "
                f"buying tops / selling bottoms. Wait for a pullback.",
                news_risk=news_risk,
                rejection_category="overextended",
                log_rejection=True,
            )
        if not pb_data["is_pullback"]:
            print(f"[Engine] ANTI-FOMO: no pullback detected — "
                  f"EMA distance {pb_data['ema_distance_atr']:.2f} ATR — holding", flush=True)
            return _no_signal(
                pair, timeframe,
                f"No pullback detected — setup is forming but price has not yet "
                f"retraced into the entry zone. Watch for a pullback to the EMA.",
                news_risk=news_risk,
                rejection_category="no_pullback",
                log_rejection=True,
            )
        print(f"[Engine] Pullback confirmed — depth={pb_data['pullback_depth']} ATR "
              f"zone={pb_data['zone']}", flush=True)

    # ── Step 4: Trade levels ───────────────────────────────────────────────────
    entry, sl, tp, rr = _compute_levels(
        direction, current_price, support, resistance, market_ctx, pair,
        pb_data=pb_data,
    )

    if entry is None:
        return _no_signal(pair, timeframe, "Could not compute valid trade levels.",
                          news_risk=news_risk,
                          rejection_category="no_price_data", log_rejection=True)

    # ── Resistance / support proximity guard ────────────────────────────────
    atr_prox = (market_ctx.get("atr", 0) if market_ctx else 0) or estimate_atr(normalize_symbol(pair), current_price)
    if direction == "BUY" and resistance and resistance > 0:
        dist_to_res = resistance - entry
        if 0 < dist_to_res < atr_prox * 0.5:
            return _no_signal(pair, timeframe,
                              f"Entry too close to resistance ({resistance}) — wait for breakout or pullback.",
                              news_risk=news_risk,
                              rejection_category="near_resistance", log_rejection=True)
    if direction == "SELL" and support and support > 0:
        dist_to_sup = entry - support
        if 0 < dist_to_sup < atr_prox * 0.5:
            return _no_signal(pair, timeframe,
                              f"Entry too close to support ({support}) — wait for breakdown or pullback.",
                              news_risk=news_risk,
                              rejection_category="near_support", log_rejection=True)

    rules  = SYMBOL_RULES.get(normalize_symbol(pair), {})
    min_rr = rules.get("min_rr", 1.5)
    if rr < min_rr:
        return _no_signal(pair, timeframe,
                          f"Risk/reward {rr:.1f} is below the minimum ({min_rr}) for this pair.",
                          news_risk=news_risk,
                          rejection_category="poor_rr", log_rejection=True)

    # ── Partial TP1 at 1R ───────────────────────────────────────────────────
    risk_amt = abs(entry - sl)
    tp1 = round(entry + risk_amt, 5) if direction == "BUY" else round(entry - risk_amt, 5)

    print(f"[Engine] Levels — entry={entry:.5f} sl={sl:.5f} tp1={tp1:.5f} tp={tp:.5f} rr={rr:.2f}", flush=True)

    # ── Step 5: Structure signals (Phase 2) ────────────────────────────────────
    atr          = market_ctx.get("atr", 0) if market_ctx else 0
    expected_atr = estimate_atr(pair, current_price)
    if atr <= 0:
        atr = expected_atr

    struct_signals = None
    if candles and len(candles) >= 10:
        struct_signals = compute_structure_signals(
            candles, direction, current_price, pair, atr, expected_atr
        )
        # Hard abort for truly dead or chaotic markets (configurable)
        if VOLATILITY_HARD_ABORT:
            vol_label = struct_signals["volatility"]["label"]
            if vol_label in ("dead", "chaotic"):
                vol_desc = struct_signals["volatility"]["description"]
                return _no_signal(
                    pair, timeframe,
                    f"Volatility blocked entry — {vol_desc}",
                    news_risk=news_risk,
                    rejection_category="volatility_blocked",
                    log_rejection=True,
                )
        print(f"[Engine] Structure signals: "
              f"sweep={struct_signals['sweep']['detected']} "
              f"fvg={struct_signals['fvg']['detected']} "
              f"ob={struct_signals['order_block']['detected']} "
              f"vol={struct_signals['volatility']['label']} "
              f"intent={struct_signals['market_intent']['intent']} "
              f"total_bonus={struct_signals['total_bonus']}", flush=True)

        live_intent = struct_signals.get("market_intent", {}).get("intent", "")
        if live_intent == "consolidation":
            return _no_signal(
                pair, timeframe,
                "NO TRADE — live data shows price is consolidating with no clear intent. "
                "Wait for a breakout or liquidity sweep.",
                news_risk=news_risk,
                rejection_category="live_consolidation",
                log_rejection=True,
            )

    # ── Step 6: Score ──────────────────────────────────────────────────────────
    confidence, score_breakdown = _compute_confidence(
        vision, market_ctx, htf_ctx, news_risk,
        combined_bias, rr, bias_conflict, htf_conflict, pair,
        quality_adjustment=quality_adjustment,
        struct_signals=struct_signals,
        pb_data=pb_data,
    )

    if confidence < 50:
        return _no_signal(pair, timeframe,
                          f"Setup confidence too low ({confidence}%). Waiting for cleaner conditions.",
                          news_risk=news_risk,
                          rejection_category="low_confluence", log_rejection=True)

    regime  = _determine_regime(market_ctx, vision)
    quality = _determine_quality(confidence, rr, vision.get("clean_chart", False))
    reason  = _build_reason(
        direction, combined_bias, regime, news_risk, vision,
        bias_conflict, htf_conflict, htf_bias, htf_ctx, pair,
        struct_signals=struct_signals,
    )
    score_explanation = _build_score_explanation(score_breakdown)

    print(f"[Engine] SIGNAL: {direction} {pair} conf={confidence}% quality={quality}", flush=True)
    print(f"[Engine] Score breakdown: {score_breakdown}", flush=True)
    print(f"{'='*50}\n", flush=True)

    return {
        "no_signal":         False,
        "pair":              pair,
        "timeframe":         timeframe,
        "direction":         direction,
        "entry":             round(entry, 5),
        "stop_loss":         round(sl, 5),
        "tp1":               tp1,
        "take_profit":       round(tp, 5),
        "rr":                round(rr, 2),
        "confidence":        confidence,
        "reason":            reason,
        "score_explanation": score_explanation,
        "market_regime":     regime,
        "setup_quality":     quality,
        "news_risk":         news_risk,
        "htf_bias":          htf_bias,
        "htf_timeframe":     htf_ctx.get("timeframe", "") if htf_ctx else "",
        "score_breakdown":   score_breakdown,
        "quality_score":     vision.get("quality_score", 80),
        "quality_issues":    vision.get("quality_issues", []),
        "market_intent":     vision.get("market_intent", ""),
        "market_intent_desc": vision.get("market_intent_reasoning", ""),
        "liquidity_sweep":   vision.get("liquidity_event", ""),
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _no_signal(pair, timeframe, reason, news_risk="low",
               rejection_category: str = None, log_rejection: bool = False):
    print(f"[Engine] NO SIGNAL — {reason}", flush=True)

    if log_rejection and pair and pair != "UNKNOWN":
        try:
            from storage import save_rejection
            category = rejection_category or _categorise_rejection(reason)
            save_rejection(pair, category, reason, source="manual")
        except Exception as e:
            print(f"[Engine] Rejection log error: {e}", flush=True)

    return {
        "no_signal":         True,
        "pair":              pair,
        "timeframe":         timeframe,
        "direction":         "",
        "entry":             None,
        "stop_loss":         None,
        "take_profit":       None,
        "rr":                None,
        "confidence":        0,
        "reason":            reason,
        "score_explanation": "",
        "market_regime":     "mixed",
        "setup_quality":     "weak",
        "news_risk":         news_risk,
        "htf_bias":          "N/A",
        "htf_timeframe":     "",
    }


def _combine_bias(vision_bias: str, market_ctx: dict) -> tuple:
    if market_ctx is None:
        return vision_bias, False
    ctx_bias = market_ctx.get("trend_bias", "neutral")
    if vision_bias == ctx_bias:
        return vision_bias, False
    if vision_bias == "neutral":
        return ctx_bias, False
    if ctx_bias == "neutral":
        return vision_bias, False
    return ctx_bias, True   # conflict — live EMA wins


def _compute_levels(direction, price, support, resistance, market_ctx, pair,
                    pb_data: dict = None):
    sym = normalize_symbol(pair)
    atr = market_ctx.get("atr", 0) if market_ctx else 0
    if atr <= 0:
        atr = estimate_atr(sym, price)
    if atr <= 0:
        return None, None, None, 0

    rules      = SYMBOL_RULES.get(sym, {})
    buf        = rules.get("stop_buffer_mult", 1.5)
    ATR_SL_MIN = max(buf, 1.2)

    # ── Entry: use pullback zone when available ────────────────────────────────
    # The zone is the EMA / demand area — this avoids buying tops
    if pb_data and pb_data.get("is_pullback") and pb_data.get("zone"):
        entry = pb_data["zone"]
    else:
        entry = price

    if direction == "BUY":
        # SL: below pullback low (structure) or ATR-based floor
        if pb_data and pb_data.get("pullback_low") and pb_data["pullback_low"] > 0:
            struct_sl = pb_data["pullback_low"] - atr * 0.2
            sl = min(struct_sl, entry - atr * ATR_SL_MIN)
        else:
            sl_raw = support if support and support < entry else entry - atr * ATR_SL_MIN
            sl     = min(sl_raw, entry - atr * ATR_SL_MIN)
        # TP: structure-based — use nearest resistance/swing high, not arbitrary
        swing_high = market_ctx.get("swing_high") if market_ctx else None
        risk_est   = abs(entry - sl)
        min_tp     = entry + risk_est * 2.0
        if swing_high and swing_high > entry and swing_high >= min_tp:
            tp = swing_high
        elif resistance and resistance > entry and resistance >= min_tp:
            tp = resistance
        else:
            tp = min_tp
    else:
        # SL: above pullback high (structure)
        if pb_data and pb_data.get("pullback_high") and pb_data["pullback_high"] > 0:
            struct_sl = pb_data["pullback_high"] + atr * 0.2
            sl = max(struct_sl, entry + atr * ATR_SL_MIN)
        else:
            sl_raw = resistance if resistance and resistance > entry else entry + atr * ATR_SL_MIN
            sl     = max(sl_raw, entry + atr * ATR_SL_MIN)
        swing_low = market_ctx.get("swing_low") if market_ctx else None
        risk_est  = abs(sl - entry)
        min_tp    = entry - risk_est * 2.0
        if swing_low and 0 < swing_low < entry and swing_low <= min_tp:
            tp = swing_low
        elif support and 0 < support < entry and support <= min_tp:
            tp = support
        else:
            tp = min_tp

    risk   = abs(entry - sl)
    reward = abs(tp - entry)

    if risk <= 0:
        return None, None, None, 0

    rr = reward / risk
    if rr < 1.5:
        # Enforce 2R minimum TP
        if direction == "BUY":
            tp = entry + risk * 2.0
        else:
            tp = entry - risk * 2.0
        rr = abs(tp - entry) / risk

    return entry, sl, tp, rr


def _compute_confidence(vision, market_ctx, htf_ctx, news_risk,
                        combined_bias, rr, bias_conflict, htf_conflict, pair,
                        quality_adjustment: int = 0,
                        struct_signals: dict = None,
                        pb_data: dict = None):
    """
    Point-based confidence scoring with full component breakdown.
    Structure signals are applied as bounded bonuses/penalties.
    Returns (confidence: int, breakdown: dict).
    """
    baseline = 45
    score    = baseline
    bd = {
        "baseline":         baseline,
        "chart_quality":    0,
        "bias_alignment":   0,
        "htf_alignment":    0,
        "regime":           0,
        "momentum":         0,
        "ema_slope":        0,
        "pullback":         0,
        "rr":               0,
        "session":          0,
        "news":             0,
        "quality_penalty":  quality_adjustment,
        # Structure signal components (Phase 2)
        "liquidity_sweep":  0,
        "fvg":              0,
        "order_block":      0,
        "breaker":          0,
        "fib_confluence":   0,
        "volatility":       0,
    }

    # ── Chart quality ──────────────────────────────────────────────────────────
    cq = _w("chart_quality_clean", 6) if vision.get("clean_chart") else _w("chart_quality_dirty", -6)
    score += cq
    bd["chart_quality"] = cq

    # ── Bias alignment ─────────────────────────────────────────────────────────
    if combined_bias in ("bullish", "bearish") and not bias_conflict:
        ba = _w("bias_aligned", 8)
    elif bias_conflict:
        ba = _w("bias_conflict", -15)
    else:
        ba = 0
    score += ba
    bd["bias_alignment"] = ba

    # ── Higher timeframe alignment ─────────────────────────────────────────────
    htf_pts = 0
    if htf_ctx:
        htf_bias = htf_ctx.get("trend_bias", "neutral")
        if htf_bias == combined_bias:
            htf_pts = _w("htf_aligned", 10)
        elif htf_conflict:
            htf_pts = _w("htf_conflict", -12)
    score += htf_pts
    bd["htf_alignment"] = htf_pts

    # ── Regime ────────────────────────────────────────────────────────────────
    reg_pts = mom_pts = slope_pts = 0
    if market_ctx:
        regime = market_ctx.get("regime", "mixed")
        if regime == "trending":
            reg_pts = _w("trending", 7)
        elif regime == "pullback":
            reg_pts = _w("pullback", 9)
        elif regime == "reversal":
            reg_pts = _w("reversal", 3)
        elif regime == "range":
            reg_pts = _w("range", -12)
        elif regime == "choppy":
            reg_pts = _w("choppy", -10)
        elif regime == "mixed":
            reg_pts = _w("mixed", -8)

        if market_ctx.get("momentum") == "strong":
            mom_pts = _w("momentum_strong", 4)
        elif market_ctx.get("pullback"):
            mom_pts = _w("momentum_pullback", 3)

        slope = market_ctx.get("ema_slope", "flat")
        if (combined_bias == "bullish" and slope == "rising") or \
           (combined_bias == "bearish" and slope == "falling"):
            slope_pts = _w("ema_slope_aligned", 3)
        elif slope != "flat":
            slope_pts = _w("ema_slope_misaligned", -3)

    score += reg_pts + mom_pts + slope_pts
    bd["regime"]    = reg_pts
    bd["momentum"]  = mom_pts + slope_pts
    bd["ema_slope"] = slope_pts

    # ── Pullback quality ───────────────────────────────────────────────────────
    pb_pts = 0
    if pb_data:
        if pb_data.get("is_pullback"):
            pb_pts = _w("pullback_confirmed", 10)   # clean pullback = best entries
        elif pb_data.get("is_overextended"):
            pb_pts = _w("overextended_penalty", -10)
        else:
            pb_pts = _w("no_pullback_penalty", -15)  # trend exists but no retrace
    elif market_ctx and market_ctx.get("pullback"):
        pb_pts = _w("pullback_confirmed", 10)  # fallback from basic pullback flag
    score += pb_pts
    bd["pullback"] = pb_pts

    # ── Risk/reward ────────────────────────────────────────────────────────────
    rr_pts = 0
    if rr >= 3.0:
        rr_pts = _w("rr_3plus", 7)
    elif rr >= 2.5:
        rr_pts = _w("rr_25plus", 4)
    elif rr >= 2.0:
        rr_pts = _w("rr_2plus", 2)
    score += rr_pts
    bd["rr"] = rr_pts

    # ── Session quality ────────────────────────────────────────────────────────
    session_bonus = min(session_confidence_bonus(pair), _w("session_cap", 8))
    score += session_bonus
    bd["session"] = session_bonus

    # ── News risk ──────────────────────────────────────────────────────────────
    news_pts = 0
    if news_risk == "medium":
        news_pts = _w("news_medium", -12)
    if news_risk == "high":
        news_pts = _w("news_high", -30)
    score += news_pts
    bd["news"] = news_pts

    # ── Chart quality penalty ──────────────────────────────────────────────────
    score += quality_adjustment

    # ── Phase 2: Structure signals ─────────────────────────────────────────────
    if struct_signals:
        # Cap total structure bonus at +15 so it can't overwhelm core scoring
        struct_bonus = max(-12, min(struct_signals["total_bonus"], 15))
        score += struct_bonus
        bd["liquidity_sweep"] = struct_signals["sweep_pts"]
        bd["fvg"]             = struct_signals["fvg_pts"]
        bd["order_block"]     = struct_signals["ob_pts"]
        bd["breaker"]         = struct_signals["breaker_pts"]
        bd["fib_confluence"]  = struct_signals["fib_pts"]
        bd["volatility"]      = struct_signals["vol_pts"]

    # ── Adaptive brain penalty ─────────────────────────────────────────────────
    try:
        import adaptive_brain as _ab
        regime  = _determine_regime(market_ctx, vision)
        session = get_session_label(None)   # current session at analysis time
        brain_adj = _ab.get_confidence_adjustment(pair, regime, session)
        if brain_adj:
            score += brain_adj
            bd["brain_adjustment"] = brain_adj
    except Exception:
        pass

    bd["total"] = max(35, min(score, 90))
    return max(35, min(score, 90)), bd


def _determine_regime(market_ctx, vision):
    if market_ctx:
        return market_ctx.get("regime", "mixed")
    bias = vision.get("trend_bias", "neutral")
    if bias in ("bullish", "bearish"):
        return "trending"
    return "mixed"


def _determine_quality(confidence, rr, clean_chart):
    score = 0
    if confidence >= 85:
        score += 3
    elif confidence >= 75:
        score += 2
    elif confidence >= 65:
        score += 1
    if rr and rr >= 3.0:
        score += 2
    elif rr and rr >= 2.0:
        score += 1
    if clean_chart:
        score += 1
    if score >= 5:
        return "strong"
    elif score >= 4:
        return "good"
    elif score >= 2:
        return "okay"
    return "weak"


def _build_score_explanation(bd: dict) -> str:
    """
    Builds a concise, ordered list of the main score drivers.
    Only includes components that meaningfully contributed (+/- 3 or more).
    """
    LABELS = {
        "chart_quality":    "Chart quality",
        "bias_alignment":   "Bias alignment",
        "htf_alignment":    "HTF alignment",
        "regime":           "Market regime",
        "momentum":         "Momentum",
        "ema_slope":        "EMA slope",
        "pullback":         "Pullback quality",
        "rr":               "R:R ratio",
        "session":          "Session quality",
        "news":             "News risk",
        "quality_penalty":  "Chart quality penalty",
        "liquidity_sweep":  "Liquidity sweep",
        "fvg":              "Fair value gap",
        "order_block":      "Order block",
        "breaker":          "Structure breaker",
        "fib_confluence":   "Fib confluence",
        "volatility":       "Volatility",
    }
    parts = []
    for key, label in LABELS.items():
        v = bd.get(key, 0)
        if v is None or v == 0:
            continue
        if abs(v) < 3:
            continue    # skip tiny contributions
        sign = "+" if v > 0 else ""
        parts.append(f"{label} ({sign}{v})")

    if not parts:
        return ""
    return "Score drivers: " + " | ".join(parts)


def _build_reason(direction, bias, regime, news_risk, vision,
                  bias_conflict, htf_conflict, htf_bias, htf_ctx, pair,
                  struct_signals=None):
    """
    Builds a human-readable narrative for the trade.
    Incorporates structure signal context where meaningful.
    """
    parts = []

    # Main directional sentence
    if bias_conflict:
        parts.append(f"Live EMA drives {direction} — chart bias differs, reduced conviction.")
    elif htf_ctx and htf_bias == bias and htf_bias not in ("neutral", "N/A", ""):
        htf_tf = htf_ctx.get("timeframe", "HTF")
        parts.append(f"{direction} confirmed on both chart timeframe and {htf_tf}.")
    else:
        parts.append(f"{direction} bias from chart structure and live market data.")

    # Regime context
    if regime == "trending":
        parts.append("Strong trend in play — continuation setup.")
    elif regime == "pullback":
        parts.append("Clean pullback into trend — high probability entry.")
    elif regime == "reversal":
        parts.append("Potential reversal structure forming.")

    # Structure signal narrative (most meaningful finding only)
    if struct_signals and struct_signals.get("narrative_parts"):
        # Pick the top signal (sweep > OB > FVG > breaker > fib)
        for sig_desc in struct_signals["narrative_parts"][:1]:
            parts.append(sig_desc)

    # HTF conflict or news warning
    if htf_conflict and htf_ctx:
        htf_tf = htf_ctx.get("timeframe", "HTF")
        parts.append(f"Note: {htf_tf} is {htf_bias} — confidence reduced.")
    if news_risk == "medium":
        parts.append("Moderate news risk — reduce size.")

    # Vision notes (if clean and non-conflicting)
    notes = vision.get("notes", "")
    if notes and len(notes) < 80 and not bias_conflict and not htf_conflict:
        parts.append(notes)

    return " ".join(parts[:3])
