#!/usr/bin/env python3
"""
Manual Stripe TEST-MODE verification script.

NOT part of the automated test suite (tests/test_stripe_entitlements.py and
tests/test_billing_http.py), and NOT executed by anyone/anything in this
repository automatically. This is prepared, not run: every automated test
mocks the actual Stripe API calls (checkout.Session.create, Customer.create,
billing_portal.Session.create) because no Stripe test-mode credentials exist
in that environment. This script makes those SAME calls for real, against
Stripe's own test-mode servers — the one thing the automated suite cannot
do — once a human supplies real test-mode credentials.

SAFETY
------
- Refuses to run any command unless STRIPE_SECRET_KEY looks like a
  test-mode key (starts with "sk_test_"). Mirrors taoscout_stripe.py's own
  STRIPE_ENABLED guardrail, independently enforced here since this script
  calls the Stripe SDK directly rather than through the gated route layer.
- Never prints STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET, or any other
  secret value in full — only a short "sk_test_...<last 4 chars>" form, to
  confirm the right key is loaded without ever exposing it.
- Reads/writes through taoscout_stripe.py's normal functions, so it uses
  whatever TAOSCOUT_STRIPE_DB points at (set it to a throwaway database
  copy if you don't want this touching anything you care about). It never
  touches TAO's users/orders/sessions tables.

WHAT THIS CANNOT DO FOR YOU
----------------------------
Stripe Checkout requires a human in a browser to enter test card details.
This script creates the checkout session and prints the URL; you open it
and pay with Stripe's test card (4242 4242 4242 4242, any future expiry,
any CVC/ZIP). It also requires a webhook endpoint Stripe can actually
reach — either a deployed server, or `stripe listen --forward-to
<your-server>/stripe/webhook` running locally — BEFORE you complete
payment, or the webhook that grants Pro access will be missed.

USAGE
-----
    export STRIPE_SECRET_KEY=sk_test_...
    export STRIPE_WEBHOOK_SECRET=whsec_...            # from the Stripe test dashboard or `stripe listen`
    export STRIPE_PRICE_PRO_MONTHLY=price_...         # a real test-mode Price id
    export TAOSCOUT_STRIPE_DB=/path/to/a/throwaway.db  # recommended

    # 1. Start a webhook forwarder (separate terminal), pointed at wherever
    #    the real app is serving /stripe/webhook:
    stripe listen --forward-to http://localhost:8765/stripe/webhook

    # 2. Create a checkout session and complete payment in the browser:
    python3 scripts/verify_stripe_test_mode.py checkout you@example.com

    # 3. Confirm the webhook granted Pro access:
    python3 scripts/verify_stripe_test_mode.py check-entitlement you@example.com

    # 4. Verify the billing portal (self-serve manage/cancel) works:
    python3 scripts/verify_stripe_test_mode.py billing-portal you@example.com

    # 5. Cancel via the real Stripe API and confirm the cancellation
    #    webhook revokes access:
    python3 scripts/verify_stripe_test_mode.py cancel you@example.com
    python3 scripts/verify_stripe_test_mode.py check-entitlement you@example.com
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import taoscout_stripe as ts  # noqa: E402


def _require_test_mode():
    if not ts.STRIPE_SECRET_KEY:
        print("STRIPE_SECRET_KEY is not set. Refusing to run.")
        sys.exit(1)
    if not ts.STRIPE_SECRET_KEY.startswith("sk_test_"):
        print("STRIPE_SECRET_KEY does not look like a test-mode key (sk_test_...).")
        print("This script will never run against a live-looking key. Refusing to run.")
        sys.exit(1)
    if not ts.STRIPE_WEBHOOK_SECRET:
        print("STRIPE_WEBHOOK_SECRET is not set — the webhook step will fail. Refusing to run.")
        sys.exit(1)
    print(f"Stripe test-mode key loaded: sk_test_...{ts.STRIPE_SECRET_KEY[-4:]} (never printed in full)")
    print(f"Database: {ts.DB_PATH}")


def cmd_checkout(email, plan):
    _require_test_mode()
    url = ts.create_checkout_session(email, plan)
    print(f"\nPlan: {plan}")
    print("Open this URL in a browser and pay with Stripe's test card:")
    print("  Card: 4242 4242 4242 4242, any future expiry, any CVC, any ZIP")
    print(f"  URL:  {url}\n")
    print("Make sure your webhook forwarder (stripe listen --forward-to .../stripe/webhook,")
    print("or a real deployed webhook endpoint) is running BEFORE you pay, or the webhook")
    print("that grants Pro access will be missed.")


def cmd_check_entitlement(email):
    _require_test_mode()
    print(f"Entitlement for {email}: {ts.get_entitlement(email)}")


def cmd_billing_portal(email):
    _require_test_mode()
    url = ts.create_billing_portal_session(email)
    print(f"\nOpen this URL to verify the billing portal (self-serve manage/cancel):")
    print(f"  {url}\n")


def cmd_cancel(email):
    _require_test_mode()
    customer_id = ts.get_customer_id(email)
    if not customer_id:
        print(f"No Stripe customer on file for {email}. Run `checkout` first.")
        sys.exit(1)
    client = ts.stripe_client()
    subs = client.Subscription.list(customer=customer_id, status="active", limit=10)
    if not subs.data:
        print(f"No active subscription found for {email}.")
        sys.exit(1)
    for sub in subs.data:
        print(f"Canceling subscription {sub.id} via the real Stripe API (test mode) ...")
        client.Subscription.cancel(sub.id)
    print("Canceled. Wait a few seconds for the webhook to arrive (check your forwarder's "
          "output), then re-run check-entitlement — pro should flip to False.")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("checkout", help="Create a real Stripe test-mode checkout session")
    p.add_argument("email")
    p.add_argument("--plan", default="pro_monthly", choices=["pro_monthly", "pro_annual"])

    p = sub.add_parser("check-entitlement", help="Print the current local entitlement for an email")
    p.add_argument("email")

    p = sub.add_parser("billing-portal", help="Create a real Stripe billing portal session")
    p.add_argument("email")

    p = sub.add_parser("cancel", help="Cancel the customer's active subscription via the real Stripe API")
    p.add_argument("email")

    args = parser.parse_args()
    {
        "checkout": lambda: cmd_checkout(args.email, args.plan),
        "check-entitlement": lambda: cmd_check_entitlement(args.email),
        "billing-portal": lambda: cmd_billing_portal(args.email),
        "cancel": lambda: cmd_cancel(args.email),
    }[args.command]()


if __name__ == "__main__":
    main()
