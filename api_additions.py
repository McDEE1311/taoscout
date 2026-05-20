# ── NEW ENDPOINTS — paste into api.py after the existing /alerts endpoint ──────
# Add this import at the top of api.py with the other imports:
#
#   from flow_tracker import compute_all_health, compute_flow_overview
#   from dereg_risk import score_all_dereg
#   from rotation_engine import detect_rotation, build_alpha_signals
#
# Also add to RATE_LIMITS dict:
#   "flow":         (10, 60),
#   "dereg":        (10, 60),
#   "rotation":     (10, 60),
#   "alpha":        (10, 60),
#
# ── New rate limits (add to RATE_LIMITS dict in api.py) ───────────────────────
#
# "flow":     (10, 60),
# "dereg":    (10, 60),
# "rotation": (10, 60),
# "alpha":    (10, 60),
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/flow")
async def flow_overview(key: str = Depends(verify_key)):
    """Ecosystem-level capital flow overview."""
    check_rate_limit(key, "flow")
    try:
        from flow_tracker import compute_all_health, compute_flow_overview
        from scout import load_data, load_enrichment
        import db as DB

        data, _    = load_data("chain")
        enrich     = load_enrichment()
        if not data:
            raise HTTPException(status_code=503, detail="No chain data available")

        subnets    = data.get("subnets", [])
        tao_price  = float(enrich.get("tao_price_usd", 0) or 0)
        movers     = DB.get_movers(hours=24, top_n=50)
        health     = compute_all_health(subnets, movers,
                         get_history_fn=lambda nid: DB.get_subnet_history(nid, hours=24))
        overview   = compute_flow_overview(health, movers, tao_price)

        return {
            **overview,
            "top_health":  health[:10],
            "snapshot_time": data.get("fetched_at", ""),
            "block":         data.get("block"),
            "tao_price_usd": tao_price,
            "computation":   "Deterministic Python scoring. No LLM.",
            "disclaimer":    "TaoScout is informational only. Not financial advice.",
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Flow engine error: {e}")


@app.get("/flow/{netuid}")
async def flow_subnet(netuid: int, key: str = Depends(verify_key)):
    """Detailed flow and health data for one subnet."""
    check_rate_limit(key, "flow")
    if netuid < 0 or netuid > 300:
        raise HTTPException(status_code=400, detail="Invalid netuid")
    try:
        from flow_tracker import compute_health_score
        from dereg_risk import score_dereg_risk
        from scout import load_data
        import db as DB

        data, _ = load_data("chain")
        if not data:
            raise HTTPException(status_code=503, detail="No chain data")

        subnet = next((s for s in data.get("subnets", []) if s.get("netuid") == netuid), None)
        if not subnet:
            raise HTTPException(status_code=404, detail=f"Subnet {netuid} not found")

        movers  = DB.get_movers(hours=24, top_n=150)
        history = DB.get_subnet_history(netuid, hours=48)
        health  = compute_health_score(subnet, history, movers)
        dereg   = score_dereg_risk(subnet, history, movers)

        return {
            **health,
            "dereg_risk":  dereg["dereg_risk"],
            "dereg_label": dereg["dereg_label"],
            "risk_flags":  dereg["risk_flags"],
            "history_points": len(history),
            "disclaimer":  "TaoScout is informational only. Not financial advice.",
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Flow subnet error: {e}")


@app.get("/dereg-risk")
async def dereg_risk_endpoint(top_n: int = 20, key: str = Depends(verify_key)):
    """Ranked deregistration and haircut risk table."""
    check_rate_limit(key, "dereg")
    if top_n > 50:
        top_n = 50
    try:
        from dereg_risk import score_all_dereg
        from scout import load_data, load_enrichment
        import db as DB
        import json

        CFG_FILE = Path(__file__).parent / "config.json"
        with open(CFG_FILE) as f:
            cfg = json.load(f)
        my_netuids = cfg.get("my_subnets", [])

        data, _   = load_data("chain")
        enrich    = load_enrichment()
        if not data:
            raise HTTPException(status_code=503, detail="No chain data")

        subnets   = data.get("subnets", [])
        tao_price = float(enrich.get("tao_price_usd", 0) or 0)
        movers    = DB.get_movers(hours=24, top_n=150)
        results   = score_all_dereg(
            subnets, movers,
            get_history_fn=lambda nid: DB.get_subnet_history(nid, hours=24),
            my_netuids=my_netuids,
            top_n=top_n,
        )

        critical = [r for r in results if r["dereg_label"] == "CRITICAL"]
        high     = [r for r in results if r["dereg_label"] == "HIGH"]

        return {
            "results":         results,
            "result_count":    len(results),
            "critical_count":  len(critical),
            "high_count":      len(high),
            "tao_price_usd":   tao_price,
            "computation":     "Deterministic weighted scoring. No LLM.",
            "weights":         "emissions_collapse(25)+validator_drop(25)+consensus_weak(20)+miner_churn(15)+stale_dev(10)+sentiment(5)",
            "snapshot_time":   data.get("fetched_at", ""),
            "disclaimer":      "TaoScout is informational only. Not financial advice.",
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Dereg risk error: {e}")


@app.get("/rotation")
async def rotation_endpoint(key: str = Depends(verify_key)):
    """Likely capital rotation map — from weak to strong subnets."""
    check_rate_limit(key, "rotation")
    try:
        from flow_tracker import compute_all_health
        from dereg_risk import score_all_dereg
        from rotation_engine import detect_rotation
        from scout import load_data, load_enrichment
        import db as DB

        data, _   = load_data("chain")
        enrich    = load_enrichment()
        if not data:
            raise HTTPException(status_code=503, detail="No chain data")

        subnets   = data.get("subnets", [])
        tao_price = float(enrich.get("tao_price_usd", 0) or 0)
        movers    = DB.get_movers(hours=24, top_n=150)
        health    = compute_all_health(subnets, movers)
        dereg     = score_all_dereg(subnets, movers, top_n=50)
        rotation  = detect_rotation(health, dereg, movers)

        return {
            **rotation,
            "tao_price_usd": tao_price,
            "computation":   "Deterministic flow analysis. No LLM.",
            "disclaimer":    "TaoScout is informational only. Not financial advice.",
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Rotation engine error: {e}")


@app.get("/alpha-signals")
async def alpha_signals_endpoint(min_relevance: int = 40, key: str = Depends(verify_key)):
    """
    Trading-bot-ready alpha signals.
    Returns subnets with trade_relevance >= min_relevance, sorted by score.
    This endpoint is the primary feed for mcdee-tao-alpha-agent.
    """
    check_rate_limit(key, "alpha")
    if min_relevance < 0 or min_relevance > 100:
        raise HTTPException(status_code=400, detail="min_relevance must be 0-100")
    try:
        from flow_tracker import compute_all_health
        from dereg_risk import score_all_dereg
        from rotation_engine import detect_rotation, build_alpha_signals
        from scout import load_data, load_enrichment
        import db as DB

        data, _   = load_data("chain")
        enrich    = load_enrichment()
        if not data:
            raise HTTPException(status_code=503, detail="No chain data")

        subnets   = data.get("subnets", [])
        tao_price = float(enrich.get("tao_price_usd", 0) or 0)
        movers    = DB.get_movers(hours=24, top_n=150)
        health    = compute_all_health(subnets, movers,
                        get_history_fn=lambda nid: DB.get_subnet_history(nid, hours=24))
        dereg     = score_all_dereg(subnets, movers, top_n=100)
        rotation  = detect_rotation(health, dereg, movers)
        signals   = build_alpha_signals(health, dereg, movers, rotation,
                                        min_relevance=min_relevance)

        bullish  = [s for s in signals if s["directional_bias"] == "bullish"]
        bearish  = [s for s in signals if s["directional_bias"] == "bearish"]
        neutral  = [s for s in signals if s["directional_bias"] == "neutral"]

        return {
            "signals":           signals,
            "signal_count":      len(signals),
            "bullish_count":     len(bullish),
            "bearish_count":     len(bearish),
            "neutral_count":     len(neutral),
            "rotation_active":   rotation["rotation_detected"],
            "rotation_confidence": rotation["confidence"],
            "tao_price_usd":     tao_price,
            "min_relevance":     min_relevance,
            "block":             data.get("block"),
            "snapshot_time":     data.get("fetched_at", ""),
            "computation":       "Deterministic scoring. No LLM. See health+dereg+rotation modules.",
            "disclaimer":        "TaoScout is informational only. Not financial advice.",
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Alpha signals error: {e}")
