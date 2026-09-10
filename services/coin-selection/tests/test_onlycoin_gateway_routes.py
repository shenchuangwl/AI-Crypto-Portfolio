"""OnlyCoin HTTP wiring: no production writes or network listeners."""
import importlib
import io
import json
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'services/api-gateway'))
sys.path.insert(0, str(ROOT / 'services/coin-selection/src'))
gw = importlib.import_module('mock_server')


class Routes(unittest.TestCase):
    def handler(self, path, body=None, headers=None):
        h = object.__new__(gw.Handler)
        h.path = path
        raw = json.dumps(body).encode() if body is not None else b''
        h.headers = {'Content-Length': str(len(raw)), 'Content-Type': 'application/json', **(headers or {})}
        h.rfile = io.BytesIO(raw)
        h.result = None
        h._json = lambda code, data: setattr(h, 'result', (code, data))
        return h

    def test_daily_and_review_route_use_new_reader(self):
        calls = []
        fake = types.ModuleType('onlycoin_api')
        fake.daily_payload = lambda qs, historical=False: (200, {'historical': historical, 'seen': calls.append(qs)})
        with patch.dict(sys.modules, {'onlycoin_api': fake}):
            for path, historical in [('/api/v1/screener-y/dmr-daily', False), ('/api/v1/review/onlycoin?board=y', True)]:
                h = self.handler(path)
                h.do_GET()
                self.assertEqual(h.result[0], 200)
                self.assertEqual(h.result[1]['historical'], historical)
        self.assertEqual(len(calls), 2)

    def test_control_missing_admin_configuration_fails_closed(self):
        h = self.handler('/api/v1/onlycoin/sources/screener-y/control', {'enabled': True})
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(callable(getattr(h, 'do_PUT', None)), 'OnlyCoin authenticated PUT missing')
            h.do_PUT()
        self.assertEqual(h.result[0], 503)

    def test_control_rejects_no_auth_and_cross_origin(self):
        with patch.dict(os.environ, {'ONLYCOIN_ADMIN_TOKEN': 'test-only-token'}):
            for headers in [{}, {'Authorization':'Bearer wrong'}, {'Authorization':'Bearer test-only-token','Origin':'https://evil.invalid','Host':'localhost'}]:
                h = self.handler('/api/v1/onlycoin/sources/screener-y/control', {'enabled': True}, headers)
                h.do_PUT()
                self.assertIn(h.result[0], (401,403))

    def test_control_requires_exact_public_fields(self):
        base = {'enabled': False, 'expected_revision': 0, 'request_id': 'test', 'reason': 'test'}
        cases = [dict(base, unexpected=True), dict(base, expected_revision=True), dict(base, reason='')]
        cases.append({k:v for k,v in base.items() if k != 'reason'})
        with patch.dict(os.environ, {'ONLYCOIN_ADMIN_TOKEN':'test-only-token'}):
            for body in cases:
                h = self.handler('/api/v1/onlycoin/sources/screener-y/control', body, {'Authorization':'Bearer test-only-token'})
                h.do_PUT()
                self.assertEqual(h.result[0], 400)

    def test_control_authenticated_dispatch_and_status(self):
        calls = []
        fake = types.ModuleType('coin_selection.onlycoin_source')
        fake.set_source_control = lambda payload, **kw: {'enabled':payload['enabled'], 'actor':kw['actor'], 'called': calls.append(payload)}
        fake.source_status = lambda **kw: {'enabled':False,'eligibility_only':True}
        with patch.dict(sys.modules, {'coin_selection.onlycoin_source':fake}), patch.dict(os.environ, {'ONLYCOIN_ADMIN_TOKEN':'test-only-token'}):
            h = self.handler('/api/v1/onlycoin/sources/screener-y/control', {'enabled':False,'expected_revision':0,'request_id':'test','reason':'test'}, {'Authorization':'Bearer test-only-token'})
            h.do_PUT()
            self.assertEqual(h.result[0], 200)
            self.assertFalse(h.result[1]['enabled'])
            h = self.handler('/api/v1/onlycoin/sources/screener-y/status')
            h.do_GET()
            self.assertEqual(h.result, (200, {'enabled':False,'eligibility_only':True}))
        self.assertEqual(len(calls),1)


if __name__ == '__main__':
    unittest.main()
