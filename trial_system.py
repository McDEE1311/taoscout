"""
TaoScout Free Trial System
Add this code to taoscout_auth.py
"""

# ── ADD TO taoscout_auth.py ───────────────────────────────────────────────────

def send_trial_setup_email(email: str, token: str, roster_num: str, trial_days: int):
    """Send free trial setup email."""
    setup_url = f"{BASE_URL}/setup?token={token}"
    subject = "TaoScout Free Trial — Create Your Access PIN"
    html = f"""
<div style="font-family:sans-serif;max-width:600px;margin:0 auto;color:#1a1a1a">
<h2 style="color:#0a0a0a">Thanks for checking out TaoScout</h2>
<p>Your free trial is ready.</p>
<p style="margin:24px 0">
  <a href="{setup_url}" style="background:#00d4ff;color:#000;padding:12px 24px;
     text-decoration:none;border-radius:6px;font-weight:bold;display:inline-block">
     Create Your PIN →
  </a>
</p>
<p style="color:#555;font-size:13px">Or paste this link: {setup_url}</p>
<p style="margin-top:24px"><strong>Your trial includes:</strong></p>
<ul style="color:#333">
  <li>Daily Brief</li>
  <li>Stake Flow</li>
  <li>Death Risk</li>
  <li>Registration Opportunities</li>
  <li>GPU Rankings</li>
  <li>Opportunity Scan</li>
</ul>
<p style="color:#888;font-size:13px">Trial length: {trial_days} days</p>
<p style="color:#888;font-size:13px">After trial: You can continue with one of the paid TAO plans.</p>
<p style="margin-top:20px">
  <a href="https://discord.gg/eaxy78EjY" style="color:#0066cc">Discord: support, updates, bug reports</a>
</p>
<p style="color:#aaa;font-size:11px;margin-top:30px">Roster number: {roster_num}</p>
</div>
"""
    text = f"""Thanks for checking out TaoScout.

Your free trial is ready.

Create your PIN here: {setup_url}

Your trial includes:
- Daily Brief
- Stake Flow
- Death Risk
- Registration Opportunities
- GPU Rankings
- Opportunity Scan

Trial length: {trial_days} days
After trial: You can continue with one of the paid TAO plans.

Discord: https://discord.gg/eaxy78EjY

Roster number: {roster_num}
"""
    return send_email(email, subject, html, text)


def create_trial_user(email: str, trial_days: int = 3, source: str = "landing_trial") -> dict:
    """
    Create a free trial user, or resend setup/login link if user already exists.
    Mirrors use_invite() pattern but for direct landing-page trial signups.
    """
    email = email.lower().strip()
    conn = get_conn()

    expires_at    = (datetime.now(timezone.utc) + timedelta(days=trial_days)).isoformat()
    setup_token   = secrets.token_urlsafe(32)
    token_expires = (datetime.now(timezone.utc) + timedelta(hours=72)).isoformat()

    user = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()

    if not user:
        # Brand new user — create trial account
        roster_num = generate_roster_num()
        conn.execute("""
            INSERT INTO users (roster_num, email, role, plan_id, plan_name, status,
                subscription_status, source, expires_at, setup_token, token_expires)
            VALUES (?, ?, 'operator', 'free_trial', 'Free Trial', 'pending_setup',
                'active', ?, ?, ?, ?)
        """, (roster_num, email, source, expires_at, setup_token, token_expires))
        conn.commit()
        send_trial_setup_email(email, setup_token, roster_num, trial_days)
        conn.close()
        return {
            "status": "trial_created",
            "email": email,
            "roster_num": roster_num,
            "plan": "Free Trial",
            "expires_at": expires_at,
        }

    roster_num = user["roster_num"]
    pin_hash = user["pin_hash"] if "pin_hash" in user.keys() else None

    if not pin_hash:
        # Existing user who never set a PIN — resend setup link, refresh trial window
        conn.execute("""
            UPDATE users SET plan_id='free_trial', plan_name='Free Trial',
                status='pending_setup', subscription_status='active', source=?,
                expires_at=?, setup_token=?, token_expires=?, token_used=0
            WHERE email=?
        """, (source, expires_at, setup_token, token_expires, email))
        conn.commit()
        send_trial_setup_email(email, setup_token, roster_num, trial_days)
        conn.close()
        return {
            "status": "setup_link_resent",
            "email": email,
            "roster_num": roster_num,
            "plan": "Free Trial",
            "expires_at": expires_at,
        }

    # Active user already has a PIN — they don't need a trial, send them a reminder to log in
    conn.close()
    subject = "TaoScout — You Already Have Access"
    html = f"""
<div style="font-family:sans-serif;max-width:600px;margin:0 auto;color:#1a1a1a">
<h2>You already have a TaoScout account</h2>
<p>Roster number: <strong>{roster_num}</strong></p>
<p>Plan: <strong>{user["plan_name"]}</strong></p>
<p style="margin:24px 0">
  <a href="{BASE_URL}/login" style="background:#00d4ff;color:#000;padding:12px 24px;
     text-decoration:none;border-radius:6px;font-weight:bold;display:inline-block">
     Log In →
  </a>
</p>
<p style="color:#888;font-size:13px">Use your email and the PIN you created during setup.</p>
</div>
"""
    text = f"You already have a TaoScout account.\n\nRoster: {roster_num}\nPlan: {user['plan_name']}\n\nLog in: {BASE_URL}/login"
    send_email(email, subject, html, text)
    return {
        "status": "already_active",
        "email": email,
        "roster_num": roster_num,
        "plan": user["plan_name"],
    }

