#!/usr/bin/env python3
"""Offline dual-path SM replay on snapshots/*.full.json.

Cold-starts from NONE (slightly under live occupancy). Does not write production
state_machine.json. Prints occupancy vs daily-unique for the requested UTC day.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "coin-selection" / "src"))

from coin_selection.scan import rank_dmr_inbox  # noqa: E402
from coin_selection.state_machine import (  # noqa: E402
    StateConfig,
    StateMachineStore,
    apply_state_machine,
    occupancy_from_rows,
)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--day", required=True, help="UTC day YYYYMMDD, e.g. 20260819")
    p.add_argument(
        "--snapshots",
        default=str(ROOT / "data" / "coin-selection" / "snapshots"),
    )
    p.add_argument("--top-k", type=int, default=16)
    args = p.parse_args()
    snap_dir = Path(args.snapshots)
    files = sorted(snap_dir.glob(f"{args.day}-???.full.json"))
    if not files:
        print(json.dumps({"error": "no files", "day": args.day, "dir": str(snap_dir)}))
        sys.exit(1)

    tmp = Path("/tmp") / f"sm-replay-{args.day}.json"
    if tmp.exists():
        tmp.unlink()
    store = StateMachineStore(tmp)
    store.states.clear()
    cfg = StateConfig()

    occ_conf: list[int] = []
    occ_qual_sides: list[int] = []
    inbox_n: list[int] = []
    daily_conf: set[str] = set()
    daily_qual: set[str] = set()
    path_m = 0
    path_s = 0
    leaks = 0
    scans = 0

    for fp in files:
        obj = json.loads(fp.read_text(encoding="utf-8"))
        rows = obj.get("rows") or []
        if not rows:
            continue
        meta = (obj.get("board") or {}).get("meta") or {}
        ts = meta.get("scan_timestamp_utc") or obj.get("scan_id")
        now_ts = None
        try:
            # scan_id YYYYMMDD-seq → 00:00 + seq*15m
            scan_id = obj.get("scan_id") or fp.stem.replace(".full", "")
            day, seq = scan_id.split("-")
            dt = datetime.strptime(day, "%Y%m%d").replace(tzinfo=timezone.utc)
            now_ts = dt.timestamp() + int(seq) * 900
        except Exception:
            now_ts = None
        apply_state_machine(rows, store, cfg=cfg, scan_id=obj.get("scan_id") or fp.name, now_ts=now_ts)
        occ = occupancy_from_rows(rows)
        occ_conf.append(int(occ["confirmed_unique"]))
        occ_qual_sides.append(int(occ["sides"].get("QUALIFIED") or 0))
        for r in rows:
            if r.get("state_up") == "CONFIRMED":
                daily_conf.add(r["symbol"])
                if r.get("confirmed_path_up") == "M":
                    path_m += 1
                elif r.get("confirmed_path_up") == "S":
                    path_s += 1
            if r.get("state_down") == "CONFIRMED":
                daily_conf.add(r["symbol"])
                if r.get("confirmed_path_down") == "M":
                    path_m += 1
                elif r.get("confirmed_path_down") == "S":
                    path_s += 1
            if r.get("state_up") == "QUALIFIED":
                daily_qual.add(r["symbol"])
            if r.get("state_down") == "QUALIFIED":
                daily_qual.add(r["symbol"])
            if r.get("state_up") == "CONFIRMED" or r.get("state_down") == "CONFIRMED":
                if r.get("liquidity_hard_pass") is False or r.get("supply_missing") or float(r.get("data_quality_score") or 100) < 60:
                    leaks += 1
        # fake dmr unique count
        conf_msgs = []
        for r in rows:
            for d, sk, sc, ss, mom in (
                ("LONG", "state_up", "score_up", "ss_up", "momentum_score_up"),
                ("SHORT", "state_down", "score_down", "ss_down", "momentum_score_down"),
            ):
                if r.get(sk) != "CONFIRMED":
                    continue
                conf_msgs.append(
                    {
                        "symbol": r["symbol"],
                        "state": "CONFIRMED",
                        "direction": d,
                        "total_score": float(r.get(sc) or 0),
                        "staircase_score": float(r.get(ss) or 0),
                        "momentum_score": float(r.get(mom) or 0),
                    }
                )
        inbox, meta_r = rank_dmr_inbox(conf_msgs, top_k=args.top_k)
        inbox_n.append(len(inbox))
        scans += 1

    def _avg(xs: list[int]) -> float:
        return round(sum(xs) / max(len(xs), 1), 2)

    ge10 = sum(1 for x in occ_conf if x >= 10)
    out = {
        "day": args.day,
        "scans": scans,
        "confirmed_occupancy_mean": _avg(occ_conf),
        "confirmed_occupancy_min": min(occ_conf) if occ_conf else 0,
        "confirmed_occupancy_max": max(occ_conf) if occ_conf else 0,
        "occupancy_ge10_pct": round(100.0 * ge10 / max(scans, 1), 1),
        "qualified_sides_mean": _avg(occ_qual_sides),
        "daily_unique_confirmed": len(daily_conf),
        "daily_unique_qualified": len(daily_qual),
        "inbox_mean": _avg(inbox_n),
        "path_side_ticks": {"S": path_s, "M": path_m},
        "hard_constraint_leaks": leaks,
        "note": "cold-start NONE; underestimates live cross-day carry; not a live A/B",
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
