#!/usr/bin/env python3
"""
Owner and complimentary access — extends the existing admin (API-key) and
invite concepts in taoscout_auth.py/taoscout_routes.py rather than creating
a competing account system.

Deliberately isolated from BOTH the TAO payment system AND taoscout_stripe.py:
this module owns its own tables (access_grants, access_invites) and never
reads or writes users.subscription_status/expires_at or any stripe_*
table. This is the actual safety property requested: a TAO expiration
sweep or a Stripe cancellation operates purely on its own tables and can
never see, let alone revoke, a row here — just like TAO and Stripe are
already isolated from each other.

Identity (who can log in at all) stays entirely taoscout_auth.py's
responsibility: redeeming a complimentary invite calls
taoscout_auth.register_account() for the login/PIN-setup part, exactly the
flow a plain Free signup already uses. This module only ever answers "does
this email have full access", independent of how that email can log in.

Owner access is never granted through any public route — grant_owner_access()
is only ever called from an admin (API-key-gated) route. The public /claim
flow can only ever create 'complimentary' grants.
"""
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from taoscout_auth import ENV, BASE_DIR as _AUTH_BASE_DIR  # noqa: F401 (reuse the same .env loader)

BASE_DIR = Path(__file__).parent
DB_PATH = Path(ENV.get("TAOSCOUT_ACCESS_DB", str(BASE_DIR / "data" / "taoscout_users.db")))


def get_conn():
    conn = sqlite3.connect(str(DB_PATH), timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS access_grants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL,
            grant_type TEXT NOT NULL CHECK(grant_type IN ('owner','complimentary')),
            granted_by TEXT,
            source_invite_token TEXT,
            expires_at TEXT,
            revoked_at TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_access_grants_email ON access_grants(email);

        CREATE TABLE IF NOT EXISTS access_invites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token TEXT UNIQUE NOT NULL,
            label TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            expires_at TEXT,
            max_uses INTEGER DEFAULT 1,
            used_count INTEGER DEFAULT 0,
            access_duration_days INTEGER,
            active INTEGER DEFAULT 1,
            revoked_at TEXT
        );
    """)
    conn.commit()
    conn.close()


init_db()


# ── Grants ──────────────────────────────────────────────────────────────────
def grant_owner_access(email, granted_by="admin"):
    """Only ever call this from an admin (API-key-gated) route. Permanent
    (no expiry) unless later revoked; immune to TAO expiration and Stripe
    cancellation because neither system can see this table."""
    email = email.lower().strip()
    conn = get_conn()
    conn.execute("""
        INSERT INTO access_grants (email, grant_type, granted_by, expires_at)
        VALUES (?, 'owner', ?, NULL)
    """, (email, granted_by))
    conn.commit()
    conn.close()
    return {"status": "granted", "email": email, "grant_type": "owner"}


def revoke_access(email, grant_type=None):
    email = email.lower().strip()
    conn = get_conn()
    if grant_type:
        conn.execute("""
            UPDATE access_grants SET revoked_at=datetime('now'), updated_at=datetime('now')
            WHERE email=? AND grant_type=? AND revoked_at IS NULL
        """, (email, grant_type))
    else:
        conn.execute("""
            UPDATE access_grants SET revoked_at=datetime('now'), updated_at=datetime('now')
            WHERE email=? AND revoked_at IS NULL
        """, (email,))
    conn.commit()
    conn.close()
    return {"status": "revoked", "email": email, "grant_type": grant_type or "all"}


def get_access(email):
    """Single source of truth for owner/complimentary access. Independent
    of users.subscription_status/expires_at and of stripe_entitlements —
    neither TAO's expiration worker nor a Stripe cancellation can affect
    this, and this never reads or writes either of those."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT * FROM access_grants WHERE email=? AND revoked_at IS NULL
        ORDER BY created_at DESC
    """, (email.lower().strip(),)).fetchall()
    conn.close()
    now = datetime.now(timezone.utc)

    def _live(row):
        return not row["expires_at"] or datetime.fromisoformat(row["expires_at"]) > now

    owner_row = next((r for r in rows if r["grant_type"] == "owner" and _live(r)), None)
    comp_row = next((r for r in rows if r["grant_type"] == "complimentary" and _live(r)), None)
    return {
        "owner": bool(owner_row),
        "complimentary": bool(comp_row),
        "active": bool(owner_row) or bool(comp_row),
        "expires_at": comp_row["expires_at"] if comp_row else None,
        "granted_by": (owner_row or comp_row)["granted_by"] if (owner_row or comp_row) else None,
    }


def has_full_access(email):
    return get_access(email)["active"]


# ── Invites (single-use, expiring, revocable — extends the existing
# influencer_invites pattern, but redeeming one never touches
# users.subscription_status/expires_at; it only ever creates a
# 'complimentary' row above) ─────────────────────────────────────────────
def create_access_invite(label, max_uses=1, invite_expires_hours=720, access_duration_days=None):
    """access_duration_days: how long the GRANTED access lasts once
    redeemed (None = until explicitly revoked). invite_expires_hours: how
    long the INVITE LINK itself remains redeemable."""
    token = secrets.token_urlsafe(32)
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=invite_expires_hours)).isoformat()
    conn = get_conn()
    conn.execute("""
        INSERT INTO access_invites (token, label, expires_at, max_uses, access_duration_days)
        VALUES (?, ?, ?, ?, ?)
    """, (token, label, expires_at, max_uses, access_duration_days))
    conn.commit()
    conn.close()
    return {"token": token, "label": label, "expires_at": expires_at,
            "max_uses": max_uses, "access_duration_days": access_duration_days}


def revoke_access_invite(token):
    conn = get_conn()
    conn.execute("""
        UPDATE access_invites SET active=0, revoked_at=datetime('now') WHERE token=?
    """, (token,))
    conn.commit()
    conn.close()
    return {"status": "revoked", "token": token}


def redeem_access_invite(token, email):
    """Grants 'complimentary' access only — never 'owner'. Identity
    (login/PIN) is NOT created here; the caller is expected to also call
    taoscout_auth.register_account(email) for that, exactly like a plain
    Free signup."""
    email = email.lower().strip()
    conn = get_conn()
    invite = conn.execute("""
        SELECT * FROM access_invites WHERE token=? AND active=1
        AND datetime(expires_at) > datetime('now') AND used_count < max_uses
    """, (token,)).fetchone()
    if not invite:
        conn.close()
        return {"error": "Invalid, expired, revoked, or already-used invite link"}

    grant_expires = None
    if invite["access_duration_days"]:
        grant_expires = (datetime.now(timezone.utc) + timedelta(days=invite["access_duration_days"])).isoformat()

    conn.execute("""
        INSERT INTO access_grants (email, grant_type, granted_by, source_invite_token, expires_at)
        VALUES (?, 'complimentary', ?, ?, ?)
    """, (email, f"invite:{invite['label'] or token[:8]}", token, grant_expires))
    conn.execute("UPDATE access_invites SET used_count=used_count+1 WHERE token=?", (token,))
    conn.commit()
    conn.close()
    return {"status": "granted", "email": email, "expires_at": grant_expires}
