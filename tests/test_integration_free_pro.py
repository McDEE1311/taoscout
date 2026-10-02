#!/usr/bin/env python3
"""
Integration test for PR #1 (market research) + PR #2 (Stripe entitlements)
together, on the `integration-test-only` branch (pushed as its own reviewable
PR — see its description for why: it must be reviewable, not exist only as a
local artifact — but neither PR #1 nor PR #2 needs to merge into dev for this
branch to exist or for these tests to run).

This does NOT boot the full production api.py (it imports daily_dashboard,
scout, bittensor, etc. — heavy, unrelated to entitlement correctness, and
installing bittensor into a venv just for this check isn't warranted). It
mounts a bare FastAPI() app the same way api.py does for the pieces that
matter here — taoscout_routes.py (registration/setup/login),
taoscout_stripe_routes.py (billing/webhook), and the market app — and, to
guarantee the market-mount wiring under test is the REAL code and not a
re-transcription that could drift, extracts and execs the exact "Optional
public research preview" block verbatim out of api.py between two anchor
lines, rather than reimplementing it.

Precision about real vs. mocked, per review feedback:
  - Registration, PIN setup, and login are REAL HTTP calls (POST /register,
    POST /setup, POST /login) against the actual route code — not a
    manually created session via taoscout_auth.create_session().
  - The webhook SIGNATURE is genuinely verified (real HMAC, real
    stripe.Webhook.construct_event()) against a test-mode-shaped secret.
  - Outbound email and the payment-watcher/expiration-checker background
    threads are replaced with no-ops/recorders — no real network or SMTP
    call happens from this test process.
  - The Stripe CHECKOUT API CALL itself is not exercised here (that's
    covered in tests/test_billing_http.py with a mocked stripe_client);
    this file focuses on what a verified webhook unlocks on /market.
  - A real Stripe test-mode checkout against Stripe's own servers is not
    performed anywhere in this repo — no test-mode credentials are
    available in this environment. That remains the next external
    validation step.

Covers what was asked for:
  - real Free vs Pro behavior on /market/api/history through actual HTTP
    calls, driven by a real registration+login (not an injected session)
    and a real (signature-verified) Stripe webhook;
  - an explicit check of /market's public routes for paid-data bypasses.
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
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

_TEST_DB = Path(tempfile.mkdtemp(prefix="taoscout-integration-test-")) / "taoscout_users_test.db"
os.environ["TAOSCOUT_STRIPE_DB"] = str(_TEST_DB)
os.environ["TAOSCOUT_USERS_DB"] = str(_TEST_DB)
os.environ["TAOSCOUT_ACCESS_DB"] = str(_TEST_DB)
os.environ["STRIPE_SECRET_KEY"] = "sk_test_integration_only"
os.environ["STRIPE_WEBHOOK_SECRET"] = "whsec_integration_test_secret"

import taoscout_auth as ta  # noqa: E402
import taoscout_stripe as ts  # noqa: E402
import taoscout_access as tacc  # noqa: E402
from market.ledger import publish  # noqa: E402
from market.engine import Candle, HOUR  # noqa: E402

from fastapi import FastAPI, HTTPException, Depends, Request  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

TEST_WEBHOOK_SECRET = "whsec_integration_test_secret"
TEST_ADMIN_KEY = "test-admin-key-for-automated-tests-only"


def sign_payload(payload_bytes: bytes, secret: str, timestamp: int = None) -> str:
    timestamp = timestamp or int(time.time())
    signed_payload = f"{timestamp}.{payload_bytes.decode()}".encode()
    sig = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={sig}"


def make_event(event_id, event_type, obj, created=None):
    return {"id": event_id, "type": event_type,
            "created": created if created is not None else int(time.time()),
            "data": {"object": obj}}


def make_subscription(sub_id, customer_id, status, period_end, price_id="price_test_pro_monthly"):
    return {"id": sub_id, "customer": customer_id, "status": status,
            "current_period_end": period_end, "cancel_at_period_end": False,
            "items": {"data": [{"price": {"id": price_id}}]}}


def build_integration_app(ledger_path):
    """Mounts taoscout_routes.py (registration/setup/login),
    taoscout_stripe_routes.py (billing/webhook), and the real market app —
    with /market's Free/Pro gate wired exactly the way api.py does,
    extracted verbatim from api.py's own source between two anchor lines,
    so this test exercises the actual production wiring rather than a
    hand-copied approximation of it."""
    app = FastAPI()

    def verify_key(request: Request):
        # Matches the real verify_key's BEHAVIOR, not just its signature:
        # requires a specific key, rejects missing/wrong ones with 401 —
        # same as api.py's own verify_key checking against config.json's
        # api_keys list. A stub that accepted everything would make the
        # "missing/wrong keys can't grant owner access" tests meaningless.
        if request.headers.get("X-API-Key", "") != TEST_ADMIN_KEY:
            raise HTTPException(status_code=401, detail="Invalid or missing API key")
        return TEST_ADMIN_KEY

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

    ns.update({
        "grant_owner_access": tacc.grant_owner_access,
        "revoke_access": tacc.revoke_access,
        "get_access": tacc.get_access,
        "has_full_access": tacc.has_full_access,
        "create_access_invite": tacc.create_access_invite,
        "revoke_access_invite": tacc.revoke_access_invite,
        "redeem_access_invite": tacc.redeem_access_invite,
        "get_conn": ta.get_conn,
    })
    exec(compile((BASE_DIR / "taoscout_access_routes.py").read_text(), "taoscout_access_routes.py", "exec"), ns)

    api_source = (BASE_DIR / "api.py").read_text()
    start = api_source.index("# Optional public research preview.")
    # Stop at the Stripe Billing block's own comment, NOT at
    # if __name__: the Stripe block now sits AFTER the market-mount
    # block in api.py (both before if __name__), and this harness
    # already exec's taoscout_stripe_routes.py separately above — ending
    # at if __name__ would re-exec that same block a second time.
    end = api_source.index("# ── TaoScout Stripe Billing Routes")
    market_wiring_source = api_source[start:end]
    ns["CFG"] = {
        "market_research_enabled": True,
        "market_ledger_path": str(ledger_path),
        "market_report_path": None,
    }
    exec(compile(market_wiring_source, "api.py (extracted market-mount block)", "exec"), ns)
    return app


def candles(n=240, base_start=None):
    base_start = base_start or datetime(2025, 1, 1, tzinfo=timezone.utc)
    return [Candle(base_start + i * HOUR, base_start + (i + 1) * HOUR, 100 + i, 102 + i, 98 + i, 101 + i)
            for i in range(n)]


class IntegrationTestBase(unittest.TestCase):
    def setUp(self):
        self.db_path = Path(tempfile.mkdtemp()) / "case.db"
        ta.DB_PATH = self.db_path
        ts.DB_PATH = self.db_path
        tacc.DB_PATH = self.db_path
        ts.STRIPE_ENABLED = True  # snapshot must be True before build_integration_app() below
        ts.PRICE_PLANS = {
            "pro_monthly": {"price_id": "price_test_pro_monthly", "label": "Pro Monthly", "amount_usd": 2.99},
            "pro_annual": {"price_id": "price_test_pro_annual", "label": "Pro Annual", "amount_usd": 24.99},
        }
        ta.init_db()
        ts.init_db()
        tacc.init_db()

        self.sent_emails = []

        def fake_send_email(to, subject, html, text=""):
            self.sent_emails.append({"to": to, "subject": subject, "html": html, "text": text})
            return True
        ta.send_email = fake_send_email

        self.ledger_path = Path(tempfile.mkdtemp()) / "market.db"
        base = datetime(2025, 1, 1, tzinfo=timezone.utc)
        bars = candles(base_start=base)
        for i in range(3):
            publish(self.ledger_path, bars, "synthetic", base + 240 * HOUR + i * 8 * HOUR)

        # https base_url, not TestClient's default of http://testserver: the
        # real session cookie is set with secure=True, so a plain-http
        # client would silently receive-but-never-resend it across a
        # followed redirect or a later request on the same client, masking
        # exactly the navigation flow under test here.
        self.client = TestClient(build_integration_app(self.ledger_path), base_url="https://testserver")

    def register_and_login_via_http(self, email, pin="482913"):
        """Real POST /register -> extract the setup token from a mocked
        (never-sent) email -> real POST /setup -> real POST /login,
        following every redirect. No cookie is ever read off a response and
        passed back manually — self.client's own cookie jar carries the
        session for every request after this returns."""
        resp = self.client.post("/register", data={"email": email})
        self.assertEqual(resp.status_code, 200, resp.text)
        matches = [e for e in self.sent_emails if e["to"] == email]
        self.assertTrue(matches, f"no setup email was sent to {email}")
        m = re.search(r"token=([\w-]+)", matches[-1]["html"])
        self.assertIsNotNone(m)
        token = m.group(1)

        self.client.post("/setup", data={"token": token, "pin": pin, "pin_confirm": pin}, follow_redirects=True)
        login_resp = self.client.post("/login", data={"email": email, "pin": pin}, follow_redirects=True)
        self.assertEqual(login_resp.status_code, 200, login_resp.text)
        self.assertTrue(str(login_resp.url).endswith("/account"), login_resp.url)

    def make_expired_tao_user_with_pin_and_login(self, email, roster_num, pin):
        """An existing user who already has a PIN but whose TAO subscription
        has lapsed, then a real POST /login on self.client (not an injected
        session) — for the expired-TAO/active-Stripe scenario, where
        registration (a brand-new-account flow) doesn't apply."""
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("""
            INSERT INTO users (roster_num, email, role, status, subscription_status, expires_at, pin_hash)
            VALUES (?, ?, 'operator', 'active', 'expired', '2020-01-01T00:00:00+00:00', ?)
        """, (roster_num, email, ta.hash_pin(pin)))
        conn.commit()
        conn.close()
        login_resp = self.client.post("/login", data={"email": email, "pin": pin}, follow_redirects=True)
        self.assertEqual(login_resp.status_code, 200, login_resp.text)
        self.assertTrue(str(login_resp.url).endswith("/account"), login_resp.url)


class TestRealFreeVsProOnMarketEndpoint(IntegrationTestBase):
    def test_unauthenticated_caller_gets_delayed_free_tier(self):
        resp = self.client.get("/market/api/history")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["delayed"])
        self.assertEqual(len(data["records"]), 2, "unauthenticated = Free = newest slot withheld")

    def test_logged_in_without_stripe_pro_still_gets_delayed_free_tier(self):
        self.register_and_login_via_http("nopro@example.com")
        resp = self.client.get("/market/api/history")
        self.assertTrue(resp.json()["delayed"])
        self.assertEqual(len(resp.json()["records"]), 2)

    def test_real_signup_login_webhook_grants_current_market_data(self):
        """End-to-end: real registration+login -> verified Stripe webhook ->
        the SAME client's session (carried entirely by its own cookie jar)
        now sees the full, undelayed /market/api/history — proving the real
        production wiring (verify_identity + has_pro), not a
        reimplementation, and not a manually injected session."""
        self.register_and_login_via_http("promarket@example.com")

        # Before purchase: delayed, same as any Free caller.
        resp = self.client.get("/market/api/history")
        self.assertTrue(resp.json()["delayed"])

        # Verified webhook grants Stripe Pro. (The checkout API call itself
        # — Stripe's checkout.Session.create — is covered with a mocked
        # stripe_client in tests/test_billing_http.py; here the focus is
        # what a verified webhook unlocks on the market endpoint.)
        ts.save_customer_id("promarket@example.com", "cus_intmarket")
        future = int(time.time()) + 30 * 86400
        sub = make_subscription("sub_intmarket", "cus_intmarket", "active", future)
        payload = json.dumps(make_event("evt_intmarket", "customer.subscription.created", sub, created=1000)).encode()
        header = sign_payload(payload, TEST_WEBHOOK_SECRET)
        resp = self.client.post("/stripe/webhook", content=payload, headers={"stripe-signature": header})
        self.assertEqual(resp.status_code, 200, resp.text)

        # Same client, now Pro: full, undelayed history.
        resp = self.client.get("/market/api/history")
        self.assertFalse(resp.json()["delayed"])
        self.assertEqual(len(resp.json()["records"]), 3)

        # Cancellation revokes it — back to delayed Free on the market endpoint.
        canceled = make_subscription("sub_intmarket", "cus_intmarket", "canceled", future)
        payload = json.dumps(make_event("evt_intmarket_cancel", "customer.subscription.deleted",
                                         canceled, created=2000)).encode()
        header = sign_payload(payload, TEST_WEBHOOK_SECRET)
        self.client.post("/stripe/webhook", content=payload, headers={"stripe-signature": header})
        resp = self.client.get("/market/api/history")
        self.assertTrue(resp.json()["delayed"])
        self.assertEqual(len(resp.json()["records"]), 2)

    def test_a_real_expired_tao_login_with_active_stripe_pro_gets_current_market_data(self):
        # A user who really logs in via POST /login (not an injected
        # session) with a lapsed TAO subscription, but a valid Stripe Pro
        # subscription, must still get current data. This is the exact
        # identity-vs-TAO-validity bug this review flagged, checked here at
        # the real /market endpoint via a real login.
        self.make_expired_tao_user_with_pin_and_login("expiredtaomarket@example.com", "TS0102", "778899")
        ts.save_customer_id("expiredtaomarket@example.com", "cus_etm")
        future = int(time.time()) + 30 * 86400
        sub = make_subscription("sub_etm", "cus_etm", "active", future)
        payload = json.dumps(make_event("evt_etm", "customer.subscription.created", sub, created=1000)).encode()
        header = sign_payload(payload, TEST_WEBHOOK_SECRET)
        self.client.post("/stripe/webhook", content=payload, headers={"stripe-signature": header})

        resp = self.client.get("/market/api/history")
        self.assertFalse(resp.json()["delayed"])
        self.assertEqual(len(resp.json()["records"]), 3)


class TestPaidDataBypassAudit(IntegrationTestBase):
    """Explicit audit: with Free/Pro gating added to /api/history, are there
    other public routes on the mounted market app that still leak the
    withheld "current" data to an unauthenticated/Free caller?"""

    def test_simulation_route_carries_no_current_forward_research_data(self):
        resp = self.client.get("/market/api/simulation")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body.get("status"), "not_available",
                          "no simulation report was configured in this test; a real one would be a "
                          "static 'historical_simulation' record type, never containing forward "
                          "research records (see market/engine.py's replay() vs. the ledger's publish())")

    def test_forged_high_before_cursor_cannot_reveal_current_data_through_the_real_mount(self):
        self.register_and_login_via_http("forger@example.com")
        for forged_before in (10**9, 2**31):
            resp = self.client.get("/market/api/history", params={"before": forged_before})
            self.assertEqual(len(resp.json()["records"]), 2,
                              f"before={forged_before} must not bypass the Free-tier redaction through the real mount")

    def test_no_other_route_returns_the_withheld_newest_record(self):
        free_ids = {r["id"] for r in self.client.get("/market/api/history").json()["records"]}
        for path in ["/market/config.json", "/market/data", "/market/research.db",
                     "/market/../data/market-research.db"]:
            resp = self.client.get(path)
            self.assertIn(resp.status_code, (404, 307), f"{path} should not be reachable")
        from market.ledger import history
        full = history(self.ledger_path, limit=10)
        self.assertEqual(len(full["records"]), 3)
        self.assertNotIn(full["records"][0]["id"], free_ids)


class TestCustomerNavigationOnCombinedApp(IntegrationTestBase):
    """The same four-persona navigation check as PR #2's
    tests/test_billing_http.py, but against the fully combined app (TAO +
    Stripe + the real /market mount together) — proving /account, /billing,
    /dashboard, and /market/api/history all agree with each other once both
    PRs are integrated, not just in isolation."""

    def test_free_user_navigates_account_and_billing_but_not_dashboard_or_current_market_data(self):
        self.register_and_login_via_http("navfree@example.com")
        account_resp = self.client.get("/account")
        self.assertIn("No active TAO plan", account_resp.text)

        billing_resp = self.client.get("/billing")
        self.assertIn("Upgrade", billing_resp.text)

        dash_resp = self.client.get("/dashboard")
        self.assertNotIn("TAOSCOUT_USER", dash_resp.text)

        market_resp = self.client.get("/market/api/history")
        self.assertTrue(market_resp.json()["delayed"])

    def test_tao_only_user_navigates_to_dashboard_and_sees_free_billing_and_delayed_market(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("""
            INSERT INTO users (roster_num, email, role, status, subscription_status, expires_at, pin_hash)
            VALUES ('TS0300', 'navtao@example.com', 'operator', 'active', 'active', '2099-01-01T00:00:00+00:00', ?)
        """, (ta.hash_pin("606060"),))
        conn.commit()
        conn.close()
        login_resp = self.client.post("/login", data={"email": "navtao@example.com", "pin": "606060"},
                                       follow_redirects=True)
        self.assertTrue(str(login_resp.url).endswith("/account"), login_resp.url)
        self.assertIn("Go to Dashboard", login_resp.text)

        dash_resp = self.client.get("/dashboard")
        self.assertEqual(dash_resp.status_code, 200)
        self.assertIn("TAOSCOUT_USER", dash_resp.text, "existing TAO dashboard access must be preserved")

        billing_resp = self.client.get("/billing")
        self.assertIn("Upgrade", billing_resp.text, "TAO access must not imply Stripe Pro")

        market_resp = self.client.get("/market/api/history")
        self.assertTrue(market_resp.json()["delayed"], "TAO access must not imply Stripe-funded Pro market data")

    def test_stripe_only_user_navigates_to_billing_and_current_market_but_not_dashboard(self):
        self.register_and_login_via_http("navstripeonly@example.com")
        ts.save_customer_id("navstripeonly@example.com", "cus_navonly")
        future = int(time.time()) + 30 * 86400
        sub = make_subscription("sub_navonly", "cus_navonly", "active", future)
        payload = json.dumps(make_event("evt_navonly", "customer.subscription.created", sub, created=1000)).encode()
        header = sign_payload(payload, TEST_WEBHOOK_SECRET)
        self.client.post("/stripe/webhook", content=payload, headers={"stripe-signature": header})

        billing_resp = self.client.get("/billing")
        self.assertIn("Manage Billing", billing_resp.text)

        market_resp = self.client.get("/market/api/history")
        self.assertFalse(market_resp.json()["delayed"])

        dash_resp = self.client.get("/dashboard")
        self.assertNotIn("TAOSCOUT_USER", dash_resp.text, "Stripe Pro must not unlock the TAO dashboard")

    def test_expired_tao_user_keeps_account_and_billing_navigation_but_loses_dashboard(self):
        self.make_expired_tao_user_with_pin_and_login("navexpired@example.com", "TS0301", "707070")
        self.assertIn("No active TAO plan", self.client.get("/account").text)

        dash_resp = self.client.get("/dashboard")
        self.assertNotIn("TAOSCOUT_USER", dash_resp.text)

        billing_resp = self.client.get("/billing")
        self.assertEqual(billing_resp.status_code, 200)
        self.assertIn("Upgrade", billing_resp.text)


class TestOwnerComplimentaryFreeAndPaidOnCombinedApp(IntegrationTestBase):
    """The four account types side by side, in the real combined app:
    owner, complimentary (via a real /claim redemption), ordinary Free,
    and paid (both TAO and Stripe). Confirms /dashboard and /market agree
    with each account type's actual access, and that owner/complimentary
    access is independent of — and survives — whatever TAO/Stripe do."""

    def claim_and_login_via_http(self, token, email, pin="864213"):
        resp = self.client.post("/claim", data={"token": token, "email": email})
        self.assertEqual(resp.status_code, 200, resp.text)
        matches = [e for e in self.sent_emails if e["to"] == email]
        self.assertTrue(matches, f"no setup email was sent to {email}")
        m = re.search(r"token=([\w-]+)", matches[-1]["html"])
        setup_token = m.group(1)
        self.client.post("/setup", data={"token": setup_token, "pin": pin, "pin_confirm": pin}, follow_redirects=True)
        login_resp = self.client.post("/login", data={"email": email, "pin": pin}, follow_redirects=True)
        self.assertTrue(str(login_resp.url).endswith("/account"), login_resp.url)

    def test_owner_account_gets_full_dashboard_and_current_market_with_no_tao_or_stripe(self):
        email = "owner-combined@example.com"
        self.register_and_login_via_http(email)  # plain Free identity first
        tacc.grant_owner_access(email, granted_by="admin_key:test")  # the only way owner access is ever created

        dash_resp = self.client.get("/dashboard")
        self.assertIn("TAOSCOUT_USER", dash_resp.text, "owner access must unlock the real dashboard")
        market_resp = self.client.get("/market/api/history")
        self.assertFalse(market_resp.json()["delayed"], "owner access must unlock current research")

        account_resp = self.client.get("/account")
        self.assertIn("Owner Access", account_resp.text)

    def test_complimentary_invite_redemption_grants_the_same_access_as_owner_but_is_distinct(self):
        invite = tacc.create_access_invite("beta-tester", access_duration_days=30)
        email = "complimentary-combined@example.com"
        self.claim_and_login_via_http(invite["token"], email)

        dash_resp = self.client.get("/dashboard")
        self.assertIn("TAOSCOUT_USER", dash_resp.text)
        market_resp = self.client.get("/market/api/history")
        self.assertFalse(market_resp.json()["delayed"])

        access = tacc.get_access(email)
        self.assertTrue(access["complimentary"])
        self.assertFalse(access["owner"], "an invite must never grant owner-level access")

    def test_ordinary_free_account_gets_neither_dashboard_nor_current_market(self):
        email = "plainfree-combined@example.com"
        self.register_and_login_via_http(email)
        dash_resp = self.client.get("/dashboard")
        self.assertNotIn("TAOSCOUT_USER", dash_resp.text)
        market_resp = self.client.get("/market/api/history")
        self.assertTrue(market_resp.json()["delayed"])
        self.assertFalse(tacc.get_access(email)["active"])

    def test_tao_paid_account_unaffected_by_and_distinct_from_owner_complimentary(self):
        email = "taopaid-combined@example.com"
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("""
            INSERT INTO users (roster_num, email, role, status, subscription_status, expires_at, pin_hash)
            VALUES ('TSPAID1', ?, 'operator', 'active', 'active', datetime('now', '+30 days'), ?)
        """, (email, ta.hash_pin("135791")))
        conn.commit()
        conn.close()
        self.client.post("/login", data={"email": email, "pin": "135791"}, follow_redirects=True)

        dash_resp = self.client.get("/dashboard")
        self.assertIn("TAOSCOUT_USER", dash_resp.text, "existing TAO access must keep working unchanged")
        self.assertFalse(tacc.get_access(email)["active"], "a TAO payment must not create an owner/complimentary grant")

    def test_stripe_paid_account_unaffected_by_and_distinct_from_owner_complimentary(self):
        email = "stripepaid-combined@example.com"
        self.register_and_login_via_http(email)
        ts.save_customer_id(email, "cus_combined_paid")
        future = int(time.time()) + 30 * 86400
        sub = make_subscription("sub_combined_paid", "cus_combined_paid", "active", future)
        payload = json.dumps(make_event("evt_combined_paid", "customer.subscription.created", sub, created=1000)).encode()
        header = sign_payload(payload, TEST_WEBHOOK_SECRET)
        self.client.post("/stripe/webhook", content=payload, headers={"stripe-signature": header})

        market_resp = self.client.get("/market/api/history")
        self.assertFalse(market_resp.json()["delayed"], "existing Stripe Pro access must keep working unchanged")
        dash_resp = self.client.get("/dashboard")
        self.assertNotIn("TAOSCOUT_USER", dash_resp.text, "Stripe Pro still must not unlock the TAO dashboard")
        self.assertFalse(tacc.get_access(email)["active"], "a Stripe subscription must not create an owner/complimentary grant")

    def test_revoking_owner_access_immediately_locks_dashboard_and_market_again(self):
        email = "revoke-combined@example.com"
        self.register_and_login_via_http(email)
        tacc.grant_owner_access(email)
        self.assertIn("TAOSCOUT_USER", self.client.get("/dashboard").text)

        tacc.revoke_access(email, "owner")
        dash_resp = self.client.get("/dashboard")
        self.assertNotIn("TAOSCOUT_USER", dash_resp.text, "revoked owner access must lock the dashboard immediately")
        self.assertTrue(self.client.get("/market/api/history").json()["delayed"])

    def test_grant_owner_access_route_requires_the_admin_key(self):
        resp = self.client.post("/admin/access/grant-owner", json={"email": "adminroute@example.com"},
                                 headers={"X-API-Key": TEST_ADMIN_KEY})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(tacc.get_access("adminroute@example.com")["owner"])

    def test_admin_access_routes_reject_missing_wrong_and_session_only_auth(self):
        """The admin API-key gate, not a session cookie, is what protects
        owner/invite creation — missing key, wrong key, and a logged-in
        ordinary user's own session must all be rejected the same way."""
        admin_calls = [
            ("POST", "/admin/access/grant-owner", {"email": "attacker@example.com"}),
            ("POST", "/admin/access/invite/create", {"label": "attacker-invite"}),
            ("POST", "/admin/access/invite/revoke", {"token": "whatever"}),
            ("POST", "/admin/access/revoke", {"email": "attacker@example.com"}),
            ("GET", "/admin/access/grants", None),
            ("GET", "/admin/access/invites", None),
        ]

        def call(method, path, body, headers=None):
            if method == "GET":
                return self.client.get(path, headers=headers or {})
            return self.client.post(path, json=body, headers=headers or {})

        # No X-API-Key header at all.
        for method, path, body in admin_calls:
            resp = call(method, path, body)
            self.assertEqual(resp.status_code, 401, f"{method} {path} with no key should be 401")

        # Wrong X-API-Key.
        for method, path, body in admin_calls:
            resp = call(method, path, body, {"X-API-Key": "definitely-not-the-admin-key"})
            self.assertEqual(resp.status_code, 401, f"{method} {path} with a wrong key should be 401")

        # A real, logged-in ordinary user's own session cookie — session
        # auth is for identity, never a substitute for the admin API key.
        self.register_and_login_via_http("ordinaryuser@example.com")
        for method, path, body in admin_calls:
            resp = call(method, path, body)  # self.client now carries a valid session cookie
            self.assertEqual(resp.status_code, 401,
                              f"{method} {path}: a valid user session must not satisfy the admin key gate")

        # Confirm none of the above actually granted or created anything.
        self.assertFalse(tacc.get_access("attacker@example.com")["active"])
        conn = sqlite3.connect(str(self.db_path))
        count = conn.execute("SELECT COUNT(*) FROM access_invites WHERE label='attacker-invite'").fetchone()[0]
        conn.close()
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
