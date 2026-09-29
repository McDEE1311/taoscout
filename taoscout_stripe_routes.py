#!/usr/bin/env python3
"""TaoScout Stripe billing routes — Free/Pro checkout, webhook, billing portal.

Exec'd into api.py's namespace (see the `from taoscout_stripe import (...)` +
exec(...) block in api.py), mirroring taoscout_routes.py's pattern. Relies on
names already present in that namespace: app, HTTPException, Cookie, Form,
Optional, RedirectResponse, HTMLResponse, verify_session (from taoscout_auth),
plus everything imported from taoscout_stripe just before this file runs.
"""
from fastapi import Request as _StripeRequest


async def require_pro(session: Optional[str] = Cookie(default=None)):
    """Reusable dependency for gating a Pro-only endpoint. Not yet attached
    to any route in this PR — ready for the first Pro-gated feature (e.g.
    current vs. delayed market research) to depend on directly."""
    if not session:
        raise HTTPException(status_code=401, detail="Login required")
    user = verify_session(session)
    if not user:
        raise HTTPException(status_code=401, detail="Session expired")
    if not get_entitlement(user["email"])["pro"]:
        raise HTTPException(status_code=402, detail="Pro subscription required")
    return user


@app.get("/billing", response_class=HTMLResponse)
async def billing_page(session: Optional[str] = Cookie(default=None)):
    if not session:
        return RedirectResponse(url="/login", status_code=302)
    user = verify_session(session)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    ent = get_entitlement(user["email"])
    if not STRIPE_ENABLED:
        body = "<p style='color:#888'>Billing is not yet configured.</p>"
    elif ent["pro"]:
        cancel_note = "<p style='color:#ffcc00'>Cancels at period end.</p>" if ent["cancel_at_period_end"] else ""
        body = f"""<p>Plan: <strong style="color:#00d4ff">{ent['plan'] or 'Pro'}</strong></p>
<p>Renews: {ent['current_period_end'][:10] if ent['current_period_end'] else '—'}</p>
{cancel_note}
<form method="post" action="/billing/portal"><button type="submit">Manage Billing →</button></form>"""
    else:
        body = """<p style="color:#888">You're on the Free plan.</p>
<form method="post" action="/billing/checkout" style="margin-bottom:8px">
<input type="hidden" name="plan" value="pro_monthly">
<button type="submit">Upgrade — $2.99/mo →</button></form>
<form method="post" action="/billing/checkout">
<input type="hidden" name="plan" value="pro_annual">
<button type="submit">Upgrade — $24.99/yr →</button></form>"""
    return HTMLResponse(f"""<!DOCTYPE html><html><head><meta charset="UTF-8"><title>Billing — TaoScout</title>
<style>body{{background:#0a0a0a;color:#e0e0e0;font-family:'Courier New',monospace;padding:2rem;max-width:480px;margin:0 auto}}
button{{background:#00d4ff;color:#000;border:none;padding:12px 20px;border-radius:6px;font-weight:700;cursor:pointer;width:100%;font-family:inherit}}
a{{color:#00d4ff;text-decoration:none}}</style></head>
<body><h1 style="color:#00d4ff">Billing</h1>{body}<p style="margin-top:1.5rem"><a href="/dashboard">← Dashboard</a></p>
</body></html>""")


@app.get("/billing/status")
async def billing_status(session: Optional[str] = Cookie(default=None)):
    if not session:
        raise HTTPException(status_code=401, detail="Not logged in")
    user = verify_session(session)
    if not user:
        raise HTTPException(status_code=401, detail="Session expired")
    return get_entitlement(user["email"])


@app.post("/billing/checkout")
async def billing_checkout(plan: str = Form(...), session: Optional[str] = Cookie(default=None)):
    if not session:
        return RedirectResponse(url="/login", status_code=302)
    user = verify_session(session)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not STRIPE_ENABLED:
        raise HTTPException(status_code=503, detail="Billing is not configured")
    try:
        url = create_checkout_session(user["email"], plan)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return RedirectResponse(url=url, status_code=303)


@app.post("/billing/portal")
async def billing_portal(session: Optional[str] = Cookie(default=None)):
    if not session:
        return RedirectResponse(url="/login", status_code=302)
    user = verify_session(session)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    try:
        url = create_billing_portal_session(user["email"])
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return RedirectResponse(url=url, status_code=303)


# Success/cancel pages are informational only and never write to the
# database. Access is granted exclusively by the verified webhook below —
# a checkout redirect is not proof of payment.
@app.get("/billing/success", response_class=HTMLResponse)
async def billing_success():
    return HTMLResponse("""<!DOCTYPE html><html><body style="background:#0a0a0a;color:#e0e0e0;
font-family:'Courier New',monospace;padding:2rem;text-align:center">
<h2 style="color:#00ff88">Payment received — activating your plan</h2>
<p style="color:#888">This can take up to a minute while we confirm your subscription.
Refresh your <a href="/billing" style="color:#00d4ff">billing page</a> shortly.</p>
</body></html>""")


@app.get("/billing/cancel", response_class=HTMLResponse)
async def billing_cancel():
    return HTMLResponse("""<!DOCTYPE html><html><body style="background:#0a0a0a;color:#e0e0e0;
font-family:'Courier New',monospace;padding:2rem;text-align:center">
<h2>Checkout canceled</h2><p><a href="/billing" style="color:#00d4ff">← Back to billing</a></p>
</body></html>""")


# ── Webhook: the only path that ever grants/revokes Stripe-funded access ───
@app.post("/stripe/webhook")
async def stripe_webhook(request: _StripeRequest):
    if not STRIPE_ENABLED:
        raise HTTPException(status_code=503, detail="Stripe is not configured")
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    try:
        event = verify_webhook(payload, sig_header)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid signature")
    if not claim_webhook_event(event["id"], event["type"]):
        return {"status": "duplicate_ignored"}
    try:
        result = handle_event(event)
    except Exception as e:
        print(f"[STRIPE WEBHOOK] error processing {event['id']}: {e}")
        raise HTTPException(status_code=500, detail="Processing error")
    return {"status": result}
