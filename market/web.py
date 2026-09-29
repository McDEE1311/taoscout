"""Read-only public research app. No accounts, payments, or trading endpoints."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from .engine import timestamp
from .ledger import history

STATIC = Path(__file__).parent / "static"


def create_app(ledger=None, report=None):
    app = FastAPI(title="TaoScout Market Research", docs_url=None, redoc_url=None, openapi_url=None)
    ledger = Path(ledger or os.environ.get("TAOSCOUT_MARKET_LEDGER", "data/market-research.db"))
    report = report or os.environ.get("TAOSCOUT_MARKET_REPORT")

    @app.middleware("http")
    async def headers(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
            "connect-src 'self'; worker-src 'self'; manifest-src 'self'; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        return response

    @app.get("/health")
    def health():
        return {"status": "ok", "mode": "public-research-only", "billing_enabled": False}

    @app.get("/api/history")
    def records(before: int | None = Query(default=None, ge=1), limit: int = Query(default=100, ge=1, le=500)):
        try:
            result = history(ledger, limit=limit, before=before)
            newest = result["records"][0] if result["records"] else None
            result["stale"] = not newest or (datetime.now(timezone.utc) - timestamp(newest["published_at"])).total_seconds() > 9 * 3600
            return result
        except (ValueError, sqlite3.Error, OSError):
            raise HTTPException(status_code=503, detail="Research record unavailable; do not rely on cached data")

    @app.get("/api/simulation")
    def simulation():
        if not report:
            return {"status": "not_available", "reason": "No historical simulation has been published."}
        try:
            data = json.loads(Path(report).read_text())
            if data.get("record_type") != "historical_simulation":
                raise ValueError("Wrong record type")
            return data
        except (ValueError, OSError):
            raise HTTPException(status_code=503, detail="Simulation report unavailable")

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    # Explicit allowlist: never serve source, database, or arbitrary paths.
    @app.get("/{asset}")
    def static(asset: str):
        if asset not in {"app.js", "style.css", "manifest.webmanifest", "sw.js", "icon-192.png", "icon-512.png"}:
            raise HTTPException(status_code=404)
        return FileResponse(STATIC / asset)

    return app


app = create_app()
