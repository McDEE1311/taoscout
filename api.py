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
from query_log import log_query, get_stats
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
    "default":    (20, 60),
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

if __name__ == "__main__":
    import uvicorn
    print(f"\n  TaoScout API v1.3.0 — Launch Hardened")
    print(f"  Host : {HOST}:{PORT}")
    print(f"  Keys : {len(API_KEYS)} configured")
    print(f"  Docs : http://{HOST}:{PORT}/docs\n")
    uvicorn.run("api:app", host=HOST, port=PORT, reload=False)
