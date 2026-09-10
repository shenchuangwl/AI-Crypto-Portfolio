#!/usr/bin/env python3
"""Explicit-root, source-only local observer; never launches a DMR strategy."""
import argparse
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'services/coin-selection/src'))
from coin_selection.onlycoin_source import bridge_once


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True, help='Explicit data root; use a disposable root for tests')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--once', action='store_true')
    mode.add_argument('--watch', action='store_true')
    parser.add_argument('--interval', type=float, default=5.0)
    args = parser.parse_args(argv)
    if not 0 < args.interval <= 30:
        parser.error('--interval must be in (0,30] seconds')
    try:
        while True:
            result = bridge_once(root=args.root.resolve())
            print(json.dumps(result, sort_keys=True), flush=True)
            if args.once:
                return 0 if result['delivery_state'] == 'acked' else 1
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
