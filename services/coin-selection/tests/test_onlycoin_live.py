"""Live projection contract; the ledger's membership is never recomputed."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'services/api-gateway'))
import onlycoin_api as api

class Live(unittest.TestCase):
    def test_real_http_route_rejects_history_and_stale(self):
        import threading
        import json
        from http.server import ThreadingHTTPServer
        from urllib.request import urlopen
        from urllib.error import HTTPError
        import mock_server
        server = ThreadingHTTPServer(('127.0.0.1', 0), mock_server.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f'http://127.0.0.1:{server.server_port}/api/v1/onlycoin/live'
            with patch.object(api, 'daily_payload', return_value=(200,self.data())):
                with urlopen(base) as response:
                    self.assertEqual(response.headers['Cache-Control'], 'no-store')
                    self.assertTrue(json.load(response)['valid'])
                with self.assertRaises(HTTPError) as err:
                    urlopen(base + '?as_of=')
                self.assertEqual(err.exception.code, 400)
            data=self.data(); data['stale']=True
            with patch.object(api, 'daily_payload', return_value=(200,data)):
                with self.assertRaises(HTTPError) as err:
                    urlopen(base)
                self.assertEqual(err.exception.code, 503)
                self.assertFalse(json.load(err.exception)['valid'])
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def data(self, **changes):
        return dict(board_key='y', dataset_id='live', business_date='2026-09-11',
                    cycle_start_utc='2026-09-11T00:00:00Z', cycle_end_utc='2026-09-12T00:00:00Z',
                    server_time='2026-09-11T12:00:00Z', last_committed_at='2026-09-11T11:59:00Z',
                    as_of_scan_id='20260911-047', projection_revision=2, stale=False,
                    onlycoin=[{'symbol':'龙虾USDT','parameter_version':'param-v2.0.0-screener-y'}],
                    long=[], short=[], counts={'onlycoin':1,'long':0,'short':0}, **changes)
    def test_live_contract(self):
        self.assertTrue(callable(getattr(api, 'live_payload', None)), 'live endpoint missing')
        with patch.object(api, 'daily_payload', return_value=(200,self.data())):
            code, body = api.live_payload({})
        self.assertEqual(code,200)
        self.assertTrue(body['valid'])
        self.assertEqual(body['symbols'],['龙虾USDT'])
        self.assertEqual(body['version'],'param-v2.0.0-screener-y')
        self.assertEqual(body['updated_at_utc'],'2026-09-11T11:59:00Z')
        self.assertEqual(body['valid_until_utc'],'2026-09-11T12:15:00Z')
    def test_invalid_never_exports_members(self):
        for changes in [{'stale':True},{'last_committed_at':None},{'last_committed_at':'2026-09-11T12:01:00Z'},
                        {'as_of_scan_id':'20260911-040'},{'business_date':'2026-09-10'},
                        {'server_time':'2026-09-12T00:00:00Z'},
                        {'server_time':'2026-09-11T12:15:00Z'},
                        {'onlycoin':[{'symbol':'A','parameter_version':'foreign'}]}]:
            data=self.data(); data.update(changes)
            with self.subTest(changes=changes), patch.object(api,'daily_payload',return_value=(200,data)):
                code,body=api.live_payload({})
                self.assertEqual(code,503); self.assertFalse(body['valid']); self.assertEqual(body['symbols'],[])
                self.assertEqual(body['onlycoin'],[])
    def test_history_parameters_rejected(self):
        for query in [{'as_of':['2026-09-11T01:00:00Z']},{'day':['2026-09-11']},{'dataset_id':['archive']}]:
            code,body=api.live_payload(query)
            self.assertEqual(code,400); self.assertFalse(body['valid'])
    def test_unavailable_and_empty(self):
        with patch.object(api,'daily_payload',return_value=(503,{'error':'ONLYCOIN_UNAVAILABLE'})):
            code,body=api.live_payload({}); self.assertEqual(code,503); self.assertFalse(body['valid'])
        data=self.data(); data.update(onlycoin=[],counts={'onlycoin':0,'long':0,'short':0})
        with patch.object(api,'daily_payload',return_value=(200,data)):
            code,body=api.live_payload({}); self.assertEqual(code,200); self.assertTrue(body['valid']); self.assertEqual(body['status'],'empty')
