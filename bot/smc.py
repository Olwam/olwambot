"""
smc.py — Smart Money Concepts validation layer.

This module sits ON TOP of structure_signals.py (which does the raw detection).
Its job is to:

  1. validate_smc_setup()  — enforce the STRICT ENTRY RULE:
       At least ONE of these must be true before a scanner alert is sent:
         A. Aligned liquidity sweep (price swept liquidity then reversed)
         B. Pullback into an active zone (Order Block, FVG, or EMA pullback)
         C. Break + retest structure (breaker block confirmed)

  2. build_smc_narrative() — produce a human-readable explanation of the
       SMC setup for use in alert messages:
       e.g. "Liquidity sweep + bullish OB retest with H1/H4 trend alignment"

  3. extract_smc_features() — return a flat dict of which features fired
       for analytics storage in each alert record.

Design rules:
  - Does NOT call any API — all inputs come from already-computed dicts
  - Does NOT modify the confidence score — that is handled in scanner.py
  - Returns structured dicts, not raw booleans, so analytics can use them
"""


# ── SMC Setup Validator ────────────────────────────────────────────────────────

def validate_smc_setup(
    struct_signals: dict,
    market_ctx: dict,
    direction: str,
) -> dict:
    """
    Enforces the strict SMC entry rule.

    Inputs:
      struct_signals — output of structure_signals.compute_structure_signals()
      market_ctx     — output of indicators.compute_market_context()
      direction      — "BUY" or "SELL"

    Returns:
      {
        "valid":    bool,
        "trigger":  str,   # which rule triggered (A / B / C / "none")
        "reasons":  list,  # human-readable list of what fired
      }
    """
    if not struct_signals or not market_ctx:
        return {"valid": False, "trigger": "none", "reasons": []}

    sweep   = struct_signals.get("sweep",       {})
    fvg     = struct_signals.get("fvg",         {})
    ob      = struct_signals.get("order_block", {})
    breaker = struct_signals.get("breaker",     {})
    reasons = []

    # ── Rule A: Aligned liquidity sweep ───────────────────────────────────────
    if sweep.get("detected"):
        sweep_type = sweep.get("type", "")
        aligned = (
            (direction == "BUY"  and sweep_type == "bullish_sweep") or
            (direction == "SELL" and sweep_type == "bearish_sweep")
        )
        if aligned:
            reasons.append(f"Liquidity sweep at {sweep.get('swept_level')}")
            return {"valid": True, "trigger": "A", "reasons": reasons}

    # ── Rule B: Pullback into active zone ─────────────────────────────────────
    zone_hits = []

    # B1: Order block
    if ob.get("detected") and ob.get("price_in_zone"):
        zone_hits.append("order block zone")

    # B2: Fair Value Gap (price inside gap)
    if fvg.get("detected") and fvg.get("price_inside"):
        fvg_type = fvg.get("type", "")
        aligned  = (
            (direction == "BUY"  and fvg_type == "bullish_fvg") or
            (direction == "SELL" and fvg_type == "bearish_fvg")
        )
        if aligned:
            zone_hits.append("FVG imbalance zone")

    # B3: EMA cluster pullback (market_ctx flag)
    if market_ctx.get("pullback"):
        zone_hits.append("EMA cluster")

    if zone_hits:
        reasons.append(f"Pullback into {' + '.join(zone_hits)}")
        return {"valid": True, "trigger": "B", "reasons": reasons}

    # ── Rule C: Break + retest (breaker block confirmed) ──────────────────────
    if breaker.get("detected"):
        reasons.append(breaker.get("description", "Structure break + retest"))
        return {"valid": True, "trigger": "C", "reasons": reasons}

    return {"valid": False, "trigger": "none", "reasons": []}


# ── SMC Narrative Builder ──────────────────────────────────────────────────────

def build_smc_narrative(
    smc_result:    dict,
    struct_signals: dict,
    mtf_alignment: dict,
    direction:     str,
    regime:        str,
) -> str:
    """
    Builds a concise, human-readable SMC explanation for the alert message.

    Example outputs:
      "Liquidity sweep + bullish OB retest — H4/D1 trend aligned"
      "Pullback into FVG zone + EMA cluster — H1/H4 aligned (partial)"
      "Break + retest structure — entry trigger confirmed on M15"
    """
    parts = []

    # ── Part 1: SMC setup type ─────────────────────────────────────────────────
    if smc_result.get("valid"):
        reasons = smc_result.get("reasons", [])
        if reasons:
            parts.append(reasons[0])

    # ── Part 2: Additional SMC signals (if any beyond the trigger) ────────────
    if struct_signals:
        fib = struct_signals.get("fib", {})
        if fib.get("detected"):
            level = fib.get("fib_level")
            parts.append(f"Fib {level} confluence" if level else "Fib confluence")

    # ── Part 3: MTF alignment ─────────────────────────────────────────────────
    aligned_tfs = mtf_alignment.get("aligned_timeframes", []) if mtf_alignment else []
    score       = mtf_alignment.get("score", 0) if mtf_alignment else 0

    if aligned_tfs and score >= 10:
        parts.append(f"{'/'.join(aligned_tfs)} trend aligned")
    elif aligned_tfs and score >= 4:
        parts.append(f"{'/'.join(aligned_tfs)} partial alignment")

    # ── Part 4: Regime context ────────────────────────────────────────────────
    if regime == "pullback":
        parts.append("trend continuation zone")
    elif regime == "trending":
        parts.append("strong momentum phase")

    return " — ".join(parts) if parts else f"{direction} SMC setup confirmed"


# ── SMC Feature Extractor (for analytics storage) ─────────────────────────────

def extract_smc_features(struct_signals: dict, smc_result: dict,
                          mtf_alignment: dict) -> dict:
    """
    Returns a flat dict of SMC features for storage in each alert record.
    Used by analytics to track which features correlate with winning trades.
    """
    if not struct_signals:
        return {
            "had_liquidity_sweep": False,
            "had_fvg":             False,
            "had_order_block":     False,
            "fib_zone":            False,
            "had_breaker":         False,
            "smc_trigger":         "none",
            "mtf_score":           0,
            "mtf_aligned_tfs":     [],
        }

    sweep = struct_signals.get("sweep", {})
    fvg   = struct_signals.get("fvg",   {})
    ob    = struct_signals.get("order_block", {})
    fib   = struct_signals.get("fib",   {})
    brkr  = struct_signals.get("breaker", {})

    return {
        "had_liquidity_sweep": bool(sweep.get("detected") and struct_signals.get("sweep_pts", 0) > 0),
        "had_fvg":             bool(fvg.get("detected") and fvg.get("price_inside")),
        "had_order_block":     bool(ob.get("detected")  and ob.get("price_in_zone")),
        "fib_zone":            bool(fib.get("detected")),
        "had_breaker":         bool(brkr.get("detected")),
        "smc_trigger":         smc_result.get("trigger", "none"),
        "mtf_score":           mtf_alignment.get("score", 0) if mtf_alignment else 0,
        "mtf_aligned_tfs":     mtf_alignment.get("aligned_timeframes", []) if mtf_alignment else [],
    }
