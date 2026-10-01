#!/usr/bin/env python3
"""
Standalone server for a REAL, browser-based Stripe test-mode verification —
registration, PIN setup, login, checkout, webhook, billing portal, and
cancellation — using the actual route code, with nothing mocked.

Why this exists instead of running the real api.py: api.py imports
daily_dashboard, scout, bittensor, and other chain-scanning modules that are
unrelated to auth/billing and require a working rig config/data directory to
be meaningful. This script mounts exactly the two route files that matter
for this verification — taoscout_routes.py (registration/setup/login) and
taoscout_stripe_routes.py (billing/webhook) — onto a bare FastAPI app, the
same way api.py exec's them in, so every route handler is the real,
unmodified production code. It is not a test double: unlike
tests/test_billing_http.py, nothing here is mocked — email really sends if
SMTP_PASS is configured, and Stripe API calls go to Stripe's real test-mode
servers.

SAFETY
------
- Refuses to start unless STRIPE_SECRET_KEY looks like a test-mode key
  (sk_test_...) — see taoscout_stripe.py's own STRIPE_ENABLED guardrail.
- Uses TAOSCOUT_STRIPE_DB / TAOSCOUT_USERS_DB if set; defaults to a
  throwaway sqlite file under /tmp so this never touches a real database.
- Never prints a secret value in full.
- The TAO payment-watcher/expiration-checker background threads are left
  OFF (this script only mounts auth + billing, not the TAO payment routes),
  so no taostats API calls happen regardless of what's in config.json.

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
    stripe listen --forward-to http://127.0.0.1:8787/stripe/webhook

Open http://127.0.0.1:8787/register in a browser to begin.
"""
import argparse
import sys
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import taoscout_auth as ta  # noqa: E402
import taoscout_stripe as ts  # noqa: E402

from fastapi import FastAPI, HTTPException, Depends  # noqa: E402


def _default_db_path():
    return Path(tempfile.gettempdir()) / "taoscout-billing-verification.db"


def build_app():
    if not ts.STRIPE_SECRET_KEY.startswith("sk_test_"):
        print("STRIPE_SECRET_KEY is not set to a test-mode key (sk_test_...). Refusing to start.")
        sys.exit(1)
    if not ts.STRIPE_WEBHOOK_SECRET:
        print("STRIPE_WEBHOOK_SECRET is not set. Refusing to start.")
        sys.exit(1)
    print(f"Stripe test-mode key loaded: sk_test_...{ts.STRIPE_SECRET_KEY[-4:]} (never printed in full)")
    print(f"Database: {ta.DB_PATH}")
    if ta.ENV.get("SMTP_PASS"):
        print("SMTP configured: setup emails will really be sent.")
    else:
        print("SMTP not configured: setup links will only appear in this log (see '[EMAIL SKIP]' lines).")

    app = FastAPI(title="TaoScout billing verification (auth + Stripe routes only)")

    def verify_key(*args, **kwargs):
        return "verification-key"

    ns = {
        "app": app, "HTTPException": HTTPException, "Depends": Depends,
        "verify_key": verify_key, "SCRIPT_DIR": BASE_DIR,
        "PLANS": {}, "create_order": lambda *a, **k: {}, "confirm_order": lambda *a, **k: {},
        "verify_pin": ta.verify_pin, "hash_pin": ta.hash_pin,
        "create_session": ta.create_session, "verify_session": ta.verify_session,
        "delete_session": ta.delete_session, "register_account": ta.register_account,
        # Left as no-ops deliberately: this script verifies Stripe billing,
        # not the TAO payment flow, so the TAO payment watcher (which polls
        # taostats.io over the network) has no reason to run here.
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

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    if ts.DB_PATH == BASE_DIR / "data" / "taoscout_users.db":
        # No TAOSCOUT_STRIPE_DB/TAOSCOUT_USERS_DB override was set — redirect
        # both modules to a throwaway file so this never touches a real
        # database, instead of requiring the human to remember the env vars.
        throwaway = _default_db_path()
        print(f"No TAOSCOUT_STRIPE_DB/TAOSCOUT_USERS_DB set — using a throwaway database: {throwaway}")
        ta.DB_PATH = throwaway
        ts.DB_PATH = throwaway
        ta.init_db()
        ts.init_db()

    app = build_app()

    import uvicorn
    print(f"\nServing on http://{args.host}:{args.port} — open /register in a browser to begin.\n")
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
