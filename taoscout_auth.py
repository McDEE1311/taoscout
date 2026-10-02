#!/usr/bin/env python3
"""
TaoScout Auth & Payment System v2.0
Correct flow: order → payment instructions only → watcher confirms → setup link sent
"""
import sqlite3, secrets, hashlib, smtplib, os, json, time, threading
from pathlib import Path
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

BASE_DIR  = Path(__file__).parent
ENV_FILE  = BASE_DIR / ".env"

def load_env():
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                env[k.strip()] = v.strip()
    env.update(os.environ)
    return env

ENV = load_env()

# TAOSCOUT_USERS_DB lets tests/tooling point this module at a database copy
# without ever touching the live file — init_db() below runs unconditionally
# at import time.
DB_PATH = Path(ENV.get("TAOSCOUT_USERS_DB", str(BASE_DIR / "data" / "taoscout_users.db")))

PAYMENT_ADDRESS = ENV.get("TAOSCOUT_PAYMENT_ADDRESS", "5GWF2n8PGg1hgcM7KEvJohHmEgtK8Mr1Qarm4MedXfGtwcTb")
BASE_URL        = ENV.get("BASE_URL", "https://app.taoscout.com")
SMTP_HOST       = ENV.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT       = int(ENV.get("SMTP_PORT", "587"))
SMTP_USER       = ENV.get("SMTP_USER", "tao.forge.ai@gmail.com")
SMTP_PASS       = ENV.get("SMTP_PASS", "")
FROM_EMAIL      = ENV.get("FROM_EMAIL", "tao.forge.ai@gmail.com")

# ── Plans ─────────────────────────────────────────────────────────────────────
PLANS = {
    "miner_monthly": {
        "name": "Miner Monthly", "base_tao": 0.04, "days": 30,
        "role": "miner", "badge": "",
        "desc": "Daily email, GPU rankings, opportunity alerts",
        "features": ["Daily intelligence email","GPU-aware rankings","Opportunity alerts","Registration alerts","Sniper mode signals"],
    },
    "operator_monthly": {
        "name": "Operator Monthly", "base_tao": 0.11, "days": 30,
        "role": "operator", "badge": "MOST POPULAR",
        "desc": "Everything in Miner + Discord alerts, watchlists, real-time",
        "features": ["Everything in Miner","Discord bot alerts","Watchlists","Real-time notifications","Participation trends","Priority support"],
    },
    "operator_quarterly": {
        "name": "Operator Quarterly", "base_tao": 0.30, "days": 90,
        "role": "operator", "badge": "BEST VALUE",
        "desc": "90 days Operator access — save vs monthly",
        "features": ["Everything in Operator Monthly","90 days access","Save 9% vs monthly"],
    },
    "premium_annual": {
        "name": "Premium Founder Pass", "base_tao": 1.00, "days": 365,
        "role": "premium", "badge": "LIMITED",
        "desc": "12 months. Founder pricing locked forever. Limited slots.",
        "features": ["Everything in Operator","365 days access","Founder price locked forever","Early access to all new features","Direct line to builder"],
    },
    "influencer_trial": {
        "name": "Influencer Trial", "base_tao": 0.0, "days": 30,
        "role": "premium", "badge": "INVITE ONLY",
        "desc": "30-day premium access by invitation",
        "features": ["Full premium access","30 days","No payment required"],
    },
}

# ── Database ──────────────────────────────────────────────────────────────────
def get_conn():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            roster_num TEXT UNIQUE NOT NULL,
            email TEXT UNIQUE NOT NULL,
            pin_hash TEXT,
            role TEXT DEFAULT 'free',
            plan_id TEXT,
            plan_name TEXT,
            status TEXT DEFAULT 'pending',
            subscription_status TEXT DEFAULT 'inactive',
            source TEXT DEFAULT 'paid',
            created_at TEXT DEFAULT (datetime('now')),
            expires_at TEXT,
            renewed_at TEXT,
            setup_token TEXT,
            token_expires TEXT,
            token_used INTEGER DEFAULT 0,
            reminder_7d_sent INTEGER DEFAULT 0,
            reminder_1d_sent INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_ref TEXT UNIQUE NOT NULL,
            email TEXT NOT NULL,
            plan_id TEXT NOT NULL,
            plan_name TEXT NOT NULL,
            tao_amount REAL NOT NULL,
            base_amount REAL NOT NULL,
            duration_days INTEGER NOT NULL,
            status TEXT DEFAULT 'pending',
            payment_status TEXT DEFAULT 'pending',
            created_at TEXT DEFAULT (datetime('now')),
            expires_at TEXT,
            paid_at TEXT,
            tx_hash TEXT
        );

        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_token TEXT UNIQUE NOT NULL,
            email TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            expires_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS influencer_invites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token TEXT UNIQUE NOT NULL,
            label TEXT,
            created_at TEXT NOT NULL,
            expires_at TEXT,
            max_uses INTEGER DEFAULT 1,
            used_count INTEGER DEFAULT 0,
            trial_days INTEGER DEFAULT 30,
            role TEXT DEFAULT 'premium',
            plan_id TEXT DEFAULT 'influencer_trial',
            plan TEXT DEFAULT 'Influencer Trial',
            active INTEGER DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS email_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT,
            subject TEXT,
            sent_at TEXT DEFAULT (datetime('now')),
            status TEXT
        );

        CREATE TABLE IF NOT EXISTS user_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT,
            event TEXT NOT NULL,
            path TEXT,
            metadata TEXT,
            ip TEXT,
            user_agent TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
    """)
    conn.commit()
    conn.close()

init_db()

# ── Utilities ─────────────────────────────────────────────────────────────────
def generate_roster_num():
    conn = get_conn()
    row = conn.execute("SELECT COUNT(*) as n FROM users").fetchone()
    num = row["n"] + 1001
    conn.close()
    return f"TS{num:04d}"

def generate_unique_tao_amount(plan_id: str) -> float:
    plan = PLANS[plan_id]
    base = plan["base_tao"]
    if base == 0:
        return 0.0
    conn = get_conn()
    row = conn.execute(
        "SELECT COUNT(*) as n FROM orders WHERE plan_id=? AND status IN ('pending','paid')",
        (plan_id,)
    ).fetchone()
    offset = (row["n"] % 1000) * 0.00001
    conn.close()
    return round(base + offset, 5)

def hash_pin(pin: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.sha256(f"{salt}{pin}".encode()).hexdigest()
    return f"{salt}:{h}"

def verify_pin(pin: str, pin_hash: str) -> bool:
    try:
        salt, h = pin_hash.split(":", 1)
        return hashlib.sha256(f"{salt}{pin}".encode()).hexdigest() == h
    except Exception:
        return False

# ── Email ─────────────────────────────────────────────────────────────────────
def send_email(to: str, subject: str, html: str, text: str = "") -> bool:
    if not SMTP_PASS:
        print(f"[EMAIL SKIP] No SMTP_PASS. Would send: {subject} → {to}")
        return True
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = f"TaoScout <{FROM_EMAIL}>"
        msg["To"]      = to
        if text:
            msg.attach(MIMEText(text, "plain"))
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.ehlo(); s.starttls(); s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(FROM_EMAIL, to, msg.as_string())
        conn = get_conn()
        conn.execute("INSERT INTO email_log (email,subject,status) VALUES (?,?,'sent')", (to, subject))
        conn.commit(); conn.close()
        return True
    except Exception as e:
        print(f"[EMAIL ERROR] {e}")
        conn = get_conn()
        conn.execute("INSERT INTO email_log (email,subject,status) VALUES (?,?,'failed')", (to, subject))
        conn.commit(); conn.close()
        return False

def _base_email(body_html: str) -> str:
    return f"""<div style="background:#0a0a0a;color:#e0e0e0;font-family:'Courier New',monospace;padding:40px;max-width:600px;margin:0 auto">
<div style="color:#00d4ff;font-size:24px;font-weight:700;letter-spacing:2px;margin-bottom:4px">TAOSCOUT</div>
<div style="color:#555;font-size:11px;margin-bottom:28px;border-bottom:1px solid #1e1e2e;padding-bottom:16px">Bittensor Operator Intelligence</div>
{body_html}
<div style="font-size:11px;color:#333;border-top:1px solid #1e1e2e;padding-top:16px;margin-top:24px">
Questions? Reply to this email or contact tao.forge.ai@gmail.com<br>
<a href="https://taoscout.com" style="color:#555">taoscout.com</a>
</div></div>"""

def send_payment_instructions(email: str, order_ref: str, plan_id: str, tao_amount: float):
    """Step 1 email — ONLY payment instructions. No setup link yet."""
    plan = PLANS[plan_id]
    status_url = f"{BASE_URL}/order-status?ref={order_ref}"
    body = f"""
<h2 style="color:#e0e0e0;font-size:18px;margin-bottom:8px">Payment Instructions</h2>
<p style="color:#888;font-family:sans-serif;font-size:14px;margin-bottom:20px">
To activate <strong style="color:#e0e0e0">{plan['name']}</strong>, send the exact TAO amount below.
</p>
<div style="background:#111;border:1px solid #1e1e2e;border-radius:8px;padding:24px;margin-bottom:20px">
  <div style="margin-bottom:16px">
    <div style="font-size:10px;color:#555;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px">Send exactly this amount</div>
    <div style="font-size:32px;font-weight:700;color:#00ff88">{tao_amount:.5f} TAO</div>
    <div style="font-size:11px;color:#555;margin-top:4px">This exact amount identifies your payment — do not round</div>
  </div>
  <div style="margin-bottom:16px">
    <div style="font-size:10px;color:#555;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px">To this address</div>
    <div style="font-size:12px;color:#00d4ff;word-break:break-all;background:#0a0a1a;padding:10px;border-radius:4px">{PAYMENT_ADDRESS}</div>
  </div>
  <div>
    <div style="font-size:10px;color:#555;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px">Order reference</div>
    <div style="font-size:13px;color:#e0e0e0">{order_ref}</div>
  </div>
</div>
<div style="background:#0a1a0a;border:1px solid #1a3a1a;border-radius:6px;padding:14px;margin-bottom:20px;font-family:sans-serif;font-size:13px;color:#888">
  <strong style="color:#00ff88">Important:</strong><br>
  • Send the <strong>exact amount</strong> — this identifies your payment automatically<br>
  • Payment window: <strong>48 hours</strong><br>
  • After payment is detected, you'll receive a separate email with your access setup link
</div>
<a href="{status_url}" style="display:block;background:#1e1e2e;color:#00d4ff;text-align:center;padding:12px;border-radius:6px;text-decoration:none;font-size:13px;letter-spacing:1px">
  Check Payment Status →
</a>"""
    send_email(email, f"TaoScout — Payment Instructions ({plan['name']})", _base_email(body))

def send_setup_link(email: str, token: str, roster_num: str, plan_name: str):
    """Step 2 email — ONLY sent after payment confirmed."""
    setup_url = f"{BASE_URL}/setup?token={token}"
    body = f"""
<div style="background:#0a2a0a;border:1px solid #1a4a1a;border-radius:8px;padding:16px;margin-bottom:20px">
  <div style="color:#00ff88;font-size:16px;font-weight:700">✓ Payment Confirmed</div>
  <div style="color:#888;font-family:sans-serif;font-size:13px;margin-top:4px">Your {plan_name} access is ready to activate.</div>
</div>
<p style="color:#888;font-family:sans-serif;font-size:14px;margin-bottom:6px">
Roster number: <strong style="color:#00d4ff">{roster_num}</strong>
</p>
<p style="color:#888;font-family:sans-serif;font-size:14px;margin-bottom:20px">
Create your 6-digit PIN to access your dashboard.
</p>
<a href="{setup_url}" style="display:block;background:#00d4ff;color:#000;text-align:center;padding:16px;border-radius:6px;font-weight:700;font-size:14px;text-decoration:none;letter-spacing:1px;margin-bottom:16px">
  Create My PIN & Access Dashboard →
</a>
<div style="font-size:11px;color:#555;font-family:sans-serif">
This link expires in 24 hours and can only be used once.
</div>"""
    send_email(email, "TaoScout — Access Activated. Create Your PIN.", _base_email(body))

def send_renewal_reminder(email: str, roster_num: str, expires_at: str, days_left: int):
    body = f"""
<h2 style="color:#ffcc00;font-size:16px;margin-bottom:16px">⚠ Access Expiring in {days_left} Day{'s' if days_left != 1 else ''}</h2>
<p style="color:#888;font-family:sans-serif;font-size:14px;margin-bottom:6px">Roster: <strong style="color:#e0e0e0">{roster_num}</strong></p>
<p style="color:#888;font-family:sans-serif;font-size:14px;margin-bottom:20px">
Your access expires on <strong style="color:#e0e0e0">{expires_at[:10]}</strong>.
</p>
<a href="{BASE_URL}/account" style="display:block;background:#00d4ff;color:#000;text-align:center;padding:14px;border-radius:6px;font-weight:700;font-size:13px;text-decoration:none">
  Renew Now →
</a>"""
    send_email(email, f"TaoScout — Access expires in {days_left} day{'s' if days_left != 1 else ''}", _base_email(body))

# ── Order management ──────────────────────────────────────────────────────────
def create_order(email: str, plan_id: str) -> dict:
    if plan_id not in PLANS:
        raise ValueError(f"Unknown plan: {plan_id}")
    plan       = PLANS[plan_id]
    tao_amount = generate_unique_tao_amount(plan_id)
    order_ref  = f"TS-{secrets.token_hex(4).upper()}"
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=48)).isoformat()

    conn = get_conn()
    conn.execute("""
        INSERT INTO orders (order_ref, email, plan_id, plan_name, tao_amount, base_amount, duration_days, expires_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (order_ref, email.lower(), plan_id, plan["name"], tao_amount, plan["base_tao"], plan["days"], expires_at))
    conn.commit()
    conn.close()

    # Send ONLY payment instructions — no setup link yet
    send_payment_instructions(email, order_ref, plan_id, tao_amount)

    return {
        "order_ref":       order_ref,
        "tao_amount":      tao_amount,
        "plan":            plan["name"],
        "plan_id":         plan_id,
        "duration_days":   plan["days"],
        "payment_address": PAYMENT_ADDRESS,
        "expires_hours":   48,
    }

def confirm_order(order_ref: str, tx_hash: str = None) -> dict:
    """Called when payment is confirmed. Creates user and sends setup link."""
    conn = get_conn()
    order = conn.execute("SELECT * FROM orders WHERE order_ref=?", (order_ref,)).fetchone()
    if not order:
        conn.close()
        return {"error": "Order not found"}
    if order["payment_status"] == "paid":
        conn.close()
        return {"error": "Order already confirmed"}

    email   = order["email"]
    plan_id = order["plan_id"]
    plan    = PLANS[plan_id]

    conn.execute("""
        UPDATE orders SET payment_status='paid', status='paid', paid_at=datetime('now'), tx_hash=?
        WHERE order_ref=?
    """, (tx_hash, order_ref))

    user = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    expires_at    = (datetime.now(timezone.utc) + timedelta(days=plan["days"])).isoformat()
    setup_token   = secrets.token_urlsafe(32)
    token_expires = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()

    if not user:
        roster_num = generate_roster_num()
        conn.execute("""
            INSERT INTO users (roster_num, email, role, plan_id, plan_name, status,
                subscription_status, expires_at, setup_token, token_expires)
            VALUES (?, ?, ?, ?, ?, 'payment_confirmed', 'active', ?, ?, ?)
        """, (roster_num, email, plan["role"], plan_id, plan["name"], expires_at, setup_token, token_expires))
    else:
        roster_num = user["roster_num"]
        conn.execute("""
            UPDATE users SET role=?, plan_id=?, plan_name=?, status='payment_confirmed',
                subscription_status='active', expires_at=?, setup_token=?, token_expires=?,
                token_used=0, renewed_at=datetime('now')
            WHERE email=?
        """, (plan["role"], plan_id, plan["name"], expires_at, setup_token, token_expires, email))

    conn.commit()
    conn.close()

    # NOW send setup link — only after payment confirmed
    send_setup_link(email, setup_token, roster_num, plan["name"])
    return {"status": "confirmed", "email": email, "roster_num": roster_num, "plan": plan["name"]}

def get_order_status(order_ref: str) -> dict:
    conn = get_conn()
    order = conn.execute("SELECT * FROM orders WHERE order_ref=?", (order_ref,)).fetchone()
    conn.close()
    if not order:
        return {"error": "Order not found"}
    return {
        "order_ref":      order["order_ref"],
        "plan":           order["plan_name"],
        "tao_amount":     order["tao_amount"],
        "payment_status": order["payment_status"],
        "status":         order["status"],
        "created_at":     order["created_at"][:19],
        "expires_at":     order["expires_at"][:19] if order["expires_at"] else None,
        "paid_at":        order["paid_at"][:19] if order["paid_at"] else None,
    }

# ── Influencer invites ────────────────────────────────────────────────────────
def create_invite(label: str, trial_days: int = 30, max_uses: int = 1, expires_hours: int = 720) -> dict:
    token      = secrets.token_urlsafe(32)
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=expires_hours)).isoformat()
    conn = get_conn()
    conn.execute("""
        INSERT INTO influencer_invites (token, label, created_at, expires_at, max_uses, trial_days)
        VALUES (?, ?, datetime('now'), ?, ?, ?)
    """, (token, label, expires_at, max_uses, trial_days))
    conn.commit()
    conn.close()
    return {
        "token":       token,
        "full_url":    f"{BASE_URL}/invite?token={token}",
        "label":       label,
        "trial_days":  trial_days,
        "max_uses":    max_uses,
        "expires_at":  expires_at[:19],
    }

def use_invite(token: str, email: str) -> dict:
    conn = get_conn()
    invite = conn.execute("""
        SELECT * FROM influencer_invites
        WHERE token=? AND active=1
        AND (expires_at IS NULL OR datetime(expires_at) > datetime('now'))
        AND used_count < max_uses
    """, (token,)).fetchone()
    if not invite:
        conn.close()
        return {"error": "Invalid or expired invite link"}

    email = email.lower().strip()
    plan_id   = invite["plan_id"]
    plan_name = invite["plan"]
    role      = invite["role"]
    days      = invite["trial_days"]
    expires_at    = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    setup_token   = secrets.token_urlsafe(32)
    token_expires = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()

    user = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if not user:
        roster_num = generate_roster_num()
        conn.execute("""
            INSERT INTO users (roster_num, email, role, plan_id, plan_name, status,
                subscription_status, source, expires_at, setup_token, token_expires)
            VALUES (?, ?, ?, ?, ?, 'payment_confirmed', 'active', 'influencer_invite', ?, ?, ?)
        """, (roster_num, email, role, plan_id, plan_name, expires_at, setup_token, token_expires))
    else:
        roster_num = user["roster_num"]
        conn.execute("""
            UPDATE users SET role=?, plan_id=?, plan_name=?, status='payment_confirmed',
                subscription_status='active', source='influencer_invite',
                expires_at=?, setup_token=?, token_expires=?, token_used=0
            WHERE email=?
        """, (role, plan_id, plan_name, expires_at, setup_token, token_expires, email))

    conn.execute("UPDATE influencer_invites SET used_count=used_count+1 WHERE token=?", (token,))
    conn.commit()
    conn.close()
    return {"status": "ok", "setup_token": setup_token, "roster_num": roster_num, "plan": plan_name}

# ── Payment watcher ───────────────────────────────────────────────────────────
def check_pending_payments():
    import requests
    conn = get_conn()
    pending = conn.execute("""
        SELECT * FROM orders WHERE payment_status='pending'
        AND datetime(expires_at) > datetime('now')
    """).fetchall()
    conn.close()
    if not pending:
        return

    try:
        cfg = json.loads((BASE_DIR / "config.json").read_text()) if (BASE_DIR / "config.json").exists() else {}
        api_key = cfg.get("taostats_api_key", "")
    except Exception:
        api_key = ""

    if not api_key:
        return

    try:
        url = f"https://api.taostats.io/api/transfer/v1?to={PAYMENT_ADDRESS}&limit=50"
        r = requests.get(url, headers={"Authorization": api_key}, timeout=10)
        if r.status_code != 200:
            return
        transfers = r.json().get("data", [])
    except Exception as e:
        print(f"[WATCHER] {e}")
        return

    for order in pending:
        target = order["tao_amount"]
        for tx in transfers:
            try:
                tx_amount = float(tx.get("amount", 0)) / 1e9
                tx_hash   = tx.get("extrinsic_id", tx.get("hash", ""))
                if abs(tx_amount - target) < 0.000001:
                    print(f"[WATCHER] Match: {order['order_ref']} — {tx_amount} TAO")
                    confirm_order(order["order_ref"], tx_hash)
                    break
            except Exception:
                continue

def start_payment_watcher(interval_seconds=300):
    def run():
        while True:
            try: check_pending_payments()
            except Exception as e: print(f"[WATCHER] {e}")
            time.sleep(interval_seconds)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    print("[WATCHER] Started — every 5 min")

# ── Expiration ────────────────────────────────────────────────────────────────
def check_expirations():
    conn = get_conn()
    now = datetime.now(timezone.utc)
    for days in [7, 1]:
        col    = f"reminder_{days}d_sent"
        window = (now + timedelta(days=days)).isoformat()
        prev   = (now + timedelta(days=days-1)).isoformat()
        users  = conn.execute(f"""
            SELECT * FROM users WHERE subscription_status='active'
            AND expires_at <= ? AND expires_at > ?
            AND {col} = 0
        """, (window, prev)).fetchall()
        for u in users:
            send_renewal_reminder(u["email"], u["roster_num"], u["expires_at"], days)
            conn.execute(f"UPDATE users SET {col}=1 WHERE email=?", (u["email"],))
        conn.commit()
    # Reset reminder flags when user renews (expires_at pushed forward)
    conn.execute("""
        UPDATE users SET reminder_7d_sent=0, reminder_1d_sent=0
        WHERE subscription_status='active'
        AND datetime(expires_at) > datetime('now', '+2 days')
        AND (reminder_7d_sent=1 OR reminder_1d_sent=1)
    """)
    conn.execute("""
        UPDATE users SET subscription_status='expired', role='free'
        WHERE subscription_status='active' AND datetime(expires_at) < datetime('now')
    """)
    conn.commit()
    conn.close()

def start_expiration_checker(interval_seconds=3600):
    def run():
        while True:
            try: check_expirations()
            except Exception as e: print(f"[EXPIRY] {e}")
            time.sleep(interval_seconds)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    print("[EXPIRY] Checker started — every hour")

# ── Sessions ──────────────────────────────────────────────────────────────────
def create_session(email: str) -> str:
    token   = secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
    conn = get_conn()
    conn.execute("INSERT INTO sessions (session_token,email,expires_at) VALUES (?,?,?)", (token, email, expires))
    conn.commit(); conn.close()
    return token

def verify_session(token: str):
    conn = get_conn()
    row = conn.execute("""
        SELECT s.email, u.role, u.roster_num, u.status, u.expires_at,
               u.subscription_status, u.plan_name
        FROM sessions s
        JOIN users u ON s.email = u.email
        WHERE s.session_token=?
        AND datetime(s.expires_at) > datetime('now')
        AND u.subscription_status='active'
        AND datetime(u.expires_at) > datetime('now')
    """, (token,)).fetchone()
    conn.close()
    return dict(row) if row else None

def verify_identity(token: str):
    """Session identity only — does NOT require an active/unexpired TAO
    subscription. For product surfaces whose access is governed by their
    own entitlement rules (e.g. Stripe-funded Pro), not TAO's, so a
    Stripe-only customer or a customer with expired TAO access can still
    be identified and log in. verify_session() above is unchanged and
    still gates the existing TAO-funded endpoints (dashboard, account,
    etc.) on TAO subscription validity."""
    conn = get_conn()
    row = conn.execute("""
        SELECT s.email, u.roster_num
        FROM sessions s
        JOIN users u ON s.email = u.email
        WHERE s.session_token=?
        AND datetime(s.expires_at) > datetime('now')
    """, (token,)).fetchone()
    conn.close()
    return dict(row) if row else None

def delete_session(token: str):
    conn = get_conn()
    conn.execute("DELETE FROM sessions WHERE session_token=?", (token,))
    conn.commit(); conn.close()
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


def send_account_setup_email(email: str, token: str, roster_num: str):
    """Setup email for a direct Free/Stripe account registration — no TAO
    trial-length or plan copy, unlike send_trial_setup_email above."""
    setup_url = f"{BASE_URL}/setup?token={token}"
    subject = "TaoScout — Create Your Access PIN"
    html = f"""
<div style="font-family:sans-serif;max-width:600px;margin:0 auto;color:#1a1a1a">
<h2 style="color:#0a0a0a">Welcome to TaoScout</h2>
<p>Create your PIN to finish setting up your account.</p>
<p style="margin:24px 0">
  <a href="{setup_url}" style="background:#00d4ff;color:#000;padding:12px 24px;
     text-decoration:none;border-radius:6px;font-weight:bold;display:inline-block">
     Create Your PIN →
  </a>
</p>
<p style="color:#555;font-size:13px">Or paste this link: {setup_url}</p>
<p style="color:#aaa;font-size:11px;margin-top:30px">Roster number: {roster_num}</p>
</div>
"""
    text = f"Welcome to TaoScout.\n\nCreate your PIN here: {setup_url}\n\nRoster number: {roster_num}"
    return send_email(email, subject, html, text)


def register_account(email: str) -> dict:
    """Create a Free-tier account (no TAO payment involved) and send a setup
    link, or resend one if the account exists but has no PIN yet. Unlike
    create_trial_user(), this sets no expires_at and leaves role='free'/
    subscription_status='inactive' — it grants login/identity only.
    verify_session() (TAO-gated endpoints) still requires an active,
    unexpired TAO subscription regardless of this account's existence;
    verify_identity() (Stripe/billing) only needs a valid session, which
    this account can obtain once its PIN is set via the existing /setup
    flow — no TAO-specific gate involved at any step here."""
    email = email.lower().strip()
    conn = get_conn()
    user = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    setup_token   = secrets.token_urlsafe(32)
    token_expires = (datetime.now(timezone.utc) + timedelta(hours=72)).isoformat()

    if not user:
        roster_num = generate_roster_num()
        conn.execute("""
            INSERT INTO users (roster_num, email, role, plan_id, plan_name, status,
                subscription_status, source, setup_token, token_expires)
            VALUES (?, ?, 'free', 'free_account', 'Free', 'pending_setup',
                'inactive', 'free_signup', ?, ?)
        """, (roster_num, email, setup_token, token_expires))
        conn.commit()
        conn.close()
        send_account_setup_email(email, setup_token, roster_num)
        return {"status": "account_created", "email": email, "roster_num": roster_num}

    roster_num = user["roster_num"]
    pin_hash = user["pin_hash"] if "pin_hash" in user.keys() else None
    if not pin_hash:
        conn.execute("""
            UPDATE users SET setup_token=?, token_expires=?, token_used=0
            WHERE email=?
        """, (setup_token, token_expires, email))
        conn.commit()
        conn.close()
        send_account_setup_email(email, setup_token, roster_num)
        return {"status": "setup_link_resent", "email": email, "roster_num": roster_num}

    conn.close()
    subject = "TaoScout — You Already Have Access"
    html = f"""<div style="font-family:sans-serif;max-width:600px;margin:0 auto;color:#1a1a1a">
<h2>You already have a TaoScout account</h2>
<p>Roster number: <strong>{roster_num}</strong></p>
<p style="margin:24px 0">
  <a href="{BASE_URL}/login" style="background:#00d4ff;color:#000;padding:12px 24px;
     text-decoration:none;border-radius:6px;font-weight:bold;display:inline-block">Log In →</a>
</p></div>"""
    text = f"You already have a TaoScout account.\n\nRoster: {roster_num}\n\nLog in: {BASE_URL}/login"
    send_email(email, subject, html, text)
    return {"status": "already_active", "email": email, "roster_num": roster_num}



def log_user_event(email: str, event: str, path: str = None, metadata: str = None, ip: str = None, ua: str = None):
    """Log a user activity event to user_events table."""
    try:
        conn = get_conn()
        conn.execute("""
            INSERT INTO user_events (email, event, path, metadata, ip, user_agent)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (email, event, path, metadata, ip, ua))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[EVENT LOG ERROR] {e}")
