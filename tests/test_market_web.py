from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
try:
    from fastapi.testclient import TestClient
    from market.web import create_app
    from market.ledger import publish
    from market.engine import Candle, HOUR
except ImportError:
    TestClient = None

START = datetime(2025, 1, 1, tzinfo=timezone.utc)


def candles(n=240):
    return [Candle(START + i * HOUR, START + (i + 1) * HOUR, 100 + i, 102 + i, 98 + i, 101 + i)
            for i in range(n)]


@unittest.skipIf(TestClient is None, 'Install requirements-market-test.txt for HTTP tests')
class WebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.client = TestClient(create_app(ledger=Path(self.tmp.name)/'missing.db'))

    def tearDown(self): self.tmp.cleanup()

    def test_empty_state_is_honest(self):
        data = self.client.get('/api/history').json()
        self.assertEqual(data['records'], [])
        self.assertTrue(data['stale'])
        self.assertEqual(self.client.get('/api/simulation').json()['status'], 'not_available')

    def test_shell_and_security_headers(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Paid access is not open',response.text)
        self.assertEqual(response.headers['cache-control'],'no-store')
        self.assertIn("frame-ancestors 'none'",response.headers['content-security-policy'])
        for path in ['/app.js','/style.css','/manifest.webmanifest','/sw.js','/icon-192.png','/icon-512.png']:
            self.assertEqual(self.client.get(path).status_code, 200)

    def test_invalid_query_and_files_are_blocked(self):
        self.assertEqual(self.client.get('/api/history?limit=501').status_code,422)
        self.assertEqual(self.client.get('/api/history?before=-1').status_code,422)
        for path in ['/config.json','/research.db','/engine.py','/api.py']:
            self.assertEqual(self.client.get(path).status_code,404)
        self.assertEqual(self.client.post('/api/history').status_code,405)

    def test_broken_ledger_fails_closed(self):
        path = Path(self.tmp.name)/'broken.db'; path.write_text('broken')
        client = TestClient(create_app(ledger=path))
        response = client.get('/api/history')
        self.assertEqual(response.status_code,503)
        self.assertNotIn(str(path), response.text)

    def test_mount_works(self):
        from fastapi import FastAPI
        parent = FastAPI(); parent.mount('/market', create_app(ledger=Path(self.tmp.name)/'missing.db'))
        client = TestClient(parent)
        self.assertEqual(client.get('/market/').status_code,200)
        self.assertEqual(client.get('/market/api/history').status_code,200)
        self.assertEqual(client.get('/market/manifest.webmanifest').json()['scope'],'./')

    def test_default_resolve_pro_is_none_and_everyone_sees_full_history(self):
        # No resolve_pro injected (this module's own standalone/default
        # behavior): every caller must see the complete, undelayed history.
        path = Path(self.tmp.name) / 'default.db'
        for i in range(3):
            publish(path, candles(), 'synthetic', START + 240 * HOUR + i * 8 * HOUR)
        client = TestClient(create_app(ledger=path))
        data = client.get('/api/history').json()
        self.assertEqual(len(data['records']), 3)
        self.assertFalse(data['delayed'])

    def test_injected_resolve_pro_delays_free_but_not_pro(self):
        path = Path(self.tmp.name) / 'gated.db'
        for i in range(3):
            publish(path, candles(), 'synthetic', START + 240 * HOUR + i * 8 * HOUR)

        free_client = TestClient(create_app(ledger=path, resolve_pro=lambda request: False))
        free_data = free_client.get('/api/history').json()
        self.assertEqual(len(free_data['records']), 2, "Free tier must have the newest published slot withheld")
        self.assertTrue(free_data['delayed'])

        pro_client = TestClient(create_app(ledger=path, resolve_pro=lambda request: True))
        pro_data = pro_client.get('/api/history').json()
        self.assertEqual(len(pro_data['records']), 3, "Pro tier must see the full, undelayed history")
        self.assertFalse(pro_data['delayed'])

        # The record Free doesn't get must be exactly the newest one Pro does.
        self.assertNotIn(pro_data['records'][0]['slot'], [r['slot'] for r in free_data['records']])

    def test_free_tier_pagination_only_withholds_the_single_newest_slot(self):
        # Regression test: redacting index 0 of every page (rather than only
        # the page that can contain the true global-newest slot) would lose
        # one record per page as a Free caller paginates with a small limit,
        # over-redacting far beyond the intended one-slot delay.
        path = Path(self.tmp.name) / 'paginated.db'
        for i in range(5):
            publish(path, candles(), 'synthetic', START + 240 * HOUR + i * 8 * HOUR)
        client = TestClient(create_app(ledger=path, resolve_pro=lambda request: False))

        collected = []
        before = None
        for _ in range(10):
            params = {'limit': 1, **({'before': before} if before is not None else {})}
            page = client.get('/api/history', params=params).json()
            collected.extend(page['records'])
            if page['next_before'] is None:
                break
            before = page['next_before']

        self.assertEqual(len(collected), 4, "Free tier should see all but the single newest slot, across pagination")

    def test_forged_high_before_cursor_cannot_reveal_the_newest_record(self):
        # Regression test: eligibility used to be enforced only when
        # before is None, so a caller could supply a `before` value larger
        # than the newest record's id and receive it unredacted anyway —
        # `before` only ever excludes ids >= itself, so a big enough value
        # excludes nothing. Eligibility must be computed server-side and
        # enforced in the query itself, independent of any caller-supplied
        # cursor value.
        path = Path(self.tmp.name) / 'forged.db'
        for i in range(3):
            publish(path, candles(), 'synthetic', START + 240 * HOUR + i * 8 * HOUR)
        client = TestClient(create_app(ledger=path, resolve_pro=lambda request: False))

        newest_slot = TestClient(create_app(ledger=path)).get('/api/history').json()['records'][0]['slot']

        for forged_before in (10**9, 2**31, 2**62):
            data = client.get('/api/history', params={'before': forged_before}).json()
            self.assertNotIn(newest_slot, [r['slot'] for r in data['records']],
                              f"before={forged_before} must not reveal the withheld newest record")
            self.assertEqual(len(data['records']), 2)

    def test_free_tier_delay_holds_across_varying_limits(self):
        path = Path(self.tmp.name) / 'varying_limits.db'
        for i in range(6):
            publish(path, candles(), 'synthetic', START + 240 * HOUR + i * 8 * HOUR)
        free_client = TestClient(create_app(ledger=path, resolve_pro=lambda request: False))
        pro_newest_slot = TestClient(create_app(ledger=path)).get(
            '/api/history', params={'limit': 1}).json()['records'][0]['slot']

        for limit in (1, 2, 3, 5, 500):
            data = free_client.get('/api/history', params={'limit': limit}).json()
            self.assertNotIn(pro_newest_slot, [r['slot'] for r in data['records']],
                              f"limit={limit} must still withhold the newest record")
            self.assertEqual(len(data['records']), min(limit, 5))

    def test_stale_flag_is_computed_from_the_free_tier_visible_records(self):
        # If the withheld newest record is very fresh but the next one down
        # is old, a Free caller's `stale` flag must reflect what THEY can
        # see, not the hidden record — otherwise `stale=False` would leak
        # that a very recent (paid-tier) record exists.
        path = Path(self.tmp.name) / 'staleness.db'
        now = datetime.now(timezone.utc)
        # The most recent 8-hour schedule boundary at/before now: always
        # within publish()'s required 30-minute window of itself (diff=0),
        # and at most 8h old — comfortably under the 9h staleness threshold.
        fresh_slot = now.replace(hour=(now.hour // 8) * 8, minute=0, second=0, microsecond=0)
        publish(path, candles(), 'synthetic', START + 240 * HOUR)  # old record
        publish(path, candles(), 'synthetic', fresh_slot)  # fresh "newest" record (withheld from Free)

        pro_data = TestClient(create_app(ledger=path)).get('/api/history').json()
        self.assertFalse(pro_data['stale'], "Pro should see the fresh record and not be marked stale")

        free_data = TestClient(create_app(ledger=path, resolve_pro=lambda request: False)).get('/api/history').json()
        self.assertEqual(len(free_data['records']), 1)
        self.assertTrue(free_data['stale'], "Free's staleness must reflect only the old record they can actually see")
