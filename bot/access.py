"""
access.py — Code generation, redemption, plan activation, and access checks.

Redemption rules (enforced consistently):
  - Same plan as current active plan: extend expiry from current expiry (not now)
  - Different plan OR expired plan: replace plan and set new expiry from now
  - Admin IDs always pass all checks regardless of plan status
"""

import uuid
from datetime import datetime, timezone, timedelta

from config import ADMIN_IDS
from storage import load_data, save_data
from plans import PLAN_FEATURES, get_plan_features, format_plan_expiry_sast, FEATURE_LABELS

SAST = timezone(timedelta(hours=2))

VALID_PLANS = set(PLAN_FEATURES.keys())


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def generate_code(plan: str, days: int, max_uses: int, created_by: int) -> str:
    """
    Generates an access code and stores it with full metadata.
    Returns the generated code string.
    """
    if plan not in VALID_PLANS:
        raise ValueError(f"Invalid plan '{plan}'. Valid plans: {sorted(VALID_PLANS)}")

    code = uuid.uuid4().hex[:10].upper()
    data = load_data()
    data["codes"][code] = {
        "plan":        plan,
        "days":        int(days),
        "max_uses":    int(max_uses),
        "used_count":  0,
        "active":      True,
        "created_by":  created_by,
        "created_at":  datetime.now(timezone.utc).isoformat(),
        "redeemed_by": [],
    }
    save_data(data)
    return code


def redeem_code(user_id: int, username: str, first_name: str,
                code_str: str) -> tuple[bool, str]:
    """
    Redeems an access code for a user.

    Activation rules:
      - Same plan as current active plan:
          extend expiry from current expiry (reward loyalty)
      - Different plan, OR current plan already expired:
          replace plan and start fresh from now

    Returns (success: bool, message: str).
    The message is user-facing and includes plan details on success.
    """
    data     = load_data()
    code_str = code_str.strip().upper()

    if code_str not in data["codes"]:
        return False, "❌ Invalid code. Please check and try again."

    code_info = data["codes"][code_str]

    if not code_info.get("active", False):
        return False, "❌ This code has been revoked."

    if code_info["used_count"] >= code_info["max_uses"]:
        return False, "❌ This code has already been fully used."

    plan = code_info["plan"]
    days = int(code_info["days"])
    now  = datetime.now(timezone.utc)

    # ── Determine expiry (same-plan extension vs fresh start) ─────────────────
    uid       = str(user_id)
    existing  = data.get("approved_users", {}).get(uid)
    new_start = now

    if existing and existing.get("active") and existing.get("plan") == plan:
        # Same plan, still active: extend from current expiry
        try:
            current_expiry = datetime.fromisoformat(
                existing["expires_at"].replace("Z", "+00:00")
            )
            if current_expiry.tzinfo is None:
                current_expiry = current_expiry.replace(tzinfo=timezone.utc)
            if current_expiry > now:
                new_start = current_expiry   # extend from current expiry
        except Exception:
            pass

    expires_at  = new_start + timedelta(days=days)
    features    = get_plan_features(plan)
    daily_limit = features["daily_limit"]
    display     = features["display_name"]

    # ── Write user record ──────────────────────────────────────────────────────
    data.setdefault("approved_users", {})
    data["approved_users"][uid] = {
        "user_id":      user_id,
        "username":     username or "",
        "first_name":   first_name or "",
        "plan":         plan,
        "activated_at": now.isoformat(),
        "expires_at":   expires_at.isoformat(),
        "daily_limit":  daily_limit,
        "active":       True,
    }

    # ── Update code usage ──────────────────────────────────────────────────────
    code_info["used_count"] += 1
    if user_id not in code_info["redeemed_by"]:
        code_info["redeemed_by"].append(user_id)

    save_data(data)

    # ── Build user-facing success message ──────────────────────────────────────
    expires_sast = expires_at.astimezone(SAST).strftime("%d %b %Y %H:%M SAST")
    extended     = (new_start != now and new_start > now)

    # Feature summary lines (only features that are enabled)
    enabled_features = [
        label for key, label in FEATURE_LABELS
        if features.get(key, False)
    ]
    feature_text = "\n".join(f"  ✅ {f}" for f in enabled_features)

    header = "🔄 Plan extended!" if extended else "✅ Plan activated!"

    return True, (
        f"{header}\n\n"
        f"📋 Plan: {display.upper()}\n"
        f"📅 Expires: {expires_sast}\n"
        f"📊 Daily limit: {daily_limit} analyses\n\n"
        f"Your features:\n{feature_text}\n\n"
        f"Use /myplan to check your status anytime."
    )


def is_approved(user_id: int) -> bool:
    """
    Legacy check — kept for backward compatibility in parts of the code that
    haven't been migrated to plans.is_user_active yet.
    Delegates to plans.is_user_active.
    """
    from plans import is_user_active
    return is_user_active(user_id)


def get_user_plan(user_id: int) -> dict:
    """Returns the approved_user record dict. Empty dict if not found."""
    data = load_data()
    return data.get("approved_users", {}).get(str(user_id), {})


def get_daily_limit(user_id: int) -> int:
    """Returns daily analysis limit for the user's plan."""
    from plans import get_user_daily_limit
    return get_user_daily_limit(user_id)


def revoke_user(user_id: int) -> bool:
    data = load_data()
    uid  = str(user_id)
    if uid in data["approved_users"]:
        data["approved_users"][uid]["active"] = False
        save_data(data)
        return True
    return False


def revoke_code(code_str: str) -> bool:
    data     = load_data()
    code_str = code_str.strip().upper()
    if code_str in data["codes"]:
        data["codes"][code_str]["active"] = False
        save_data(data)
        return True
    return False


def list_approved_users() -> list:
    data = load_data()
    return list(data.get("approved_users", {}).values())


def list_codes() -> list:
    data   = load_data()
    result = []
    for code_str, info in data.get("codes", {}).items():
        entry        = dict(info)
        entry["code"] = code_str
        result.append(entry)
    return result


def get_plan_stats() -> dict:
    """
    Returns a breakdown of users by plan (active vs expired).
    Used by /planstats admin command.
    """
    from plans import is_user_active as _active
    data  = load_data()
    now   = datetime.now(timezone.utc)
    stats = {}

    for uid_str, info in data.get("approved_users", {}).items():
        plan = info.get("plan", "unknown")
        stats.setdefault(plan, {"active": 0, "expired": 0})
        try:
            uid = int(uid_str)
        except ValueError:
            uid = 0
        if _active(uid):
            stats[plan]["active"] += 1
        else:
            stats[plan]["expired"] += 1

    codes      = data.get("codes", {})
    total_codes = len(codes)
    used_codes  = sum(1 for c in codes.values() if c.get("used_count", 0) > 0)
    active_codes = sum(1 for c in codes.values() if c.get("active", False))

    return {
        "by_plan":       stats,
        "total_users":   len(data.get("approved_users", {})),
        "total_codes":   total_codes,
        "used_codes":    used_codes,
        "active_codes":  active_codes,
    }
