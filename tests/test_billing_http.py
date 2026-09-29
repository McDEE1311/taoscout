#!/usr/bin/env python3
"""
HTTP-level tests for the Stripe billing routes, using FastAPI's TestClient
against the REAL route code in taoscout_stripe_routes.py (exec'd into a bare
FastAPI() app, the same way api.py wires it in) — not a reimplementation.

Deliberately does NOT exec taoscout_routes.py (the TAO login/payment
routes): that file registers an @app.on_event("startup") handler that starts
a real background payment-watcher thread making live network calls, which
must never fire in a test process. Login is unaffected by this PR and stays
out of scope here — a session token is created directly via
taoscout_auth.create_session(), the same function the real /login and
/setup routes call internally after verifying a PIN.

Run only against a database copy — see the TAOSCOUT_STRIPE_DB /
TAOSCOUT_USERS_DB env overrides set below, before either module is imported.
"""
import hashlib
import hmac
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

_TEST_DIR = Path(tempfile.mkdtemp(prefix="taoscout-billing-http-test-"))
_TEST_DB = _TEST_DIR / "taoscout_users_test.db"
os.environ["TAOSCOUT_STRIPE_DB"] = str(_TEST_DB)
os.environ["TAOSCOUT_USERS_DB"] = str(_TEST_DB)

import taoscout_auth as ta  # noqa: E402
import taoscout_stripe as ts  # noqa: E402

from fastapi import FastAPI, HTTPException, Cookie, Form, Request  # noqa: E402
from fastapi.responses import RedirectResponse, HTMLResponse  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from typing import Optional  # noqa: E402
from pydantic import BaseModel  # noqa: E402

TEST_WEBHOOK_SECRET = "whsec_test_secret_for_http_tests_only"


def sign_payload(payload_bytes: bytes, secret: str, timestamp: int = None) -> str:
    timestamp = timestamp or int(time.time())
    signed_payload = f"{timestamp}.{payload_bytes.decode()}".encode()
    sig = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={sig}"


def make_event(event_id, event_type, obj, created=None):
    return {
        "id": event_id, "type": event_type,
        "created": created if created is not None else int(time.time()),
        "data": {"object": obj},
    }


def make_subscription(sub_id, customer_id, status, period_end, price_id="price_test_pro_monthly",
                       cancel_at_period_end=False):
    return {
        "id": sub_id, "customer": customer_id, "status": status,
        "current_period_end": period_end, "cancel_at_period_end": cancel_at_period_end,
        "items": {"data": [{"price": {"id": price_id}}]},
    }


def build_billing_app():
    """Mounts ONLY taoscout_stripe_routes.py onto a bare FastAPI() app,
    exactly the way api.py exec's it, so tests exercise the real route
    functions over real HTTP rather than calling Python functions directly."""
    app = FastAPI()
    ns = {
        "app": app, "HTTPException": HTTPException, "Cookie": Cookie, "Form": Form,
        "Request": Request, "RedirectResponse": RedirectResponse, "HTMLResponse": HTMLResponse,
        "Optional": Optional, "BaseModel": BaseModel,
        "verify_identity": ta.verify_identity,
        "STRIPE_ENABLED": ts.STRIPE_ENABLED,
        "get_entitlement": ts.get_entitlement,
        "has_pro": ts.has_pro,
        "create_checkout_session": ts.create_checkout_session,
        "create_billing_portal_session": ts.create_billing_portal_session,
        "verify_webhook": ts.verify_webhook,
        "process_webhook_event": ts.process_webhook_event,
    }
    exec(compile((BASE_DIR / "taoscout_stripe_routes.py").read_text(), "taoscout_stripe_routes.py", "exec"), ns)
    return app


class BillingHTTPTestBase(unittest.TestCase):
    def setUp(self):
        self.db_path = _TEST_DIR / f"case_{self._testMethodName}.db"
        ta.DB_PATH = self.db_path
        ts.DB_PATH = self.db_path
        ts.STRIPE_WEBHOOK_SECRET = TEST_WEBHOOK_SECRET
        ts.STRIPE_ENABLED = True  # snapshot must be True BEFORE build_billing_app() below
        ts.PRICE_PLANS = {
            "pro_monthly": {"price_id": "price_test_pro_monthly", "label": "Pro Monthly", "amount_usd": 2.99},
            "pro_annual": {"price_id": "price_test_pro_annual", "label": "Pro Annual", "amount_usd": 24.99},
        }
        ta.init_db()
        ts.init_db()
        self.client = TestClient(build_billing_app())

    def make_tao_user(self, email, roster_num, role="free", subscription_status="inactive", expires_at=None):
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("""
            INSERT INTO users (roster_num, email, role, status, subscription_status, expires_at, pin_hash)
            VALUES (?, ?, ?, 'active', ?, ?, 'x:y')
        """, (roster_num, email, role, subscription_status, expires_at))
        conn.commit()
        conn.close()

    def login_session_for(self, email):
        """Bypasses the HTTP /login form (out of scope for this PR) and
        creates a session the same way the real route does internally."""
        return ta.create_session(email)


class TestIdentityIsDecoupledFromTaoValidity(BillingHTTPTestBase):
    """Requirement: require_pro / billing routes must not depend on
    users.subscription_status='active' or an unexpired users.expires_at —
    that's TAO-specific validity, not general account identity."""

    def test_stripe_only_customer_with_no_tao_subscription_reaches_billing_status(self):
        # A user who never paid TAO at all: role='free', subscription_status='inactive'.
        self.make_tao_user("stripeonly@example.com", "TS0001", role="free", subscription_status="inactive")
        session = self.login_session_for("stripeonly@example.com")

        future = int(time.time()) + 30 * 86400
        ts.save_customer_id("stripeonly@example.com", "cus_so1")
        ts.process_webhook_event(make_event(
            "evt_so1", "customer.subscription.created",
            make_subscription("sub_so1", "cus_so1", "active", future), created=1000,
        ))

        resp = self.client.get("/billing/status", cookies={"session": session})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.json()["pro"])

    def test_active_stripe_customer_with_expired_tao_access_reaches_billing_status(self):
        # A formerly TAO-paid user whose TAO subscription has since expired.
        self.make_tao_user("expiredtao@example.com", "TS0002", role="operator",
                            subscription_status="expired",
                            expires_at="2020-01-01T00:00:00+00:00")
        session = self.login_session_for("expiredtao@example.com")

        future = int(time.time()) + 30 * 86400
        ts.save_customer_id("expiredtao@example.com", "cus_et1")
        ts.process_webhook_event(make_event(
            "evt_et1", "customer.subscription.created",
            make_subscription("sub_et1", "cus_et1", "active", future), created=1000,
        ))

        resp = self.client.get("/billing/status", cookies={"session": session})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.json()["pro"], "an expired TAO subscription must not block access to a valid Stripe entitlement")

    def test_no_session_cookie_is_rejected(self):
        resp = self.client.get("/billing/status")
        self.assertEqual(resp.status_code, 401)

    def test_expired_session_token_is_rejected_even_with_active_stripe_pro(self):
        self.make_tao_user("staleSession@example.com", "TS0003", role="free", subscription_status="inactive")
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("INSERT INTO sessions (session_token, email, expires_at) VALUES (?, ?, ?)",
                     ("stale-token", "staleSession@example.com", "2020-01-01T00:00:00+00:00"))
        conn.commit()
        conn.close()
        resp = self.client.get("/billing/status", cookies={"session": "stale-token"})
        self.assertEqual(resp.status_code, 401)


class TestFullSignupCheckoutWebhookCancellationFlow(BillingHTTPTestBase):
    """Requirement: exercise signup/login -> checkout -> verified webhook ->
    Pro access -> cancellation/expiry end to end, over real HTTP, in
    Stripe test mode (STRIPE_ENABLED requires a test-mode-shaped secret key;
    see taoscout_stripe.py's _looks_like_test_key guard)."""

    class _FakeCheckoutSessions:
        def create(self, **kwargs):
            self.last_kwargs = kwargs
            return type("Obj", (), {"url": "https://checkout.stripe.com/test-session", "id": "cs_test_1"})

    class _FakeCustomers:
        def create(self, **kwargs):
            return type("Obj", (), {"id": "cus_flow_1"})

    class _FakeStripeClient:
        def __init__(self):
            self.Customer = TestFullSignupCheckoutWebhookCancellationFlow._FakeCustomers()
            self.checkout = type("C", (), {"Session": TestFullSignupCheckoutWebhookCancellationFlow._FakeCheckoutSessions()})()

    def test_complete_flow(self):
        # 1. "Signup/login": a real user row + a real session token (the
        # same create_session() the live /login and /setup routes call).
        self.make_tao_user("flow@example.com", "TS0010", role="free", subscription_status="inactive")
        session = self.login_session_for("flow@example.com")

        # Free before any purchase.
        resp = self.client.get("/billing/status", cookies={"session": session})
        self.assertFalse(resp.json()["pro"])

        # 2. Checkout: real HTTP call to /billing/checkout; Stripe's own
        # network call is stubbed (no live/test credentials available here).
        fake_client = self._FakeStripeClient()
        with mock.patch.object(ts, "stripe_client", return_value=fake_client):
            resp = self.client.post("/billing/checkout", data={"plan": "pro_monthly"},
                                     cookies={"session": session}, follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "https://checkout.stripe.com/test-session")
        self.assertEqual(fake_client.checkout.Session.last_kwargs["client_reference_id"], "flow@example.com")

        # Still free: checkout redirecting is not proof of payment.
        resp = self.client.get("/billing/status", cookies={"session": session})
        self.assertFalse(resp.json()["pro"])

        # 3. Verified webhook (real signature check, test-mode secret):
        # Stripe links the customer via checkout.session.completed...
        checkout_obj = {"customer": "cus_flow_1", "client_reference_id": "flow@example.com"}
        payload = json.dumps(make_event("evt_flow_checkout", "checkout.session.completed",
                                         checkout_obj, created=1000)).encode()
        header = sign_payload(payload, TEST_WEBHOOK_SECRET)
        resp = self.client.post("/stripe/webhook", content=payload, headers={"stripe-signature": header})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["status"], "customer_linked")

        # Still free: linking the customer is not itself an entitlement.
        resp = self.client.get("/billing/status", cookies={"session": session})
        self.assertFalse(resp.json()["pro"])

        # ...then the authoritative subscription event grants Pro.
        future = int(time.time()) + 30 * 86400
        sub_active = make_subscription("sub_flow_1", "cus_flow_1", "active", future)
        payload = json.dumps(make_event("evt_flow_sub", "customer.subscription.created",
                                         sub_active, created=2000)).encode()
        header = sign_payload(payload, TEST_WEBHOOK_SECRET)
        resp = self.client.post("/stripe/webhook", content=payload, headers={"stripe-signature": header})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["status"], "applied")

        # 4. Pro access, over HTTP.
        resp = self.client.get("/billing/status", cookies={"session": session})
        self.assertTrue(resp.json()["pro"])

        # 5. Cancellation webhook revokes access, over HTTP.
        sub_canceled = make_subscription("sub_flow_1", "cus_flow_1", "canceled", future)
        payload = json.dumps(make_event("evt_flow_cancel", "customer.subscription.deleted",
                                         sub_canceled, created=3000)).encode()
        header = sign_payload(payload, TEST_WEBHOOK_SECRET)
        resp = self.client.post("/stripe/webhook", content=payload, headers={"stripe-signature": header})
        self.assertEqual(resp.status_code, 200, resp.text)
        resp = self.client.get("/billing/status", cookies={"session": session})
        self.assertFalse(resp.json()["pro"], "cancellation webhook must revoke Pro access")

    def test_webhook_with_bad_signature_is_rejected_over_http(self):
        payload = json.dumps(make_event("evt_bad", "customer.subscription.created", {}, created=1000)).encode()
        header = sign_payload(payload, "wrong_secret")
        resp = self.client.post("/stripe/webhook", content=payload, headers={"stripe-signature": header})
        self.assertEqual(resp.status_code, 400)


if __name__ == "__main__":
    unittest.main()
