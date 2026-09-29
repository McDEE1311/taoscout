#!/usr/bin/env python3
"""
Tests for taoscout_stripe.py — run only against a database COPY, never the
live file at data/taoscout_users.db.

Before taoscout_stripe is ever imported, TAOSCOUT_STRIPE_DB is pointed at a
temp copy of the current production database (if one exists locally) so
taoscout_auth.py's import-time init_db() (which does run unconditionally,
same as it always has) cannot write to production. This is also the "test
migrations on a database copy" step: init_db()'s CREATE TABLE IF NOT EXISTS
is applied to that copy, and we assert the pre-existing users/orders/sessions
rows are byte-for-byte unchanged afterward.

taoscout_stripe.py's own init_db() only runs at import time when
STRIPE_ENABLED is already true (see TestDisabledStripeHasNoSideEffects
below, which verifies this in a fresh subprocess — STRIPE_ENABLED is fixed
at import time, so it can't be re-tested within this already-imported
process).
"""
import hashlib
import hmac
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

_TEST_DIR = Path(tempfile.mkdtemp(prefix="taoscout-stripe-test-"))
_TEST_DB = _TEST_DIR / "taoscout_users_test.db"
_LIVE_DB = BASE_DIR / "data" / "taoscout_users.db"
if _LIVE_DB.exists():
    shutil.copy2(_LIVE_DB, _TEST_DB)
# Both modules' init_db() runs unconditionally at import time; redirecting
# both env vars *before* either import means neither one can ever create or
# touch a file at the real default path (data/taoscout_users.db).
os.environ["TAOSCOUT_STRIPE_DB"] = str(_TEST_DB)
os.environ["TAOSCOUT_USERS_DB"] = str(_TEST_DB)

import taoscout_stripe as ts  # noqa: E402  (import must follow the env overrides above)
import taoscout_auth as ta  # noqa: E402  (only used for the cross-isolation test)

TEST_WEBHOOK_SECRET = "whsec_test_secret_for_unit_tests_only"


def sign_payload(payload_bytes: bytes, secret: str, timestamp: int = None) -> str:
    """Builds a Stripe-format `Stripe-Signature` header locally (no network
    call), per Stripe's publicly documented signing scheme."""
    timestamp = timestamp or int(time.time())
    signed_payload = f"{timestamp}.{payload_bytes.decode()}".encode()
    sig = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={sig}"


def make_event(event_id, event_type, obj, created=None):
    return {
        "id": event_id,
        "type": event_type,
        "created": created if created is not None else int(time.time()),
        "data": {"object": obj},
    }


def make_subscription(sub_id, customer_id, status, period_end, price_id="price_test_pro_monthly",
                       cancel_at_period_end=False):
    return {
        "id": sub_id,
        "customer": customer_id,
        "status": status,
        "current_period_end": period_end,
        "cancel_at_period_end": cancel_at_period_end,
        "items": {"data": [{"price": {"id": price_id}}]},
    }


def dump_table(conn, table):
    return [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()]


class StripeIsolationSetup(unittest.TestCase):
    """Every test gets a fresh copy of the (copied-at-module-load) test DB,
    so tests cannot interfere with each other or with production."""

    def setUp(self):
        self.db_path = _TEST_DIR / f"case_{self._testMethodName}.db"
        if _TEST_DB.exists():
            shutil.copy2(_TEST_DB, self.db_path)
        ts.DB_PATH = self.db_path
        ta.DB_PATH = self.db_path
        ts.STRIPE_WEBHOOK_SECRET = TEST_WEBHOOK_SECRET
        ts.PRICE_PLANS = {
            "pro_monthly": {"price_id": "price_test_pro_monthly", "label": "Pro Monthly", "amount_usd": 2.99},
            "pro_annual": {"price_id": "price_test_pro_annual", "label": "Pro Annual", "amount_usd": 24.99},
        }
        ts.init_db()
        ta.init_db()


class TestMigrationSafety(StripeIsolationSetup):
    def test_migration_is_additive_and_preserves_existing_rows(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        before = {t: dump_table(conn, t) for t in ("users", "orders", "sessions")}
        conn.close()

        ts.init_db()  # re-running the migration must be a safe no-op
        ta.init_db()

        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        after = {t: dump_table(conn, t) for t in ("users", "orders", "sessions")}
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()

        self.assertEqual(before, after, "TAO users/orders/sessions rows changed after Stripe migration")
        self.assertIn("stripe_customers", tables)
        self.assertIn("stripe_entitlements", tables)
        self.assertIn("stripe_webhook_events", tables)

    def test_fresh_disaster_recovery_bootstrap_supports_tao_worker_and_event_log(self):
        """init_db() must be able to bootstrap a brand-new, empty database
        (disaster recovery / new deploy) well enough for the TAO expiration
        worker and event logging to run without error. Regression test for a
        gap found while testing this migration on a copy: reminder_7d_sent/
        reminder_1d_sent and the user_events table existed only via ad hoc
        ALTER TABLEs against production and were missing from init_db()."""
        fresh_path = _TEST_DIR / f"fresh_{self._testMethodName}.db"
        if fresh_path.exists():
            fresh_path.unlink()
        ta.DB_PATH = fresh_path
        ts.DB_PATH = fresh_path
        ta.init_db()
        ts.init_db()

        ta.log_user_event("new@example.com", "login_success", path="/login")
        ta.check_expirations()  # must not raise OperationalError on a fresh schema

        conn = sqlite3.connect(str(fresh_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM user_events WHERE email='new@example.com'").fetchone()
        conn.close()
        self.assertEqual(row["event"], "login_success")


class TestWebhookSecurity(StripeIsolationSetup):
    def test_valid_signature_is_accepted(self):
        payload = json.dumps(make_event("evt_1", "customer.subscription.created",
                                         make_subscription("sub_1", "cus_1", "active", int(time.time()) + 3600))).encode()
        header = sign_payload(payload, TEST_WEBHOOK_SECRET)
        event = ts.verify_webhook(payload, header)
        self.assertEqual(event["id"], "evt_1")

    def test_invalid_signature_is_rejected(self):
        payload = json.dumps(make_event("evt_2", "customer.subscription.created", {})).encode()
        header = sign_payload(payload, "wrong_secret")
        with self.assertRaises(Exception):
            ts.verify_webhook(payload, header)

    def test_duplicate_event_id_is_ignored_on_second_delivery(self):
        sub = make_subscription("sub_dupcheck", "cus_dupcheck", "active", int(time.time()) + 3600)
        event = make_event("evt_dup", "customer.subscription.created", sub, created=1000)
        ts.save_customer_id("dupcheck@example.com", "cus_dupcheck")
        first = ts.process_webhook_event(event)
        second = ts.process_webhook_event(event)
        self.assertEqual(first, "applied")
        self.assertEqual(second, "duplicate_ignored")


class TestCheckoutDoesNotGrantAccess(StripeIsolationSetup):
    def test_checkout_completed_only_links_customer_no_entitlement(self):
        session_obj = {
            "customer": "cus_ck1",
            "client_reference_id": "buyer@example.com",
            "customer_details": {"email": "buyer@example.com"},
        }
        event = make_event("evt_checkout", "checkout.session.completed", session_obj)
        result = ts.process_webhook_event(event)
        self.assertEqual(result, "customer_linked")
        self.assertEqual(ts.get_customer_id("buyer@example.com"), "cus_ck1")
        # Checkout completing must never itself grant Pro access.
        self.assertFalse(ts.has_pro("buyer@example.com"))


class TestSubscriptionLifecycle(StripeIsolationSetup):
    def test_active_subscription_grants_pro(self):
        ts.save_customer_id("pro@example.com", "cus_active")
        future = int(time.time()) + 30 * 86400
        sub = make_subscription("sub_active", "cus_active", "active", future)
        ts.process_webhook_event(make_event("evt_a1", "customer.subscription.created", sub, created=1000))
        ent = ts.get_entitlement("pro@example.com")
        self.assertTrue(ent["pro"])
        self.assertEqual(ent["plan"], "pro_monthly")

    def test_cancellation_revokes_pro(self):
        ts.save_customer_id("cancel@example.com", "cus_cancel")
        future = int(time.time()) + 30 * 86400
        active_sub = make_subscription("sub_cancel", "cus_cancel", "active", future)
        ts.process_webhook_event(make_event("evt_c1", "customer.subscription.created", active_sub, created=1000))
        self.assertTrue(ts.has_pro("cancel@example.com"))

        canceled_sub = make_subscription("sub_cancel", "cus_cancel", "canceled", future)
        ts.process_webhook_event(make_event("evt_c2", "customer.subscription.deleted", canceled_sub, created=2000))
        self.assertFalse(ts.has_pro("cancel@example.com"))

    def test_out_of_order_event_does_not_regress_state(self):
        ts.save_customer_id("order@example.com", "cus_order")
        future = int(time.time()) + 30 * 86400
        active_sub = make_subscription("sub_order", "cus_order", "active", future)
        # Newer event (created=5000) applied first...
        ts.process_webhook_event(make_event("evt_o1", "customer.subscription.updated", active_sub, created=5000))
        self.assertTrue(ts.has_pro("order@example.com"))

        # ...then an older, out-of-order "canceled" event (created=1000) arrives late.
        stale_canceled = make_subscription("sub_order", "cus_order", "canceled", future)
        result = ts.process_webhook_event(make_event("evt_o2", "customer.subscription.deleted", stale_canceled, created=1000))
        self.assertEqual(result, "stale_ignored")
        self.assertTrue(ts.has_pro("order@example.com"), "an out-of-order event must not revoke a newer active state")

    def test_failure_between_dedupe_and_entitlement_update_is_fully_rolled_back_and_retryable(self):
        """Regression test: claim_webhook_event() used to commit its dedupe
        row BEFORE handle_event() ran, so a processing failure meant a
        genuine Stripe retry of the same event id was discarded as a
        duplicate and the entitlement change was permanently lost.
        process_webhook_event() must roll back everything — including the
        dedupe marker — on failure, so a retry of the same event fully
        reprocesses it."""
        ts.save_customer_id("retry@example.com", "cus_retry")
        future = int(time.time()) + 30 * 86400
        sub = make_subscription("sub_retry", "cus_retry", "active", future)
        event = make_event("evt_retry", "customer.subscription.created", sub, created=1000)

        with mock.patch.object(ts, "_upsert_subscription_tx", side_effect=RuntimeError("simulated DB failure")):
            with self.assertRaises(RuntimeError):
                ts.process_webhook_event(event)

        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        dedupe_row = conn.execute("SELECT 1 FROM stripe_webhook_events WHERE event_id='evt_retry'").fetchone()
        entitlement_row = conn.execute("SELECT 1 FROM stripe_entitlements WHERE stripe_subscription_id='sub_retry'").fetchone()
        conn.close()
        self.assertIsNone(dedupe_row, "a failed delivery must not be marked processed")
        self.assertIsNone(entitlement_row, "a failed delivery must not partially apply the entitlement")
        self.assertFalse(ts.has_pro("retry@example.com"))

        # Stripe retries the identical event after the transient failure clears.
        result = ts.process_webhook_event(event)
        self.assertEqual(result, "applied")
        self.assertTrue(ts.has_pro("retry@example.com"))

    def test_duplicate_webhook_delivery_is_idempotent(self):
        ts.save_customer_id("idem@example.com", "cus_idem")
        future = int(time.time()) + 30 * 86400
        sub = make_subscription("sub_idem", "cus_idem", "active", future)
        payload = json.dumps(make_event("evt_idem", "customer.subscription.created", sub, created=1000)).encode()
        header = sign_payload(payload, TEST_WEBHOOK_SECRET)

        for _ in range(3):
            event = ts.verify_webhook(payload, header)
            ts.process_webhook_event(event)

        conn = sqlite3.connect(str(self.db_path))
        count = conn.execute("SELECT COUNT(*) FROM stripe_entitlements WHERE stripe_subscription_id='sub_idem'").fetchone()[0]
        conn.close()
        self.assertEqual(count, 1)


class TestExpirationReconciliation(StripeIsolationSetup):
    def test_expired_period_end_is_reconciled_to_expired(self):
        ts.save_customer_id("expiring@example.com", "cus_exp")
        past = int(time.time()) - 3600
        sub = make_subscription("sub_exp", "cus_exp", "active", past)
        ts.process_webhook_event(make_event("evt_exp", "customer.subscription.created", sub, created=1000))
        # get_entitlement already treats a past current_period_end as not-pro...
        self.assertFalse(ts.has_pro("expiring@example.com"))
        # ...and the reconciler flips the stored status accordingly.
        ts.reconcile_expirations()
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT status FROM stripe_entitlements WHERE stripe_subscription_id='sub_exp'").fetchone()
        conn.close()
        self.assertEqual(row["status"], "expired")


class TestCrossSystemIsolation(StripeIsolationSetup):
    def test_stripe_operations_never_modify_users_orders_sessions(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        before = {t: dump_table(conn, t) for t in ("users", "orders", "sessions")}
        conn.close()

        ts.save_customer_id("iso@example.com", "cus_iso")
        future = int(time.time()) + 30 * 86400
        sub = make_subscription("sub_iso", "cus_iso", "active", future)
        ts.process_webhook_event(make_event("evt_iso1", "customer.subscription.created", sub, created=1000))
        canceled = make_subscription("sub_iso", "cus_iso", "canceled", future)
        ts.process_webhook_event(make_event("evt_iso2", "customer.subscription.deleted", canceled, created=2000))
        ts.reconcile_expirations()

        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        after = {t: dump_table(conn, t) for t in ("users", "orders", "sessions")}
        conn.close()
        self.assertEqual(before, after, "Stripe entitlement operations modified TAO tables")

    def test_tao_expiration_worker_never_modifies_stripe_tables(self):
        # Seed one Stripe-Pro entitlement, then run the TAO expiration worker.
        ts.save_customer_id("tao-iso@example.com", "cus_tao_iso")
        future = int(time.time()) + 30 * 86400
        sub = make_subscription("sub_tao_iso", "cus_tao_iso", "active", future)
        ts.process_webhook_event(make_event("evt_ti1", "customer.subscription.created", sub, created=1000))

        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        before = {t: dump_table(conn, t) for t in ("stripe_customers", "stripe_entitlements", "stripe_webhook_events")}
        conn.close()

        ta.check_expirations()  # the TAO worker: must not see/touch Stripe tables

        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        after = {t: dump_table(conn, t) for t in ("stripe_customers", "stripe_entitlements", "stripe_webhook_events")}
        conn.close()
        self.assertEqual(before, after, "TAO expiration worker modified Stripe entitlement tables")
        # And Pro access granted by Stripe must survive the TAO worker running.
        self.assertTrue(ts.has_pro("tao-iso@example.com"))

    def test_stripe_cancellation_does_not_touch_a_tao_paid_users_row(self):
        # A user who is both a TAO-paid operator AND (separately) a Stripe
        # Pro subscriber: canceling Stripe must not touch their TAO row.
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("""
            INSERT INTO users (roster_num, email, role, status, subscription_status, expires_at)
            VALUES ('TS9999', 'both@example.com', 'operator', 'active', 'active', datetime('now', '+30 days'))
        """)
        conn.commit()
        before_user = dump_table(conn, "users")
        conn.close()

        ts.save_customer_id("both@example.com", "cus_both")
        future = int(time.time()) + 30 * 86400
        sub = make_subscription("sub_both", "cus_both", "active", future)
        ts.process_webhook_event(make_event("evt_b1", "customer.subscription.created", sub, created=1000))
        canceled = make_subscription("sub_both", "cus_both", "canceled", future)
        ts.process_webhook_event(make_event("evt_b2", "customer.subscription.deleted", canceled, created=2000))

        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        after_user = dump_table(conn, "users")
        conn.close()
        self.assertEqual(before_user, after_user)
        self.assertFalse(ts.has_pro("both@example.com"))  # Stripe access correctly revoked...
        # ...while their TAO row (checked directly, bypassing Stripe entirely) is untouched.
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        tao_row = conn.execute("SELECT role, subscription_status FROM users WHERE email='both@example.com'").fetchone()
        conn.close()
        self.assertEqual(tao_row["role"], "operator")
        self.assertEqual(tao_row["subscription_status"], "active")


class TestCheckoutSessionCreation(StripeIsolationSetup):
    """Exercises create_checkout_session's local logic (validation, customer
    caching) with the Stripe SDK network calls stubbed out — no real network
    access or live Stripe keys are available in this environment."""

    class _FakeCheckoutSessions:
        def __init__(self):
            self.calls = []

        def create(self, **kwargs):
            self.calls.append(kwargs)
            return type("Obj", (), {"url": "https://checkout.stripe.com/test-session", "id": "cs_test_1"})

    class _FakeCustomers:
        def __init__(self):
            self.calls = []

        def create(self, **kwargs):
            self.calls.append(kwargs)
            return type("Obj", (), {"id": "cus_fake_new"})

    class _FakeCheckout:
        def __init__(self):
            self.Session = TestCheckoutSessionCreation._FakeCheckoutSessions()

    class _FakeStripeClient:
        def __init__(self):
            self.Customer = TestCheckoutSessionCreation._FakeCustomers()
            self.checkout = TestCheckoutSessionCreation._FakeCheckout()

    def test_unknown_plan_is_rejected_before_any_network_call(self):
        with self.assertRaises(ValueError):
            ts.create_checkout_session("x@example.com", "not_a_real_plan")

    def test_checkout_reuses_existing_customer_id(self):
        fake = self._FakeStripeClient()
        ts.save_customer_id("existing@example.com", "cus_existing_123")
        with mock.patch.object(ts, "stripe_client", return_value=fake):
            url = ts.create_checkout_session("existing@example.com", "pro_monthly")
        self.assertEqual(url, "https://checkout.stripe.com/test-session")
        self.assertEqual(len(fake.Customer.calls), 0, "should not create a new Stripe customer when one is cached")
        self.assertEqual(fake.checkout.Session.calls[0]["customer"], "cus_existing_123")


class TestDisabledStripeHasNoSideEffects(unittest.TestCase):
    """STRIPE_ENABLED is computed once at import time, so verifying its
    effect on import-time behavior requires a fresh interpreter per case
    rather than reusing the already-imported `ts` module in this process."""

    def _run(self, env_overrides, code):
        env = dict(os.environ)
        for k in ("STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET"):
            env.pop(k, None)
        env.update(env_overrides)
        env["PYTHONPATH"] = str(BASE_DIR)
        return subprocess.run([sys.executable, "-c", code], cwd=str(BASE_DIR),
                               env=env, capture_output=True, text=True)

    def test_unconfigured_stripe_creates_no_database_file_at_import(self):
        scratch = _TEST_DIR / "disabled_no_side_effects.db"
        result = self._run({"TAOSCOUT_STRIPE_DB": str(scratch)}, "import taoscout_stripe")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(scratch.exists(),
                          "importing taoscout_stripe with no keys configured must not create a database file")

    def test_live_looking_secret_key_never_enables_stripe(self):
        scratch = _TEST_DIR / "live_key_check.db"
        result = self._run({
            "STRIPE_SECRET_KEY": "sk_live_should_never_enable_anything",
            "STRIPE_WEBHOOK_SECRET": "whsec_whatever",
            "TAOSCOUT_STRIPE_DB": str(scratch),
        }, "import taoscout_stripe; print('ENABLED=', taoscout_stripe.STRIPE_ENABLED)")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ENABLED= False", result.stdout)
        self.assertFalse(scratch.exists(), "a live-looking key must not enable Stripe or touch the database")

    def test_test_mode_key_with_webhook_secret_enables_and_initializes_tables(self):
        scratch = _TEST_DIR / "test_mode_key_check.db"
        result = self._run({
            "STRIPE_SECRET_KEY": "sk_test_abc123",
            "STRIPE_WEBHOOK_SECRET": "whsec_abc123",
            "TAOSCOUT_STRIPE_DB": str(scratch),
        }, "import taoscout_stripe; print('ENABLED=', taoscout_stripe.STRIPE_ENABLED)")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ENABLED= True", result.stdout)
        self.assertTrue(scratch.exists(), "enabling Stripe (test-mode key + webhook secret) should create its tables")


if __name__ == "__main__":
    unittest.main()
