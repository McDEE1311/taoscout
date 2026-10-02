#!/usr/bin/env python3
"""Owner/complimentary access admin + claim routes.

Exec'd into api.py's namespace like taoscout_routes.py/taoscout_stripe_routes.py.
Relies on names already present there: app, HTTPException, Depends, verify_key
(the EXISTING admin API-key gate — extended here, not replaced), Cookie, Form,
Optional, RedirectResponse, HTMLResponse (from taoscout_routes.py's own
imports), register_account (from taoscout_auth, already imported for the TAO
block), plus everything imported from taoscout_access just before this runs.
"""
from fastapi import Body
import re as _access_re
from taoscout_auth import BASE_URL

_EMAIL_RE = r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$'


# ── Admin: owner/complimentary grants (same verify_key gate as the
# existing /admin/* routes — extending that mechanism, not a new one) ──────
@app.post("/admin/access/grant-owner")
async def admin_grant_owner(email: str = Body(..., embed=True), key: str = Depends(verify_key)):
    """Never reachable from any public route — owner access only ever
    originates here."""
    email = email.strip().lower()
    if not _access_re.match(_EMAIL_RE, email):
        raise HTTPException(status_code=400, detail="Invalid email address")
    return grant_owner_access(email, granted_by=f"admin_key:{key[:6]}")


@app.post("/admin/access/revoke")
async def admin_revoke_access(email: str = Body(...), grant_type: str = Body(default=None),
                               key: str = Depends(verify_key)):
    return revoke_access(email.strip().lower(), grant_type)


@app.get("/admin/access/grants")
async def admin_list_grants(key: str = Depends(verify_key)):
    conn = get_conn()
    rows = conn.execute("SELECT * FROM access_grants ORDER BY created_at DESC LIMIT 200").fetchall()
    conn.close()
    return {"count": len(rows), "grants": [dict(r) for r in rows]}


@app.post("/admin/access/invite/create")
async def admin_create_access_invite(
    label: str = Body(...), max_uses: int = Body(default=1),
    invite_expires_hours: int = Body(default=720),
    access_duration_days: int = Body(default=None),
    key: str = Depends(verify_key),
):
    result = create_access_invite(label, max_uses, invite_expires_hours, access_duration_days)
    result["claim_url"] = f"{BASE_URL}/claim?token={result['token']}"
    return result


@app.post("/admin/access/invite/revoke")
async def admin_revoke_access_invite(token: str = Body(..., embed=True), key: str = Depends(verify_key)):
    return revoke_access_invite(token)


@app.get("/admin/access/invites")
async def admin_list_access_invites(key: str = Depends(verify_key)):
    conn = get_conn()
    rows = conn.execute("SELECT * FROM access_invites ORDER BY created_at DESC LIMIT 200").fetchall()
    conn.close()
    return {"count": len(rows), "invites": [dict(r) for r in rows]}


# ── Public: claim a single-use complimentary-access invite ────────────────
# Mirrors /invite + /invite-activate's existing shape, but redeeming this
# only ever creates a 'complimentary' access_grants row — never 'owner',
# and never touches users.subscription_status/expires_at.
@app.get("/claim", response_class=HTMLResponse)
async def claim_page(token: str = ""):
    if not token:
        return HTMLResponse(_err_html("Invalid invite link."), status_code=400)
    conn = get_conn()
    invite = conn.execute("""
        SELECT * FROM access_invites WHERE token=? AND active=1
        AND datetime(expires_at) > datetime('now') AND used_count < max_uses
    """, (token,)).fetchone()
    conn.close()
    if not invite:
        return HTMLResponse(_err_html("This invite link has expired, been revoked, or already been used."),
                             status_code=400)
    duration_note = (f"{invite['access_duration_days']}-day" if invite["access_duration_days"]
                      else "unlimited-duration")
    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>TaoScout — Complimentary Access</title>
<style>*{{box-sizing:border-box}}body{{background:#0a0a0a;color:#e0e0e0;font-family:'Courier New',monospace;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;padding:1rem}}
.card{{background:#111;border:1px solid #1e1e2e;border-radius:12px;padding:2rem;width:100%;max-width:400px}}
.logo{{color:#00d4ff;font-size:20px;font-weight:700;letter-spacing:2px;margin-bottom:8px}}
.badge{{background:#0a2a0a;border:1px solid #1a4a1a;border-radius:6px;padding:10px 14px;margin-bottom:1.5rem;color:#00ff88;font-size:13px}}
label{{font-size:10px;color:#555;text-transform:uppercase;letter-spacing:1px;display:block;margin-bottom:4px}}
input{{background:#0a0a0a;border:1px solid #2a2a3e;color:#e0e0e0;padding:12px;border-radius:6px;width:100%;font-family:'Courier New',monospace;font-size:14px;outline:none;margin-bottom:1rem}}
input:focus{{border-color:#00d4ff}}button{{background:#00d4ff;color:#000;border:none;padding:14px;border-radius:6px;width:100%;font-family:'Courier New',monospace;font-size:13px;font-weight:700;cursor:pointer}}
.note{{font-size:11px;color:#555;margin-top:1rem;font-family:sans-serif;line-height:1.6}}</style>
</head><body><div class="card">
<div class="logo">TAOSCOUT</div>
<div class="badge">Complimentary access — {duration_note}. No payment required.</div>
<form method="post" action="/claim">
<input type="hidden" name="token" value="{token}" />
<label>Your Email</label>
<input type="email" name="email" placeholder="your@email.com" required autofocus />
<button type="submit">Claim My Access →</button>
</form>
<div class="note">Enter your email to claim this access. You'll set a PIN next.</div>
</div></body></html>""")


@app.post("/claim", response_class=HTMLResponse)
async def claim_submit(token: str = Form(...), email: str = Form(...)):
    email = email.strip().lower()
    if not _access_re.match(_EMAIL_RE, email):
        return HTMLResponse(_err_html("Invalid email address."), status_code=400)
    result = redeem_access_invite(token, email)
    if "error" in result:
        return HTMLResponse(_err_html(result["error"]), status_code=400)
    identity_result = register_account(email)
    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>TaoScout — Access Claimed</title>
<style>body{{background:#0a0a0a;color:#e0e0e0;font-family:'Courier New',monospace;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}}
.card{{background:#111;border:1px solid #1e1e2e;border-radius:12px;padding:2rem;max-width:400px;text-align:center}}
a{{color:#00d4ff;text-decoration:none}}</style></head><body><div class="card">
<div style="color:#00d4ff;font-size:20px;font-weight:700;margin-bottom:1rem">TAOSCOUT</div>
<p style="color:#00ff88">Complimentary access granted{' until ' + result['expires_at'][:10] if result.get('expires_at') else ' (no expiration)'}.</p>
<p>Check your email ({email}) for a link to create your PIN.</p>
<p style="margin-top:1rem"><a href="/login">← Back to login</a></p>
</div></body></html>""")
