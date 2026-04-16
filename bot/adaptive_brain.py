"""
adaptive_brain.py — Self-learning loss analysis and adaptive correction engine.

After every resolved loss, the brain:
  1. Analyses ALL resolved trades to detect systematic failure patterns
  2. Applies targeted confidence penalties to the specific conditions that keep losing
     (regime, session, pair, overall calibration)
  3. Automatically relaxes penalties when performance recovers in that category
  4. Reports every change to the admin in plain English

Design rules:
  - Transparent   : every adjustment is reported and visible via /brainreport
  - Bounded       : penalties are capped so the bot doesn't go silent forever
  - Reversible    : penalties shrink automatically when win rate recovers
  - Pattern-based : adjusts specific conditions, not random noise
  - Never modifies code — only numeric weights stored in bot_data.json
"""

import time
from datetime import datetime, timezone, timedelta

# ── Tuning constants ───────────────────────────────────────────────────────────
MIN_SAMPLES            = 3      # minimum trades in a category before adjusting
POOR_WIN_RATE          = 0.40   # < 40% WR triggers a penalty
GOOD_WIN_RATE          = 0.60   # > 60% WR relaxes a penalty
ANALYSIS_COOLDOWN_SECS = 600    # max once per 10 min to avoid thrashing

MAX_REGIME_PENALTY     = -15    # never penalise regime more than -15pp
MAX_SESSION_PENALTY    = -10
MAX_PAIR_PENALTY       = -12
MAX_GLOBAL_BUMP        = 20     # max extra pp added to global confidence floor

REGIME_STEP            = 5      # pp change per analysis cycle
SESSION_STEP           = 4
PAIR_STEP              = 6
GLOBAL_STEP            = 5
RELAX_STEP             = 3      # pp recovered per win cycle

# ── Module state ───────────────────────────────────────────────────────────────
_adjustments: dict = {
    "regime_penalties":  {},   # {"trending": -5, ...}
    "session_penalties": {},   # {"asian": -6, ...}
    "pair_penalties":    {},   # {"EURUSD": -8, ...}
    "global_conf_bump":  0,    # extra pp added to scanner/engine confidence minimum
}
_analysis_log: list  = []   # last 20 analysis results
_last_analysis: float = 0.0  # epoch ts of last full analysis run


# ── Public API ─────────────────────────────────────────────────────────────────

def on_loss(alert: dict, notify_callback=None):
    """
    Call this from outcome_checker after a loss is confirmed.
    Runs pattern analysis and updates adjustments if warranted.
    """
    global _last_analysis
    if time.time() - _last_analysis < ANALYSIS_COOLDOWN_SECS:
        return   # don't re-analyse too frequently
    try:
        from storage import load_data
        resolved = [a for a in load_data().get("scanner_alerts", [])
                    if a.get("outcome") in ("win", "loss")]
        if len(resolved) >= MIN_SAMPLES:
            _run_analysis(resolved, notify_callback)
    except Exception as e:
        print(f"[Brain] on_loss error: {e}", flush=True)


def on_win(alert: dict):
    """
    Call this from outcome_checker after a win.
    Relaxes penalties in categories where performance has recovered.
    """
    try:
        from storage import load_data
        resolved = [a for a in load_data().get("scanner_alerts", [])
                    if a.get("outcome") in ("win", "loss")]
        if len(resolved) >= MIN_SAMPLES:
            _relax_if_recovering(resolved, alert)
    except Exception as e:
        print(f"[Brain] on_win error: {e}", flush=True)


def get_confidence_adjustment(pair: str, regime: str, session: str) -> int:
    """
    Returns the total brain penalty/bonus (negative = harder to fire).
    Applied ON TOP of the normal scanner/engine confidence score.
    """
    adj  = _adjustments["regime_penalties"].get(regime, 0)
    adj += _adjustments["session_penalties"].get(_normalise_session(session), 0)
    adj += _adjustments["pair_penalties"].get(pair.upper(), 0)
    return adj


def get_global_conf_bump() -> int:
    """Extra pp to add to the effective confidence threshold (always >= 0)."""
    return max(0, _adjustments.get("global_conf_bump", 0))


def get_status() -> dict:
    active = {k: v for k, v in _adjustments.items()
              if (isinstance(v, dict) and v) or (isinstance(v, (int, float)) and v)}
    return {
        "active_adjustments": active,
        "last_analysis":      datetime.fromtimestamp(_last_analysis, tz=timezone.utc)
                              .strftime("%d %b %H:%M UTC") if _last_analysis else "never",
        "analysis_count":     len(_analysis_log),
    }


def get_full_report() -> str:
    """Human-readable Markdown report for the /brainreport Telegram command."""
    if not _analysis_log:
        return (
            "🧠 *Adaptive Brain*\n\n"
            "No analysis yet — need at least 3 resolved trades.\n"
            "Keep trading and the brain will start learning."
        )

    last = _analysis_log[-1]
    wr   = last.get("win_rate", 0)
    lines = [
        "🧠 *ADAPTIVE BRAIN REPORT*\n",
        f"Last run: {last.get('timestamp', '?')}",
        f"Trades: {last.get('total_resolved', 0)} total "
        f"({last.get('wins', 0)}W / {last.get('losses', 0)}L — {wr}% win rate)\n",
    ]

    # Active adjustments
    reg = _adjustments["regime_penalties"]
    ses = _adjustments["session_penalties"]
    par = _adjustments["pair_penalties"]
    gb  = _adjustments.get("global_conf_bump", 0)

    if not any([reg, ses, par, gb]):
        lines.append("✅ Performance looks acceptable — no adjustments active.")
    else:
        lines.append("⚙️ *Active adjustments (what the brain changed):*")
        if gb:
            lines.append(f"  • Global confidence floor: +{gb}pp stricter")
        for r, p in reg.items():
            lines.append(f"  • Regime `{r}`: {p:+d}pp penalty")
        for s, p in ses.items():
            lines.append(f"  • Session `{s}`: {p:+d}pp penalty")
        for pair, p in par.items():
            lines.append(f"  • {pair}: {p:+d}pp penalty")

    # Most recent findings
    if last.get("findings"):
        lines.append("\n🔍 *What went wrong (latest analysis):*")
        for f in last["findings"][:6]:
            lines.append(f"  • {f}")

    lines.append(
        "\n📌 Penalties auto-relax when that category's win rate exceeds 60%."
    )
    return "\n".join(lines)


def refresh_from_storage():
    """Restore brain state from storage on bot startup."""
    global _adjustments, _analysis_log, _last_analysis
    try:
        from storage import load_data
        brain = load_data().get("adaptive_brain", {})
        if brain.get("adjustments"):
            saved = brain["adjustments"]
            _adjustments["regime_penalties"].update(saved.get("regime_penalties", {}))
            _adjustments["session_penalties"].update(saved.get("session_penalties", {}))
            _adjustments["pair_penalties"].update(saved.get("pair_penalties", {}))
            _adjustments["global_conf_bump"] = saved.get("global_conf_bump", 0)
        _analysis_log  = brain.get("analysis_log", [])
        _last_analysis = brain.get("last_analysis_ts", 0.0)
        total_adj = (len(_adjustments["regime_penalties"]) +
                     len(_adjustments["session_penalties"]) +
                     len(_adjustments["pair_penalties"]) +
                     (1 if _adjustments["global_conf_bump"] else 0))
        if total_adj:
            print(f"[Brain] Restored — {total_adj} active adjustment(s)", flush=True)
    except Exception as e:
        print(f"[Brain] refresh_from_storage error: {e}", flush=True)


# ── Core analysis ──────────────────────────────────────────────────────────────

def _run_analysis(resolved: list, notify_callback=None):
    global _adjustments, _analysis_log, _last_analysis

    SAST     = timezone(timedelta(hours=2))
    now_sast = datetime.now(SAST).strftime("%d %b %Y %H:%M SAST")

    wins   = [a for a in resolved if a.get("outcome") == "win"]
    losses = [a for a in resolved if a.get("outcome") == "loss"]
    total  = len(resolved)

    findings = []
    changes  = []

    overall_wr = len(wins) / total if total else 0.0

    # ── 1. Overall calibration ─────────────────────────────────────────────────
    if overall_wr < POOR_WIN_RATE and total >= MIN_SAMPLES:
        current_gb = _adjustments.get("global_conf_bump", 0)
        if current_gb < MAX_GLOBAL_BUMP:
            bump = min(GLOBAL_STEP, MAX_GLOBAL_BUMP - current_gb)
            _adjustments["global_conf_bump"] = current_gb + bump
            findings.append(
                f"Overall win rate is {overall_wr:.0%} ({len(wins)}W/{len(losses)}L) — "
                f"confidence bar raised by +{bump}pp to filter weaker setups"
            )
            changes.append(f"Global confidence minimum +{bump}pp (total: +{_adjustments['global_conf_bump']}pp)")

    # ── 2. By regime ───────────────────────────────────────────────────────────
    regime_groups: dict = {}
    for a in resolved:
        r = a.get("market_regime") or "unknown"
        regime_groups.setdefault(r, []).append(a)

    for regime, group in regime_groups.items():
        if len(group) < MIN_SAMPLES:
            continue
        wr = _win_rate(group)
        if wr < POOR_WIN_RATE:
            cur = _adjustments["regime_penalties"].get(regime, 0)
            new = max(MAX_REGIME_PENALTY, cur - REGIME_STEP)
            if new != cur:
                _adjustments["regime_penalties"][regime] = new
                w = len([a for a in group if a.get("outcome") == "win"])
                l = len([a for a in group if a.get("outcome") == "loss"])
                findings.append(
                    f"Regime '{regime}': {wr:.0%} win rate ({w}W/{l}L) — "
                    f"bot keeps losing in this condition"
                )
                changes.append(f"Regime '{regime}' penalty: {new:+d}pp")

    # ── 3. By session ──────────────────────────────────────────────────────────
    session_groups: dict = {}
    for a in resolved:
        s = _normalise_session(a.get("session") or "")
        session_groups.setdefault(s, []).append(a)

    for session, group in session_groups.items():
        if len(group) < MIN_SAMPLES:
            continue
        wr = _win_rate(group)
        if wr < POOR_WIN_RATE:
            cur = _adjustments["session_penalties"].get(session, 0)
            new = max(MAX_SESSION_PENALTY, cur - SESSION_STEP)
            if new != cur:
                _adjustments["session_penalties"][session] = new
                findings.append(
                    f"Session '{session}': {wr:.0%} win rate — "
                    f"signals in this window consistently underperform"
                )
                changes.append(f"Session '{session}' penalty: {new:+d}pp")

    # ── 4. By pair ─────────────────────────────────────────────────────────────
    pair_groups: dict = {}
    for a in resolved:
        p = (a.get("pair") or "UNKNOWN").upper()
        pair_groups.setdefault(p, []).append(a)

    for pair, group in pair_groups.items():
        if len(group) < MIN_SAMPLES:
            continue
        wr = _win_rate(group)
        if wr < POOR_WIN_RATE:
            cur = _adjustments["pair_penalties"].get(pair, 0)
            new = max(MAX_PAIR_PENALTY, cur - PAIR_STEP)
            if new != cur:
                _adjustments["pair_penalties"][pair] = new
                w = len([a for a in group if a.get("outcome") == "win"])
                l = len([a for a in group if a.get("outcome") == "loss"])
                findings.append(
                    f"{pair}: {wr:.0%} win rate ({w}W/{l}L) — "
                    f"pair-specific penalty applied"
                )
                changes.append(f"{pair} confidence penalty: {new:+d}pp")

    # ── 5. Overconfidence calibration ──────────────────────────────────────────
    # 75-85% confidence bucket losing more than expected?
    hc_group = [a for a in resolved if 75 <= a.get("confidence", 0) <= 85]
    if len(hc_group) >= MIN_SAMPLES:
        hc_wins = [a for a in hc_group if a.get("outcome") == "win"]
        hc_wr   = len(hc_wins) / len(hc_group)
        if hc_wr < POOR_WIN_RATE:
            findings.append(
                f"⚠️ 75–85% confidence signals winning only {hc_wr:.0%} of the time "
                f"({len(hc_wins)}W/{len(hc_group) - len(hc_wins)}L) — "
                f"confidence scores are optimistic for current market conditions"
            )

    # ── 6. Score driver forensics (losses only) ────────────────────────────────
    recent_losses = [a for a in losses if a.get("score_breakdown")][-10:]
    if recent_losses:
        component_totals: dict = {}
        for a in recent_losses:
            for k, v in (a.get("score_breakdown") or {}).items():
                if isinstance(v, (int, float)) and k not in ("baseline", "total"):
                    component_totals.setdefault(k, []).append(v)

        FRIENDLY = {
            "regime":          "Market regime score",
            "htf_alignment":   "HTF alignment",
            "momentum":        "Momentum",
            "order_block":     "Order block bonus",
            "liquidity_sweep": "Liquidity sweep bonus",
            "fvg":             "Fair value gap bonus",
            "pullback":        "Pullback quality",
        }
        for k, vals in component_totals.items():
            avg = sum(vals) / len(vals)
            if avg >= 8 and k in FRIENDLY:
                findings.append(
                    f"'{FRIENDLY[k]}' averaging +{avg:.0f}pts in LOSING trades — "
                    f"this signal is being over-weighted"
                )

    # ── Log + save + notify ────────────────────────────────────────────────────
    log_entry = {
        "timestamp":      now_sast,
        "total_resolved": total,
        "wins":           len(wins),
        "losses":         len(losses),
        "win_rate":       round(overall_wr * 100),
        "findings":       findings,
        "changes":        changes,
    }
    _analysis_log.append(log_entry)
    if len(_analysis_log) > 20:
        _analysis_log = _analysis_log[-20:]

    _last_analysis = time.time()
    _save_state()

    if changes:
        print(f"[Brain] Analysis complete — {len(changes)} adjustment(s) applied", flush=True)
        for f in findings:
            print(f"  [Brain] {f}", flush=True)

        if notify_callback:
            msg = (
                "🧠 *Brain Auto-Update*\n\n"
                f"Analysed {total} trades — Win rate: {overall_wr:.0%} "
                f"({len(wins)}W/{len(losses)}L)\n\n"
            )
            if findings:
                msg += "*What went wrong:*\n"
                for f in findings[:4]:
                    msg += f"• {f}\n"
            if changes:
                msg += "\n*Adjustments made:*\n"
                for c in changes:
                    msg += f"⚙️ {c}\n"
            msg += "\nUse /brainreport to see the full picture."

            from config import ADMIN_IDS
            for admin_id in ADMIN_IDS:
                try:
                    notify_callback(admin_id, msg, parse_mode="Markdown")
                except Exception:
                    try:
                        notify_callback(admin_id, msg)
                    except Exception:
                        pass
    else:
        print(f"[Brain] Analysis OK — WR {overall_wr:.0%}, no adjustments needed", flush=True)


def _relax_if_recovering(resolved: list, recent_win: dict):
    """Relax penalties in categories that have recovered to a healthy win rate."""
    changed  = False
    pair     = (recent_win.get("pair")    or "").upper()
    regime   = (recent_win.get("market_regime") or "")
    session  = _normalise_session(recent_win.get("session") or "")

    def _category_wr(key: str, value: str) -> tuple:
        """Returns (win_rate, count) for a specific field value."""
        group = [a for a in resolved if (a.get(key) or "") == value]
        if len(group) < MIN_SAMPLES:
            return 0.0, 0
        return _win_rate(group), len(group)

    # Pair
    if pair and _adjustments["pair_penalties"].get(pair, 0) < 0:
        wr, n = _category_wr("pair", pair)
        if wr >= GOOD_WIN_RATE and n >= MIN_SAMPLES:
            old = _adjustments["pair_penalties"][pair]
            new = min(0, old + RELAX_STEP)
            _adjustments["pair_penalties"][pair] = new
            if new == 0:
                del _adjustments["pair_penalties"][pair]
            print(f"[Brain] {pair} recovering ({wr:.0%} WR) — penalty {old:+d}pp → {new:+d}pp", flush=True)
            changed = True

    # Regime
    if regime and _adjustments["regime_penalties"].get(regime, 0) < 0:
        wr, n = _category_wr("market_regime", regime)
        if wr >= GOOD_WIN_RATE and n >= MIN_SAMPLES:
            old = _adjustments["regime_penalties"][regime]
            new = min(0, old + RELAX_STEP)
            _adjustments["regime_penalties"][regime] = new
            if new == 0:
                del _adjustments["regime_penalties"][regime]
            print(f"[Brain] Regime '{regime}' recovering — penalty {old:+d}pp → {new:+d}pp", flush=True)
            changed = True

    # Session
    if session and _adjustments["session_penalties"].get(session, 0) < 0:
        ses_alerts = [a for a in resolved
                      if _normalise_session(a.get("session") or "") == session]
        if len(ses_alerts) >= MIN_SAMPLES:
            wr = _win_rate(ses_alerts)
            if wr >= GOOD_WIN_RATE:
                old = _adjustments["session_penalties"][session]
                new = min(0, old + RELAX_STEP)
                _adjustments["session_penalties"][session] = new
                if new == 0:
                    del _adjustments["session_penalties"][session]
                print(f"[Brain] Session '{session}' recovering — penalty {old:+d}pp → {new:+d}pp", flush=True)
                changed = True

    # Global confidence bump
    gb = _adjustments.get("global_conf_bump", 0)
    if gb > 0:
        overall_wr = _win_rate(resolved)
        if overall_wr >= GOOD_WIN_RATE:
            new_gb = max(0, gb - RELAX_STEP)
            _adjustments["global_conf_bump"] = new_gb
            print(f"[Brain] Overall WR {overall_wr:.0%} — global confidence bump relaxed "
                  f"+{gb}pp → +{new_gb}pp", flush=True)
            changed = True

    if changed:
        _save_state()


# ── Helpers ────────────────────────────────────────────────────────────────────

def _win_rate(alerts: list) -> float:
    if not alerts:
        return 0.0
    wins = sum(1 for a in alerts if a.get("outcome") == "win")
    return wins / len(alerts)


def _normalise_session(raw: str) -> str:
    s = (raw or "").lower()
    if "overlap" in s:
        return "overlap"
    if "london" in s:
        return "london"
    if "new_york" in s or "new york" in s:
        return "new_york"
    if "asian" in s or "asia" in s:
        return "asian"
    return s or "unknown"


def _save_state():
    try:
        from storage import load_data, save_data
        data = load_data()
        data["adaptive_brain"] = {
            "adjustments":    {
                "regime_penalties":  dict(_adjustments["regime_penalties"]),
                "session_penalties": dict(_adjustments["session_penalties"]),
                "pair_penalties":    dict(_adjustments["pair_penalties"]),
                "global_conf_bump":  _adjustments.get("global_conf_bump", 0),
            },
            "analysis_log":     _analysis_log[-20:],
            "last_analysis_ts": _last_analysis,
        }
        save_data(data)
    except Exception as e:
        print(f"[Brain] _save_state error: {e}", flush=True)
