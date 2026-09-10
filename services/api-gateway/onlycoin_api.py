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
