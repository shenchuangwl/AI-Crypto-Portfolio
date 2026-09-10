"""Read-only helper contract; all filesystem activity stays in tempdirs."""
import importlib
import importlib.util
import sys
import unittest
from unittest.mock import patch
from test_onlycoin_ledger import ROOT, batch
import test_onlycoin_ledger as ledger_tests

sys.path.insert(0, str(ROOT / 'services/api-gateway'))


class OnlyCoinApiTests(unittest.TestCase):
    setUp = ledger_tests.OnlyCoinLedgerTests.setUp
    ledger = ledger_tests.OnlyCoinLedgerTests.ledger

    def api(self):
        self.assertIsNotNone(importlib.util.find_spec('onlycoin_api'), 'read-only helper missing')
        module = importlib.import_module('onlycoin_api')
        self.patch = patch.object(module, 'ROOT', self.path.parent)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        return module

    def test_absent_store_does_not_create_data(self):
        module = self.api()
        status, payload = module.daily_payload({})
        self.assertEqual(status, 200)
        self.assertEqual(payload['onlycoin'], [])
        self.assertTrue(payload['stale'])
        self.assertEqual(list(self.path.parent.iterdir()), [])

    def test_history_requires_y_and_valid_explicit_day(self):
        module = self.api()
        for qs in ({}, {'board': ['main'], 'day': ['2026-09-09']},
                   {'board': ['y'], 'day': ['2026-02-30']},
                   {'board': ['y'], 'day': ['2026-09-09'], 'as_of': ['2026-09-09T01:00:00']},
                   {'board': ['y'], 'day': ['2026-09-09'], 'business_date': ['2026-09-08']},
                   {'board': ['y', 'main'], 'day': ['2026-09-09']},
                   {'board': ['y'], 'day': ['2026-09-09'], 'as_of': ['2026-09-10T00:00:00Z']}):
            self.assertEqual(module.daily_payload(qs, historical=True)[0], 400, qs)

    def test_history_selects_one_dataset_without_merging(self):
        module = self.api()
        from coin_selection.onlycoin_ledger import OnlyCoinLedger
        review = self.path.parent / 'data/coin-selection-y/review'
        with OnlyCoinLedger(review / 'onlycoin-archive.sqlite') as archive:
            archive.ingest_batch(batch(symbols=('ARCHIVEUSDT',), day='2026-09-08'))
            archive.ingest_batch(batch(symbols=('ARCHIVEUSDT',)))
        with OnlyCoinLedger(review / 'onlycoin.sqlite') as live:
            live.ingest_batch(batch(symbols=('LIVEUSDT',)), provenance='live_committed',
                              available_at='2026-09-09T00:02:00Z')
        before = {p: p.read_bytes() for p in review.iterdir()}
        for dataset, day, expected_id, symbols in (
                (None, '2026-09-09', 'live', ['LIVEUSDT']),
                ('auto', '2026-09-09', 'live', ['LIVEUSDT']),
                ('archive', '2026-09-09', 'archive', ['ARCHIVEUSDT']),
                ('live', '2026-09-08', 'live', []),
                (None, '2026-09-08', 'archive', ['ARCHIVEUSDT'])):
            with self.subTest(dataset=dataset, day=day):
                qs = {'board': ['y'], 'day': [day]}
                if dataset is not None:
                    qs['dataset_id'] = [dataset]
                status, payload = module.daily_payload(qs, historical=True)
                self.assertEqual(status, 200)
                self.assertEqual(payload.get('dataset_id'), expected_id)
                self.assertEqual([r['symbol'] for r in payload['onlycoin']], symbols)
        # Dataset choice depends on day commits, not availability at the cutoff.
        status, payload = module.daily_payload({'board': ['y'], 'day': ['2026-09-09'],
                                               'as_of': ['2026-09-09T00:01:00Z']}, historical=True)
        self.assertEqual(status, 200)
        self.assertEqual(payload['dataset_id'], 'live')
        self.assertEqual(payload['onlycoin'], [])
        self.assertEqual(before, {p: p.read_bytes() for p in review.iterdir()})

    def test_dataset_queries_are_strict_and_missing_stores_stay_readonly(self):
        module = self.api()
        qs = {'board': ['y'], 'day': ['2026-09-09']}
        for value in (['unknown'], ['LIVE'], [''], [], ['live', 'archive'], 'archive'):
            self.assertEqual(module.daily_payload(dict(qs, dataset_id=value), historical=True)[0], 400)
        for dataset in ('auto', 'archive'):
            self.assertEqual(module.daily_payload({'dataset_id': [dataset]})[0], 400)
        for dataset in ('auto', 'archive', 'live'):
            status, payload = module.daily_payload(dict(qs, dataset_id=[dataset]), historical=True)
            self.assertEqual(status, 200)
            self.assertEqual(payload['dataset_id'], 'archive' if dataset == 'auto' else dataset)
            self.assertEqual(payload['onlycoin'], [])
        self.assertEqual(module.daily_payload({})[1]['dataset_id'], 'live')
        self.assertEqual(list(self.path.parent.iterdir()), [])

    def test_empty_live_commit_wins_over_archive(self):
        module = self.api()
        from coin_selection.onlycoin_ledger import OnlyCoinLedger
        review = self.path.parent / 'data/coin-selection-y/review'
        with OnlyCoinLedger(review / 'onlycoin-archive.sqlite') as archive:
            archive.ingest_batch(batch())
        with OnlyCoinLedger(review / 'onlycoin.sqlite') as live:
            live.ingest_batch(batch(symbols=()), provenance='live_committed',
                              available_at='2026-09-09T00:01:00Z')
        status, payload = module.daily_payload({'board': ['y'], 'day': ['2026-09-09']}, historical=True)
        self.assertEqual(status, 200)
        self.assertEqual(payload['dataset_id'], 'live')
        self.assertEqual(payload['onlycoin'], [])
        self.assertEqual(payload['coverage']['observed_scans'], 1)

    def test_corrupt_live_store_does_not_fall_back_to_archive(self):
        module = self.api()
        review = self.path.parent / 'data/coin-selection-y/review'
        review.mkdir(parents=True)
        (review / 'onlycoin.sqlite').write_bytes(b'not sqlite')
        status, payload = module.daily_payload({'board': ['y'], 'day': ['2026-09-09']}, historical=True)
        self.assertEqual(status, 503)
        self.assertEqual(payload['error'], 'ONLYCOIN_UNAVAILABLE')

    def test_readonly_payload_matches_ledger(self):
        module = self.api()
        self.path = self.path.parent / 'data/coin-selection-y/review/onlycoin.sqlite'
        ledger = self.ledger()
        ledger.ingest_batch(batch())
        before = self.path.read_bytes()
        status, payload = module.daily_payload({'board': ['y'], 'business_date': ['2026-09-09'],
                                               'as_of': ['2026-09-09T00:01:00Z']}, historical=True)
        self.assertEqual(status, 200)
        expected = ledger.daily('2026-09-09', as_of='2026-09-09T00:01:00Z')
        for key in ('long', 'short', 'onlycoin', 'projection_revision', 'as_of_scan_id'):
            self.assertEqual(payload[key], expected[key])
        self.assertEqual(before, self.path.read_bytes())


if __name__ == '__main__':
    unittest.main()
