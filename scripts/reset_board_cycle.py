#!/usr/bin/env python3
"""查看 / 手动触发板面的时间区（24 小时周期）重置。

平时**不需要**跑这个：周期到点后，15 分钟扫描循环会在该节点自动清空并重建
（见 coin_selection/cycle_reset.py）。这里给三种场合用：

  * 看当前周期状态与倒计时                ``--status``
  * 循环停机跨过了周期节点，想立刻对齐    ``--board y``（幂等，同周期内不会重复清）
  * 参数改动后想立刻从零重建             ``--board y --force``

用法::

    python3 scripts/reset_board_cycle.py --status
    python3 scripts/reset_board_cycle.py --board y --dry-run
    python3 scripts/reset_board_cycle.py --board y
    python3 scripts/reset_board_cycle.py --board y --force

清空的只有**当刻板面的分区归属**（state_machine.json + daily_unique.json）。
snapshots/ 与 review/ledger.sqlite 是复盘的事实源，任何情况下都不动。

清空之后板面要等下一轮扫描才会重建分区。想立刻重建就跟一句::

    PYTHONPATH=services/coin-selection/src python3 -m coin_selection --force
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "coin-selection" / "src"))

from coin_selection.board_variants import load_variants, get_variant  # noqa: E402
from coin_selection.cycle_reset import load_cycle_state, maybe_reset  # noqa: E402
from coin_selection.scan import make_scan_id, resolve_anchor_00utc, scan_sequence  # noqa: E402


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _status(now: datetime) -> list[dict]:
    out = []
    for v in load_variants():
        data = v.data_path(ROOT)
        row = {
            "board": v.key,
            "label": v.label,
            "parameter_version": v.parameter_version,
            "data_dir": str(data),
            "cycle_enabled": v.cycle.enabled,
        }
        if v.cycle.enabled:
            start, end = v.cycle.cycle_start(now), v.cycle.cycle_end(now)
            st = load_cycle_state(data)
            row.update(
                {
                    "period_hours": v.cycle.period_hours,
                    "anchor_utc": v.cycle.anchor_utc,
                    "cycle_key": v.cycle.cycle_key(now),
                    "cycle_start_utc": start.isoformat().replace("+00:00", "Z"),
                    "cycle_end_utc": end.isoformat().replace("+00:00", "Z"),
                    "node_in_cycle": f"{v.cycle.node_in_cycle(now)}/{v.cycle.nodes_per_cycle()}",
                    "seconds_to_next_reset": int((end - now).total_seconds()),
                    "last_reset_cycle_key": st.get("cycle_key"),
                    "last_reset_scan_id": st.get("reset_scan_id"),
                    "last_reset_at_utc": st.get("reset_at_utc"),
                    "pending": st.get("cycle_key") != v.cycle.cycle_key(now),
                    "warmup_active": v.cycle.in_warmup(now),
                }
            )
        else:
            row["note"] = "该板面没有周期重置，分区成员跨天连续持有（选币榜的既有行为）"
        out.append(row)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Board 24h cycle reset")
    ap.add_argument("--board", default=None, help="板面 key（如 y）。省略 = 只看状态")
    ap.add_argument("--status", action="store_true", help="只打印各板面的周期状态")
    ap.add_argument("--force", action="store_true", help="同周期内也强制再清一次")
    ap.add_argument("--dry-run", action="store_true", help="只说会清什么，不动文件")
    args = ap.parse_args()

    now = _now()
    if args.status or not args.board:
        print(json.dumps({"now_utc": now.isoformat().replace("+00:00", "Z"),
                          "boards": _status(now)}, ensure_ascii=False, indent=2))
        return 0

    v = get_variant(args.board)
    if v is None:
        print(f"unknown board: {args.board}", file=sys.stderr)
        return 2
    if v.primary or not v.cycle.enabled:
        # 主板面没有周期，硬拒 —— 一次手滑就会把选币榜的全部分区清空。
        print(
            f"refusing: board {v.key} ({v.label}) has no cycle management; "
            "清空它的分区等于改掉选币榜的既有行为",
            file=sys.stderr,
        )
        return 2

    data = v.data_path(ROOT)
    st = load_cycle_state(data)
    key = v.cycle.cycle_key(now)
    pending = st.get("cycle_key") != key
    anchor = resolve_anchor_00utc(now)
    scan_id = make_scan_id(anchor, scan_sequence(now, anchor))

    if args.dry_run:
        sm = data / "state_machine.json"
        n = 0
        if sm.is_file():
            try:
                n = len(json.loads(sm.read_text(encoding="utf-8")).get("states") or {})
            except Exception:
                n = -1
        print(json.dumps({
            "board": v.key, "cycle_key": key, "scan_id": scan_id,
            "would_reset": bool(pending or args.force),
            "reason": "cycle rolled" if pending else ("--force" if args.force else "already reset this cycle"),
            "state_machine_entries": n,
            "clears": list(v.cycle.clear),
            "never_touched": ["snapshots/", "review/ledger.sqlite", "dmr inbox"],
        }, ensure_ascii=False, indent=2))
        return 0

    meta = maybe_reset(
        v, data_dir=data, now=now, scan_id=scan_id,
        parameter_version=v.parameter_version, force=args.force,
    )
    print(json.dumps(meta, ensure_ascii=False, indent=2))
    if meta.get("reset_at_this_node"):
        print(
            "\n分区已清空。下一轮 15 分钟扫描会从零重建；想立刻重建：\n"
            "  PYTHONPATH=services/coin-selection/src python3 -m coin_selection --force",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
