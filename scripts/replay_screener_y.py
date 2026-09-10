#!/usr/bin/env python3
"""为任一投影板面回放历史、铺底独立复盘账本（默认 y，选币榜X 用 --board x）。

选币榜X v1.3.0 当前复刻 main v1.4.0，shadow 仅观察；未来仅改 X overrides
并同步 YAML。冷启动历史重算不等于主榜当时实际执行，账本不得合并。

主板 ``snapshots/*.full.json`` 的原始 G1–G4 指标按目标变体重算 Score→状态机→DMR→账本。
不另取行情；X 当前沿用 main 阈值，Y 已是七权重+主导层 on+周期重置，不能混称克隆。
只读源快照，目标 data_dir / inbox / ledger / --reset 均由注册表解析；拒绝 main。

    python3 scripts/replay_screener_y.py --board x --dry-run
    python3 scripts/replay_screener_y.py --board x --all
    python3 scripts/replay_screener_y.py --board y --days 14
    python3 scripts/replay_screener_y.py --board x --from 20260822-000 --to 20260824-095

默认最近 7 天、默认 board=y；--reset 是显式破坏性重建，先停目标板面的实时投影。
其余为续跑：从目标快照水位之后继续。铺底期间须禁用目标 ENABLE_BOARD_X/Y，
否则实时写者抢先推进水位，会导致历史尚未入账就被跳过。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "coin-selection" / "src"))

from coin_selection.board_projection import (  # noqa: E402
    adopt_enter_times,
    project_board,
    rows_from_snapshot,
)
from coin_selection.board_variants import BOARD_Y_KEY, get_variant  # noqa: E402
from coin_selection.review_ledger import ReviewLedger, default_ledger_path  # noqa: E402
from coin_selection.scan import (  # noqa: E402
    SelectionSettings,
    make_scan_id,
    node_time,
    resolve_anchor_00utc,
)

_UNI_VER_RE = re.compile(r'"universe_version"\s*:\s*"([^"]*)"')


def _scan_id_parts(scan_id: str) -> tuple[datetime, int]:
    """``20260825-017`` → (锚点日 00:00 UTC, 序号)。"""
    day, seq = scan_id.split("-")
    anchor = datetime(int(day[:4]), int(day[4:6]), int(day[6:8]), tzinfo=timezone.utc)
    return anchor, int(seq)


def _universe_version(board_path: Path) -> str:
    """从板面文件头部抠 universe_version —— 板面 2.5 MB，不值得整份 parse。"""
    try:
        with board_path.open("rb") as f:
            head = f.read(4096).decode("utf-8", "ignore")
    except OSError:
        return ""
    m = _UNI_VER_RE.search(head)
    return m.group(1) if m else ""


def main() -> int:
    p = argparse.ArgumentParser(description="任一投影板面历史铺底：X v1.3.0 复刻 main v1.4.0，复盘独立；调参仅对应 overrides")
    p.add_argument("--board", default=BOARD_Y_KEY, help="投影板面 key：x=选币榜X v1.3.0，y=选币榜Y（默认）；禁止 main")
    p.add_argument("--source-data-dir", default=str(ROOT / "data" / "coin-selection"),
                   help="主板面数据目录（只读）")
    p.add_argument("--days", type=float, default=7.0, help="回放最近 N 天（默认 7；--all 覆盖它）")
    p.add_argument("--all", action="store_true", help="回放磁盘上全部 full.json 历史")
    p.add_argument("--from", dest="from_scan", default=None, help="起始 scan_id（含）")
    p.add_argument("--to", dest="to_scan", default=None, help="终止 scan_id（含）")
    p.add_argument("--reset", action="store_true", help="先清空该变体的状态机 / 快照 / inbox / 账本")
    p.add_argument("--no-snapshots", action="store_true",
                   help="不保留目标板面快照，只喂其账本（省磁盘，但之后无法 --reset 重建）")
    p.add_argument(
        "--no-adopt-enter-times",
        action="store_true",
        help="回放结束后不从主板面状态机补「入区时刻」（默认补；只在状态一致时补，绝不改状态）",
    )
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args()

    variant = get_variant(args.board)
    if variant is None:
        print(f"unknown board variant: {args.board}", file=sys.stderr)
        return 2
    if variant.primary:
        print("refusing to replay onto the primary board (选币榜 must stay untouched)", file=sys.stderr)
        return 2

    src_snaps = Path(args.source_data_dir) / "snapshots"
    if not src_snaps.is_dir():
        print(f"source snapshots not found: {src_snaps}", file=sys.stderr)
        return 2

    dst_dir = variant.data_path(ROOT)
    dst_snaps = dst_dir / "snapshots"
    dst_inbox = variant.inbox_path(ROOT)

    if args.reset and not args.dry_run:
        # cycle/ 与 cycle_state.json 一并清掉：水位留着会让回放跳过第一个周期的重置，
        # 那样重放出来的账本就不是「24h 循环」下的成绩了。
        for path in (dst_snaps, dst_inbox, dst_dir / "review", dst_dir / "cycle"):
            if path.is_dir():
                shutil.rmtree(path)
        for f in ("state_machine.json", "latest.json", "daily_unique.json", "cycle_state.json"):
            fp = dst_dir / f
            if fp.is_file():
                fp.unlink()
        print(f"reset {dst_dir} and {dst_inbox}")

    # 续跑水位：Y 已经有的最后一个节点。scan_id 零填充，字典序即时间序。
    done = sorted(
        p.name[:-5] for p in dst_snaps.glob("*.json") if not p.name.endswith(".full.json")
    ) if dst_snaps.is_dir() else []
    watermark = done[-1] if done else None

    fulls = sorted(src_snaps.glob("*.full.json"))
    ids = [p.name[: -len(".full.json")] for p in fulls]
    if args.from_scan:
        ids = [x for x in ids if x >= args.from_scan]
    if args.to_scan:
        ids = [x for x in ids if x <= args.to_scan]
    if watermark:
        ids = [x for x in ids if x > watermark]
    if not args.all and args.days > 0 and ids:
        cutoff_anchor = resolve_anchor_00utc()
        cutoff_seq = int(-args.days * 96)
        cutoff = make_scan_id(*_anchor_shift(cutoff_anchor, cutoff_seq))
        ids = [x for x in ids if x >= cutoff]

    if args.dry_run:
        print(json.dumps({
            "board": variant.key,
            "parameter_version": variant.parameter_version,
            "source": str(src_snaps),
            "target": str(dst_dir),
            "watermark": watermark,
            "pending": len(ids),
            "first": ids[0] if ids else None,
            "last": ids[-1] if ids else None,
        }, ensure_ascii=False, indent=2))
        return 0

    if not ids:
        print(json.dumps({"board": variant.key, "pending": 0, "watermark": watermark},
                         ensure_ascii=False))
        return 0

    base = SelectionSettings()
    t0 = time.time()
    ok = skipped = 0
    boards_for_ledger: list[str] = []
    resets: list[str] = []

    for i, sid in enumerate(ids):
        fp = src_snaps / f"{sid}.full.json"
        try:
            full = json.loads(fp.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"skip unreadable {sid}: {e}", flush=True)
            skipped += 1
            continue
        raw_rows = full.get("rows") or []
        if not raw_rows:
            skipped += 1
            continue
        anchor, seq = _scan_id_parts(sid)
        now = node_time(anchor, seq)
        rows = rows_from_snapshot(variant, base, raw_rows)
        res = project_board(
            variant,
            base,
            rows,
            anchor=anchor,
            scan_id=sid,
            seq=seq,
            now=now,
            universe_count=int(full.get("universe_count") or len(raw_rows)),
            uni_ver=_universe_version(src_snaps / f"{sid}.json"),
            stats={
                k: full.get(k) or {}
                for k in ("gate1", "gate2", "gate3", "gate4", "mcap_tf", "long_ret")
            },
            generated_at=now,
            root=ROOT,
            # 冷启动：状态机从零长起来，不继承主板面 —— 回放的意义就在于「用 v2.0.0
            # 从头走一遍」，继承当刻状态会把 v1.4.0 的记忆混进 v2.0.0 的账本。
            seed_state_from=None,
            write_full=False,
            ingest_ledger=False,   # 账本统一在最后一次性喂，避免逐节点开关 sqlite
            backfill_prices=False,
            skip_sm=False,
        )
        boards_for_ledger.append(sid)
        ok += 1
        cyc = res.get("cycle") or {}
        if cyc.get("reset_at_this_node"):
            resets.append(sid)
            if not args.quiet:
                closed = sum(v for k, v in (cyc.get("closed_zones") or {}).items() if k != "NONE")
                print(f"  cycle reset {cyc.get('cycle_key')} at {sid} (清出 {closed} 个分区成员)", flush=True)
        if not args.quiet and i % 48 == 0:
            print(f"{i}/{len(ids)} {sid}", flush=True)

    # —— 账本：喂刚刚生成的 Y 板面 ——
    led = ReviewLedger(default_ledger_path(dst_dir))
    n_led = 0
    for sid in boards_for_ledger:
        board = _read_json(dst_snaps / f"{sid}.json")
        if board is None:
            continue
        inbox_doc = _read_json(dst_inbox / f"{sid}.candidates.json")
        if led.ingest_board(board, inbox_doc=inbox_doc).get("status") == "ok":
            n_led += 1
    cov = led.coverage()
    led.close()

    # 回放窗口起点之前的历史不可知 —— 那之前就已在某区的币，入区时刻会被系统性低估，
    # 而停留时长正是 min_dwell 闸门的输入。用主板面的状态机补齐，只补时间/入点价，
    # 且只在两边状态一致时补：状态不同说明参数真的分叉了，那是要保留的信息。
    #
    # 但**启用了周期管理的板面不能补**：24h 重置之后，每个入区时刻都必然落在本周期内
    # 且精确已知；从主板面（跨天连续持有）抄一个几天前的入区时刻过来，
    # 等于凭空造出一笔「跨周期持有」，正是这个栏目要消灭的东西。
    adopt = {"adopted": 0, "skipped": None}
    if variant.cycle.enabled:
        adopt["skipped"] = "cycle_enabled"
        if not args.quiet:
            print("skip adopt-enter-times: 该板面有 24h 周期重置，入区时刻本就精确已知", flush=True)
    elif not args.no_adopt_enter_times:
        adopt = adopt_enter_times(
            variant, Path(args.source_data_dir) / "state_machine.json", root=ROOT
        )

    if args.no_snapshots:
        # 账本已经吃完，快照可以丢。代价写在 --help 里：之后没法 --reset 重建。
        for sid in boards_for_ledger:
            (dst_snaps / f"{sid}.json").unlink(missing_ok=True)

    print(json.dumps({
        "board": variant.key,
        "parameter_version": variant.parameter_version,
        "data_dir": str(dst_dir),
        "ledger": str(default_ledger_path(dst_dir)),
        "projected": ok,
        "skipped": skipped,
        "ledger_nodes": n_led,
        # 周期重置在回放里也照跑：每天 00:00 UTC 清空重建，账本里的强平会带 CYCLE_RESET
        "cycle_resets": len(resets),
        "cycle_reset_nodes": resets[:5] + (["…"] if len(resets) > 5 else []),
        "enter_stamps_adopted": adopt,
        "elapsed_s": round(time.time() - t0, 1),
        "coverage": {
            k: cov.get(k)
            for k in ("watermark_scan_id", "watermark_ts", "first_ts", "last_ts",
                      "trade_rows", "closed_rows", "open_rows", "nodes_ingested")
        },
    }, ensure_ascii=False, indent=2))
    return 0


def _anchor_shift(anchor: datetime, seq: int) -> tuple[datetime, int]:
    """把负 seq 折算回「更早的锚点日 + 正 seq」，让 make_scan_id 能生成合法 id。"""
    days, rem = divmod(seq, 96)
    from datetime import timedelta

    return anchor + timedelta(days=days), rem


def _read_json(path: Path) -> Optional[dict[str, Any]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


if __name__ == "__main__":
    raise SystemExit(main())
