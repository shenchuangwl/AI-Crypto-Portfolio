"""OnlyCoin 回放统计：合成候选 + 临时库/临时快照目录，绝不触碰生产数据。

覆盖需求 §6 的验收项 1–9 的服务端部分：区间独立去重、首次入选冻结、完整交易日
收线退出、多空方向、未完成日 / 缺价 / 缺快照的诚实标注，以及查询校验与功能开关。
"""
import importlib
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'services/coin-selection/src'))
sys.path.insert(0, str(ROOT / 'services/api-gateway'))

from coin_selection.onlycoin_ledger import OnlyCoinLedger  # noqa: E402
from coin_selection.onlycoin_stats import (  # noqa: E402
    LAST_NODE_SEQ,
    OnlyCoinStatsError,
    SnapshotReader,
    build_stats,
    dedupe_by_coin,
    first_entries,
    range_stats,
)

UTC = timezone.utc


def stamp(day, seq):
    return f'{day}T{seq // 4:02d}:{seq % 4 * 15:02d}:00Z'


def batch(day, seq, members):
    """members: [(symbol, direction, canonical_asset_id)]"""
    sid = day.replace('-', '') + f'-{seq:03d}'
    at = stamp(day, seq)
    return {
        'board_key': 'y', 'scan_id': sid, 'generated_at_utc': at,
        'parameter_version': 'param-v2.0.0-screener-y',
        'rule_identity': {'rule_revision': 'y-v2.0.0-r4', 'param_hash': None},
        'candidates': [
            {'symbol': s, 'direction': d, 'scan_id': sid, 'scan_timestamp_utc': at,
             'generated_at_utc': at, 'state': 'CONFIRMED', 'dmr_selected': True,
             'canonical_asset_id': c, 'underlying_asset': s[:-4], 'contract_multiplier': 1}
            for s, d, c in members
        ],
    }


def board(day, seq, prices):
    """一张最小板面快照：prices = {(symbol, direction): last_price}，None = 该刻不在板上。"""
    long_pool, short_pool = [], []
    for (symbol, direction), px in prices.items():
        if px is None:
            continue
        row = {'symbol': symbol, 'direction': direction, 'last_price': px, 'ref_price': px,
               'state': 'CONFIRMED', 'dmr_selected': True, 'final_zone': 'CONFIRMED'}
        (long_pool if direction == 'up' else short_pool).append(row)
    return {'meta': {'scan_id': day.replace('-', '') + f'-{seq:03d}', 'scan_timestamp_utc': stamp(day, seq)},
            'long_pool': long_pool, 'short_pool': short_pool}


class OnlyCoinStatsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.snapshots = self.root / 'data/coin-selection-y/snapshots'
        self.snapshots.mkdir(parents=True)
        self.review = self.root / 'data/coin-selection-y/review'
        self.review.mkdir(parents=True)
        self.path = self.review / 'onlycoin.sqlite'

    # ---- fixtures -------------------------------------------------------
    def write_board(self, day, seq, prices):
        sid = day.replace('-', '') + f'-{seq:03d}'
        (self.snapshots / f'{sid}.json').write_text(json.dumps(board(day, seq, prices)), encoding='utf-8')

    def ingest(self, batches, provenance='observed_archive', available=None):
        with OnlyCoinLedger(self.path) as store:
            for i, b in enumerate(batches):
                store.ingest_batch(b, provenance=provenance,
                                   available_at=available[i] if available else None)

    def conn(self):
        store = OnlyCoinLedger(self.path, readonly=True)
        self.addCleanup(store.close)
        return store.conn

    def reader(self):
        return SnapshotReader(self.snapshots)

    # ---- 首次入选投影与 daily() 的等价性 --------------------------------
    def test_full_day_range_matches_daily_projection_field_by_field(self):
        day = '2026-09-09'
        self.ingest([
            batch(day, 4, [('AAAUSDT', 'LONG', 'aaa')]),
            batch(day, 8, [('AAAUSDT', 'SHORT', 'aaa'), ('BBBUSDT', 'SHORT', 'bbb')]),
            batch(day, 12, [('CCCUSDT', 'LONG', 'ccc')]),
        ])
        store = OnlyCoinLedger(self.path, readonly=True)
        self.addCleanup(store.close)
        d0 = datetime(2026, 9, 9, tzinfo=UTC)
        ref = store.daily(day, now='2026-09-10T00:00:00Z')
        mine = first_entries(store.conn, start=d0, end=d0 + timedelta(days=1),
                             cutoff=d0 + timedelta(days=1))
        key = lambda m: (m['symbol'], m['first_selected_at'], m['first_scan_id'],
                         m['first_direction'], tuple(m['directions']))
        self.assertEqual(sorted(map(key, ref['onlycoin'])), sorted(map(key, mine)))
        # 后续重复入选不新增成员，只累计次数；方向历史仍然追加。
        aaa = next(m for m in mine if m['symbol'] == 'AAAUSDT')
        self.assertEqual(aaa['first_direction'], 'LONG')
        self.assertEqual(aaa['directions'], ['LONG', 'SHORT'])
        self.assertEqual(aaa['repeat_entries'], 1)

    def test_availability_cutoff_hides_not_yet_published_commits(self):
        day = '2026-09-09'
        self.ingest(
            [batch(day, 4, [('AAAUSDT', 'LONG', 'aaa')]), batch(day, 8, [('BBBUSDT', 'LONG', 'bbb')])],
            available=['2026-09-09T01:05:00Z', '2026-09-09T02:05:00Z'],
        )
        d0 = datetime(2026, 9, 9, tzinfo=UTC)
        got = first_entries(self.conn(), start=d0, end=d0 + timedelta(days=1),
                            cutoff=datetime(2026, 9, 9, 1, 30, tzinfo=UTC))
        self.assertEqual([m['symbol'] for m in got], ['AAAUSDT'])

    # ---- 唯一币种去重 ---------------------------------------------------
    def test_unique_coin_dedupe_uses_canonical_identity_not_display_name(self):
        entries = [
            {'symbol': '1000PEPEUSDT', 'canonical_asset_id': 'pepe', 'first_selected_at': '2026-09-09T02:00:00Z',
             'first_ordinal': 0, 'repeat_entries': 0},
            {'symbol': 'PEPEUSDT', 'canonical_asset_id': 'pepe', 'first_selected_at': '2026-09-09T03:00:00Z',
             'first_ordinal': 0, 'repeat_entries': 0},
            {'symbol': 'AAAUSDT', 'canonical_asset_id': 'aaa', 'first_selected_at': '2026-09-09T04:00:00Z',
             'first_ordinal': 0, 'repeat_entries': 0},
        ]
        out = dedupe_by_coin(entries)
        self.assertEqual([r['coin_id'] for r in out], ['PEPE', 'AAA'])
        # 保留的是**最早**那条，后来的只累计次数。
        self.assertEqual(out[0]['symbol'], '1000PEPEUSDT')
        self.assertEqual(out[0]['repeat_entries'], 1)

    def test_same_coin_on_two_days_counts_once_in_a_spanning_range_twice_when_split(self):
        self.ingest([
            batch('2026-09-09', 4, [('AAAUSDT', 'LONG', 'aaa')]),
            batch('2026-09-10', 4, [('AAAUSDT', 'LONG', 'aaa')]),
        ])
        for day in ('2026-09-09', '2026-09-10'):
            self.write_board(day, 4, {('AAAUSDT', 'up'): 100.0})
            self.write_board(day, LAST_NODE_SEQ, {('AAAUSDT', 'up'): 110.0})
        conn, reader = self.conn(), self.reader()
        now = datetime(2026, 9, 12, tzinfo=UTC)
        span = range_stats(conn, reader, ident='m', label='span',
                           start=datetime(2026, 9, 9, tzinfo=UTC), end=datetime(2026, 9, 11, tzinfo=UTC),
                           cutoff=datetime(2026, 9, 11, tzinfo=UTC), now=now)
        self.assertEqual(len(span['rows']), 1, '跨日区间内同一唯一币种只计一次')
        self.assertEqual(span['rows'][0]['enter_time_utc'], '2026-09-09T01:00:00Z')
        self.assertEqual(span['rows'][0]['repeat_entries'], 1)
        # 两个独立区间各自执行首次入选与去重 —— 各得一行。
        for day, start in (('2026-09-09', 9), ('2026-09-10', 10)):
            one = range_stats(conn, reader, ident=day, label=day,
                              start=datetime(2026, 9, start, tzinfo=UTC),
                              end=datetime(2026, 9, start + 1, tzinfo=UTC),
                              cutoff=datetime(2026, 9, start + 1, tzinfo=UTC), now=now)
            self.assertEqual(len(one['rows']), 1)
            self.assertEqual(one['rows'][0]['business_date'], day)

    # ---- 退出口径：完整交易日收线，不是真实退出 -------------------------
    def test_exit_is_day_close_node_even_when_symbol_left_the_pool_earlier(self):
        day = '2026-09-09'
        self.ingest([batch(day, 4, [('AAAUSDT', 'LONG', 'aaa')])])
        self.write_board(day, 4, {('AAAUSDT', 'up'): 100.0})
        self.write_board(day, 20, {('AAAUSDT', 'up'): 90.0})   # 真实退出候选池的时点
        self.write_board(day, LAST_NODE_SEQ, {('AAAUSDT', 'up'): 120.0})
        out = range_stats(self.conn(), self.reader(), ident='m', label='d', start=datetime(2026, 9, 9, tzinfo=UTC),
                          end=datetime(2026, 9, 10, tzinfo=UTC), cutoff=datetime(2026, 9, 10, tzinfo=UTC),
                          now=datetime(2026, 9, 11, tzinfo=UTC))
        row = out['rows'][0]
        self.assertEqual(row['exit_scan_id'], '20260909-095')
        self.assertEqual(row['exit_time_utc'], '2026-09-09T23:45:00Z')
        self.assertEqual(row['exit_price'], 120.0)
        self.assertEqual(row['enter_price'], 100.0)
        self.assertAlmostEqual(row['pnl_pct'], 0.2)
        self.assertEqual(row['pnl_sign'], 1)
        self.assertEqual(row['dwell_minutes'], 22.75 * 60)
        self.assertEqual(row['status'], 'CLOSED')
        self.assertEqual(row['flags'], [])

    def test_missing_close_snapshot_backtracks_and_says_so(self):
        day = '2026-09-09'
        self.ingest([batch(day, 4, [('AAAUSDT', 'LONG', 'aaa')])])
        self.write_board(day, 4, {('AAAUSDT', 'up'): 100.0})
        self.write_board(day, LAST_NODE_SEQ - 2, {('AAAUSDT', 'up'): 105.0})
        out = range_stats(self.conn(), self.reader(), ident='m', label='d', start=datetime(2026, 9, 9, tzinfo=UTC),
                          end=datetime(2026, 9, 10, tzinfo=UTC), cutoff=datetime(2026, 9, 10, tzinfo=UTC),
                          now=datetime(2026, 9, 11, tzinfo=UTC))
        row = out['rows'][0]
        self.assertIn('EXIT_PRICE_PREV_NODE', row['flags'])
        self.assertEqual(row['exit_scan_id'], '20260909-093')
        self.assertEqual(row['exit_time_utc'], '2026-09-09T23:15:00Z')
        self.assertAlmostEqual(row['pnl_pct'], 0.05)

    def test_no_close_price_is_missing_not_zero(self):
        day = '2026-09-09'
        self.ingest([batch(day, 4, [('AAAUSDT', 'LONG', 'aaa')])])
        self.write_board(day, 4, {('AAAUSDT', 'up'): 100.0})
        out = range_stats(self.conn(), self.reader(), ident='m', label='d', start=datetime(2026, 9, 9, tzinfo=UTC),
                          end=datetime(2026, 9, 10, tzinfo=UTC), cutoff=datetime(2026, 9, 10, tzinfo=UTC),
                          now=datetime(2026, 9, 11, tzinfo=UTC))
        row = out['rows'][0]
        self.assertIsNone(row['exit_price'])
        self.assertIsNone(row['pnl_pct'])
        self.assertIsNone(row['pnl_sign'])
        self.assertIn('MISSING_EXIT_PRICE', row['flags'])
        self.assertEqual(out['summary']['trades'], 1)
        self.assertEqual(out['summary']['usable'], 0)
        self.assertEqual(out['summary']['missing_px'], 1)

    def test_missing_entry_price_is_flagged_and_excluded_from_pnl(self):
        day = '2026-09-09'
        self.ingest([batch(day, 4, [('AAAUSDT', 'LONG', 'aaa')])])
        self.write_board(day, LAST_NODE_SEQ, {('AAAUSDT', 'up'): 120.0})
        out = range_stats(self.conn(), self.reader(), ident='m', label='d', start=datetime(2026, 9, 9, tzinfo=UTC),
                          end=datetime(2026, 9, 10, tzinfo=UTC), cutoff=datetime(2026, 9, 10, tzinfo=UTC),
                          now=datetime(2026, 9, 11, tzinfo=UTC))
        row = out['rows'][0]
        self.assertIsNone(row['enter_price'])
        self.assertIn('MISSING_ENTRY_PRICE', row['flags'])
        self.assertIsNone(row['pnl_pct'])
        self.assertEqual(out['summary']['missing_px'], 1)

    def test_unfinished_business_day_is_open_never_a_complete_day_statistic(self):
        day = '2026-09-09'
        self.ingest([batch(day, 4, [('AAAUSDT', 'LONG', 'aaa')])])
        self.write_board(day, 4, {('AAAUSDT', 'up'): 100.0})
        self.write_board(day, LAST_NODE_SEQ, {('AAAUSDT', 'up'): 120.0})
        out = range_stats(self.conn(), self.reader(), ident='m', label='d', start=datetime(2026, 9, 9, tzinfo=UTC),
                          end=datetime(2026, 9, 10, tzinfo=UTC), cutoff=datetime(2026, 9, 9, 12, tzinfo=UTC),
                          now=datetime(2026, 9, 9, 12, tzinfo=UTC))
        row = out['rows'][0]
        self.assertEqual(row['status'], 'OPEN')
        self.assertFalse(row['day_complete'])
        self.assertIn('INCOMPLETE_DAY', row['flags'])
        self.assertIsNone(row['pnl_pct'])
        self.assertIsNone(row['exit_time_utc'])
        self.assertEqual(out['summary']['trades'], 0, '未完成日不得进入完整日统计')
        self.assertEqual(out['summary']['open_trades'], 1)
        self.assertEqual(out['incomplete_days'], ['2026-09-09'])

    # ---- 多空方向 -------------------------------------------------------
    def test_short_pool_pnl_is_direction_aware(self):
        day = '2026-09-09'
        self.ingest([batch(day, 4, [('UPUSDT', 'LONG', 'up1'), ('DNUSDT', 'SHORT', 'dn1')])])
        self.write_board(day, 4, {('UPUSDT', 'up'): 100.0, ('DNUSDT', 'down'): 200.0})
        self.write_board(day, LAST_NODE_SEQ, {('UPUSDT', 'up'): 90.0, ('DNUSDT', 'down'): 180.0})
        out = range_stats(self.conn(), self.reader(), ident='m', label='d', start=datetime(2026, 9, 9, tzinfo=UTC),
                          end=datetime(2026, 9, 10, tzinfo=UTC), cutoff=datetime(2026, 9, 10, tzinfo=UTC),
                          now=datetime(2026, 9, 11, tzinfo=UTC))
        rows = {r['symbol']: r for r in out['rows']}
        self.assertEqual(rows['UPUSDT']['direction'], 'up')
        self.assertAlmostEqual(rows['UPUSDT']['pnl_pct'], -0.1)
        self.assertEqual(rows['UPUSDT']['pnl_sign'], -1)
        self.assertEqual(rows['DNUSDT']['direction'], 'down')
        self.assertEqual(rows['DNUSDT']['pool'], 'SHORT')
        self.assertAlmostEqual(rows['DNUSDT']['pnl_pct'], 0.1, places=12)
        self.assertEqual(rows['DNUSDT']['pnl_sign'], 1)
        self.assertEqual(out['summary']['up'], 1)
        self.assertEqual(out['summary']['down'], 1)
        self.assertEqual(out['summary']['win'], 1)
        self.assertEqual(out['summary']['loss'], 1)
        self.assertEqual(out['summary']['win_rate'], 0.5)

    # ---- 空数据 / 范围校验 ----------------------------------------------
    def test_empty_range_is_empty_not_an_error(self):
        self.ingest([batch('2026-09-09', 4, [('AAAUSDT', 'LONG', 'aaa')])])
        out = range_stats(self.conn(), self.reader(), ident='m', label='d', start=datetime(2026, 9, 1, tzinfo=UTC),
                          end=datetime(2026, 9, 2, tzinfo=UTC), cutoff=datetime(2026, 9, 2, tzinfo=UTC),
                          now=datetime(2026, 9, 11, tzinfo=UTC))
        self.assertEqual(out['rows'], [])
        self.assertEqual(out['days_with_commits'], [])
        self.assertEqual(out['summary']['trades'], 0)
        self.assertIsNone(out['summary']['win_rate'])

    def test_range_limits_are_rejected_loudly(self):
        conn, reader = None, self.reader()
        with self.assertRaises(OnlyCoinStatsError):
            range_stats(conn, reader, ident='m', label='d', start=datetime(2026, 9, 9, tzinfo=UTC),
                        end=datetime(2026, 9, 9, tzinfo=UTC), cutoff=datetime(2026, 9, 9, tzinfo=UTC),
                        now=datetime(2026, 9, 11, tzinfo=UTC))
        with self.assertRaises(OnlyCoinStatsError):
            range_stats(conn, reader, ident='m', label='d', start=datetime(2026, 1, 1, tzinfo=UTC),
                        end=datetime(2026, 9, 9, tzinfo=UTC), cutoff=datetime(2026, 9, 9, tzinfo=UTC),
                        now=datetime(2026, 9, 11, tzinfo=UTC))

    def test_snapshot_budget_refuses_instead_of_hanging(self):
        day = '2026-09-09'
        self.ingest([batch(day, s, [(f'S{s}USDT', 'LONG', f's{s}')]) for s in range(4, 12)])
        for s in range(4, 12):
            self.write_board(day, s, {(f'S{s}USDT', 'up'): 10.0})
        reader = SnapshotReader(self.snapshots, budget=3)
        with self.assertRaises(OnlyCoinStatsError):
            range_stats(self.conn(), reader, ident='m', label='d', start=datetime(2026, 9, 9, tzinfo=UTC),
                        end=datetime(2026, 9, 10, tzinfo=UTC), cutoff=datetime(2026, 9, 10, tzinfo=UTC),
                        now=datetime(2026, 9, 11, tzinfo=UTC))

    # ---- 整体载荷 -------------------------------------------------------
    def test_build_stats_main_default_range_is_the_replay_business_day(self):
        day = '2026-09-09'
        self.ingest([batch(day, 4, [('AAAUSDT', 'LONG', 'aaa')])])
        self.write_board(day, 4, {('AAAUSDT', 'up'): 100.0})
        self.write_board(day, LAST_NODE_SEQ, {('AAAUSDT', 'up'): 120.0})
        body = build_stats(ledger_conn=self.conn(), snapshots=self.snapshots, business_date=day,
                           as_of=datetime(2026, 9, 9, 23, 59, 59, tzinfo=UTC),
                           compare=[('c1', datetime(2026, 9, 9, tzinfo=UTC), datetime(2026, 9, 9, 12, tzinfo=UTC))],
                           now=datetime(2026, 9, 11, tzinfo=UTC))
        self.assertEqual(body['main']['from'], '2026-09-09T00:00:00Z')
        self.assertEqual(body['main']['to'], '2026-09-10T00:00:00Z')
        self.assertEqual(body['main']['cutoff'], '2026-09-09T23:59:59Z')
        self.assertEqual(len(body['main']['rows']), 1)
        self.assertEqual(body['compare'][0]['summary']['trades'], 1)
        self.assertNotIn('rows', body['compare'][0], '对比列只出汇总，不重复画明细')
        self.assertEqual(body['pnl_basis']['excluded'][:3], ['仓位', '数量', '本金'])

    def test_build_stats_custom_range_overrides_the_day(self):
        self.ingest([
            batch('2026-09-09', 4, [('AAAUSDT', 'LONG', 'aaa')]),
            batch('2026-09-10', 4, [('BBBUSDT', 'LONG', 'bbb')]),
        ])
        for day, sym in (('2026-09-09', 'AAAUSDT'), ('2026-09-10', 'BBBUSDT')):
            self.write_board(day, 4, {(sym, 'up'): 100.0})
            self.write_board(day, LAST_NODE_SEQ, {(sym, 'up'): 110.0})
        body = build_stats(ledger_conn=self.conn(), snapshots=self.snapshots, business_date='2026-09-09',
                           as_of=datetime(2026, 9, 9, 23, 59, 59, tzinfo=UTC),
                           custom=(datetime(2026, 9, 10, tzinfo=UTC), datetime(2026, 9, 11, tzinfo=UTC)),
                           now=datetime(2026, 9, 12, tzinfo=UTC))
        self.assertEqual(body['main']['label'], '自定义起止')
        self.assertEqual([r['symbol'] for r in body['main']['rows']], ['BBBUSDT'])


class OnlyCoinStatsGatewayTests(unittest.TestCase):
    """查询校验与功能开关 —— 网关层。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.module = importlib.import_module('onlycoin_api')
        from unittest.mock import patch
        self.patch = patch.object(self.module, 'ROOT', Path(self.tmp.name))
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def q(self, **kw):
        return {k: (v if isinstance(v, list) else [v]) for k, v in kw.items()}

    def test_query_validation(self):
        good = dict(board='y', business_date='2026-09-09', as_of='2026-09-09T12:00:00Z')
        for bad in (
            self.q(**{**good, 'board': 'main'}),
            self.q(business_date='2026-09-09', as_of='2026-09-09T12:00:00Z'),
            self.q(board='y', as_of='2026-09-09T12:00:00Z'),
            self.q(board='y', business_date='2026-09-09'),
            self.q(**{**good, 'as_of': '2026-09-10T00:00:00Z'}),
            self.q(**{**good, 'as_of': '2026-09-09T12:00:00'}),
            self.q(**{**good, 'from': '2026-09-09T00:00:00Z'}),
            self.q(**{**good, 'to': '2026-09-09T00:00:00Z'}),
            self.q(**{**good, 'from': '2026-09-09T06:00:00Z', 'to': '2026-09-09T06:00:00Z'}),
            self.q(**{**good, 'compare': 'not-a-range'}),
            self.q(**{**good, 'compare': ['a~b~c']}),
            self.q(**{**good, 'compare': ['2026-09-09T00:00:00Z~2026-09-09T00:00:00Z']}),
            # 超过 MAX_COMPARE_RANGES（=3）列
            self.q(**{**good, 'compare': ['2026-09-0%dT00:00:00Z~2026-09-0%dT12:00:00Z' % (i, i) for i in range(1, 6)]}),
            self.q(**{**good, 'dataset_id': 'archive'}),
            self.q(**{**good, 'board': ['y', 'y']}),
        ):
            code, body = self.module.stats_payload(bad)
            self.assertEqual(code, 400, bad)
            self.assertEqual(body['error'], 'INVALID_ONLYCOIN_STATS_QUERY')
        code, body = self.module.stats_payload(self.q(**good))
        self.assertEqual(code, 200)
        self.assertEqual(body['main']['rows'], [])
        self.assertEqual(body['schema'], 'onlycoin-stats-v1')

    def test_feature_flag_off_returns_404_without_touching_data(self):
        import os
        from unittest.mock import patch
        for value in ('off', 'OFF', '0', 'false', 'no'):
            with patch.dict(os.environ, {self.module.STATS_FLAG_ENV: value}):
                code, body = self.module.stats_payload(
                    self.q(board='y', business_date='2026-09-09', as_of='2026-09-09T12:00:00Z'))
                self.assertEqual(code, 404)
                self.assertEqual(body['error'], 'ONLYCOIN_STATS_DISABLED')
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [], 'disabled route must not create data')


if __name__ == '__main__':
    unittest.main()
