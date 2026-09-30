#!/usr/bin/env python3
"""TaoScout Routes v2 — correct payment flow + influencer invites"""

from fastapi import Cookie, Form
from fastapi.responses import RedirectResponse, HTMLResponse
from typing import Optional
from pydantic import BaseModel
import re as _re

# ── Startup workers ───────────────────────────────────────────────────────────
@app.on_event("startup")
async def _start_workers():
    start_payment_watcher(interval_seconds=300)
    start_expiration_checker(interval_seconds=3600)

# ── Plans (public) ────────────────────────────────────────────────────────────
@app.get("/plans")
async def get_plans():
    return {
        "payment_address": PAYMENT_ADDRESS,
        "plans": [
            {"id": pid, "name": p["name"], "tao": p["base_tao"],
             "days": p["days"], "role": p["role"], "badge": p.get("badge",""),
             "desc": p["desc"], "features": p.get("features",[])}
            for pid, p in PLANS.items() if pid != "influencer_trial"
        ]
    }

# ── Order creation ────────────────────────────────────────────────────────────
class OrderRequest(BaseModel):
    email: str
    plan_id: str

@app.post("/order")
async def create_new_order(body: OrderRequest):
    email = body.email.strip().lower()
    if not _re.match(r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$', email):
        raise HTTPException(status_code=400, detail="Invalid email address")
    if body.plan_id not in PLANS or body.plan_id == "influencer_trial":
        raise HTTPException(status_code=400, detail=f"Invalid plan. Choose: {[k for k in PLANS if k != 'influencer_trial']}")
    try:
        result = create_order(email, body.plan_id)
        return {**result, "status": "pending",
                "message": f"Payment instructions sent to {email}. Send exactly {result['tao_amount']:.5f} TAO to activate."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ── Order status page ─────────────────────────────────────────────────────────
@app.get("/order-status", response_class=HTMLResponse)
async def order_status_page(ref: str = ""):
    if not ref:
        return HTMLResponse("<h2>No order reference provided.</h2>", status_code=400)
    status = get_order_status(ref)
    if "error" in status:
        return HTMLResponse(f"<h2>{status['error']}</h2>", status_code=404)

    color   = "#00ff88" if status["payment_status"] == "paid" else "#ffcc00"
    label   = "✓ PAID" if status["payment_status"] == "paid" else "⏳ WAITING FOR PAYMENT"
    refresh = "" if status["payment_status"] == "paid" else '<meta http-equiv="refresh" content="30">'

    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
{refresh}<title>Order Status — TaoScout</title>
<style>*{{box-sizing:border-box}}body{{background:#0a0a0a;color:#e0e0e0;font-family:'Courier New',monospace;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;padding:1rem}}
.card{{background:#111;border:1px solid #1e1e2e;border-radius:12px;padding:2rem;width:100%;max-width:480px}}
.logo{{color:#00d4ff;font-size:20px;font-weight:700;letter-spacing:2px;margin-bottom:1.5rem}}
.status{{font-size:18px;font-weight:700;margin-bottom:1.5rem;color:{color}}}
.row{{display:flex;justify-content:space-between;padding:10px 0;border-bottom:1px solid #1a1a1a;font-size:13px}}
.label{{color:#555}}.value{{color:#e0e0e0;text-align:right}}
.amount{{font-size:24px;color:#00ff88;font-weight:700;margin:1rem 0}}
.addr{{font-size:11px;color:#00d4ff;word-break:break-all;background:#0a0a1a;padding:10px;border-radius:4px;margin:8px 0}}
.note{{font-size:12px;color:#555;font-family:sans-serif;margin-top:1rem;line-height:1.6}}
a{{color:#00d4ff;text-decoration:none}}</style>
</head><body><div class="card">
<div class="logo">TAOSCOUT</div>
<div class="status">{label}</div>
<div class="row"><span class="label">Order</span><span class="value">{status['order_ref']}</span></div>
<div class="row"><span class="label">Plan</span><span class="value">{status['plan']}</span></div>
<div class="row"><span class="label">Created</span><span class="value">{status['created_at'][:16]} UTC</span></div>
{'<div class="row"><span class="label">Paid</span><span class="value">' + status['paid_at'][:16] + ' UTC</span></div>' if status.get('paid_at') else ''}
<div style="margin:1.5rem 0">
<div style="font-size:11px;color:#555;margin-bottom:6px">SEND EXACTLY</div>
<div class="amount">{status['tao_amount']:.5f} TAO</div>
<div style="font-size:11px;color:#555;margin-bottom:6px">TO</div>
<div class="addr">{PAYMENT_ADDRESS}</div>
</div>
{'<div style="background:#0a2a0a;border:1px solid #1a4a1a;border-radius:6px;padding:12px;font-family:sans-serif;font-size:13px;color:#00ff88">Payment confirmed. Check your email for the setup link.</div>' if status['payment_status'] == 'paid' else '<div class="note">Page refreshes every 30 seconds. Once payment is detected, you\'ll receive a setup email automatically.</div>'}
<p style="margin-top:1.5rem;font-size:12px"><a href="/">← TaoScout</a></p>
</div></body></html>""")

# ── Login ─────────────────────────────────────────────────────────────────────
@app.get("/login", response_class=HTMLResponse)
async def login_page():
    return HTMLResponse(_login_html())

@app.post("/login", response_class=HTMLResponse)
async def login_submit(email: str = Form(...), pin: str = Form(...)):
    # Identity check only — no TAO subscription_status/expires_at filter.
    # Login authenticates who you are; product-specific access (the TAO
    # dashboard/account pages via verify_session(), Stripe billing via
    # verify_identity()) is enforced separately by each surface, not here.
    email = email.strip().lower()
    conn = get_conn()
    user = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    conn.close()
    if not user or not user["pin_hash"]:
        return HTMLResponse(_login_html("Invalid email or PIN."))
    if not verify_pin(pin, user["pin_hash"]):
        return HTMLResponse(_login_html("Invalid email or PIN."))
    token = create_session(email)
    try:
        from taoscout_auth import log_user_event
        log_user_event(email, "login_success", path="/login")
    except Exception:
        pass
    resp  = RedirectResponse(url="/dashboard", status_code=302)
    resp.set_cookie("session", token, httponly=True, secure=True, max_age=86400)
    return resp

@app.post("/logout")
async def logout(session: Optional[str] = Cookie(default=None)):
    if session: delete_session(session)
    resp = RedirectResponse(url="/login", status_code=302)
    resp.delete_cookie("session")
    return resp

# ── Registration (Free / Stripe accounts — no TAO payment required) ───────────
@app.get("/register", response_class=HTMLResponse)
async def register_page():
    return HTMLResponse(_register_html())

@app.post("/register", response_class=HTMLResponse)
async def register_submit(email: str = Form(...)):
    email = email.strip().lower()
    if not _re.match(r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$', email):
        return HTMLResponse(_register_html("Invalid email address."))
    try:
        result = register_account(email)
    except Exception as e:
        return HTMLResponse(_register_html(f"Registration failed: {e}"))
    return HTMLResponse(_register_sent_html(result))

# ── Setup (PIN creation — only after payment confirmed) ───────────────────────
@app.get("/setup", response_class=HTMLResponse)
async def setup_page(token: str = ""):
    if not token:
        return HTMLResponse(_err_html("Invalid setup link."), status_code=400)
    conn = get_conn()
    user = conn.execute("""
        SELECT * FROM users WHERE setup_token=? AND token_used=0
        AND datetime(token_expires) > datetime('now')
    """, (token,)).fetchone()
    conn.close()
    if not user:
        return HTMLResponse(_err_html("Setup link expired or already used. Contact tao.forge.ai@gmail.com"), status_code=400)
    return HTMLResponse(_setup_html(token, user["email"], user["roster_num"], user["plan_name"] or ""))

@app.post("/setup", response_class=HTMLResponse)
async def setup_submit(token: str = Form(...), pin: str = Form(...), pin_confirm: str = Form(...)):
    if len(pin) != 6 or not pin.isdigit():
        return HTMLResponse(_err_html("PIN must be exactly 6 digits."), status_code=400)
    if pin != pin_confirm:
        return HTMLResponse(_err_html("PINs do not match. Please try again."), status_code=400)
    conn = get_conn()
    user = conn.execute("""
        SELECT * FROM users WHERE setup_token=? AND token_used=0
        AND datetime(token_expires) > datetime('now')
    """, (token,)).fetchone()
    if not user:
        conn.close()
        return HTMLResponse(_err_html("Setup link expired or already used."), status_code=400)
    conn.execute("""
        UPDATE users SET pin_hash=?, status='active', token_used=1,
        setup_token=NULL, token_expires=NULL WHERE setup_token=?
    """, (hash_pin(pin), token))
    conn.commit(); conn.close()
    try:
        from taoscout_auth import log_user_event
        log_user_event(user["email"], "pin_created", path="/setup")
        log_user_event(user["email"], "login_success", path="/setup")
    except Exception:
        pass
    sess = create_session(user["email"])
    resp = RedirectResponse(url="/dashboard", status_code=302)
    resp.set_cookie("session", sess, httponly=True, secure=True, max_age=86400)
    return resp

# ── Invite (influencer) ───────────────────────────────────────────────────────
@app.get("/invite", response_class=HTMLResponse)
async def invite_page(token: str = ""):
    if not token:
        return HTMLResponse(_err_html("Invalid invite link."), status_code=400)
    conn = get_conn()
    invite = conn.execute("""
        SELECT * FROM influencer_invites WHERE token=? AND active=1
        AND (expires_at IS NULL OR datetime(expires_at) > datetime('now'))
        AND used_count < max_uses
    """, (token,)).fetchone()
    conn.close()
    if not invite:
        return HTMLResponse(_err_html("This invite link has expired or already been used."), status_code=400)
    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>TaoScout — You're Invited</title>
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
<div class="badge">🎟 You've been invited — {invite['trial_days']}-day Premium access. No payment required.</div>
<form method="post" action="/invite-activate">
<input type="hidden" name="token" value="{token}" />
<label>Your Email</label>
<input type="email" name="email" placeholder="your@email.com" required autofocus />
<button type="submit">Claim My Access →</button>
</form>
<div class="note">Enter your email to activate your free {invite['trial_days']}-day Premium trial. After activation, you'll set your PIN.</div>
</div></body></html>""")

@app.post("/invite-activate", response_class=HTMLResponse)
async def invite_activate(token: str = Form(...), email: str = Form(...)):
    email = email.strip().lower()
    if not _re.match(r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$', email):
        return HTMLResponse(_err_html("Invalid email address."), status_code=400)
    result = use_invite(token, email)
    if "error" in result:
        return HTMLResponse(_err_html(result["error"]), status_code=400)
    return HTMLResponse(_setup_html(result["setup_token"], email, result["roster_num"], result["plan"]))

# ── Dashboard (gated) ─────────────────────────────────────────────────────────
@app.get("/dashboard", response_class=HTMLResponse)
async def app_dashboard(session: Optional[str] = Cookie(default=None)):
    if not session:
        return RedirectResponse(url="/login", status_code=302)
    user = verify_session(session)
    if not user:
        # Check if user exists but expired
        conn = get_conn()
        from_session = conn.execute("SELECT email FROM sessions WHERE session_token=?", (session,)).fetchone()
        conn.close()
        if from_session:
            return HTMLResponse(_expired_html(from_session["email"]))
        return RedirectResponse(url="/login", status_code=302)
    dashboard_file = SCRIPT_DIR / "dashboard.html"
    if not dashboard_file.exists():
        return HTMLResponse("<h2>Dashboard not found.</h2>", status_code=404)
    content = dashboard_file.read_text()
    inject  = f"""<script>
window.TAOSCOUT_USER = {{
  email: "{user['email']}",
  role: "{user['role']}",
  roster: "{user['roster_num']}",
  plan: "{user.get('plan_name','')}",
  expires: "{user.get('expires_at','')[:10]}"
}};
</script>"""
    content = content.replace("</head>", inject + "</head>")
    return HTMLResponse(content)

# ── Account ───────────────────────────────────────────────────────────────────
@app.get("/account", response_class=HTMLResponse)
async def account_page(session: Optional[str] = Cookie(default=None)):
    if not session:
        return RedirectResponse(url="/login", status_code=302)
    user = verify_session(session)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    plans_opts = "".join([
        f'<option value="{pid}">{p["name"]} — {p["base_tao"]} TAO / {p["days"]} days</option>'
        for pid, p in PLANS.items() if pid != "influencer_trial"
    ])
    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>TaoScout Account</title>
<style>*{{box-sizing:border-box}}body{{background:#0a0a0a;color:#e0e0e0;font-family:'Courier New',monospace;padding:2rem;max-width:600px;margin:0 auto}}
h1{{color:#00d4ff;margin-bottom:1.5rem}}.card{{background:#111;border:1px solid #1e1e2e;border-radius:8px;padding:1.5rem;margin:1rem 0}}
.row{{display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid #1a1a1a;font-size:13px}}
.lbl{{color:#555}}select,input{{background:#0a0a0a;border:1px solid #2a2a3e;color:#e0e0e0;padding:10px;border-radius:4px;width:100%;font-family:'Courier New',monospace;margin:8px 0}}
button{{background:#00d4ff;color:#000;border:none;padding:12px 24px;border-radius:4px;cursor:pointer;font-weight:700;width:100%}}
a{{color:#00d4ff;text-decoration:none}}</style>
</head><body>
<h1>TAOSCOUT</h1>
<div class="card">
<div class="row"><span class="lbl">Roster</span><span style="color:#00d4ff">{user['roster_num']}</span></div>
<div class="row"><span class="lbl">Email</span><span>{user['email']}</span></div>
<div class="row"><span class="lbl">Plan</span><span>{user.get('plan_name','—')}</span></div>
<div class="row"><span class="lbl">Role</span><span>{user['role'].upper()}</span></div>
<div class="row"><span class="lbl">Expires</span><span>{user.get('expires_at','')[:10]}</span></div>
</div>
<div class="card">
<h3 style="margin-bottom:1rem;font-size:15px">Renew / Upgrade</h3>
<form method="post" action="/order-form">
<select name="plan_id">{plans_opts}</select>
<p style="font-size:11px;color:#555;margin:8px 0">You'll receive payment instructions by email</p>
<button type="submit">Get Payment Instructions</button>
</form>
</div>
<p><a href="/dashboard">← Dashboard</a> &nbsp; <a href="/logout" style="color:#555">Logout</a></p>
</body></html>""")

@app.get("/order", response_class=HTMLResponse)
async def order_page(plan: str = "", session: Optional[str] = Cookie(default=None)):
    """Order page - optionally pre-selects a plan via ?plan= query param."""
    user = verify_session(session) if session else None
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    plans_opts = ""
    for pid, pdata in PLANS.items():
        if pid == "influencer_trial":
            continue
        selected = "selected" if pid == plan else ""
        plans_opts += f'<option value="{pid}" {selected}>{pdata["name"]} — {pdata["tao_amount"]} TAO/{pdata.get("duration_days",30)}d</option>'
    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>TaoScout — Subscribe</title>
<style>body{{background:#0a0a0a;color:#e0e0e0;font-family:'Courier New',monospace;padding:2rem;max-width:500px;margin:0 auto}}
h1{{color:#00d4ff;margin-bottom:2rem;font-size:18px;letter-spacing:2px}}
.card{{background:#111;border:1px solid #1e1e2e;border-radius:8px;padding:1.5rem;margin:1rem 0}}
select{{width:100%;background:#0a0a1a;border:1px solid #2a2a3e;color:#e0e0e0;padding:10px;border-radius:4px;font-family:'Courier New',monospace;font-size:13px;margin-bottom:1rem}}
button{{background:#00d4ff;color:#000;border:none;padding:12px 24px;border-radius:4px;font-family:'Courier New',monospace;font-weight:700;cursor:pointer;width:100%;font-size:13px;letter-spacing:1px}}
p{{font-size:12px;color:#555;margin-top:8px}}
a{{color:#00d4ff;text-decoration:none}}</style></head><body>
<h1>TAOSCOUT — SUBSCRIBE</h1>
<div class="card">
<p style="color:#888;margin-bottom:1rem;font-size:13px">Logged in as {user['email']} ({user['roster_num']})</p>
<form method="post" action="/order-form">
<select name="plan_id">{plans_opts}</select>
<p style="color:#555;font-size:11px;margin-bottom:1rem">You'll receive payment instructions by email. Access activates automatically in ~5 min after payment is detected.</p>
<button type="submit">Get Payment Instructions →</button>
</form>
</div>
<p><a href="/dashboard">← Back to Dashboard</a></p>
</body></html>""")

@app.post("/order-form", response_class=HTMLResponse)
async def order_form_post(plan_id: str = Form(...), session: Optional[str] = Cookie(default=None)):
    user = verify_session(session) if session else None
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    result = create_order(user["email"], plan_id)
    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Payment Instructions</title>
<style>body{{background:#0a0a0a;color:#e0e0e0;font-family:'Courier New',monospace;padding:2rem;max-width:500px;margin:0 auto}}
.card{{background:#111;border:1px solid #1e1e2e;border-radius:8px;padding:1.5rem;margin:1rem 0}}
.amount{{font-size:32px;color:#00ff88;font-weight:700}}.addr{{color:#00d4ff;word-break:break-all;font-size:12px;background:#0a0a1a;padding:10px;border-radius:4px}}
a{{color:#00d4ff;text-decoration:none}}</style></head><body>
<h1 style="color:#00d4ff">TAOSCOUT</h1>
<h2 style="color:#00ff88;margin-bottom:1rem">✓ Order Created</h2>
<div class="card">
<p style="color:#555;font-size:12px;margin-bottom:4px">PLAN</p><p style="margin-bottom:1rem">{result['plan']}</p>
<p style="color:#555;font-size:12px;margin-bottom:4px">ORDER REF</p><p style="margin-bottom:1rem">{result['order_ref']}</p>
<p style="color:#555;font-size:12px;margin-bottom:4px">SEND EXACTLY</p>
<div class="amount">{result['tao_amount']:.5f} TAO</div>
<p style="color:#555;font-size:12px;margin:1rem 0 4px">TO ADDRESS</p>
<div class="addr">{PAYMENT_ADDRESS}</div>
</div>
<p style="color:#888;font-family:sans-serif;font-size:13px">Payment instructions sent to your email. Access activates automatically after payment is detected.</p>
<p style="margin-top:1rem"><a href="/order-status?ref={result['order_ref']}">Check Payment Status →</a></p>
<p><a href="/account">← Account</a></p>
</body></html>""")

# ── Admin routes ──────────────────────────────────────────────────────────────
@app.get("/admin/orders")
async def admin_orders(key: str = Depends(verify_key)):
    conn = get_conn()
    rows = conn.execute("SELECT * FROM orders ORDER BY created_at DESC LIMIT 100").fetchall()
    conn.close()
    return {"count": len(rows), "orders": [dict(r) for r in rows]}

@app.get("/admin/users")
async def admin_users(key: str = Depends(verify_key)):
    conn = get_conn()
    rows = conn.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()
    conn.close()
    return {"count": len(rows), "users": [dict(r) for r in rows]}

@app.post("/admin/confirm-order")
async def admin_confirm(order_ref: str, key: str = Depends(verify_key)):
    result = confirm_order(order_ref)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result

@app.post("/admin/revoke")
async def admin_revoke(email: str, key: str = Depends(verify_key)):
    conn = get_conn()
    conn.execute("UPDATE users SET subscription_status='revoked',role='free' WHERE email=?", (email,))
    conn.commit(); conn.close()
    return {"status": "revoked", "email": email}

@app.post("/admin/extend")
async def admin_extend(email: str, days: int, key: str = Depends(verify_key)):
    conn = get_conn()
    conn.execute("UPDATE users SET expires_at=datetime(expires_at,? || ' days') WHERE email=?", (str(days), email))
    conn.commit(); conn.close()
    return {"status": "extended", "email": email, "days": days}

@app.post("/admin/invite/create")
async def admin_create_invite(
    label: str, trial_days: int = 30, max_uses: int = 1,
    expires_hours: int = 720, key: str = Depends(verify_key)
):
    result = create_invite(label, trial_days, max_uses, expires_hours)
    return result

@app.get("/admin/invites")
async def admin_invites(key: str = Depends(verify_key)):
    conn = get_conn()
    rows = conn.execute("SELECT * FROM influencer_invites ORDER BY created_at DESC").fetchall()
    conn.close()
    return {"count": len(rows), "invites": [dict(r) for r in rows]}

@app.post("/admin/refresh-payments")
async def admin_refresh_payments(key: str = Depends(verify_key)):
    try:
        check_pending_payments()
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ── HTML helpers ──────────────────────────────────────────────────────────────
def _login_html(error=""):
    err = f'<p style="color:#ff4444;font-size:13px;margin-bottom:1rem">{error}</p>' if error else ""
    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>TaoScout Login</title>
<style>*{{box-sizing:border-box}}body{{background:#0a0a0a;color:#e0e0e0;font-family:'Courier New',monospace;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;padding:1rem}}
.card{{background:#111;border:1px solid #1e1e2e;border-radius:12px;padding:2.5rem;width:100%;max-width:360px}}
.logo{{color:#00d4ff;font-size:20px;font-weight:700;letter-spacing:2px;margin-bottom:4px}}
.sub{{color:#555;font-size:11px;margin-bottom:2rem}}
label{{font-size:10px;color:#555;text-transform:uppercase;letter-spacing:1px;display:block;margin-bottom:4px}}
input{{background:#0a0a0a;border:1px solid #2a2a3e;color:#e0e0e0;padding:12px;border-radius:6px;width:100%;font-family:'Courier New',monospace;font-size:14px;outline:none;margin-bottom:1rem}}
input:focus{{border-color:#00d4ff}}
button{{background:#00d4ff;color:#000;border:none;padding:14px;border-radius:6px;width:100%;font-family:'Courier New',monospace;font-size:13px;font-weight:700;cursor:pointer;letter-spacing:1px}}
.links{{text-align:center;margin-top:1rem;font-size:12px;color:#555}}
.links a{{color:#00d4ff;text-decoration:none}}</style>
</head><body><div class="card">
<div class="logo">TAOSCOUT</div>
<div class="sub">Bittensor Operator Intelligence</div>
{err}
<form method="post" action="/login">
<label>Email</label>
<input type="email" name="email" placeholder="your@email.com" required autofocus />
<label>PIN</label>
<input type="password" name="pin" placeholder="6-digit PIN" maxlength="6" pattern="[0-9]{{6}}" inputmode="numeric" required />
<button type="submit">Login →</button>
</form>
<div class="links"><a href="/">← TaoScout.com</a></div>
</div></body></html>"""

def _register_html(error=""):
    err = f'<p style="color:#ff4444;font-size:13px;margin-bottom:1rem">{error}</p>' if error else ""
    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>TaoScout — Create Account</title>
<style>*{{box-sizing:border-box}}body{{background:#0a0a0a;color:#e0e0e0;font-family:'Courier New',monospace;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;padding:1rem}}
.card{{background:#111;border:1px solid #1e1e2e;border-radius:12px;padding:2.5rem;width:100%;max-width:360px}}
.logo{{color:#00d4ff;font-size:20px;font-weight:700;letter-spacing:2px;margin-bottom:4px}}
.sub{{color:#555;font-size:11px;margin-bottom:2rem}}
label{{font-size:10px;color:#555;text-transform:uppercase;letter-spacing:1px;display:block;margin-bottom:4px}}
input{{background:#0a0a0a;border:1px solid #2a2a3e;color:#e0e0e0;padding:12px;border-radius:6px;width:100%;font-family:'Courier New',monospace;font-size:14px;outline:none;margin-bottom:1rem}}
input:focus{{border-color:#00d4ff}}
button{{background:#00d4ff;color:#000;border:none;padding:14px;border-radius:6px;width:100%;font-family:'Courier New',monospace;font-size:13px;font-weight:700;cursor:pointer;letter-spacing:1px}}
.links{{text-align:center;margin-top:1rem;font-size:12px;color:#555}}
.links a{{color:#00d4ff;text-decoration:none}}</style>
</head><body><div class="card">
<div class="logo">TAOSCOUT</div>
<div class="sub">Create a free account</div>
{err}
<form method="post" action="/register">
<label>Email</label>
<input type="email" name="email" placeholder="your@email.com" required autofocus />
<button type="submit">Create Account →</button>
</form>
<div class="links"><a href="/login">Already have an account? Log in</a></div>
</div></body></html>"""

def _register_sent_html(result):
    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>TaoScout — Check Your Email</title>
<style>body{{background:#0a0a0a;color:#e0e0e0;font-family:'Courier New',monospace;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}}
.card{{background:#111;border:1px solid #1e1e2e;border-radius:12px;padding:2rem;max-width:400px;text-align:center}}
a{{color:#00d4ff;text-decoration:none}}</style></head><body><div class="card">
<div style="color:#00d4ff;font-size:20px;font-weight:700;margin-bottom:1rem">TAOSCOUT</div>
<p>Check your email ({result['email']}) for a link to create your PIN.</p>
<p style="font-size:12px;color:#555;margin-top:1rem">Roster: {result['roster_num']}</p>
<p style="margin-top:1rem"><a href="/login">← Back to login</a></p>
</div></body></html>"""

def _setup_html(token, email, roster_num, plan_name):
    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>TaoScout — Create PIN</title>
<style>*{{box-sizing:border-box}}body{{background:#0a0a0a;color:#e0e0e0;font-family:'Courier New',monospace;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;padding:1rem}}
.card{{background:#111;border:1px solid #1e1e2e;border-radius:12px;padding:2rem;width:100%;max-width:400px}}
.logo{{color:#00d4ff;font-size:20px;font-weight:700;letter-spacing:2px;margin-bottom:8px}}
.badge{{background:#0a2a0a;border:1px solid #1a4a1a;border-radius:6px;padding:12px;margin-bottom:1.5rem;font-size:13px;color:#00ff88}}
label{{font-size:10px;color:#555;text-transform:uppercase;letter-spacing:1px;display:block;margin-bottom:4px}}
input{{background:#0a0a0a;border:1px solid #2a2a3e;color:#e0e0e0;padding:14px;border-radius:6px;width:100%;font-family:'Courier New',monospace;font-size:20px;outline:none;margin-bottom:1rem;letter-spacing:6px;text-align:center}}
input:focus{{border-color:#00d4ff}}
button{{background:#00d4ff;color:#000;border:none;padding:14px;border-radius:6px;width:100%;font-family:'Courier New',monospace;font-size:13px;font-weight:700;cursor:pointer}}
.note{{font-size:11px;color:#555;margin-top:1rem;font-family:sans-serif;line-height:1.6}}</style>
</head><body><div class="card">
<div class="logo">TAOSCOUT</div>
<div class="badge">✓ Access confirmed — {plan_name or 'Premium'}<br>
<span style="color:#888;font-size:12px">Roster: <strong style="color:#00d4ff">{roster_num}</strong> | {email}</span></div>
<p style="color:#888;font-family:sans-serif;font-size:14px;margin-bottom:1.5rem">Create a 6-digit PIN to secure your account.</p>
<form method="post" action="/setup">
<input type="hidden" name="token" value="{token}" />
<label>Choose PIN</label>
<input type="password" name="pin" placeholder="● ● ● ● ● ●" maxlength="6" pattern="[0-9]{{6}}" inputmode="numeric" required />
<label>Confirm PIN</label>
<input type="password" name="pin_confirm" placeholder="● ● ● ● ● ●" maxlength="6" pattern="[0-9]{{6}}" inputmode="numeric" required />
<button type="submit">Activate My Account →</button>
</form>
<div class="note">6 digits only. You'll use email + PIN to log in each time.</div>
</div></body></html>"""

def _err_html(msg):
    return f"""<!DOCTYPE html>
<html><head><title>TaoScout</title>
<style>body{{background:#0a0a0a;color:#e0e0e0;font-family:'Courier New',monospace;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}}
.card{{background:#111;border:1px solid #ff4444;border-radius:12px;padding:2rem;max-width:400px;text-align:center}}</style>
</head><body><div class="card">
<div style="color:#00d4ff;font-size:20px;font-weight:700;margin-bottom:1rem">TAOSCOUT</div>
<p style="color:#ff4444;font-family:sans-serif">{msg}</p>
<p style="margin-top:1rem;font-size:13px"><a href="/" style="color:#00d4ff">← TaoScout.com</a></p>
</div></body></html>"""

def _expired_html(email):
    plans_html = "".join([
        f"""<div style="background:#111;border:1px solid #1e1e2e;border-radius:8px;padding:1rem;margin-bottom:10px;display:flex;justify-content:space-between;align-items:center">
<div><div style="font-size:13px">{p['name']}</div><div style="font-size:11px;color:#555">{p['days']} days</div></div>
<div style="text-align:right"><div style="font-size:18px;color:#00ff88;font-weight:700">{p['base_tao']} TAO</div>
<form method="post" action="/order-form" style="margin:0"><input type="hidden" name="plan_id" value="{pid}">
<button type="submit" style="background:#00d4ff;color:#000;border:none;padding:6px 14px;border-radius:4px;cursor:pointer;font-family:'Courier New',monospace;font-size:11px;font-weight:700;margin-top:4px">Renew</button></form></div></div>"""
        for pid, p in PLANS.items() if pid != "influencer_trial"
    ])
    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>TaoScout — Renew</title>
<style>body{{background:#0a0a0a;color:#e0e0e0;font-family:'Courier New',monospace;padding:2rem;max-width:500px;margin:0 auto}}
a{{color:#00d4ff;text-decoration:none}}</style></head><body>
<div style="color:#00d4ff;font-size:20px;font-weight:700;margin-bottom:1.5rem">TAOSCOUT</div>
<div style="background:#1a0a0a;border:1px solid #ff4444;border-radius:8px;padding:1rem;margin-bottom:1.5rem;color:#ff6666;font-family:sans-serif;font-size:14px">
Your access has expired. Renew to continue.
</div>
{plans_html}
<p style="margin-top:1rem;font-size:12px;color:#555"><a href="/logout">Logout</a></p>
</body></html>"""
