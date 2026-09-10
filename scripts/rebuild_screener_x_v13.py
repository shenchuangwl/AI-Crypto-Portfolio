#!/usr/bin/env python3
"""把《选币榜X》的**全部历史**按 v1.3 语义重算，并与《复盘选币》对齐。

切换（`switch_x_to_v13.py`）只让 X **往后**按 v1.3 出数；磁盘上那 2139 个历史节点
与账本里那 4 万多笔交易仍然是 v1.4 克隆期的产物。于是会出现一个很别扭的状态：
板面挂着 v1.3 的标签，历史却是 v1.4 的数据，复盘怎么筛都对不齐。本脚本负责补上
这一步 —— 用同一套 v1.3 规则把历史整段重放出来。

**为什么 X 的历史可以重建，而 main / Y 的不行**：X 是投影板面，它的历史本来就是
`replay_screener_y.py --board x --all` 重放出来的，不是当年实盘产出的。已发布的
`x-v1.3.0-r1` 自己也写明「effective_from 采用可用历史起日的 00:00 UTC，专供以
当前规则重算历史，不声称 X 当时已上线」。所以按新规则重算历史正是它的既定用途。
main / Y 是实盘产出，绝不重写 —— 本脚本从不碰它们，源快照只读。

**单写者**：重放全程写 staging 目录，生产 X 目录由实时投影继续独占；只有最后
`--promote` 那一下做目录换名，且要求落在扫描节点间隙内。旧数据**归档不删除**。

流程（每步都可单独重跑）：
    python3 scripts/rebuild_screener_x_v13.py --plan            # 只看计划与代价
    python3 scripts/rebuild_screener_x_v13.py --staging         # 重放到 staging（约 20 分钟）
    python3 scripts/rebuild_screener_x_v13.py --verify-staging  # 对 staging 跑全套一致性闸
    python3 scripts/rebuild_screener_x_v13.py --promote         # 归档旧段 + 切换到新段
    python3 scripts/rebuild_screener_x_v13.py --rollback-promote
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services/coin-selection/src"))

REGISTRY = ROOT / "packages/config/board-variants.json"
CANDIDATE_OVERRIDES = ROOT / "packages/config/candidates/board-variants.x-adapted.json"
LIVE_X = ROOT / "data/coin-selection-x"
LIVE_INBOX = ROOT / "data/dmr-adapter-x/inbox"
STAGING = ROOT / "data/coin-selection-x-staging"
STAGING_INBOX = ROOT / "data/dmr-adapter-x-staging/inbox"
SRC = ROOT / "data/coin-selection"
STAGING_REGISTRY = ROOT / "data/coin-selection-x-staging/.board-variants.staging.json"
LOG = ROOT / "doc/verification/screener-x-v13-continue-20260906/rebuild-log.jsonl"

ADAPTED_STATE = {"selection_semantics": "dual-path-v1.3"}


def say(ok, label, detail=""):
    mark = "PASS" if ok else ("FAIL" if ok is False else "INFO")
    print(f"[{mark}] {label}" + (f"   {detail}" if detail else ""))
    return bool(ok)


def audit(event, **f):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(
            {"ts_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
             "event": event, **f}, ensure_ascii=False) + "\n")


def node_gap_seconds() -> tuple[datetime, float]:
    now = datetime.now(timezone.utc)
    nxt = now.replace(second=0, microsecond=0) + timedelta(minutes=15 - now.minute % 15)
    return nxt, (nxt - now).total_seconds()


def write_staging_registry() -> Path:
    """一份只把 X 指向 staging 的临时注册表；生产 board-variants.json 一个字节都不动。

    **overrides 直接沿用生产当前值**，不再读候选片段。
    原实现固定读 candidates/board-variants.x-adapted.json（r2 的形态），于是 X 切到
    r3(rank216) 之后，重建出来的历史仍是 r2 语义 —— 重建的全部意义就是让历史与
    线上同源，读一份可能已经过期的候选件恰好破坏了这一点。
    重建要复刻的是「现在线上跑的那套」，事实源只能是生产注册表本身。
    """
    doc = json.loads(REGISTRY.read_text(encoding="utf-8"))
    for b in doc["boards"]:
        if b["key"] == "x":
            b["data_dir"] = str(STAGING)
            b["dmr_inbox"] = str(STAGING_INBOX)
            # overrides 原样保留
    STAGING_REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    STAGING_REGISTRY.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return STAGING_REGISTRY


def ledger_stats(data_dir: Path) -> dict:
    p = data_dir / "review/ledger.sqlite"
    if not p.is_file():
        return {"ledger": "missing"}
    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    try:
        n = c.execute("select count(*) from trades").fetchone()[0]
        hashes = c.execute("select param_hash,count(*) from trades group by 1").fetchall()
        rng = c.execute("select min(enter_scan_id),max(enter_scan_id) from trades").fetchone()
        return {"trades": n, "param_hashes": hashes, "scan_range": rng}
    finally:
        c.close()


# ---------------------------------------------------------------------------
def do_plan() -> int:
    print("== 重建计划 ==")
    src_nodes = sorted(SRC.glob("snapshots/*.full.json"))
    say(bool(src_nodes), "main 源快照（只读输入）", f"{len(src_nodes)} 个 .full.json")
    say(None, "现网 X 段", json.dumps(ledger_stats(LIVE_X), ensure_ascii=False))
    live_gb = sum(f.stat().st_size for f in LIVE_X.rglob("*") if f.is_file()) / 1e9
    free_gb = shutil.disk_usage(ROOT).free / 1e9
    say(free_gb > live_gb * 1.2, "磁盘余量足够容纳 staging",
        f"现网 X {live_gb:.1f}G，可用 {free_gb:.1f}G")
    say(None, "预计重放耗时", f"约 {len(src_nodes) * 0.55 / 60:.0f} 分钟（实测 326 节点 180s）")
    x = next(b for b in json.loads(REGISTRY.read_text(encoding="utf-8"))["boards"]
             if b["key"] == "x")
    adapted = x["overrides"].get("state_config", {}).get("selection_semantics") == "dual-path-v1.3"
    say(None, "生产 X 当前形态", "ADAPTED（已切换）" if adapted else
        "CLONE（尚未切换 —— staging 可以先做，promote 应在切换之后）")
    print("\n下一步： --staging")
    return 0


def do_staging(from_scan: str | None, to_scan: str | None) -> int:
    print("== 重放到 staging（生产 X 目录只读，不受影响）==")
    if STAGING.exists():
        say(None, "清空旧 staging", str(STAGING))
        shutil.rmtree(STAGING, ignore_errors=True)
    shutil.rmtree(STAGING_INBOX.parent, ignore_errors=True)
    reg = write_staging_registry()
    say(True, "临时注册表已写", f"{reg.name}（X → staging，overrides=ADAPTED）")

    cmd = [sys.executable, str(ROOT / "scripts/replay_screener_y.py"),
           "--board", "x", "--reset", "--quiet"]
    if from_scan or to_scan:
        if from_scan:
            cmd += ["--from", from_scan]
        if to_scan:
            cmd += ["--to", to_scan]
    else:
        cmd += ["--all"]
    say(None, "开始重放", " ".join(cmd[1:]))
    env = {**os.environ, "BOARD_VARIANTS_CONFIG": str(reg)}
    r = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True)
    if r.returncode != 0:
        say(False, "重放失败", (r.stderr or r.stdout)[-600:])
        return 1
    tail = [l for l in r.stdout.splitlines() if any(
        k in l for k in ("nodes_ingested", "trade_rows", "closed_rows", "open_rows", "elapsed_s"))]
    say(True, "重放完成", " ".join(x.strip() for x in tail))
    say(None, "staging 段", json.dumps(ledger_stats(STAGING), ensure_ascii=False))
    audit("staging_built", stats=ledger_stats(STAGING))
    print("\n下一步： --verify-staging")
    return 0


def do_verify_staging() -> int:
    print("== 对 staging 跑全套一致性闸 ==")
    if not (STAGING / "latest.json").is_file():
        return 1 if not say(False, "staging 尚未构建", "先跑 --staging") else 1
    reg = write_staging_registry()
    env = {**os.environ, "BOARD_VARIANTS_CONFIG": str(reg)}
    ok = True

    # 期望身份 = 生产当前配置现算出来的那个，不写死。
    # 写死会在下一个 revision 上线后变成「验证器还在验上一版」。
    import subprocess as _sp
    want = _sp.run([sys.executable, "-c",
        "import sys;sys.path.insert(0,'services/coin-selection/src');"
        "from coin_selection.board_variants import *;from coin_selection.scan import SelectionSettings;"
        "v=get_variant('x');print(param_fingerprint(v,variant_settings(SelectionSettings(),v),"
        "variant_state_config(v)))"], capture_output=True, text=True, cwd=ROOT).stdout.strip()
    st = ledger_stats(STAGING)
    hashes = [h for h, _ in (st.get("param_hashes") or [])]
    ok &= say(hashes == [want],
              f"staging 账本身份单一且 == 生产当前身份 {want}",
              json.dumps(st, ensure_ascii=False))

    for label, cmd in (
        ("X 形态验收（ADAPTED 口径）", [sys.executable, str(ROOT / "scripts/verify_screener_x.py")]),
        ("四区硬约束闸", [sys.executable, str(ROOT / "scripts/verify_new_four_zone.py"), "--board", "x"]),
        ("在线↔离线一致性 K0–K9", [sys.executable, str(ROOT / "scripts/verify_rule_consistency.py"),
                                "--board", "x", "--sample", "12"]),
        ("复盘账本闸 C1–C14", [sys.executable, str(ROOT / "scripts/verify_review_ledger.py"),
                          "--data-dir", str(STAGING), "--check-param-hash"]),
    ):
        r = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True)
        ok &= say(r.returncode == 0, label, f"rc={r.returncode}")
        for line in r.stdout.splitlines():
            if any(k in line for k in ("216排除", "严格收窄", "hard failures", "[FAIL]", "K5", "K6")):
                print("       " + line.strip()[:170])

    audit("staging_verified", ok=ok)
    print(f"\n{'下一步： --promote' if ok else '闸未全绿，不要 promote'}")
    return 0 if ok else 1


def do_promote(force: bool) -> int:
    print("== 归档旧段 + 切换到新段 ==")
    ok = True
    ok &= say((STAGING / "latest.json").is_file(), "staging 已构建")
    x = next(b for b in json.loads(REGISTRY.read_text(encoding="utf-8"))["boards"]
             if b["key"] == "x")
    adapted = x["overrides"].get("state_config", {}).get("selection_semantics") == "dual-path-v1.3"
    if not adapted and not force:
        ok &= say(False, "生产 X 仍是 CLONE 形态",
                  "promote 前请先跑 switch_x_to_v13.py --execute（或 --force 强行）")
    nxt, slack = node_gap_seconds()
    ok &= say(slack >= 60, "距下一个扫描节点 >= 60 秒",
              f"下一个节点 {nxt.strftime('%H:%MZ')}，余 {slack:.0f}s")
    if not ok:
        print("\n前置闸未通过，未做任何改动。")
        return 1

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive = ROOT / f"data/coin-selection-x-archive-clone-{stamp}"
    before = ledger_stats(LIVE_X)
    # 归档而不是删除：旧克隆段是可追溯引用，任何时候都能拿回来对照。
    shutil.move(str(LIVE_X), str(archive))
    say(True, "旧克隆段已归档（未删除）", str(archive.relative_to(ROOT)))
    shutil.move(str(STAGING), str(LIVE_X))
    say(True, "新 v1.3 段已就位", str(LIVE_X.relative_to(ROOT)))
    if STAGING_INBOX.parent.exists():
        if LIVE_INBOX.parent.exists():
            shutil.move(str(LIVE_INBOX.parent), str(ROOT / f"data/dmr-adapter-x-archive-{stamp}"))
        shutil.move(str(STAGING_INBOX.parent), str(LIVE_INBOX.parent))
        say(True, "inbox 同步切换", str(LIVE_INBOX.parent.relative_to(ROOT)))
    # staging 注册表已无意义，删掉免得被误用
    (LIVE_X / ".board-variants.staging.json").unlink(missing_ok=True)

    # —— 补齐「建 staging 到 promote 之间」新产出的节点 ——
    #
    # staging 是某一刻的快照，而实时投影一直在往前跑。从 --staging 结束到
    # --promote 执行之间（跑四道闸、等节点间隙）会新产出若干节点，它们只存在于
    # 旧目录里；换入 staging 后这些节点就成了缺口。
    # 实测第一次 promote 就漏了 2 个（20260907-011/012），K8 覆盖闸会报。
    # 这里自动补齐 —— 缺口是流程的必然产物，不该靠人记得手动补。
    # 注意：Path("20260815-001.full.json").stem 是 "20260815-001.full"（只剥最后一层
    # 后缀），不是 "20260815-001"。用 .stem 会让**每一个**源节点都被判成缺失 ——
    # 实测一次误报 2411 个，并拿 "20260815-001.full" 当 scan_id 去跑回放。
    # 假警报比漏报更糟：它会让人以为缺口机制在工作，而它其实从没比对成功过。
    src_nodes = {p.name[: -len(".full.json")] for p in SRC.glob("snapshots/*.full.json")}
    have = {p.stem for p in (LIVE_X / "snapshots").glob("*.json")}
    missing = sorted(n for n in src_nodes - have if have and n >= min(have))
    if missing:
        say(None, "检测到 staging 与现网之间的新节点", f"{len(missing)} 个：{missing[:5]}")
        r = subprocess.run(
            [sys.executable, str(ROOT / "scripts/replay_screener_y.py"), "--board", "x",
             "--from", missing[0], "--to", missing[-1], "--quiet"],
            cwd=ROOT, capture_output=True, text=True)
        still = sorted(n for n in src_nodes - {p.stem for p in (LIVE_X/"snapshots").glob("*.json")}
                       if n >= min(have))
        say(not still, "缺口已补齐", f"rc={r.returncode} 剩余缺口={still[:5] or '无'}")
    audit("promoted", archive=str(archive), before=before, after=ledger_stats(LIVE_X))
    print(f"\n完成。回滚： --rollback-promote（归档目录 {archive.name}）")
    print("请在下一个节点产出后跑： python3 scripts/switch_x_to_v13.py --verify")
    return 0


def do_rollback_promote() -> int:
    print("== 回滚 promote ==")
    archives = sorted(ROOT.glob("data/coin-selection-x-archive-clone-*"))
    if not say(bool(archives), "找到归档段", str(archives[-1].name) if archives else "无"):
        return 1
    latest = archives[-1]
    nxt, slack = node_gap_seconds()
    if not say(slack >= 60, "距下一个扫描节点 >= 60 秒", f"余 {slack:.0f}s"):
        return 1
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    shutil.move(str(LIVE_X), str(ROOT / f"data/coin-selection-x-v13-{stamp}"))
    shutil.move(str(latest), str(LIVE_X))
    say(True, "已回到克隆段", f"v1.3 段保留为 coin-selection-x-v13-{stamp}")
    audit("promote_rolled_back", restored=str(latest))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--plan", action="store_true")
    g.add_argument("--staging", action="store_true")
    g.add_argument("--verify-staging", action="store_true")
    g.add_argument("--promote", action="store_true")
    g.add_argument("--rollback-promote", action="store_true")
    ap.add_argument("--from", dest="from_scan", default=None, help="限定重放起点（默认 --all）")
    ap.add_argument("--to", dest="to_scan", default=None, help="限定重放终点")
    ap.add_argument("--force", action="store_true", help="生产仍是 CLONE 时也允许 promote")
    a = ap.parse_args()

    if a.plan:
        return do_plan()
    if a.staging:
        return do_staging(a.from_scan, a.to_scan)
    if a.verify_staging:
        return do_verify_staging()
    if a.promote:
        return do_promote(a.force)
    if a.rollback_promote:
        return do_rollback_promote()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
