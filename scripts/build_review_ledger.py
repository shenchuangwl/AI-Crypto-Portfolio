#!/usr/bin/env python3
"""Cold-build / catch-up the review occupancy ledger from on-disk board snapshots.

Usage:
  python3 scripts/build_review_ledger.py                 # resume from watermark
  python3 scripts/build_review_ledger.py --reset         # full rebuild
  python3 scripts/build_review_ledger.py --from 20260822-014
  python3 scripts/build_review_ledger.py --dry-run       # show what is pending

Resume is the default: without ``--reset`` the script starts at the node right
after the stored watermark. Replaying nodes the ledger has already consumed
would diff a stale ``prev_index`` against an old board and fabricate exits.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "coin-selection" / "src"))

from coin_selection.review_ledger import ReviewLedger, default_ledger_path  # noqa: E402
from coin_selection.review_replay import iter_board_paths  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description="Build review ledger from snapshots")
    p.add_argument("--data-dir", default=None)
    p.add_argument("--inbox-dir", default=None)
    p.add_argument("--board", default=None, help="main | y；指定后覆盖 --data-dir / --inbox-dir")
    p.add_argument("--from", dest="from_scan", default=None, help="inclusive scan_id override")
    p.add_argument("--to", dest="to_scan", default=None, help="inclusive scan_id upper bound")
    p.add_argument("--reset", action="store_true", help="delete existing ledger first")
    p.add_argument("--db", default=None, help="write to this path instead of the default ledger")
    p.add_argument(
        "--promote",
        action="store_true",
        help="with --db: atomically move the finished file over the live ledger",
    )
    p.add_argument("--dry-run", action="store_true", help="list pending nodes and exit")
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args()

    if args.board:
        sys.path.insert(0, str(ROOT / "services" / "coin-selection" / "src"))
        from coin_selection.board_variants import get_variant  # noqa: E402

        v = get_variant(args.board)
        if v is None:
            print(f"unknown board: {args.board}", file=sys.stderr)
            return 2
        data_dir = v.data_path(ROOT)
        inbox_dir = v.inbox_path(ROOT)
    else:
        data_dir = Path(args.data_dir or (ROOT / "data" / "coin-selection"))
        inbox_dir = Path(args.inbox_dir or (ROOT / "data" / "dmr-adapter" / "inbox"))
    snaps = data_dir / "snapshots"
    live_db = default_ledger_path(data_dir)
    db = Path(args.db) if args.db else live_db

    if args.reset and db.is_file():
        db.unlink()
        for suffix in ("-wal", "-shm"):
            side = Path(str(db) + suffix)
            if side.is_file():
                side.unlink()

    led = ReviewLedger(db)
    watermark = led.watermark().get("scan_id")
    paths = iter_board_paths(snaps)

    # Resume semantics: never re-feed a node the ledger already consumed —
    # `scan_id` is zero-padded so lexical order is chronological order.
    if args.from_scan:
        paths = [x for x in paths if x.name[:-5] >= args.from_scan]
    if watermark:
        paths = [x for x in paths if x.name[:-5] > watermark]
    if args.to_scan:
        paths = [x for x in paths if x.name[:-5] <= args.to_scan]

    if args.dry_run:
        led.close()
        print(
            json.dumps(
                {
                    "db": str(db),
                    "watermark": watermark,
                    "pending": len(paths),
                    "first": paths[0].name[:-5] if paths else None,
                    "last": paths[-1].name[:-5] if paths else None,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    t0 = time.time()
    n_ok = 0
    n_skip = 0
    for i, path in enumerate(paths):
        sid = path.name[:-5]
        try:
            board = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            print("skip unreadable", sid, e, flush=True)
            n_skip += 1
            continue
        inbox_doc = None
        ip = inbox_dir / f"{sid}.candidates.json"
        if ip.is_file():
            try:
                inbox_doc = json.loads(ip.read_text(encoding="utf-8"))
            except Exception:
                inbox_doc = None
        # `ingest_board` applies the inbox DMR overlay itself; doing it here too
        # would be a no-op but hides where the rule lives.
        out = led.ingest_board(board, inbox_doc=inbox_doc)
        if out.get("status") == "ok":
            n_ok += 1
        else:
            n_skip += 1
        if not args.quiet and i % 50 == 0:
            print(f"{i}/{len(paths)} {sid} {out.get('status')}", flush=True)

    cov = led.coverage()
    zone_rows = {
        r["zone"]: {"closed": r["c"], "open": r["o"], "coins": r["u"]}
        for r in led._conn.execute(
            "SELECT zone, SUM(status='CLOSED') c, SUM(status='OPEN') o, "
            "COUNT(DISTINCT canonical_asset_id) u FROM trades GROUP BY zone"
        )
    }
    led.close()
    promoted = None
    if args.promote and args.db:
        # Build beside the live file, then swap: the gateway opens the ledger per
        # request, so an in-place --reset would 503 for the length of the rebuild.
        for suffix in ("", "-wal", "-shm"):
            side = Path(str(db) + suffix)
            target = Path(str(live_db) + suffix)
            if side.is_file():
                side.replace(target)
            elif target.is_file():
                target.unlink()
        promoted = str(live_db)
    print(
        json.dumps(
            {
                "db": str(db),
                "promoted_to": promoted,
                "resumed_from": watermark,
                "ok": n_ok,
                "skip": n_skip,
                "elapsed_s": round(time.time() - t0, 1),
                "by_zone": zone_rows,
                "coverage": {
                    k: v
                    for k, v in cov.items()
                    if k in ("watermark_scan_id", "watermark_ts", "first_ts", "last_ts", "trade_rows",
                             "closed_rows", "open_rows", "nodes_ingested", "parameter_segments")
                },
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
