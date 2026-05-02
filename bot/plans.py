"""
plans.py — Single source of truth for all plan feature permissions and limits.

This is the ONLY place where plan features are defined.
All other modules must use the helper functions here to check access.
No plan behavior should be hardcoded anywhere else.

Feature keys:
  manual_analysis       — can submit chart screenshots for analysis
  scanner_entry_alerts  — receives scanner entry alerts
  scanner_watch_alerts  — receives scanner watch (pre-entry) alerts
  delayed_signals       — signals are intentionally delayed (trial limitation)
  confidence_breakdown  — receives score driver breakdown in signal messages
  premium_narrative     — receives full trade narrative (vs basic)
  daily_summary         — receives daily bot performance summary
  outcome_followups     — receives TP/SL outcome follow-up messages
  highest_quality_filter — scanner only sends quality=strong setups (VIP only)
  priority_alerts        — receives all alerts regardless of pair watchlist (VIP)
"""

from datetime import datetime, timezone, timedelta

SAST = timezone(timedelta(hours=2))

# ── Plan feature matrix ───────────────────────────────────────────────────────

PLAN_FEATURES: dict = {
    "trial": {
        "display_name":           "Trial",
        "manual_analysis":        True,
        "scanner_entry_alerts":   False,
        "scanner_watch_alerts":   False,
        "delayed_signals":        True,
        "confidence_breakdown":   False,
        "premium_narrative":      False,
        "daily_summary":          False,
        "outcome_followups":      False,
        "highest_quality_filter": False,
        "priority_alerts":        False,
        "daily_limit":            3,
    },
    "weekly": {
        "display_name":           "Weekly",
        "manual_analysis":        True,
        "scanner_entry_alerts":   True,
        "scanner_watch_alerts":   False,
        "delayed_signals":        False,
        "confidence_breakdown":   False,
        "premium_narrative":      False,
        "daily_summary":          False,
        "outcome_followups":      True,
        "highest_quality_filter": False,
        "priority_alerts":        False,
        "daily_limit":            10,
    },
    "monthly": {
        "display_name":           "Monthly",
        "manual_analysis":        True,
        "scanner_entry_alerts":   True,
        "scanner_watch_alerts":   True,
        "delayed_signals":        False,
        "confidence_breakdown":   True,
        "premium_narrative":      True,
        "daily_summary":          True,
        "outcome_followups":      True,
        "highest_quality_filter": False,
        "priority_alerts":        False,
        "daily_limit":            30,
    },
    "vip": {
        "display_name":           "VIP",
        "manual_analysis":        True,
        "scanner_entry_alerts":   True,
        "scanner_watch_alerts":   True,
        "delayed_signals":        False,
        "confidence_breakdown":   True,
        "premium_narrative":      True,
        "daily_summary":          True,
        "outcome_followups":      True,
        "highest_quality_filter": True,
        "priority_alerts":        True,
        "daily_limit":            100,
    },
}

# Features shown in /myplan output (human-readable labels in display order)
FEATURE_LABELS: list = [
    ("manual_analysis",        "Chart analysis"),
    ("scanner_entry_alerts",   "Entry alerts"),
    ("scanner_watch_alerts",   "Watch alerts"),
    ("confidence_breakdown",   "Confidence breakdown"),
    ("premium_narrative",      "Full trade narrative"),
    ("outcome_followups",      "Outcome follow-ups"),
    ("daily_summary",          "Daily summary"),
    ("highest_quality_filter", "VIP quality filter"),
    ("priority_alerts",        "Priority alerts"),
]


# ── Core helpers (all modules must use these) ─────────────────────────────────

def get_plan_features(plan: str) -> dict:
    """Returns the feature dict for a plan name (defaults to trial if unknown)."""
    return PLAN_FEATURES.get(plan.lower() if plan else "trial", PLAN_FEATURES["trial"])


def is_user_active(user_id: int) -> bool:
    """
    Returns True only when the user has a valid, non-expired plan.
    This is the ONLY check that should be used to gate any premium action.
    Admins are always considered active.
    """
    from config import ADMIN_IDS
    if user_id in ADMIN_IDS:
        return True
    from storage import load_data
    data = load_data()
    uid  = str(user_id)
    info = data.get("approved_users", {}).get(uid)
    if not info:
        return False
    if not info.get("active", False):
        return False
    expires_raw = info.get("expires_at", "")
    if not expires_raw:
        return False
    try:
        expires = datetime.fromisoformat(expires_raw.replace("Z", "+00:00"))
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) <= expires
    except Exception:
        return False


def get_user_plan_name(user_id: int) -> str:
    """Returns the user's current plan name string, or 'none'."""
    from storage import load_data
    data = load_data()
    uid  = str(user_id)
    info = data.get("approved_users", {}).get(uid, {})
    return info.get("plan", "none")


def get_user_plan_info(user_id: int) -> dict:
    """
    Returns the full approved_user record for a user.
    Includes plan, expires_at, activated_at, active flag.
    Returns {} if not found.
    """
    from storage import load_data
    data = load_data()
    return data.get("approved_users", {}).get(str(user_id), {})


def get_user_plan_features(user_id: int) -> dict:
    """Returns the feature dict for the user's current plan."""
    plan = get_user_plan_name(user_id)
    return get_plan_features(plan)


def user_has_feature(user_id: int, feature: str) -> bool:
    """
    Returns True if:
      1. user has an active plan, AND
      2. their plan includes this feature.
    Admins always have all features.
    """
    from config import ADMIN_IDS
    if user_id in ADMIN_IDS:
        return True
    if not is_user_active(user_id):
        return False
    return bool(get_user_plan_features(user_id).get(feature, False))


def get_user_daily_limit(user_id: int) -> int:
    """Returns the daily analysis limit for the user's current plan."""
    from config import ADMIN_IDS
    if user_id in ADMIN_IDS:
        return 9999
    return get_user_plan_features(user_id).get("daily_limit", 0)


def format_plan_expiry_sast(expires_at_str: str) -> str:
    """Converts an ISO expiry string to a human-readable SAST string."""
    if not expires_at_str:
        return "N/A"
    try:
        dt = datetime.fromisoformat(expires_at_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(SAST).strftime("%d %b %Y %H:%M SAST")
    except Exception:
        return expires_at_str[:19].replace("T", " ")


def format_myplan(user_id: int, daily_usage: int) -> str:
    """
    Generates the /myplan response text for a user.
    Shows plan, status, expiry, usage, and feature list.
    """
    from config import ADMIN_IDS
    if user_id in ADMIN_IDS:
        return (
            "📋 YOUR PLAN\n\n"
            "Plan:   ADMIN\n"
            "Status: ✅ Full access (no expiry)\n"
            "Limit:  Unlimited"
        )

    info = get_user_plan_info(user_id)
    if not info:
        return (
            "❌ No active plan.\n\n"
            "Use /redeem CODE to activate a plan.\n"
            "Use /plans to see available plans."
        )

    plan_name   = info.get("plan", "none")
    active      = is_user_active(user_id)
    expires_str = format_plan_expiry_sast(info.get("expires_at", ""))
    features    = get_plan_features(plan_name)
    daily_limit = features.get("daily_limit", 0)
    display     = features.get("display_name", plan_name.upper())
    status      = "✅ Active" if active else "❌ Expired"

    feature_lines = []
    for key, label in FEATURE_LABELS:
        has = features.get(key, False)
        feature_lines.append(f"  {'✅' if has else '❌'} {label}")

    lines = [
        f"📋 YOUR PLAN\n",
        f"Plan:     {display}",
        f"Status:   {status}",
        f"Expires:  {expires_str}",
        f"Usage:    {daily_usage}/{daily_limit} today\n",
        "Features:",
    ] + feature_lines

    if not active:
        lines += ["", "⚠️ Your plan has expired. Use /redeem CODE to reactivate."]

    return "\n".join(lines)


PLANS_MESSAGE = """📊 CHEFBUNTSA FOREX BOT PLANS

🟢 TRIAL — Free
• 3 days access
• 3 chart analyses per day
• Manual chart analysis only
• Delayed/basic results
• No scanner alerts
• No watch alerts
• No premium breakdown

🟡 WEEKLY — R49
• 7 days access
• 10 chart analyses per day
• Real-time manual analysis
• Scanner entry alerts
• Basic signal explanation
• Outcome follow-up messages
• No watch alerts
• No premium confidence breakdown

🟠 MONTHLY — R99
• 30 days access
• 30 chart analyses per day
• Real-time manual analysis
• Scanner entry alerts
• Scanner watch alerts
• Confidence breakdown
• Full trade narrative
• Outcome follow-up messages
• Daily summary

🔴 VIP — R199
• 30 days access
• 100 chart analyses per day
• Full real-time access
• Entry alerts + watch alerts
• Premium confidence breakdown
• Full trade narrative
• Outcome follow-up messages
• Daily summary
• Strongest filtered setups
• Priority access

💡 To activate a plan:
Use /redeem CODE

📌 To check your plan:
Use /myplan"""
