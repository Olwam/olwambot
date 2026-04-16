import os
import json
import uuid
import time
import threading
from datetime import datetime, timezone

from config import DATA_FILE, MAX_SIGNALS_HISTORY, REJECTION_LOG_LIMIT

DATA_LOCK = threading.RLock()


def _default_data():
    return {
        "users":          [],
        "approved_users": {},
        "codes":          {},
        "signals":        [],
        "stats":          {},
        "watchlists":     {},
        "balances":       {},
        "daily_usage":    {},
        "alert_prefs":    {},
        "scanner_alerts": [],
        "rejection_log":  [],   # track why setups / manual charts were rejected
        "pre_alerts":     [],   # watch-level alerts (forming but not entry-ready)
        "scanner_enabled": True,  # global on/off switch — admin-controlled
        "copy_trading":    {},    # uid_str → copy trading settings + encrypted creds
        "copy_trade_log":  [],    # history of every copy trade executed / rejected
        "adaptive_brain":  {},    # brain adjustments, analysis log, last run timestamp
        "pending_watches": {},    # uid_str → {pair: expiry_ts} — auto-watch from NO TRADE
    }


def load_data() -> dict:
    with DATA_LOCK:
        if not os.path.exists(DATA_FILE):
            return _default_data()
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return _default_data()
            base = _default_data()
            base.update(data)
            return base
        except Exception:
            return _default_data()


def save_data(data: dict):
    with DATA_LOCK:
        tmp_path = f"{DATA_FILE}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, DATA_FILE)


def register_user(chat_id: int):
    data = load_data()
    if chat_id not in data["users"]:
        data["users"].append(chat_id)
        save_data(data)


def save_signal(chat_id: int, analysis: dict, source: str = "manual",
                quality_score: int = None, quality_issues: list = None):
    """
    Save a manual chart-analysis signal.
    source: 'manual' for user-submitted charts, 'scanner' for auto-scanner
    quality_score/quality_issues: from vision quality scorer
    """
    data = load_data()
    data["signals"].append({
        "chat_id":        chat_id,
        "time":           datetime.now(timezone.utc).isoformat(),
        "pair":           analysis.get("pair"),
        "timeframe":      analysis.get("timeframe"),
        "direction":      analysis.get("direction"),
        "entry":          analysis.get("entry"),
        "stop_loss":      analysis.get("stop_loss"),
        "take_profit":    analysis.get("take_profit"),
        "rr":             analysis.get("rr"),
        "confidence":     analysis.get("confidence"),
        "setup_quality":  analysis.get("setup_quality"),
        "market_regime":  analysis.get("market_regime"),
        "news_risk":      analysis.get("news_risk"),
        "htf_bias":       analysis.get("htf_bias"),
        "source":         source,
        "quality_score":  quality_score,
        "quality_issues": quality_issues or [],
        "signal":         analysis,
    })
    if len(data["signals"]) > MAX_SIGNALS_HISTORY:
        data["signals"] = data["signals"][-MAX_SIGNALS_HISTORY:]
    save_data(data)


def update_stats(chat_id: int, win: bool) -> dict:
    data = load_data()
    uid = str(chat_id)
    data["stats"].setdefault(uid, {"wins": 0, "losses": 0})
    if win:
        data["stats"][uid]["wins"] += 1
    else:
        data["stats"][uid]["losses"] += 1
    save_data(data)
    return data["stats"][uid]


def get_stats(chat_id: int) -> dict:
    data = load_data()
    return data["stats"].get(str(chat_id), {"wins": 0, "losses": 0})


def get_user_signals(chat_id: int) -> list:
    data = load_data()
    return [s for s in data["signals"] if s.get("chat_id") == chat_id]


def get_balance(chat_id: int):
    data = load_data()
    return data["balances"].get(str(chat_id))


def set_balance(chat_id: int, balance: float):
    data = load_data()
    data["balances"][str(chat_id)] = balance
    save_data(data)


def get_watchlist(chat_id: int) -> list:
    data = load_data()
    return data["watchlists"].get(str(chat_id), [])


def add_to_watchlist(chat_id: int, pairs: list) -> list:
    data = load_data()
    uid = str(chat_id)
    data["watchlists"].setdefault(uid, [])
    added = []
    for pair in pairs:
        p = pair.upper()
        if p not in data["watchlists"][uid]:
            data["watchlists"][uid].append(p)
            added.append(p)
    save_data(data)
    return added


PENDING_WATCH_TTL = 24 * 3600


def add_pending_watch(chat_id: int, pair: str):
    data = load_data()
    uid = str(chat_id)
    pw = data.setdefault("pending_watches", {})
    pw.setdefault(uid, {})
    pw[uid][pair.upper()] = time.time() + PENDING_WATCH_TTL
    save_data(data)


def get_pending_watch_users(pair: str) -> list:
    data = load_data()
    pair = pair.upper()
    now = time.time()
    users = []
    pw = data.get("pending_watches", {})
    for uid, pairs_dict in pw.items():
        expiry = pairs_dict.get(pair, 0)
        if expiry > now:
            users.append(int(uid))
    return users


def clear_pending_watch(chat_id: int, pair: str):
    data = load_data()
    uid = str(chat_id)
    pw = data.get("pending_watches", {})
    if uid in pw and pair.upper() in pw[uid]:
        del pw[uid][pair.upper()]
        if not pw[uid]:
            del pw[uid]
        save_data(data)


def cleanup_expired_watches():
    data = load_data()
    pw = data.get("pending_watches", {})
    now = time.time()
    changed = False
    empty_uids = []
    for uid, pairs_dict in pw.items():
        expired = [p for p, exp in pairs_dict.items() if exp <= now]
        for p in expired:
            del pairs_dict[p]
            changed = True
        if not pairs_dict:
            empty_uids.append(uid)
    for uid in empty_uids:
        del pw[uid]
        changed = True
    if changed:
        save_data(data)


def remove_from_watchlist(chat_id: int, pairs: list) -> list:
    data = load_data()
    uid = str(chat_id)
    data["watchlists"].setdefault(uid, [])
    removed = []
    for pair in pairs:
        p = pair.upper()
        if p in data["watchlists"][uid]:
            data["watchlists"][uid].remove(p)
            removed.append(p)
    save_data(data)
    return removed


def increment_daily_usage(chat_id: int) -> int:
    data  = load_data()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    uid   = str(chat_id)
    usage = data.get("daily_usage", {})
    if uid not in usage or usage[uid].get("date") != today:
        usage[uid] = {"date": today, "count": 0}
    usage[uid]["count"] += 1
    data["daily_usage"] = usage
    save_data(data)
    return usage[uid]["count"]


def get_daily_usage(chat_id: int) -> int:
    data  = load_data()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    uid   = str(chat_id)
    entry = data.get("daily_usage", {}).get(uid, {})
    if entry.get("date") != today:
        return 0
    return entry.get("count", 0)


# ── Alert preference helpers ──────────────────────────────────────────────────

def get_alert_prefs(chat_id: int) -> dict:
    data = load_data()
    uid  = str(chat_id)
    return data.get("alert_prefs", {}).get(uid, {
        "alerts_on": True,
        "pairs":     [],
        "threshold": 72,
    })


def set_alert_prefs(chat_id: int, prefs: dict):
    data = load_data()
    uid  = str(chat_id)
    data.setdefault("alert_prefs", {})
    existing = data["alert_prefs"].get(uid, {"alerts_on": True, "pairs": [], "threshold": 72})
    existing.update(prefs)
    data["alert_prefs"][uid] = existing
    save_data(data)


# ── Global scanner switch ──────────────────────────────────────────────────────

def get_scanner_enabled() -> bool:
    """Returns True if the auto-scanner is globally enabled."""
    data = load_data()
    return bool(data.get("scanner_enabled", True))


def set_scanner_enabled(enabled: bool):
    """Enable or disable the auto-scanner globally (admin-controlled)."""
    data = load_data()
    data["scanner_enabled"] = enabled
    save_data(data)


# ── Scanner alert outcome tracking ────────────────────────────────────────────

def save_scanner_alert(setup: dict, score_breakdown: dict = None,
                       recipients: list = None) -> str:
    """
    Persists a scanner alert with full metadata + score breakdown.
    recipients: list of chat_ids who received this alert (for follow-up messages).
    Returns the unique alert ID.
    """
    data     = load_data()
    alert_id = str(uuid.uuid4())[:12]

    smc_feat = setup.get("smc_features", {})
    mtf      = setup.get("mtf_alignment", {})

    record = {
        "alert_id":        alert_id,
        "pair":            setup.get("pair"),
        "timeframe":       setup.get("timeframe"),
        "direction":       setup.get("direction"),
        "entry":           setup.get("entry"),
        "stop_loss":       setup.get("stop_loss"),
        "take_profit":     setup.get("take_profit"),
        "rr":              setup.get("rr"),
        "confidence":      setup.get("confidence"),
        "market_regime":   setup.get("market_regime"),
        "session":         setup.get("session"),
        "htf_bias":        setup.get("htf_bias"),
        "htf_timeframe":   setup.get("htf_timeframe"),
        "news_risk":       setup.get("news_risk"),
        "reason":          setup.get("reason"),
        "smc_narrative":   setup.get("smc_narrative", ""),
        "atr":             setup.get("atr"),
        "source":          "scanner",
        # Score component breakdown — shows what drove the confidence
        "score_breakdown": score_breakdown or {},
        # Who received this alert — used for outcome follow-up messages
        "recipients":      recipients or [],
        "timestamp":       datetime.now(timezone.utc).isoformat(),
        "outcome":         "pending",
        "outcome_price":   None,
        "outcome_time":    None,
        "outcome_notes":   "",
        "latency_minutes": None,   # filled when resolved
        # ── SMC analytics (Phase 2) ─────────────────────────────────────────
        "had_liquidity_sweep": smc_feat.get("had_liquidity_sweep", False),
        "had_fvg":             smc_feat.get("had_fvg",             False),
        "had_order_block":     smc_feat.get("had_order_block",     False),
        "fib_zone":            smc_feat.get("fib_zone",            False),
        "had_breaker":         smc_feat.get("had_breaker",         False),
        "smc_trigger":         smc_feat.get("smc_trigger",         "none"),
        "mtf_score":           mtf.get("score",              0),
        "market_intent":       setup.get("market_intent",      ""),
        "market_intent_desc":  setup.get("market_intent_desc", ""),
        "liquidity_sweep":     setup.get("liquidity_sweep",    ""),
        "bos_choch_desc":      setup.get("bos_choch_desc",     ""),
        "liq_quality_label":   setup.get("liq_quality_label",  "none"),
        "mtf_bucket":          mtf.get("bucket",             "neutral"),
        "mtf_aligned_tfs":     mtf.get("aligned_timeframes", []),
        "mtf_bias_by_tf":      mtf.get("bias_by_tf",         {}),
    }

    data.setdefault("scanner_alerts", [])
    data["scanner_alerts"].append(record)
    if len(data["scanner_alerts"]) > 2000:
        data["scanner_alerts"] = data["scanner_alerts"][-2000:]

    save_data(data)
    return alert_id


def save_pre_alert(pair: str, direction: str, confidence: int,
                   regime: str, session: str, reason: str) -> str:
    """Stores a watch-level (pre-alert) record for analytics."""
    data     = load_data()
    alert_id = str(uuid.uuid4())[:12]
    record = {
        "alert_id":  alert_id,
        "pair":      pair,
        "direction": direction,
        "confidence": confidence,
        "regime":    regime,
        "session":   session,
        "reason":    reason,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "type":      "watch",
    }
    data.setdefault("pre_alerts", [])
    data["pre_alerts"].append(record)
    if len(data["pre_alerts"]) > 500:
        data["pre_alerts"] = data["pre_alerts"][-500:]
    save_data(data)
    return alert_id


def get_alert_by_id(alert_id: str) -> dict | None:
    """Returns a scanner alert by its ID, or None if not found."""
    data   = load_data()
    prefix = alert_id.lower().strip()
    for a in data.get("scanner_alerts", []):
        if a.get("alert_id", "").startswith(prefix) or a.get("alert_id", "") == prefix:
            return a
    return None


def get_pending_scanner_alerts() -> list:
    data = load_data()
    return [a for a in data.get("scanner_alerts", []) if a.get("outcome") == "pending"]


def update_alert_outcome(alert_id: str, outcome: str,
                         outcome_price: float = None, notes: str = ""):
    """Resolve an alert's outcome and compute resolution latency."""
    data     = load_data()
    now_iso  = datetime.now(timezone.utc).isoformat()
    for alert in data.get("scanner_alerts", []):
        if alert.get("alert_id") == alert_id:
            alert["outcome"]       = outcome
            alert["outcome_price"] = outcome_price
            alert["outcome_time"]  = now_iso
            alert["outcome_notes"] = notes
            # Compute latency in minutes from alert creation to resolution
            try:
                ts = alert.get("timestamp", "")
                if ts:
                    created = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    resolved = datetime.fromisoformat(now_iso)
                    alert["latency_minutes"] = round(
                        (resolved - created).total_seconds() / 60.0, 1
                    )
            except Exception:
                pass
            break
    save_data(data)


def get_scanner_alerts(pair: str = None, limit: int = 50) -> list:
    data   = load_data()
    alerts = data.get("scanner_alerts", [])
    if pair:
        alerts = [a for a in alerts if a.get("pair", "").upper() == pair.upper()]
    return alerts[-limit:]


def get_scanner_stats(pair: str = None) -> dict:
    """Overall or pair-specific scanner performance statistics."""
    alerts   = get_scanner_alerts(pair=pair, limit=2000)
    resolved = [a for a in alerts if a.get("outcome") in ("win", "loss")]
    pending  = [a for a in alerts if a.get("outcome") == "pending"]
    expired  = [a for a in alerts if a.get("outcome") == "expired"]
    wins     = [a for a in resolved if a.get("outcome") == "win"]
    losses   = [a for a in resolved if a.get("outcome") == "loss"]

    total_resolved = len(resolved)
    win_rate       = round((len(wins) / total_resolved) * 100) if total_resolved else 0

    avg_conf = 0
    if alerts:
        avg_conf = round(sum(a.get("confidence", 0) for a in alerts) / len(alerts))

    # By regime
    regime_wins   = {}
    regime_totals = {}
    for a in resolved:
        r = a.get("market_regime", "unknown")
        regime_totals[r] = regime_totals.get(r, 0) + 1
        if a.get("outcome") == "win":
            regime_wins[r] = regime_wins.get(r, 0) + 1

    regime_stats = {}
    for r, total in regime_totals.items():
        w = regime_wins.get(r, 0)
        regime_stats[r] = {"wins": w, "total": total, "rate": round((w / total) * 100)}

    # By session
    session_wins   = {}
    session_totals = {}
    for a in resolved:
        s = a.get("session", "unknown")
        session_totals[s] = session_totals.get(s, 0) + 1
        if a.get("outcome") == "win":
            session_wins[s] = session_wins.get(s, 0) + 1

    session_stats = {}
    for s, total in session_totals.items():
        w = session_wins.get(s, 0)
        session_stats[s] = {"wins": w, "total": total, "rate": round((w / total) * 100)}

    return {
        "total":          len(alerts),
        "resolved":       total_resolved,
        "wins":           len(wins),
        "losses":         len(losses),
        "pending":        len(pending),
        "expired":        len(expired),
        "win_rate":       win_rate,
        "avg_confidence": avg_conf,
        "by_regime":      regime_stats,
        "by_session":     session_stats,
    }


def get_expectancy_stats(source: str = None) -> dict:
    """
    Calculates expectancy-style analytics across resolved scanner alerts.
    """
    data   = load_data()
    alerts = data.get("scanner_alerts", [])

    if source:
        alerts = [a for a in alerts if a.get("source", "scanner") == source]

    resolved = [a for a in alerts if a.get("outcome") in ("win", "loss")]
    wins     = [a for a in resolved if a.get("outcome") == "win"]
    losses   = [a for a in resolved if a.get("outcome") == "loss"]

    total = len(resolved)
    if total == 0:
        return {
            "total_resolved": 0,
            "win_rate":       0,
            "avg_win_rr":     0,
            "avg_loss_rr":    0,
            "expectancy":     0,
            "by_confidence":  {},
        }

    win_rate = len(wins) / total

    win_rrs    = [a.get("rr", 1.0) for a in wins if a.get("rr")]
    avg_win_rr = round(sum(win_rrs) / len(win_rrs), 2) if win_rrs else 0
    avg_loss_rr = 1.0   # a loss always costs 1 unit of risk

    expectancy = round((win_rate * avg_win_rr) - ((1 - win_rate) * avg_loss_rr), 3)

    buckets = {
        "65–69": [],
        "70–74": [],
        "75–79": [],
        "80–84": [],
        "85+":   [],
    }
    for a in resolved:
        c = a.get("confidence", 0)
        outcome = a.get("outcome")
        if c < 70:
            buckets["65–69"].append(outcome)
        elif c < 75:
            buckets["70–74"].append(outcome)
        elif c < 80:
            buckets["75–79"].append(outcome)
        elif c < 85:
            buckets["80–84"].append(outcome)
        else:
            buckets["85+"].append(outcome)

    by_conf = {}
    for label, outcomes in buckets.items():
        if not outcomes:
            continue
        n  = len(outcomes)
        w  = outcomes.count("win")
        by_conf[label] = {
            "total":    n,
            "wins":     w,
            "win_rate": round((w / n) * 100),
        }

    return {
        "total_resolved": total,
        "win_rate":       round(win_rate * 100),
        "avg_win_rr":     avg_win_rr,
        "avg_loss_rr":    avg_loss_rr,
        "expectancy":     expectancy,
        "by_confidence":  by_conf,
    }


def get_manual_signal_stats() -> dict:
    """Stats for manually submitted chart signals (user photo submissions)."""
    data    = load_data()
    signals = [s for s in data.get("signals", []) if s.get("source") == "manual"]

    total   = len(signals)
    with_dir = [s for s in signals if s.get("direction")]
    buys    = [s for s in with_dir if s.get("direction") == "BUY"]
    sells   = [s for s in with_dir if s.get("direction") == "SELL"]
    no_sig  = [s for s in signals if not s.get("direction")]

    avg_conf = 0
    conf_vals = [s.get("confidence", 0) for s in signals if s.get("confidence")]
    if conf_vals:
        avg_conf = round(sum(conf_vals) / len(conf_vals))

    avg_qs = 0
    qs_vals = [s.get("quality_score", 0) for s in signals if s.get("quality_score")]
    if qs_vals:
        avg_qs = round(sum(qs_vals) / len(qs_vals))

    regime_counts = {}
    for s in signals:
        r = s.get("market_regime") or s.get("signal", {}).get("market_regime", "unknown")
        regime_counts[r] = regime_counts.get(r, 0) + 1

    return {
        "total":             total,
        "signals_sent":      len(with_dir),
        "no_signals":        len(no_sig),
        "buys":              len(buys),
        "sells":             len(sells),
        "avg_confidence":    avg_conf,
        "avg_quality_score": avg_qs,
        "by_regime":         regime_counts,
    }


# ── Rejection tracking ────────────────────────────────────────────────────────

def save_rejection(pair: str, category: str, reason: str, source: str = "manual"):
    """
    Log a rejected setup or chart analysis.
    category: e.g. 'low_quality', 'range_regime', 'news_risk', 'htf_conflict',
              'poor_rr', 'weak_session', 'low_confluence', 'unreadable_chart',
              'no_bias', 'mixed_regime', 'low_atr', 'dead_hours'
    source:   'manual' (user submitted chart) or 'scanner'
    """
    data = load_data()
    data.setdefault("rejection_log", [])
    data["rejection_log"].append({
        "pair":      pair,
        "category":  category,
        "reason":    reason[:200],
        "source":    source,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    # Keep rolling window
    if len(data["rejection_log"]) > REJECTION_LOG_LIMIT:
        data["rejection_log"] = data["rejection_log"][-REJECTION_LOG_LIMIT:]
    save_data(data)


def get_rejections(limit: int = 500) -> list:
    data = load_data()
    return data.get("rejection_log", [])[-limit:]
