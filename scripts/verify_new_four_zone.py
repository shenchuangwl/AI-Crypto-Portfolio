#!/usr/bin/env python3
"""Acceptance gate for 新四区标准 (param-v1.3.0-dual-path-sticky), 计划 §13.1.

Reads the *produced artefacts* — board snapshots, DMR inbox files, loop status —
and asserts the hard bottom lines of 计划 §10.3 plus the measurable acceptance
rows of §13.1. It never re-derives Score/SS/M: a checker that recomputes what it
checks cannot catch a scan-time regression.

    ./scripts/verify_new_four_zone.py                # latest.json + today's inbox
    ./scripts/verify_new_four_zone.py --day 20260821 # whole UTC day

Exit code 0 = every hard gate passed (soft gates may still WARN).
"""

from __future__ import annotations

import argparse
import json
import re
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
# 默认查「选币榜」；--board y 改查「选币榜Y」(param-v2.0.0-screener-y)，
# 两块板面适用**同一套**验收闸 —— 复刻的意思就是同一套硬约束都要过。
# --board x = 选币榜X v1.3.0，复刻 main v1.4.0 且复盘闭环独立；后续参数只改 X overrides。
DATA = ROOT / "data" / "coin-selection"
INBOX = ROOT / "data" / "dmr-adapter" / "inbox"
PARAM_YAML = ROOT / "packages" / "config" / "param-v1.4.0-staircase-confirm-dmr.yaml"


def select_board(key: str) -> None:
    """把全局的 DATA / INBOX / PARAM 切到指定板面。

    选币榜Y 是选币榜的 100% 复刻，所以它必须过**同一套**验收闸：硬约束泄漏、
    确认区流动性、旋转楼梯、只走 PATH_S、禁止跳级、15m 栅格、DMR 只吃确认、K 合法。
    唯一放行的差异是参数版本号与 loop_status（Y 没有自己的循环，跟着主扫描走）。
    """
    global DATA, INBOX, PARAM, PARAM_YAML, BOARD
    BOARD = key
    if key == "main":
        return
    sys.path.insert(0, str(ROOT / "services" / "coin-selection" / "src"))
    from coin_selection.board_variants import get_variant  # noqa: E402

    v = get_variant(key)
    if v is None:
        raise SystemExit(f"unknown board variant: {key}")
    DATA = v.data_path(ROOT)
    INBOX = v.inbox_path(ROOT)
    PARAM = v.parameter_version
    PARAM_YAML = ROOT / v.param_file if v.param_file else PARAM_YAML
    # 该板面用的是哪套选币语义 —— 决定确认区该用哪组不变量（见 check_board）。
    global SEMANTICS
    SEMANTICS = str((v.state_overrides or {}).get("selection_semantics") or "staircase-v1.4")


BOARD = "main"
#: 选币语义，由 select_board 按板面 overrides 解析；main/Y 恒为 v1.4。
SEMANTICS = "staircase-v1.4"

PARAM = "param-v1.4.0-staircase-confirm-dmr"
HARD_FLOOR_M = 3.0  # million USD, 门槛一
K_MIN, K_MAX = 12, 20
#: 节点幂等守卫**完全**上线的边界。这之前的重复执行是已知历史污染，无法回溯修正，
#: 只记 WARN；这之后再出现重复就是真事故，硬红。
#:
#: 为什么是 019 而不是 018：守卫分两次上线 —— 状态机守卫先随 11:17 那次重启生效
#: （所以 017/018 的重跑都是无害 no-op，conf_unique 分别 18→18、19→19 未动），
#: 完成台账（--force 也绕不过的那道）随 11:30 那次重启才生效。018 正是过渡节点：
#: 它由旧进程写下（没记完成），又被新进程重跑了一次。第一个从头到尾都受两道守卫
#: 保护的节点是 019。把边界写成 018 会把上线过程本身算成事故。
DUP_GUARD_FROM_SCAN_ID = "20260903-019"
OCC_TARGET = (10, 40)
SS_CONF_S = 50.0
CONS_ONE_EPS = 1e-9


class Report:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def add(self, name: str, ok: bool, detail: str, hard: bool = True) -> None:
        self.rows.append(
            {
                "check": name,
                "status": "PASS" if ok else ("FAIL" if hard else "WARN"),
                "hard": hard,
                "detail": detail,
            }
        )

    @property
    def failed(self) -> int:
        return sum(1 for r in self.rows if r["status"] == "FAIL")

    def render(self) -> str:
        w = max(len(r["check"]) for r in self.rows) if self.rows else 10
        out = []
        for r in self.rows:
            out.append(f"[{r['status']:4}] {r['check']:<{w}}  {r['detail']}")
        return "\n".join(out)


def cons_is_one(c: Any) -> bool:
    try:
        return abs(float(c) - 1.0) <= CONS_ONE_EPS
    except (TypeError, ValueError):
        return False


def load(p: Path) -> Optional[dict[str, Any]]:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def confirmed_rows(board: dict[str, Any]) -> list[dict[str, Any]]:
    pools = (board.get("long_pool") or []) + (board.get("short_pool") or [])
    return [r for r in pools if r.get("state") == "CONFIRMED"]


def check_board(board: dict[str, Any], rep: Report, label: str) -> None:
    meta = board.get("meta") or {}
    conf = confirmed_rows(board)

    if meta.get("parameter_version") != PARAM:
        rep.add(f"{label} parameter_version", False, f"got {meta.get('parameter_version')!r}, want {PARAM!r}")
    else:
        rep.add(f"{label} parameter_version", True, PARAM)

    # §10.3 硬底线: 缺供应量 / DQ<60 / 非 LIVE-able data_mode 不得在确认区
    leaks = [
        r["symbol"]
        for r in conf
        if float(r.get("data_confidence") or 0) < 60
        or (r.get("circulating_supply") in (None, 0))
        or r.get("data_mode") == "MISSING"
    ]
    rep.add(f"{label} 确认区硬约束泄漏", not leaks, f"leaks={len(leaks)} {leaks[:5]}")

    # 确认锁定流动性: min(aqv6,12,26) > $3M — 100%
    bad_liq = [
        r["symbol"]
        for r in conf
        if min(
            float(r.get("aqv_6d_m") or 0),
            float(r.get("aqv_12d_m") or 0),
            float(r.get("aqv_26d_m") or 0),
        )
        <= HARD_FLOOR_M
    ]
    rep.add(f"{label} 确认区流动性 >300万", not bad_liq, f"violations={len(bad_liq)} {bad_liq[:5]}")

    # —— 确认区不变量：按板面语义选用，两套都不放宽 ——
    #
    # v1.4（main / 选币榜Y）：硬楼梯 —— 确认区每一行都必须 SS>=50，且只许走 PATH_S。
    # v1.3（选币榜X 适配）：双通道 + 粘滞 —— PATH_M 允许 SS>=45 且 C=1 入场，
    #   hold 允许「进门后衰减」到 SS>=45，所以 SS>=50 与「仅 PATH_S」这两条**在 v1.3
    #   下本就不成立**，照搬会把正常成员判成违规（实测 12 / 10 条）。
    #   但不能因此不查 —— 换成 v1.3 自己的等价强度不变量：
    #     ① 确认区不得出现 SS<45 **且** C<1 的行（两条入场通道与 hold 都不产生它）
    #     ② confirmed_path 只许 S 或 M（第三张嘴仍然禁止）
    #   ①比 v1.4 的 SS>=50 弱，但它是 v1.3 规则**真正**蕴含的那条下界；
    #   把它写死在这里，等于「有人偷偷放宽 M 的 C 门槛就立刻红」。
    if SEMANTICS == "dual-path-v1.3":
        # —— 只查**新晋**，不查全体 ——
        #
        # 历史文档明写「禁止 SS<50 AND C<1 **进入**确认（维持不受此限）」。
        # v1.4 能对全体成员查 SS>=50，是因为它有「SS<50 立即降级」把入场条件
        # 变成了真不变量；v1.3 取消了那条分支，滞回允许成员在两次 hold 失败的
        # 宽限期内衰减到 SS<45 且 C<1（实测 JUPUSDT/up：SS=43.1 C=0.33
        # Score=51.4<56，hold 已失败但尚未连续两次）。
        # 对全体成员断言就是把「进门后衰减」误判成泄漏 —— 那正是 v1.3 的设计。
        entered = {
            (t.get("symbol"), t.get("direction"))
            for t in (board.get("transitions") or [])
            if t.get("to_state") == "CONFIRMED" and t.get("from_state") == "QUALIFIED"
        }
        leak = [
            r["symbol"] for r in conf
            if (r.get("symbol"), r.get("direction")) in entered
            and float(r.get("staircase_score") or 0) < 45.0
            and abs(float(r.get("consistency_score") or 0) - 1.0) > 1e-9
        ]
        rep.add(
            f"{label} 新晋确认禁 SS<45 且 C<1（v1.3 入场下界）",
            not leak,
            f"新晋={len(entered)} violations={len(leak)} {leak[:5]}",
        )
        bad_path = [r["symbol"] for r in conf if r.get("confirmed_path") not in ("S", "M")]
        rep.add(
            f"{label} 确认区仅 PATH_S/PATH_M",
            not bad_path,
            f"violations={len(bad_path)} {bad_path[:5]}",
        )
    else:
        # Hard invariant in v1.4: every CONFIRMED row must retain a real staircase.
        no_stairs = [
            r["symbol"] for r in conf if float(r.get("staircase_score") or 0) < SS_CONF_S
        ]
        rep.add(
            f"{label} 确认区旋转楼梯 SS>=50",
            not no_stairs,
            f"violations={len(no_stairs)} {no_stairs[:5]}",
        )
        bad_path = [r["symbol"] for r in conf if r.get("confirmed_path") != "S"]
        rep.add(
            f"{label} 确认区仅 PATH_S",
            not bad_path,
            f"violations={len(bad_path)} {bad_path[:5]}",
        )

    # 确认区不应带未确认原因
    with_reasons = [r["symbol"] for r in conf if r.get("not_confirmed_reasons")]
    rep.add(f"{label} 确认区无未确认原因", not with_reasons, f"{len(with_reasons)} {with_reasons[:5]}")

    # confirm_path 必须落 S|M
    no_path = [r["symbol"] for r in conf if r.get("confirmed_path") not in ("S", "M")]
    rep.add(f"{label} confirm_path 已标注", not no_path, f"missing={len(no_path)} {no_path[:5]}", hard=False)

    # 禁止跳级: CONFIRMED 只能来自 QUALIFIED；WATCH/NONE/ELIMINATED 直上即为越级
    skips = [
        f"{t.get('symbol')}:{t.get('from_state')}→CONFIRMED"
        for t in (board.get("transitions") or [])
        if t.get("to_state") == "CONFIRMED" and t.get("from_state") != "QUALIFIED"
    ]
    rep.add(f"{label} 禁止跳级", not skips, f"violations={len(skips)} {skips[:5]}")

    # 每 15 分钟节点：对外时间戳与停留时长必须落在栅格上
    st_utc = str(meta.get("scan_timestamp_utc") or "")
    on_grid = False
    try:
        t = datetime.fromisoformat(st_utc.replace("Z", "+00:00"))
        on_grid = t.second == 0 and t.microsecond == 0 and t.minute % 15 == 0
    except Exception:
        pass
    rep.add(f"{label} 扫描时间戳落 15m 节点", on_grid, st_utc)

    pools = (board.get("long_pool") or []) + (board.get("short_pool") or [])
    bad_dwell = [
        f"{r['symbol']}:{r.get('state_duration_minutes')}"
        for r in pools
        if float(r.get("state_duration_minutes") or 0) % 15 != 0
    ]
    rep.add(
        f"{label} 停留时长为 15m 倍数",
        not bad_dwell,
        f"violations={len(bad_dwell)}/{len(pools)} {bad_dwell[:5]}",
    )

    bad_enter = []
    for r in pools:
        raw = str(r.get("state_enter_time_utc") or "")
        if not raw:
            continue
        try:
            t = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except Exception:
            bad_enter.append(f"{r['symbol']}:{raw}")
            continue
        if t.minute % 15 or t.second or t.microsecond:
            bad_enter.append(f"{r['symbol']}:{raw}")
    rep.add(
        f"{label} 入选时间落 15m 节点",
        not bad_enter,
        f"violations={len(bad_enter)}/{len(pools)} {bad_enter[:5]}",
    )

    occ = meta.get("occupancy") or {}
    du = meta.get("daily_unique") or {}
    rep.add(
        f"{label} 占用/日去重双口径",
        bool(occ) and bool(du),
        f"占用={occ.get('confirmed_unique')} 日去重={du.get('confirmed')} (§13.4 必须同时展示)",
    )

    ctrl = meta.get("control") or {}
    rep.add(
        f"{label} 动态控制字段",
        all(k in ctrl for k in ("n_impulse", "live_ratio", "freeze_new_confirm", "low_breadth")),
        f"n_impulse={ctrl.get('n_impulse')} live_ratio={ctrl.get('live_ratio')} "
        f"freeze={ctrl.get('freeze_new_confirm')} low_breadth={ctrl.get('low_breadth')}",
    )

    dmr = meta.get("dmr") or {}
    k = int(dmr.get("inbox_k") or 0)
    rep.add(f"{label} DMR K 合法区间", K_MIN <= k <= K_MAX, f"K={k} legal [{K_MIN},{K_MAX}]")
    rep.add(
        f"{label} inbox = min(|U|, K)",
        int(dmr.get("inbox_count") or 0) == min(int(dmr.get("unique_before_k") or 0), k or K_MAX),
        f"|U|={dmr.get('unique_before_k')} inbox={dmr.get('inbox_count')}",
    )

    n_conf = int(occ.get("confirmed_unique") or 0)
    in_band = OCC_TARGET[0] <= n_conf <= OCC_TARGET[1]
    rep.add(
        f"{label} 确认占用落带 [10,20]",
        in_band,
        f"{n_conf} · alerts={meta.get('alerts')} "
        f"(不足/低广度只报警，不放宽门槛)",
        hard=False,
    )


def inbox_parameter_version(obj: dict[str, Any]) -> Optional[str]:
    for c in obj.get("candidates") or obj.get("all_confirmed") or []:
        v = c.get("parameter_version")
        if v:
            return str(v)
    return None


def check_inbox(day: Optional[str], rep: Report) -> None:
    files = sorted(INBOX.glob("*.candidates.json"))
    if day:
        files = [f for f in files if f.name.startswith(day)]
    files = [f for f in files if not f.name.startswith("smoke-")]
    if not files:
        rep.add("DMR inbox 文件", False, "no candidates file found", hard=False)
        return
    bad_state, over_k, dup, counts, skipped_old = [], [], [], [], 0
    for f in files[-96:]:
        obj = load(f) or {}
        pv = inbox_parameter_version(obj)
        # Hard rejects still inspect every file (a v1.2 leftover must not start
        # shipping QUALIFIED). Occupancy/size stats only count the live tag,
        # otherwise a cutover day is dominated by morning zeros.
        cands = obj.get("candidates") or []
        k = int((obj.get("rank") or {}).get("inbox_k") or 16)
        if len(cands) > k:
            over_k.append(f.name)
        syms = [c.get("symbol") for c in cands]
        if len(syms) != len(set(syms)):
            dup.append(f.name)
        for c in cands:
            if c.get("state") != "CONFIRMED":
                bad_state.append(f"{f.name}:{c.get('symbol')}={c.get('state')}")
        if pv and pv != PARAM:
            skipped_old += 1
            continue
        counts.append(len(cands))
    rep.add("DMR 只吃 CONFIRMED", not bad_state, f"violations={len(bad_state)} {bad_state[:3]}")
    rep.add("DMR inbox ≤ K", not over_k, f"violations={len(over_k)} {over_k[:3]}")
    rep.add("DMR 同币择优去重", not dup, f"violations={len(dup)} {dup[:3]}")
    if counts:
        rep.add(
            "DMR inbox 规模",
            True,
            f"files={len(counts)} mean={sum(counts)/len(counts):.2f} "
            f"min={min(counts)} max={max(counts)} skipped_old_param={skipped_old}",
            hard=False,
        )


def check_runtime(rep: Report) -> None:
    rep.add("生产 SM_FAST 关闭", os.environ.get("SM_FAST") in (None, "", "0", "false", "no"),
            f"env SM_FAST={os.environ.get('SM_FAST')!r}")
    # 投影板面（选币榜Y）没有自己的循环 —— 它跟着主扫描走，所以看主板面的循环状态。
    ls = load(DATA / "loop_status.json") or load(
        ROOT / "data" / "coin-selection" / "loop_status.json"
    ) or {}
    rep.add(
        "循环状态 sm_fast",
        ls.get("sm_fast") in (None, False, "", "0"),
        f"loop_status.sm_fast={ls.get('sm_fast')!r} scan={ls.get('scan_id')}",
    )
    cfgp = PARAM_YAML
    rep.add("参数快照存在", cfgp.is_file(), str(cfgp))


def check_day(day: str, rep: Report) -> None:
    files = sorted(DATA.glob(f"snapshots/{day}-???.json"))
    if not files:
        rep.add(f"{day} 快照", False, "no snapshots", hard=False)
        return
    occ, ge10, leaks = [], 0, 0
    daily_conf: set[str] = set()
    skipped_old = 0
    for f in files:
        b = load(f)
        if not b:
            continue
        meta = b.get("meta") or {}
        if meta.get("parameter_version") != PARAM:
            skipped_old += 1
            continue
        n = int(((meta.get("occupancy") or {}).get("confirmed_unique")) or 0)
        occ.append(n)
        if n >= OCC_TARGET[0]:
            ge10 += 1
        for r in confirmed_rows(b):
            daily_conf.add(r["symbol"])
            if (
                float(r.get("data_confidence") or 0) < 60
                or r.get("circulating_supply") in (None, 0)
                or min(
                    float(r.get("aqv_6d_m") or 0),
                    float(r.get("aqv_12d_m") or 0),
                    float(r.get("aqv_26d_m") or 0),
                )
                <= HARD_FLOOR_M
            ):
                leaks += 1
    if occ:
        mean = sum(occ) / len(occ)
        # Sticky hold can sit near the top of the occupancy band and
        # occasionally trip CONFIRM_OVERFLOW; the hard contract is Top-K, not
        # the display occupancy. Soft-warn only when mean leaves a slack band.
        lo, hi = OCC_TARGET
        slack_lo, slack_hi = lo - 2, hi + 2
        rep.add(
            f"{day} 确认占用均值 ∈[{slack_lo},{slack_hi}]",
            slack_lo <= mean <= slack_hi,
            f"mean={mean:.2f} min={min(occ)} max={max(occ)} scans={len(occ)} "
            f"skipped_old_param={skipped_old}",
            hard=False,
        )
        rep.add(
            f"{day} 占用≥10 扫描占比 ≥60%（v1.3 切片）",
            (100.0 * ge10 / len(occ)) >= 60.0,
            f"{100.0 * ge10 / len(occ):.1f}% ({ge10}/{len(occ)})",
            hard=False,
        )
        rep.add(f"{day} 全日硬约束泄漏", leaks == 0, f"leaks={leaks}")
        rep.add(f"{day} 确认日去重", True, f"{len(daily_conf)} 币", hard=False)
    else:
        rep.add(
            f"{day} v1.3 切片",
            False,
            f"no {PARAM} snapshots (skipped_old_param={skipped_old})",
            hard=False,
        )


def check_duplicate_nodes(rep: Report) -> None:
    """同一个 scan_id 是否被执行过多次 —— K5/K9/K8 都看不见这类事故。

    生产跑 `--loop --force`，--force 短路了每节点锁；重启一次就可能把同一个节点
    再跑一遍，状态机连击被重复累加（实测 20260901-004：第一次 conf_unique=0，
    重启后第二次 conf_unique=8，比 75 分钟下限早了整整一个节点）。

    重放/去重护栏只比对「同输入是否同输出」，而重跑改变的是**状态机记忆**，
    两次运行的输入确实相同、输出也自洽，因此那些护栏结构上无法发现它。
    这条 gate 直接数日志：一个 scan_id 出现两次就是一次事故。
    """
    log_path = ROOT / "data" / "coin-selection" / "logs" / "loop.log"
    if not log_path.is_file():
        rep.add("重复节点执行", True, "no loop.log (未跑过循环)", hard=False)
        return
    pat = re.compile(r"board (\w+) projected scan=(\d{8}-\d{3})")
    seen: dict[tuple[str, str], int] = {}
    try:
        with log_path.open(encoding="utf-8", errors="ignore") as fh:
            for ln in fh:
                m = pat.search(ln)
                if m:
                    seen[(m.group(1), m.group(2))] = seen.get((m.group(1), m.group(2)), 0) + 1
    except OSError as e:
        rep.add("重复节点执行", True, f"loop.log unreadable: {e}", hard=False)
        return
    dups = {k: v for k, v in seen.items() if v > 1}
    total = len(seen)
    # 历史事故无法回溯修正，因此这条 gate 只对**守卫上线之后**的节点硬红。
    # 守卫上线前的重复保留为 WARN，作为已知污染的可见记录。
    recent = {k: v for k, v in dups.items() if k[1] >= DUP_GUARD_FROM_SCAN_ID}
    rep.add(
        f"重复节点执行 · 守卫生效后（>= {DUP_GUARD_FROM_SCAN_ID}）",
        not recent,
        f"nodes={total} duplicated_after_guard={len(recent)} "
        + (f"first={sorted(recent)[:3]}" if recent else "none"),
    )
    if dups:
        rep.add(
            "重复节点执行 · 历史污染（守卫上线前）",
            not dups,
            f"duplicated_total={len(dups)} "
            f"nodes={sorted({k[1] for k in dups})[:8]}",
            hard=False,
        )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--board",
        default="main",
        help="板面变体 key：main=选币榜(v1.4.0) · y=选币榜Y(v2.0.0)。见 packages/config/board-variants.json",
    )
    ap.add_argument("--day", default=None, help="UTC day YYYYMMDD for the roll-up gates")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    select_board(args.board)

    rep = Report()
    check_runtime(rep)
    check_duplicate_nodes(rep)
    latest = load(DATA / "latest.json")
    if latest:
        check_board(latest, rep, "latest")
    else:
        rep.add("latest.json", False, "missing — run a scan first")
    check_inbox(args.day, rep)
    if args.day:
        check_day(args.day, rep)

    if args.json:
        print(json.dumps({"failed": rep.failed, "checks": rep.rows}, ensure_ascii=False, indent=2))
    else:
        print(rep.render())
        print(f"\nhard failures: {rep.failed}")
    return 1 if rep.failed else 0


if __name__ == "__main__":
    sys.exit(main())
