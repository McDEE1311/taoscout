#!/usr/bin/env python3
"""
HTTP-level tests for account registration, login, and Stripe billing, using
FastAPI's TestClient against the REAL route code in taoscout_routes.py and
taoscout_stripe_routes.py (exec'd into a bare FastAPI() app, the same way
api.py wires both in) — not a reimplementation.

Login and registration are exercised over real HTTP (POST /register,
POST /setup, POST /login) rather than by calling taoscout_auth.create_session()
directly. The background payment-watcher/expiration-checker startup hooks and
all outbound email are replaced with no-ops/recorders in setUp, so no real
network or SMTP call can happen from this test process regardless of what
.env/config.json this checkout happens to have on disk.

Run only against a database copy — see the TAOSCOUT_STRIPE_DB /
TAOSCOUT_USERS_DB env overrides set below, before either module is imported.
"""
import hashlib
import hmac
import json
import os
import re
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

from fastapi import FastAPI, HTTPException, Depends  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

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


def build_full_app():
    """Mounts BOTH taoscout_routes.py (registration/setup/login) and
    taoscout_stripe_routes.py (billing/webhook) onto a bare FastAPI() app —
    the same two exec() calls api.py makes, in the same order — so tests
    exercise the real route functions over real HTTP.

    start_payment_watcher/start_expiration_checker are replaced with no-ops:
    the real ones start a background thread that calls the Stripe/taostats
    APIs over the network the moment the (unused, in tests) startup event
    fires, which must never happen in a test process. PLANS/create_order/
    create_invite/etc. are inert stand-ins — this file doesn't exercise the
    TAO order/invite flows, only registration/login/setup and billing.
    """
    app = FastAPI()

    def verify_key(*args, **kwargs):
        return "test-key"

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


class BillingHTTPTestBase(unittest.TestCase):
    def setUp(self):
        self.db_path = _TEST_DIR / f"case_{self._testMethodName}.db"
        ta.DB_PATH = self.db_path
        ts.DB_PATH = self.db_path
        ts.STRIPE_WEBHOOK_SECRET = TEST_WEBHOOK_SECRET
        ts.STRIPE_ENABLED = True  # snapshot must be True BEFORE build_full_app() below
        ts.PRICE_PLANS = {
            "pro_monthly": {"price_id": "price_test_pro_monthly", "label": "Pro Monthly", "amount_usd": 2.99},
            "pro_annual": {"price_id": "price_test_pro_annual", "label": "Pro Annual", "amount_usd": 24.99},
        }
        ta.init_db()
        ts.init_db()

        # Mocked email, not a real SMTP call — regardless of what SMTP_PASS
        # this checkout's .env happens to have. Captured so tests can pull
        # the real setup token out of the real email body, exactly as a
        # user would click a real link.
        self.sent_emails = []

        def fake_send_email(to, subject, html, text=""):
            self.sent_emails.append({"to": to, "subject": subject, "html": html, "text": text})
            return True
        ta.send_email = fake_send_email

        self.client = TestClient(build_full_app())

    def make_tao_user_with_pin(self, email, roster_num, pin, role="free",
                                subscription_status="inactive", expires_at=None):
        """Simulates a user who already completed setup previously (has a
        PIN) but whose TAO subscription is inactive/expired — as opposed to
        register_via_http(), which is for a brand-new signup with no PIN yet."""
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("""
            INSERT INTO users (roster_num, email, role, status, subscription_status, expires_at, pin_hash)
            VALUES (?, ?, ?, 'active', ?, ?, ?)
        """, (roster_num, email, role, subscription_status, expires_at, ta.hash_pin(pin)))
        conn.commit()
        conn.close()

    def register_via_http(self, email):
        """Real POST /register, exactly as a browser would submit the form."""
        resp = self.client.post("/register", data={"email": email})
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp

    def extract_setup_token(self, email):
        matches = [e for e in self.sent_emails if e["to"] == email]
        self.assertTrue(matches, f"no setup email was sent to {email}")
        m = re.search(r"token=([\w-]+)", matches[-1]["html"])
        self.assertIsNotNone(m, "setup link not found in the sent email")
        return m.group(1)

    def complete_setup_via_http(self, token, pin="654321"):
        """Real POST /setup — creates the PIN and returns the response
        (which sets a session cookie on success, matching production)."""
        resp = self.client.post("/setup", data={"token": token, "pin": pin, "pin_confirm": pin},
                                 follow_redirects=False)
        return resp

    def login_via_http(self, email, pin):
        """Real POST /login — the thing under test in this file, not a
        bypass. Returns the response so callers can inspect status/cookies."""
        return self.client.post("/login", data={"email": email, "pin": pin}, follow_redirects=False)


class TestRealRegistrationAndLogin(BillingHTTPTestBase):
    """Requirement: a working Free/Stripe registration + login path, using
    the existing identity system, with NO TAO payment gate on login itself.
    verify_session() (TAO-gated dashboard/account) is untouched and still
    requires an active TAO subscription — checked separately below."""

    def test_brand_new_stripe_only_user_can_register_setup_and_log_in_over_http(self):
        email = "newstripeuser@example.com"
        self.register_via_http(email)
        token = self.extract_setup_token(email)

        setup_resp = self.complete_setup_via_http(token, pin="111222")
        self.assertEqual(setup_resp.status_code, 302)
        self.assertIn("session", setup_resp.cookies)

        # The real test: logging in again from scratch, via POST /login,
        # with no session/PIN bypass of any kind.
        login_resp = self.login_via_http(email, "111222")
        self.assertEqual(login_resp.status_code, 302, login_resp.text)
        self.assertEqual(login_resp.headers["location"], "/dashboard")
        self.assertIn("session", login_resp.cookies)

        # That session is real: verify_identity() accepts it immediately.
        session_token = login_resp.cookies["session"]
        identity = ta.verify_identity(session_token)
        self.assertEqual(identity["email"], email)

    def test_login_rejects_wrong_pin(self):
        email = "wrongpin@example.com"
        self.register_via_http(email)
        token = self.extract_setup_token(email)
        self.complete_setup_via_http(token, pin="999999")

        resp = self.login_via_http(email, "000000")
        self.assertEqual(resp.status_code, 200)  # re-renders the login form
        self.assertNotIn("session", resp.cookies)
        self.assertIn("Invalid email or PIN", resp.text)

    def test_active_stripe_customer_with_expired_tao_access_logs_in_over_http(self):
        # An existing user whose TAO subscription has lapsed — this is the
        # exact case the previous version of this test bypassed with
        # create_session(). Now driven through real POST /login.
        email = "expiredtao@example.com"
        self.make_tao_user_with_pin(email, "TS0002", pin="445566", role="operator",
                                     subscription_status="expired",
                                     expires_at="2020-01-01T00:00:00+00:00")

        login_resp = self.login_via_http(email, "445566")
        self.assertEqual(login_resp.status_code, 302, login_resp.text)
        self.assertEqual(login_resp.headers["location"], "/dashboard",
                          "login itself must succeed regardless of TAO subscription status")
        self.assertIn("session", login_resp.cookies)

        # Grant Stripe Pro, then confirm the SAME real-login session reaches
        # /billing/status — proving the whole chain: real login -> real
        # webhook -> real HTTP billing check, not a manually constructed session.
        session_token = login_resp.cookies["session"]
        ts.save_customer_id(email, "cus_et1")
        future = int(time.time()) + 30 * 86400
        ts.process_webhook_event(make_event(
            "evt_et1", "customer.subscription.created",
            make_subscription("sub_et1", "cus_et1", "active", future), created=1000,
        ))
        status_resp = self.client.get("/billing/status", cookies={"session": session_token})
        self.assertEqual(status_resp.status_code, 200, status_resp.text)
        self.assertTrue(status_resp.json()["pro"])

    def test_tao_endpoint_protection_is_preserved_for_a_free_stripe_only_account(self):
        """Product-specific TAO protection must be untouched: a real login
        for a Free/Stripe-only account (no active TAO subscription) must
        NOT pass verify_session()'s TAO gate, even though /login itself
        now succeeds for them."""
        email = "freeonly@example.com"
        self.register_via_http(email)
        token = self.extract_setup_token(email)
        self.complete_setup_via_http(token, pin="222333")

        login_resp = self.login_via_http(email, "222333")
        self.assertEqual(login_resp.status_code, 302)
        session_token = login_resp.cookies["session"]

        # verify_identity (Stripe/billing surfaces): succeeds.
        self.assertIsNotNone(ta.verify_identity(session_token))
        # verify_session (TAO-gated surfaces): still correctly denies,
        # because this account has no active TAO subscription.
        self.assertIsNone(ta.verify_session(session_token))

    def test_no_session_cookie_is_rejected_on_billing_status(self):
        resp = self.client.get("/billing/status")
        self.assertEqual(resp.status_code, 401)


class TestFullSignupCheckoutWebhookCancellationFlow(BillingHTTPTestBase):
    """Exercises signup/login -> checkout -> verified webhook -> Pro access
    -> cancellation/expiry end to end, over real HTTP.

    Precision about what is real vs. mocked, per review feedback:
      - Registration, PIN setup, and login are REAL HTTP calls
        (POST /register, POST /setup, POST /login) against the actual
        route code — not a manually created session.
      - The webhook SIGNATURE is genuinely verified (real HMAC computed
        locally, real stripe.Webhook.construct_event() call) against a
        test-mode-shaped secret — not mocked.
      - The Stripe CHECKOUT API CALL (checkout.Session.create) IS mocked
        (via ts.stripe_client), because no real Stripe test-mode
        credentials are available in this environment. This is not the
        same as an actual Stripe test-mode checkout against Stripe's own
        servers — that remains the next external validation step.
    """

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
        email = "flow@example.com"

        # 1. Real signup + real login (POST /register -> POST /setup -> POST /login).
        self.register_via_http(email)
        token = self.extract_setup_token(email)
        self.complete_setup_via_http(token, pin="135790")
        login_resp = self.login_via_http(email, "135790")
        self.assertEqual(login_resp.status_code, 302, login_resp.text)
        session = login_resp.cookies["session"]

        # Free before any purchase.
        resp = self.client.get("/billing/status", cookies={"session": session})
        self.assertFalse(resp.json()["pro"])

        # 2. Checkout: real HTTP call to /billing/checkout; Stripe's own
        # network call (checkout.Session.create) is mocked here — no real
        # Stripe test-mode credentials are available in this environment.
        fake_client = self._FakeStripeClient()
        with mock.patch.object(ts, "stripe_client", return_value=fake_client):
            resp = self.client.post("/billing/checkout", data={"plan": "pro_monthly"},
                                     cookies={"session": session}, follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "https://checkout.stripe.com/test-session")
        self.assertEqual(fake_client.checkout.Session.last_kwargs["client_reference_id"], email)

        # Still free: checkout redirecting is not proof of payment.
        resp = self.client.get("/billing/status", cookies={"session": session})
        self.assertFalse(resp.json()["pro"])

        # 3. Verified webhook — this IS a real signature check (real HMAC,
        # real stripe.Webhook.construct_event()), not mocked. Stripe links
        # the customer via checkout.session.completed...
        checkout_obj = {"customer": "cus_flow_1", "client_reference_id": email}
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

        # 4. Pro access, over HTTP, on the session created by the real login above.
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
