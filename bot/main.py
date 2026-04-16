import os
import sys
import time
import tempfile
import traceback
import threading
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler

import telebot

from config import (
    TELEGRAM_BOT_TOKEN, PORT, USER_COOLDOWN_SECONDS, ADMIN_IDS,
    SCAN_INTERVAL_SECONDS,
)
from storage import (
    register_user, load_data, save_data, save_signal,
    update_stats, get_stats, get_user_signals, get_balance,
    set_balance, get_watchlist, add_to_watchlist, remove_from_watchlist,
    increment_daily_usage, get_daily_usage,
    get_alert_prefs, set_alert_prefs,
    get_scanner_stats, get_scanner_alerts,
    get_scanner_enabled, set_scanner_enabled,
    add_pending_watch,
)
from access import (
    is_admin, is_approved, generate_code, redeem_code,
    get_user_plan, get_daily_limit, revoke_user, revoke_code,
    list_approved_users, list_codes,
)
from vision import analyze_chart_vision
from decision_engine import run_decision
from news_data import (
    fetch_todays_high_impact_news, format_news_event,
    fetch_calendar_events, parse_event_datetime, event_id,
)
from formatters import format_signal_text
from sessions import get_session_label
from scanner import scan_market_for_alerts
from outcome_checker import run_outcome_checker_loop, check_pending_outcomes

class _BotExceptionHandler(telebot.ExceptionHandler):
    def handle(self, exception):
        print(f"[Handler ERROR] {type(exception).__name__}: {exception}", flush=True)
        traceback.print_exc()
        return True


bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN, exception_handler=_BotExceptionHandler())

# SAST = UTC+2 (South African Standard Time, no daylight saving)
SAST = timezone(timedelta(hours=2))

# ── Cooldown ──────────────────────────────────────────────────────────────────

COOLDOWN_LOCK   = threading.RLock()
LAST_REQUEST_AT = {}


def get_cooldown_remaining(chat_id: int) -> int:
    with COOLDOWN_LOCK:
        last_ts = LAST_REQUEST_AT.get(chat_id)
        if last_ts is None:
            return 0
        elapsed = time.time() - last_ts
        remain  = USER_COOLDOWN_SECONDS - int(elapsed)
        return max(remain, 0)


def mark_request(chat_id: int):
    with COOLDOWN_LOCK:
        LAST_REQUEST_AT[chat_id] = time.time()


def safe_send(chat_id: int, text: str):
    try:
        bot.send_message(chat_id, text)
    except Exception as e:
        print(f"Send error for {chat_id}: {e}", flush=True)


def broadcast(message: str):
    data = load_data()
    for chat_id in data["users"]:
        safe_send(chat_id, message)


# ── Commands ──────────────────────────────────────────────────────────────────

@bot.message_handler(commands=["start"])
def cmd_start(message):
    register_user(message.chat.id)
    uid = message.from_user.id
    if is_approved(uid) or is_admin(uid):
        admin_note = (
            "\n\n🔑 Admin: /gencode /users /codes /revokeuser /revokecode"
            "\n📊 Analytics: /scanstats /pairstats /recentalerts /topsetups"
            "\n🔬 Deep Analytics: /auditalert /expectancy /componentstats"
            "\n   /confidencecal /regimestats /pairsessionstats"
            "\n   /latencystats /rejectionstats /recentrejections"
            "\n⚙️ Tuning: /tuningsummary /tuningsuggestions /tuningconfidence"
            "\n   /tuningregimes /tuningpairsessions /tuningthresholds"
            "\n   /tuningfilters /currentweights"
            "\n🔌 Scanner: /scanneron /scanneroff /scannerstate"
            "\n🔭 Preview: /applytuningpreview /compareweights"
            "\n📊 Intel: /health /heatmap /pnl"
        ) if is_admin(uid) else ""
        bot.reply_to(
            message,
            "👋 Chefbuntsa Forex Bot is live.\n\n"
            "Send a chart screenshot for AI-powered analysis.\n\n"
            "Commands:\n"
            "/news — Today's high-impact news\n"
            "/alerts on|off — Enable or disable auto alerts\n"
            "/setpairs EURUSD XAUUSD — Set your alert pairs\n"
            "/mypairs — View your alert pairs\n"
            "/alertthreshold 75 — Set minimum confidence for alerts\n"
            "/watch EURUSD — Add to watchlist\n"
            "/lot 1000 — Set balance for lot sizing\n"
            "/win / /loss — Log trade result\n"
            "/stats — View your stats\n"
            "/myplan — View your plan\n"
            "/help — Full help\n\n"
            "🔑 Need an access code? Contact @chefbuntsa"
            f"{admin_note}"
        )
    else:
        bot.reply_to(
            message,
            "👋 Welcome to Chefbuntsa Forex Bot.\n\n"
            "🔒 Private bot — you need an access code.\n"
            "Use: /redeem YOUR_CODE\n\n"
            "📋 View available plans: /plans\n"
            "🔑 Need a code? Contact @chefbuntsa"
        )


@bot.message_handler(commands=["help"])
def cmd_help(message):
    register_user(message.chat.id)
    bot.reply_to(
        message,
        "📖 How to use the bot\n\n"
        "1) Open MT4 / MT5 / TradingView\n"
        "2) Screenshot your chart (M15/M30/H1)\n"
        "3) Send it here for analysis\n\n"
        "The bot combines:\n"
        "• AI chart reading (OpenAI vision)\n"
        "• Live market data + EMAs\n"
        "• Higher timeframe confirmation\n"
        "• Session awareness\n"
        "• News risk filtering\n"
        "• Confidence scoring engine\n\n"
        "Auto-Alert Commands:\n"
        "/alerts on — Enable proactive market alerts\n"
        "/alerts off — Disable alerts\n"
        "/setpairs EURUSD XAUUSD — Pairs to alert on\n"
        "/mypairs — View your alert pairs\n"
        "/alertthreshold 75 — Min confidence for alerts\n\n"
        "Watchlist:\n"
        "/watch EURUSD XAUUSD — Add pairs\n"
        "/unwatch EURUSD — Remove pair\n\n"
        "Scheduled Alerts:\n"
        "• 08:30 SAST — Daily brief\n"
        "• 09:00 SAST — London open\n"
        "• 14:30 SAST — New York open\n"
        "• 18:00 SAST — London close\n"
        "• Every 15 min — Smart market scan\n\n"
        "⚠️ Educational tool only. Not financial advice."
    )


@bot.message_handler(commands=["redeem"])
def cmd_redeem(message):
    register_user(message.chat.id)
    parts = message.text.strip().split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(
            message,
            "Usage: /redeem YOUR_CODE\n\n"
            "Get a code from @chefbuntsa or use /plans to see available plans."
        )
        return
    code_str = parts[1].strip()
    user     = message.from_user
    ok, msg  = redeem_code(user.id, user.username or "", user.first_name or "", code_str)
    if ok:
        bot.reply_to(message, msg)
    else:
        bot.reply_to(message, f"{msg}\n\nContact @chefbuntsa to get a valid code.")


@bot.message_handler(commands=["myplan"])
def cmd_myplan(message):
    register_user(message.chat.id)
    from plans import format_myplan
    uid   = message.from_user.id
    usage = get_daily_usage(uid)
    bot.reply_to(message, format_myplan(uid, usage))


@bot.message_handler(commands=["plans"])
def cmd_plans(message):
    register_user(message.chat.id)
    from plans import PLANS_MESSAGE
    bot.reply_to(message, PLANS_MESSAGE)


# ── Alert preference commands ─────────────────────────────────────────────────

@bot.message_handler(commands=["alerts"])
def cmd_alerts(message):
    register_user(message.chat.id)
    uid   = message.from_user.id
    parts = message.text.strip().lower().split()

    if not (is_approved(uid) or is_admin(uid)):
        bot.reply_to(message, "🔒 You need an active plan to use alerts.")
        return

    if len(parts) < 2 or parts[1] not in ("on", "off"):
        prefs = get_alert_prefs(uid)
        status = "ON ✅" if prefs.get("alerts_on", True) else "OFF ❌"
        bot.reply_to(message, f"Auto alerts are currently: {status}\n\nUsage: /alerts on  or  /alerts off")
        return

    alerts_on = parts[1] == "on"
    set_alert_prefs(uid, {"alerts_on": alerts_on})
    status = "enabled ✅" if alerts_on else "disabled ❌"
    bot.reply_to(message, f"Auto market alerts {status}.\n\nUse /setpairs to control which pairs you receive alerts for.")


@bot.message_handler(commands=["setpairs"])
def cmd_setpairs(message):
    register_user(message.chat.id)
    uid   = message.from_user.id
    parts = message.text.strip().upper().split()

    if not (is_approved(uid) or is_admin(uid)):
        bot.reply_to(message, "🔒 You need an active plan to set alert pairs.")
        return

    if len(parts) < 2:
        bot.reply_to(message, "Usage: /setpairs EURUSD XAUUSD GBPUSD\n\nSets which pairs you receive auto alerts for.")
        return

    pairs = [p for p in parts[1:] if p.isalpha() and len(p) >= 5]
    if not pairs:
        bot.reply_to(message, "❌ No valid pairs provided. Example: /setpairs EURUSD XAUUSD")
        return

    set_alert_prefs(uid, {"pairs": pairs})
    bot.reply_to(message, f"✅ Alert pairs set to: {', '.join(pairs)}\n\nYou'll receive auto alerts for these pairs when strong setups appear.")


@bot.message_handler(commands=["mypairs"])
def cmd_mypairs(message):
    register_user(message.chat.id)
    uid   = message.from_user.id
    prefs = get_alert_prefs(uid)
    pairs = prefs.get("pairs", [])
    threshold = prefs.get("threshold", 72)
    alerts_on = prefs.get("alerts_on", True)
    status    = "ON ✅" if alerts_on else "OFF ❌"

    wl = get_watchlist(uid)

    lines = [f"📊 Alert Settings\n", f"Alerts: {status}", f"Min Confidence: {threshold}%"]
    if pairs:
        lines.append(f"Alert Pairs: {', '.join(pairs)}")
    else:
        lines.append("Alert Pairs: All default pairs (EURUSD, GBPUSD, USDJPY, XAUUSD)")
    if wl:
        lines.append(f"Watchlist: {', '.join(wl)}")

    bot.reply_to(message, "\n".join(lines))


@bot.message_handler(commands=["alertthreshold"])
def cmd_alertthreshold(message):
    register_user(message.chat.id)
    uid   = message.from_user.id
    parts = message.text.strip().split()

    if not (is_approved(uid) or is_admin(uid)):
        bot.reply_to(message, "🔒 You need an active plan.")
        return

    if len(parts) < 2:
        prefs     = get_alert_prefs(uid)
        threshold = prefs.get("threshold", 72)
        bot.reply_to(message, f"Current alert threshold: {threshold}%\n\nUsage: /alertthreshold 75\nRange: 60–90")
        return

    try:
        val = int(parts[1])
        if val < 60 or val > 90:
            raise ValueError
    except ValueError:
        bot.reply_to(message, "❌ Threshold must be between 60 and 90. Example: /alertthreshold 75")
        return

    set_alert_prefs(uid, {"threshold": val})
    bot.reply_to(message, f"✅ Alert threshold set to {val}%.\nYou'll only receive alerts with confidence ≥ {val}%.")


# ── Admin commands ────────────────────────────────────────────────────────────

@bot.message_handler(commands=["gencode"])
def cmd_gencode(message):
    from plans import get_plan_features, FEATURE_LABELS
    uid = message.from_user.id
    if not is_admin(uid):
        bot.reply_to(message, "❌ Admin only.")
        return
    parts = message.text.strip().split()
    if len(parts) < 4:
        bot.reply_to(
            message,
            "Usage: /gencode PLAN DAYS MAXUSES\n"
            "Example: /gencode monthly 30 5\n\n"
            "Plans: trial, weekly, monthly, vip"
        )
        return
    plan = parts[1].lower()
    if plan not in ("trial", "weekly", "monthly", "vip"):
        bot.reply_to(message, "❌ Invalid plan. Use: trial, weekly, monthly, vip")
        return
    try:
        days     = int(parts[2])
        max_uses = int(parts[3])
        if days <= 0 or max_uses <= 0:
            raise ValueError
    except ValueError:
        bot.reply_to(message, "❌ Days and max uses must be positive numbers.")
        return

    code     = generate_code(plan, days, max_uses, uid)
    features = get_plan_features(plan)

    enabled = [label for key, label in FEATURE_LABELS if features.get(key, False)]
    feat_text = "\n".join(f"  ✅ {f}" for f in enabled) if enabled else "  (none)"

    bot.reply_to(
        message,
        f"🔑 CODE GENERATED\n\n"
        f"Code:      {code}\n"
        f"Plan:      {features['display_name'].upper()}\n"
        f"Duration:  {days} days\n"
        f"Max Uses:  {max_uses}\n"
        f"Daily Limit: {features['daily_limit']}\n\n"
        f"Includes:\n{feat_text}"
    )


@bot.message_handler(commands=["planstats"])
def cmd_planstats(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "❌ Admin only.")
        return
    from access import get_plan_stats
    stats = get_plan_stats()
    lines = ["📊 PLAN STATS\n"]
    plan_order = ["trial", "weekly", "monthly", "vip"]
    for plan in plan_order:
        info = stats["by_plan"].get(plan, {"active": 0, "expired": 0})
        total = info["active"] + info["expired"]
        if total == 0:
            continue
        lines.append(
            f"{plan.upper()}: {info['active']} active, {info['expired']} expired"
        )
    if not any(stats["by_plan"].values()):
        lines.append("No users yet.")
    lines += [
        f"\nTotal users:  {stats['total_users']}",
        f"Total codes:  {stats['total_codes']}",
        f"Active codes: {stats['active_codes']}",
        f"Used codes:   {stats['used_codes']}",
    ]
    bot.reply_to(message, "\n".join(lines))


@bot.message_handler(commands=["revokeuser"])
def cmd_revokeuser(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "❌ Admin only.")
        return
    parts = message.text.strip().split()
    if len(parts) < 2:
        bot.reply_to(message, "Usage: /revokeuser USER_ID")
        return
    try:
        target = int(parts[1])
    except ValueError:
        bot.reply_to(message, "❌ Invalid user ID.")
        return
    if revoke_user(target):
        bot.reply_to(message, f"✅ User {target} revoked.")
    else:
        bot.reply_to(message, "❌ User not found.")


@bot.message_handler(commands=["revokecode"])
def cmd_revokecode(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "❌ Admin only.")
        return
    parts = message.text.strip().split()
    if len(parts) < 2:
        bot.reply_to(message, "Usage: /revokecode CODE")
        return
    if revoke_code(parts[1]):
        bot.reply_to(message, "✅ Code revoked.")
    else:
        bot.reply_to(message, "❌ Code not found.")


@bot.message_handler(commands=["users"])
def cmd_users(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "❌ Admin only.")
        return
    users = list_approved_users()
    if not users:
        bot.reply_to(message, "No approved users yet.")
        return
    lines = ["👥 APPROVED USERS\n"]
    for u in users[:30]:
        status = "✅" if u.get("active") else "❌"
        name   = u.get("first_name") or u.get("username") or str(u.get("user_id"))
        plan   = u.get("plan", "?").upper()
        lines.append(f"{status} {name} — {plan} (ID: {u.get('user_id')})")
    bot.reply_to(message, "\n".join(lines))


@bot.message_handler(commands=["codes"])
def cmd_codes(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "❌ Admin only.")
        return
    codes = list_codes()
    if not codes:
        bot.reply_to(message, "No codes generated yet.")
        return
    lines = ["🔑 CODES\n"]
    for c in codes[:30]:
        status = "✅" if c.get("active") else "❌"
        lines.append(
            f"{status} {c['code']} — {c.get('plan','?').upper()} | "
            f"Uses: {c.get('used_count',0)}/{c.get('max_uses',0)}"
        )
    bot.reply_to(message, "\n".join(lines))


# ── Admin analytics commands ──────────────────────────────────────────────────

@bot.message_handler(commands=["scanstats"])
def cmd_scanstats(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "❌ Admin only.")
        return
    stats = get_scanner_stats()
    wr    = stats["win_rate"]
    lines = [
        "📊 SCANNER PERFORMANCE\n",
        f"Total Alerts: {stats['total']}",
        f"Resolved: {stats['resolved']}",
        f"Wins: {stats['wins']}  Losses: {stats['losses']}",
        f"Pending: {stats['pending']}  Expired: {stats['expired']}",
        f"Win Rate: {wr}%",
        f"Avg Confidence: {stats['avg_confidence']}%",
    ]
    if stats["by_regime"]:
        lines.append("\nBy Regime:")
        for r, d in stats["by_regime"].items():
            lines.append(f"  {r}: {d['wins']}W/{d['total']}T ({d['rate']}%)")
    if stats["by_session"]:
        lines.append("\nBy Session:")
        for s, d in stats["by_session"].items():
            short = s.split()[0] if s else "?"
            lines.append(f"  {short}: {d['wins']}W/{d['total']}T ({d['rate']}%)")
    bot.reply_to(message, "\n".join(lines))


@bot.message_handler(commands=["pairstats"])
def cmd_pairstats(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "❌ Admin only.")
        return
    parts = message.text.strip().upper().split()
    if len(parts) < 2:
        bot.reply_to(message, "Usage: /pairstats EURUSD")
        return
    pair  = parts[1].replace("/", "").replace("-", "")
    stats = get_scanner_stats(pair=pair)
    if stats["total"] == 0:
        bot.reply_to(message, f"No scanner history for {pair} yet.")
        return
    lines = [
        f"📊 {pair} SCANNER STATS\n",
        f"Total Alerts: {stats['total']}",
        f"Resolved: {stats['resolved']}",
        f"Wins: {stats['wins']}  Losses: {stats['losses']}",
        f"Win Rate: {stats['win_rate']}%",
        f"Avg Confidence: {stats['avg_confidence']}%",
    ]
    if stats["by_regime"]:
        lines.append("\nBy Regime:")
        for r, d in stats["by_regime"].items():
            lines.append(f"  {r}: {d['wins']}W/{d['total']}T ({d['rate']}%)")
    bot.reply_to(message, "\n".join(lines))


@bot.message_handler(commands=["recentalerts"])
def cmd_recentalerts(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "❌ Admin only.")
        return
    parts = message.text.strip().split()
    n     = 10
    if len(parts) >= 2:
        try:
            n = min(int(parts[1]), 20)
        except ValueError:
            pass
    alerts = get_scanner_alerts(limit=n)
    if not alerts:
        bot.reply_to(message, "No scanner alerts recorded yet.")
        return
    lines = [f"📋 LAST {len(alerts)} SCANNER ALERTS\n"]
    outcome_symbols = {"win": "✅", "loss": "❌", "pending": "⏳", "expired": "⌛"}
    for a in reversed(alerts):
        ts      = a.get("timestamp", "")[:16].replace("T", " ")
        oc      = a.get("outcome", "pending")
        oc_icon = outcome_symbols.get(oc, "?")
        lines.append(
            f"{oc_icon} {a.get('pair')} {a.get('direction')} "
            f"| conf={a.get('confidence')}% rr={a.get('rr')} "
            f"| {a.get('market_regime','')} | {ts}"
        )
    bot.reply_to(message, "\n".join(lines))


@bot.message_handler(commands=["topsetups"])
def cmd_topsetups(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "❌ Admin only.")
        return
    alerts = get_scanner_alerts(limit=200)
    if not alerts:
        bot.reply_to(message, "No scanner alerts recorded yet.")
        return
    top = sorted(alerts, key=lambda a: a.get("confidence", 0), reverse=True)[:10]
    lines = ["🏆 TOP 10 SETUPS BY CONFIDENCE\n"]
    outcome_symbols = {"win": "✅", "loss": "❌", "pending": "⏳", "expired": "⌛"}
    for a in top:
        ts      = a.get("timestamp", "")[:16].replace("T", " ")
        oc      = a.get("outcome", "pending")
        oc_icon = outcome_symbols.get(oc, "?")
        lines.append(
            f"{oc_icon} {a.get('pair')} {a.get('direction')} "
            f"conf={a.get('confidence')}% rr={a.get('rr')} | {ts}"
        )
    bot.reply_to(message, "\n".join(lines))


@bot.message_handler(commands=["auditalert"])
def cmd_auditalert(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "❌ Admin only.")
        return
    parts = message.text.strip().split()
    if len(parts) < 2:
        bot.reply_to(message, "Usage: /auditalert ALERT_ID\nGet the ID from /recentalerts")
        return
    from storage import get_alert_by_id
    alert = get_alert_by_id(parts[1].strip())
    if not alert:
        bot.reply_to(message, f"❌ Alert '{parts[1]}' not found.")
        return

    outcome_symbols = {"win": "✅", "loss": "❌", "pending": "⏳", "expired": "⌛"}
    oc      = alert.get("outcome", "pending")
    oc_icon = outcome_symbols.get(oc, "?")
    ts      = alert.get("timestamp", "")[:16].replace("T", " ")

    lines = [
        f"🔍 ALERT AUDIT — {alert.get('alert_id')}\n",
        f"Pair: {alert.get('pair')} | TF: {alert.get('timeframe')}",
        f"Direction: {alert.get('direction')} | Entry: {alert.get('entry')}",
        f"SL: {alert.get('stop_loss')} | TP: {alert.get('take_profit')} | RR: {alert.get('rr')}",
        f"Confidence: {alert.get('confidence')}% | ATR: {alert.get('atr','-')}",
        f"Regime: {alert.get('market_regime')} | Session: {alert.get('session','-')}",
        f"HTF: {alert.get('htf_bias','N/A')} ({alert.get('htf_timeframe','-')})",
        f"News Risk: {alert.get('news_risk','low')}",
        f"Timestamp: {ts}",
        f"Outcome: {oc_icon} {oc.upper()}",
    ]
    if alert.get("outcome_price"):
        lines.append(f"Outcome Price: {alert.get('outcome_price')}")
    if alert.get("outcome_time"):
        lines.append(f"Resolved At: {alert.get('outcome_time','')[:16]}")
    if alert.get("outcome_notes"):
        lines.append(f"Notes: {alert.get('outcome_notes')}")

    # Score breakdown
    bd = alert.get("score_breakdown", {})
    if bd:
        lines.append("\n📊 Score Breakdown:")
        lines.append(f"  Baseline:      {bd.get('baseline', 42):+}")
        for key in ("regime", "momentum", "ema_slope", "htf_alignment",
                    "session", "rr", "news", "chart_quality", "bias_alignment",
                    "quality_penalty"):
            v = bd.get(key)
            if v is not None and v != 0:
                label = key.replace("_", " ").title()
                lines.append(f"  {label:<20} {v:+}")
        lines.append(f"  ─────────────────────")
        lines.append(f"  Total:               {bd.get('total', alert.get('confidence',0))}")

    lines.append(f"\n💡 Reason: {alert.get('reason', '-')}")
    bot.reply_to(message, "\n".join(lines))


# ── New analytics commands ────────────────────────────────────────────────────

@bot.message_handler(commands=["componentstats"])
def cmd_componentstats(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "❌ Admin only.")
        return
    from analytics import get_component_stats, KNOWN_COMPONENTS
    parts      = message.text.strip().split()
    comp_filter = parts[1].lower() if len(parts) >= 2 else None

    if comp_filter and comp_filter not in KNOWN_COMPONENTS:
        bot.reply_to(
            message,
            f"❌ Unknown component '{comp_filter}'.\n"
            f"Available: {', '.join(KNOWN_COMPONENTS)}"
        )
        return

    results = get_component_stats(comp_filter)
    if not results:
        bot.reply_to(message, "📉 No resolved alerts with score breakdowns yet.")
        return

    lines = ["📊 COMPONENT PERFORMANCE ANALYTICS\n"]
    if comp_filter:
        lines[0] = f"📊 COMPONENT: {comp_filter.upper()}\n"

    for comp, groups in results.items():
        lines.append(f"▸ {comp.replace('_', ' ').title()}")
        for grp, d in groups.items():
            marker = "+" if grp == "positive" else ("−" if grp == "negative" else "○")
            exp    = d.get("expectancy", 0)
            exp_icon = "✅" if exp > 0 else "❌"
            lines.append(
                f"  [{marker}] {grp}: {d['wins']}W/{d['total']}T "
                f"({d['win_rate']}%) {exp_icon} E={exp:+.2f}R"
            )

    bot.reply_to(message, "\n".join(lines))


@bot.message_handler(commands=["confidencecal"])
def cmd_confidencecal(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "❌ Admin only.")
        return
    from analytics import get_confidence_calibration
    results = get_confidence_calibration()
    if not results:
        bot.reply_to(message, "📉 No resolved alerts to calibrate against yet.")
        return

    lines = ["🎯 CONFIDENCE CALIBRATION\n"]
    lines.append(f"{'Band':<8} {'Total':>5} {'W':>4} {'L':>4} {'WR':>5} {'AvgRR':>6} {'Exp':>7}")
    lines.append("─" * 42)
    for band in ["65–69", "70–74", "75–79", "80–84", "85+"]:
        d = results.get(band)
        if not d:
            continue
        exp_sign = "+" if d["expectancy"] >= 0 else ""
        lines.append(
            f"{band:<8} {d['total']:>5} {d['wins']:>4} {d['losses']:>4} "
            f"{d['win_rate']:>4}% {d['avg_rr']:>6.2f} "
            f"{exp_sign}{d['expectancy']:.3f}R"
        )

    lines.append("\n📌 Tip: Win rate should rise with each confidence band.")
    bot.reply_to(message, "\n".join(lines))


@bot.message_handler(commands=["regimestats"])
def cmd_regimestats(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "❌ Admin only.")
        return
    from analytics import get_regime_expectancy
    results = get_regime_expectancy()
    if not results:
        bot.reply_to(message, "📉 No resolved alerts yet.")
        return

    lines = ["🌊 REGIME EXPECTANCY\n"]
    for regime, d in sorted(results.items(), key=lambda x: -x[1]["win_rate"]):
        exp_icon = "✅" if d["expectancy"] > 0 else "❌"
        lines.append(
            f"▸ {regime.upper():<12} "
            f"{d['wins']}W/{d['total']}T ({d['win_rate']}%) "
            f"{exp_icon} E={d['expectancy']:+.3f}R AvgRR={d['avg_rr']}"
        )

    lines.append("\n📌 Regimes with positive expectancy are worth keeping.")
    bot.reply_to(message, "\n".join(lines))


@bot.message_handler(commands=["pairsessionstats"])
def cmd_pairsessionstats(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "❌ Admin only.")
        return
    from analytics import get_pair_session_stats
    parts       = message.text.strip().upper().split()
    pair_filter = parts[1].replace("/", "") if len(parts) >= 2 else None
    results     = get_pair_session_stats(pair_filter)

    if not results:
        pair_msg = f" for {pair_filter}" if pair_filter else ""
        bot.reply_to(message, f"📉 No resolved alerts{pair_msg} yet.")
        return

    header = f"📊 PAIR × SESSION STATS{f' — {pair_filter}' if pair_filter else ''}\n"
    lines  = [header]
    shown  = 0
    for combo, d in results.items():
        if shown >= 20:
            lines.append(f"... and {len(results) - shown} more")
            break
        exp_icon = "✅" if d["expectancy"] > 0 else "❌"
        lines.append(
            f"▸ {combo}\n"
            f"  {d['wins']}W/{d['total']}T ({d['win_rate']}%) "
            f"{exp_icon} E={d['expectancy']:+.3f}R"
        )
        shown += 1

    bot.reply_to(message, "\n".join(lines))


@bot.message_handler(commands=["latencystats"])
def cmd_latencystats(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "❌ Admin only.")
        return
    from analytics import get_latency_stats
    stats = get_latency_stats()

    if stats["total_resolved"] == 0:
        bot.reply_to(message, "📉 No resolved alerts with latency data yet.\nData will appear once alerts are resolved.")
        return

    lines = [
        "⏱ OUTCOME LATENCY STATS\n",
        f"Resolved Alerts: {stats['total_resolved']}",
        f"Overall Avg: {stats['overall_avg_minutes']} min ({stats['overall_avg_hours']}h)\n",
    ]
    for oc, d in sorted(stats["by_outcome"].items()):
        emoji = {"win": "✅", "loss": "❌", "expired": "⌛"}.get(oc, "•")
        lines.append(
            f"{emoji} {oc.upper()} ({d['count']} alerts)\n"
            f"   Avg: {d['avg_minutes']}m ({d['avg_hours']}h) | "
            f"Min: {d['min_minutes']}m | Max: {d['max_minutes']}m"
        )

    lines.append("\n📌 Long win latency may mean TPs are too ambitious.")
    lines.append("📌 Short loss latency may mean SLs are too tight.")
    bot.reply_to(message, "\n".join(lines))


@bot.message_handler(commands=["rejectionstats"])
def cmd_rejectionstats(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "❌ Admin only.")
        return
    from analytics import get_rejection_stats
    stats = get_rejection_stats()

    if stats["total"] == 0:
        bot.reply_to(message, "📉 No rejections logged yet.\nThey are recorded as setups are analysed.")
        return

    lines = [f"🚫 REJECTION ANALYTICS (last {stats['total']} events)\n"]
    by_src = stats.get("by_source", {})
    lines.append(
        f"Source: scanner={by_src.get('scanner', 0)} | manual={by_src.get('manual', 0)}\n"
    )
    lines.append("By Category:")
    for cat, d in list(stats["by_category"].items())[:15]:
        label = cat.replace("_", " ").title()
        lines.append(
            f"  {label:<22} {d['count']:>4} ({d['pct']}%)  "
            f"[S:{d['scanner']} M:{d['manual']}]"
        )

    lines.append("\n📌 High counts = filter is active. Low edge filters worth reviewing.")
    bot.reply_to(message, "\n".join(lines))


@bot.message_handler(commands=["recentrejections"])
def cmd_recentrejections(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "❌ Admin only.")
        return
    from analytics import get_recent_rejections
    parts = message.text.strip().split()
    n     = 10
    if len(parts) >= 2:
        try:
            n = min(int(parts[1]), 25)
        except ValueError:
            pass

    items = get_recent_rejections(n)
    if not items:
        bot.reply_to(message, "📉 No rejections logged yet.")
        return

    lines = [f"🚫 LAST {len(items)} REJECTIONS\n"]
    source_icons = {"scanner": "🤖", "manual": "📸"}
    for r in items:
        ts    = r.get("timestamp", "")[:16].replace("T", " ")
        icon  = source_icons.get(r.get("source", "manual"), "•")
        cat   = r.get("category", "?").replace("_", " ").title()
        pair  = r.get("pair", "?")
        reason = r.get("reason", "")[:60]
        lines.append(f"{icon} {pair} [{cat}] {ts}\n   {reason}")

    bot.reply_to(message, "\n".join(lines))


@bot.message_handler(commands=["expectancy"])
def cmd_expectancy(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "❌ Admin only.")
        return
    from storage import get_expectancy_stats, get_manual_signal_stats
    scanner_stats = get_expectancy_stats(source="scanner")
    manual_stats  = get_manual_signal_stats()

    lines = ["📐 EXPECTANCY ANALYTICS\n"]

    # Scanner stats
    lines.append("🤖 Scanner Alerts:")
    if scanner_stats["total_resolved"] == 0:
        lines.append("  No resolved alerts yet.")
    else:
        exp = scanner_stats["expectancy"]
        exp_icon = "✅" if exp > 0 else "❌"
        lines.append(f"  Resolved: {scanner_stats['total_resolved']}")
        lines.append(f"  Win Rate: {scanner_stats['win_rate']}%")
        lines.append(f"  Avg Win RR: {scanner_stats['avg_win_rr']}")
        lines.append(f"  {exp_icon} Expectancy: {exp:+.3f}R per trade")
        if scanner_stats["by_confidence"]:
            lines.append("  By Confidence:")
            for label, d in sorted(scanner_stats["by_confidence"].items()):
                lines.append(f"    {label}: {d['wins']}W/{d['total']}T ({d['win_rate']}%)")

    lines.append("")

    # Manual submission stats
    lines.append("📸 Manual Submissions:")
    if manual_stats["total"] == 0:
        lines.append("  No manual signals recorded yet.")
    else:
        lines.append(f"  Total: {manual_stats['total']}")
        lines.append(f"  Signals Issued: {manual_stats['signals_sent']}")
        lines.append(f"  No-Signal Responses: {manual_stats['no_signals']}")
        lines.append(f"  Avg Chart Quality: {manual_stats['avg_quality_score']}/100")
        lines.append(f"  Avg Confidence: {manual_stats['avg_confidence']}%")
        if manual_stats["by_regime"]:
            lines.append("  By Regime:")
            for r, count in manual_stats["by_regime"].items():
                lines.append(f"    {r}: {count}")

    bot.reply_to(message, "\n".join(lines))


# ── General commands ──────────────────────────────────────────────────────────

@bot.message_handler(commands=["news"])
def cmd_news(message):
    register_user(message.chat.id)
    events = fetch_todays_high_impact_news()
    if not events:
        bot.reply_to(message, "✅ No high-impact news scheduled today.")
        return
    lines = ["📅 Today's High-Impact News:"]
    for e in events:
        lines.append("")
        lines.append(format_news_event(e))
    bot.reply_to(message, "\n".join(lines))


@bot.message_handler(commands=["lot"])
def cmd_lot(message):
    register_user(message.chat.id)
    parts = message.text.strip().split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, "Usage: /lot 1000")
        return
    try:
        balance = float(parts[1].replace(",", "").strip())
        if balance <= 0:
            raise ValueError
        set_balance(message.chat.id, balance)
        bot.reply_to(
            message,
            f"✅ Balance set to ${balance:,.2f}\n"
            f"Risk per trade at 1%: ${balance * 0.01:,.2f}"
        )
    except Exception:
        bot.reply_to(message, "❌ Invalid amount. Example: /lot 1000")


@bot.message_handler(commands=["watch"])
def cmd_watch(message):
    register_user(message.chat.id)
    parts = message.text.strip().split()
    if len(parts) < 2:
        wl = get_watchlist(message.chat.id)
        if wl:
            bot.reply_to(message, "👁 Your watchlist:\n" + ", ".join(wl))
        else:
            bot.reply_to(message, "👁 Watchlist is empty.\nUsage: /watch EURUSD XAUUSD")
        return
    added = add_to_watchlist(message.chat.id, parts[1:])
    bot.reply_to(message, f"✅ Added: {', '.join(added) if added else 'Nothing new added'}")


@bot.message_handler(commands=["unwatch"])
def cmd_unwatch(message):
    register_user(message.chat.id)
    parts = message.text.strip().split()
    if len(parts) < 2:
        bot.reply_to(message, "Usage: /unwatch EURUSD")
        return
    removed = remove_from_watchlist(message.chat.id, parts[1:])
    bot.reply_to(message, f"✅ Removed: {', '.join(removed) if removed else 'Nothing found'}")


@bot.message_handler(commands=["win"])
def cmd_win(message):
    register_user(message.chat.id)
    stats = update_stats(message.chat.id, True)
    w, l  = stats["wins"], stats["losses"]
    total = w + l
    wr    = round((w / total) * 100) if total else 0
    bot.reply_to(message, f"✅ Win logged.\nRecord: {w}W / {l}L\nWin Rate: {wr}%")


@bot.message_handler(commands=["loss"])
def cmd_loss(message):
    register_user(message.chat.id)
    stats = update_stats(message.chat.id, False)
    w, l  = stats["wins"], stats["losses"]
    total = w + l
    wr    = round((w / total) * 100) if total else 0
    bot.reply_to(message, f"❌ Loss logged.\nRecord: {w}W / {l}L\nWin Rate: {wr}%")


@bot.message_handler(commands=["stats"])
def cmd_stats(message):
    register_user(message.chat.id)
    stats   = get_stats(message.chat.id)
    w, l    = stats["wins"], stats["losses"]
    total   = w + l
    wr      = round((w / total) * 100) if total else 0
    signals = get_user_signals(message.chat.id)
    bot.reply_to(
        message,
        f"📊 Your Stats\n\n"
        f"Wins: {w}\nLosses: {l}\n"
        f"Total Logged: {total}\n"
        f"Win Rate: {wr}%\n"
        f"Signals Received: {len(signals)}"
    )


# ── Photo handler ─────────────────────────────────────────────────────────────

@bot.message_handler(content_types=["photo"])
def handle_photo(message):
    register_user(message.chat.id)
    uid = message.from_user.id

    from plans import is_user_active, user_has_feature, get_user_daily_limit

    # GATE 1: Must have an active plan
    if not is_admin(uid) and not is_user_active(uid):
        bot.reply_to(
            message,
            "🔒 You need an active plan to use chart analysis.\n\n"
            "Use /redeem CODE to activate access.\n"
            "Use /plans to see available plans."
        )
        return

    # GATE 2: Plan must include manual_analysis feature
    if not is_admin(uid) and not user_has_feature(uid, "manual_analysis"):
        bot.reply_to(
            message,
            "🔒 Your current plan does not include chart analysis.\n\n"
            "Use /plans to see which plans include this feature."
        )
        return

    # GATE 3: Daily usage limit
    daily_limit   = get_user_daily_limit(uid) if not is_admin(uid) else 9999
    current_usage = get_daily_usage(uid)
    if not is_admin(uid) and daily_limit > 0 and current_usage >= daily_limit:
        bot.reply_to(
            message,
            f"📊 Daily limit reached ({current_usage}/{daily_limit}).\n\n"
            "Your limit resets at midnight UTC.\n"
            "Consider upgrading your plan for a higher limit."
        )
        return

    remaining = get_cooldown_remaining(message.chat.id)
    if remaining > 0:
        bot.reply_to(message, f"⏳ Cooldown active. Try again in {remaining}s.")
        return

    mark_request(message.chat.id)

    temp_path = None
    try:
        bot.reply_to(message, "📸 Chart received. Analyzing with AI + market data + HTF...")

        file_info  = bot.get_file(message.photo[-1].file_id)
        downloaded = bot.download_file(file_info.file_path)

        if not downloaded:
            raise RuntimeError("Could not download image.")
        if len(downloaded) > 12 * 1024 * 1024:
            raise RuntimeError("Image too large. Please send a smaller screenshot.")

        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmp:
            tmp.write(downloaded)
            temp_path = tmp.name

        vision = analyze_chart_vision(downloaded)
        qs     = vision.get("quality_score", 80)
        issues = vision.get("quality_issues", [])

        # Hard reject for unreadable charts (separate from quality scorer)
        if not vision.get("readable", False):
            bot.reply_to(
                message,
                "🚫 This chart couldn't be analysed — it appears unreadable or too noisy.\n\n"
                "For best results:\n"
                "• Use a clean M15, M30, or H1 chart\n"
                "• Make sure the pair label and timeframe are visible\n"
                "• Remove excessive drawings or indicators\n"
                "• Avoid cropped or blurry screenshots"
            )
            return

        # Map issue keys to friendly, specific messages
        _issue_labels = {
            "pair not visible":        "The trading pair is not clearly visible on this chart.",
            "timeframe not visible":   "The timeframe is not shown — include it for better analysis.",
            "candles unclear":         "Candle details are hard to read — zoom in slightly.",
            "too many drawings":       "Too many drawings are covering price action.",
            "chart over-zoomed":       "The chart appears over-zoomed — show a wider view.",
            "screenshot cropped":      "The screenshot is cropped too tightly.",
            "low resolution":          "Image resolution is too low for accurate analysis.",
        }

        def _friendly_issues(raw_issues, max_count=3):
            friendly = []
            for iss in raw_issues[:max_count]:
                iss_lower = iss.lower()
                matched = False
                for key, label in _issue_labels.items():
                    if key in iss_lower:
                        friendly.append(f"• {label}")
                        matched = True
                        break
                if not matched:
                    friendly.append(f"• {iss.capitalize()}")
            return friendly

        # Give the user specific, readable quality feedback
        if qs < 50 and issues:
            friendly = _friendly_issues(issues, 3)
            bot.reply_to(
                message,
                f"⚠️ Chart quality is low ({qs}/100).\n\n"
                "Issues found:\n" + "\n".join(friendly) + "\n\n"
                "Analysis will continue but confidence will be reduced.\n"
                "Cleaner charts produce more reliable signals."
            )
        elif qs < 70 and issues:
            friendly = _friendly_issues(issues, 2)
            bot.reply_to(
                message,
                f"📊 Chart quality note ({qs}/100):\n" + "\n".join(friendly) + "\n\n"
                "Proceeding with analysis..."
            )

        analysis = run_decision(vision)
        text     = format_signal_text(analysis, message.chat.id)
        bot.reply_to(message, text)

        if analysis.get("no_signal"):
            pair = analysis.get("pair", "").upper().replace("/", "")
            if pair and pair != "UNKNOWN" and len(pair) >= 6:
                add_pending_watch(message.chat.id, pair)
                bot.send_message(
                    message.chat.id,
                    f"👁 {pair} added to your auto-watch.\n"
                    "I'll notify you when the scanner finds a valid setup on this pair.\n"
                    "Auto-watch expires in 24 hours."
                )

        save_signal(
            message.chat.id,
            analysis,
            source="manual",
            quality_score=qs,
            quality_issues=issues,
        )
        increment_daily_usage(uid)

        print(
            f"Signal sent to {message.chat.id}: "
            f"{analysis.get('pair')} {analysis.get('direction')} "
            f"conf={analysis.get('confidence')} quality={qs}",
            flush=True,
        )

    except Exception as e:
        bot.reply_to(message, f"❌ Analysis error: {e}")
        print(f"PHOTO HANDLER ERROR:\n{traceback.format_exc()}", flush=True)
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass


@bot.message_handler(func=lambda message: message.content_type == "text" and not (message.text or "").startswith("/"))
def handle_text(message):
    register_user(message.chat.id)
    bot.reply_to(message, "📷 Send a chart screenshot to get a signal.\nType /help for commands.")


# ── Scheduler ─────────────────────────────────────────────────────────────────

ALERTED_NEWS_IDS = set()
LAST_RUN_MARKS   = set()


def once_per_minute(key: str) -> bool:
    now  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    mark = f"{key}|{now}"
    if mark in LAST_RUN_MARKS:
        return False
    LAST_RUN_MARKS.add(mark)
    if len(LAST_RUN_MARKS) > 5000:
        LAST_RUN_MARKS.clear()
    return True


def check_upcoming_news():
    try:
        now_utc = datetime.now(timezone.utc)
        for e in fetch_calendar_events():
            if str(e.get("impact", "")).lower() != "high":
                continue
            dt = parse_event_datetime(e.get("date", ""))
            if not dt:
                continue
            diff_minutes = (dt - now_utc).total_seconds() / 60.0
            eid = event_id(e)
            if 28 <= diff_minutes <= 32 and eid not in ALERTED_NEWS_IDS:
                ALERTED_NEWS_IDS.add(eid)
                country  = e.get("country", "")
                title    = e.get("title", "")
                forecast = e.get("forecast", "—")
                previous = e.get("previous", "—")
                broadcast(
                    "⚠️ NEWS ALERT — 30 MIN WARNING\n\n"
                    f"🔴 {country} — {title}\n"
                    f"📊 Forecast: {forecast} | Previous: {previous}\n"
                    f"⏰ Release in ~30 minutes.\n"
                    "📌 Avoid opening fresh trades into the spike."
                )
    except Exception as e:
        print(f"News check error: {e}", flush=True)


def send_morning_brief():
    events  = fetch_todays_high_impact_news()
    now_str = datetime.now(SAST).strftime("%A, %d %B %Y")
    session = get_session_label()
    if events:
        lines = [f"☀️ DAILY BRIEF — {now_str}", f"📍 Session: {session}", "", "📅 High-Impact News Today:"]
        for e in events:
            lines.append("")
            lines.append(format_news_event(e))
        lines += ["", "⚠️ Plan around these releases. Patience is edge."]
        broadcast("\n".join(lines))
    else:
        broadcast(
            f"☀️ DAILY BRIEF — {now_str}\n"
            f"📍 Session: {session}\n\n"
            "✅ No high-impact news scheduled today.\n"
            "Clean trading conditions expected."
        )


def send_session_alert(title: str, emoji: str, time_str: str, focus_lines: list):
    data  = load_data()
    lines = [
        f"{emoji} {title}\n",
        f"⏰ {time_str}\n",
        "🎯 Focus:",
    ]
    for f in focus_lines:
        lines.append(f"• {f}")
    lines.append("\n📸 Send your chart for analysis.")
    broadcast("\n".join(lines))

    for uid, wl in data.get("watchlists", {}).items():
        if wl:
            safe_send(
                int(uid),
                f"👁 Your Watchlist — {title.split('—')[0].strip()}\n\n"
                f"{', '.join(wl)}\n\nCheck these pairs now."
            )


# Scanner / health state
_LAST_SCAN_TS  = 0
_BOT_START_TS  = time.time()
_LAST_SCAN_SAST = "—"


def run_market_scan():
    """Runs one market scan cycle and sends alerts for qualifying setups."""
    global _LAST_SCAN_TS, _LAST_SCAN_SAST
    try:
        if not get_scanner_enabled():
            return
        from scanner import get_current_scan_interval
        dynamic_interval = get_current_scan_interval(datetime.now(timezone.utc))
        now_ts = time.time()
        if now_ts - _LAST_SCAN_TS < dynamic_interval:
            return
        _LAST_SCAN_TS   = now_ts
        _LAST_SCAN_SAST = datetime.now(SAST).strftime("%H:%M SAST")
        print(f"[Scheduler] Running market scan (interval={dynamic_interval}s)...", flush=True)
        count = scan_market_for_alerts(bot)
        print(f"[Scheduler] Market scan done — {count} alert(s) sent.", flush=True)
    except Exception as e:
        print(f"Market scan error: {e}", flush=True)


def send_daily_health_report():
    """
    Sends a daily bot health + performance summary.
    - Admins always receive the full report.
    - Non-admin users only receive a concise summary if their plan includes daily_summary.
    """
    from analytics import get_daily_health_report
    from storage import get_scanner_enabled, load_data
    from plans import is_user_active, user_has_feature
    import circuit_breaker
    try:
        rpt    = get_daily_health_report()
        state  = "ON" if get_scanner_enabled() else "OFF"
        r_sign = "+" if rpt["r_24h"] >= 0 else ""
        wr_icon = "🟢" if rpt["all_win_rate"] >= 60 else ("🟡" if rpt["all_win_rate"] >= 50 else "🔴")
        cb      = circuit_breaker.get_status()
        cb_str  = "ARMED 🔴" if cb["active"] else "Standby 🟢"

        # Full report for admins
        admin_lines = [
            f"📊 DAILY BOT HEALTH REPORT — {datetime.now(SAST).strftime('%d %b %Y, %H:%M SAST')}",
            "",
            "Last 24 hours:",
            f"  Resolved:  {rpt['resolved_24h']} alert(s)",
            f"  Wins:      {rpt['wins_24h']}  |  Losses: {rpt['losses_24h']}",
            f"  R made:    {r_sign}{rpt['r_24h']}R",
            "",
            "All-time:",
            f"  Resolved:  {rpt['all_resolved']}",
            f"  Win rate:  {wr_icon} {rpt['all_win_rate']}%",
            f"  Expectancy:{rpt['expectancy']:+.3f}R",
            "",
            "Status:",
            f"  Scanner:   {state}",
            f"  Pending:   {rpt['pending']} alert(s)",
            f"  Users:     {rpt['active_users']} active",
            f"  Circuit Brk:{cb_str}",
        ]
        if rpt.get("drift_warning"):
            admin_lines += ["", rpt["drift_warning"]]

        admin_msg = "\n".join(admin_lines)
        for admin_id in ADMIN_IDS:
            safe_send(admin_id, admin_msg)

        # Concise daily summary for eligible non-admin users
        user_summary = (
            f"📊 DAILY SUMMARY — {datetime.now(SAST).strftime('%d %b %Y')}\n\n"
            f"Last 24h: {rpt['wins_24h']}W / {rpt['losses_24h']}L "
            f"({r_sign}{rpt['r_24h']}R)\n"
            f"All-time win rate: {wr_icon} {rpt['all_win_rate']}%\n\n"
            f"Use /stats to see your personal record."
        )
        data = load_data()
        for uid_str in data.get("approved_users", {}):
            try:
                uid = int(uid_str)
            except ValueError:
                continue
            if uid in ADMIN_IDS:
                continue
            if not is_user_active(uid):
                continue
            if not user_has_feature(uid, "daily_summary"):
                continue
            try:
                safe_send(uid, user_summary)
            except Exception:
                pass

    except Exception as e:
        print(f"[Health report] Error: {e}", flush=True)


def run_scheduler():
    while True:
        try:
            now  = datetime.now(timezone.utc)
            hhmm = now.strftime("%H:%M")

            if hhmm == "06:30" and once_per_minute("morning_brief"):
                send_morning_brief()

            if hhmm == "07:00" and once_per_minute("london_open"):
                send_session_alert(
                    "LONDON KILLZONE — NOW OPEN", "🇬🇧",
                    "09:00 SAST",
                    ["Asian high/low sweeps", "BOS on M15/M30",
                     "OB/FVG reactions", "Strong displacement candles"]
                )

            if hhmm == "12:30" and once_per_minute("newyork_open"):
                send_session_alert(
                    "NEW YORK KILLZONE — NOW OPEN", "🇺🇸",
                    "14:30 SAST",
                    ["London high/low sweeps", "Sharp displacement from key zones",
                     "Continuation or CHoCH setups"]
                )

            if hhmm == "16:00" and once_per_minute("london_close"):
                broadcast(
                    "🔔 LONDON SESSION CLOSING\n\n"
                    "⏰ 18:00 SAST\n\n"
                    "Checklist:\n"
                    "• Move SL to breakeven where valid\n"
                    "• Avoid late random entries\n"
                    "• Log result with /win or /loss\n"
                    "• Review your trades"
                )

            # Daily health report — config-driven UTC time (default 05:30 UTC = 07:30 SAST)
            from config import DAILY_HEALTH_REPORT_UTC_HOUR, DAILY_HEALTH_REPORT_UTC_MINUTE
            _health_hhmm = f"{DAILY_HEALTH_REPORT_UTC_HOUR:02d}:{DAILY_HEALTH_REPORT_UTC_MINUTE:02d}"
            if hhmm == _health_hhmm and once_per_minute("daily_health"):
                send_daily_health_report()

            # News check every 30 minutes
            if now.minute in (0, 30) and once_per_minute("news_check"):
                check_upcoming_news()

            # Market scanner — runs every SCAN_INTERVAL_SECONDS
            run_market_scan()

        except Exception as e:
            print(f"Scheduler error: {e}", flush=True)

        time.sleep(5)


# ── Health server ─────────────────────────────────────────────────────────────

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK - Chefbuntsa Forex Bot is running")

    def log_message(self, format, *args):
        return


class _ReuseHTTPServer(HTTPServer):
    """HTTPServer with SO_REUSEADDR so restarts never hit 'Address already in use'."""
    allow_reuse_address = True


def run_health_server():
    import socket, time as _time
    for attempt in range(10):
        try:
            server = _ReuseHTTPServer(("0.0.0.0", PORT), HealthHandler)
            print(f"Health server on port {PORT}", flush=True)
            server.serve_forever()
            return
        except OSError:
            print(f"Health server port {PORT} busy, retrying ({attempt+1}/10)...", flush=True)
            _time.sleep(3)
    print(f"Health server failed to bind after retries — skipping.", flush=True)


# ── Scanner global on/off commands ────────────────────────────────────────────

@bot.message_handler(commands=["scanneron"])
def cmd_scanneron(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "❌ Admin only.")
        return
    if get_scanner_enabled():
        bot.reply_to(message, "✅ Auto-scanner is already ON.")
        return
    set_scanner_enabled(True)
    print("[Admin] Scanner ENABLED by admin.", flush=True)
    bot.reply_to(
        message,
        "✅ Auto-scanner is now ON.\n\n"
        "The bot will resume scanning every 15 minutes and sending "
        "buy/sell alerts to all users automatically."
    )


@bot.message_handler(commands=["scanneroff"])
def cmd_scanneroff(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "❌ Admin only.")
        return
    if not get_scanner_enabled():
        bot.reply_to(message, "⏸ Auto-scanner is already OFF.")
        return
    set_scanner_enabled(False)
    print("[Admin] Scanner DISABLED by admin — API calls paused.", flush=True)
    bot.reply_to(
        message,
        "⏸ Auto-scanner is now OFF.\n\n"
        "No buy/sell alerts will be sent until you run /scanneron.\n"
        "API calls are paused — no market data credits will be used.\n\n"
        "Manual chart analysis (screenshot) still works normally."
    )


@bot.message_handler(commands=["scannerstate"])
def cmd_scannerstate(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "❌ Admin only.")
        return
    enabled = get_scanner_enabled()
    if enabled:
        bot.reply_to(
            message,
            "🔌 Scanner Status: ✅ ON\n\n"
            "The auto-scanner is active and sending alerts every 15 minutes.\n"
            "Use /scanneroff to pause and save API credits."
        )
    else:
        bot.reply_to(
            message,
            "🔌 Scanner Status: ⏸ OFF\n\n"
            "The auto-scanner is paused. No alerts are being sent and "
            "no market data API calls are being made.\n"
            "Use /scanneron to resume."
        )


# ── /health command ───────────────────────────────────────────────────────────

@bot.message_handler(commands=["health"])
def cmd_health(message):
    print(f"[CMD] /health from uid={message.from_user.id}", flush=True)
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "❌ Admin only.")
        return

    from storage import get_scanner_enabled, load_data as _ld
    from market_data import get_api_usage_today
    import os
    import circuit_breaker

    data     = _ld()
    alerts   = data.get("scanner_alerts", [])
    pending  = sum(1 for a in alerts if a.get("outcome") == "pending")
    resolved = [a for a in alerts if a.get("outcome") in ("win", "loss")]
    wins     = sum(1 for a in resolved if a["outcome"] == "win")
    total    = len(resolved)
    wr       = round((wins / total) * 100, 1) if total else 0.0
    users    = len(data.get("approved_users", {}))
    reg      = len(data.get("users", []))

    # Uptime
    uptime_s = int(time.time() - _BOT_START_TS)
    h, rem   = divmod(uptime_s, 3600)
    m, _     = divmod(rem, 60)
    uptime_s_str = f"{h}h {m}m"

    # Storage size
    try:
        from config import DATA_FILE as _DF
        sz_kb = round(os.path.getsize(_DF) / 1024, 1)
    except Exception:
        sz_kb = "?"

    from outcome_checker import get_last_outcome_check_sast
    from scanner import get_current_scan_interval

    scanner_state    = "ON ✅" if get_scanner_enabled() else "OFF ⏸"
    wr_icon          = "🟢" if wr >= 60 else ("🟡" if wr >= 50 else "🔴")
    dyn_interval_min = round(get_current_scan_interval(datetime.now(timezone.utc)) / 60, 1)

    # Circuit breaker status
    cb          = circuit_breaker.get_status()
    cb_line     = ""
    if cb["active"]:
        cb_line = (
            f"\n⚡ Circuit Breaker: ARMED 🔴\n"
            f"  Streak:   {cb['consecutive_losses']} consecutive losses\n"
            f"  Effect:   threshold +{cb['confidence_bump']}pp\n"
            f"  Resets:   {cb['auto_reset_at']}"
        )
    else:
        cb_line = (
            f"\n⚡ Circuit Breaker: Standby 🟢  "
            f"(streak: {cb['consecutive_losses']}/{cb['streak_threshold']})"
        )

    # API usage
    api_today = get_api_usage_today()

    lines = [
        f"🏥 BOT HEALTH — {datetime.now(SAST).strftime('%d %b %Y %H:%M SAST')}",
        "",
        f"Status:          Online ✅",
        f"Scanner:         {scanner_state}",
        f"Last scan:       {_LAST_SCAN_SAST}",
        f"Scan interval:   {dyn_interval_min} min (session-dynamic)",
        f"Last outcome chk:{get_last_outcome_check_sast()}",
        f"Uptime:          {uptime_s_str}",
        f"API calls today: {api_today}",
        "",
        "Alerts:",
        f"  Pending:       {pending}",
        f"  Resolved:      {total}",
        f"  Win rate:      {wr_icon} {wr}%",
        "",
        "Users:",
        f"  Approved:      {users}",
        f"  Registered:    {reg}",
        "",
        f"Storage:         {sz_kb} KB",
        cb_line,
    ]

    # Brain status
    try:
        import adaptive_brain
        brain_st  = adaptive_brain.get_status()
        adj       = brain_st.get("active_adjustments", {})
        adj_count = sum(
            len(v) if isinstance(v, dict) else (1 if v else 0)
            for v in adj.values()
        )
        gb = adj.get("global_conf_bump", 0)
        brain_str = (
            f"\n🧠 Brain: {adj_count} active adj"
            + (f" | +{gb}pp global floor" if gb else "")
            + f" | last: {brain_st.get('last_analysis', 'never')}"
        )
        lines.append(brain_str)
    except Exception:
        pass

    lines += [
        "",
        "Use /healthreport for full daily summary.",
    ]
    bot.reply_to(message, "\n".join(lines))


# ── /healthreport command ─────────────────────────────────────────────────────

@bot.message_handler(commands=["healthreport"])
def cmd_healthreport(message):
    print(f"[CMD] /healthreport from uid={message.from_user.id}", flush=True)
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "❌ Admin only.")
        return

    from analytics import get_daily_health_report, get_regime_expectancy, get_pair_session_stats
    from storage import get_scanner_enabled
    from outcome_checker import get_last_outcome_check_sast
    from scanner import get_current_scan_interval

    try:
        import circuit_breaker
        rpt          = get_daily_health_report()
        regime_data  = get_regime_expectancy()
        state        = "ON" if get_scanner_enabled() else "OFF"
        r_sign       = "+" if rpt["r_24h"] >= 0 else ""
        wr_icon      = "🟢" if rpt["all_win_rate"] >= 60 else ("🟡" if rpt["all_win_rate"] >= 50 else "🔴")
        dyn_interval = round(get_current_scan_interval(datetime.now(timezone.utc)) / 60, 1)
        cb           = circuit_breaker.get_status()
        cb_str       = ("ARMED 🔴" if cb["active"] else "Standby 🟢") + \
                       (f" — +{cb['confidence_bump']}pp threshold" if cb["active"] else "")

        lines = [
            f"📊 DAILY HEALTH REPORT — {datetime.now(SAST).strftime('%d %b %Y, %H:%M SAST')}",
            "",
            "Last 24 hours:",
            f"  Resolved:   {rpt['resolved_24h']} alert(s)",
            f"  Wins:       {rpt['wins_24h']}  |  Losses: {rpt['losses_24h']}",
            f"  R made:     {r_sign}{rpt['r_24h']}R",
            "",
            "All-time performance:",
            f"  Resolved:   {rpt['all_resolved']}",
            f"  Win rate:   {wr_icon} {rpt['all_win_rate']}%",
            f"  Expectancy: {rpt['expectancy']:+.3f}R",
            "",
            "Bot status:",
            f"  Scanner:    {state}",
            f"  Interval:   {dyn_interval} min (session-dynamic)",
            f"  Pending:    {rpt['pending']} alert(s)",
            f"  Users:      {rpt['active_users']} active",
            f"  Outcome chk:{get_last_outcome_check_sast()}",
            f"  Circuit Brk:{cb_str}",
        ]

        # Brain status in healthreport
        try:
            import adaptive_brain as _ab
            b_st    = _ab.get_status()
            b_adj   = b_st.get("active_adjustments", {})
            b_count = sum(len(v) if isinstance(v, dict) else (1 if v else 0) for v in b_adj.values())
            b_gb    = b_adj.get("global_conf_bump", 0)
            b_line  = f"  Brain:      {b_count} adj" + (f" (+{b_gb}pp floor)" if b_gb else " (no penalties)") + \
                      f" | {b_st.get('last_analysis','never')}"
            lines.append(b_line)
        except Exception:
            pass

        # Drift warning (if recent performance drifting below all-time)
        drift = rpt.get("drift_warning", "")
        if drift:
            lines += ["", drift]

        # Best / worst combo
        best  = rpt.get("best_combo", "")
        worst = rpt.get("worst_combo", "")
        if best or worst:
            lines += ["", "Edge analysis (≥4 alerts):"]
            if best:
                lines.append(f"  Best:  {best}")
            if worst:
                lines.append(f"  Worst: {worst}")

        # Regime breakdown (top 3 by trade count)
        if regime_data:
            lines += ["", "Regime performance (top 3):"]
            sorted_regimes = sorted(
                regime_data.items(),
                key=lambda x: x[1].get("total", 0), reverse=True
            )[:3]
            for regime_name, rd in sorted_regimes:
                if rd.get("total", 0) > 0:
                    icon = "🟢" if rd.get("win_rate", 0) >= 60 else "🟡"
                    lines.append(
                        f"  {regime_name:12} {icon} {rd.get('win_rate', 0)}% "
                        f"({rd.get('wins', 0)}W/{rd.get('total', 0)}T) "
                        f"E={rd.get('expectancy', 0):+.2f}R"
                    )

        bot.reply_to(message, "\n".join(lines))
    except Exception as e:
        bot.reply_to(message, f"⚠️ Report error: {e}")
        print(f"[CMD] /healthreport error: {e}", flush=True)


# ── /brainreport command ───────────────────────────────────────────────────────

@bot.message_handler(commands=["brainreport"])
def cmd_brainreport(message):
    print(f"[CMD] /brainreport from uid={message.from_user.id}", flush=True)
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "❌ Admin only.")
        return
    try:
        import adaptive_brain
        report = adaptive_brain.get_full_report()
        bot.reply_to(message, report, parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(message, f"⚠️ Brain report error: {e}")
        print(f"[CMD] /brainreport error: {e}", flush=True)


# ── /heatmap command ──────────────────────────────────────────────────────────

@bot.message_handler(commands=["heatmap"])
def cmd_heatmap(message):
    print(f"[CMD] /heatmap from uid={message.from_user.id}", flush=True)
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "❌ Admin only.")
        return

    from analytics import get_hour_heatmap, get_day_heatmap

    hour_data = get_hour_heatmap()
    day_data  = get_day_heatmap()

    if not hour_data and not day_data:
        bot.reply_to(message, "No resolved alerts yet — heatmap will populate as trades complete.")
        return

    def _bar(wr: int, width: int = 10) -> str:
        filled = round(wr / 100 * width)
        return "█" * filled + "░" * (width - filled)

    lines = ["🗓 PERFORMANCE HEATMAP\n"]

    # Day of week
    if day_data:
        lines.append("By Day of Week (SAST):")
        for day, d in day_data.items():
            bar = _bar(d["win_rate"])
            lines.append(
                f"  {day}  {bar} {d['win_rate']}%  "
                f"({d['wins']}W/{d['losses']}L  exp:{d['expectancy']:+.2f}R)"
            )
        lines.append("")

    # Hour of day
    if hour_data:
        lines.append("By Hour (SAST) — active hours only:")
        for hour, d in hour_data.items():
            bar = _bar(d["win_rate"])
            lines.append(
                f"  {d['label']}  {bar} {d['win_rate']}%  "
                f"({d['wins']}W/{d['losses']}L)"
            )
        lines.append("")

    lines.append("⚠️ Based on scanner entry alerts only. More data = better signal.")
    bot.reply_to(message, "\n".join(lines))


@bot.message_handler(commands=["timeheatmap"])
def cmd_timeheatmap(message):
    """Alias for /heatmap — shows the same performance heatmap by time."""
    cmd_heatmap(message)


# ── /pnl command ──────────────────────────────────────────────────────────────

@bot.message_handler(commands=["pnl"])
def cmd_pnl(message):
    print(f"[CMD] /pnl from uid={message.from_user.id}", flush=True)
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "❌ Admin only.")
        return

    from analytics import get_pnl_curve

    # Optional: /pnl 30 to limit to last 30 trades
    parts = message.text.strip().split()
    limit = 50
    if len(parts) > 1:
        try:
            limit = max(5, min(int(parts[1]), 200))
        except ValueError:
            pass

    result = get_pnl_curve(limit=limit)

    if not result["curve"]:
        bot.reply_to(message, "No resolved alerts yet — P&L curve will populate as trades complete.")
        return

    final_r  = result["final_r"]
    r_sign   = "+" if final_r >= 0 else ""
    wr_icon  = "🟢" if result["win_rate"] >= 60 else ("🟡" if result["win_rate"] >= 50 else "🔴")

    lines = [
        f"📈 RUNNING P&L CURVE (last {result['total']} resolved alerts)\n",
        f"Win rate:   {wr_icon} {result['win_rate']}%  "
        f"({result['wins']}W / {result['losses']}L)",
        f"Total R:    {r_sign}{final_r}R\n",
        "Trade log:",
    ]

    for c in result["curve"]:
        icon    = "✅" if c["outcome"] == "win" else "❌"
        d_sign  = "+" if c["delta"] >= 0 else ""
        run_s   = f"{'+' if c['running'] >= 0 else ''}{c['running']}"
        lines.append(
            f"  {icon} {c['pair']} {c['direction']}  "
            f"{d_sign}{c['delta']}R  [{run_s}R]  {c['date']}"
        )

    lines.append(f"\nFinal equity: {r_sign}{final_r}R")
    bot.reply_to(message, "\n".join(lines))


@bot.message_handler(commands=["rcurve"])
def cmd_rcurve(message):
    """Alias for /pnl — shows running R curve (P&L)."""
    cmd_pnl(message)


@bot.message_handler(commands=["drawdown"])
def cmd_drawdown(message):
    print(f"[CMD] /drawdown from uid={message.from_user.id}", flush=True)
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "❌ Admin only.")
        return

    from analytics import get_pnl_curve
    result = get_pnl_curve(limit=500)
    curve  = result.get("curve", [])
    if not curve:
        bot.reply_to(message, "No resolved trades yet — drawdown stats will appear once trades complete.")
        return

    # Build equity curve and compute peak-to-trough max drawdown
    running_r = 0.0
    peak_r    = 0.0
    max_dd    = 0.0
    trough_r  = 0.0
    dd_start  = ""
    dd_end    = ""
    _peak_r_tmp = 0.0
    _peak_date  = ""
    for t in curve:
        running_r += t.get("delta", 0)
        if running_r > _peak_r_tmp:
            _peak_r_tmp = running_r
            _peak_date  = t.get("date", "")
        dd = _peak_r_tmp - running_r
        if dd > max_dd:
            max_dd   = dd
            peak_r   = _peak_r_tmp
            trough_r = running_r
            dd_start = _peak_date
            dd_end   = t.get("date", "")

    # Worst pair by loss count
    from collections import Counter
    loss_pairs   = Counter(t["pair"] for t in curve if t.get("outcome") == "loss")
    worst_pair   = loss_pairs.most_common(1)
    worst_pair_s = f"{worst_pair[0][0]} ({worst_pair[0][1]} losses)" if worst_pair else "N/A"

    # Worst session
    loss_sessions = Counter(t.get("session", "unknown") for t in curve if t.get("outcome") == "loss")
    worst_sess    = loss_sessions.most_common(1)
    worst_sess_s  = f"{worst_sess[0][0]} ({worst_sess[0][1]} losses)" if worst_sess else "N/A"

    dd_pct_of_peak = (max_dd / peak_r * 100) if peak_r > 0 else 0

    lines = [
        "📉 DRAWDOWN DASHBOARD\n",
        f"Trades analysed : {len(curve)}",
        f"Peak equity     : +{peak_r:.1f}R",
        f"Trough equity   : {'+' if trough_r >= 0 else ''}{trough_r:.1f}R",
        f"Max drawdown    : -{max_dd:.1f}R ({dd_pct_of_peak:.0f}% of peak)",
        f"DD period       : {dd_start or 'N/A'} → {dd_end or 'N/A'}",
        f"Final equity    : {'+' if result['final_r'] >= 0 else ''}{result['final_r']}R",
        "",
        f"Worst pair     : {worst_pair_s}",
        f"Worst session  : {worst_sess_s}",
    ]
    bot.reply_to(message, "\n".join(lines))


# ── Tuning preview commands ───────────────────────────────────────────────────

@bot.message_handler(commands=["applytuningpreview"])
def cmd_applytuningpreview(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "❌ Admin only.")
        return

    from tuning_preview import parse_candidate_args, run_preview, VALID_KEYS

    raw_parts = message.text.strip().split()[1:]
    if not raw_parts:
        bot.reply_to(
            message,
            "Usage: /applytuningpreview key=value [key=value ...]\n\n"
            "Examples:\n"
            "  /applytuningpreview pullback=11\n"
            "  /applytuningpreview pullback=11 ema_slope=2\n"
            "  /applytuningpreview entry_threshold=75 watch_threshold=64\n"
            "  /applytuningpreview source=scanner days=30 pullback=11\n\n"
            f"Valid keys: {', '.join(sorted(VALID_KEYS))}"
        )
        return

    candidate, meta, errors = parse_candidate_args(raw_parts)

    if errors:
        bot.reply_to(message, "❌ Parsing errors:\n" + "\n".join(f"• {e}" for e in errors))
        return

    if not candidate:
        bot.reply_to(message, "❌ No valid weight changes found. Provide at least one key=value pair.")
        return

    result = run_preview(candidate, source=meta["source"], days=meta["days"])

    if "error" in result:
        bot.reply_to(message, f"📉 Preview unavailable: {result['error']}")
        return

    # ── Format candidate change lines ─────────────────────────────────────────
    from tuning_preview import _effective_defaults, _DEFAULTS
    from config import ALERT_MIN_CONFIDENCE as _CUR_ENTRY, WATCH_ALERT_MIN_CONFIDENCE as _WATCH_TH
    current_live = _effective_defaults()

    change_lines = []
    for k, v in sorted(candidate.items()):
        if k == "entry_threshold":
            old = _CUR_ENTRY
        elif k == "watch_threshold":
            old = _WATCH_TH
        else:
            old = current_live.get(k, _DEFAULTS.get(k, "?"))
        delta = v - old if isinstance(old, int) else "?"
        sign  = "+" if isinstance(delta, int) and delta > 0 else ""
        change_lines.append(f"  {k}: {old:+} → {v:+} ({sign}{delta})")

    # ── Build output ──────────────────────────────────────────────────────────
    tier_icons = {"high": "🟢", "medium": "🟡", "low": "🔴"}
    tier_icon  = tier_icons.get(result["confidence_tier"], "•")
    source_str = meta["source"].capitalize()
    days_str   = f" | Last {meta['days']} days" if meta["days"] > 0 else " | All-time"

    wr_delta   = round(result["new_win_rate"]  - result["cur_win_rate"],  1)
    exp_delta  = round(result["new_expectancy"] - result["cur_expectancy"], 3)
    cf_delta   = round(result["new_avg_conf"]  - result["cur_avg_conf"],  1)

    wr_sign    = "+" if wr_delta  >= 0 else ""
    exp_sign   = "+" if exp_delta >= 0 else ""
    cf_sign    = "+" if cf_delta  >= 0 else ""

    lines = [
        f"🔭 TUNING PREVIEW {tier_icon} {result['confidence_tier'].upper()} CONFIDENCE",
        f"Source: {source_str}{days_str} | {result['sample_size']} resolved alerts\n",
        "Candidate changes:",
    ]
    lines += change_lines
    lines += [
        "",
        "Impact on resolved entry alerts:",
        f"  Win rate:    {result['cur_win_rate']}% → {result['new_win_rate']}% ({wr_sign}{wr_delta}pp)",
        f"  Expectancy:  {result['cur_expectancy']:+.3f}R → {result['new_expectancy']:+.3f}R ({exp_sign}{exp_delta:+.3f}R)",
        f"  Avg conf:    {result['cur_avg_conf']} → {result['new_avg_conf']} ({cf_sign}{cf_delta})",
        f"  Entry alerts:{result['cur_entries']:>3} → {result['new_entries']}",
        f"  Watch alerts:{result['cur_watches']:>3} → {result['new_watches']}",
        "",
        "Classification changes:",
        f"  Promoted watch → entry:  {result['promoted']}",
        f"  Demoted  entry → watch:  {result['demoted']}",
        f"  Removed  entirely:       {result['filtered_out']}",
        "",
        f"Interpretation:\n{result['interpretation']}",
        "",
        "⚠️ Read-only preview — no changes applied.\n"
        "Edit TUNING_OVERRIDES in config.py to apply manually.",
    ]

    bot.reply_to(message, "\n".join(lines))


@bot.message_handler(commands=["compareweights"])
def cmd_compareweights(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "❌ Admin only.")
        return

    from tuning_preview import parse_candidate_args, compare_weights, VALID_KEYS

    raw_parts = message.text.strip().split()[1:]
    if not raw_parts:
        bot.reply_to(
            message,
            "Usage: /compareweights key=value [key=value ...]\n\n"
            "Examples:\n"
            "  /compareweights pullback=11\n"
            "  /compareweights pullback=11 ema_slope=2 entry_threshold=75\n\n"
            f"Valid keys: {', '.join(sorted(VALID_KEYS))}"
        )
        return

    candidate, meta, errors = parse_candidate_args(raw_parts)

    if errors:
        bot.reply_to(message, "❌ Parsing errors:\n" + "\n".join(f"• {e}" for e in errors))
        return

    if not candidate:
        bot.reply_to(message, "❌ No valid weight keys found.")
        return

    rows = compare_weights(candidate)
    if not rows:
        bot.reply_to(message, "No rows to compare.")
        return

    action_icons = {"increase": "⬆️", "reduce": "⬇️", "keep": "✅"}
    override_tag = " ✏️"  # marks currently overridden values

    lines = ["⚖️ WEIGHT COMPARISON\n"]
    for r in rows:
        icon     = action_icons.get(r["action"], "•")
        override = override_tag if r["is_override"] else ""
        delta_s  = f"{r['delta']:+}" if r["delta"] != 0 else "±0"
        lines.append(
            f"{icon} {r['key']}{override}\n"
            f"   Current:   {r['current']:+}\n"
            f"   Candidate: {r['candidate']:+}\n"
            f"   Delta:     {delta_s}\n"
            f"   {r['description']}"
        )

    lines += [
        "",
        "✏️ = currently active TUNING_OVERRIDE",
        "⚠️ Read-only — edit TUNING_OVERRIDES in config.py to apply.",
    ]

    bot.reply_to(message, "\n\n".join(lines))


# ── Tuning commands ───────────────────────────────────────────────────────────

@bot.message_handler(commands=["tuningsuggestions"])
def cmd_tuningsuggestions(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "❌ Admin only.")
        return
    from tuning import get_component_tuning
    parts     = message.text.strip().split()
    component = parts[1].lower() if len(parts) >= 2 else None
    recs      = get_component_tuning(component)

    if not recs:
        bot.reply_to(message, "📉 Not enough resolved alert data yet.")
        return

    action_icons = {"increase": "⬆️", "keep": "✅", "reduce": "⬇️", "disable": "🚫"}
    lines = ["⚙️ COMPONENT TUNING SUGGESTIONS\n"]
    for r in recs:
        icon = action_icons.get(r["action"], "•")
        delta = ""
        if r["recommended_weight"] != r["current_weight"]:
            delta = f" → {r['recommended_weight']:+}"
        lines.append(
            f"{icon} {r['component'].upper().replace('_', ' ')}\n"
            f"   Weight: {r['current_weight']:+}{delta} | Conf: {r['confidence'].upper()}\n"
            f"   Active: WR={r['pos_win_rate']}% E={r['pos_expectancy']:+.3f}R "
            f"vs Neutral: WR={r['neu_win_rate']}% E={r['neu_expectancy']:+.3f}R\n"
            f"   Sample: {r['sample_size']}\n"
            f"   {r['reason']}"
        )

    bot.reply_to(message, "\n\n".join(lines[:6]))
    if len(lines) > 6:
        bot.reply_to(message, "\n\n".join(lines[6:]))


@bot.message_handler(commands=["tuningconfidence"])
def cmd_tuningconfidence(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "❌ Admin only.")
        return
    from tuning import get_confidence_tuning
    res = get_confidence_tuning()
    if not res.get("bands"):
        bot.reply_to(message, "📉 Not enough data yet.")
        return

    lines = [
        f"📊 CONFIDENCE BAND TUNING\n"
        f"Entry threshold: {res['current_entry_threshold']}% | "
        f"Watch threshold: {res['current_watch_threshold']}%\n"
    ]
    lines.append(f"{'Band':<8} {'Total':>6} {'WR%':>5} {'AvgRR':>6} {'Exp':>7}")
    lines.append("─" * 40)
    for label in ["65–69", "70–74", "75–79", "80–84", "85+"]:
        s = res["bands"].get(label)
        if s:
            lines.append(
                f"{label:<8} {s['total']:>6} {s['win_rate']:>5}% "
                f"{s['avg_rr']:>6.2f} {s['expectancy']:>+7.3f}R"
            )
        else:
            lines.append(f"{label:<8}  {'—':>6}")

    lines.append(f"\n💡 {res['threshold_recommendation']}")
    bot.reply_to(message, "\n".join(lines))


@bot.message_handler(commands=["tuningregimes"])
def cmd_tuningregimes(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "❌ Admin only.")
        return
    from tuning import get_regime_tuning
    recs = get_regime_tuning()
    if not recs:
        bot.reply_to(message, "📉 Not enough data yet.")
        return

    action_icons = {"increase": "⬆️", "keep": "✅", "reduce": "⬇️", "disable": "🚫"}
    lines = ["📊 REGIME TUNING RECOMMENDATIONS\n"]
    for r in recs:
        icon  = action_icons.get(r["action"], "•")
        delta = ""
        if r["recommended_weight"] != r["current_weight"]:
            delta = f" → {r['recommended_weight']:+}"
        lines.append(
            f"{icon} {r['regime'].upper()} | Weight: {r['current_weight']:+}{delta}\n"
            f"   WR={r['win_rate']}% E={r['expectancy']:+.3f}R "
            f"({r['sample_size']} alerts) | Conf: {r['confidence'].upper()}\n"
            f"   {r['reason']}"
        )

    bot.reply_to(message, "\n\n".join(lines))


@bot.message_handler(commands=["tuningpairsessions"])
def cmd_tuningpairsessions(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "❌ Admin only.")
        return
    from tuning import get_pair_session_tuning
    res = get_pair_session_tuning()
    top    = res.get("top", [])
    bottom = res.get("bottom", [])

    if not top and not bottom:
        bot.reply_to(message, "📉 Not enough pair-session data yet.")
        return

    lines = ["📊 PAIR-SESSION TUNING\n"]
    if top:
        lines.append("🏆 STRONGEST COMBOS")
        for r in top:
            lines.append(
                f"  ✅ {r['combo']} | WR={r['win_rate']}% E={r['expectancy']:+.3f}R "
                f"({r['sample_size']} alerts)\n     → {r['recommendation']}"
            )
    if bottom:
        lines.append("\n⚠️ WEAKEST COMBOS")
        for r in bottom:
            lines.append(
                f"  🔻 {r['combo']} | WR={r['win_rate']}% E={r['expectancy']:+.3f}R "
                f"({r['sample_size']} alerts)\n     → {r['recommendation']}"
            )
    lines.append(f"\n💡 {res['recommendation']}")
    bot.reply_to(message, "\n".join(lines))


@bot.message_handler(commands=["tuningthresholds"])
def cmd_tuningthresholds(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "❌ Admin only.")
        return
    from tuning import get_threshold_tuning
    res = get_threshold_tuning()

    lines = [
        f"📊 THRESHOLD TUNING\n"
        f"Entry: {res['current_entry_threshold']}% | "
        f"Watch: {res['current_watch_threshold']}%\n"
        f"Samples → Watch range: {res['watch_sample']} | "
        f"Low entry: {res['entry_low_sample']} | "
        f"Strong entry: {res['entry_strong_sample']}\n"
    ]
    for rec in res["recommendations"]:
        lines.append(f"💡 {rec}")

    bot.reply_to(message, "\n\n".join(lines))


@bot.message_handler(commands=["tuningfilters"])
def cmd_tuningfilters(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "❌ Admin only.")
        return
    from tuning import get_filter_tuning
    recs = get_filter_tuning()

    if not recs:
        bot.reply_to(message, "📉 No rejection data yet.")
        return

    verdict_icons = {
        "appropriate":                 "✅",
        "possibly too aggressive — high rejection rate": "⚠️",
        "rarely triggered — filter may be too lenient or rarely applicable": "🔍",
    }

    lines = [f"🔎 REJECTION FILTER TUNING ({len(recs)} filters)\n"]
    for r in recs:
        icon = verdict_icons.get(r["verdict"], "•")
        cat  = r["category"].replace("_", " ").title()
        lines.append(
            f"{icon} {cat}: {r['total']} rejections ({r['pct']}%) "
            f"[🤖{r['scanner']} 📸{r['manual']}]\n"
            f"   {r['verdict'].capitalize()}\n"
            f"   {r['advice']}"
        )

    # Send in chunks if long
    chunk  = []
    chunks = []
    total  = 0
    for line in lines:
        total += len(line)
        chunk.append(line)
        if total > 3000:
            chunks.append("\n\n".join(chunk))
            chunk = []
            total = 0
    if chunk:
        chunks.append("\n\n".join(chunk))

    for c in chunks:
        bot.reply_to(message, c)


@bot.message_handler(commands=["tuningsummary"])
def cmd_tuningsummary(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "❌ Admin only.")
        return
    from tuning import get_tuning_summary
    s = get_tuning_summary()

    if "error" in s:
        bot.reply_to(message, f"📉 {s['error']}")
        return

    lines = [
        "⚙️ TUNING SUMMARY\n",
        f"📈 Overall: {s['total_resolved']} resolved | "
        f"WR={s['overall_win_rate']}% | E={s['overall_expectancy']:+.3f}R\n",
        f"🏆 Best component:  {s['best_component'].upper().replace('_', ' ')} "
        f"(E={s['best_comp_exp']:+.3f}R)",
        f"⬇️  Worst component: {s['worst_component'].upper().replace('_', ' ')} "
        f"(E={s['worst_comp_exp']:+.3f}R)",
        f"🏆 Best regime:     {s['best_regime'].upper()} "
        f"(E={s['best_regime_exp']:+.3f}R)",
        f"⬇️  Worst regime:    {s['worst_regime'].upper()} "
        f"(E={s['worst_regime_exp']:+.3f}R)",
        f"🏆 Best pair/sess:  {s['best_pair_session']} "
        f"(E={s['best_ps_exp']:+.3f}R)",
        f"⬇️  Worst pair/sess: {s['worst_pair_session']} "
        f"(E={s['worst_ps_exp']:+.3f}R)\n",
        "🔧 PRIORITY ACTIONS:",
    ]
    for i, action in enumerate(s["priority_actions"], 1):
        lines.append(f"  {i}. {action}")

    bot.reply_to(message, "\n".join(lines))


@bot.message_handler(commands=["currentweights"])
def cmd_currentweights(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "❌ Admin only.")
        return
    from config import (
        ALERT_MIN_CONFIDENCE, WATCH_ALERT_MIN_CONFIDENCE,
        WATCH_ALERT_COOLDOWN_MINUTES, TUNING_OVERRIDES,
    )
    from tuning import DEFAULT_WEIGHTS

    overrides_active = bool(TUNING_OVERRIDES)
    lines = ["⚙️ CURRENT ENGINE WEIGHTS\n"]

    # Group display
    groups = {
        "Baseline": ["baseline_manual", "baseline_scanner"],
        "HTF Alignment": ["htf_aligned", "htf_conflict"],
        "Regime": ["pullback", "trending", "reversal", "range", "mixed"],
        "EMA / Momentum": ["ema_slope_aligned", "ema_slope_misaligned",
                           "momentum_strong", "momentum_pullback"],
        "Risk/Reward": ["rr_3plus", "rr_25plus", "rr_2plus"],
        "Session": ["session_cap"],
        "News": ["news_medium", "news_high"],
        "Bias": ["bias_aligned", "bias_conflict"],
        "Chart Quality": ["chart_quality_clean", "chart_quality_dirty"],
    }

    for group, keys in groups.items():
        lines.append(f"── {group}")
        for k in keys:
            default = DEFAULT_WEIGHTS.get(k, 0)
            active  = TUNING_OVERRIDES.get(k, default)
            suffix  = " ✏️" if k in TUNING_OVERRIDES else ""
            lines.append(f"   {k}: {active:+}{suffix}")

    lines.append("\n── Thresholds")
    lines.append(f"   entry_alert_min:        {ALERT_MIN_CONFIDENCE}%")
    lines.append(f"   watch_alert_min:        {WATCH_ALERT_MIN_CONFIDENCE}%")
    lines.append(f"   watch_alert_cooldown:   {WATCH_ALERT_COOLDOWN_MINUTES}min")

    if overrides_active:
        override_keys = ", ".join(TUNING_OVERRIDES.keys())
        lines.append(f"\n✏️ Active overrides: {override_keys}")
        lines.append("Edit TUNING_OVERRIDES in config.py to adjust weights.")
    else:
        lines.append("\nAll defaults active (no overrides set).")
        lines.append("Edit TUNING_OVERRIDES in config.py to apply tuning recommendations.")

    bot.reply_to(message, "\n".join(lines))


# ── Copy Trading Commands ─────────────────────────────────────────────────────

def _copy_check_approved(message) -> bool:
    uid = message.from_user.id
    if is_admin(uid):
        return True
    if not is_approved(uid):
        bot.reply_to(message, "❌ You need an approved plan to use copy trading.")
        return False
    return True


# /connectctrader — 2-step: access token + account ID
@bot.message_handler(commands=["setctraderapp"])
def cmd_setctraderapp(message):
    if not (message.from_user and message.from_user.id in ADMIN_IDS):
        bot.reply_to(message, "❌ Admin only.")
        return
    parts = (message.text or "").strip().split(maxsplit=2)
    if len(parts) < 3:
        bot.reply_to(
            message,
            "Usage: /setctraderapp <client_id> <client_secret>\n"
            "Get your credentials at https://connect.spotware.com/apps",
            parse_mode="Markdown",
        )
        return
    client_id, client_secret = parts[1].strip(), parts[2].strip()
    import os as _os, json as _sca_json
    _bd_path = _os.path.join(_os.path.dirname(__file__), "bot_data.json")
    _bd: dict = {}
    try:
        if _os.path.exists(_bd_path):
            _bd = _sca_json.loads(open(_bd_path).read())
    except Exception:
        pass
    _bd["ctrader_app_config"] = {"client_id": client_id, "client_secret": client_secret}
    with open(_bd_path, "w") as _f:
        _f.write(_sca_json.dumps(_bd, indent=2))
    bot.reply_to(
        message,
        f"✅ cTrader app credentials saved.\n"
        f"Client ID: `{client_id}`\n"
        "Users can now run /connectctrader to link their accounts.",
        parse_mode="Markdown",
    )


@bot.message_handler(commands=["connectctrader"])
def cmd_connectctrader(message):
    if not _copy_check_approved(message):
        return
    # Check if credentials exist via env or bot_data
    import os as _os, json as _bd_json
    _ct_client_id = _os.getenv("CTRADER_CLIENT_ID", "").strip()
    if not _ct_client_id:
        try:
            _bd_path = _os.path.join(_os.path.dirname(__file__), "bot_data.json")
            if _os.path.exists(_bd_path):
                _bd = _bd_json.loads(open(_bd_path).read())
                _ct_client_id = _bd.get("ctrader_app_config", {}).get("client_id", "")
        except Exception:
            pass
    if not _ct_client_id:
        is_admin = message.from_user and message.from_user.id in ADMIN_IDS
        admin_hint = "\n\nAs admin, run: /setctraderapp <client_id> <client_secret>" if is_admin else ""
        bot.reply_to(
            message,
            "⚠️ *cTrader not configured yet.*\n\n"
            "The admin needs to configure the cTrader app credentials first.\n\n"
            "Contact the bot admin to enable copy trading." + admin_hint,
            parse_mode="Markdown",
        )
        return

    msg = bot.reply_to(
        message,
        "🔗 *IC Markets Copy Trading Setup*\n\n"
        "*Step 1 of 2 — Access Token*\n\n"
        "Get your token here:\n"
        "👉 https://connect.spotware.com/apps/auth\n\n"
        "Log in with your IC Markets cTrader account, then copy and paste "
        "your access token below.\n\n"
        "Type /cancel to abort.",
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )
    bot.register_next_step_handler(msg, _ct_step_token)


def _ct_step_token(message):
    if message.text and message.text.strip().startswith("/"):
        bot.reply_to(message, "Cancelled.")
        return
    token = (message.text or "").strip()
    if not token:
        bot.reply_to(message, "❌ Token cannot be empty. Try /connectctrader again.")
        return
    msg = bot.reply_to(
        message,
        "*Step 2 of 2 — Account ID*\n\n"
        "Enter your numeric cTrader account ID.\n"
        "_(You can find it in the IC Markets cTrader app or web portal)_",
        parse_mode="Markdown",
    )
    bot.register_next_step_handler(msg, _ct_step_account, token)


def _ct_step_account(message, token):
    if message.text and message.text.strip().startswith("/"):
        bot.reply_to(message, "Cancelled.")
        return
    account_id = (message.text or "").strip()
    if not account_id.isdigit():
        bot.reply_to(message,
                     "❌ Account ID must be a number (e.g. 12345678).\n"
                     "Try /connectctrader again.")
        return

    uid_str = str(message.from_user.id)
    creds   = {
        "access_token": token,
        "account_id":   account_id,
        "environment":  "demo",   # default; user can switch with /setenv
    }

    bot.reply_to(message, "⏳ Testing connection to IC Markets...")

    try:
        from broker_ctrader import CTraderConnector
        from copy_trading_store import link_broker
        connector = CTraderConnector()
        result    = connector.connect(creds)
        connector.disconnect()

        if not result["ok"]:
            bot.reply_to(
                message,
                f"❌ *Connection failed*\n\n{result['error']}\n\n"
                "Double-check your access token and account ID, then try /connectctrader again.",
                parse_mode="Markdown",
            )
            return

        link_broker(uid_str, "ctrader", creds)
        bot.reply_to(
            message,
            f"✅ *IC Markets Connected!*\n\n"
            f"Account ID: {account_id}\n\n"
            f"Copy trading is *OFF* by default.\n"
            f"Use /copyon when you're ready to start auto-executing trades.\n\n"
            f"Set risk per trade with /setrisk (default 1%).",
            parse_mode="Markdown",
        )
    except Exception as e:
        bot.reply_to(message, f"❌ Error connecting: {e}\n\nTry /connectctrader again.")


# /copyon — enable copy trading
@bot.message_handler(commands=["copyon"])
def cmd_copyon(message):
    if not _copy_check_approved(message):
        return
    from copy_trading_store import set_copy_enabled, get_user_copy_settings
    uid_str  = str(message.from_user.id)
    settings = get_user_copy_settings(uid_str)
    if not settings:
        bot.reply_to(message,
                     "❌ No broker linked yet.\n"
                     "Use /connectctrader first.")
        return
    set_copy_enabled(uid_str, True)
    broker = settings.get("broker", "?").upper()
    rp     = settings.get("risk_pct", 1.0)
    mt     = settings.get("max_trades", 3)
    bot.reply_to(
        message,
        f"✅ *Copy Trading ENABLED*\n\n"
        f"Broker: {broker}\n"
        f"Risk per trade: {rp}%\n"
        f"Max open trades: {mt}\n\n"
        f"The next scanner signal will execute automatically on your account.",
        parse_mode="Markdown",
    )


# /copyoff — disable copy trading
@bot.message_handler(commands=["copyoff"])
def cmd_copyoff(message):
    if not _copy_check_approved(message):
        return
    from copy_trading_store import set_copy_enabled
    uid_str = str(message.from_user.id)
    ok      = set_copy_enabled(uid_str, False)
    if ok:
        bot.reply_to(message, "🔴 Copy trading *DISABLED*. No more auto-execution.",
                     parse_mode="Markdown")
    else:
        bot.reply_to(message, "No broker linked. Nothing to disable.")


# /copystatus — show current copy trading status
@bot.message_handler(commands=["copystatus"])
def cmd_copystatus(message):
    if not _copy_check_approved(message):
        return
    from copy_engine import get_user_broker_status
    uid_str = str(message.from_user.id)
    status  = get_user_broker_status(uid_str)

    if not status["linked"]:
        bot.reply_to(message,
                     "No broker linked.\n"
                     "Use /connectoanda or /connectctrader to get started.")
        return

    enabled_str   = "🟢 ENABLED"  if status["enabled"]   else "🔴 DISABLED"
    connected_str = "✅ Live"     if status["connected"]  else "❌ Offline"
    broker        = status.get("broker", "?").upper()
    balance       = status.get("balance", 0)
    currency      = status.get("currency", "USD")
    open_trades   = status.get("open_trades", [])
    rp            = status.get("risk_pct", 1.0)
    mt            = status.get("max_trades", 3)

    trade_lines = ""
    for t in open_trades:
        pnl   = t.get("unrealized_pnl", 0)
        arrow = "↑" if t["direction"] == "BUY" else "↓"
        trade_lines += (
            f"\n  {arrow} {t['symbol']} {t['direction']} "
            f"| P&L: {'+' if pnl >= 0 else ''}{round(pnl, 2)}"
        )

    lines = [
        f"📊 *COPY TRADING STATUS*\n",
        f"Status:    {enabled_str}",
        f"Broker:    {broker}",
        f"API:       {connected_str}",
        f"Balance:   {round(balance, 2)} {currency}",
        f"Risk/trade: {rp}%",
        f"Max trades: {mt}",
        f"Open now:   {len(open_trades)}",
    ]
    if trade_lines:
        lines.append(f"\nOpen positions:{trade_lines}")
    lines.append("\n/copyon  /copyoff  /setrisk  /copytrades  /disconnectbroker")

    bot.reply_to(message, "\n".join(lines), parse_mode="Markdown")


# /copytrades — show recent trade history
@bot.message_handler(commands=["copytrades"])
def cmd_copytrades(message):
    if not _copy_check_approved(message):
        return
    from copy_trading_store import get_user_trade_history
    uid_str = str(message.from_user.id)
    history = get_user_trade_history(uid_str, limit=10)

    if not history:
        bot.reply_to(message, "No copy trades recorded yet.")
        return

    lines = ["📋 *YOUR LAST COPY TRADES*\n"]
    for r in reversed(history):
        pair      = r.get("pair", "?")
        direction = r.get("direction", "?")
        outcome   = r.get("outcome", "?")
        lots      = r.get("lots", 0)
        pnl       = r.get("pnl")
        error     = r.get("error", "")

        if not r.get("ok"):
            icon   = "❌"
            detail = error or "rejected"
        elif outcome == "tp_hit":
            icon   = "✅"
            detail = f"TP hit | P&L: +{round(pnl or 0, 2)}"
        elif outcome == "sl_hit":
            icon   = "🛑"
            detail = f"SL hit | P&L: {round(pnl or 0, 2)}"
        elif outcome == "open":
            icon   = "🔵"
            detail = "Still open"
        else:
            icon   = "📤"
            detail = outcome

        arrow = "↑" if direction == "BUY" else "↓"
        lines.append(f"{icon} {pair} {arrow} | {lots} lots | {detail}")

    bot.reply_to(message, "\n".join(lines), parse_mode="Markdown")


# /setrisk — change risk percentage per trade
@bot.message_handler(commands=["setrisk"])
def cmd_setrisk(message):
    if not _copy_check_approved(message):
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        bot.reply_to(message, "Usage: /setrisk 1.5\n_(Risk % per trade, range 0.1–10)_",
                     parse_mode="Markdown")
        return
    try:
        pct = float(parts[1])
    except ValueError:
        bot.reply_to(message, "❌ Invalid number. Example: /setrisk 1.5")
        return

    from copy_trading_store import set_risk_pct
    uid_str = str(message.from_user.id)
    ok      = set_risk_pct(uid_str, pct)
    if ok:
        final = max(0.1, min(10.0, pct))
        bot.reply_to(message, f"✅ Risk set to *{final}%* per trade.", parse_mode="Markdown")
    else:
        bot.reply_to(message, "No broker linked yet. Use /connectctrader first.")


# /disconnectbroker — unlink broker and delete credentials
@bot.message_handler(commands=["disconnectbroker"])
def cmd_disconnectbroker(message):
    if not _copy_check_approved(message):
        return
    from copy_trading_store import save_user_copy_settings, get_user_copy_settings
    from copy_engine import disconnect_user
    uid_str  = str(message.from_user.id)
    settings = get_user_copy_settings(uid_str)
    if not settings:
        bot.reply_to(message, "No broker linked.")
        return
    disconnect_user(uid_str)
    save_user_copy_settings(uid_str, {})   # wipe credentials
    bot.reply_to(message,
                 "✅ Broker disconnected and credentials removed.\n"
                 "Use /connectctrader to reconnect.")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("Starting Chefbuntsa Forex Bot...", flush=True)
    print(f"Admin IDs: {ADMIN_IDS}", flush=True)
    print(f"Scanner interval: {SCAN_INTERVAL_SECONDS}s | Alert threshold: from config", flush=True)

    # Restore circuit breaker state from historical outcomes
    try:
        import circuit_breaker
        circuit_breaker.refresh_from_storage()
        print("[Startup] Circuit breaker state restored from storage.", flush=True)
    except Exception as _cbe:
        print(f"[Startup] Circuit breaker restore error: {_cbe}", flush=True)

    try:
        import loss_streak
        loss_streak.refresh_from_storage()
        print("[Startup] Loss streak state restored from storage.", flush=True)
    except Exception as _lse:
        print(f"[Startup] Loss streak restore error: {_lse}", flush=True)

    try:
        import adaptive_brain
        adaptive_brain.refresh_from_storage()
        print("[Startup] Adaptive brain state restored from storage.", flush=True)
    except Exception as _abe:
        print(f"[Startup] Adaptive brain restore error: {_abe}", flush=True)

    def _notify(chat_id: int, text: str):
        safe_send(chat_id, text)

    health_thread    = threading.Thread(target=run_health_server, daemon=True)
    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    outcome_thread   = threading.Thread(
        target=run_outcome_checker_loop,
        kwargs={"notify_callback": _notify, "interval_seconds": 1800},
        daemon=True
    )
    health_thread.start()
    scheduler_thread.start()
    outcome_thread.start()
    print("Outcome checker thread started (30-min intervals).", flush=True)

    try:
        from trade_monitor import start_monitor
        from config import COPY_MONITOR_INTERVAL_SECS
        start_monitor(notify_callback=_notify, interval_seconds=COPY_MONITOR_INTERVAL_SECS)
    except Exception as _tme:
        print(f"[Startup] Trade monitor start error: {_tme}", flush=True)

    print("Bot polling started.", flush=True)
    while True:
        try:
            bot.infinity_polling(timeout=30, long_polling_timeout=30, skip_pending=True)
        except Exception as e:
            print(f"Polling error: {e}. Retrying in 15s...", flush=True)
            time.sleep(15)


if __name__ == "__main__":
    main()
