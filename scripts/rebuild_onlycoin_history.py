#!/usr/bin/env python3
"""Plan archive reconstruction by default; --apply needs an explicit NEW DB.

Never opens old occupancy ledgers. Archive generations are availability proxies,
not proof of original publication. No source files, services or orders are changed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'services/coin-selection/src'))
from coin_selection.onlycoin_ledger import OnlyCoinLedger, business_day, iso, scan_time


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inbox-dir', required=True, type=Path)
    parser.add_argument('--from', dest='from_day', required=True, help='inclusive UTC YYYY-MM-DD')
    parser.add_argument('--to', dest='to_day', required=True, help='inclusive UTC YYYY-MM-DD')
    parser.add_argument('--db', type=Path, help='explicit new isolated OnlyCoin database (required with --apply)')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    temporary = None
    try:
        start, last = business_day(args.from_day), business_day(args.to_day)
        if start > last:
            raise ValueError('--from must not be after --to')
        if not args.inbox_dir.is_dir():
            raise ValueError('--inbox-dir must exist')
        if args.apply and args.db is None:
            raise ValueError('--apply requires an explicit --db new target')
        if args.db is not None:
            if args.db.name == 'ledger.sqlite' or args.db.exists() or args.db.is_symlink():
                raise ValueError('--db must be NEW and must never be ledger.sqlite')
            if args.db.resolve().is_relative_to(args.inbox_dir.resolve()):
                raise ValueError('--db must be outside the source inbox')
        entries = []
        for path in sorted(args.inbox_dir.glob('*.candidates.json')):
            sid = path.name.removesuffix('.candidates.json')
            stamp = scan_time(sid)
            if not start <= stamp < last + timedelta(days=1):
                continue
            content = path.read_bytes()
            batch = json.loads(content)
            if batch.get('scan_id') != sid or batch.get('board_key') != 'y' or not isinstance(batch.get('candidates'), list):
                raise ValueError(f'invalid Y candidates source: {path.name}')
            entries.append((path, hashlib.sha256(content).hexdigest(), batch))
        if not entries:
            raise ValueError('no matching archive batches; refusing empty rebuild')
        manifest = [{'file': p.name, 'sha256': digest, 'scan_id': b['scan_id']} for p, digest, b in entries]
        report = {'mode': 'apply' if args.apply else 'plan', 'provenance': 'observed_archive',
                  'availability_quality': 'PROXY', 'from': args.from_day, 'to': args.to_day,
                  'target': str(args.db) if args.db else None, 'input_batches': len(entries),
                  'input_digest': hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest(),
                  'manifest': manifest, 'old_ledger_touched': False,
                  'warnings': ['Archive files cannot prove original publication.',
                               'Old occupancy conflicts are not reconciled or overwritten.']}
        if args.apply:
            args.db.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix='.onlycoin-rebuild-', suffix='.sqlite', dir=args.db.parent)
            os.close(fd)
            temporary = Path(name)
            with OnlyCoinLedger(temporary) as ledger:
                for _, _, batch in entries:
                    ledger.ingest_batch(batch, provenance='observed_archive')
                days = sorted({b['scan_id'][:8] for _, _, b in entries})
                report['days'] = []
                for raw_day in days:
                    day = f'{raw_day[:4]}-{raw_day[4:6]}-{raw_day[6:]}'
                    view = ledger.daily(day, now=iso(last + timedelta(days=2)))
                    report['days'].append({'business_date': day, 'counts': view['counts'], 'coverage': view['coverage']})
                if ledger.conn.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise ValueError('rebuilt SQLite integrity check failed')
            # link is atomic and refuses a target created concurrently; no overwrite.
            os.link(temporary, args.db)
            report['committed_batches'] = len(entries)
            with OnlyCoinLedger(args.db, readonly=True) as check:
                actual = check.conn.execute('SELECT COUNT(*) FROM onlycoin_scan_commits').fetchone()[0]
                if actual != len(entries):
                    raise ValueError('read-back batch count mismatch')
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(json.dumps({'error': str(exc), 'mode': 'apply' if args.apply else 'plan'}), file=sys.stderr)
        return 2
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


if __name__ == '__main__':
    raise SystemExit(main())
