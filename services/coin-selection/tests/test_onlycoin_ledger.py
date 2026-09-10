"""OnlyCoin tests use synthetic candidates and isolated temporary databases only."""
import importlib
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'services/coin-selection/src'))


def batch(seq=0, symbols=('AAAUSDT',), direction='LONG', day='2026-09-09'):
    sid = day.replace('-', '') + f'-{seq:03d}'
    stamp = f'{day}T{seq // 4:02d}:{seq % 4 * 15:02d}:00Z'
    return {'board_key': 'y', 'scan_id': sid, 'generated_at_utc': stamp,
            'parameter_version': 'param-v2.0.0-screener-y',
            'rule_identity': {'rule_revision': 'original', 'param_hash': None},
            'candidates': [{'symbol': s, 'direction': direction, 'scan_id': sid,
                            'scan_timestamp_utc': stamp, 'generated_at_utc': stamp,
                            'state': 'CONFIRMED', 'dmr_selected': True, 'market_rank': n + 1,
                            'canonical_asset_id': s.lower(), 'contract_multiplier': 1}
                           for n, s in enumerate(symbols)]}


class OnlyCoinLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'onlycoin.sqlite'

    def ledger(self, readonly=False):
        mod = importlib.import_module('coin_selection.onlycoin_ledger')
        obj = mod.OnlyCoinLedger(self.path, readonly=readonly)
        self.addCleanup(obj.close)
        return obj

    def test_first_batch_is_durable_full_ordered_daily_projection(self):
        self.assertIsNotNone(importlib.util.find_spec('coin_selection.onlycoin_ledger'),
                             'OnlyCoin persistent store is missing')
        ledger = self.ledger()
        symbols = tuple(f'T{i}USDT' for i in range(20))
        result = ledger.ingest_batch(batch(symbols=symbols))
        self.assertEqual(result['event_count'], 20)
        view = self.ledger(readonly=True).daily('2026-09-09', now='2026-09-10T00:00:00Z')
        self.assertEqual([m['symbol'] for m in view['onlycoin']], list(symbols))
        self.assertEqual(view['counts'], {'long': 20, 'short': 0, 'onlycoin': 20})
        self.assertEqual(view['board_key'], 'y')
        self.assertEqual(view['cycle_end_utc'], '2026-09-10T00:00:00Z')
        first = view['onlycoin'][0]
        for key in ('member_id', 'first_selected_at', 'first_available_at', 'first_scan_id',
                    'first_direction', 'directions', 'parameter_version', 'rule_identity', 'provenance'):
            self.assertIn(key, first)
        self.assertEqual(first['rule_identity']['param_hash'], None)
        self.assertEqual(first['canonical_asset_id'], 't0usdt')
        self.assertEqual(first['provenance'], 'observed_archive')
        self.assertEqual(view['coverage']['availability_quality'], 'PROXY')

    def test_direction_history_is_asof_and_first_identity_never_changes(self):
        ledger = self.ledger()
        original = batch()
        original['candidates'][0]['generated_at_utc'] = '2026-09-09T00:00:53Z'
        ledger.ingest_batch(original)
        self.assertEqual(ledger.daily('2026-09-09', as_of='2026-09-09T00:00:52Z')['onlycoin'], [])
        second = batch(1, ('BBBUSDT', 'AAAUSDT'), 'SHORT')
        second['rule_identity'] = {'rule_revision': 'new'}
        ledger.ingest_batch(second)
        early = ledger.daily('2026-09-09', as_of='2026-09-09T00:01:00Z')
        late = ledger.daily('2026-09-09', as_of='2026-09-09T00:16:00Z')
        self.assertEqual(early['onlycoin'][0]['directions'], ['LONG'])
        self.assertEqual(late['onlycoin'][0]['directions'], ['LONG', 'SHORT'])
        self.assertEqual(late['onlycoin'][0]['rule_identity'], original['rule_identity'])
        self.assertEqual(late['short'][1]['first_scan_id'], second['scan_id'])
        self.assertEqual(late['short'][1]['directions'], ['SHORT'])
        self.assertEqual(late['onlycoin'][0]['first_available_at'], '2026-09-09T00:00:53Z')
        self.assertIn('MIXED_IDENTITY_DAY', late['coverage']['flags'])

    def test_live_retry_preserves_first_availability(self):
        ledger = self.ledger()
        b = batch()
        b['candidates'][0]['state'] = 'CONFIRMED'
        first = ledger.ingest_batch(b, provenance='live_committed', available_at='2026-09-09T00:01:00Z')
        retry = ledger.ingest_batch(b, provenance='live_committed', available_at='2026-09-09T00:02:00Z')
        self.assertEqual(first['status'], 'ok')
        self.assertEqual(retry['status'], 'duplicate')
        self.assertTrue(retry['idempotent'])
        self.assertEqual(first['commit_seq'], retry['commit_seq'])
        view = ledger.daily('2026-09-09', as_of='2026-09-09T00:01:00Z')
        self.assertEqual(view['onlycoin'][0]['first_available_at'], '2026-09-09T00:01:00Z')
        self.assertEqual(view['coverage']['observed_scans'], 1)
        with self.assertRaises(ValueError):
            ledger.ingest_batch(b, provenance='observed_archive')
        b['candidates'][0]['market_rank'] = 99
        with self.assertRaises(ValueError):
            ledger.ingest_batch(b, provenance='live_committed')

    def test_scan_retries_conflicts_out_of_order_and_atomic_rejection(self):
        ledger = self.ledger()
        b = batch(1)
        first = ledger.ingest_batch(b)
        self.assertTrue(ledger.ingest_batch(b)['idempotent'])
        changed = batch(1, ('BBBUSDT',))
        for invalid in (changed, batch(0)):
            with self.assertRaises(ValueError):
                ledger.ingest_batch(invalid)
        invalid = batch(2, ('CC CUSDT',))
        with self.assertRaises(ValueError):
            ledger.ingest_batch(invalid)
        view = ledger.daily('2026-09-09')
        self.assertEqual(view['projection_revision'], first['commit_seq'])
        self.assertEqual(view['counts']['onlycoin'], 1)
        self.assertIn('20260909-000', view['coverage']['missing_scan_ids'])

    def test_midnight_empty_batch_and_actual_today_never_fall_back(self):
        ledger = self.ledger()
        ledger.ingest_batch(batch(95))
        self.assertEqual(ledger.daily(now='2026-09-10T00:00:00Z')['onlycoin'], [])
        ledger.ingest_batch(batch(0, (), day='2026-09-10'))
        today = ledger.daily(now='2026-09-10T00:01:00Z')
        self.assertEqual(today['as_of_scan_id'], '20260910-000')
        self.assertEqual(today['counts']['onlycoin'], 0)
        self.assertEqual(ledger.daily('2026-09-09', now='2026-09-11T00:00:00Z')['counts']['onlycoin'], 1)
        from datetime import datetime, timezone
        self.assertEqual(ledger.daily()['business_date'], datetime.now(timezone.utc).date().isoformat())

    def test_readonly_and_dataset_isolation_and_immutable_sql(self):
        import sqlite3
        ledger = self.ledger()
        ledger.ingest_batch(batch(), provenance='live_committed', available_at='2026-09-09T00:01:00Z')
        self.assertEqual(ledger.daily('2026-09-09')['coverage']['availability_quality'], 'COMMITTED')
        with self.assertRaises(ValueError):
            ledger.ingest_batch(batch(1))
        with self.assertRaises(PermissionError):
            self.ledger(readonly=True).ingest_batch(batch(1))
        with self.assertRaises(sqlite3.DatabaseError):
            ledger.conn.execute('DELETE FROM onlycoin_scan_commits')
        ledger.conn.rollback()
        with self.assertRaises(sqlite3.DatabaseError):
            ledger.conn.execute("UPDATE onlycoin_datasets SET provenance='observed_archive'")
        ledger.conn.rollback()

    def test_live_requires_confirmed_selected_and_y_parameter(self):
        ledger = self.ledger()
        for field, value in (('state', None), ('state', 'QUALIFIED'),
                             ('dmr_selected', None), ('dmr_selected', 1),
                             ('parameter_version', None), ('parameter_version', 'other')):
            with self.subTest(field=field, value=value):
                b = batch()
                b['candidates'][0][field] = value
                with self.assertRaises(ValueError):
                    ledger.ingest_batch(b, provenance='live_committed')
        b = batch()
        del b['candidates'][0]['state']
        del b['candidates'][0]['dmr_selected']
        with self.assertRaises(ValueError):
            ledger.ingest_batch(b, provenance='live_committed')
        for value in (None, 'other'):
            b = batch(symbols=())
            b['parameter_version'] = value
            with self.assertRaises(ValueError):
                ledger.ingest_batch(b, provenance='live_committed')
        self.assertEqual(ledger.daily('2026-09-09')['projection_revision'], 0)
        # Historical batches may predate these fields; keep their evidence intact.
        b = batch()
        del b['candidates'][0]['state']
        del b['candidates'][0]['dmr_selected']
        b['parameter_version'] = None
        ledger.ingest_batch(b)
        self.assertEqual(ledger.daily('2026-09-09')['counts']['onlycoin'], 1)

    def test_no_symbol_normalization_or_non_selected_inputs(self):
        ledger = self.ledger()
        for symbol in ('aaaUSDT', ' AAAUSDT', 'AAA/USDT', 'AAAUSDT '):
            with self.assertRaises(ValueError):
                ledger.ingest_batch(batch(symbols=(symbol,)))
        b = batch()
        b['candidates'][0]['dmr_selected'] = False
        with self.assertRaises(ValueError):
            ledger.ingest_batch(b)
        b = batch()
        b['board_key'] = 'main'
        with self.assertRaises(ValueError):
            ledger.ingest_batch(b)

    def test_unicode_exchange_symbols_preserved_without_normalization(self):
        ledger = self.ledger()
        ledger.ingest_batch(batch(symbols=('币安人生USDT', '1000PEPEUSDT', 'PEPEUSDT')))
        view = ledger.daily('2026-09-09')
        self.assertEqual([m['symbol'] for m in view['onlycoin']], ['币安人生USDT', '1000PEPEUSDT', 'PEPEUSDT'])

    def test_unknown_market_and_duplicate_symbol_batch_are_rejected(self):
        ledger = self.ledger()
        for field, value in (('exchange', 'other'), ('market_type', 'spot')):
            b = batch()
            b['candidates'][0][field] = value
            with self.assertRaises(ValueError):
                ledger.ingest_batch(b)
        with self.assertRaises(ValueError):
            ledger.ingest_batch(batch(symbols=('AAAUSDT', 'AAAUSDT')))

    def test_original_batch_is_preserved_verbatim_semantically(self):
        import json
        ledger = self.ledger()
        b = batch()
        b['unknown_future_schema_field'] = {'unchanged': [1, None]}
        b['candidates'][0]['parameter_version'] = None
        ledger.ingest_batch(b)
        raw = ledger.conn.execute('SELECT batch_json FROM onlycoin_scan_commits').fetchone()[0]
        self.assertEqual(json.loads(raw), b)
        self.assertIsNone(ledger.daily('2026-09-09')['onlycoin'][0]['parameter_version'])

    def test_schema_version_is_checked_on_reopen(self):
        import sqlite3
        self.path.touch()
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE onlycoin_datasets(id INTEGER, schema_version TEXT, provenance TEXT)')
            db.execute("INSERT INTO onlycoin_datasets VALUES(1,'future-schema','observed_archive')")
        with self.assertRaises(ValueError):
            self.ledger()


if __name__ == '__main__':
    unittest.main()
