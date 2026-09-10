"""Isolated, append-only post-TopK OnlyCoin facts; never an occupancy/trading ledger.

Each database is one provenance dataset. Original batch JSON is retained intact.
Archive availability is a conservative generation-time PROXY, not publication proof.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

UTC = timezone.utc
SOURCE_ID = 'screener-y.onlycoin'
SCHEMA = 'onlycoin-ledger-v1'


def timestamp(value):
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})', value):
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    else:
        raise ValueError('timestamp must be timezone-aware ISO 8601')
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError('timestamp timezone required')
    return result.astimezone(UTC)


def iso(value):
    return value.astimezone(UTC).isoformat().replace('+00:00', 'Z')


def business_day(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise ValueError('day must be YYYY-MM-DD')
    return datetime.strptime(value, '%Y-%m-%d').replace(tzinfo=UTC)


def scan_time(scan_id):
    if not isinstance(scan_id, str) or not re.fullmatch(r'\d{8}-\d{3}', scan_id):
        raise ValueError('invalid scan_id')
    seq = int(scan_id[-3:])
    if seq > 95:
        raise ValueError('scan sequence outside UTC day')
    return datetime.strptime(scan_id[:8], '%Y%m%d').replace(tzinfo=UTC) + timedelta(minutes=15 * seq)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


class OnlyCoinLedger:
    def __init__(self, path, readonly=False):
        self.path = Path(path)
        self.readonly = readonly
        if self.path.name == 'ledger.sqlite':
            raise ValueError('OnlyCoin must not open the old occupancy ledger')
        if readonly and not self.path.exists():
            self.conn = None
            return
        if readonly:
            self.conn = sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True)
            self.conn.execute('PRAGMA query_only=ON')
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(self.path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        tables = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables and 'onlycoin_datasets' not in tables:
            self.close()
            raise ValueError('target is not an OnlyCoin database')
        if 'onlycoin_datasets' in tables:
            versions = self.conn.execute('SELECT schema_version FROM onlycoin_datasets').fetchall()
            if any(r[0] != SCHEMA for r in versions):
                self.close()
                raise ValueError('unsupported OnlyCoin schema version')
        if not readonly:
            self.conn.executescript('''
                CREATE TABLE IF NOT EXISTS onlycoin_datasets (
                    id INTEGER PRIMARY KEY CHECK(id=1), schema_version TEXT NOT NULL,
                    provenance TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS onlycoin_scan_commits (
                    commit_seq INTEGER PRIMARY KEY AUTOINCREMENT, scan_id TEXT UNIQUE NOT NULL,
                    business_date TEXT NOT NULL, selected_at TEXT NOT NULL,
                    available_at TEXT NOT NULL, provenance TEXT NOT NULL,
                    source_batch_hash TEXT NOT NULL, batch_json TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS onlycoin_day ON onlycoin_scan_commits(business_date);
                CREATE TRIGGER IF NOT EXISTS onlycoin_commits_no_update
                    BEFORE UPDATE ON onlycoin_scan_commits BEGIN SELECT RAISE(ABORT,'immutable scan'); END;
                CREATE TRIGGER IF NOT EXISTS onlycoin_commits_no_delete
                    BEFORE DELETE ON onlycoin_scan_commits BEGIN SELECT RAISE(ABORT,'immutable scan'); END;
                CREATE TRIGGER IF NOT EXISTS onlycoin_dataset_no_update
                    BEFORE UPDATE ON onlycoin_datasets BEGIN SELECT RAISE(ABORT,'immutable dataset'); END;
                CREATE TRIGGER IF NOT EXISTS onlycoin_dataset_no_delete
                    BEFORE DELETE ON onlycoin_datasets BEGIN SELECT RAISE(ABORT,'immutable dataset'); END;
            ''')
        else:
            self.conn.execute('SELECT schema_version FROM onlycoin_datasets').fetchall()

    def close(self):
        if self.conn is not None:
            self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def ingest_batch(self, batch, *, provenance='observed_archive', available_at=None):
        if self.readonly or self.conn is None:
            raise PermissionError('read-only OnlyCoin ledger')
        raw = canonical(batch)
        digest = hashlib.sha256(raw.encode()).hexdigest()
        sid = batch['scan_id']
        selected = scan_time(sid)
        candidates = batch.get('candidates')
        if batch.get('board_key') != 'y' or not isinstance(candidates, list):
            raise ValueError('expected board y final candidates batch')
        if provenance == 'live_committed' and batch.get('parameter_version') != 'param-v2.0.0-screener-y':
            raise ValueError('live batch requires param-v2.0.0-screener-y')
        generation = [selected]
        for obj in [batch] + candidates:
            if obj.get('generated_at_utc') is not None:
                generation.append(timestamp(obj['generated_at_utc']))
        seen_symbols = set()
        for c in candidates:
            if provenance == 'live_committed':
                if c.get('state') != 'CONFIRMED' or c.get('dmr_selected') is not True:
                    raise ValueError('live candidates must be CONFIRMED and explicitly selected')
                if c.get('parameter_version', batch.get('parameter_version')) != 'param-v2.0.0-screener-y':
                    raise ValueError('live candidate requires param-v2.0.0-screener-y')
            if c.get('exchange', 'binance') != 'binance' or c.get('market_type', 'usdtm_perpetual') != 'usdtm_perpetual':
                raise ValueError('unsupported instrument market')
            if c.get('symbol') in seen_symbols:
                raise ValueError('post-TopK batch must deduplicate symbol directions')
            seen_symbols.add(c.get('symbol'))
            symbol = c.get('symbol')
            if (not isinstance(symbol, str) or not symbol.endswith('USDT') or len(symbol) <= 4
                    or not symbol.isalnum() or symbol != symbol.upper()):
                raise ValueError('invalid Binance USDT contract symbol; no normalization')
            if c.get('direction') not in ('LONG', 'SHORT'):
                raise ValueError('invalid direction')
            if c.get('scan_id', sid) != sid or c.get('board_key', 'y') != 'y':
                raise ValueError('candidate source mismatch')
            if c.get('dmr_selected', True) is not True:
                raise ValueError('candidates must be post-TopK selected')
            if c.get('scan_timestamp_utc') and timestamp(c['scan_timestamp_utc']) != selected:
                raise ValueError('candidate logical scan time mismatch')
        if provenance not in ('observed_archive', 'live_committed', 'counterfactual_replay'):
            raise ValueError('invalid provenance')
        # Exact retries recover the original commit timestamp (not a new wall clock).
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            previous = self.conn.execute('SELECT * FROM onlycoin_scan_commits WHERE scan_id=?', (sid,)).fetchone()
            if previous:
                if previous['source_batch_hash'] != digest or previous['provenance'] != provenance:
                    raise ValueError('changed same scan; use a separate correction dataset')
                if provenance != 'live_committed' and available_at is not None and iso(timestamp(available_at)) != previous['available_at']:
                    raise ValueError('changed same scan availability')
                self.conn.rollback()
                return {'status': 'duplicate', 'scan_id': sid, 'commit_seq': previous['commit_seq'], 'event_count': len(candidates), 'idempotent': True}
            available = timestamp(available_at) if available_at is not None else (
                datetime.now(UTC) if provenance == 'live_committed' else max(generation))
            if available < max(generation):
                raise ValueError('available_at precedes selection/generation')
            dataset = self.conn.execute('SELECT * FROM onlycoin_datasets').fetchone()
            if dataset and dataset['provenance'] != provenance:
                raise ValueError('provenance datasets must remain isolated')
            latest = self.conn.execute('SELECT * FROM onlycoin_scan_commits ORDER BY commit_seq DESC LIMIT 1').fetchone()
            if latest and (sid <= latest['scan_id'] or available < timestamp(latest['available_at'])):
                raise ValueError('out-of-order scan or availability; rebuild a new dataset')
            if not dataset:
                self.conn.execute('INSERT INTO onlycoin_datasets VALUES (1,?,?)', (SCHEMA, provenance))
            cur = self.conn.execute('INSERT INTO onlycoin_scan_commits(scan_id,business_date,selected_at,available_at,provenance,source_batch_hash,batch_json) VALUES (?,?,?,?,?,?,?)',
                                    (sid, selected.date().isoformat(), iso(selected), iso(available), provenance, digest, raw))
            self.conn.commit()
            return {'status': 'ok', 'scan_id': sid, 'commit_seq': cur.lastrowid, 'event_count': len(candidates), 'idempotent': False}
        except Exception:
            self.conn.rollback()
            raise

    def has_day(self, day):
        """Whether this store has any day commits, including empty/late batches."""
        day = business_day(day).date().isoformat()
        return self.conn is not None and self.conn.execute(
            'SELECT 1 FROM onlycoin_scan_commits WHERE business_date=? LIMIT 1',
            (day,)).fetchone() is not None

    def daily(self, day=None, *, as_of=None, now=None):
        clock = timestamp(now) if now is not None else datetime.now(UTC)
        start = business_day(day if day is not None else clock.date().isoformat())
        end = start + timedelta(days=1)
        cutoff = timestamp(as_of) if as_of is not None else clock
        # A single SELECT materializes the committed projection at one SQLite snapshot.
        rows = self.conn.execute('SELECT * FROM onlycoin_scan_commits WHERE business_date=? ORDER BY commit_seq', (start.date().isoformat(),)).fetchall() if self.conn is not None else []
        rows = [r for r in rows if timestamp(r['available_at']) <= cutoff]
        members, longs, shorts = {}, {}, {}
        identities = set()
        for r in rows:
            batch = json.loads(r['batch_json'])
            for ordinal, c in enumerate(batch['candidates']):
                symbol, direction = c['symbol'], c['direction']
                identity = c.get('rule_identity', batch.get('rule_identity'))
                identities.add(canonical(identity))
                member_id = f'{SOURCE_ID}:{start.date().isoformat()}:binance:usdtm_perpetual:{symbol}'
                first = dict(c)
                first.update(member_id=member_id, symbol=symbol,
                             first_selected_at=r['selected_at'], first_available_at=r['available_at'],
                             first_scan_id=r['scan_id'], first_direction=direction, directions=[direction],
                             parameter_version=c.get('parameter_version', batch.get('parameter_version')),
                             rule_identity=identity, provenance=r['provenance'], first_ordinal=ordinal,
                             availability_quality='COMMITTED' if r['provenance'] == 'live_committed' else 'PROXY')
                if symbol not in members:
                    members[symbol] = first
                elif direction not in members[symbol]['directions']:
                    members[symbol]['directions'].append(direction)
                side = longs if direction == 'LONG' else shorts
                if symbol not in side:
                    side[symbol] = dict(first, directions=[direction])
        slots = {int(r['scan_id'][-3:]) for r in rows}
        horizon = min(cutoff, clock, end - timedelta(microseconds=1))
        expected = min(96, max(0, int((horizon - start).total_seconds() // 900) + 1))
        missing = [f'{start:%Y%m%d}-{i:03d}' for i in range(expected) if i not in slots]
        quality = 'COMMITTED' if rows and all(r['provenance'] == 'live_committed' for r in rows) else 'PROXY' if rows else 'UNAVAILABLE'
        flags = []
        if quality == 'PROXY':
            flags.append('ARCHIVE_AVAILABILITY_PROXY')
        if len(identities) > 1:
            flags.append('MIXED_IDENTITY_DAY')
        if rows and any(json.loads(r['batch_json']).get('rule_identity') is None for r in rows):
            flags.append('MISSING_IDENTITY')
        return {'board_key': 'y', 'source_id': SOURCE_ID, 'schema': SCHEMA,
                'business_date': start.date().isoformat(), 'cycle_start_utc': iso(start),
                'cycle_end_utc': iso(end), 'server_time': iso(clock),
                'projection_revision': rows[-1]['commit_seq'] if rows else 0,
                'as_of_scan_id': rows[-1]['scan_id'] if rows else None,
                'last_committed_at': rows[-1]['available_at'] if rows else None,
                'long': list(longs.values()), 'short': list(shorts.values()), 'onlycoin': list(members.values()),
                'counts': {'long': len(longs), 'short': len(shorts), 'onlycoin': len(members)},
                'coverage': {'observed_scans': len(rows), 'expected_scans': expected,
                             'missing_scan_ids': missing, 'partial': bool(missing) or not rows,
                             'availability_quality': quality, 'flags': flags,
                             'strict_causal_certified': quality == 'COMMITTED' and not missing and not flags},
                'stale': not rows or (start.date() == clock.date() and clock - timestamp(rows[-1]['available_at']) > timedelta(minutes=30)),
                'dmr_executable': False, 'consumable_by_dmr': False}
