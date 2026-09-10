"""Source-only tests: every mutable root is pytest's disposable tmp_path."""
import importlib
import sqlite3
from typing import Any

import pytest


def source():
    return importlib.import_module('coin_selection.onlycoin_source')


def request(enabled, revision=0, request_id='request-1'):
    return dict(enabled=enabled, expected_revision=revision, request_id=request_id,
                replay_policy='current_day_snapshot', reason='test')


def test_status_absent_is_readonly_off(tmp_path):
    root = tmp_path / 'absent'
    status = source().source_status(root=root)
    assert status['effective_state'] == 'OFF'
    assert status['eligibility_only'] is True
    assert not root.exists()


def test_status_existing_database_never_writes(monkeypatch, tmp_path):
    s = source()
    s.set_source_control(request(False), root=tmp_path)
    db = tmp_path / 'data/coin-selection-y/onlycoin-source/control.sqlite'
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in db.parent.iterdir()}
    connect = sqlite3.connect
    statements = []

    def readonly_connect(*args, **kwargs):
        assert kwargs.get('uri') is True and 'mode=ro' in str(args[0])
        conn = connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(sqlite3, 'connect', readonly_connect)
    assert s.source_status(root=tmp_path)['effective_state'] == 'OFF'
    assert all(sql.lstrip().upper().startswith(('SELECT', 'BEGIN', 'ROLLBACK')) for sql in statements)
    assert before == {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in db.parent.iterdir()}


def test_default_off_and_persistent_cas_idempotency(tmp_path):
    s = source()
    assert s.source_status(root=tmp_path)['effective_state'] == 'OFF'
    result = s.set_source_control(request(True), root=tmp_path, actor='test-admin')
    assert result['enabled'] is True
    assert result['revision'] == result['generation'] == 1
    assert s.source_status(root=tmp_path)['effective_state'] == 'ENABLING'
    assert not s.source_status(root=tmp_path)['consumer_connected']
    assert s.set_source_control(request(True), root=tmp_path) == result
    with pytest.raises(s.SourceConflict):
        s.set_source_control(request(False), root=tmp_path)
    with pytest.raises(s.SourceConflict):
        s.set_source_control(request(False, 0, 'stale'), root=tmp_path)
    for enabled in ('false', 1, None):
        with pytest.raises(ValueError):
            s.set_source_control(request(enabled), root=tmp_path)
    db = tmp_path / 'data/coin-selection-y/onlycoin-source/control.sqlite'
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT count(*) FROM audit').fetchone()[0] == 1
    reopened = importlib.reload(s)
    assert reopened.source_status(root=tmp_path)['generation'] == 1


def install_ledger(monkeypatch) -> dict[str, Any]:
    # Backend is built concurrently; stub only its read-only boundary.
    import sys
    import types
    from datetime import datetime
    identity = dict(parameter_version='param-v2.0.0-screener-y',
                    rule_revision='y-v2-test', config_hash='config-test',
                    mapping_hash='mapping-test', code_commit='commit-test',
                    identity_status='MATCH')
    state = dict(board_key='y', business_date='2026-09-10',
                 cycle_end_utc='2026-09-11T00:00:00Z', projection_revision=7,
                 stale=False, coverage=dict(availability_quality='COMMITTED', partial=False,
                                            flags=[], missing_scan_ids=[]),
                 onlycoin=[dict(member_id='2026-09-10:BTCUSDT', symbol='BTCUSDT',
                                provenance='live_committed', rule_identity=identity,
                                parameter_version=identity['parameter_version'])])

    class Ledger:
        def __init__(self, path, readonly=False):
            assert readonly is True
            assert str(path).endswith('/review/onlycoin.sqlite')

        def daily(self, *, now):
            assert isinstance(now, datetime) and now.tzinfo is not None
            return dict(state)

    monkeypatch.setitem(sys.modules, 'coin_selection.onlycoin_ledger',
                        types.SimpleNamespace(OnlyCoinLedger=Ledger))
    return state


@pytest.mark.parametrize('field,value', [
    ('board_key', 'screener-y'), ('coverage', None), ('coverage', 'complete'),
    ('coverage.partial', True), ('coverage.partial', 0),
    ('coverage.availability_quality', 'PROXY'), ('coverage.flags', ['MISSING_IDENTITY']),
    ('coverage.flags', None), ('coverage.missing_scan_ids', ['missing']),
    ('onlycoin', []), ('onlycoin', None), ('projection_revision', None),
    ('member.provenance', 'observed_archive'), ('member.provenance', 'counterfactual_replay'),
    ('member.rule_identity', None), ('member.parameter_version', 'wrong'),
    ('identity.parameter_version', 'wrong'), ('identity.identity_status', 'DRIFT'),
    ('identity.rule_revision', ''), ('identity.config_hash', None),
    ('identity.mapping_hash', ' '), ('identity.code_commit', ''),
])
def test_invalid_ledger_contract_fails_closed(monkeypatch, tmp_path, field, value):
    state = install_ledger(monkeypatch)
    target = state
    parts = field.split('.')
    if parts[0] == 'member':
        target = state['onlycoin'][0]
    elif parts[0] == 'identity':
        target = state['onlycoin'][0]['rule_identity']
    elif len(parts) > 1:
        target = state[parts[0]]
    target[parts[-1]] = value
    s = source()
    s.set_source_control(request(True), root=tmp_path)
    result = s.bridge_once(root=tmp_path, now='2026-09-10T12:00:00Z')
    assert result['snapshot']['stale'] is True
    assert result['snapshot']['error'] == 'ledger_unavailable_or_invalid'
    assert result['snapshot']['members'] == []
    assert s.observer_members(root=tmp_path, now='2026-09-10T12:00:00Z') == []


@pytest.mark.parametrize('invalid', ['nan', 'object', 'mixed', 'missing'])
def test_malformed_metadata_and_full_identity_fail_closed(monkeypatch, tmp_path, invalid):
    from copy import deepcopy
    state = install_ledger(monkeypatch)
    if invalid == 'nan':
        state['coverage']['observed_scans'] = float('nan')
    elif invalid == 'object':
        state['projection_revision'] = object()
    elif invalid == 'missing':
        del state['onlycoin'][0]['rule_identity']['code_commit']
    else:
        second = deepcopy(state['onlycoin'][0])
        second['member_id'] = 'other'
        second['rule_identity']['config_hash'] = 'other-config'
        state['onlycoin'].append(second)
    s = source()
    s.set_source_control(request(True), root=tmp_path)
    result = s.get_source_snapshot(root=tmp_path, now='2026-09-10T12:00:00Z')
    assert result['stale'] and result['error'] and result['members'] == []


def test_snapshot_readonly_staleness_expiry_and_day(monkeypatch, tmp_path):
    state = install_ledger(monkeypatch)
    s = source()
    now = '2026-09-10T12:00:00Z'
    assert s.get_source_snapshot(root=tmp_path, now=now)['members'] == []
    s.set_source_control(request(True), root=tmp_path)
    snap = s.get_source_snapshot(root=tmp_path, now=now)
    assert snap['enabled'] and snap['day'] == '2026-09-10'
    assert snap['members'] == state['onlycoin']
    assert snap['order_instruction'] is False and snap['eligibility_only'] is True
    assert s.get_source_snapshot(root=tmp_path, now=now)['snapshot_revision'] == snap['snapshot_revision']
    state['stale'] = True
    stale = s.get_source_snapshot(root=tmp_path, now=now)
    assert stale['members'] == [] and stale['stale']
    assert stale['snapshot_revision'] > snap['snapshot_revision']
    state['stale'] = False
    expired = s.get_source_snapshot(root=tmp_path, now='2026-09-11T00:00:00Z')
    assert expired['members'] == [] and expired['stale']
    with pytest.raises(ValueError):
        s.get_source_snapshot(root=tmp_path, now='not-a-date')


def test_bridge_ack_fencing_retry_restart_and_namespace(monkeypatch, tmp_path):
    from datetime import datetime, timezone
    state = install_ledger(monkeypatch)
    fixed = datetime(2026, 9, 10, 12, tzinfo=timezone.utc)
    original_now = source()._now
    monkeypatch.setattr(source(), '_now', lambda value=None: original_now(fixed if value is None else value))
    today = fixed.date()
    from datetime import timedelta
    state['business_date'] = today.isoformat()
    state['cycle_end_utc'] = (today + timedelta(days=1)).isoformat() + 'T00:00:00Z'
    s = source()
    s.set_source_control(request(True), root=tmp_path)
    snap = s.get_source_snapshot(root=tmp_path)
    wrong = dict(snap, snapshot_revision=snap['snapshot_revision'] + 1)
    with pytest.raises(s.SourceConflict):
        s.ack_source_snapshot(wrong, root=tmp_path)
    assert s.observer_members(root=tmp_path) == []
    def failure(snapshot):
        raise OSError('simulated disk failure')
    failed = s.bridge_once(root=tmp_path, before_apply=failure)
    assert failed['delivery_state'] == 'retry'
    assert s.bridge_once(root=tmp_path)['delivery_state'] == 'waiting_retry'
    later = fixed + timedelta(seconds=40)
    delivered = s.bridge_once(root=tmp_path, now=later)
    assert delivered['delivery_state'] == 'acked'
    assert len(s.observer_members(root=tmp_path, now=later)) == 1
    assert s.bridge_once(root=tmp_path, now=later)['delivery_state'] == 'acked'
    assert s.source_status(root=tmp_path)['effective_state'] == 'ON'
    db = tmp_path / 'data/coin-selection-y/onlycoin-source/control.sqlite'
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO observer_registry VALUES('other-source','other','{}',9999999999)")
    old = s.get_source_snapshot(root=tmp_path)
    s.set_source_control(request(False, 1, 'off'), root=tmp_path)
    assert s.source_status(root=tmp_path)['effective_state'] == 'DISABLING'
    assert s.observer_members(root=tmp_path) == []
    with pytest.raises(s.SourceConflict):
        s.ack_source_snapshot(old, root=tmp_path)
    s.bridge_once(root=tmp_path)
    assert s.source_status(root=tmp_path)['effective_state'] == 'OFF'
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM observer_registry WHERE source_id='other-source'").fetchone()[0] == 1
    s.set_source_control(request(True, 2, 'reopen'), root=tmp_path)
    assert s.bridge_once(root=tmp_path)['delivery_state'] == 'acked'
    assert importlib.reload(s).source_status(root=tmp_path)['generation'] == 3
    assert s.observer_members(root=tmp_path, now=state['cycle_end_utc']) == []
    assert s.bridge_once(root=tmp_path, now=state['cycle_end_utc'])['snapshot']['members'] == []
    assert not s.source_status(root=tmp_path)['trading_connected']


@pytest.mark.parametrize('change', ['projection', 'quality', 'identity', 'members', 'day', 'ack'])
def test_status_requires_fresh_projection_and_exact_ack(monkeypatch, tmp_path, change):
    state = install_ledger(monkeypatch)
    s = source()
    real_now = s._now
    clock = [real_now('2026-09-10T12:00:00Z')]
    monkeypatch.setattr(s, '_now', lambda value=None: clock[0] if value is None else real_now(value))
    s.set_source_control(request(True), root=tmp_path)
    s.bridge_once(root=tmp_path)
    assert s.source_status(root=tmp_path)['effective_state'] == 'ON'
    if change == 'projection':
        state['projection_revision'] += 1
    elif change == 'quality':
        state['coverage']['partial'] = True
    elif change == 'identity':
        state['onlycoin'][0]['rule_identity']['identity_status'] = 'DRIFT'
    elif change == 'members':
        state['onlycoin'] = []
    elif change == 'day':
        clock[0] = real_now('2026-09-11T00:00:00Z')
    else:
        db = tmp_path / 'data/coin-selection-y/onlycoin-source/control.sqlite'
        with sqlite3.connect(db) as conn:
            conn.execute('DELETE FROM acknowledgements')
    db = tmp_path / 'data/coin-selection-y/onlycoin-source/control.sqlite'
    before = db.read_bytes()
    assert s.source_status(root=tmp_path)['effective_state'] != 'ON'
    assert db.read_bytes() == before


def test_pending_snapshot_and_stale_data_revoke_observer(monkeypatch, tmp_path):
    state = install_ledger(monkeypatch)
    s = source()
    now = '2026-09-10T12:00:00Z'
    s.set_source_control(request(True), root=tmp_path)
    first = s.bridge_once(root=tmp_path, now=now)['snapshot']
    state['stale'] = True
    s.get_source_snapshot(root=tmp_path, now=now)
    assert s.observer_members(root=tmp_path, now=now) == []
    with pytest.raises(s.SourceConflict):
        s.ack_source_snapshot(first, root=tmp_path, now=now)
    s.set_source_control(request(False, 1, 'off'), root=tmp_path)
    db = tmp_path / 'data/coin-selection-y/onlycoin-source/control.sqlite'
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM outbox WHERE state='pending'").fetchone()[0] == 0


def test_cli_requires_root_and_runs_only_in_tempdir(tmp_path):
    import os
    from pathlib import Path
    import subprocess
    import sys
    script = Path(__file__).resolve().parents[3] / 'scripts/onlycoin_source_bridge.py'
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    missing = subprocess.run([sys.executable, str(script), '--once'], env=env, capture_output=True, text=True)
    assert missing.returncode == 2 and '--root' in missing.stderr
    for _ in range(2):
        run = subprocess.run([sys.executable, str(script), '--root', str(tmp_path), '--once'],
                             env=env, capture_output=True, text=True)
        assert run.returncode == 0, run.stderr
        import json
        result = json.loads(run.stdout)
        assert result['snapshot']['enabled'] is False
        assert result['delivery_state'] == 'acked'
    assert not (tmp_path / 'data/coin-selection-y/review').exists()


def test_protocol_schema_and_real_empty_ledger(tmp_path):
    import json
    from pathlib import Path
    import jsonschema
    from coin_selection.onlycoin_ledger import OnlyCoinLedger
    path = tmp_path / 'data/coin-selection-y/review/onlycoin.sqlite'
    path.parent.mkdir(parents=True)
    ledger = OnlyCoinLedger(path)
    ledger.close()
    s = source()
    s.set_source_control(request(True), root=tmp_path)
    result = s.bridge_once(root=tmp_path)
    assert result['snapshot']['stale'] and result['snapshot']['members'] == []
    schema = json.loads((Path(__file__).resolve().parents[3] / 'contracts/json-schema/onlycoin-source.schema.json').read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(result['snapshot'], schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(dict(result['snapshot'], order_instruction=True), schema)
    import ast
    module = ast.parse(Path(s.__file__).read_text())
    called = {n.func.attr for n in ast.walk(module) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert not called.intersection({'cancel', 'flatten', 'create_order', 'cancel_order'})


def test_inflight_off_fences_before_apply_and_control_survives_review_loss(monkeypatch, tmp_path):
    install_ledger(monkeypatch)
    s = source()
    s.set_source_control(request(True), root=tmp_path)
    def turn_off(snapshot):
        s.set_source_control(request(False, 1, 'concurrent-off'), root=tmp_path)
    result = s.bridge_once(root=tmp_path, now='2026-09-10T12:00:00Z', before_apply=turn_off)
    assert result['delivery_state'] == 'fenced'
    assert s.observer_members(root=tmp_path) == []
    assert s.source_status(root=tmp_path)['generation'] == 2
    assert not (tmp_path / 'data/coin-selection-y/review/onlycoin.sqlite').exists()
    assert s.set_source_control(request(True, 2, 'on-again'), root=tmp_path)['generation'] == 3
    assert s.set_source_control(request(True), root=tmp_path)['generation'] == 1
    assert s.source_status(root=tmp_path)['generation'] == 3

