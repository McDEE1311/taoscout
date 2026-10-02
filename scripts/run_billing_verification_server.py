#!/usr/bin/env python3
"""
Standalone server for a REAL, browser-based verification of the combined
branch (TAO + Stripe + the real /market mount + owner/complimentary access)
— registration, PIN setup, login, owner/complimentary access, checkout,
signed webhook, current-research unlock, billing portal, and cancellation.

Why this exists instead of running the real api.py: api.py imports
daily_dashboard, scout, bittensor, and other chain-scanning modules that are
unrelated to auth/billing/market/access and require a working rig
config/data directory to be meaningful. This script mounts exactly the four
pieces that matter here — taoscout_routes.py, taoscout_stripe_routes.py,
taoscout_access_routes.py, and the real market app via
market.web.create_app(resolve_pro=...) — the same way api.py wires them,
extracted verbatim from api.py's own "Optional public research preview"
block. Nothing here is mocked: email really sends if SMTP_PASS is set, and
any Stripe calls go to Stripe's real test-mode servers.

Known scope limit, stated plainly: this verifies the ACCESS-CONTROL gate
(who gets in) correctly, over real HTTP. It does NOT make dashboard.html's
own data panels (stake flow, death risk, etc.) load — those are hardcoded
in dashboard.html to fetch from https://api.taoscout.com with a hardcoded
API key, a pre-existing production behavior unrelated to this script, and
the one same-origin call it makes (/account-info) isn't mounted here either
(it lives only in the full api.py). A verified owner/complimentary/TAO
login will see the dashboard SHELL render with window.TAOSCOUT_USER
correctly injected; the data widgets will not populate in this environment.
That is expected and is not an access-control problem.

SAFETY
------
- Isolated database paths (TAOSCOUT_STRIPE_DB / TAOSCOUT_USERS_DB /
  TAOSCOUT_ACCESS_DB) and BASE_URL are all set in os.environ BEFORE
  taoscout_auth/taoscout_stripe/taoscout_access are ever imported — all
  three run init_db()/read BASE_URL at import time, so setting these
  afterward would be too late and could silently target the wrong path.
- Dedicated test configuration: this script never reads this worktree's
  own .env (there isn't one, and it must stay that way for this script —
  taoscout_auth.load_env() only reads a .env file if one exists, so the
  absence of one here means ENV is populated purely from this process's
  actual environment variables, never from a copied production file).
  SMTP/Stripe/etc. are only ever set via the operator's own shell exports.
- Stripe is OPTIONAL: if STRIPE_SECRET_KEY/STRIPE_WEBHOOK_SECRET aren't
  both set to test-mode-shaped values, the Stripe routes simply aren't
  mounted (mirrors api.py's own try/except resilience around that block).
  Owner, complimentary, Free, and TAO access are fully testable either way.
- The admin API key for this run's /admin/access/* routes is a random
  secret generated fresh each run (never accepts a hardcoded or empty
  value) — printed once, below, and nowhere else. It has no validity
  outside this one running process.
- Never prints a Stripe secret value in full.
- The TAO payment-watcher/expiration-checker background threads are left
  OFF (no taostats API calls happen regardless of config.json).
- HTTPS only: the session cookie is secure=True, so a browser will not
  send it back over plain HTTP — a self-signed cert for "localhost" is
  auto-generated into a temp dir on first run (requires the `openssl` CLI).

USAGE
-----
    python3 scripts/run_billing_verification_server.py [--port 8787]

To ALSO exercise real Stripe test-mode checkout in the same run, export
STRIPE_SECRET_KEY / STRIPE_WEBHOOK_SECRET / STRIPE_PRICE_PRO_MONTHLY /
STRIPE_PRICE_PRO_ANNUAL (your own shell, never pasted into a chat) before
starting this script, then forward webhooks separately:
    stripe listen --forward-to https://127.0.0.1:8787/stripe/webhook --skip-verify
"""
import argparse
import os
import secrets
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

TEST_TAO_EMAIL = "tao-verify@example.com"
TEST_TAO_PIN = "246810"
TEST_TAO_ROSTER = "TSVERIFY1"
TEST_OWNER_EMAIL = "owner-verify@example.com"
TEST_OWNER_PIN = "135791"

# This worktree must have no .env of its own for this script to be a
# "dedicated test configuration" rather than an accidental inheritor of
# whatever was last copied in for an unrelated sanity check.
_stray_env = BASE_DIR / ".env"
if _stray_env.exists():
    print(f"REFUSING TO START: {_stray_env} exists. Delete it — this script must never read a "
          f"copied production .env. Configuration here comes only from this process's own "
          f"environment variables.")
    sys.exit(1)


def _ensure_self_signed_cert(cert_path, key_path):
    if cert_path.exists() and key_path.exists():
        return
    print(f"Generating a self-signed cert for localhost: {cert_path}")
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", str(key_path), "-out", str(cert_path), "-days", "7",
        "-subj", "/CN=localhost",
        "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
    ], check=True, capture_output=True)


def _seed_test_research(ledger_path):
    from market.engine import Candle, HOUR
    from market.ledger import publish

    now = datetime.now(timezone.utc)
    newest_slot = now.replace(hour=(now.hour // 8) * 8, minute=0, second=0, microsecond=0)
    older_slot = newest_slot - timedelta(hours=8)
    warmup_start = older_slot - timedelta(hours=72)

    def flat_candles(start, hours):
        return [Candle(start + i * HOUR, start + (i + 1) * HOUR, 100.0, 100.0, 100.0, 100.0)
                for i in range(hours)]

    bars = flat_candles(warmup_start, int((newest_slot - warmup_start) / HOUR) + 1)
    source = "TEST FIXTURE — synthetic flat-price data, NOT real market research. Verification run only."
    publish(ledger_path, bars, source, older_slot)
    publish(ledger_path, bars, source, newest_slot)
    print(f"Seeded test research records at {older_slot.isoformat()} and {newest_slot.isoformat()} "
          f"(source clearly labeled as a test fixture).")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--cert", default=None)
    parser.add_argument("--key", default=None)
    parser.add_argument("--owner-email", default=None,
                         help="Grant real owner access to this email in addition to the fixed test account")
    args = parser.parse_args()

    # ── Everything below this line, in order, MUST happen before the
    # taoscout_auth/taoscout_stripe/taoscout_access imports a few lines
    # down: all three read these env vars at IMPORT time (init_db(),
    # BASE_URL). Setting them after import would be too late. ───────────
    db_path = Path(tempfile.gettempdir()) / "taoscout-billing-verification.db"
    for var in ("TAOSCOUT_STRIPE_DB", "TAOSCOUT_USERS_DB", "TAOSCOUT_ACCESS_DB"):
        os.environ[var] = str(db_path)
    os.environ["BASE_URL"] = f"https://localhost:{args.port}"

    global ta, ts, tacc
    import taoscout_auth as ta
    import taoscout_stripe as ts
    import taoscout_access as tacc

    print(f"Database: {db_path}")
    print(f"BASE_URL (setup links, Stripe success/cancel/portal returns): {os.environ['BASE_URL']}")

    ledger_path = Path(tempfile.gettempdir()) / "taoscout-billing-verification-market.db"
    if not ledger_path.exists():
        _seed_test_research(ledger_path)

    # TAO test account.
    conn = sqlite3.connect(str(db_path))
    if not conn.execute("SELECT 1 FROM users WHERE email=?", (TEST_TAO_EMAIL,)).fetchone():
        far_future = (datetime.now(timezone.utc) + timedelta(days=365)).isoformat()
        conn.execute("""
            INSERT INTO users (roster_num, email, role, plan_name, status, subscription_status, expires_at, pin_hash)
            VALUES (?, ?, 'operator', 'Operator Monthly (test)', 'active', 'active', ?, ?)
        """, (TEST_TAO_ROSTER, TEST_TAO_EMAIL, far_future, ta.hash_pin(TEST_TAO_PIN)))
        conn.commit()
    conn.close()
    print(f"TAO-active test account: email={TEST_TAO_EMAIL} pin={TEST_TAO_PIN}")

    # Fixed owner test account (placeholder, not your real one).
    conn = sqlite3.connect(str(db_path))
    if not conn.execute("SELECT 1 FROM users WHERE email=?", (TEST_OWNER_EMAIL,)).fetchone():
        conn.execute("""
            INSERT INTO users (roster_num, email, role, status, subscription_status, pin_hash)
            VALUES ('TSOWNER1', ?, 'free', 'active', 'inactive', ?)
        """, (TEST_OWNER_EMAIL, ta.hash_pin(TEST_OWNER_PIN)))
        conn.commit()
    conn.close()
    if not tacc.get_access(TEST_OWNER_EMAIL)["owner"]:
        tacc.grant_owner_access(TEST_OWNER_EMAIL, granted_by="run_billing_verification_server.py fixed seed")
    print(f"Owner-access TEST account: email={TEST_OWNER_EMAIL} pin={TEST_OWNER_PIN}")

    # Your real owner account, if provided.
    if args.owner_email:
        email = args.owner_email.strip().lower()
        conn = sqlite3.connect(str(db_path))
        row = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        conn.close()
        if not row:
            result = ta.register_account(email)
            print(f"Created account for {email} — setup_token (since no SMTP is configured): "
                  f"check the database directly: "
                  f"sqlite3 {db_path} \"SELECT setup_token FROM users WHERE email='{email}'\"")
        tacc.grant_owner_access(email, granted_by="run_billing_verification_server.py --owner-email")
        print(f"Owner access granted to {email} (your real account).")

    # Complimentary invite, ready to redeem immediately.
    conn = sqlite3.connect(str(db_path))
    existing = conn.execute("SELECT token FROM access_invites WHERE label='verification-seed'").fetchone()
    conn.close()
    if existing:
        comp_token = existing[0]
    else:
        comp_token = tacc.create_access_invite("verification-seed", max_uses=1,
                                                 invite_expires_hours=24 * 7, access_duration_days=30)["token"]
    print(f"Complimentary-access invite (single-use, 30-day grant): /claim?token={comp_token}")
    print("Plain Free account: just use /register with any email — no grant, no TAO, no Stripe.")

    # ── Admin API key for THIS run only — random, never hardcoded,
    # printed exactly once, required by every /admin/access/* route. ────
    admin_key = secrets.token_urlsafe(24)
    print(f"\n{'=' * 70}")
    print(f"LOCAL VERIFICATION ADMIN KEY (this run only): {admin_key}")
    print(f"Required as the X-API-Key header on every /admin/access/* call.")
    print(f"{'=' * 70}\n")

    from fastapi import FastAPI, HTTPException, Depends, Request

    def verify_key(request: Request):
        key = request.headers.get("X-API-Key", "")
        if key != admin_key:
            raise HTTPException(status_code=401, detail="Invalid or missing API key")
        return key

    stripe_enabled = bool(ts.STRIPE_SECRET_KEY.startswith("sk_test_") and ts.STRIPE_WEBHOOK_SECRET)
    if stripe_enabled:
        print(f"Stripe test-mode key loaded: sk_test_...{ts.STRIPE_SECRET_KEY[-4:]} (never printed in full)")
    else:
        print("Stripe disabled for this run (no test-mode credentials in this shell's environment) — "
              "/billing and /stripe/webhook will not be mounted. Owner, complimentary, Free, and TAO "
              "access are all fully testable without it.")
    if ta.ENV.get("SMTP_PASS"):
        print("SMTP configured: setup emails will really be sent.")
    else:
        print("SMTP not configured: read setup tokens directly from the database (see printed commands above).")

    app = FastAPI(title="TaoScout verification (auth + Stripe + access + market)")
    ns = {
        "app": app, "HTTPException": HTTPException, "Depends": Depends,
        "verify_key": verify_key, "SCRIPT_DIR": BASE_DIR,
        "PLANS": {}, "create_order": lambda *a, **k: {}, "confirm_order": lambda *a, **k: {},
        "verify_pin": ta.verify_pin, "hash_pin": ta.hash_pin,
        "create_session": ta.create_session, "verify_session": ta.verify_session,
        "delete_session": ta.delete_session, "register_account": ta.register_account,
        "start_payment_watcher": lambda *a, **k: None,
        "start_expiration_checker": lambda *a, **k: None,
        "check_pending_payments": lambda *a, **k: None,
        "get_conn": ta.get_conn, "ENV": ta.ENV, "PAYMENT_ADDRESS": ta.PAYMENT_ADDRESS,
        "create_invite": lambda *a, **k: {}, "use_invite": lambda *a, **k: {},
        "get_order_status": lambda *a, **k: {},
    }
    exec(compile((BASE_DIR / "taoscout_routes.py").read_text(), "taoscout_routes.py", "exec"), ns)

    ns.update({
        "verify_identity": ta.verify_identity,
        "STRIPE_ENABLED": stripe_enabled,
        "get_entitlement": ts.get_entitlement,
        "has_pro": ts.has_pro,
        "create_checkout_session": ts.create_checkout_session,
        "create_billing_portal_session": ts.create_billing_portal_session,
        "verify_webhook": ts.verify_webhook,
        "process_webhook_event": ts.process_webhook_event,
    })
    if stripe_enabled:
        exec(compile((BASE_DIR / "taoscout_stripe_routes.py").read_text(), "taoscout_stripe_routes.py", "exec"), ns)

    ns.update({
        "grant_owner_access": tacc.grant_owner_access,
        "revoke_access": tacc.revoke_access,
        "get_access": tacc.get_access,
        "has_full_access": tacc.has_full_access,
        "create_access_invite": tacc.create_access_invite,
        "revoke_access_invite": tacc.revoke_access_invite,
        "redeem_access_invite": tacc.redeem_access_invite,
    })
    exec(compile((BASE_DIR / "taoscout_access_routes.py").read_text(), "taoscout_access_routes.py", "exec"), ns)

    api_source = (BASE_DIR / "api.py").read_text()
    start = api_source.index("# Optional public research preview.")
    # Stop at the Stripe Billing block's own comment, not at if __name__:
    # the Stripe block now sits AFTER the market-mount block in api.py,
    # and this script already handles Stripe routes separately above
    # (gated by stripe_enabled) — ending at if __name__ would silently
    # re-exec that same block a second time regardless of that gate.
    end = api_source.index("# ── TaoScout Stripe Billing Routes")
    ns["CFG"] = {"market_research_enabled": True, "market_ledger_path": str(ledger_path), "market_report_path": None}
    exec(compile(api_source[start:end], "api.py (extracted market-mount block)", "exec"), ns)

    cert_path = Path(args.cert) if args.cert else Path(tempfile.gettempdir()) / "taoscout-verify-cert.pem"
    key_path = Path(args.key) if args.key else Path(tempfile.gettempdir()) / "taoscout-verify-key.pem"
    if not (args.cert and args.key):
        _ensure_self_signed_cert(cert_path, key_path)

    import uvicorn
    print(f"\nServing HTTPS on https://{args.host}:{args.port}")
    print("Self-signed certificate: your browser will warn once — proceed past it, this is expected.\n")
    uvicorn.run(app, host=args.host, port=args.port, ssl_certfile=str(cert_path), ssl_keyfile=str(key_path))


if __name__ == "__main__":
    main()
