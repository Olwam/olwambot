"""
Automatic market scanner and alert engine.

Two-stage alert system:
  WATCH alert  — setup forming (confidence >= effective_watch threshold)
  ENTRY alert  — all filters passed (confidence >= effective_entry threshold)

Hard gates (abort before scoring):
  1. market_closed   — forex weekend / after-hours schedule
  2. stale_candle    — most recent candle older than threshold
  3. stale_quote     — quote missing or timestamped stale
  4. dead_hours      — silent gate (not logged)
  5. volatility      — dead/chaotic market (if VOLATILITY_HARD_ABORT=true)

Phase 2 additions:
  - Structure signal scoring (sweep, FVG, OB, breaker, fib, volatility)
  - Circuit breaker integration (tightened thresholds after loss streak)
  - Session-adaptive confidence thresholds
  - Dynamic scan interval per session
"""

import hashlib
import time
from datetime import datetime, timezone, timedelta

from config import (
    ALERT_MIN_CONFIDENCE,
    ALERT_PAIR_COOLDOWN_MINUTES,
    ADMIN_IDS,
    WATCH_ALERT_MIN_CONFIDENCE,
    WATCH_ALERT_COOLDOWN_MINUTES,
    CORRELATED_PAIR_GROUPS,
    CORRELATED_ALERT_WINDOW_MINUTES,
    SESSION_THRESHOLD_OFFSETS,
    SESSION_SCAN_INTERVALS,
    SCAN_INTERVAL_SECONDS,
    SCAN_TIMEFRAMES,
    VOLATILITY_HARD_ABORT,
    ATR_SL_MULTIPLIER,
    ENTRY_CANDLE_BODY_ATR_MAX_RATIO,
    EMA_DISTANCE_ATR_MAX,
    LOW_VOLATILITY_ATR_RATIO,
    DIRECTION_FLIP_DELAY_MINUTES,
)
from market_data import (
    get_candles, get_quote, timeframe_to_interval,
    has_market_data, normalize_symbol, get_higher_timeframe_context,
    compute_mtf_alignment,
)
from market_status import is_market_open, is_candle_stale, is_quote_stale
from indicators import compute_market_context, estimate_atr, find_support_resistance
from news_data import get_relevant_news_block
from sessions import get_session_score, get_session_label, is_dead_hours, get_current_sessions
from decision_engine import SYMBOL_RULES
from structure_signals import compute_structure_signals
from storage import (
    load_data, save_scanner_alert, save_pre_alert, save_rejection,
    get_pending_watch_users, clear_pending_watch, cleanup_expired_watches,
)
import circuit_breaker
import loss_streak
import smc as smc_engine

DEFAULT_SCAN_PAIRS = [
    # Major pairs
    "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "USDCAD", "AUDUSD", "NZDUSD",
    # Cross pairs
    "EURJPY", "GBPJPY", "EURGBP", "EURAUD", "GBPAUD", "AUDJPY",
    # Commodities
    "XAUUSD",
]
SCAN_TIMEFRAME = SCAN_TIMEFRAMES[0] if SCAN_TIMEFRAMES else "M15"  # legacy fallback

# ── Correlated pair lookup ─────────────────────────────────────────────────────
_CORR_MAP: dict = {}
for _group in CORRELATED_PAIR_GROUPS:
    for _sym in _group:
        _CORR_MAP[_sym] = [s for s in _group if s != _sym]

# ── Deduplication state ────────────────────────────────────────────────────────
_LAST_ALERT_TIMES         = {}
_LAST_ALERT_HASHES        = set()
_LAST_ALERT_DIRECTION     = {}
_LAST_ALERT_DIRECTION_TIME = {}   # {pair: (direction, epoch_ts)} for flip-delay
_LAST_WATCH_TIMES         = {}
_LAST_WATCH_HASHES        = set()


# ── Deduplication helpers ──────────────────────────────────────────────────────

def _setup_hash(pair: str, direction: str, entry: float) -> str:
    raw = f"{pair}|{direction}|{entry:.4f}"
    return hashlib.md5(raw.encode()).hexdigest()[:12]


def _should_send_alert(pair: str, setup_hash: str) -> bool:
    cooldown = ALERT_PAIR_COOLDOWN_MINUTES * 60
    now_ts   = time.time()
    return (
        now_ts - _LAST_ALERT_TIMES.get(pair, 0) >= cooldown and
        setup_hash not in _LAST_ALERT_HASHES
    )


def _should_send_watch(pair: str, watch_hash: str) -> bool:
    cooldown = WATCH_ALERT_COOLDOWN_MINUTES * 60
    now_ts   = time.time()
    return (
        now_ts - _LAST_WATCH_TIMES.get(pair, 0) >= cooldown and
        watch_hash not in _LAST_WATCH_HASHES
    )


def _is_correlated_duplicate(pair: str, direction: str) -> tuple:
    window = CORRELATED_ALERT_WINDOW_MINUTES * 60
    now_ts = time.time()
    for sibling in _CORR_MAP.get(pair, []):
        if (now_ts - _LAST_ALERT_TIMES.get(sibling, 0) < window and
                _LAST_ALERT_DIRECTION.get(sibling) == direction):
            return True, sibling
    return False, None


def _mark_alerted(pair: str, setup_hash: str, direction: str = ""):
    now = time.time()
    _LAST_ALERT_TIMES[pair]     = now
    _LAST_ALERT_DIRECTION[pair] = direction
    _LAST_ALERT_HASHES.add(setup_hash)
    if direction:
        _LAST_ALERT_DIRECTION_TIME[pair] = (direction, now)
    if len(_LAST_ALERT_HASHES) > 500:
        _LAST_ALERT_HASHES.clear()


def _is_direction_flip_blocked(pair: str, direction: str) -> bool:
    """Returns True if we recently sent an alert in the opposite direction for this pair."""
    entry = _LAST_ALERT_DIRECTION_TIME.get(pair)
    if not entry:
        return False
    last_dir, last_ts = entry
    if last_dir == direction:
        return False
    elapsed_minutes = (time.time() - last_ts) / 60
    return elapsed_minutes < DIRECTION_FLIP_DELAY_MINUTES


def _mark_watch_alerted(pair: str, watch_hash: str):
    _LAST_WATCH_TIMES[pair] = time.time()
    _LAST_WATCH_HASHES.add(watch_hash)
    if len(_LAST_WATCH_HASHES) > 500:
        _LAST_WATCH_HASHES.clear()


# ── Session-adaptive threshold helper ─────────────────────────────────────────

def _session_threshold_offset(now_utc: datetime) -> int:
    active = get_current_sessions(now_utc)
    if is_dead_hours(now_utc):
        return SESSION_THRESHOLD_OFFSETS.get("dead", +8)
    if "overlap" in active:
        return SESSION_THRESHOLD_OFFSETS.get("overlap", 0)
    if "london" in active:
        return SESSION_THRESHOLD_OFFSETS.get("london", 0)
    if "new_york" in active:
        return SESSION_THRESHOLD_OFFSETS.get("new_york", 0)
    if "asian" in active:
        return SESSION_THRESHOLD_OFFSETS.get("asian", +4)
    return SESSION_THRESHOLD_OFFSETS.get("default", +2)


def get_current_scan_interval(now_utc: datetime = None) -> int:
    """Returns session-appropriate scan interval in seconds."""
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    if is_dead_hours(now_utc):
        return SESSION_SCAN_INTERVALS.get("dead", SCAN_INTERVAL_SECONDS)
    active = get_current_sessions(now_utc)
    if "overlap"  in active:
        return SESSION_SCAN_INTERVALS.get("overlap",  SCAN_INTERVAL_SECONDS)
    if "london"   in active:
        return SESSION_SCAN_INTERVALS.get("london",   SCAN_INTERVAL_SECONDS)
    if "new_york" in active:
        return SESSION_SCAN_INTERVALS.get("new_york", SCAN_INTERVAL_SECONDS)
    if "asian"    in active:
        return SESSION_SCAN_INTERVALS.get("asian",    SCAN_INTERVAL_SECONDS)
    return SESSION_SCAN_INTERVALS.get("default", SCAN_INTERVAL_SECONDS)


# ── Setup detection ────────────────────────────────────────────────────────────

def scan_pair_for_setup(symbol: str, timeframe: str = None) -> dict | None:
    """
    Analyses a pair using live candles and returns a scored setup dict.
    Sets 'is_watch_alert': True if below entry threshold but above watch threshold.
    Returns None if below watch threshold or hard-gated.
    """
    sym     = normalize_symbol(symbol)
    now_utc = datetime.now(timezone.utc)

    # ── GATE 1: Market open ────────────────────────────────────────────────────
    market_ok, market_reason = is_market_open(sym, now_utc)
    if not market_ok:
        save_rejection(sym, "market_closed", market_reason, source="scanner")
        print(f"  [Scanner] {sym} gated: {market_reason}", flush=True)
        return None

    # ── GATE 1b: Per-pair loss streak protection ───────────────────────────────
    pair_blocked, pair_block_reason = loss_streak.is_pair_blocked(sym)
    if pair_blocked:
        save_rejection(sym, "loss_streak_block", pair_block_reason, source="scanner")
        print(f"  [Scanner] {sym} gated: {pair_block_reason}", flush=True)
        return None

    # ── Silent gate: dead hours ────────────────────────────────────────────────
    if is_dead_hours(now_utc):
        return None

    # ── Silent gate: session quality ───────────────────────────────────────────
    session_score = get_session_score(sym, now_utc)
    session_label = get_session_label(now_utc)
    if session_score < 7:
        return None

    # Resolve timeframe for this scan
    tf       = (timeframe or SCAN_TIMEFRAME).strip().upper()

    # ── Candle data ────────────────────────────────────────────────────────────
    interval = timeframe_to_interval(tf)
    candles  = get_candles(sym, interval, outputsize=100)
    if len(candles) < 30:
        return None

    # ── GATE 2: Stale candle ──────────────────────────────────────────────────
    latest_ts = candles[-1].get("datetime", "")
    stale_c, stale_c_reason = is_candle_stale(tf, latest_ts, now_utc)
    if stale_c:
        save_rejection(sym, "stale_candle", stale_c_reason, source="scanner")
        print(f"  [Scanner] {sym} gated: {stale_c_reason}", flush=True)
        return None

    quote = get_quote(sym)

    # ── GATE 3: Stale / missing quote ─────────────────────────────────────────
    stale_q, stale_q_reason = is_quote_stale(sym, quote, now_utc)
    if stale_q:
        save_rejection(sym, "stale_quote", stale_q_reason, source="scanner")
        print(f"  [Scanner] {sym} gated: {stale_q_reason}", flush=True)
        return None

    current_price = quote.get("price")
    if not current_price:
        return None

    # ── Market context ─────────────────────────────────────────────────────────
    ctx = compute_market_context(candles)
    if ctx["trend_bias"] == "neutral":
        return None

    atr          = ctx.get("atr", 0)
    expected_atr = estimate_atr(sym, current_price)
    if atr <= 0:
        atr = expected_atr
    if atr <= 0:
        return None

    # ── GATE 4 (Phase 2): Volatility hard abort ────────────────────────────────
    if VOLATILITY_HARD_ABORT and len(candles) >= 10:
        from structure_signals import score_volatility
        vol = score_volatility(atr, expected_atr)
        if vol["label"] in ("dead", "chaotic"):
            save_rejection(sym, "volatility_blocked",
                           f"{sym} volatility {vol['label']} — {vol['description']}",
                           source="scanner")
            print(f"  [Scanner] {sym} gated: {vol['description']}", flush=True)
            return None

    # ── Minimum ATR check ──────────────────────────────────────────────────────
    rules   = SYMBOL_RULES.get(sym, {})
    min_atr = rules.get("min_atr", 0.0)
    if min_atr > 0 and atr < min_atr * 0.5:
        return None

    # ── Multi-timeframe confluence (LTF + MTF + HTF must all align) ───────────
    from market_data import get_higher_timeframe
    htf_ctx  = get_higher_timeframe_context(sym, tf)
    htf_bias = htf_ctx.get("trend_bias", "neutral") if htf_ctx else "neutral"
    htf_tf   = get_higher_timeframe(tf)

    # Soft penalty for HTF neutral (no hard reject — only HTF *opposition* is fatal)
    htf_neutral_penalty = 0
    if htf_bias == "neutral":
        htf_neutral_penalty = -8
        print(f"  [Scanner] {sym} {htf_tf} neutral — soft penalty {htf_neutral_penalty}pp", flush=True)
    elif htf_bias != ctx["trend_bias"]:
        save_rejection(sym, "htf_conflict",
                       f"{htf_tf} {htf_bias} opposes LTF {ctx['trend_bias']} — skipping {sym}",
                       source="scanner")
        return None

    # ── 3rd timeframe (HTF of HTF) — required for M15 and H1 scans ────────────
    htf2_bias = "neutral"
    htf2_tf   = get_higher_timeframe(htf_tf)
    htf2_neutral_penalty = 0
    if tf in ("M15", "H1"):
        htf2_ctx  = get_higher_timeframe_context(sym, htf_tf)
        htf2_bias = htf2_ctx.get("trend_bias", "neutral") if htf2_ctx else "neutral"
        if htf2_bias == "neutral":
            htf2_neutral_penalty = -5
            print(f"  [Scanner] {sym} {htf2_tf} neutral — soft penalty {htf2_neutral_penalty}pp", flush=True)
        elif htf2_bias != ctx["trend_bias"]:
            save_rejection(sym, "htf2_conflict",
                           f"{htf2_tf} {htf2_bias} conflicts — skipping {sym}",
                           source="scanner")
            return None

    if htf_ctx:
        htf_regime = htf_ctx.get("regime", "mixed")
        if htf_regime in ("range", "mixed") and ctx.get("regime") not in ("trending", "pullback"):
            save_rejection(sym, "mixed_regime",
                           f"{htf_tf} regime '{htf_regime}' with LTF '{ctx.get('regime')}' — no edge",
                           source="scanner")
            return None

    regime = ctx.get("regime", "mixed")
    if regime in ("range", "mixed"):
        save_rejection(sym, "bad_regime",
                       f"{sym} M15 regime '{regime}' — no directional edge",
                       source="scanner")
        return None

    # ── News check ─────────────────────────────────────────────────────────────
    news_block    = get_relevant_news_block(sym)
    news_risk     = news_block.get("risk", "low")
    news_countdown = news_block.get("message", "")
    if news_risk in ("high", "medium"):
        save_rejection(sym, "news_risk",
                       f"{news_risk.title()} news risk blocked {sym} scanner",
                       source="scanner")
        return None

    direction = "BUY" if ctx["trend_bias"] == "bullish" else "SELL"

    # ── Direction flip delay ────────────────────────────────────────────────────
    if _is_direction_flip_blocked(sym, direction):
        save_rejection(sym, "direction_flip_blocked",
                       f"{sym} direction flip within {DIRECTION_FLIP_DELAY_MINUTES}min — too soon",
                       source="scanner")
        return None

    # ── SNIPER: Price location guard — don't enter mid-range ─────────────────
    sr_support, sr_resistance = find_support_resistance(candles, current_price=current_price)
    swing_hi = ctx.get("swing_high")
    swing_lo = ctx.get("swing_low")

    near_support    = False
    near_resistance = False
    if sr_support and atr > 0:
        near_support = abs(current_price - sr_support) < atr * 1.5
    if sr_resistance and atr > 0:
        near_resistance = abs(sr_resistance - current_price) < atr * 1.5

    if near_support and near_resistance:
        save_rejection(sym, "sniper_compression",
                       f"{sym} rejected — price squeezed between S({sr_support}) and R({sr_resistance}), "
                       f"no edge in compressed range",
                       source="scanner")
        return None
    if direction == "BUY" and near_resistance and not near_support:
        save_rejection(sym, "sniper_near_resistance",
                       f"{sym} BUY rejected — price near resistance {sr_resistance}, "
                       f"wait for breakout or pullback to support {sr_support or 'N/A'}",
                       source="scanner")
        return None
    if direction == "SELL" and near_support and not near_resistance:
        save_rejection(sym, "sniper_near_support",
                       f"{sym} SELL rejected — price near support {sr_support}, "
                       f"wait for breakdown or pullback to resistance {sr_resistance or 'N/A'}",
                       source="scanner")
        return None

    if not near_support and not near_resistance:
        if swing_hi and swing_lo and swing_hi > swing_lo:
            full_range   = swing_hi - swing_lo
            price_in_pct = (current_price - swing_lo) / full_range if full_range > 0 else 0.5
            if 0.35 < price_in_pct < 0.65:
                save_rejection(sym, "sniper_mid_range",
                               f"{sym} rejected — price in mid-range ({price_in_pct:.0%}), "
                               f"not near any key level",
                               source="scanner")
                return None

    # ── SNIPER: Confirmation candle check ────────────────────────────────────
    last_candle   = candles[-1]
    candle_open   = last_candle["open"]
    candle_close  = last_candle["close"]
    candle_high   = last_candle["high"]
    candle_low    = last_candle["low"]
    candle_body   = abs(candle_close - candle_open)
    candle_range  = candle_high - candle_low if candle_high > candle_low else 0.00001
    body_ratio    = candle_body / candle_range

    is_bullish_candle = candle_close > candle_open
    is_bearish_candle = candle_close < candle_open

    upper_wick = candle_high - max(candle_open, candle_close)
    lower_wick = min(candle_open, candle_close) - candle_low

    has_rejection_wick = False
    has_engulfing      = False

    if direction == "BUY":
        has_rejection_wick = (lower_wick > candle_body * 1.5) and is_bullish_candle
        if len(candles) >= 2:
            prev = candles[-2]
            has_engulfing = (is_bullish_candle and
                            candle_close > prev["high"] and
                            candle_open <= prev["close"])
    else:
        has_rejection_wick = (upper_wick > candle_body * 1.5) and is_bearish_candle
        if len(candles) >= 2:
            prev = candles[-2]
            has_engulfing = (is_bearish_candle and
                            candle_close < prev["low"] and
                            candle_open >= prev["close"])

    has_confirmation = has_rejection_wick or has_engulfing

    # ── Late NY session restriction (18:00–21:00 UTC, no London overlap) ────────
    utc_hour = now_utc.hour + now_utc.minute / 60.0
    if 18.0 <= utc_hour < 21.0:
        active_sessions = get_current_sessions(now_utc)
        if "new_york" in active_sessions and "london" not in active_sessions:
            save_rejection(sym, "weak_session",
                           f"{sym} late NY session ({now_utc.strftime('%H:%M')} UTC) — liquidity fading",
                           source="scanner")
            return None

    # ── Low volatility filter ───────────────────────────────────────────────────
    vol_ratio = (atr / expected_atr) if expected_atr > 0 else 1.0
    if vol_ratio < LOW_VOLATILITY_ATR_RATIO:
        save_rejection(sym, "low_volatility",
                       f"{sym} ATR too low ({vol_ratio:.2f}x expected) — market too quiet",
                       source="scanner")
        return None

    # ── Pullback detection (replaces hard-reject gates) ────────────────────────
    # Overextended / no-pullback setups become WATCH alerts, not hard rejects.
    pb_data       = ctx.get("pullback_data", {})
    is_overextend = pb_data.get("is_overextended", False)
    has_pullback  = pb_data.get("is_pullback",     False)

    # SNIPER: Always require confirmation candle — pullback alone is not enough
    if not has_confirmation:
        if not has_pullback:
            save_rejection(sym, "sniper_no_confirmation",
                           f"{sym} no pullback AND no confirmation candle — waiting for setup",
                           source="scanner")
        else:
            save_rejection(sym, "sniper_no_trigger",
                           f"{sym} pullback detected but no trigger candle (engulfing/rejection) — watch only",
                           source="scanner")
        return None

    # Still apply the raw candle-body check as a hard gate (price spiking ≠ pullback)
    if candle_body > atr * ENTRY_CANDLE_BODY_ATR_MAX_RATIO and is_overextend:
        save_rejection(sym, "overextended_entry",
                       f"{sym} entry candle extended AND price overextended — "
                       f"likely impulse top/bottom — skipping entirely",
                       source="scanner")
        return None

    # ── Trade levels (pullback-aware SL / TP) ──────────────────────────────────
    buf         = rules.get("stop_buffer_mult", 2.0)
    atr_sl_dist = atr * max(buf, ATR_SL_MULTIPLIER)

    # Entry: use pullback zone if available (avoids buying tops)
    entry_price = pb_data.get("zone") if (has_pullback and pb_data.get("zone")) \
                  else current_price

    if direction == "BUY":
        # SL: below pullback low + small buffer, floored at ATR minimum
        pb_low = pb_data.get("pullback_low") if pb_data else 0
        if pb_low and pb_low > 0:
            sl = min(pb_low - atr * 0.2, entry_price - atr_sl_dist)
        else:
            sl = entry_price - atr_sl_dist
            swing_low = ctx.get("swing_low")
            if swing_low and 0 < swing_low < entry_price:
                sl = min(sl, swing_low - atr * 0.1)
        # TP: structure-based — nearest swing high, capped at 2R minimum
        risk_est   = abs(entry_price - sl)
        min_tp_val = entry_price + risk_est * 2.0
        swing_high = ctx.get("swing_high")
        if swing_high and swing_high > entry_price and swing_high >= min_tp_val:
            tp = swing_high
        else:
            tp = min_tp_val
    else:
        pb_high = pb_data.get("pullback_high") if pb_data else 0
        if pb_high and pb_high > 0:
            sl = max(pb_high + atr * 0.2, entry_price + atr_sl_dist)
        else:
            sl = entry_price + atr_sl_dist
            swing_high = ctx.get("swing_high")
            if swing_high and swing_high > entry_price:
                sl = max(sl, swing_high + atr * 0.1)
        # TP: structure-based — nearest swing low, capped at 2R minimum
        risk_est   = abs(sl - entry_price)
        min_tp_val = entry_price - risk_est * 2.0
        swing_low  = ctx.get("swing_low")
        if swing_low and 0 < swing_low < entry_price and swing_low <= min_tp_val:
            tp = swing_low
        else:
            tp = min_tp_val

    risk   = abs(entry_price - sl)
    reward = abs(tp - entry_price)
    rr     = reward / risk if risk > 0 else 0

    # Enforce 2R minimum TP
    min_rr = max(rules.get("min_rr", 1.5), 2.0)
    if rr < min_rr:
        if direction == "BUY":
            tp = entry_price + risk * 2.0
        else:
            tp = entry_price - risk * 2.0
        rr = abs(tp - entry_price) / risk if risk > 0 else 0

    # Partial TP1 at 1R — close 50% here and move SL to breakeven
    tp1 = round(entry_price + risk, 5) if direction == "BUY" else round(entry_price - risk, 5)

    if rr < 1.5:
        save_rejection(sym, "poor_rr",
                       f"{sym} RR {rr:.2f} < minimum 1.5 even after adjustment",
                       source="scanner")
        return None

    # ── ATR spike hard gate (news spike / chaotic move) ───────────────────────
    if expected_atr > 0 and atr > expected_atr * 2.0:
        save_rejection(sym, "atr_spike",
                       f"{sym} ATR spike ({round(atr, 5)} > 2x expected {round(expected_atr, 5)}) — "
                       f"likely news event, skipping",
                       source="scanner")
        return None

    # ── Phase 2: Structure signals ─────────────────────────────────────────────
    struct_signals = None
    if len(candles) >= 10:
        struct_signals = compute_structure_signals(
            candles, direction, current_price, sym, atr, expected_atr
        )

    # ── Multi-timeframe alignment (D1 / H4 / H1) ──────────────────────────────
    # Runs AFTER early gates pass to conserve API quota.
    mtf_alignment = compute_mtf_alignment(sym, direction)
    mtf_score     = mtf_alignment.get("score", 0)
    mtf_bucket    = mtf_alignment.get("bucket", "neutral")

    # Hard block: all higher timeframes oppose this trade direction
    if mtf_bucket == "conflict":
        save_rejection(sym, "htf_conflict",
                       f"{sym} full MTF conflict — D1/H4/H1 oppose {direction}",
                       source="scanner")
        return None

    # ── SMC strict entry gate ──────────────────────────────────────────────────
    smc_result = smc_engine.validate_smc_setup(struct_signals, ctx, direction)
    if not smc_result["valid"]:
        save_rejection(sym, "no_smc_setup",
                       f"{sym} no SMC trigger — need sweep / zone pullback / breaker retest",
                       source="scanner")
        return None

    # Precompute SMC features for storage and narrative
    smc_features  = smc_engine.extract_smc_features(struct_signals, smc_result, mtf_alignment)

    # ── Market intent gate ───────────────────────────────────────────────────
    market_intent = struct_signals.get("market_intent", {}) if struct_signals else {}
    intent_label  = market_intent.get("intent", "unclear")
    if intent_label == "consolidation":
        save_rejection(sym, "consolidation_intent",
                       f"{sym} price in consolidation — no clear market intent, waiting",
                       source="scanner")
        return None

    # ── Entry Trigger Engine ─────────────────────────────────────────────────
    bos_choch   = struct_signals.get("bos_choch", {}) if struct_signals else {}
    liq_quality = struct_signals.get("liq_quality", {}) if struct_signals else {}
    sweep_data  = struct_signals.get("sweep", {}) if struct_signals else {}

    if intent_label == "reversal":
        choch_type = bos_choch.get("choch_type", "") or ""
        has_choch = bos_choch.get("choch_detected", False) and (
            (direction == "BUY"  and "bullish" in choch_type) or
            (direction == "SELL" and "bearish" in choch_type)
        )
        sweep_type_raw = sweep_data.get("type", "") or ""
        has_sweep = sweep_data.get("detected", False) and (
            (direction == "BUY"  and "bullish" in sweep_type_raw) or
            (direction == "SELL" and "bearish" in sweep_type_raw)
        )
        trigger_count = sum([has_choch, has_sweep, has_confirmation])
        if trigger_count < 2:
            missing = []
            if not has_choch:
                missing.append("CHOCH")
            if not has_sweep:
                missing.append("liquidity sweep")
            if not has_confirmation:
                missing.append("rejection candle")
            save_rejection(sym, "incomplete_reversal_trigger",
                           f"{sym} reversal trigger incomplete — need 2 of 3: {', '.join(missing)} missing",
                           source="scanner")
            return None
    elif intent_label == "continuation":
        bos_type_raw = bos_choch.get("bos_type", "") or ""
        has_bos = bos_choch.get("bos_detected", False) and (
            (direction == "BUY"  and "bullish" in bos_type_raw) or
            (direction == "SELL" and "bearish" in bos_type_raw)
        )
        has_momentum = ctx.get("momentum") == "strong"
        trigger_count = sum([has_bos, has_pullback, has_momentum])
        if trigger_count < 2:
            missing = []
            if not has_bos:
                missing.append("BOS")
            if not has_pullback:
                missing.append("pullback")
            if not has_momentum:
                missing.append("strong momentum")
            save_rejection(sym, "incomplete_continuation_trigger",
                           f"{sym} continuation trigger incomplete — need 2 of 3: {', '.join(missing)} missing",
                           source="scanner")
            return None

    # ── Session Intelligence ─────────────────────────────────────────────────
    if session_label == "Asian Session":
        is_breakout_attempt = bos_choch.get("bos_detected", False) and not has_pullback
        if is_breakout_attempt:
            save_rejection(sym, "asian_breakout_avoid",
                           f"{sym} Asian session breakout attempt — high false breakout risk, skipping",
                           source="scanner")
            return None

    # ── Confidence scoring ─────────────────────────────────────────────────────
    baseline   = 42
    confidence = baseline

    bd = {
        "baseline":        baseline,
        "regime":          0,
        "momentum":        0,
        "ema_slope":       0,
        "htf_alignment":   0,
        "mtf_score":       mtf_score,
        "session":         0,
        "rr":              0,
        "news":            0,
        # SMC / Structure signals
        "liquidity_sweep": 0,
        "fvg":             0,
        "order_block":     0,
        "breaker":         0,
        "fib_confluence":  0,
        "volatility":      0,
    }

    # Regime
    if regime == "trending":
        pts = 7
    elif regime == "pullback":
        pts = 9
    elif regime == "reversal":
        pts = 3
    elif regime == "mixed":
        pts = -7
    else:
        pts = 0
    confidence += pts
    bd["regime"] = pts

    # Momentum (reduced weight — pullback confirmation is more valuable)
    mom_pts = 0
    if ctx.get("momentum") == "strong":
        mom_pts += 2
    if ctx.get("pullback"):
        mom_pts += 5   # pullback to EMA zone = higher-quality entry
    confidence += mom_pts
    bd["momentum"] = mom_pts

    # Pullback quality (+10 clean pullback / -10 overextended / -15 no retrace)
    pb_pts = 0
    if is_overextend:
        pb_pts = -10
    elif has_pullback:
        pb_pts = 10
    else:
        pb_pts = -15   # trend valid but price hasn't retraced — watch-only
    confidence += pb_pts
    bd["pullback"] = pb_pts

    # EMA slope
    slope   = ctx.get("ema_slope", "flat")
    slp_pts = 0
    if (direction == "BUY"  and slope == "rising") or \
       (direction == "SELL" and slope == "falling"):
        slp_pts = 4
    elif slope != "flat":
        slp_pts = -3
    confidence += slp_pts
    bd["ema_slope"] = slp_pts

    # MTF alignment (D1/H4/H1) — full = +12, partial = +5, conflict = −10
    if mtf_bucket == "full":
        htf_pts = 12
    elif mtf_bucket == "partial":
        htf_pts = 5
    elif mtf_bucket == "conflict":
        htf_pts = -10
    else:  # neutral
        # Fall back to single-timeframe HTF check
        htf_pts = 6 if (htf_bias == ctx["trend_bias"] and htf_bias != "neutral") else 0
    confidence += htf_pts
    bd["htf_alignment"] = htf_pts

    # Session
    ses_pts = min(session_score, 10)
    confidence += ses_pts
    bd["session"] = ses_pts

    # RR
    rr_pts = 5 if rr >= 3.0 else (3 if rr >= 2.5 else (2 if rr >= 2.0 else 0))
    confidence += rr_pts
    bd["rr"] = rr_pts

    # Phase 2: structure signal bonuses (capped so they can't dominate)
    if struct_signals:
        struct_bonus = max(-12, min(struct_signals["total_bonus"], 15))
        confidence  += struct_bonus
        bd["liquidity_sweep"] = struct_signals["sweep_pts"]
        bd["fvg"]             = struct_signals["fvg_pts"]
        bd["order_block"]     = struct_signals["ob_pts"]
        bd["breaker"]         = struct_signals["breaker_pts"]
        bd["fib_confluence"]  = struct_signals["fib_pts"]
        bd["volatility"]      = struct_signals["vol_pts"]
        bd["eq_levels"]       = struct_signals.get("eq_pts", 0)
        bd["intent"]          = struct_signals.get("intent_pts", 0)
        bd["bos_choch"]       = struct_signals.get("bos_choch_pts", 0)
        bd["liq_quality"]     = struct_signals.get("liq_qual_pts", 0)

    # ── HTF neutral penalties (soft replacement for old hard reject) ──────────
    if htf_neutral_penalty:
        confidence += htf_neutral_penalty
        bd["htf_neutral_penalty"] = htf_neutral_penalty
    if htf2_neutral_penalty:
        confidence += htf2_neutral_penalty
        bd["htf2_neutral_penalty"] = htf2_neutral_penalty

    # ── Adaptive brain adjustment ──────────────────────────────────────────────
    try:
        import adaptive_brain
        brain_adj = adaptive_brain.get_confidence_adjustment(
            sym, regime, get_session_label(now_utc)
        )
        if brain_adj:
            confidence += brain_adj
            bd["brain_adjustment"] = brain_adj
            if brain_adj < 0:
                print(f"  [Scanner] {sym} brain penalty {brain_adj:+d}pp "
                      f"(regime={regime}, session={get_session_label(now_utc)})", flush=True)
    except Exception as _be:
        print(f"  [Scanner] Brain adjustment error: {_be}", flush=True)

    confidence = max(35, min(confidence, 90))
    bd["total"] = confidence

    # ── Session-adaptive thresholds + circuit breaker ──────────────────────────
    offset          = _session_threshold_offset(now_utc)
    cb_bump         = circuit_breaker.get_confidence_bump()  # 0 when inactive
    # Brain global confidence bump (extra strictness after sustained losses)
    try:
        import adaptive_brain as _ab
        brain_global_bump = _ab.get_global_conf_bump()
    except Exception:
        brain_global_bump = 0
    effective_entry = ALERT_MIN_CONFIDENCE + offset + cb_bump + brain_global_bump
    effective_watch = WATCH_ALERT_MIN_CONFIDENCE + offset

    if cb_bump > 0:
        print(f"  [Scanner] Circuit breaker active (+{cb_bump}pp) — "
              f"entry threshold raised to {effective_entry}%", flush=True)

    htf_tf        = htf_ctx.get("timeframe", "") if htf_ctx else ""

    # ── SMC narrative (replaces generic reason) ────────────────────────────────
    smc_narrative = smc_engine.build_smc_narrative(
        smc_result, struct_signals, mtf_alignment, direction, regime
    )

    # Build full reason line (SMC explanation + context)
    reason_parts = [smc_narrative]
    if regime == "pullback":
        reason_parts.append("Trend continuation pullback — structure intact.")
    elif regime == "trending":
        reason_parts.append("Momentum phase — trend accelerating.")
    reason_parts.append(f"{session_label} — active liquidity window.")

    # Add any additional structure signal descriptions
    if struct_signals and struct_signals.get("narrative_parts"):
        for part in struct_signals["narrative_parts"][1:2]:   # at most 1 extra
            reason_parts.append(part)

    # ── No-pullback: force watch alert with a clear waiting message ───────────
    force_watch   = False
    pullback_note = ""
    if is_overextend:
        force_watch   = True
        pullback_note = (
            f"⏳ Setup forming — price is extended {pb_data.get('ema_distance_atr', '?'):.1f}× "
            f"ATR from EMA. Waiting for a pullback into the zone before entry."
        )
    elif not has_pullback:
        force_watch   = True
        pullback_note = (
            f"⏳ Setup forming — {direction} trend confirmed but no pullback yet. "
            f"Waiting for price to retrace to the EMA zone before entry."
        )

    if confidence < effective_watch and not force_watch:
        save_rejection(sym, "low_confluence",
                       f"{sym} confidence {confidence}% below watch threshold "
                       f"{effective_watch}% (session +{offset}, cb +{cb_bump})",
                       source="scanner")
        return None

    # If we're below watch threshold but force_watch is set, still let it through
    # as a watch alert — the pullback context message is more useful than silence.
    if confidence < effective_watch:
        # Log it but don't return None — let it fall through as watch alert
        save_rejection(sym, "no_pullback" if not is_overextend else "overextended",
                       f"{sym} watch-only: pullback not yet confirmed",
                       source="scanner")

    return {
        "no_signal":        False,
        "pair":             sym,
        "timeframe":        tf,
        "direction":        direction,
        "entry":            round(entry_price, 5),
        "stop_loss":        round(sl, 5),
        "tp1":              tp1,
        "take_profit":      round(tp, 5),
        "rr":               round(rr, 2),
        "confidence":       confidence,
        "reason":           pullback_note if force_watch else " ".join(reason_parts[:3]),
        "smc_narrative":    smc_narrative,
        "market_regime":    regime,
        "setup_quality":    "scanner",
        "news_risk":        news_risk,
        "news_countdown":   news_countdown,
        "htf_bias":         htf_bias,
        "htf_timeframe":    htf_tf,
        "htf2_bias":        htf2_bias,
        "htf2_timeframe":   htf2_tf,
        "mtf_alignment":    mtf_alignment,
        "session":          session_label,
        "is_auto_alert":    True,
        "atr":              round(atr, 5),
        "score_breakdown":  bd,
        "is_watch_alert":   confidence < effective_entry or force_watch,
        # SMC analytics features
        "smc_features":     smc_features,
        # Market intent & liquidity context
        "market_intent":    market_intent.get("intent", "unclear"),
        "market_intent_desc": market_intent.get("description", ""),
        "liquidity_sweep":  struct_signals.get("sweep", {}).get("description", "") if struct_signals else "",
        "bos_choch_desc":   bos_choch.get("description", ""),
        "liq_quality_desc": liq_quality.get("description", ""),
        "liq_quality_label": liq_quality.get("quality", "none"),
        # For score explanation
        "struct_signals":   {
            k: v for k, v in (struct_signals or {}).items()
            if k in ("sweep_pts","fvg_pts","ob_pts","breaker_pts","fib_pts","vol_pts","eq_pts","intent_pts","total_bonus")
        },
    }


# ── Recipient resolution ───────────────────────────────────────────────────────

def get_alert_recipients(pair: str, confidence: int = 0,
                          watch_alert: bool = False,
                          setup_quality: str = "") -> list:
    """
    Returns the list of user IDs that should receive this alert.

    Enforced plan gates (in order):
      1. User must be active (non-expired plan)
      2. User's plan must include scanner_entry_alerts (or scanner_watch_alerts)
      3. User must not have explicitly disabled alerts (/alertsoff)
      4. User's custom confidence threshold must be met
      5. Pair must be in user's watchlist/pair prefs (unless priority_alerts VIP)
      6. VIP highest_quality_filter: only 'strong' quality setups

    Admins always receive all alerts.
    """
    from plans import is_user_active, user_has_feature

    data        = load_data()
    alert_prefs = data.get("alert_prefs", {})
    recipients  = []

    for uid_str in data.get("approved_users", {}):
        try:
            uid = int(uid_str)
        except ValueError:
            continue

        # GATE 1: active plan required
        if not is_user_active(uid):
            continue

        # GATE 2: plan must allow this alert type
        if watch_alert:
            if not user_has_feature(uid, "scanner_watch_alerts"):
                continue
        else:
            if not user_has_feature(uid, "scanner_entry_alerts"):
                continue

        # GATE 3: user must not have turned alerts off
        prefs     = alert_prefs.get(uid_str, {})
        alerts_on = prefs.get("alerts_on", True)
        if not alerts_on:
            continue

        # GATE 4: custom confidence threshold
        if not watch_alert:
            user_threshold = prefs.get("threshold", ALERT_MIN_CONFIDENCE)
            if confidence > 0 and confidence < user_threshold:
                continue

        # GATE 5: pair filter (VIP priority_alerts bypass this)
        has_priority = user_has_feature(uid, "priority_alerts")
        if not has_priority:
            user_pairs = prefs.get("pairs", [])
            watchlist  = data.get("watchlists", {}).get(uid_str, [])
            combined   = [p.upper() for p in (user_pairs + watchlist)]
            if combined and pair.upper() not in combined:
                continue

        # GATE 6: VIP quality filter — only strong setups
        if user_has_feature(uid, "highest_quality_filter") and not watch_alert:
            if setup_quality and setup_quality not in ("strong", "good"):
                continue

        recipients.append(uid)

    # Admins always get alerts (for monitoring)
    for admin_id in ADMIN_IDS:
        if admin_id not in recipients:
            recipients.append(admin_id)

    return recipients


# ── Alert message formatters ───────────────────────────────────────────────────

def format_alert_message(setup: dict, uid: int = 0) -> str:
    """
    Formats a scanner entry alert for a specific user.
    Premium features (confidence breakdown, full reason) are gated by plan.
    Pass uid=0 for a basic format with no plan gating (e.g. admins).
    """
    from plans import user_has_feature
    from config import ADMIN_IDS

    direction_emoji = "🟢" if setup["direction"] == "BUY" else "🔴"
    conf = setup.get("confidence", 0)
    if conf >= 85:
        tier = "🔥 STRONG SETUP"
    elif conf >= 75:
        tier = "✅ GOOD SETUP"
    else:
        tier = "⚠️ MODERATE SETUP"

    # ── MTF alignment line ─────────────────────────────────────────────────────
    mtf        = setup.get("mtf_alignment", {})
    mtf_bucket = mtf.get("bucket", "")
    mtf_tfs    = mtf.get("aligned_timeframes", [])

    htf_line = ""
    if mtf_bucket in ("full", "partial") and mtf_tfs:
        aligned_str  = "/".join(mtf_tfs)
        bucket_label = "✅ Full" if mtf_bucket == "full" else "⚡ Partial"
        htf_line = f"\n🕐 MTF: {bucket_label} alignment ({aligned_str} → {setup['direction']})"
    elif setup.get("htf_bias") and setup["htf_bias"] not in ("neutral", "N/A", ""):
        htf_line = f"\n🕐 HTF ({setup.get('htf_timeframe','')}) Bias: {setup['htf_bias'].upper()}"

    # ── SMC trigger explanation ────────────────────────────────────────────────
    smc_line = ""
    smc_narr = setup.get("smc_narrative", "")
    if smc_narr:
        smc_line = f"\n🧠 SMC: {smc_narr}"

    # ── Structure signals summary ──────────────────────────────────────────────
    bd          = setup.get("score_breakdown", {})
    struct_items = []
    if bd.get("liquidity_sweep", 0) > 0:
        struct_items.append("Sweep")
    if bd.get("fvg", 0) > 0:
        struct_items.append("FVG")
    if bd.get("order_block", 0) > 0:
        struct_items.append("OB")
    if bd.get("fib_confluence", 0) > 0:
        struct_items.append("Fib")
    if bd.get("breaker", 0) > 0:
        struct_items.append("Breaker")
    if bd.get("bos_choch", 0) > 0:
        bos_desc = setup.get("bos_choch_desc", "")
        if "CHOCH" in bos_desc:
            struct_items.append("CHOCH")
        elif "BOS" in bos_desc:
            struct_items.append("BOS")
    struct_line = f"\n📐 Structure: {' + '.join(struct_items)}" if struct_items else ""

    # PLAN GATE: confidence breakdown (score driver line)
    is_admin_user = uid in ADMIN_IDS if uid else False
    drivers_line  = ""
    if is_admin_user or (uid and user_has_feature(uid, "confidence_breakdown")):
        driver_parts = []
        DRIVER_LABELS = {
            "bias_alignment": "Bias", "htf_alignment": "MTF",
            "regime": "Regime", "session": "Session",
            "rr": "R:R", "news": "News",
        }
        for key, label in DRIVER_LABELS.items():
            v = bd.get(key, 0)
            if v and abs(v) >= 3:
                sign = "+" if v > 0 else ""
                driver_parts.append(f"{label}({sign}{v})")
        if driver_parts:
            drivers_line = f"\n📊 Drivers: {' '.join(driver_parts)}"

    # PLAN GATE: premium narrative (full reason vs first sentence)
    reason = setup.get("reason", "")
    if not is_admin_user and uid and not user_has_feature(uid, "premium_narrative"):
        first = reason.split(".")[0].strip()
        reason = first + "." if first else reason

    intent      = setup.get("market_intent", "")
    intent_desc = setup.get("market_intent_desc", "")
    intent_line = ""
    if intent and intent not in ("", "unclear"):
        intent_line = f"\n🧠 Intent: {intent.upper()}"
        if intent_desc:
            intent_line += f" — {intent_desc}"

    sweep_line = ""
    sweep_desc = setup.get("liquidity_sweep", "")
    if sweep_desc:
        sweep_line = f"\n🔍 {sweep_desc}"

    bos_choch_line = ""
    bos_choch_desc = setup.get("bos_choch_desc", "")
    if bos_choch_desc:
        bos_choch_line = f"\n📊 {bos_choch_desc}"

    liq_qual_line = ""
    liq_q = setup.get("liq_quality_label", "none")
    liq_q_desc = setup.get("liq_quality_desc", "")
    if liq_q != "none" and liq_q_desc:
        liq_qual_line = f"\n💧 Liquidity: {liq_q_desc}"

    notes_parts = []
    if sweep_desc:
        notes_parts.append(sweep_desc)
    notes_line = ""
    if notes_parts:
        notes_line = "\n⚠️ " + "; ".join(notes_parts)

    return (
        f"🚨 CHEFBUNTSA SIGNAL (SNIPER MODE) — {tier}\n\n"
        f"{direction_emoji} {setup['pair']} — {setup['direction']}\n"
        f"⏱ Timeframe: {setup['timeframe']}{htf_line}\n"
        f"🎯 Entry: {setup['entry']}\n"
        f"🛑 Stop Loss: {setup['stop_loss']}\n"
        f"✅ TP1 (50% close): {setup.get('tp1', setup['take_profit'])}\n"
        f"🏆 TP2 (full target): {setup['take_profit']}\n"
        f"📌 Move SL to breakeven when TP1 is hit\n"
        f"📐 R:R — 1:{setup['rr']}{struct_line}\n"
        f"📈 Confidence: {conf}%"
        f"{drivers_line}\n"
        f"🌊 Regime: {setup.get('market_regime','')}\n"
        f"⏰ Session: {setup.get('session','')}"
        f"{intent_line}"
        f"{sweep_line}"
        f"{bos_choch_line}"
        f"{liq_qual_line}\n"
        f"{smc_line}\n\n"
        + (f"📅 {setup['news_countdown']}\n\n" if setup.get('news_countdown') else "")
        + f"💡 {reason}"
        + (f"{notes_line}\n\n" if notes_line else "\n\n")
        + "⚠️ Educational tool only — manage your own risk."
    )


def format_watch_message(setup: dict) -> str:
    direction_emoji = "🟢" if setup["direction"] == "BUY" else "🔴"
    conf   = setup.get("confidence", 0)
    reason = setup.get("reason", "")

    # Detect pullback-wait vs generic watch
    is_pullback_watch = "pullback" in reason.lower() or "extended" in reason.lower()
    header = "⏳ PULLBACK WATCH — Setup Forming" if is_pullback_watch \
             else "👁 SETUP FORMING — Watch Alert"

    return (
        f"{header}\n\n"
        f"{direction_emoji} {setup['pair']} — {setup['direction']}\n"
        f"📈 Confidence: {conf}% (watch)\n"
        f"🌊 Regime: {setup.get('market_regime','')}\n"
        f"⏰ Session: {setup.get('session','')}\n\n"
        f"💡 {reason}\n\n"
        "⚠️ Not an entry signal — wait for price to pull back into the zone."
    )


# ── Main scan loop ─────────────────────────────────────────────────────────────

def scan_market_for_alerts(bot_instance) -> int:
    if not has_market_data():
        return 0

    now_utc = datetime.now(timezone.utc)
    if is_dead_hours(now_utc):
        return 0

    # ── Global drawdown protection ─────────────────────────────────────────────
    globally_paused, pause_reason = loss_streak.is_globally_paused()
    if globally_paused:
        print(f"[Scanner] PAUSED — {pause_reason}", flush=True)
        return 0

    cleanup_expired_watches()

    data          = load_data()
    alert_prefs   = data.get("alert_prefs", {})
    pairs_to_scan = set(DEFAULT_SCAN_PAIRS)

    for uid_str, prefs in alert_prefs.items():
        if prefs.get("alerts_on", True):
            for p in prefs.get("pairs", []):
                pairs_to_scan.add(p.upper())

    for uid_str, wl in data.get("watchlists", {}).items():
        for p in wl:
            pairs_to_scan.add(p.upper())

    for uid_str, pw_pairs in data.get("pending_watches", {}).items():
        for p in pw_pairs:
            pairs_to_scan.add(p.upper())

    SAST     = timezone(timedelta(hours=2))
    sast_now = now_utc.astimezone(SAST)
    cb_status = circuit_breaker.get_status()
    cb_note   = f" [CB active +{cb_status['confidence_bump']}pp]" if cb_status["active"] else ""
    ls_status = loss_streak.get_status()
    ls_note   = (f" [LS: {ls_status['recent_losses_in_window']} losses, "
                 f"{len(ls_status['blocked_pairs'])} pairs blocked]"
                 if ls_status["recent_losses_in_window"] > 0 or ls_status["blocked_pairs"]
                 else "")
    print(f"[Scanner] Scanning {len(pairs_to_scan)} pairs at "
          f"{sast_now.strftime('%H:%M')} SAST{cb_note}{ls_note}...", flush=True)

    alerts_sent = 0
    scan_tfs = SCAN_TIMEFRAMES if SCAN_TIMEFRAMES else ["M15"]
    for tf_scan in scan_tfs:
      for pair in sorted(pairs_to_scan):
        try:
            setup = scan_pair_for_setup(pair, timeframe=tf_scan)
            if not setup:
                continue

            is_watch   = setup.get("is_watch_alert", False)
            setup_hash = _setup_hash(pair, setup["direction"], setup["entry"])

            if is_watch:
                if not _should_send_watch(pair, setup_hash):
                    print(f"  [Scanner] {pair} — watch suppressed (cooldown/dedup).", flush=True)
                    continue
                recipients = get_alert_recipients(pair, confidence=setup["confidence"],
                                                  watch_alert=True,
                                                  setup_quality=setup.get("setup_quality",""))
                if not recipients:
                    continue
                msg = format_watch_message(setup)
                for uid in recipients:
                    try:
                        bot_instance.send_message(uid, msg)
                        alerts_sent += 1
                    except Exception as e:
                        print(f"  [Scanner] Watch send error to {uid}: {e}", flush=True)
                try:
                    save_pre_alert(
                        pair=pair, direction=setup["direction"],
                        confidence=setup["confidence"],
                        regime=setup.get("market_regime", ""),
                        session=setup.get("session", ""),
                        reason=setup.get("reason", ""),
                    )
                except Exception as e:
                    print(f"  [Scanner] Pre-alert storage error: {e}", flush=True)
                _mark_watch_alerted(pair, setup_hash)
                print(f"  [Scanner] Watch alert: {pair} {setup['direction']} "
                      f"conf={setup['confidence']}%", flush=True)

            else:
                if not _should_send_alert(pair, setup_hash):
                    save_rejection(pair, "replay_prevented",
                                   f"{pair} entry suppressed — cooldown/dedup",
                                   source="scanner")
                    print(f"  [Scanner] {pair} — entry suppressed (cooldown/dedup).", flush=True)
                    continue

                corr_dup, blocker = _is_correlated_duplicate(pair, setup["direction"])
                if corr_dup:
                    save_rejection(pair, "correlated_duplicate",
                                   f"{pair} {setup['direction']} suppressed — correlated with {blocker}",
                                   source="scanner")
                    print(f"  [Scanner] {pair} {setup['direction']} suppressed "
                          f"— correlated with {blocker}.", flush=True)
                    continue

                recipients = get_alert_recipients(pair, confidence=setup["confidence"],
                                                  watch_alert=False,
                                                  setup_quality=setup.get("setup_quality",""))
                if not recipients:
                    continue

                for uid in recipients:
                    try:
                        msg = format_alert_message(setup, uid=uid)
                        bot_instance.send_message(uid, msg)
                        alerts_sent += 1
                    except Exception as e:
                        print(f"  [Scanner] Alert send error to {uid}: {e}", flush=True)

                try:
                    alert_id = save_scanner_alert(
                        setup,
                        score_breakdown=setup.get("score_breakdown"),
                        recipients=recipients,
                    )
                    print(f"  [Scanner] Alert stored: {alert_id} | "
                          f"{pair} {setup['direction']} conf={setup['confidence']}% "
                          f"rr={setup['rr']}", flush=True)
                except Exception as e:
                    print(f"  [Scanner] Alert storage error: {e}", flush=True)

                # ── Copy trading hook ────────────────────────────────────────
                try:
                    from copy_engine import route_signal
                    route_signal(setup, recipients)
                except Exception as _ce:
                    print(f"  [Scanner] Copy engine error: {_ce}", flush=True)

                # ── Notify pending watch users ──────────────────────────────
                try:
                    pw_users = get_pending_watch_users(pair)
                    for pw_uid in pw_users:
                        if pw_uid not in recipients:
                            msg = format_alert_message(setup, uid=pw_uid)
                            notify_text = (
                                f"👁 AUTO-WATCH ALERT — {pair}\n\n"
                                "A valid setup has appeared on a pair you were watching!\n\n"
                                + msg
                            )
                            try:
                                bot_instance.send_message(pw_uid, notify_text)
                                alerts_sent += 1
                                print(f"  [Scanner] Pending watch notify: {pair} → {pw_uid}", flush=True)
                            except Exception as e:
                                print(f"  [Scanner] Pending watch send error to {pw_uid}: {e}", flush=True)
                        clear_pending_watch(pw_uid, pair)
                except Exception as e:
                    print(f"  [Scanner] Pending watch error: {e}", flush=True)

                _mark_alerted(pair, setup_hash, direction=setup["direction"])

            time.sleep(1.2)

        except Exception as e:
            print(f"  [Scanner] Error scanning {pair}: {e}", flush=True)

    if alerts_sent:
        print(f"[Scanner] Scan complete — {alerts_sent} alert(s) sent.", flush=True)
    else:
        print(f"[Scanner] Scan complete — no qualifying setups.", flush=True)

    return alerts_sent
