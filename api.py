#!/usr/bin/env python3
"""
TaoScout API v1.3.0 — Launch Hardened
Rate limiting | Concurrency control | Security hardening
"""
import json, time, asyncio
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional
from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

SCRIPT_DIR = Path(__file__).parent
CFG_FILE   = SCRIPT_DIR / "config.json"

def load_config():
    with open(CFG_FILE) as f:
        return json.load(f)

CFG      = load_config()
API_KEYS = CFG.get("api_keys", ["taoscout-local-key-1"])
PORT     = CFG.get("api_port", 8765)
HOST     = CFG.get("api_host", "127.0.0.1")

import sys
sys.path.insert(0, str(SCRIPT_DIR))
from daily_dashboard import build_dashboard
from alerts import build_alerts, format_alerts_table
from query_log import log_query, get_stats
from flow_tracker import compute_all_health, compute_flow_overview
from dereg_risk import score_all_dereg
from rotation_engine import detect_rotation, build_alpha_signals
from pathlib import Path as _Path
from scout import (
    api_ask, api_brief, api_scan, api_status,
    api_rankings, api_movers, api_subnet,
    get_chain_data, load_enrichment,
    META, DATA_DIR
)

# ── Rate limiting — per endpoint per key ──────────────────────────────────────
rate_store: dict = {}
RATE_LIMITS = {
    "ask":        (10, 60),
    "brief":      (6,  60),
    "scan":       (6,  60),
    "rankings":   (20, 60),
    "movers":     (20, 60),
    "subnet":     (20, 60),
    "flow": (10,60), "dereg": (10,60), "rotation": (10,60), "alpha": (10,60), "default":    (20, 60),
}

def check_rate_limit(key: str, endpoint: str):
    limit, window = RATE_LIMITS.get(endpoint, RATE_LIMITS["default"])
    store_key = f"{key}:{endpoint}"
    now = time.time()
    if store_key not in rate_store:
        rate_store[store_key] = []
    rate_store[store_key] = [t for t in rate_store[store_key] if now - t < window]
    if len(rate_store[store_key]) >= limit:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit: {limit} requests per {window}s for this endpoint"
        )
    rate_store[store_key].append(now)

# ── Concurrency control — max 2 LLM jobs per key ─────────────────────────────
active_jobs: dict = {}
MAX_CONCURRENT = 2

def acquire_job_slot(key: str):
    active_jobs[key] = active_jobs.get(key, 0) + 1
    if active_jobs[key] > MAX_CONCURRENT:
        active_jobs[key] -= 1
        raise HTTPException(
            status_code=429,
            detail=f"Too many concurrent requests. Max {MAX_CONCURRENT} per key. Try again shortly."
        )

def release_job_slot(key: str):
    if key in active_jobs and active_jobs[key] > 0:
        active_jobs[key] -= 1

# ── Auth ───────────────────────────────────────────────────────────────────────
def verify_key(request: Request):
    key = request.headers.get("X-API-Key", "")
    if key not in API_KEYS:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
    return key

# ── App ────────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="TaoScout API",
    description="Bittensor Operator Intelligence — v1.3.0",
    version="1.3.0",
    docs_url="/docs",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

class AskRequest(BaseModel):
    question: str
    refresh: Optional[bool] = False

# ── Endpoints ──────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    s = api_status()
    return {
        "status":               "ok",
        "version":              "1.3.0",
        "ollama":               s.get("ollama"),
        "snapshot_age_minutes": s.get("snapshot_age_minutes"),
        "taostats_connected":   s.get("taostats_connected"),
        "analytics_engine":     s.get("analytics_engine"),
        "tao_price_usd":        s.get("tao_price_usd"),
        "timestamp":            datetime.now(timezone.utc).isoformat(),
    }

@app.get("/status")
async def status(key: str = Depends(verify_key)):
    check_rate_limit(key, "default")
    return api_status()

@app.post("/ask")
async def ask(body: AskRequest, key: str = Depends(verify_key)):
    check_rate_limit(key, "ask")
    if not body.question or not body.question.strip():
        raise HTTPException(status_code=400, detail="Question cannot be empty")
    if len(body.question) > 300:
        raise HTTPException(status_code=400, detail="Question too long (max 300 chars)")
    acquire_job_slot(key)
    t0 = __import__("time").time()
    try:
        result = api_ask(body.question.strip(), force=body.refresh or False)
        latency = int((__import__("time").time() - t0) * 1000)
        if "error" in result:
            log_query("ask", body.question, error=result["error"],
                      success=False, latency_ms=latency)
            raise HTTPException(status_code=503, detail=result["error"])
        log_query("ask", body.question,
                  intent=result.get("router_intent",""),
                  model=result.get("model",""),
                  latency_ms=latency,
                  fallback=bool(result.get("router_error")))
        return result
    finally:
        release_job_slot(key)

@app.get("/brief")
async def brief(key: str = Depends(verify_key)):
    check_rate_limit(key, "brief")
    acquire_job_slot(key)
    try:
        result = api_brief()
        if "error" in result:
            raise HTTPException(status_code=503, detail=result["error"])
        return result
    finally:
        release_job_slot(key)

@app.get("/scan")
async def scan(key: str = Depends(verify_key)):
    check_rate_limit(key, "scan")
    acquire_job_slot(key)
    try:
        result = api_scan()
        if "error" in result:
            raise HTTPException(status_code=503, detail=result["error"])
        return result
    finally:
        release_job_slot(key)

@app.get("/rankings")
async def rankings(gpu: str = "24gb", key: str = Depends(verify_key)):
    check_rate_limit(key, "rankings")
    result = api_rankings(gpu_class=gpu.lower())
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@app.get("/dashboard")
async def dashboard(key: str = Depends(verify_key)):
    check_rate_limit(key, "dashboard")
    acquire_job_slot(key)
    try:
        result = build_dashboard()
        if "error" in result:
            raise HTTPException(status_code=503, detail=result["error"])
        return result
    finally:
        release_job_slot(key)


@app.get("/alerts")
async def alerts(hours: int = 24, key: str = Depends(verify_key)):
    check_rate_limit(key, "alerts")
    if hours not in [1, 6, 12, 24, 48, 72]:
        raise HTTPException(status_code=400, detail="hours must be 1,6,12,24,48,72")
    result = build_alerts(hours=hours)
    if "error" in result:
        raise HTTPException(status_code=503, detail=result["error"])
    return result

@app.get("/query-stats")
async def query_stats(hours: int = 24, key: str = Depends(verify_key)):
    return get_stats(hours=hours)

@app.get("/movers")
async def movers(hours: int = 24, key: str = Depends(verify_key)):
    check_rate_limit(key, "movers")
    if hours not in [1, 6, 12, 20, 24, 48, 72]:
        raise HTTPException(status_code=400, detail="hours must be 1,6,12,20,24,48,72")
    result = api_movers(hours=hours)
    if "error" in result:
        raise HTTPException(status_code=503, detail=result["error"])
    return result

@app.get("/subnet/{netuid}")
async def subnet_detail(netuid: int, github: bool = False, key: str = Depends(verify_key)):
    check_rate_limit(key, "subnet")
    if netuid < 0 or netuid > 200:
        raise HTTPException(status_code=400, detail="Invalid netuid (0-200)")
    result = api_subnet(netuid, include_github=github)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result

@app.get("/subnets")
async def subnets(key: str = Depends(verify_key)):
    check_rate_limit(key, "default")
    from scout import load_data
    data, ts = load_data("chain")
    if not data:
        raise HTTPException(status_code=503, detail="No chain data available")
    return {
        "snapshot_time": ts[:19].replace("T"," ")+" UTC" if ts else None,
        "block":         data.get("block"),
        "subnet_count":  len(data.get("subnets", [])),
        "subnets":       data.get("subnets", []),
    }

@app.post("/admin/refresh")
async def admin_refresh(key: str = Depends(verify_key)):
    check_rate_limit(key, "default")
    try:
        data, src = get_chain_data(force=True)
        enrich = load_enrichment()
        if data:
            return {
                "status":        "ok",
                "message":       "Chain data refreshed",
                "block":         data.get("block"),
                "subnets":       len(data.get("subnets", [])),
                "tao_price_usd": enrich.get("tao_price_usd"),
                "timestamp":     datetime.now(timezone.utc).isoformat(),
            }
        return {"status": "failed", "message": f"Refresh failed: {src}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail="Refresh failed")

@app.get("/admin/stats")
async def admin_stats(key: str = Depends(verify_key)):
    check_rate_limit(key, "default")
    snaps = sorted(DATA_DIR.glob("snapshot_*.json"))
    try:
        import db
        db_info = db.db_status()
    except Exception:
        db_info = {}
    return {
        "version":            "1.3.0",
        "json_snapshots":     len(snaps),
        "db_snapshots":       db_info.get("snapshot_count", 0),
        "db_subnet_rows":     db_info.get("subnet_rows", 0),
        "latest_snapshot":    db_info.get("latest_snapshot"),
        "metadata_coverage":  f"{sum(1 for k in META)} of 129 subnets verified",
        "active_keys":        len(API_KEYS),
        "active_jobs":        dict(active_jobs),
        "timestamp":          datetime.now(timezone.utc).isoformat(),
    }

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    dashboard_file = SCRIPT_DIR / "dashboard.html"
    if dashboard_file.exists():
        return HTMLResponse(content=dashboard_file.read_text(), status_code=200)
    return HTMLResponse(content="""<html><body style="background:#0a0a0a;color:#00ff88;font-family:monospace;padding:2rem">
    <h2>TaoScout API v1.3.0</h2><p>Running. Dashboard not found.</p>
    <p><a href="/docs" style="color:#00aaff">API Docs</a></p></body></html>""")

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


if __name__ == "__main__":
    import uvicorn
    print(f"\n  TaoScout API v1.3.0 — Launch Hardened")
    print(f"  Host : {HOST}:{PORT}")
    print(f"  Keys : {len(API_KEYS)} configured")
    print(f"  Docs : http://{HOST}:{PORT}/docs\n")
    uvicorn.run("api:app", host=HOST, port=PORT, reload=False)
