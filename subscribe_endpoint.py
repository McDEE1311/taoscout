# ── Subscriber database ────────────────────────────────────────────────────────
# Add this to api.py after imports

import sqlite3
import re
from pathlib import Path

SUBSCRIBERS_DB = Path(__file__).parent / "data" / "subscribers.db"

def init_subscribers_db():
    conn = sqlite3.connect(str(SUBSCRIBERS_DB))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS subscribers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            plan TEXT DEFAULT 'free',
            source TEXT DEFAULT 'landing',
            created_at TEXT DEFAULT (datetime('now')),
            active INTEGER DEFAULT 1
        )
    """)
    conn.commit()
    conn.close()

init_subscribers_db()

def is_valid_email(email: str) -> bool:
    return bool(re.match(r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$', email))

# ── Subscribe endpoint ─────────────────────────────────────────────────────────
from pydantic import BaseModel

class SubscribeRequest(BaseModel):
    email: str
    source: str = "landing"

@app.post("/subscribe")
async def subscribe(body: SubscribeRequest):
    email = body.email.strip().lower()
    if not is_valid_email(email):
        raise HTTPException(status_code=400, detail="Invalid email address")
    try:
        conn = sqlite3.connect(str(SUBSCRIBERS_DB))
        conn.execute(
            "INSERT INTO subscribers (email, source) VALUES (?, ?)",
            (email, body.source)
        )
        conn.commit()
        conn.close()
        return {"status": "ok", "message": "You're on the list!"}
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="Already subscribed")
    except Exception as e:
        raise HTTPException(status_code=500, detail="Could not save subscription")

@app.get("/admin/subscribers")
async def get_subscribers(key: str = Depends(verify_key)):
    """Admin only — list all subscribers."""
    conn = sqlite3.connect(str(SUBSCRIBERS_DB))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, email, plan, source, created_at, active FROM subscribers ORDER BY created_at DESC"
    ).fetchall()
    conn.close()
    return {
        "count": len(rows),
        "subscribers": [dict(r) for r in rows]
    }

@app.get("/landing", response_class=HTMLResponse)
async def landing():
    """Serve the landing page."""
    landing_file = SCRIPT_DIR / "landing.html"
    if landing_file.exists():
        return HTMLResponse(content=landing_file.read_text(), status_code=200)
    raise HTTPException(status_code=404, detail="Landing page not found")
