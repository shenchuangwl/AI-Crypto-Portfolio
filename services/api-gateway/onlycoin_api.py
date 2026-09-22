"""Read-only Y payloads; history selects one dataset, daily stays live-only."""
from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

CODE_ROOT = Path(__file__).resolve().parents[2]
ROOT = Path(os.environ.get('HERMES_ROOT', str(CODE_ROOT))).resolve()
SRC = CODE_ROOT / 'services/coin-selection/src'
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
from coin_selection.onlycoin_ledger import OnlyCoinLedger, business_day, timestamp

#: 《OnlyCoin · 来源候选回放》历史统计的功能开关。设为 off / 0 / false / no 时
#: 路由整体退回 404，前端面板同步隐藏 —— 无需回滚代码即可停用新功能。
STATS_FLAG_ENV = 'ONLYCOIN_STATS'


def stats_enabled() -> bool:
    return os.environ.get(STATS_FLAG_ENV, 'on').strip().lower() not in ('off', '0', 'false', 'no')


def live_payload(qs):
    """Current read-only daily membership, never a historical or trading feed.

    A lease ends at the earliest of midnight, commit+30m and scan+30m.
    The scan bound prevents a late re-publication reviving an old scan.
    """
    version = 'param-v2.0.0-screener-y'
    empty = dict(board_key='y', schema='onlycoin-live-v1', version=version,
                 valid=False, status='unavailable', stale=True, symbols=[],
                 onlycoin=[], long=[], short=[], counts={'onlycoin': 0, 'long': 0, 'short': 0},
                 updated_at_utc=None, valid_until_utc=None,
                 dmr_executable=False, consumable_by_dmr=False)
    if qs:
        return 400, dict(empty, status='invalid_query', error='LIVE_ENDPOINT_ACCEPTS_NO_QUERY')
    code, data = daily_payload({})
    if code != 200:
        return 503, dict(empty, error='ONLYCOIN_UNAVAILABLE')
    try:
        now = timestamp(data['server_time'])
        updated = timestamp(data['last_committed_at'])
        start, end = timestamp(data['cycle_start_utc']), timestamp(data['cycle_end_utc'])
        sid = data['as_of_scan_id']
        scan_day, seq = sid.split('-')
        if len(scan_day) != 8 or len(seq) != 3 or not 0 <= int(seq) < 96:
            raise ValueError('invalid scan')
        scan = datetime.strptime(scan_day, '%Y%m%d').replace(tzinfo=timezone.utc) + timedelta(minutes=15 * int(seq))
        expiry = min(end, updated + timedelta(minutes=30), scan + timedelta(minutes=30))
        valid = (data['board_key'] == 'y' and data['dataset_id'] == 'live'
                 and data['business_date'] == now.date().isoformat()
                 and start <= scan <= now < expiry and start <= updated <= now
                 and data['stale'] is False
                 and all(m.get('parameter_version') == version for m in data['onlycoin']))
        metadata = dict(data, schema=empty['schema'], version=version,
                        updated_at_utc=data['last_committed_at'],
                        valid_until_utc=expiry.isoformat().replace('+00:00', 'Z'))
        if not valid:
            return 503, {**metadata, **{k: v for k, v in empty.items() if k not in ('updated_at_utc', 'valid_until_utc')}, 'status': 'stale'}
        return 200, dict(metadata, valid=True, status='live' if data['onlycoin'] else 'empty',
                         symbols=[m['symbol'] for m in data['onlycoin']])
    except (KeyError, ValueError, TypeError, AttributeError):
        return 503, dict(empty, error='INVALID_LIVE_PROJECTION')


def daily_payload(qs, *, historical=False):
    """Return (HTTP status, JSON object); qs is urllib.parse.parse_qs output."""
    try:
        def one(name):
            value = qs.get(name)
            if value is None:
                return None
            if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], str) or not value[0]:
                raise ValueError(f'{name} requires one nonempty value')
            return value[0]

        board = one('board')
        if board not in (None, 'y') or historical and board != 'y':
            raise ValueError('OnlyCoin history requires board=y')
        day, date_alias, as_of = one('day'), one('business_date'), one('as_of')
        if day and date_alias and day != date_alias:
            raise ValueError('day and business_date disagree')
        day = day or date_alias
        if historical and day is None:
            raise ValueError('history requires day or business_date')
        if not historical and day is not None:
            if day != datetime.now(timezone.utc).date().isoformat():
                raise ValueError('use historical endpoint for another UTC day')
        start = business_day(day) if day else None
        if as_of:
            cutoff = timestamp(as_of)
            if start is None:
                start = business_day(datetime.now(timezone.utc).date().isoformat())
            if not start <= cutoff < start + timedelta(days=1):
                raise ValueError('as_of must belong to requested UTC business day')
        dataset_id = one('dataset_id') or ('auto' if historical else 'live')
        if dataset_id not in ('archive', 'live', 'auto'):
            raise ValueError('dataset_id must be archive, live or auto')
        if not historical and dataset_id != 'live':
            raise ValueError('daily endpoint requires live dataset')
        for unsupported in ('cursor', 'version'):
            if unsupported in qs:
                raise ValueError(f'{unsupported} is not supported by this dataset endpoint')
    except (ValueError, TypeError) as exc:
        return 400, {'error': 'INVALID_ONLYCOIN_QUERY', 'detail': str(exc), 'board_key': 'y'}
    review = ROOT / 'data/coin-selection-y/review'
    path = review / 'onlycoin.sqlite'
    try:
        if dataset_id == 'auto':
            with OnlyCoinLedger(path, readonly=True) as live:
                # Inspect day commits before as_of filtering: never substitute
                # archive evidence for a live day not yet available at cutoff.
                dataset_id = 'live' if live.has_day(day) else 'archive'
                if dataset_id == 'live':
                    return 200, dict(live.daily(day, as_of=as_of), dataset_id=dataset_id)
        if dataset_id == 'archive':
            path = review / 'onlycoin-archive.sqlite'
        with OnlyCoinLedger(path, readonly=True) as ledger:
            return 200, dict(ledger.daily(day, as_of=as_of), dataset_id=dataset_id)
    except (sqlite3.Error, OSError, ValueError) as exc:
        return 503, {'error': 'ONLYCOIN_UNAVAILABLE', 'detail': str(exc), 'board_key': 'y', 'stale': True}


def stats_payload(qs):
    """《OnlyCoin · 来源候选回放》DMR 区历史统计。返回 (HTTP 状态, JSON 对象)。

    成员只取 OnlyCoin 独立账本（与上方三列名单同一投影），价格只取板面快照打印价。
    本函数不打开 ``review/ledger.sqlite``，因此不可能套用原账本的成交结果。
    """
    from coin_selection.onlycoin_stats import (
        MAX_COMPARE_RANGES,
        OnlyCoinStatsError,
        build_stats,
        parse_ts,
    )

    if not stats_enabled():
        return 404, {'error': 'ONLYCOIN_STATS_DISABLED', 'board_key': 'y',
                     'hint': f'unset {STATS_FLAG_ENV} (or set it to "on") to enable the replay statistics'}
    try:
        def one(name):
            value = qs.get(name)
            if value is None:
                return None
            if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], str) or not value[0]:
                raise ValueError(f'{name} requires one nonempty value')
            return value[0]

        if one('board') != 'y':
            raise ValueError('OnlyCoin statistics require board=y')
        day = one('business_date')
        if not day:
            raise ValueError('business_date is required')
        start = business_day(day)
        as_of_raw = one('as_of')
        if not as_of_raw:
            raise ValueError('as_of is required')
        as_of = timestamp(as_of_raw)
        if not start <= as_of < start + timedelta(days=1):
            raise ValueError('as_of must belong to requested UTC business day')

        # 自定义起止：两端必须同时给，且必须是显式 UTC。半填不成立，也绝不静默回落。
        cf, ct = one('from'), one('to')
        if bool(cf) != bool(ct):
            raise ValueError('custom range requires both from and to')
        custom = (parse_ts(cf), parse_ts(ct)) if cf and ct else None
        if custom and custom[1] <= custom[0]:
            raise ValueError('custom range end must be after start')

        compare = []
        raw_compare = qs.get('compare') or []
        if not isinstance(raw_compare, list):
            raise ValueError('compare must be repeated from~to values')
        if len(raw_compare) > MAX_COMPARE_RANGES:
            raise ValueError(f'at most {MAX_COMPARE_RANGES} compare ranges')
        for i, raw in enumerate(raw_compare):
            if not isinstance(raw, str) or raw.count('~') != 1:
                raise ValueError('compare value must be <fromISO>~<toISO>')
            a, _, b = raw.partition('~')
            ca, cb = parse_ts(a), parse_ts(b)
            if cb <= ca:
                raise ValueError('compare range end must be after start')
            compare.append((f'c{i + 1}', ca, cb))

        for unsupported in ('cursor', 'version', 'dataset_id'):
            if unsupported in qs:
                raise ValueError(f'{unsupported} is not supported by this endpoint')
    except (ValueError, TypeError, OnlyCoinStatsError) as exc:
        return 400, {'error': 'INVALID_ONLYCOIN_STATS_QUERY', 'detail': str(exc), 'board_key': 'y'}

    review = ROOT / 'data/coin-selection-y/review'
    snapshots = ROOT / 'data/coin-selection-y/snapshots'
    try:
        # 数据集选择与 /review/onlycoin 同规则：该业务日有 live 提交就用 live，
        # 否则读档案库。绝不把档案证据替换成一个当刻还不存在的 live 日。
        with OnlyCoinLedger(review / 'onlycoin.sqlite', readonly=True) as live:
            dataset_id = 'live' if live.has_day(day) else 'archive'
            if dataset_id == 'live':
                body = build_stats(ledger_conn=live.conn, snapshots=snapshots, business_date=day,
                                   as_of=as_of, custom=custom, compare=compare)
                return 200, dict(body, dataset_id=dataset_id)
        with OnlyCoinLedger(review / 'onlycoin-archive.sqlite', readonly=True) as archive:
            body = build_stats(ledger_conn=archive.conn, snapshots=snapshots, business_date=day,
                               as_of=as_of, custom=custom, compare=compare)
            return 200, dict(body, dataset_id='archive')
    except OnlyCoinStatsError as exc:
        return 400, {'error': 'ONLYCOIN_STATS_RANGE_REJECTED', 'detail': str(exc), 'board_key': 'y'}
    except (sqlite3.Error, OSError, ValueError) as exc:
        return 503, {'error': 'ONLYCOIN_STATS_UNAVAILABLE', 'detail': str(exc), 'board_key': 'y', 'stale': True}
