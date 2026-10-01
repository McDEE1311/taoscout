#!/usr/bin/env python3
"""
Standalone server for the REAL, browser-based Stripe test-mode verification
on the combined branch (TAO + Stripe + the real /market mount, with
resolve_pro wired to actual Stripe entitlement) — registration, PIN setup,
login, checkout, signed webhook, current-research unlock, billing portal,
and cancellation (both immediate and scheduled-at-period-end).

Why this exists instead of running the real api.py: api.py imports
daily_dashboard, scout, bittensor, and other chain-scanning modules that are
unrelated to auth/billing/market and require a working rig config/data
directory to be meaningful. This script mounts exactly the three pieces
that matter here — taoscout_routes.py (registration/setup/login),
taoscout_stripe_routes.py (billing/webhook), and the real market app via
market.web.create_app(resolve_pro=...) — the same way api.py wires them,
extracted verbatim from api.py's own "Optional public research preview"
block so the /market wiring under test is guaranteed to be the real code,
not a re-transcription. Nothing here is mocked: email really sends if
SMTP_PASS is configured, and Stripe API calls go to Stripe's real test-mode
servers.

SAFETY
------
- Refuses to start unless STRIPE_SECRET_KEY looks like a test-mode key
  (sk_test_...) — see taoscout_stripe.py's own STRIPE_ENABLED guardrail.
- Uses TAOSCOUT_STRIPE_DB / TAOSCOUT_USERS_DB / TAOSCOUT_MARKET_LEDGER if
  set; otherwise defaults to throwaway files under /tmp so this never
  touches a real database.
- Seeds the market ledger with a small number of CLEARLY LABELED synthetic
  test records (source string says so explicitly; prices are flat,
  obviously-placeholder values) so nothing in the browser could be mistaken
  for real research.
- Also seeds one TAO-active test account (fixed, printed PIN) so the
  verification can confirm existing TAO access is untouched by anything
  done on the Stripe side.
- Never prints a Stripe secret value in full.
- The TAO payment-watcher/expiration-checker background threads are left
  OFF (no taostats API calls happen regardless of config.json).
- HTTPS only: the session cookie is secure=True, so a browser will not
  send it back over plain HTTP — a self-signed cert for "localhost" is
  auto-generated into a temp dir on first run (requires the `openssl` CLI;
  confirmed present on this host) unless --cert/--key are given.

USAGE
-----
    export STRIPE_SECRET_KEY=sk_test_...
    export STRIPE_WEBHOOK_SECRET=whsec_...
    export STRIPE_PRICE_PRO_MONTHLY=price_...
    export STRIPE_PRICE_PRO_ANNUAL=price_...
    # Optional — real email delivery for the setup link. Without these,
    # the setup token is only visible in this server's own log line.
    export SMTP_HOST=... SMTP_PORT=... SMTP_USER=... SMTP_PASS=... FROM_EMAIL=...

    python3 scripts/run_billing_verification_server.py [--port 8787]

Then, in a separate terminal, forward Stripe's webhooks to this server:
    stripe listen --forward-to https://127.0.0.1:8787/stripe/webhook --skip-verify

(--skip-verify: the Stripe CLI otherwise refuses the self-signed cert.)

Open https://localhost:8787/register in a browser to begin (see the
connection runbook for reaching this from a different machine over SSH).
"""
import argparse
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import taoscout_auth as ta  # noqa: E402
import taoscout_stripe as ts  # noqa: E402

from fastapi import FastAPI, HTTPException, Depends  # noqa: E402

TEST_TAO_EMAIL = "tao-verify@example.com"
TEST_TAO_PIN = "246810"
TEST_TAO_ROSTER = "TSVERIFY1"


def _default_db_path():
    return Path(tempfile.gettempdir()) / "taoscout-billing-verification.db"


def _default_ledger_path():
    return Path(tempfile.gettempdir()) / "taoscout-billing-verification-market.db"


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
        # Deliberately flat, round-number prices: unmistakably synthetic.
        return [Candle(start + i * HOUR, start + (i + 1) * HOUR, 100.0, 100.0, 100.0, 100.0)
                for i in range(hours)]

    bars = flat_candles(warmup_start, int((newest_slot - warmup_start) / HOUR) + 1)
    source = "TEST FIXTURE — synthetic flat-price data, NOT real market research. Stripe verification run only."
    _, created_older = publish(ledger_path, bars, source, older_slot)
    _, created_newer = publish(ledger_path, bars, source, newest_slot)
    print(f"Seeded test research records at {older_slot.isoformat()} and {newest_slot.isoformat()} "
          f"(source clearly labeled as a test fixture).")


def _seed_tao_test_user(db_path):
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    existing = conn.execute("SELECT 1 FROM users WHERE email=?", (TEST_TAO_EMAIL,)).fetchone()
    if not existing:
        far_future = (datetime.now(timezone.utc) + timedelta(days=365)).isoformat()
        conn.execute("""
            INSERT INTO users (roster_num, email, role, plan_name, status, subscription_status, expires_at, pin_hash)
            VALUES (?, ?, 'operator', 'Operator Monthly (test)', 'active', 'active', ?, ?)
        """, (TEST_TAO_ROSTER, TEST_TAO_EMAIL, far_future, ta.hash_pin(TEST_TAO_PIN)))
        conn.commit()
    conn.close()
    print(f"TAO-active test account ready: email={TEST_TAO_EMAIL} pin={TEST_TAO_PIN} "
          f"— log in as this user separately to confirm existing TAO/dashboard access is untouched.")


def build_app(ledger_path):
    if not ts.STRIPE_SECRET_KEY.startswith("sk_test_"):
        print("STRIPE_SECRET_KEY is not set to a test-mode key (sk_test_...). Refusing to start.")
        sys.exit(1)
    if not ts.STRIPE_WEBHOOK_SECRET:
        print("STRIPE_WEBHOOK_SECRET is not set. Refusing to start.")
        sys.exit(1)
    print(f"Stripe test-mode key loaded: sk_test_...{ts.STRIPE_SECRET_KEY[-4:]} (never printed in full)")
    print(f"User/entitlement database: {ta.DB_PATH}")
    print(f"Market ledger: {ledger_path}")
    if ta.ENV.get("SMTP_PASS"):
        print("SMTP configured: setup emails will really be sent.")
    else:
        print("SMTP not configured: setup links will only appear in this log (see '[EMAIL SKIP]' lines).")

    app = FastAPI(title="TaoScout billing + market verification (combined PR #2 + #3 branch)")

    def verify_key(*args, **kwargs):
        return "verification-key"

    ns = {
        "app": app, "HTTPException": HTTPException, "Depends": Depends,
        "verify_key": verify_key, "SCRIPT_DIR": BASE_DIR,
        "PLANS": {}, "create_order": lambda *a, **k: {}, "confirm_order": lambda *a, **k: {},
        "verify_pin": ta.verify_pin, "hash_pin": ta.hash_pin,
        "create_session": ta.create_session, "verify_session": ta.verify_session,
        "delete_session": ta.delete_session, "register_account": ta.register_account,
        # Left as no-ops deliberately: this script verifies Stripe billing
        # and market access, not the TAO payment flow, so the TAO payment
        # watcher (which polls taostats.io over the network) has no reason
        # to run here.
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
        "STRIPE_ENABLED": ts.STRIPE_ENABLED,
        "get_entitlement": ts.get_entitlement,
        "has_pro": ts.has_pro,
        "create_checkout_session": ts.create_checkout_session,
        "create_billing_portal_session": ts.create_billing_portal_session,
        "verify_webhook": ts.verify_webhook,
        "process_webhook_event": ts.process_webhook_event,
    })
    exec(compile((BASE_DIR / "taoscout_stripe_routes.py").read_text(), "taoscout_stripe_routes.py", "exec"), ns)

    # Extracted verbatim from api.py's own "Optional public research
    # preview" block (same anchors tests/test_integration_free_pro.py
    # uses), so the /market wiring under test is the real production code.
    api_source = (BASE_DIR / "api.py").read_text()
    start = api_source.index("# Optional public research preview.")
    end = api_source.index("if __name__ == \"__main__\":")
    ns["CFG"] = {
        "market_research_enabled": True,
        "market_ledger_path": str(ledger_path),
        "market_report_path": None,
    }
    exec(compile(api_source[start:end], "api.py (extracted market-mount block)", "exec"), ns)

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--cert", default=None, help="Path to a TLS cert; auto-generated self-signed if omitted")
    parser.add_argument("--key", default=None, help="Path to the matching TLS key")
    args = parser.parse_args()

    if ts.DB_PATH == BASE_DIR / "data" / "taoscout_users.db":
        throwaway = _default_db_path()
        print(f"No TAOSCOUT_STRIPE_DB/TAOSCOUT_USERS_DB set — using a throwaway database: {throwaway}")
        ta.DB_PATH = throwaway
        ts.DB_PATH = throwaway
        ta.init_db()
        ts.init_db()

    ledger_path = Path(ta.ENV.get("TAOSCOUT_MARKET_LEDGER") or _default_ledger_path())
    if not ledger_path.exists():
        _seed_test_research(ledger_path)
    _seed_tao_test_user(ta.DB_PATH)

    app = build_app(ledger_path)

    cert_path = Path(args.cert) if args.cert else Path(tempfile.gettempdir()) / "taoscout-verify-cert.pem"
    key_path = Path(args.key) if args.key else Path(tempfile.gettempdir()) / "taoscout-verify-key.pem"
    if not (args.cert and args.key):
        _ensure_self_signed_cert(cert_path, key_path)

    import uvicorn
    print(f"\nServing HTTPS on https://{args.host}:{args.port} — open /register in a browser to begin.")
    print("Self-signed certificate: your browser will warn once — proceed past it, this is expected for a local test.\n")
    uvicorn.run(app, host=args.host, port=args.port, ssl_certfile=str(cert_path), ssl_keyfile=str(key_path))


if __name__ == "__main__":
    main()
