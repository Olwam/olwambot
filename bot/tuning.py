"""
Score tuning recommendation engine.

Reads stored alert history, outcomes, score breakdowns, and rejection logs
to generate evidence-based recommendations for the scoring engine.

Rules:
- Recommendations are based only on actual stored results.
- Nothing is automatically changed; everything is advisory.
- Minimum sample sizes enforced to avoid noisy conclusions.
- All output is explainable in plain English.

Used by admin tuning commands:
  /tuningsuggestions   — per-component recommendations
  /tuningconfidence    — confidence band analysis
  /tuningregimes       — regime bonus recommendations
  /tuningpairsessions  — pair + session combo recommendations
  /tuningthresholds    — watch/entry threshold recommendations
  /tuningfilters       — rejection filter tightness analysis
  /tuningsummary       — high-level overview
"""

from storage import load_data, get_rejections

# ── Sample size thresholds ─────────────────────────────────────────────────────
MIN_SAMPLE    = 8    # below this → "insufficient data"
MEDIUM_SAMPLE = 20   # below this → "low" confidence
HIGH_SAMPLE   = 40   # at or above → "high" confidence

# ── Current default engine weights (manual path) ──────────────────────────────
# Used for delta display in recommendations — not used for live scoring.
DEFAULT_WEIGHTS = {
    "baseline_manual":      45,
    "baseline_scanner":     42,
    "htf_aligned":          10,
    "htf_conflict":         -12,
    "pullback":             9,
    "trending":             7,
    "reversal":             3,
    "range":               -12,
    "mixed":               -8,
    "ema_slope_aligned":    3,
    "ema_slope_misaligned": -3,
    "momentum_strong":      4,
    "momentum_pullback":    3,
    "rr_3plus":             7,
    "rr_25plus":            4,
    "rr_2plus":             2,
    "session_cap":          8,
    "news_medium":         -12,
    "news_high":           -30,
    "bias_aligned":         8,
    "bias_conflict":       -15,
    "chart_quality_clean":  6,
    "chart_quality_dirty": -6,
}

# Components exposed for per-component drill-down
SCORE_COMPONENTS = [
    "htf_alignment", "regime", "session",
    "rr", "ema_slope", "momentum",
    "bias_alignment", "chart_quality", "news",
    "quality_penalty",
]


# ── Data helpers ───────────────────────────────────────────────────────────────

def _resolved_alerts(limit: int = 2000) -> list:
    data   = load_data()
    alerts = data.get("scanner_alerts", [])[-limit:]
    return [a for a in alerts if a.get("outcome") in ("win", "loss")]


def _expectancy(wins: int, total: int, avg_win_rr: float) -> float:
    if total == 0:
        return 0.0
    wr = wins / total
    return round(wr * avg_win_rr - (1 - wr) * 1.0, 3)


def _avg_rr(alerts_list: list) -> float:
    win_rrs = [a.get("rr", 1.0) for a in alerts_list
               if a.get("outcome") == "win" and a.get("rr")]
    return round(sum(win_rrs) / len(win_rrs), 2) if win_rrs else 1.0


def _group_stats(group: list) -> dict:
    total  = len(group)
    wins   = sum(1 for a in group if a.get("outcome") == "win")
    losses = total - wins
    wr     = round((wins / total) * 100) if total else 0
    arr    = _avg_rr(group)
    exp    = _expectancy(wins, total, arr)
    return {"total": total, "wins": wins, "losses": losses,
            "win_rate": wr, "avg_rr": arr, "expectancy": exp}


def _confidence_label(n: int) -> str:
    if n >= HIGH_SAMPLE:
        return "high"
    elif n >= MEDIUM_SAMPLE:
        return "medium"
    return "low"


def _bucket_label(conf: int) -> str:
    if conf < 70: return "65–69"
    elif conf < 75: return "70–74"
    elif conf < 80: return "75–79"
    elif conf < 85: return "80–84"
    else:           return "85+"


def _session_short(label: str) -> str:
    if not label: return "Unknown"
    if "Overlap" in label or "overlap" in label: return "Overlap"
    return label.strip().split()[0]


# ── Core recommendation logic ──────────────────────────────────────────────────

def _recommend_component(pos_stats: dict, neu_stats: dict, current_weight: int) -> tuple:
    """
    Compare positive-contribution group vs neutral group.
    Returns (action, confidence_label, reason_str, recommended_weight).

    action: 'increase' | 'keep' | 'reduce' | 'disable'
    """
    n   = pos_stats["total"]
    neu = neu_stats["total"]

    if n < MIN_SAMPLE:
        return (
            "keep",
            "low",
            f"Only {n} resolved alerts with this component active — insufficient data to recommend changes.",
            current_weight,
        )

    wr_diff  = pos_stats["win_rate"] - neu_stats["win_rate"]
    exp_diff = pos_stats["expectancy"] - neu_stats["expectancy"]
    conf     = _confidence_label(n)

    rec_weight = current_weight

    if pos_stats["expectancy"] < -0.1 and n >= MEDIUM_SAMPLE:
        return (
            "disable",
            conf,
            (f"Expectancy is negative ({pos_stats['expectancy']:+.3f}R) when component is active "
             f"across {n} resolved alerts. Consider removing or hard-penalising."),
            0,
        )

    if wr_diff > 12 and exp_diff > 0.25:
        rec_weight = current_weight + 2
        return (
            "increase",
            conf,
            (f"Win rate is {wr_diff:+.0f}pp higher and expectancy {exp_diff:+.2f}R better "
             f"than setups without this component ({n} samples). Strong evidence of edge."),
            rec_weight,
        )

    if wr_diff > 6 or exp_diff > 0.10:
        return (
            "keep",
            conf,
            (f"Moderate improvement: WR {wr_diff:+.0f}pp, expectancy {exp_diff:+.2f}R "
             f"vs neutral group ({n} samples). Useful — keep current weight."),
            current_weight,
        )

    if -6 <= wr_diff <= 6 and abs(exp_diff) <= 0.10:
        return (
            "keep",
            conf,
            (f"Near-neutral impact: WR diff {wr_diff:+.0f}pp, expectancy diff {exp_diff:+.2f}R "
             f"({n} samples). Component does not hurt; marginal benefit — keep."),
            current_weight,
        )

    if wr_diff < -6 or exp_diff < -0.10:
        rec_weight = max(0, current_weight - 2)
        return (
            "reduce",
            conf,
            (f"Positive contribution linked to WR {wr_diff:+.0f}pp and expectancy {exp_diff:+.2f}R "
             f"vs setups without it ({n} samples). May be overvalued — consider reducing."),
            rec_weight,
        )

    return (
        "keep",
        "low",
        f"Mixed signals across {n} samples. Keep and re-evaluate as more data accumulates.",
        current_weight,
    )


# ── 1. Component tuning suggestions ───────────────────────────────────────────

_COMPONENT_WEIGHTS = {
    "htf_alignment":  10,
    "regime":          9,   # pullback (best case)
    "session":         8,
    "rr":              7,   # 3R+ (best case)
    "ema_slope":       3,
    "momentum":        4,
    "bias_alignment":  8,
    "chart_quality":   6,
    "news":           -12,  # medium risk penalty
    "quality_penalty": 0,
}


def get_component_tuning(component: str = None) -> list:
    """
    Returns a list of tuning recommendation dicts for each component.
    If component is specified, filters to that single component.
    """
    resolved = _resolved_alerts()
    if not resolved:
        return []

    components = [component] if component else SCORE_COMPONENTS
    recommendations = []

    for comp in components:
        positive = [a for a in resolved
                    if a.get("score_breakdown", {}).get(comp, 0) > 0]
        neutral  = [a for a in resolved
                    if a.get("score_breakdown", {}).get(comp, 0) == 0]

        if not positive:
            continue

        pos_stats = _group_stats(positive)
        neu_stats = _group_stats(neutral) if neutral else {"total": 0, "wins": 0,
                                                           "losses": 0, "win_rate": 0,
                                                           "avg_rr": 1.0, "expectancy": 0.0}

        cw  = _COMPONENT_WEIGHTS.get(comp, 4)
        action, conf, reason, rec_weight = _recommend_component(pos_stats, neu_stats, cw)

        recommendations.append({
            "component":          comp,
            "current_weight":     cw,
            "recommended_weight": rec_weight,
            "action":             action,
            "confidence":         conf,
            "reason":             reason,
            "sample_size":        pos_stats["total"],
            "pos_win_rate":       pos_stats["win_rate"],
            "pos_expectancy":     pos_stats["expectancy"],
            "neu_win_rate":       neu_stats["win_rate"],
            "neu_expectancy":     neu_stats["expectancy"],
        })

    return recommendations


# ── 2. Confidence bucket tuning ────────────────────────────────────────────────

def get_confidence_tuning() -> dict:
    """
    Analyses each confidence band and recommends threshold adjustments.
    Returns dict with per-band stats and a threshold recommendation.
    """
    from config import ALERT_MIN_CONFIDENCE, WATCH_ALERT_MIN_CONFIDENCE

    resolved = _resolved_alerts()
    if not resolved:
        return {"bands": {}, "threshold_recommendation": "Insufficient data."}

    bands = {}
    for label in ["65–69", "70–74", "75–79", "80–84", "85+"]:
        bands[label] = []

    for a in resolved:
        bands[_bucket_label(a.get("confidence", 0))].append(a)

    band_stats = {}
    for label, group in bands.items():
        if group:
            band_stats[label] = _group_stats(group)

    # Threshold recommendation
    recs = []
    below_entry = []
    for label in ["65–69", "70–74"]:
        s = band_stats.get(label)
        if s and s["total"] >= MIN_SAMPLE:
            below_entry.append((label, s))

    above_entry = []
    for label in ["75–79", "80–84", "85+"]:
        s = band_stats.get(label)
        if s and s["total"] >= MIN_SAMPLE:
            above_entry.append((label, s))

    threshold_rec = ""
    current_entry = ALERT_MIN_CONFIDENCE
    current_watch = WATCH_ALERT_MIN_CONFIDENCE

    # Check if low bands are performing poorly
    low_underperformers = [
        (l, s) for (l, s) in below_entry
        if s["expectancy"] < -0.05
    ]
    if low_underperformers and len(low_underperformers) == len(below_entry):
        threshold_rec = (
            f"Low confidence bands ({', '.join(l for l, _ in low_underperformers)}) "
            f"all show negative expectancy. Consider raising entry threshold from "
            f"{current_entry}% to {min(current_entry + 3, 78)}%."
        )
    elif above_entry:
        best_band = max(above_entry, key=lambda x: x[1]["expectancy"])
        if best_band[1]["expectancy"] > 0.1 and best_band[1]["total"] >= MEDIUM_SAMPLE:
            threshold_rec = (
                f"Best performing band is {best_band[0]} (E={best_band[1]['expectancy']:+.3f}R). "
                f"Current entry threshold of {current_entry}% appears well-placed."
            )
        else:
            threshold_rec = (
                f"Insufficient resolved data in upper bands to make a confident threshold recommendation. "
                f"Keep entry threshold at {current_entry}% and re-evaluate after more data."
            )
    else:
        threshold_rec = f"Keep entry threshold at {current_entry}% — not enough data in upper bands yet."

    return {
        "bands":                    band_stats,
        "current_entry_threshold":  current_entry,
        "current_watch_threshold":  current_watch,
        "threshold_recommendation": threshold_rec,
    }


# ── 3. Regime tuning ──────────────────────────────────────────────────────────

_REGIME_WEIGHTS = {
    "trending": 7, "pullback": 9, "reversal": 3,
    "mixed": -8, "range": -12,
}


def get_regime_tuning() -> list:
    """
    Analyses each regime's historical performance and recommends weight changes.
    """
    resolved = _resolved_alerts()
    if not resolved:
        return []

    by_regime = {}
    for a in resolved:
        r = a.get("market_regime", "unknown")
        by_regime.setdefault(r, [])
        by_regime[r].append(a)

    recs = []
    for regime, group in sorted(by_regime.items()):
        stats = _group_stats(group)
        cw    = _REGIME_WEIGHTS.get(regime, 0)
        n     = stats["total"]

        if n < MIN_SAMPLE:
            action = "keep"
            conf   = "low"
            reason = f"Only {n} resolved alerts in {regime} regime — not enough data."
            rec_w  = cw
        elif stats["expectancy"] < -0.15 and n >= MEDIUM_SAMPLE:
            action = "reduce" if cw > 0 else "keep"
            conf   = _confidence_label(n)
            reason = (
                f"Negative expectancy ({stats['expectancy']:+.3f}R) over {n} alerts. "
                f"{'Consider reducing bonus or blocking this regime.' if cw > 0 else 'Current penalty may be insufficient — consider strengthening.'}"
            )
            rec_w  = max(cw - 2, -15) if cw > 0 else min(cw - 2, -15)
        elif stats["expectancy"] > 0.2 and n >= MEDIUM_SAMPLE:
            action = "increase" if cw > 0 else "keep"
            conf   = _confidence_label(n)
            reason = (
                f"Strong expectancy ({stats['expectancy']:+.3f}R, WR {stats['win_rate']}%) "
                f"over {n} alerts. {'Regime is earning its bonus — consider increasing.' if cw > 0 else 'Keep observation ongoing.'}"
            )
            rec_w  = cw + 2 if cw > 0 else cw
        else:
            action = "keep"
            conf   = _confidence_label(n)
            reason = (
                f"Expectancy {stats['expectancy']:+.3f}R, WR {stats['win_rate']}% "
                f"({n} alerts) — current weighting appears reasonable."
            )
            rec_w  = cw

        recs.append({
            "regime":             regime,
            "current_weight":     cw,
            "recommended_weight": rec_w,
            "action":             action,
            "confidence":         conf,
            "reason":             reason,
            "win_rate":           stats["win_rate"],
            "expectancy":         stats["expectancy"],
            "sample_size":        n,
        })

    return sorted(recs, key=lambda x: -x["expectancy"])


# ── 4. Pair-session tuning ─────────────────────────────────────────────────────

def get_pair_session_tuning() -> dict:
    """
    Identifies the strongest and weakest pair + session combinations.
    Returns top/bottom lists and contextual recommendations.
    """
    resolved = _resolved_alerts()
    if not resolved:
        return {"top": [], "bottom": [], "recommendation": "No data yet."}

    combos = {}
    for a in resolved:
        pair    = (a.get("pair") or "UNKNOWN").upper()
        session = _session_short(a.get("session", ""))
        key     = f"{pair} | {session}"
        combos.setdefault(key, [])
        combos[key].append(a)

    combo_stats = {}
    for key, group in combos.items():
        if len(group) >= MIN_SAMPLE:
            combo_stats[key] = _group_stats(group)

    if not combo_stats:
        return {"top": [], "bottom": [], "recommendation": "Insufficient data per combination."}

    ranked = sorted(combo_stats.items(), key=lambda x: x[1]["expectancy"], reverse=True)
    top    = []
    bottom = []

    for key, stats in ranked[:5]:
        top.append({
            "combo":       key,
            "win_rate":    stats["win_rate"],
            "expectancy":  stats["expectancy"],
            "sample_size": stats["total"],
            "action":      "boost" if stats["expectancy"] > 0.2 else "keep",
            "recommendation": (
                f"Strong combo — consider +2 to +3 contextual bonus."
                if stats["expectancy"] > 0.2
                else "Good combo — keep without bonus."
            ),
        })

    for key, stats in ranked[-5:]:
        if stats["expectancy"] < 0:
            bottom.append({
                "combo":       key,
                "win_rate":    stats["win_rate"],
                "expectancy":  stats["expectancy"],
                "sample_size": stats["total"],
                "action":      "penalize" if stats["expectancy"] < -0.1 else "monitor",
                "recommendation": (
                    f"Negative expectancy — consider -3 to -5 contextual penalty or filter out."
                    if stats["expectancy"] < -0.1
                    else "Slightly below neutral — monitor before making changes."
                ),
            })

    rec = "No pair-session combinations have enough data for strong recommendations yet."
    if top and top[0]["expectancy"] > 0.2:
        rec = f"Best combo: {top[0]['combo']} (E={top[0]['expectancy']:+.3f}R). "
    if bottom and bottom[0]["expectancy"] < -0.1:
        rec += f"Weakest: {bottom[0]['combo']} (E={bottom[0]['expectancy']:+.3f}R) — consider penalising."

    return {"top": top, "bottom": bottom, "recommendation": rec}


# ── 5. Threshold tuning ────────────────────────────────────────────────────────

def get_threshold_tuning() -> dict:
    """
    Analyses watch and entry alert confidence ranges to recommend threshold changes.
    """
    from config import ALERT_MIN_CONFIDENCE, WATCH_ALERT_MIN_CONFIDENCE

    resolved = _resolved_alerts()
    data     = load_data()

    # Watch alerts are stored in pre_alerts — we don't have outcomes for them directly.
    # We can infer by checking low-confidence resolved scanner alerts.
    watch_range  = [a for a in resolved
                    if WATCH_ALERT_MIN_CONFIDENCE <= a.get("confidence", 0) < ALERT_MIN_CONFIDENCE]
    entry_low    = [a for a in resolved
                    if ALERT_MIN_CONFIDENCE <= a.get("confidence", 0) < ALERT_MIN_CONFIDENCE + 5]
    entry_strong = [a for a in resolved if a.get("confidence", 0) >= ALERT_MIN_CONFIDENCE + 5]

    recs = []

    # Watch range analysis
    if len(watch_range) >= MIN_SAMPLE:
        ws = _group_stats(watch_range)
        if ws["expectancy"] < -0.05:
            recs.append(
                f"Watch-alert range ({WATCH_ALERT_MIN_CONFIDENCE}–{ALERT_MIN_CONFIDENCE-1}%) "
                f"shows negative expectancy ({ws['expectancy']:+.3f}R, {len(watch_range)} alerts). "
                f"Consider raising watch threshold from {WATCH_ALERT_MIN_CONFIDENCE} to {WATCH_ALERT_MIN_CONFIDENCE + 3}."
            )
        else:
            recs.append(
                f"Watch range ({WATCH_ALERT_MIN_CONFIDENCE}–{ALERT_MIN_CONFIDENCE-1}%): "
                f"E={ws['expectancy']:+.3f}R ({len(watch_range)} alerts) — threshold appears appropriate."
            )
    else:
        recs.append(
            f"Watch range has {len(watch_range)} resolved alerts — "
            "insufficient data to recommend threshold changes."
        )

    # Entry range low end
    if len(entry_low) >= MIN_SAMPLE:
        es = _group_stats(entry_low)
        if es["expectancy"] < -0.05:
            recs.append(
                f"Entry alerts at {ALERT_MIN_CONFIDENCE}–{ALERT_MIN_CONFIDENCE+4}%: "
                f"E={es['expectancy']:+.3f}R over {len(entry_low)} alerts — underperforming. "
                f"Consider raising entry threshold from {ALERT_MIN_CONFIDENCE} to {ALERT_MIN_CONFIDENCE + 3}."
            )
        elif es["expectancy"] > 0.1:
            recs.append(
                f"Low-end entry alerts ({ALERT_MIN_CONFIDENCE}–{ALERT_MIN_CONFIDENCE+4}%): "
                f"E={es['expectancy']:+.3f}R ({len(entry_low)} alerts) — performing adequately, keep threshold."
            )
        else:
            recs.append(
                f"Low-end entry range is marginal (E={es['expectancy']:+.3f}R). "
                "Monitor before deciding to raise threshold."
            )
    else:
        recs.append(
            f"Low-end entry alerts have {len(entry_low)} resolved samples — need more data."
        )

    # Strong entry comparison
    if len(entry_strong) >= MIN_SAMPLE:
        ss = _group_stats(entry_strong)
        recs.append(
            f"Entry alerts ≥{ALERT_MIN_CONFIDENCE + 5}%: "
            f"E={ss['expectancy']:+.3f}R, WR={ss['win_rate']}% ({len(entry_strong)} alerts) — "
            + ("strong — threshold well calibrated." if ss["expectancy"] > 0.1
               else "underperforming even at higher confidence — review scoring overall.")
        )

    return {
        "current_entry_threshold": ALERT_MIN_CONFIDENCE,
        "current_watch_threshold": WATCH_ALERT_MIN_CONFIDENCE,
        "recommendations":        recs,
        "watch_sample":           len(watch_range),
        "entry_low_sample":       len(entry_low),
        "entry_strong_sample":    len(entry_strong),
    }


# ── 6. Rejection filter tuning ────────────────────────────────────────────────

def get_filter_tuning() -> list:
    """
    Analyses rejection frequency vs accepted-setup performance to recommend
    whether each filter is too aggressive, appropriate, or too lenient.
    """
    resolved   = _resolved_alerts()
    rejections = get_rejections(limit=2000)

    if not rejections:
        return []

    total_rejs = len(rejections)
    by_cat     = {}
    for r in rejections:
        cat = r.get("category", "other")
        by_cat.setdefault(cat, {"scanner": 0, "manual": 0})
        src = r.get("source", "manual")
        by_cat[cat][src] = by_cat[cat].get(src, 0) + 1

    # Build a picture of accepted setup performance by category proxy
    # (We infer: if a filter rejects X% of setups but accepted setups still fail → filter needed)
    total_resolved = len(resolved)
    overall_exp    = 0.0
    if total_resolved > 0:
        wins        = sum(1 for a in resolved if a.get("outcome") == "win")
        avg_win_rr  = _avg_rr(resolved)
        overall_exp = _expectancy(wins, total_resolved, avg_win_rr)

    recommendations = []

    # Hardcoded interpretation logic per category
    category_advice = {
        "range_regime": (
            "Range regime rejection is a core filter — range breakouts are unpredictable. "
            "Justified if accepted trending/pullback setups show positive expectancy. Keep strict."
        ),
        "htf_conflict": (
            "HTF conflict filter blocks setups where lower and higher timeframes disagree. "
            "If overall expectancy is positive, this filter is protecting signal quality. Keep or strengthen."
        ),
        "news_risk": (
            "News risk filter prevents trading into volatility spikes. "
            "High count here is normal around major releases. Appropriate if used consistently."
        ),
        "poor_rr": (
            "Poor RR rejection means the setup's stop or target didn't meet the minimum ratio. "
            "If accepted signals (higher RR) perform well, this filter is correctly calibrated."
        ),
        "low_quality": (
            "Low chart quality rejections from manual submissions. "
            "A high count suggests users frequently submit poor charts — feedback messaging should help reduce this."
        ),
        "unreadable_chart": (
            "Unreadable chart is an absolute quality gate. "
            "Cannot be loosened — any unreadable chart cannot be reliably analysed."
        ),
        "low_confluence": (
            "Low confluence / confidence-threshold rejection. "
            "If accepted signals at the threshold are underperforming, consider raising the threshold."
        ),
        "mixed_regime": (
            "Mixed regime rejection blocks choppy conditions. "
            "High count is expected in sideways markets. Appropriate filter."
        ),
        "no_bias": (
            "No directional bias detected. "
            "This is a fundamental requirement — cannot be loosened without degrading signal quality."
        ),
        "weak_session": (
            "Session / dead-hours rejection. "
            "If accepted session setups outperform, this filter is adding value."
        ),
    }

    for cat, counts in sorted(by_cat.items(), key=lambda x: -(x[1].get("scanner", 0) + x[1].get("manual", 0))):
        total_in_cat = counts.get("scanner", 0) + counts.get("manual", 0)
        pct_of_total = round((total_in_cat / total_rejs) * 100) if total_rejs else 0
        advice       = category_advice.get(cat, "Review whether this filter is producing noise or value.")

        verdict = "appropriate"
        if pct_of_total > 30 and cat not in ("range_regime", "htf_conflict", "news_risk"):
            verdict = "possibly too aggressive — high rejection rate"
        elif pct_of_total < 2:
            verdict = "rarely triggered — filter may be too lenient or rarely applicable"

        recommendations.append({
            "category":   cat,
            "total":      total_in_cat,
            "pct":        pct_of_total,
            "scanner":    counts.get("scanner", 0),
            "manual":     counts.get("manual", 0),
            "verdict":    verdict,
            "advice":     advice,
        })

    return recommendations


# ── 7. Tuning summary ─────────────────────────────────────────────────────────

def get_tuning_summary() -> dict:
    """
    Returns a short high-level summary of the strongest and weakest factors.
    """
    resolved = _resolved_alerts()
    if len(resolved) < MIN_SAMPLE:
        return {"error": f"Only {len(resolved)} resolved alerts — need at least {MIN_SAMPLE} for a summary."}

    overall_wins = sum(1 for a in resolved if a.get("outcome") == "win")
    overall_rr   = _avg_rr(resolved)
    overall_exp  = _expectancy(overall_wins, len(resolved), overall_rr)
    overall_wr   = round((overall_wins / len(resolved)) * 100)

    # Best and worst component
    comp_recs    = get_component_tuning()
    best_comp    = max(comp_recs, key=lambda x: x["pos_expectancy"], default=None)
    worst_comp   = min(comp_recs, key=lambda x: x["pos_expectancy"], default=None)

    # Best and worst regime
    regime_recs  = get_regime_tuning()
    best_regime  = regime_recs[0] if regime_recs else None
    worst_regime = regime_recs[-1] if regime_recs else None

    # Best and worst pair-session
    ps_data      = get_pair_session_tuning()
    best_ps      = ps_data["top"][0] if ps_data["top"] else None
    worst_ps     = ps_data["bottom"][0] if ps_data["bottom"] else None

    # Top priority recommendation
    priority = []
    if worst_comp and worst_comp["action"] in ("reduce", "disable") and worst_comp["confidence"] != "low":
        priority.append(
            f"Reduce '{worst_comp['component']}' from current weight — "
            f"it shows {worst_comp['pos_expectancy']:+.3f}R expectancy"
        )
    if best_comp and best_comp["action"] == "increase" and best_comp["confidence"] != "low":
        priority.append(
            f"Consider increasing '{best_comp['component']}' — "
            f"strongly correlated with wins (E={best_comp['pos_expectancy']:+.3f}R)"
        )
    if worst_regime and worst_regime["expectancy"] < -0.1 and worst_regime["confidence"] != "low":
        priority.append(
            f"Review '{worst_regime['regime']}' regime bonus — "
            f"negative expectancy ({worst_regime['expectancy']:+.3f}R)"
        )

    if not priority:
        priority.append("No urgent changes. Continue accumulating data for higher-confidence recommendations.")

    return {
        "total_resolved":    len(resolved),
        "overall_win_rate":  overall_wr,
        "overall_expectancy": overall_exp,
        "best_component":    best_comp["component"] if best_comp else "N/A",
        "best_comp_exp":     best_comp["pos_expectancy"] if best_comp else 0,
        "worst_component":   worst_comp["component"] if worst_comp else "N/A",
        "worst_comp_exp":    worst_comp["pos_expectancy"] if worst_comp else 0,
        "best_regime":       best_regime["regime"] if best_regime else "N/A",
        "best_regime_exp":   best_regime["expectancy"] if best_regime else 0,
        "worst_regime":      worst_regime["regime"] if worst_regime else "N/A",
        "worst_regime_exp":  worst_regime["expectancy"] if worst_regime else 0,
        "best_pair_session": best_ps["combo"] if best_ps else "N/A",
        "best_ps_exp":       best_ps["expectancy"] if best_ps else 0,
        "worst_pair_session": worst_ps["combo"] if worst_ps else "N/A",
        "worst_ps_exp":      worst_ps["expectancy"] if worst_ps else 0,
        "priority_actions":  priority,
    }
