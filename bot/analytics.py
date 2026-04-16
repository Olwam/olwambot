"""
Analytics module — computes performance statistics from historical alert data.

All functions operate on scanner_alerts stored in bot_data.json.
This module is read-only; it never modifies stored data.

Intended for admin commands:
  /componentstats  — does each score component actually predict wins?
  /confidencecal   — are confidence buckets meaningful?
  /regimestats     — which regimes produce the best alerts?
  /pairsessionstats — which pair+session combos are strongest?
  /latencystats    — how long do trades take to resolve?
  /rejectionstats  — what is blocking the most setups?
  /recentrejections — recent rejection log
"""

from datetime import datetime, timezone, timedelta

from storage import load_data, get_rejections

SAST = timezone(timedelta(hours=2))
_DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _resolved_alerts(limit: int = 2000) -> list:
    """Return all resolved (win/loss) scanner alerts."""
    data   = load_data()
    alerts = data.get("scanner_alerts", [])[-limit:]
    return [a for a in alerts if a.get("outcome") in ("win", "loss")]


def _bucket_label(confidence: int) -> str:
    if confidence < 70:
        return "65–69"
    elif confidence < 75:
        return "70–74"
    elif confidence < 80:
        return "75–79"
    elif confidence < 85:
        return "80–84"
    else:
        return "85+"


def _expectancy(wins: int, total: int, avg_win_rr: float) -> float:
    """Standard expectancy = (wr * avg_win_rr) - (lr * 1R)."""
    if total == 0:
        return 0.0
    wr = wins / total
    lr = 1 - wr
    return round(wr * avg_win_rr - lr * 1.0, 3)


def _session_short(session_label: str) -> str:
    """Normalise long session labels to a short key."""
    if not session_label:
        return "Unknown"
    label = session_label.strip()
    if "Overlap" in label or "overlap" in label:
        return "Overlap"
    first_word = label.split()[0]
    return first_word


def _make_bucket_row(outcomes: list, rr_list: list) -> dict:
    total  = len(outcomes)
    wins   = outcomes.count("win")
    losses = outcomes.count("loss")
    wr     = round((wins / total) * 100) if total else 0
    avg_rr = round(sum(rr_list) / len(rr_list), 2) if rr_list else 0
    exp    = _expectancy(wins, total, avg_rr)
    return {
        "total":    total,
        "wins":     wins,
        "losses":   losses,
        "win_rate": wr,
        "avg_rr":   avg_rr,
        "expectancy": exp,
    }


# ── Component performance analytics ──────────────────────────────────────────

KNOWN_COMPONENTS = [
    "regime", "htf_alignment", "session", "rr",
    "ema_slope", "momentum", "bias_alignment",
    "chart_quality", "news", "quality_penalty",
]


def get_component_stats(component: str = None) -> dict:
    """
    For each score component in score_breakdown, split resolved alerts into:
      - benefited: component value > 0
      - neutral:   component value == 0
      - penalised: component value < 0

    Returns a dict keyed by component name, each with per-group stats.
    If component is specified, only return stats for that component.
    """
    resolved = _resolved_alerts()
    if not resolved:
        return {}

    components = [component] if component else KNOWN_COMPONENTS
    results    = {}

    for comp in components:
        groups = {"positive": [], "neutral": [], "negative": []}
        rrs    = {"positive": [], "neutral": [], "negative": []}

        for a in resolved:
            bd  = a.get("score_breakdown", {})
            val = bd.get(comp)
            if val is None:
                continue
            oc  = a.get("outcome")
            rr  = a.get("rr", 1.0) or 1.0

            if val > 0:
                key = "positive"
            elif val < 0:
                key = "negative"
            else:
                key = "neutral"

            groups[key].append(oc)
            if oc == "win":
                rrs[key].append(rr)

        comp_result = {}
        for grp, outcomes in groups.items():
            if not outcomes:
                continue
            comp_result[grp] = _make_bucket_row(outcomes, rrs[grp])

        if comp_result:
            results[comp] = comp_result

    return results


# ── Confidence calibration analytics ─────────────────────────────────────────

def get_confidence_calibration() -> dict:
    """
    Breaks resolved alerts into confidence bands and computes stats per band.
    Shows whether higher confidence truly means higher win rate.
    """
    resolved = _resolved_alerts()
    if not resolved:
        return {}

    bucket_outcomes = {}
    bucket_rrs      = {}

    for a in resolved:
        label = _bucket_label(a.get("confidence", 0))
        oc    = a.get("outcome")
        rr    = a.get("rr", 1.0) or 1.0

        bucket_outcomes.setdefault(label, [])
        bucket_rrs.setdefault(label, [])

        bucket_outcomes[label].append(oc)
        if oc == "win":
            bucket_rrs[label].append(rr)

    results = {}
    for label in ["65–69", "70–74", "75–79", "80–84", "85+"]:
        outcomes = bucket_outcomes.get(label, [])
        if not outcomes:
            continue
        results[label] = _make_bucket_row(outcomes, bucket_rrs.get(label, []))

    return results


# ── Regime expectancy analytics ───────────────────────────────────────────────

def get_regime_expectancy() -> dict:
    """
    Groups resolved alerts by market_regime and computes per-regime stats.
    Highlights which regimes produce the most reliable signals.
    """
    resolved = _resolved_alerts()
    if not resolved:
        return {}

    regime_outcomes = {}
    regime_rrs      = {}

    for a in resolved:
        regime = a.get("market_regime", "unknown") or "unknown"
        oc     = a.get("outcome")
        rr     = a.get("rr", 1.0) or 1.0

        regime_outcomes.setdefault(regime, [])
        regime_rrs.setdefault(regime, [])

        regime_outcomes[regime].append(oc)
        if oc == "win":
            regime_rrs[regime].append(rr)

    results = {}
    for regime, outcomes in sorted(regime_outcomes.items()):
        results[regime] = _make_bucket_row(outcomes, regime_rrs.get(regime, []))

    return results


# ── Pair + session combined analytics ─────────────────────────────────────────

def get_pair_session_stats(pair_filter: str = None) -> dict:
    """
    Groups resolved alerts by (pair, session_short) and returns stats.
    If pair_filter is given (e.g. "EURUSD"), only returns rows for that pair.
    """
    resolved = _resolved_alerts()
    if not resolved:
        return {}

    combos_outcomes = {}
    combos_rrs      = {}

    for a in resolved:
        pair    = (a.get("pair") or "UNKNOWN").upper()
        session = _session_short(a.get("session", ""))
        oc      = a.get("outcome")
        rr      = a.get("rr", 1.0) or 1.0

        if pair_filter and pair != pair_filter.upper():
            continue

        key = f"{pair} | {session}"
        combos_outcomes.setdefault(key, [])
        combos_rrs.setdefault(key, [])

        combos_outcomes[key].append(oc)
        if oc == "win":
            combos_rrs[key].append(rr)

    results = {}
    for key, outcomes in sorted(combos_outcomes.items(),
                                 key=lambda x: -len(x[1])):
        results[key] = _make_bucket_row(outcomes, combos_rrs.get(key, []))

    return results


# ── Outcome latency analytics ─────────────────────────────────────────────────

def get_latency_stats() -> dict:
    """
    Computes resolution latency (minutes from alert to TP/SL/expiry).
    Groups stats by outcome type.
    """
    data    = load_data()
    alerts  = data.get("scanner_alerts", [])
    resolved = [
        a for a in alerts
        if a.get("outcome") in ("win", "loss", "expired")
        and a.get("latency_minutes") is not None
    ]

    if not resolved:
        return {
            "total_resolved": 0,
            "by_outcome":     {},
            "note":           "No resolved alerts with latency data yet.",
        }

    by_outcome = {}
    for a in resolved:
        oc  = a.get("outcome")
        lat = a.get("latency_minutes", 0)
        by_outcome.setdefault(oc, [])
        by_outcome[oc].append(lat)

    result_groups = {}
    for oc, lats in sorted(by_outcome.items()):
        avg = round(sum(lats) / len(lats), 1)
        mn  = round(min(lats), 1)
        mx  = round(max(lats), 1)
        result_groups[oc] = {
            "count":       len(lats),
            "avg_minutes": avg,
            "avg_hours":   round(avg / 60, 1),
            "min_minutes": mn,
            "max_minutes": mx,
        }

    all_lats = [a.get("latency_minutes", 0) for a in resolved]
    overall_avg = round(sum(all_lats) / len(all_lats), 1) if all_lats else 0

    return {
        "total_resolved": len(resolved),
        "overall_avg_minutes": overall_avg,
        "overall_avg_hours":   round(overall_avg / 60, 1),
        "by_outcome": result_groups,
    }


# ── Rejection analytics ───────────────────────────────────────────────────────

def get_rejection_stats() -> dict:
    """
    Aggregates rejection events by category and source.
    Returns counts and percentage breakdown.
    """
    rejections = get_rejections(limit=2000)
    if not rejections:
        return {"total": 0, "by_category": {}, "by_source": {}}

    total      = len(rejections)
    by_cat     = {}
    by_source  = {}
    cat_source = {}  # category → {scanner: N, manual: N}

    for r in rejections:
        cat    = r.get("category", "other")
        source = r.get("source", "manual")

        by_cat[cat]       = by_cat.get(cat, 0) + 1
        by_source[source] = by_source.get(source, 0) + 1

        cat_source.setdefault(cat, {"scanner": 0, "manual": 0})
        cat_source[cat][source] = cat_source[cat].get(source, 0) + 1

    # Sort categories by frequency
    sorted_cats = sorted(by_cat.items(), key=lambda x: -x[1])
    cat_rows = {}
    for cat, count in sorted_cats:
        pct = round((count / total) * 100)
        cat_rows[cat] = {
            "count":    count,
            "pct":      pct,
            "scanner":  cat_source.get(cat, {}).get("scanner", 0),
            "manual":   cat_source.get(cat, {}).get("manual", 0),
        }

    return {
        "total":       total,
        "by_category": cat_rows,
        "by_source":   by_source,
    }


def get_recent_rejections(limit: int = 10) -> list:
    """Returns the most recent rejection log entries."""
    all_rejs = get_rejections(limit=500)
    return list(reversed(all_rejs))[:limit]


# ── Hour-of-day heatmap ────────────────────────────────────────────────────────

def get_hour_heatmap() -> dict:
    """
    Groups resolved win/loss scanner alerts by SAST hour (0-23).
    Returns stats per hour so the caller can draw a text heatmap.
    """
    data     = load_data()
    resolved = [a for a in data.get("scanner_alerts", [])
                if a.get("outcome") in ("win", "loss")]

    by_hour: dict = {}
    for a in resolved:
        ts = a.get("timestamp", "")
        try:
            dt   = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            dt   = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
            hour = dt.astimezone(SAST).hour
        except Exception:
            continue

        bucket = by_hour.setdefault(hour, {"wins": 0, "losses": 0, "rrs": []})
        if a.get("outcome") == "win":
            bucket["wins"] += 1
            bucket["rrs"].append(a.get("rr", 1.0) or 1.0)
        else:
            bucket["losses"] += 1

    result = {}
    for hour in sorted(by_hour):
        b     = by_hour[hour]
        total = b["wins"] + b["losses"]
        wr    = round((b["wins"] / total) * 100) if total else 0
        avg_rr = round(sum(b["rrs"]) / len(b["rrs"]), 2) if b["rrs"] else 0.0
        result[hour] = {
            "label":      f"{hour:02d}:00 SAST",
            "total":      total,
            "wins":       b["wins"],
            "losses":     b["losses"],
            "win_rate":   wr,
            "avg_rr":     avg_rr,
            "expectancy": _expectancy(b["wins"], total, avg_rr),
        }
    return result


# ── Day-of-week heatmap ────────────────────────────────────────────────────────

def get_day_heatmap() -> dict:
    """
    Groups resolved win/loss scanner alerts by SAST day of week (Mon–Fri).
    """
    data     = load_data()
    resolved = [a for a in data.get("scanner_alerts", [])
                if a.get("outcome") in ("win", "loss")]

    by_day: dict = {}
    for a in resolved:
        ts = a.get("timestamp", "")
        try:
            dt  = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            dt  = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
            day = dt.astimezone(SAST).weekday()   # 0=Mon … 6=Sun
        except Exception:
            continue

        bucket = by_day.setdefault(day, {"wins": 0, "losses": 0, "rrs": []})
        if a.get("outcome") == "win":
            bucket["wins"] += 1
            bucket["rrs"].append(a.get("rr", 1.0) or 1.0)
        else:
            bucket["losses"] += 1

    result = {}
    for day in sorted(by_day):
        b      = by_day[day]
        total  = b["wins"] + b["losses"]
        wr     = round((b["wins"] / total) * 100) if total else 0
        avg_rr = round(sum(b["rrs"]) / len(b["rrs"]), 2) if b["rrs"] else 0.0
        result[_DAY_NAMES[day]] = {
            "total":      total,
            "wins":       b["wins"],
            "losses":     b["losses"],
            "win_rate":   wr,
            "avg_rr":     avg_rr,
            "expectancy": _expectancy(b["wins"], total, avg_rr),
        }
    return result


# ── Running R-multiple P&L curve ──────────────────────────────────────────────

def get_pnl_curve(limit: int = 50) -> dict:
    """
    Computes running cumulative R-multiple P&L from resolved scanner alerts.
    Wins add the alert's RR value; losses subtract 1R.
    Returns a list of data points plus summary stats.
    """
    data     = load_data()
    resolved = [a for a in data.get("scanner_alerts", [])
                if a.get("outcome") in ("win", "loss")]

    def _ts(a):
        return a.get("outcome_time") or a.get("timestamp") or ""

    resolved.sort(key=_ts)
    resolved = resolved[-limit:]

    curve   = []
    running = 0.0

    for a in resolved:
        oc    = a.get("outcome")
        rr    = float(a.get("rr") or 1.0)
        delta = rr if oc == "win" else -1.0
        running = round(running + delta, 2)

        ts = _ts(a)
        try:
            dt      = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            dt      = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
            date_s  = dt.astimezone(SAST).strftime("%m/%d %H:%M")
        except Exception:
            date_s  = ts[:10]

        curve.append({
            "pair":      a.get("pair", "?"),
            "direction": a.get("direction", "?"),
            "outcome":   oc,
            "rr":        rr,
            "delta":     delta,
            "running":   running,
            "date":      date_s,
        })

    total  = len(curve)
    wins   = sum(1 for c in curve if c["outcome"] == "win")
    losses = total - wins
    wr     = round((wins / total) * 100, 1) if total else 0.0

    return {
        "curve":    curve,
        "total":    total,
        "wins":     wins,
        "losses":   losses,
        "win_rate": wr,
        "final_r":  running,
    }


# ── Daily health report ───────────────────────────────────────────────────────

def get_daily_health_report() -> dict:
    """
    Generates a daily performance summary with drift detection.

    Drift warning is issued when the recent (last 7d / last 20 resolved) win rate
    is HEALTH_DRIFT_THRESHOLD_PP or more percentage points below the all-time rate.
    This helps surface real performance degradation early.
    """
    from config import HEALTH_DRIFT_THRESHOLD_PP

    data    = load_data()
    alerts  = data.get("scanner_alerts", [])
    now_utc = datetime.now(timezone.utc)

    # ── Last 24 h ─────────────────────────────────────────────────────────────
    cutoff_24h      = now_utc - timedelta(hours=24)
    recent_24h      = []
    for a in alerts:
        ts = a.get("outcome_time") or a.get("timestamp", "")
        if a.get("outcome") not in ("win", "loss"):
            continue
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            dt = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
            if dt >= cutoff_24h:
                recent_24h.append(a)
        except Exception:
            pass

    wins_24h   = sum(1 for a in recent_24h if a["outcome"] == "win")
    losses_24h = len(recent_24h) - wins_24h
    r_24h      = round(
        sum((a.get("rr", 1.0) or 1.0) if a["outcome"] == "win" else -1.0
            for a in recent_24h), 2,
    )

    # ── All-time ──────────────────────────────────────────────────────────────
    all_resolved = [a for a in alerts if a.get("outcome") in ("win", "loss")]
    all_wins     = sum(1 for a in all_resolved if a["outcome"] == "win")
    all_total    = len(all_resolved)
    all_wr       = round((all_wins / all_total) * 100, 1) if all_total else 0.0

    win_rrs    = [a.get("rr", 1.0) or 1.0 for a in all_resolved if a["outcome"] == "win"]
    avg_win_rr = round(sum(win_rrs) / len(win_rrs), 2) if win_rrs else 1.0
    expectancy = _expectancy(all_wins, all_total, avg_win_rr)

    pending = sum(1 for a in alerts if a.get("outcome") == "pending")

    # ── Drift detection (last 20 resolved trades vs all-time) ─────────────────
    drift_warning     = ""
    recent_20         = all_resolved[-20:] if len(all_resolved) >= 20 else []
    if len(recent_20) >= 10 and all_total >= 30:
        r20_wins = sum(1 for a in recent_20 if a["outcome"] == "win")
        r20_wr   = round((r20_wins / len(recent_20)) * 100, 1)
        gap      = all_wr - r20_wr
        if gap >= HEALTH_DRIFT_THRESHOLD_PP:
            drift_warning = (
                f"⚠️ DRIFT ALERT: Recent {len(recent_20)}-trade win rate "
                f"({r20_wr}%) is {round(gap, 1)}pp below all-time ({all_wr}%). "
                "Review recent setups for regime or session mismatch."
            )

    # ── Best and worst pair/session combos ────────────────────────────────────
    pair_session: dict = {}
    for a in all_resolved:
        key = f"{a.get('pair','?')} / {a.get('session','?')}"
        row = pair_session.setdefault(key, {"wins": 0, "total": 0})
        row["total"] += 1
        if a.get("outcome") == "win":
            row["wins"] += 1

    best_combo  = ""
    worst_combo = ""
    if pair_session:
        qualified = {k: v for k, v in pair_session.items() if v["total"] >= 4}
        if qualified:
            best_k  = max(qualified, key=lambda k: qualified[k]["wins"] / qualified[k]["total"])
            worst_k = min(qualified, key=lambda k: qualified[k]["wins"] / qualified[k]["total"])
            best_combo  = (f"{best_k} "
                           f"({round(qualified[best_k]['wins']/qualified[best_k]['total']*100)}% "
                           f"from {qualified[best_k]['total']} alerts)")
            worst_combo = (f"{worst_k} "
                           f"({round(qualified[worst_k]['wins']/qualified[worst_k]['total']*100)}% "
                           f"from {qualified[worst_k]['total']} alerts)")

    return {
        "resolved_24h":  len(recent_24h),
        "wins_24h":      wins_24h,
        "losses_24h":    losses_24h,
        "r_24h":         r_24h,
        "pending":       pending,
        "all_resolved":  all_total,
        "all_wins":      all_wins,
        "all_win_rate":  all_wr,
        "expectancy":    expectancy,
        "active_users":  len(data.get("approved_users", {})),
        "drift_warning": drift_warning,
        "best_combo":    best_combo,
        "worst_combo":   worst_combo,
    }
