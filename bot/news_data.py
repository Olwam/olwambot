import time
import requests
import feedparser
import re
from datetime import datetime, timezone, timedelta

from config import FINNHUB_API_KEY, REQUEST_TIMEOUT

http = requests.Session()

_CALENDAR_CACHE = {"events": [], "fetched_at": 0}
_CALENDAR_TTL_SECONDS = 600

_HEADLINES_CACHE = {"headlines": [], "fetched_at": 0}
_HEADLINES_TTL_SECONDS = 300

CURRENCY_COUNTRY_MAP = {
    "USD": ["US", "United States"],
    "EUR": ["EU", "EMU", "Eurozone", "European"],
    "GBP": ["GB", "UK", "United Kingdom"],
    "JPY": ["JP", "Japan"],
    "AUD": ["AU", "Australia"],
    "CAD": ["CA", "Canada"],
    "NZD": ["NZ", "New Zealand"],
    "CHF": ["CH", "Switzerland"],
    "XAU": ["US", "United States"],
    "XAG": ["US", "United States"],
}

CURRENCY_KEYWORDS = {
    "USD": ["usd", "dollar", "fed", "fomc", "nonfarm", "us economy", "treasury"],
    "EUR": ["eur", "euro", "ecb", "eurozone"],
    "GBP": ["gbp", "pound", "sterling", "boe", "bank of england"],
    "JPY": ["jpy", "yen", "boj", "bank of japan"],
    "AUD": ["aud", "aussie", "rba", "australia"],
    "CAD": ["cad", "loonie", "boc", "canada", "canadian"],
    "NZD": ["nzd", "kiwi", "rbnz", "new zealand"],
    "CHF": ["chf", "franc", "snb", "swiss"],
    "XAU": ["gold", "xau", "precious metal", "bullion"],
    "XAG": ["silver", "xag"],
}

RSS_FEEDS = [
    "https://www.fxstreet.com/rss",
    "https://www.dailyfx.com/feeds/all",
    "https://www.forexlive.com/feed",
    "https://www.investing.com/rss/news_14.rss",
]


# =============================================================================
# RSS FEED — live forex news headlines (free, no key needed)
# =============================================================================

def _fetch_all_rss_headlines() -> list:
    global _HEADLINES_CACHE
    now_ts = time.time()

    if _HEADLINES_CACHE["headlines"] and (now_ts - _HEADLINES_CACHE["fetched_at"]) < _HEADLINES_TTL_SECONDS:
        return _HEADLINES_CACHE["headlines"]

    all_headlines = []
    for feed_url in RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:15]:
                title = entry.get("title", "").strip()
                if not title:
                    continue
                summary = entry.get("summary", "")
                if summary:
                    summary = re.sub(r"<[^>]+>", "", summary).strip()[:200]
                all_headlines.append({
                    "title": title,
                    "summary": summary,
                    "source": feed.feed.get("title", feed_url),
                    "link": entry.get("link", ""),
                })
        except Exception as e:
            print(f"  [News] RSS feed error ({feed_url}): {e}", flush=True)

    _HEADLINES_CACHE["headlines"] = all_headlines
    _HEADLINES_CACHE["fetched_at"] = now_ts
    print(f"  [News] RSS feeds returned {len(all_headlines)} total headlines.", flush=True)
    return all_headlines


def _rss_headlines_for_pair(pair: str) -> list:
    pair = pair.upper().replace("/", "")
    currencies = [pair[:3], pair[3:6]] if len(pair) >= 6 else []

    keywords = set()
    for cur in currencies:
        for kw in CURRENCY_KEYWORDS.get(cur, []):
            keywords.add(kw)
        keywords.add(cur.lower())

    if not keywords:
        return []

    all_headlines = _fetch_all_rss_headlines()

    relevant = []
    for h in all_headlines:
        text = (h["title"] + " " + h.get("summary", "")).lower()
        if any(kw in text for kw in keywords):
            relevant.append(h["title"])

    print(f"  [News] {pair}: {len(relevant)} relevant headlines from RSS.", flush=True)
    return relevant[:5]


# =============================================================================
# ECONOMIC CALENDAR — high-impact event timing
# =============================================================================

def _free_calendar_events() -> list:
    """ForexFactory weekly calendar — always available, no key needed."""
    try:
        url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
        print("  [News] Fetching free ForexFactory calendar...", flush=True)
        r = http.get(url, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        events = data if isinstance(data, list) else []
        high = [e for e in events if str(e.get("impact", "")).lower() == "high"]
        print(f"  [News] Free calendar: {len(high)} high-impact events this week.", flush=True)
        return events
    except Exception as e:
        print(f"  [News] Free calendar error: {e}", flush=True)
        return []


def _finnhub_calendar_events() -> list:
    """Finnhub economic calendar — used if key is present."""
    if not FINNHUB_API_KEY:
        return []
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        url = "https://finnhub.io/api/v1/calendar/economic"
        params = {"from": today, "to": today, "token": FINNHUB_API_KEY}
        print("  [News] Fetching Finnhub economic calendar...", flush=True)
        r = http.get(url, params=params, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        events = data.get("economicCalendar", [])
        result = []
        for e in events:
            impact = str(e.get("impact", "")).lower()
            if impact not in ("high", "3"):
                continue
            result.append({
                "title":    e.get("event", "Unknown"),
                "country":  e.get("country", ""),
                "date":     e.get("time", ""),
                "impact":   "High",
                "forecast": e.get("estimate", "—") or "—",
                "previous": e.get("prev", "—") or "—",
            })
        print(f"  [News] Finnhub returned {len(result)} high-impact events.", flush=True)
        return result
    except Exception as e:
        print(f"  [News] Finnhub error: {e}", flush=True)
        return []


def parse_event_datetime(date_str: str):
    if not date_str:
        return None
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(date_str[:16], fmt[:16])
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    try:
        dt = datetime.fromisoformat(date_str)
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    except Exception:
        return None


def fetch_calendar_events() -> list:
    """
    Returns high-impact economic calendar events.
    Results are cached for 10 minutes to avoid rate-limiting the free endpoint.
    Priority: Finnhub > free ForexFactory feed.
    """
    global _CALENDAR_CACHE
    now_ts = time.time()

    # Return cached data if still fresh
    if _CALENDAR_CACHE["events"] and (now_ts - _CALENDAR_CACHE["fetched_at"]) < _CALENDAR_TTL_SECONDS:
        return _CALENDAR_CACHE["events"]

    # Fetch fresh
    events = []
    if FINNHUB_API_KEY:
        events = _finnhub_calendar_events()
    if not events:
        events = _free_calendar_events()

    _CALENDAR_CACHE["events"] = events
    _CALENDAR_CACHE["fetched_at"] = now_ts
    return events


def fetch_todays_high_impact_news() -> list:
    today = datetime.now(timezone.utc).date()
    high_impact = []
    for e in fetch_calendar_events():
        if str(e.get("impact", "")).lower() != "high":
            continue
        dt = parse_event_datetime(e.get("date", ""))
        if dt and dt.date() == today:
            high_impact.append(e)
    high_impact.sort(
        key=lambda x: parse_event_datetime(x.get("date", ""))
        or datetime.max.replace(tzinfo=timezone.utc)
    )
    return high_impact


def event_id(e: dict) -> str:
    return f"{e.get('date','')}|{e.get('country','')}|{e.get('title','')}"


def format_news_event(e: dict) -> str:
    title    = e.get("title", "Unknown Event")
    country  = e.get("country", "")
    forecast = e.get("forecast", "—")
    previous = e.get("previous", "—")
    dt       = parse_event_datetime(e.get("date", ""))
    time_part = dt.strftime("%H:%M UTC") if dt else "TBA"
    return (
        f"🔴 {country} — {title}\n"
        f"⏰ {time_part}\n"
        f"📊 Forecast: {forecast} | Previous: {previous}"
    )


def get_relevant_news_block(pair: str, minutes_ahead: int = 60) -> dict:
    """
    Returns news risk level for a pair.
    Also fetches ForexNewsAPI headlines for sentiment context.
    """
    pair = pair.upper().replace("/", "")
    currencies = [pair[:3], pair[3:6]] if len(pair) >= 6 else []

    relevant_countries = set()
    for cur in currencies:
        for country in CURRENCY_COUNTRY_MAP.get(cur, []):
            relevant_countries.add(country.upper())

    headlines = _rss_headlines_for_pair(pair)

    now_utc = datetime.now(timezone.utc)
    events   = fetch_calendar_events()
    upcoming = []

    for e in events:
        if str(e.get("impact", "")).lower() != "high":
            continue
        dt = parse_event_datetime(e.get("date", ""))
        if not dt:
            continue
        diff_minutes = (dt - now_utc).total_seconds() / 60.0
        if diff_minutes < -5 or diff_minutes > minutes_ahead:
            continue
        country = str(e.get("country", "")).upper()
        if country in relevant_countries or not relevant_countries:
            upcoming.append({
                "event":         e,
                "minutes_until": round(diff_minutes, 1),
                "country":       country,
            })

    sentiment_note = ""
    if headlines:
        sentiment_note = f" Recent news: {headlines[0][:80]}"

    if not upcoming:
        print(f"  [News] No high-impact events near {pair} in next {minutes_ahead}min.{sentiment_note}", flush=True)
        return {"risk": "low", "events": [], "message": "No high-impact news nearby.", "headlines": headlines}

    closest = min(upcoming, key=lambda x: abs(x["minutes_until"]))
    mins    = closest["minutes_until"]

    if mins <= 10:
        risk = "high"
        msg  = f"High-impact news in ~{int(max(mins,0))} min — avoid trading."
    elif mins <= 30:
        risk = "medium"
        msg  = f"High-impact news in ~{int(mins)} min — trade with caution."
    else:
        risk = "low"
        msg  = f"News in ~{int(mins)} min — manageable risk."

    print(f"  [News] {pair} news risk: {risk.upper()} — {msg}{sentiment_note}", flush=True)
    return {"risk": risk, "events": upcoming, "message": msg, "headlines": headlines}
