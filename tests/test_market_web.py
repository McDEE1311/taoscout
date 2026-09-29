from pathlib import Path
import tempfile
import unittest
try:
    from fastapi.testclient import TestClient
    from market.web import create_app
except ImportError:
    TestClient = None


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
