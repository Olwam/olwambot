"""
formatters.py — Signal and plan message formatters.

All premium feature gates (confidence_breakdown, premium_narrative) are
enforced here using plans.user_has_feature so there is no accidental leakage.
"""

import re
import math
from datetime import datetime, timezone, timedelta

from storage import get_balance

SAST = timezone(timedelta(hours=2))


def _to_sast_str(dt_str: str) -> str:
    """Convert an ISO/database timestamp string to a SAST-formatted string."""
    if not dt_str or dt_str in ("N/A", ""):
        return "N/A"
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(SAST).strftime("%d %b %Y %H:%M SAST")
    except Exception:
        return dt_str[:19].replace("T", " ")


def estimate_lot_size(symbol: str, entry: float, stop_loss: float,
                      account_balance: float):
    if not account_balance or account_balance <= 0:
        return None, "No balance set."
    stop_distance = abs(entry - stop_loss)
    if stop_distance <= 0:
        return None, "Invalid stop distance."

    risk_amount = account_balance * 0.01
    sym         = re.sub(r"[^A-Z0-9]", "", symbol.upper())

    dollars_per_one_move = None
    if sym == "XAUUSD":
        dollars_per_one_move = 100.0
    elif sym == "XAGUSD":
        dollars_per_one_move = 5000.0
    elif "NAS100" in sym or sym == "NAS" or "USTEC" in sym:
        dollars_per_one_move = 1.0
    elif "US30" in sym or "DJI" in sym or "WS30" in sym:
        dollars_per_one_move = 1.0
    elif len(sym) >= 6 and sym.endswith("JPY"):
        dollars_per_one_move = 100000.0 / max(entry, 1.0)
    elif len(sym) >= 6:
        dollars_per_one_move = 100000.0
    else:
        return None, "Unsupported symbol for lot estimation."

    risk_per_lot = stop_distance * dollars_per_one_move
    if risk_per_lot <= 0:
        return None, "Invalid risk-per-lot."
    lots = risk_amount / risk_per_lot
    if not math.isfinite(lots) or lots <= 0:
        return None, "Lot size calc failed."
    lots = round(max(lots, 0.01), 2)
    if lots > 100:
        return None, "Estimated lot size unrealistic; check balance or symbol specs."
    return lots, "Estimated at 1% risk using standard contract specs."


def _format_score_drivers(score_breakdown: dict) -> str:
    """Renders key score driver components as a compact line."""
    if not score_breakdown:
        return ""
    LABELS = {
        "chart_quality":   "Chart",
        "bias_alignment":  "Bias",
        "htf_alignment":   "HTF",
        "regime":          "Regime",
        "momentum":        "Momentum",
        "rr":              "R:R",
        "session":         "Session",
        "news":            "News",
        "liquidity_sweep": "Sweep",
        "fvg":             "FVG",
        "order_block":     "OB",
        "breaker":         "Breaker",
        "fib_confluence":  "Fib",
        "volatility":      "Vol",
        "eq_levels":       "EqLevels",
        "intent":          "Intent",
        "bos_choch":       "BOS/CHOCH",
        "liq_quality":     "LiqQ",
    }
    parts = []
    for key, label in LABELS.items():
        v = score_breakdown.get(key, 0)
        if not v or abs(v) < 3:
            continue
        sign = "+" if v > 0 else ""
        parts.append(f"{label}({sign}{v})")
    return "Drivers: " + " ".join(parts) if parts else ""


def format_signal_text(analysis: dict, chat_id: int) -> str:
    """
    Formats a signal for display to a specific user.
    Premium features (confidence breakdown, full narrative) are gated by plan.
    Trial users receive a basic result; monthly/VIP get full breakdown.
    """
    from plans import user_has_feature, is_user_active
    from config import ADMIN_IDS

    is_admin_user = chat_id in ADMIN_IDS

    if analysis.get("no_signal"):
        return (
            f"🚫 NO TRADE SIGNAL\n\n"
            f"Pair: {analysis.get('pair', 'N/A')}\n"
            f"Timeframe: {analysis.get('timeframe', 'N/A')}\n"
            f"News Risk: {analysis.get('news_risk', 'N/A')}\n\n"
            f"Reason: {analysis.get('reason', 'No valid setup found.')}"
        )

    balance  = get_balance(chat_id)
    lot_text = ""
    if balance:
        lots, note = estimate_lot_size(
            analysis["pair"], analysis["entry"], analysis["stop_loss"], balance
        )
        if lots:
            lot_text = (
                f"\n\n💰 Lot Size (1% risk on ${balance:,.2f}): {lots} lots\n"
                f"ℹ️ {note}"
            )

    emoji         = "🟢" if analysis["direction"] == "BUY" else "🔴"
    quality_emoji = {
        "strong": "🔥", "good": "✅", "okay": "⚠️", "weak": "❓"
    }.get(analysis.get("setup_quality", "okay"), "⚠️")

    # HTF bias line (shown to all)
    htf_bias = analysis.get("htf_bias", "")
    htf_tf   = analysis.get("htf_timeframe", "")
    htf_line = ""
    if htf_bias and htf_bias not in ("N/A", "", "neutral") and htf_tf:
        htf_emoji = "🟢" if htf_bias == "bullish" else ("🔴" if htf_bias == "bearish" else "⚪")
        htf_line  = f"\n{htf_emoji} HTF ({htf_tf}): {htf_bias.upper()}"

    # ── Plan-gated content ────────────────────────────────────────────────────
    # Confidence breakdown — monthly/vip only
    drivers_line = ""
    if is_admin_user or user_has_feature(chat_id, "confidence_breakdown"):
        bd = analysis.get("score_breakdown")
        if bd:
            drivers_str = _format_score_drivers(bd)
            if drivers_str:
                drivers_line = f"\n📊 {drivers_str}"

    # Premium narrative — monthly/vip only
    # For basic plans (trial/weekly), truncate the reason to essential info
    reason = analysis.get("reason", "")
    if not is_admin_user and not user_has_feature(chat_id, "premium_narrative"):
        # Keep only first sentence for basic plans
        first_sentence = reason.split(".")[0].strip()
        reason = first_sentence + "." if first_sentence else reason

    # Score explanation — only for premium narrative users
    score_exp = ""
    if (is_admin_user or user_has_feature(chat_id, "premium_narrative")):
        score_exp_raw = analysis.get("score_explanation", "")
        if score_exp_raw:
            score_exp = f"\n🔍 {score_exp_raw}"

    intent      = analysis.get("market_intent", "")
    intent_desc = analysis.get("market_intent_desc", "")
    intent_line = ""
    if intent and intent not in ("", "unclear"):
        intent_label = intent.upper()
        intent_line = f"\n🧠 Market Intent: {intent_label}"
        if intent_desc:
            intent_line += f" — {intent_desc}"

    sweep_line = ""
    sweep_desc = analysis.get("liquidity_sweep", "")
    if sweep_desc:
        sweep_line = f"\n🔍 {sweep_desc}"

    notes_parts = []
    if sweep_desc:
        notes_parts.append(sweep_desc)
    if analysis.get("is_watch_alert"):
        notes_parts.append("Setup forming — waiting for confirmation before entry")
    notes_line = ""
    if notes_parts:
        notes_line = "\n\n⚠️ Notes:\n" + "\n".join(f"- {n}" for n in notes_parts)

    return (
        "🚨 CHEFBUNTSA SIGNAL (SNIPER MODE)\n\n"
        f"📊 {analysis['pair']} | {analysis['timeframe']}{htf_line}\n\n"
        f"{emoji} Direction: {analysis['direction']}\n"
        f"🎯 Entry: {analysis['entry']}\n"
        f"🛑 Stop Loss: {analysis['stop_loss']}\n"
        f"✅ TP1 (50% close): {analysis.get('tp1', analysis['take_profit'])}\n"
        f"🏆 TP2 (full target): {analysis['take_profit']}\n"
        f"📌 Move SL to breakeven when TP1 is hit\n"
        f"📐 RR: 1:{analysis['rr']}\n"
        f"📈 Confidence: {analysis['confidence']}%\n"
        f"🌊 Regime: {analysis.get('market_regime', 'N/A')}\n"
        f"{quality_emoji} Quality: {analysis.get('setup_quality', 'N/A')}\n"
        f"📰 News Risk: {analysis.get('news_risk', 'low')}"
        f"{intent_line}"
        f"{sweep_line}"
        f"{drivers_line}"
        f"{score_exp}\n\n"
        f"💡 {reason}"
        f"{notes_line}"
        f"{lot_text}"
    )


def format_plan_info(plan_info: dict, daily_usage: int) -> str:
    """Legacy helper used by /myplan in older code — delegates to plans.format_myplan."""
    if not plan_info:
        return "❌ No active plan. Use /redeem CODE to activate."

    plan        = plan_info.get("plan", "unknown").upper()
    expires_raw = plan_info.get("expires_at", "N/A")
    expires     = _to_sast_str(str(expires_raw)) if expires_raw else "N/A"
    daily_limit = plan_info.get("daily_limit", 0)
    active      = plan_info.get("active", False)
    status      = "✅ Active" if active else "❌ Expired/Revoked"

    return (
        f"📋 YOUR PLAN\n\n"
        f"Plan: {plan}\n"
        f"Status: {status}\n"
        f"Expires: {expires}\n"
        f"Daily Limit: {daily_limit}\n"
        f"Used Today: {daily_usage}/{daily_limit}"
    )
