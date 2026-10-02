#!/usr/bin/env python3
"""
TaoScout Stripe billing — Free/Pro entitlements.

Deliberately isolated from taoscout_auth.py's TAO payment system: this module
owns its own tables (stripe_customers, stripe_entitlements, stripe_webhook_events)
in the same sqlite file and never writes to users/orders/sessions. The TAO
expiration worker in taoscout_auth.py cannot see these tables, and this module
never touches the `users` table, so a Stripe cancellation can never revoke
TAO-paid access and a TAO expiration can never revoke a Stripe subscription.

Access is granted ONLY by a verified webhook event (customer.subscription.*).
A checkout success redirect is purely informational — see checkout.session.completed
handling below, which links the Stripe customer to an email but grants nothing.
"""
import hmac, hashlib, json, os, sqlite3, threading, time
from pathlib import Path
from datetime import datetime, timezone

BASE_DIR = Path(__file__).parent
ENV_FILE = BASE_DIR / ".env"


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

# TAOSCOUT_STRIPE_DB lets tests/tooling point this module at a database copy
# without ever touching the live file.
DB_PATH = Path(ENV.get("TAOSCOUT_STRIPE_DB", str(BASE_DIR / "data" / "taoscout_users.db")))

STRIPE_SECRET_KEY        = ENV.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET    = ENV.get("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRICE_PRO_MONTHLY = ENV.get("STRIPE_PRICE_PRO_MONTHLY", "")
STRIPE_PRICE_PRO_ANNUAL  = ENV.get("STRIPE_PRICE_PRO_ANNUAL", "")
BASE_URL                 = ENV.get("BASE_URL", "https://app.taoscout.com")

# Test-mode is enforced at this stage: a live-looking secret key (sk_live_...)
# never enables billing, regardless of what else is configured. This is a
# deliberate guardrail, not just a test convenience — live charges are not
# authorized yet.
_looks_like_test_key = STRIPE_SECRET_KEY.startswith("sk_test_")
if STRIPE_SECRET_KEY and not _looks_like_test_key:
    print("[STRIPE] STRIPE_SECRET_KEY is not a test-mode key (sk_test_...). "
          "Refusing to enable Stripe billing — live billing is not authorized at this stage.")

# Enabled only when both keys are present AND the secret key is test-mode.
# A missing/unconfigured/non-test Stripe setup never breaks the rest of the
# app, and — critically — never causes this module to touch the database or
# start a background worker (see the bottom of this file).
STRIPE_ENABLED = bool(STRIPE_SECRET_KEY and STRIPE_WEBHOOK_SECRET and _looks_like_test_key)

PRICE_PLANS = {
    "pro_monthly": {"price_id": STRIPE_PRICE_PRO_MONTHLY, "label": "Pro Monthly", "amount_usd": 2.99},
    "pro_annual":  {"price_id": STRIPE_PRICE_PRO_ANNUAL,  "label": "Pro Annual",  "amount_usd": 24.99},
}

_stripe = None


def stripe_client():
    """Import and configure the stripe SDK lazily so a missing package or
    unset keys never breaks import of this module (tables still init fine)."""
    global _stripe
    if _stripe is None:
        import stripe as _s
        _s.api_key = STRIPE_SECRET_KEY
        _s.max_network_retries = 2
        _stripe = _s
    return _stripe


# ── Database (separate tables; never touches users/orders/sessions) ─────────
def get_conn():
    conn = sqlite3.connect(str(DB_PATH), timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS stripe_customers (
            email TEXT PRIMARY KEY,
            stripe_customer_id TEXT UNIQUE NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS stripe_entitlements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL,
            stripe_subscription_id TEXT UNIQUE NOT NULL,
            stripe_price_id TEXT,
            plan TEXT,
            status TEXT NOT NULL,
            current_period_end TEXT,
            cancel_at_period_end INTEGER DEFAULT 0,
            last_event_created INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_stripe_entitlements_email ON stripe_entitlements(email);

        CREATE TABLE IF NOT EXISTS stripe_webhook_events (
            event_id TEXT PRIMARY KEY,
            type TEXT,
            received_at TEXT DEFAULT (datetime('now'))
        );
    """)
    conn.commit()
    conn.close()


# Unlike taoscout_auth.py's pattern, this does NOT run unconditionally at
# import time: a disabled/unconfigured Stripe setup must have zero database
# side effects. Tests call init_db() explicitly regardless of STRIPE_ENABLED
# (see tests/test_stripe_entitlements.py), so this gate only affects
# production import behavior.
if STRIPE_ENABLED:
    init_db()


def _iso(unix_ts):
    if unix_ts is None:
        return None
    return datetime.fromtimestamp(unix_ts, tz=timezone.utc).isoformat()


# ── Customer <-> email mapping ────────────────────────────────────────────
def get_customer_id(email):
    conn = get_conn()
    row = conn.execute("SELECT stripe_customer_id FROM stripe_customers WHERE email=?", (email,)).fetchone()
    conn.close()
    return row["stripe_customer_id"] if row else None


def save_customer_id(email, customer_id):
    conn = get_conn()
    conn.execute("""
        INSERT INTO stripe_customers (email, stripe_customer_id) VALUES (?, ?)
        ON CONFLICT(email) DO UPDATE SET stripe_customer_id=excluded.stripe_customer_id
    """, (email, customer_id))
    conn.commit()
    conn.close()


def get_email_for_customer(customer_id):
    conn = get_conn()
    row = conn.execute("SELECT email FROM stripe_customers WHERE stripe_customer_id=?", (customer_id,)).fetchone()
    conn.close()
    return row["email"] if row else None


def get_or_create_customer(email):
    existing = get_customer_id(email)
    if existing:
        return existing
    client = stripe_client()
    customer = client.Customer.create(email=email, metadata={"email": email})
    save_customer_id(email, customer.id)
    return customer.id


# ── Checkout / billing portal ─────────────────────────────────────────────
def create_checkout_session(email, plan_id):
    if plan_id not in PRICE_PLANS:
        raise ValueError(f"Unknown plan: {plan_id}")
    price_id = PRICE_PLANS[plan_id]["price_id"]
    if not price_id:
        raise ValueError(f"No Stripe price configured for {plan_id}")
    client = stripe_client()
    customer_id = get_or_create_customer(email)
    session = client.checkout.Session.create(
        mode="subscription",
        customer=customer_id,
        client_reference_id=email,
        line_items=[{"price": price_id, "quantity": 1}],
        subscription_data={"metadata": {"email": email, "plan": plan_id}},
        success_url=f"{BASE_URL}/billing/success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{BASE_URL}/billing/cancel",
        allow_promotion_codes=True,
    )
    return session.url


def create_billing_portal_session(email):
    customer_id = get_customer_id(email)
    if not customer_id:
        raise ValueError("No billing account found for this email")
    client = stripe_client()
    portal = client.billing_portal.Session.create(
        customer=customer_id,
        return_url=f"{BASE_URL}/billing",
    )
    return portal.url


# ── Webhook verification + idempotency ────────────────────────────────────
def verify_webhook(payload: bytes, sig_header: str):
    """Raises an exception on a bad/missing signature. Never trust an
    unverified body — this is the only gate before an event can change
    entitlement state."""
    client = stripe_client()
    return client.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)


# ── Entitlement state (transaction-scoped helpers) ────────────────────────
# These take an explicit connection and never commit themselves: the caller
# (process_webhook_event, below) controls the transaction so the dedupe
# marker and the entitlement change commit — or roll back — together.
def _save_customer_id_tx(conn, email, customer_id):
    conn.execute("""
        INSERT INTO stripe_customers (email, stripe_customer_id) VALUES (?, ?)
        ON CONFLICT(email) DO UPDATE SET stripe_customer_id=excluded.stripe_customer_id
    """, (email, customer_id))


def _get_email_for_customer_tx(conn, customer_id):
    row = conn.execute("SELECT email FROM stripe_customers WHERE stripe_customer_id=?", (customer_id,)).fetchone()
    return row["email"] if row else None


def _upsert_subscription_tx(conn, sub, email, event_created):
    """Insert/update one subscription row, guarded against out-of-order
    delivery: an event no newer than the last one applied to this
    subscription is ignored rather than allowed to regress state."""
    existing = conn.execute(
        "SELECT last_event_created FROM stripe_entitlements WHERE stripe_subscription_id=?",
        (sub["id"],),
    ).fetchone()
    if existing and event_created <= existing["last_event_created"]:
        return False

    items = (sub.get("items") or {}).get("data") or []
    price_id = items[0]["price"]["id"] if items else None
    plan = next((pid for pid, p in PRICE_PLANS.items() if p["price_id"] and p["price_id"] == price_id), None)
    period_end = _iso(sub.get("current_period_end"))
    conn.execute("""
        INSERT INTO stripe_entitlements
            (email, stripe_subscription_id, stripe_price_id, plan, status,
             current_period_end, cancel_at_period_end, last_event_created, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(stripe_subscription_id) DO UPDATE SET
            email=excluded.email, stripe_price_id=excluded.stripe_price_id,
            plan=excluded.plan, status=excluded.status,
            current_period_end=excluded.current_period_end,
            cancel_at_period_end=excluded.cancel_at_period_end,
            last_event_created=excluded.last_event_created,
            updated_at=datetime('now')
    """, (email, sub["id"], price_id, plan, sub["status"], period_end,
          int(bool(sub.get("cancel_at_period_end"))), event_created))
    return True


def _dispatch_tx(conn, event):
    """checkout.session.completed ONLY links customer<->email; it never
    writes an entitlement row. Access is granted/revoked exclusively by
    customer.subscription.* events, which Stripe sends independently and
    which carry the authoritative status/current_period_end."""
    etype = event["type"]
    created = event["created"]
    obj = event["data"]["object"]

    if etype == "checkout.session.completed":
        email = (obj.get("client_reference_id")
                 or (obj.get("metadata") or {}).get("email")
                 or (obj.get("customer_details") or {}).get("email")
                 or obj.get("customer_email"))
        customer_id = obj.get("customer")
        if email and customer_id:
            _save_customer_id_tx(conn, email.lower().strip(), customer_id)
        return "customer_linked"

    if etype in ("customer.subscription.created", "customer.subscription.updated",
                 "customer.subscription.deleted"):
        customer_id = obj.get("customer")
        email = _get_email_for_customer_tx(conn, customer_id) or (obj.get("metadata") or {}).get("email")
        if not email:
            return "no_email_mapping"
        applied = _upsert_subscription_tx(conn, obj, email.lower().strip(), created)
        return "applied" if applied else "stale_ignored"

    return "ignored"


def process_webhook_event(event):
    """Atomically dedupe-and-apply one verified Stripe event in a single
    transaction: the dedupe marker and the entitlement change either both
    commit or both roll back. If this raises, nothing was persisted —
    including no dedupe row — so Stripe's retry of the same event id will
    genuinely reprocess it rather than being silently swallowed as a
    duplicate on the next delivery attempt. Returns a short status string.
    """
    # verify_webhook() returns a real stripe.Event (a StripeObject): it
    # supports event["key"] subscripting but raises AttributeError on
    # .get(...) by design (it wants an explicit .to_dict() first). Normalize
    # once here so the rest of this function can use plain dict semantics
    # for both real Stripe objects and the plain dicts used in tests.
    if hasattr(event, "to_dict"):
        event = event.to_dict()

    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM stripe_webhook_events WHERE event_id=?", (event["id"],)).fetchone():
            conn.rollback()
            return "duplicate_ignored"
        result = _dispatch_tx(conn, event)
        conn.execute("INSERT INTO stripe_webhook_events (event_id, type) VALUES (?, ?)",
                     (event["id"], event["type"]))
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Reads (server-side enforcement) ───────────────────────────────────────
def get_entitlement(email):
    """Single source of truth for Stripe-funded access. Independent of
    users.role/expires_at: a TAO expiration never touches this table, and
    this function never reads or writes `users`."""
    conn = get_conn()
    row = conn.execute("""
        SELECT * FROM stripe_entitlements
        WHERE email=? AND status IN ('active','trialing')
        ORDER BY current_period_end DESC LIMIT 1
    """, (email.lower().strip(),)).fetchone()
    conn.close()
    if not row or not row["current_period_end"]:
        return {"pro": False, "plan": None, "current_period_end": None, "cancel_at_period_end": False}
    active = datetime.fromisoformat(row["current_period_end"]) > datetime.now(timezone.utc)
    return {
        "pro": active,
        "plan": row["plan"],
        "current_period_end": row["current_period_end"],
        "cancel_at_period_end": bool(row["cancel_at_period_end"]),
    }


def has_pro(email):
    return get_entitlement(email)["pro"]


# ── Local expiration reconciliation (Stripe-only; never touches TAO tables) ─
def reconcile_expirations():
    """Safety net: if a renewal/cancellation webhook is ever missed, locally
    flip status to 'expired' once current_period_end has passed. Purely
    time-based — never calls the Stripe API, never touches users/orders."""
    conn = get_conn()
    conn.execute("""
        UPDATE stripe_entitlements SET status='expired', updated_at=datetime('now')
        WHERE status IN ('active','trialing')
        AND current_period_end IS NOT NULL
        AND datetime(current_period_end) < datetime('now')
    """)
    conn.commit()
    conn.close()


def start_stripe_reconciler(interval_seconds=3600):
    def run():
        while True:
            try:
                reconcile_expirations()
            except Exception as e:
                print(f"[STRIPE RECONCILE] {e}")
            time.sleep(interval_seconds)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    print("[STRIPE RECONCILE] Started — every hour")
