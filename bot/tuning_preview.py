"""
Tuning preview / simulation engine.

Re-scores historical resolved alerts with proposed candidate weights
and compares performance against current weights — without touching
any live data, config files, or scoring logic.

Used by admin commands:
  /applytuningpreview  — historical impact simulation of proposed changes
  /compareweights      — side-by-side weight comparison table
"""

from datetime import datetime, timezone, timedelta

from storage  import load_data
from config   import ALERT_MIN_CONFIDENCE, WATCH_ALERT_MIN_CONFIDENCE, TUNING_OVERRIDES

# ── Sample size thresholds ─────────────────────────────────────────────────────
MIN_SAMPLE    = 10
MEDIUM_SAMPLE = 20
HIGH_SAMPLE   = 50

# ── Valid candidate keys ───────────────────────────────────────────────────────
# Maps user-facing key → (score_breakdown_field, human description)
# field=None means threshold-only (no score change, classification change only)
VALID_KEYS = {
    # Regime bonuses/penalties
    "pullback":             ("regime",         "Pullback regime bonus"),
    "trending":             ("regime",         "Trending regime bonus"),
    "reversal":             ("regime",         "Reversal regime bonus"),
    "range":                ("regime",         "Range regime penalty"),
    "mixed":                ("regime",         "Mixed regime penalty"),
    # HTF alignment
    "htf_aligned":          ("htf_alignment",  "HTF alignment bonus"),
    "htf_conflict":         ("htf_alignment",  "HTF conflict penalty"),
    # EMA slope (aligned side; misaligned mirrors proportionally)
    "ema_slope":            ("ema_slope",      "EMA slope aligned bonus"),
    # Risk/reward
    "rr_3plus":             ("rr",             "RR ≥ 3.0 bonus"),
    "rr_25plus":            ("rr",             "RR ≥ 2.5 bonus"),
    "rr_2plus":             ("rr",             "RR ≥ 2.0 bonus"),
    # Session cap
    "session_cap":          ("session",        "Session score cap"),
    # News penalties
    "news_medium":          ("news",           "Medium news risk penalty"),
    "news_high":            ("news",           "High news risk penalty"),
    # Bias
    "bias_aligned":         ("bias_alignment", "Directional bias aligned bonus"),
    "bias_conflict":        ("bias_alignment", "Directional bias conflict penalty"),
    # Chart quality
    "chart_quality_clean":  ("chart_quality",  "Clean chart bonus"),
    "chart_quality_dirty":  ("chart_quality",  "Dirty chart penalty"),
    # Thresholds (classification only — do not change scores)
    "entry_threshold":      (None,             "Entry alert confidence threshold"),
    "watch_threshold":      (None,             "Watch alert confidence threshold"),
}

# ── Current engine defaults (manual path) ────────────────────────────────────
_DEFAULTS: dict = {
    "pullback":             9,
    "trending":             7,
    "reversal":             3,
    "range":               -12,
    "mixed":               -8,
    "htf_aligned":          10,
    "htf_conflict":        -12,
    "ema_slope":            3,   # aligned side; misaligned = -3
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


def _effective_defaults() -> dict:
    """Merge TUNING_OVERRIDES on top of _DEFAULTS to get actual live values."""
    merged = dict(_DEFAULTS)
    merged.update(TUNING_OVERRIDES)
    return merged


# ── Argument parsing ──────────────────────────────────────────────────────────

def parse_candidate_args(parts: list) -> tuple:
    """
    Parse a list of 'key=value' strings into (candidate_dict, errors_list).
    Also handles source=scanner|manual|all and days=N.
    """
    candidate = {}
    meta      = {"source": "all", "days": 0}
    errors    = []

    for part in parts:
        if "=" not in part:
            errors.append(f"Bad format: '{part}' — use key=value")
            continue
        k, v = part.split("=", 1)
        k    = k.strip().lower()
        v    = v.strip()

        if k == "source":
            if v in ("scanner", "manual", "all"):
                meta["source"] = v
            else:
                errors.append(f"source must be scanner, manual, or all — got '{v}'")
            continue

        if k == "days":
            try:
                meta["days"] = max(0, int(v))
            except ValueError:
                errors.append(f"days must be an integer — got '{v}'")
            continue

        if k not in VALID_KEYS:
            errors.append(
                f"Unknown key: '{k}'\n"
                f"Valid keys: {', '.join(sorted(VALID_KEYS))}"
            )
            continue

        try:
            candidate[k] = int(v)
        except ValueError:
            errors.append(f"Value must be an integer — got '{k}={v}'")

    return candidate, meta, errors


# ── Historical data loader ────────────────────────────────────────────────────

def _load_resolved(source: str = "all", days: int = 0, limit: int = 2000) -> list:
    """
    Load resolved alerts (win/loss/expired) from stored scanner history.
    Applies source and days filters if provided.
    """
    data   = load_data()
    alerts = data.get("scanner_alerts", [])[-limit:]
    result = [a for a in alerts if a.get("outcome") in ("win", "loss", "expired")]

    if source != "all":
        result = [a for a in result if a.get("source", "scanner") == source]

    if days > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        kept   = []
        for a in result:
            ts = a.get("timestamp") or a.get("alerted_at", "")
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt >= cutoff:
                    kept.append(a)
            except Exception:
                kept.append(a)   # keep if timestamp unparseable
        result = kept

    return result


# ── Score delta engine ────────────────────────────────────────────────────────

def _score_delta(alert: dict, candidate: dict, current: dict) -> int:
    """
    Compute the score adjustment a single alert would receive under candidate weights.

    Strategy: for each candidate key, check whether the alert's stored
    score_breakdown shows that this component was active, then apply the
    difference between candidate and current value.

    Limitations:
    - For regime keys we rely on alert.market_regime for exact matching.
    - For RR we rely on alert.rr value.
    - For other components we infer from breakdown sign.
    - We cannot reconstruct news or bias sub-type without extra stored fields.
    """
    bd     = alert.get("score_breakdown", {})
    regime = alert.get("market_regime", "")
    rr_val = float(alert.get("rr") or 0)
    delta  = 0

    # ── Regime ────────────────────────────────────────────────────────────────
    for reg_key in ("pullback", "trending", "reversal", "range", "mixed"):
        if reg_key in candidate and regime == reg_key:
            old    = current.get(reg_key, _DEFAULTS[reg_key])
            delta += candidate[reg_key] - old

    # ── HTF alignment ─────────────────────────────────────────────────────────
    htf_pts = bd.get("htf_alignment", 0)
    if "htf_aligned" in candidate and htf_pts > 0:
        old    = current.get("htf_aligned", _DEFAULTS["htf_aligned"])
        delta += candidate["htf_aligned"] - old
    if "htf_conflict" in candidate and htf_pts < 0:
        old    = current.get("htf_conflict", _DEFAULTS["htf_conflict"])
        delta += candidate["htf_conflict"] - old

    # ── EMA slope ─────────────────────────────────────────────────────────────
    # aligned side changes → misaligned side mirrors proportionally
    if "ema_slope" in candidate:
        ema_pts = bd.get("ema_slope", 0)
        if ema_pts > 0:
            old    = current.get("ema_slope", _DEFAULTS["ema_slope"])
            delta += candidate["ema_slope"] - old
        elif ema_pts < 0:
            old_neg = -(current.get("ema_slope", _DEFAULTS["ema_slope"]))
            new_neg = -(candidate["ema_slope"])
            delta += new_neg - old_neg

    # ── RR ────────────────────────────────────────────────────────────────────
    rr_pts = bd.get("rr", 0)
    if "rr_3plus" in candidate and rr_val >= 3.0 and rr_pts > 0:
        old    = current.get("rr_3plus", _DEFAULTS["rr_3plus"])
        delta += candidate["rr_3plus"] - old
    elif "rr_25plus" in candidate and 2.5 <= rr_val < 3.0 and rr_pts > 0:
        old    = current.get("rr_25plus", _DEFAULTS["rr_25plus"])
        delta += candidate["rr_25plus"] - old
    elif "rr_2plus" in candidate and 2.0 <= rr_val < 2.5 and rr_pts > 0:
        old    = current.get("rr_2plus", _DEFAULTS["rr_2plus"])
        delta += candidate["rr_2plus"] - old

    # ── Session cap ───────────────────────────────────────────────────────────
    if "session_cap" in candidate:
        sess = bd.get("session", 0)
        if sess > 0:
            new_cap = candidate["session_cap"]
            new_sess = min(sess, new_cap)
            delta += new_sess - sess

    # ── News ──────────────────────────────────────────────────────────────────
    news_pts = bd.get("news", 0)
    if "news_medium" in candidate and -20 < news_pts < 0:
        old    = current.get("news_medium", _DEFAULTS["news_medium"])
        delta += candidate["news_medium"] - old
    if "news_high" in candidate and news_pts <= -20:
        old    = current.get("news_high", _DEFAULTS["news_high"])
        delta += candidate["news_high"] - old

    # ── Bias alignment ────────────────────────────────────────────────────────
    bias_pts = bd.get("bias_alignment", 0)
    if "bias_aligned" in candidate and bias_pts > 0:
        old    = current.get("bias_aligned", _DEFAULTS["bias_aligned"])
        delta += candidate["bias_aligned"] - old
    if "bias_conflict" in candidate and bias_pts < 0:
        old    = current.get("bias_conflict", _DEFAULTS["bias_conflict"])
        delta += candidate["bias_conflict"] - old

    # ── Chart quality ─────────────────────────────────────────────────────────
    cq_pts = bd.get("chart_quality", 0)
    if "chart_quality_clean" in candidate and cq_pts > 0:
        old    = current.get("chart_quality_clean", _DEFAULTS["chart_quality_clean"])
        delta += candidate["chart_quality_clean"] - old
    if "chart_quality_dirty" in candidate and cq_pts < 0:
        old    = current.get("chart_quality_dirty", _DEFAULTS["chart_quality_dirty"])
        delta += candidate["chart_quality_dirty"] - old

    return delta


# ── Main preview function ─────────────────────────────────────────────────────

def run_preview(candidate: dict, source: str = "all", days: int = 0) -> dict:
    """
    Simulates candidate weight changes against resolved historical alerts.
    Returns a dict with all preview metrics. Does not modify any stored data.
    """
    current      = _effective_defaults()
    new_entry_th = candidate.get("entry_threshold", ALERT_MIN_CONFIDENCE)
    new_watch_th = candidate.get("watch_threshold", WATCH_ALERT_MIN_CONFIDENCE)
    cur_entry_th = ALERT_MIN_CONFIDENCE
    cur_watch_th = WATCH_ALERT_MIN_CONFIDENCE

    alerts = _load_resolved(source=source, days=days)
    n      = len(alerts)

    if n < MIN_SAMPLE:
        return {
            "error":       f"Only {n} resolved alerts available — need ≥ {MIN_SAMPLE} for a meaningful preview.",
            "sample_size": n,
        }

    tier = "high" if n >= HIGH_SAMPLE else ("medium" if n >= MEDIUM_SAMPLE else "low")

    # ── Current metrics ───────────────────────────────────────────────────────
    cur_wins     = sum(1 for a in alerts if a.get("outcome") == "win")
    cur_wr       = round((cur_wins / n) * 100, 1)
    cur_confs    = [a.get("confidence", 0) for a in alerts]
    cur_avg_cf   = round(sum(cur_confs) / n, 1)
    cur_win_rrs  = [a.get("rr", 1.0) for a in alerts if a.get("outcome") == "win" and a.get("rr")]
    cur_avg_rr   = round(sum(cur_win_rrs) / len(cur_win_rrs), 2) if cur_win_rrs else 1.0
    cur_exp      = round((cur_wins / n) * cur_avg_rr - ((n - cur_wins) / n) * 1.0, 3)
    cur_entries  = sum(1 for a in alerts if a.get("confidence", 0) >= cur_entry_th)
    cur_watches  = sum(1 for a in alerts if cur_watch_th <= a.get("confidence", 0) < cur_entry_th)

    # ── Candidate simulation ───────────────────────────────────────────────────
    weight_only  = {k: v for k, v in candidate.items() if k not in ("entry_threshold", "watch_threshold")}
    new_confs    = []
    new_entries  = []
    new_watches  = []
    demoted      = 0   # entry alert → watch alert
    promoted     = 0   # watch alert → entry alert
    filtered_out = 0   # was entry or watch, now below watch threshold

    for a in alerts:
        orig_conf = a.get("confidence", 0)
        d         = _score_delta(a, weight_only, current) if weight_only else 0
        new_conf  = max(35, min(orig_conf + d, 92))
        new_confs.append(new_conf)

        orig_is_entry = orig_conf >= cur_entry_th
        orig_is_watch = cur_watch_th <= orig_conf < cur_entry_th
        new_is_entry  = new_conf >= new_entry_th
        new_is_watch  = new_watch_th <= new_conf < new_entry_th

        if new_is_entry:
            new_entries.append(a)
            if orig_is_watch:
                promoted += 1
        elif new_is_watch:
            new_watches.append(a)
            if orig_is_entry:
                demoted += 1
        else:
            if orig_is_entry or orig_is_watch:
                filtered_out += 1

    new_n      = len(new_entries)
    new_avg_cf = round(sum(new_confs) / len(new_confs), 1) if new_confs else 0.0

    if new_n > 0:
        new_wins    = sum(1 for a in new_entries if a.get("outcome") == "win")
        new_wr      = round((new_wins / new_n) * 100, 1)
        new_win_rrs = [a.get("rr", 1.0) for a in new_entries if a.get("outcome") == "win" and a.get("rr")]
        new_avg_rr  = round(sum(new_win_rrs) / len(new_win_rrs), 2) if new_win_rrs else 1.0
        new_exp     = round((new_wins / new_n) * new_avg_rr - ((new_n - new_wins) / new_n) * 1.0, 3)
    else:
        new_wins = new_wr = new_avg_rr = new_exp = 0

    interp = _interpret(
        cur_wr, new_wr, cur_exp, new_exp,
        cur_entries, new_n,
        demoted, promoted, filtered_out, n, tier,
    )

    return {
        "sample_size":      n,
        "confidence_tier":  tier,
        "source":           source,
        "days":             days,
        # Current metrics
        "cur_win_rate":     cur_wr,
        "cur_expectancy":   cur_exp,
        "cur_avg_conf":     cur_avg_cf,
        "cur_entries":      cur_entries,
        "cur_watches":      cur_watches,
        "cur_entry_thresh": cur_entry_th,
        "cur_watch_thresh": cur_watch_th,
        # Candidate metrics
        "new_win_rate":     new_wr,
        "new_expectancy":   new_exp,
        "new_avg_conf":     new_avg_cf,
        "new_entries":      new_n,
        "new_watches":      len(new_watches),
        "new_entry_thresh": new_entry_th,
        "new_watch_thresh": new_watch_th,
        # Changes
        "demoted":          demoted,
        "promoted":         promoted,
        "filtered_out":     filtered_out,
        "interpretation":   interp,
    }


def _interpret(cur_wr, new_wr, cur_exp, new_exp,
               cur_ent, new_ent, demoted, promoted, filtered, total, tier):
    """Generate a plain-English interpretation of preview results."""
    if tier == "low":
        return (
            "⚠️ Sample size is small — treat this preview as directional guidance only. "
            "Accumulate more resolved alerts before applying changes."
        )

    wr_diff  = round(new_wr  - cur_wr,  1)
    exp_diff = round(new_exp - cur_exp, 3)
    ent_diff = new_ent - cur_ent
    parts    = []

    # Expectancy verdict
    if exp_diff > 0.05 and wr_diff > 0:
        parts.append("Candidate improves both win rate and expectancy.")
    elif exp_diff > 0.05:
        parts.append("Candidate improves expectancy with a marginal win rate change.")
    elif exp_diff < -0.05:
        parts.append("Candidate reduces expectancy — review carefully before applying.")
    else:
        parts.append("Marginal expectancy impact — candidate is largely neutral.")

    # Volume change
    if ent_diff <= -10:
        parts.append(f"Alert frequency drops sharply ({cur_ent} → {new_ent} entry alerts).")
    elif ent_diff < -3:
        parts.append(f"Alert frequency drops moderately ({cur_ent} → {new_ent} entry alerts).")
    elif ent_diff >= 10:
        parts.append(f"Alert frequency rises significantly ({cur_ent} → {new_ent}) — verify quality holds.")
    elif ent_diff > 3:
        parts.append(f"Alert frequency rises slightly ({cur_ent} → {new_ent} entry alerts).")

    # Movement detail
    if demoted > 0:
        parts.append(f"{demoted} entry alert(s) downgrade to watch level.")
    if promoted > 0:
        parts.append(f"{promoted} watch alert(s) promote to entry level.")
    if filtered > 0:
        parts.append(f"{filtered} alert(s) removed entirely (below watch threshold).")

    if tier == "medium":
        parts.append("Confidence is medium — continue accumulating data before applying.")

    return " ".join(parts)


# ── Compare weights (no simulation) ──────────────────────────────────────────

def compare_weights(candidate: dict) -> list:
    """
    Returns a list of comparison rows for /compareweights.
    No simulation is run — this is a pure weight comparison table.
    """
    current = _effective_defaults()
    rows    = []

    for k, v in sorted(candidate.items()):
        desc = VALID_KEYS.get(k, (None, k))[1]
        if k == "entry_threshold":
            cur_val = ALERT_MIN_CONFIDENCE
        elif k == "watch_threshold":
            cur_val = WATCH_ALERT_MIN_CONFIDENCE
        else:
            cur_val = current.get(k, _DEFAULTS.get(k, 0))

        delta = v - cur_val
        action = "increase" if delta > 0 else ("reduce" if delta < 0 else "keep")
        rows.append({
            "key":         k,
            "description": desc,
            "current":     cur_val,
            "candidate":   v,
            "delta":       delta,
            "action":      action,
            "is_override": k in TUNING_OVERRIDES,
        })

    return rows
