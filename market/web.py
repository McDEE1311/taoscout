"""Read-only public research app. No accounts, payments, or trading endpoints."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from .engine import timestamp
from .ledger import history

STATIC = Path(__file__).parent / "static"


def create_app(ledger=None, report=None, resolve_pro=None):
    """resolve_pro: optional callable(Request) -> bool, injected by whoever
    mounts this app, used to decide whether /api/history's Free-tier delay
    applies to the caller. When omitted (the default — and always the case
    when this app is run standalone, as documented above and covered by
    this module's own test suite), every caller sees the full, undelayed
    history: this module remains a public, accountless preview on its own.
    The delay policy itself (withholding the single newest published slot)
    is a placeholder for a product decision, not a fixed spec; the
    enforcement mechanism is real."""
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
    def records(request: Request, before: int | None = Query(default=None, ge=1),
                limit: int = Query(default=100, ge=1, le=500)):
        try:
            result = history(ledger, limit=limit, before=before)
            newest = result["records"][0] if result["records"] else None
            result["stale"] = not newest or (datetime.now(timezone.utc) - timestamp(newest["published_at"])).total_seconds() > 9 * 3600
            is_pro = bool(resolve_pro(request)) if resolve_pro else True
            result["delayed"] = not is_pro
            # Only the first page (no pagination cursor) can ever contain
            # the single globally-newest slot, so only it is redacted —
            # stripping index 0 of every page would instead lose one record
            # per page as a Free caller paginates, which both over-redacts
            # and is inconsistent depending on their chosen page size.
            if not is_pro and before is None and result["records"]:
                result["records"] = result["records"][1:]
                result["notice"] = ("Free tier: the most recently published slot is withheld. "
                                     "Upgrade to Pro for current research. " + result.get("notice", "")).strip()
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
