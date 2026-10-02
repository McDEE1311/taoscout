#!/usr/bin/env python3
"""
Tests for taoscout_access.py — owner/complimentary access, isolated from
both TAO (users.subscription_status/expires_at) and Stripe
(stripe_entitlements). Run only against a database copy.
"""
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

_TEST_DIR = Path(tempfile.mkdtemp(prefix="taoscout-access-test-"))
_TEST_DB = _TEST_DIR / "taoscout_users_test.db"
_LIVE_DB = BASE_DIR / "data" / "taoscout_users.db"
if _LIVE_DB.exists():
    shutil.copy2(_LIVE_DB, _TEST_DB)
os.environ["TAOSCOUT_USERS_DB"] = str(_TEST_DB)
os.environ["TAOSCOUT_ACCESS_DB"] = str(_TEST_DB)
os.environ["TAOSCOUT_STRIPE_DB"] = str(_TEST_DB)

import taoscout_auth as ta  # noqa: E402
import taoscout_access as tacc  # noqa: E402
import taoscout_stripe as ts  # noqa: E402


def dump_table(conn, table):
    return [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()]


class AccessTestBase(unittest.TestCase):
    def setUp(self):
        self.db_path = _TEST_DIR / f"case_{self._testMethodName}.db"
        if _TEST_DB.exists():
            shutil.copy2(_TEST_DB, self.db_path)
        ta.DB_PATH = self.db_path
        tacc.DB_PATH = self.db_path
        ts.DB_PATH = self.db_path
        ta.init_db()
        tacc.init_db()
        ts.init_db()


class TestOwnerGrant(AccessTestBase):
    def test_grant_owner_access_is_permanent_and_active(self):
        tacc.grant_owner_access("owner@example.com", granted_by="admin_key:test")
        access = tacc.get_access("owner@example.com")
        self.assertTrue(access["owner"])
        self.assertTrue(access["active"])
        self.assertIsNone(access["expires_at"])  # never expires

    def test_no_grant_means_no_access(self):
        self.assertFalse(tacc.has_full_access("nobody@example.com"))

    def test_revoking_owner_access_removes_it(self):
        tacc.grant_owner_access("owner2@example.com")
        self.assertTrue(tacc.has_full_access("owner2@example.com"))
        tacc.revoke_access("owner2@example.com", "owner")
        self.assertFalse(tacc.has_full_access("owner2@example.com"))

    def test_owner_access_is_never_created_by_registration_or_invite_redemption(self):
        """The one hard safety rule: owner access only ever comes from
        grant_owner_access(), called only from an admin route. Neither a
        plain Free registration nor a complimentary invite redemption can
        ever produce an 'owner' row."""
        ta.register_account("plainuser@example.com")
        invite = tacc.create_access_invite("test invite")
        tacc.redeem_access_invite(invite["token"], "invitee@example.com")

        conn = sqlite3.connect(str(self.db_path))
        owner_rows = conn.execute("SELECT * FROM access_grants WHERE grant_type='owner'").fetchall()
        conn.close()
        self.assertEqual(len(owner_rows), 0, "no public path may ever create an owner grant")
        self.assertFalse(tacc.get_access("plainuser@example.com")["owner"])
        self.assertFalse(tacc.get_access("invitee@example.com")["owner"])
        self.assertTrue(tacc.get_access("invitee@example.com")["complimentary"])


class TestComplimentaryInvites(AccessTestBase):
    def test_redeeming_a_valid_invite_grants_complimentary_access(self):
        invite = tacc.create_access_invite("beta tester", access_duration_days=30)
        result = tacc.redeem_access_invite(invite["token"], "beta@example.com")
        self.assertEqual(result["status"], "granted")
        access = tacc.get_access("beta@example.com")
        self.assertTrue(access["complimentary"])
        self.assertTrue(access["active"])
        self.assertIsNotNone(access["expires_at"])

    def test_invite_with_no_duration_grants_unlimited_access(self):
        invite = tacc.create_access_invite("permanent comp", access_duration_days=None)
        tacc.redeem_access_invite(invite["token"], "permanent@example.com")
        access = tacc.get_access("permanent@example.com")
        self.assertTrue(access["active"])
        self.assertIsNone(access["expires_at"])

    def test_invite_is_single_use_by_default(self):
        invite = tacc.create_access_invite("single use")
        first = tacc.redeem_access_invite(invite["token"], "first@example.com")
        second = tacc.redeem_access_invite(invite["token"], "second@example.com")
        self.assertEqual(first["status"], "granted")
        self.assertIn("error", second)
        self.assertFalse(tacc.get_access("second@example.com")["active"])

    def test_invite_respects_max_uses_greater_than_one(self):
        invite = tacc.create_access_invite("multi use", max_uses=2)
        r1 = tacc.redeem_access_invite(invite["token"], "a@example.com")
        r2 = tacc.redeem_access_invite(invite["token"], "b@example.com")
        r3 = tacc.redeem_access_invite(invite["token"], "c@example.com")
        self.assertEqual(r1["status"], "granted")
        self.assertEqual(r2["status"], "granted")
        self.assertIn("error", r3)

    def test_expired_invite_link_cannot_be_redeemed(self):
        invite = tacc.create_access_invite("expiring", invite_expires_hours=0)
        time.sleep(1.1)
        result = tacc.redeem_access_invite(invite["token"], "late@example.com")
        self.assertIn("error", result)

    def test_revoked_invite_cannot_be_redeemed(self):
        invite = tacc.create_access_invite("revocable")
        tacc.revoke_access_invite(invite["token"])
        result = tacc.redeem_access_invite(invite["token"], "toolate@example.com")
        self.assertIn("error", result)

    def test_revoking_granted_complimentary_access_removes_it(self):
        invite = tacc.create_access_invite("revoke test")
        tacc.redeem_access_invite(invite["token"], "revokeme@example.com")
        self.assertTrue(tacc.has_full_access("revokeme@example.com"))
        tacc.revoke_access("revokeme@example.com", "complimentary")
        self.assertFalse(tacc.has_full_access("revokeme@example.com"))


class TestIsolationFromTaoAndStripe(AccessTestBase):
    def test_access_grants_never_touch_users_orders_sessions(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        before = {t: dump_table(conn, t) for t in ("users", "orders", "sessions")}
        conn.close()

        tacc.grant_owner_access("iso-owner@example.com")
        invite = tacc.create_access_invite("iso test")
        tacc.redeem_access_invite(invite["token"], "iso-comp@example.com")
        tacc.revoke_access("iso-owner@example.com")

        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        after = {t: dump_table(conn, t) for t in ("users", "orders", "sessions")}
        conn.close()
        self.assertEqual(before, after, "owner/complimentary grant operations must never touch TAO tables")

    def test_tao_expiration_worker_cannot_revoke_owner_or_complimentary_access(self):
        tacc.grant_owner_access("owner-vs-tao@example.com")
        invite = tacc.create_access_invite("vs tao")
        tacc.redeem_access_invite(invite["token"], "comp-vs-tao@example.com")

        ta.check_expirations()  # the TAO worker: must not see/touch access_grants

        self.assertTrue(tacc.has_full_access("owner-vs-tao@example.com"))
        self.assertTrue(tacc.has_full_access("comp-vs-tao@example.com"))

    def test_stripe_cancellation_cannot_revoke_owner_or_complimentary_access(self):
        tacc.grant_owner_access("owner-vs-stripe@example.com")
        ts.save_customer_id("owner-vs-stripe@example.com", "cus_vs_access")
        future = int(time.time()) + 30 * 86400
        sub = {"id": "sub_vs_access", "customer": "cus_vs_access", "status": "active",
               "current_period_end": future, "cancel_at_period_end": False,
               "items": {"data": [{"price": {"id": "price_test"}}]}}
        ts.process_webhook_event({"id": "evt_vs1", "type": "customer.subscription.created",
                                   "created": 1000, "data": {"object": sub}})
        canceled = {**sub, "status": "canceled"}
        ts.process_webhook_event({"id": "evt_vs2", "type": "customer.subscription.deleted",
                                   "created": 2000, "data": {"object": canceled}})

        self.assertFalse(ts.has_pro("owner-vs-stripe@example.com"))  # Stripe correctly revoked its own grant
        self.assertTrue(tacc.has_full_access("owner-vs-stripe@example.com"),
                         "a Stripe cancellation must never revoke owner/complimentary access")

    def test_revoking_access_grant_does_not_touch_an_active_tao_subscription(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("""
            INSERT INTO users (roster_num, email, role, status, subscription_status, expires_at, pin_hash)
            VALUES ('TSACC1', 'both-tao-and-owner@example.com', 'operator', 'active', 'active',
                    datetime('now', '+30 days'), 'x:y')
        """)
        conn.commit()
        conn.close()
        tacc.grant_owner_access("both-tao-and-owner@example.com")
        tacc.revoke_access("both-tao-and-owner@example.com", "owner")

        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        tao_row = conn.execute("SELECT subscription_status FROM users WHERE email='both-tao-and-owner@example.com'").fetchone()
        conn.close()
        self.assertEqual(tao_row["subscription_status"], "active", "revoking owner access must not touch TAO status")


if __name__ == "__main__":
    unittest.main()
