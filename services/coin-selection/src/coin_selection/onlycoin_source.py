"""Durable OnlyCoin supply control. No execution or trading dependencies."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

ROOT = Path(__file__).resolve().parents[4]
SOURCE_ID = 'screener-y.onlycoin'


class SourceConflict(ValueError):
    """CAS or idempotency conflict; gateway should return HTTP 409."""


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


@contextmanager
def _db(root):
    path = Path(root) / 'data/coin-selection-y/onlycoin-source/control.sqlite'
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript('''
          CREATE TABLE IF NOT EXISTS control (
            id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL,
            revision INTEGER NOT NULL, generation INTEGER NOT NULL);
          INSERT OR IGNORE INTO control VALUES(1,0,0,0);
          CREATE TABLE IF NOT EXISTS outbox (
            snapshot_revision INTEGER PRIMARY KEY AUTOINCREMENT,
            digest TEXT NOT NULL, payload TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
            next_retry REAL NOT NULL DEFAULT 0, error TEXT);
          CREATE TABLE IF NOT EXISTS observer_registry (
            source_id TEXT NOT NULL, member_id TEXT NOT NULL, payload TEXT NOT NULL,
            valid_until REAL NOT NULL, PRIMARY KEY(source_id,member_id));
          CREATE TABLE IF NOT EXISTS observer (
            id INTEGER PRIMARY KEY CHECK(id=1), heartbeat REAL NOT NULL,
            accepted_revision INTEGER NOT NULL, generation INTEGER NOT NULL);
          CREATE TABLE IF NOT EXISTS acknowledgements (
            delivery_id TEXT PRIMARY KEY, consumer_id TEXT NOT NULL,
            generation INTEGER NOT NULL, accepted_revision INTEGER NOT NULL,
            applied_count INTEGER NOT NULL, acknowledged_at TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS audit (
            request_id TEXT PRIMARY KEY, payload TEXT NOT NULL, actor TEXT NOT NULL,
            created_at TEXT NOT NULL, result TEXT NOT NULL);
        ''')
        conn.execute('BEGIN IMMEDIATE')
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def _readonly_db(path):
    conn = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('BEGIN')
        yield conn
    finally:
        conn.close()


def _control(conn):
    row = dict(conn.execute('SELECT * FROM control WHERE id=1').fetchone())
    row.pop('id')
    row['enabled'] = bool(row['enabled'])
    return row


def source_status(root=ROOT):
    """Desired source control, not trading enablement."""
    now = _now()
    path = Path(root) / 'data/coin-selection-y/onlycoin-source/control.sqlite'
    if not path.exists():
        return dict(enabled=False, revision=0, generation=0, effective_state='OFF',
                    eligibility_only=True, consumer_connected=False,
                    consumer_kind='local_observer', consumer_id=None,
                    trading_connected=False, dmr_executable=False, disable_pending=False,
                    snapshot_revision=None, accepted_revision=None)
    # No schema initialization, journal PRAGMA, outbox publication or control writes.
    with _readonly_db(path) as conn:
        result = _control(conn)
        observer = conn.execute('SELECT * FROM observer WHERE id=1').fetchone()
        latest = conn.execute('SELECT * FROM outbox ORDER BY snapshot_revision DESC LIMIT 1').fetchone()
        connected = bool(observer and -60 <= now.timestamp() - observer['heartbeat'] <= 60)
        snapshot = json.loads(latest['payload']) if latest else None
        ack = conn.execute('SELECT * FROM acknowledgements WHERE delivery_id=?',
                           (snapshot['delivery_id'],)).fetchone() if snapshot else None
        exact = bool(observer and snapshot and ack and latest['state'] == 'acked'
                     and ack['consumer_id'] == 'local-observer'
                     and ack['generation'] == result['generation']
                     and ack['accepted_revision'] == snapshot['snapshot_revision']
                     and ack['applied_count'] == (len(snapshot['members']) if not snapshot['stale'] else 0)
                     and observer['generation'] == result['generation']
                     and observer['accepted_revision'] == snapshot['snapshot_revision']
                     and snapshot['generation'] == result['generation']
                     and snapshot['control_revision'] == result['revision'])
        current = _snapshot_body(result, now, root) if result['enabled'] else None
        fresh = bool(current and not current['stale'] and snapshot
                     and all(snapshot.get(key) == value for key, value in current.items())
                     and snapshot['snapshot_digest'] == hashlib.sha256(_json(current).encode()).hexdigest())
        state = 'OFF'
        if result['enabled']:
            state = 'ENABLING'
            if snapshot and snapshot['generation'] == result['generation']:
                if not fresh or snapshot['stale'] or _now(snapshot['valid_until']) <= now or latest['error']:
                    state = 'DEGRADED'
                elif connected and exact:
                    state = 'ON'
            if observer and not connected:
                state = 'DEGRADED'
        elif observer and not exact:
            state = 'DISABLING'
        result.update(consumer_connected=connected, consumer_kind='local_observer',
                      consumer_id='local-observer' if observer else None,
                      trading_connected=False, dmr_executable=False, eligibility_only=True,
                      effective_state=state, disable_pending=state == 'DISABLING',
                      snapshot_revision=snapshot['snapshot_revision'] if snapshot else None,
                      accepted_revision=observer['accepted_revision'] if observer else None)
    return result


def set_source_control(payload, *, root=ROOT, actor='unspecified'):
    """Authenticated caller supplies strict bool, CAS revision and request ID."""
    if not isinstance(payload, dict) or type(payload.get('enabled')) is not bool:
        raise ValueError('enabled must be a JSON boolean')
    if type(payload.get('expected_revision')) is not int or payload['expected_revision'] < 0:
        raise ValueError('expected_revision must be a nonnegative integer')
    if not isinstance(payload.get('request_id'), str) or not payload['request_id'].strip():
        raise ValueError('request_id must be a nonempty string')
    if payload.get('replay_policy', 'current_day_snapshot') != 'current_day_snapshot':
        raise ValueError('only current_day_snapshot replay is allowed')
    if set(payload) - {'enabled', 'expected_revision', 'request_id', 'replay_policy', 'reason'}:
        raise ValueError('unknown control fields')
    if not isinstance(actor, str) or not actor.strip():
        raise ValueError('actor must be nonempty')
    if 'reason' in payload and not isinstance(payload['reason'], str):
        raise ValueError('reason must be a string')
    canonical = _json(payload)
    with _db(root) as conn:
        previous = conn.execute('SELECT * FROM audit WHERE request_id=?',
                                (payload['request_id'],)).fetchone()
        if previous:
            if previous['payload'] != canonical:
                raise SourceConflict('request_id reused with different payload')
            return json.loads(previous['result'])
        before = _control(conn)
        if before['revision'] != payload['expected_revision']:
            raise SourceConflict('expected_revision does not match current revision')
        result = dict(enabled=payload['enabled'], revision=before['revision'] + 1,
                      generation=before['generation'] + 1,
                      replay_policy='current_day_snapshot')
        conn.execute('UPDATE control SET enabled=?,revision=?,generation=? WHERE id=1',
                     (int(result['enabled']), result['revision'], result['generation']))
        conn.execute("UPDATE outbox SET state='fenced' WHERE state='pending'")
        conn.execute('INSERT INTO audit VALUES(?,?,?,?,?)',
                     (payload['request_id'], canonical, actor,
                      datetime.now(timezone.utc).isoformat(), _json(result)))
        return result


def _now(value=None):
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError('now must be an ISO timestamp with timezone or aware datetime')
    return value.astimezone(timezone.utc)


def _snapshot_body(control, now, root):
    """Read and validate current facts without publishing or mutating control."""
    day = now.date().isoformat()
    end = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), timezone.utc)
    members, stale, error, projection, coverage = [], False, None, None, None
    if control['enabled']:
        try:
            from .onlycoin_ledger import OnlyCoinLedger
            ledger = OnlyCoinLedger(Path(root) / 'data/coin-selection-y/review/onlycoin.sqlite', readonly=True)
            try:
                daily = ledger.daily(now=now)
            finally:
                close = getattr(ledger, 'close', None)
                if close:
                    close()
            if not isinstance(daily, dict):
                raise ValueError('invalid daily projection')
            projection = daily.get('projection_revision')
            coverage = daily.get('coverage')
            if (daily.get('board_key') != 'y'
                    or type(projection) is not int or projection < 1
                    or daily.get('stale') is not False or daily.get('business_date') != day
                    or _now(daily['cycle_end_utc']) != end
                    or not isinstance(coverage, dict)
                    or coverage.get('availability_quality') != 'COMMITTED'
                    or coverage.get('partial') is not False
                    or coverage.get('flags') != []
                    or coverage.get('missing_scan_ids', []) != []):
                raise ValueError('unsafe daily projection')
            candidates = daily['onlycoin']
            if not isinstance(candidates, list) or not candidates:
                raise ValueError('empty or invalid members')
            identities = set()
            for member in candidates:
                if not isinstance(member, dict) or member.get('provenance') != 'live_committed':
                    raise ValueError('non-live member')
                identity = member.get('rule_identity')
                if (not isinstance(identity, dict)
                        or identity.get('parameter_version') != 'param-v2.0.0-screener-y'
                        or member.get('parameter_version') != identity['parameter_version']
                        or identity.get('identity_status') != 'MATCH'
                        or any(not isinstance(identity.get(key), str) or not identity[key].strip()
                               for key in ('rule_revision', 'config_hash', 'mapping_hash', 'code_commit'))):
                    raise ValueError('unsafe member identity')
                identities.add(_json(identity))
            if len(identities) != 1:
                raise ValueError('mixed member identities')
            _json(daily)  # Reject non-JSON and non-finite metadata before publication.
            members = candidates
        except (ImportError, OSError, sqlite3.Error, ValueError, KeyError, TypeError):
            stale, error, members = True, 'ledger_unavailable_or_invalid', []
            projection, coverage = None, None
    body = dict(schema='onlycoin-source-v1', source_id=SOURCE_ID,
                message_type='SOURCE_DISABLED' if not control['enabled'] else 'SNAPSHOT',
                enabled=control['enabled'], day=day, business_date=day,
                generation=control['generation'], control_revision=control['revision'],
                valid_until=end.isoformat(), members=members, stale=stale, error=error,
                projection_revision=projection, coverage=coverage,
                eligibility_only=True, order_instruction=False, dmr_executable=False)
    return body


def _snapshot(conn, now, root):
    control = _control(conn)
    body = _snapshot_body(control, now, root)
    digest = hashlib.sha256(_json(body).encode()).hexdigest()
    previous = conn.execute('SELECT * FROM outbox ORDER BY snapshot_revision DESC LIMIT 1').fetchone()
    if previous and previous['digest'] == digest:
        return json.loads(previous['payload'])
    conn.execute("UPDATE outbox SET state='fenced' WHERE state='pending'")
    cursor = conn.execute('INSERT INTO outbox(digest,payload) VALUES(?,?)', (digest, '{}'))
    revision = cursor.lastrowid
    body.update(snapshot_revision=revision, sequence=revision,
                delivery_id=f'{SOURCE_ID}:{control["generation"]}:{revision}', snapshot_digest=digest)
    conn.execute('UPDATE outbox SET payload=? WHERE snapshot_revision=?', (_json(body), revision))
    return body


def get_source_snapshot(*, root=ROOT, now=None):
    """Publish an atomic current-day source snapshot into the durable outbox."""
    now = _now(now)
    with _db(root) as conn:
        return _snapshot(conn, now, root)


def _apply(conn, snapshot, now):
    current = _control(conn)
    row = conn.execute('SELECT * FROM outbox ORDER BY snapshot_revision DESC LIMIT 1').fetchone()
    if (not row or json.loads(row['payload']) != snapshot
            or snapshot['generation'] != current['generation']
            or snapshot['enabled'] != current['enabled']
            or snapshot['control_revision'] != current['revision']
            or _now(snapshot['valid_until']) <= now
            or snapshot['day'] != now.date().isoformat()):
        raise SourceConflict('fenced, expired or nonexact snapshot ACK')
    # Registry replacement and exact ACK share the control transaction: OFF cannot race.
    conn.execute('DELETE FROM observer_registry WHERE source_id=?', (SOURCE_ID,))
    if snapshot['enabled'] and not snapshot['stale']:
        for member in snapshot['members']:
            identity = member.get('member_id') or hashlib.sha256(_json(member).encode()).hexdigest()
            conn.execute('INSERT OR REPLACE INTO observer_registry VALUES(?,?,?,?)',
                         (SOURCE_ID, identity, _json(member), _now(snapshot['valid_until']).timestamp()))
    count = conn.execute('SELECT count(*) FROM observer_registry WHERE source_id=?', (SOURCE_ID,)).fetchone()[0]
    conn.execute('INSERT OR REPLACE INTO observer VALUES(1,?,?,?)',
                 (now.timestamp(), snapshot['snapshot_revision'], snapshot['generation']))
    conn.execute('INSERT OR IGNORE INTO acknowledgements VALUES(?,?,?,?,?,?)',
                 (snapshot['delivery_id'], 'local-observer', snapshot['generation'],
                  snapshot['snapshot_revision'], count, now.isoformat()))
    conn.execute("UPDATE outbox SET state='acked',error=NULL WHERE snapshot_revision=?",
                 (snapshot['snapshot_revision'],))
    return dict(consumer_id='local-observer', delivery_id=snapshot['delivery_id'],
                generation=snapshot['generation'], accepted_revision=snapshot['snapshot_revision'],
                applied_count=count)


def ack_source_snapshot(snapshot, *, root=ROOT, now=None):
    """Apply and ACK an exact local-observer snapshot atomically; not a remote ACK API."""
    now = _now(now)
    with _db(root) as conn:
        return _apply(conn, snapshot, now)


def observer_members(*, root=ROOT, now=None):
    """Read eligible local observer members, enforcing OFF/generation/day fences."""
    now = _now(now)
    with _db(root) as conn:
        control = _control(conn)
        observer = conn.execute('SELECT * FROM observer WHERE id=1').fetchone()
        conn.execute('DELETE FROM observer_registry WHERE source_id=? AND valid_until<=?',
                     (SOURCE_ID, now.timestamp()))
        latest = conn.execute('SELECT * FROM outbox ORDER BY snapshot_revision DESC LIMIT 1').fetchone()
        snapshot = json.loads(latest['payload']) if latest else None
        if (not control['enabled'] or not observer or observer['generation'] != control['generation']
                or not snapshot or snapshot['stale']
                or observer['accepted_revision'] != snapshot['snapshot_revision']):
            return []
        return [json.loads(row[0]) for row in conn.execute(
            'SELECT payload FROM observer_registry WHERE source_id=? ORDER BY member_id', (SOURCE_ID,))]


def bridge_once(*, root=ROOT, now=None, before_apply=None):
    """One local observer reconciliation. Test hook must not perform external effects."""
    now = _now(now)
    snapshot = get_source_snapshot(root=root, now=now)
    with _db(root) as conn:
        row = conn.execute('SELECT * FROM outbox WHERE snapshot_revision=?',
                           (snapshot['snapshot_revision'],)).fetchone()
        if row['next_retry'] > now.timestamp() and row['state'] == 'pending':
            return dict(delivery_state='waiting_retry', snapshot=snapshot)
    try:
        if before_apply:
            before_apply(snapshot)
        ack = ack_source_snapshot(snapshot, root=root, now=now)
        return dict(delivery_state='acked', snapshot=snapshot, ack=ack)
    except SourceConflict:
        return dict(delivery_state='fenced', snapshot=snapshot)
    except Exception as exc:
        # Persist a bounded exponential retry; never leak arbitrary exception/secret text.
        with _db(root) as conn:
            row = conn.execute('SELECT attempts FROM outbox WHERE snapshot_revision=?',
                               (snapshot['snapshot_revision'],)).fetchone()
            attempts = row['attempts'] + 1
            jitter = int(snapshot['snapshot_digest'][:4], 16) % 1000 / 1000
            delay = min(300, 2 ** min(attempts, 8)) + jitter
            conn.execute("UPDATE outbox SET attempts=?,next_retry=?,error=? WHERE snapshot_revision=? AND state='pending'",
                         (attempts, now.timestamp() + delay, type(exc).__name__, snapshot['snapshot_revision']))
        return dict(delivery_state='retry', snapshot=snapshot, attempts=attempts,
                    alert=attempts >= 10)
