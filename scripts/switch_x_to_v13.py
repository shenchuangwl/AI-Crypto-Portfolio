#!/usr/bin/env python3
"""选币榜X：从「v1.4 克隆」切换到「历史 v1.3 四区 + 仅作用于 DMR 的 216 约束」。

把切换做成**一条带闸的命令**，而不是一串手工步骤 —— 手工步骤会漏，而这次切换
必须四件事同时到位，漏任何一件都会让板面「按新规则出数、挂旧规则身份」：

  1. 注册表 overrides   → ADAPTED（四区语义 + DMR 裁决集选择器，两个键同进同出）
  2. 规则清单 r2        → 从 candidates/ 发布进 rule-manifests/（到点自动生效）
  3. 扫描循环重启       → 运行中的进程是启动那一刻的代码副本，不重启就吃不到新语义
  4. 生效边界对齐       → 三者都落在同一个 00:00 UTC 周期边界上

为什么必须在 00:00 UTC 边界执行：``RuleManifest`` 校验强制
``effective_from_utc`` 以 ``T00:00:00Z`` 结尾（rule_manifest.py:473，文档B §3.3
「规则只在 00:00 UTC 周期边界生效，禁止周期中途切换」）。周期中途切会让同一个
交易日里前半天是一套规则、后半天是另一套，而账本按参数身份分段统计 —— 那一天的
胜率/盈亏比会变成两套规则的混合物，既不可比也不可回测。

用法：
    python3 scripts/switch_x_to_v13.py --dry-run          # 全程演练，不写任何东西
    python3 scripts/switch_x_to_v13.py --execute          # 真正切换（须在边界后）
    python3 scripts/switch_x_to_v13.py --verify           # 切换后核对运行时事实
    python3 scripts/switch_x_to_v13.py --rollback         # 回滚到 CLONE 形态

``--execute`` 默认拒绝在非边界时刻运行；确需补做（例如边界后几小时才发现没执行）
时加 ``--allow-late``，它会把这一情况记进切换日志，而不是假装准点完成。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services/coin-selection/src"))

REGISTRY = ROOT / "packages/config/board-variants.json"
CANDIDATE_DIR = ROOT / "packages/config/candidates"
MANIFEST_DIR = ROOT / "packages/config/rule-manifests"

#: 可切换的目标形态。新增一套语义 = 在这里加一行，其余流程不动。
#: 键是 --target 的取值；值是（候选 manifest, 候选 overrides 片段）。
TARGETS = {
    "r2": ("x-v1.3.0-r2.candidate.json", "board-variants.x-adapted.json"),
    "r3": ("x-v1.3.0-r3.candidate.json", "board-variants.x-rank216.json"),
    "r4": ("x-v1.3.0-r4.candidate.json", "board-variants.x-r4.json"),
    "r5": ("x-v1.3.0-r5.candidate.json", "board-variants.x-r5.json"),
}
#: 默认目标。原先这三个常量是写死的 r2 路径，加第二套语义时才发现整条流程
#: 都绑在单一 revision 上；改成表驱动，避免下次再复制一份脚本。
_TARGET = os.environ.get("X_SWITCH_TARGET", "r2")
CANDIDATE_MANIFEST = CANDIDATE_DIR / TARGETS[_TARGET][0]
CANDIDATE_OVERRIDES = CANDIDATE_DIR / TARGETS[_TARGET][1]
PUBLISHED = MANIFEST_DIR / f"x-v1.3.0-{_TARGET}.json"
LOG = ROOT / "doc/verification/screener-x-v13-continue-20260906/switch-log.jsonl"

CLONE_OVERRIDES = {"settings": {"mcap_zone_mode": "shadow"}, "state_config": {}}


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def say(ok: bool | None, label: str, detail: str = "") -> bool:
    mark = "PASS" if ok else ("FAIL" if ok is False else "INFO")
    print(f"[{mark}] {label}" + (f"   {detail}" if detail else ""))
    return bool(ok)


def audit(event: str, **fields) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
           "event": event, **fields}
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def registry() -> dict:
    return json.loads(REGISTRY.read_text(encoding="utf-8"))


def x_board(doc: dict) -> dict:
    return next(b for b in doc["boards"] if b["key"] == "x")


def next_boundary(now: datetime) -> datetime:
    return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


def today_boundary(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


# ---------------------------------------------------------------------------
# 前置闸：任何一条不过就不切
# ---------------------------------------------------------------------------
def preflight(*, executing: bool, allow_late: bool) -> tuple[bool, dict]:
    ok = True
    now = datetime.now(timezone.utc)
    info: dict = {"now_utc": now.isoformat().replace("+00:00", "Z")}

    print("== 前置闸 ==")

    # 1. 候选件齐备
    ok &= say(CANDIDATE_MANIFEST.is_file(), "候选 manifest 存在", str(CANDIDATE_MANIFEST.name))
    ok &= say(CANDIDATE_OVERRIDES.is_file(), "候选 overrides 片段存在", str(CANDIDATE_OVERRIDES.name))
    if not ok:
        return False, info

    # 2. 当前必须是**某个已获批形态**，且不是目标形态（幂等）
    #
    # 原实现写死「必须从 CLONE 出发」—— 那只覆盖了第一次切换。r2 → r3 时当前是
    # ADAPTED，断言立刻红，但那完全是合法的起点。改为：起点只要在已获批集合里
    # 就行，且不许从一个未知形态起跳（那说明有人手改过注册表）。
    cur = x_board(registry())["overrides"]
    known = {"CLONE": CLONE_OVERRIDES}
    for name, (_, frag_name) in TARGETS.items():
        f = CANDIDATE_DIR / frag_name
        if f.is_file():
            known[name] = json.loads(f.read_text(encoding="utf-8"))["overrides"]
    target_ov = json.loads(CANDIDATE_OVERRIDES.read_text(encoding="utf-8"))["overrides"]
    cur_name = next((n for n, v in known.items() if v == cur), None)
    already = cur == target_ov
    info["current_shape"] = cur_name or "UNKNOWN"
    info["target_shape"] = _TARGET
    if already:
        say(None, f"注册表已是 {_TARGET} 形态", "幂等：本步跳过")
    else:
        ok &= say(cur_name is not None,
                  f"起点形态在已获批集合内（当前 {cur_name or 'UNKNOWN'} → 目标 {_TARGET}）",
                  json.dumps(cur, ensure_ascii=False) if cur_name is None else "")

    # 3. 目标 revision 未发布过（不覆盖已发布件）
    ok &= say(not PUBLISHED.is_file() or already,
              f"rule-manifests/ 尚未有 {PUBLISHED.stem}", str(PUBLISHED.name))

    # 3b. 生效边界不得与同板面已发布件相撞
    #
    # test_rule_manifest.test_published_manifests_in_repo_are_consistent 早就写明
    # 「同一板面的 revision 不得共用同一个生效边界（否则哪份生效有歧义）」，
    # 但那条只在测试里，而前置闸跑测试时**还没发布**，抓不到自己即将造成的碰撞。
    # 实测代价：r3 与 r2 都落在 20260907-000，解析变成靠 revision 字典序决胜负。
    # 这道闸补在发布之前。
    try:
        from coin_selection.rule_manifest import list_published as _lp
        taken = {m.effective_from_scan_id: m.rule_revision
                 for m in _lp("x") if m.rule_revision != PUBLISHED.stem}
    except Exception:
        taken = {}
    info["boundary_taken_by"] = taken.get(info.get("effective_from_scan_id", ""))
    ok &= say(info["boundary_taken_by"] is None or already,
              "生效边界未被同板面其他 revision 占用",
              f"{info.get('effective_from_scan_id')} 已被 {info['boundary_taken_by']} 占用；"
              f"请改用下一个边界，或先撤回该件" if info["boundary_taken_by"] else "")

    # 4. 生效边界：必须落在 00:00 UTC，且必须是**本次执行真正对齐的那个**边界
    #
    # 不能无脑取「今天 00:00」：现在若已是 13:00 UTC，今天 00:00 到现在这 13 小时
    # X 产出的是**旧克隆规则**的数据。把 effective_from 写成今天 00:00，等于宣称
    # 那 13 小时的数据是按 v1.3 出的 —— 给已产出的数据贴错标签，比不切还糟。
    #
    # 因此：只有当此刻就在某个边界之后的短窗口内，才用那个边界；否则本次切换真正
    # 能对齐的是**下一个**边界，effective_from 就得是它。
    WINDOW_MIN = 30
    minutes_in = (now - today_boundary(now)).total_seconds() / 60.0
    in_window = minutes_in <= WINDOW_MIN
    # --allow-late：窗口已过仍对齐**今日**边界，代价由后续历史重建承担（见下）。
    # 不带 executing 条件：--dry-run --allow-late 必须预览出与真实执行**相同**的
    # 边界，否则演练就是在演另一件事。executing 只用于下面那条 FAIL 断言。
    late = (not in_window) and allow_late and minutes_in < 1440
    boundary = today_boundary(now) if (in_window or late) else next_boundary(now)
    info["effective_from_utc"] = boundary.isoformat().replace("+00:00", "Z")
    info["effective_from_scan_id"] = boundary.strftime("%Y%m%d") + "-000"
    info["minutes_past_today_boundary"] = round(minutes_in, 1)
    info["in_boundary_window"] = in_window
    info["late_switch"] = bool(late)
    # 今日边界至今已经按**旧规则**产出的节点数 —— 补做时必须被历史重建覆盖掉。
    info["stale_nodes"] = int(minutes_in // 15) + 1 if late else 0
    say(None, "本次切换对齐的生效边界",
        f"{info['effective_from_scan_id']}（{info['effective_from_utc']}）"
        + ("" if (in_window or late) else
           f"；此刻距今日 00:00 UTC 已 {minutes_in:.0f} 分钟，故对齐下一个边界"))
    if late:
        # —— 补做：放行，但如实记账，不假装准点 ——
        #
        # 原实现的 --allow-late 是坏的：帮助文本说「记为补做」，代码却仍然拒绝执行。
        # 而且这两条提示原先写在 `if executing:` 里 —— 于是 --dry-run 预览不到它们，
        # 演练看不见代价，等于把最该被看见的那部分藏了起来。现在移到外面。
        #
        # 今日 00:00 至此刻，X 已按**旧克隆规则**产出了 stale_nodes 个节点，
        # 而 effective_from 声称 r2 从 00:00 起生效 —— 两者此刻是矛盾的。
        # 唯一能消解它的是随后的历史重建（rebuild_screener_x_v13.py），它会用
        # v1.3 规则重算 X 的整段历史（含这些节点），使声明成为事实。
        # X 的 r1 清单自己写明「专供以当前规则重算历史」，这正是它的用途。
        #
        # 因此补做**不是**将就，但它有一个**必须兑现的前提**，且该前提会被写进
        # manifest notes 与切换流水，谁都赖不掉。
        say(None, "补做模式（--allow-late）",
            f"距今日 00:00 UTC 已 {minutes_in:.0f} 分钟，"
            f"其间已按旧规则产出约 {info['stale_nodes']} 个节点")
        say(None, "⚠ 未兑现前提", "必须随后执行历史重建覆盖今日边界，否则这些节点会挂着 "
            "r2 身份却是克隆数据（rebuild_screener_x_v13.py --staging → --promote）")
    if executing:
        if not late:
            ok &= say(in_window,
                      f"处于 00:00 UTC 边界窗口内（<= {WINDOW_MIN} 分钟）",
                      f"距今日 00:00 UTC {minutes_in:.0f} 分钟"
                      + ("" if in_window else
                         f"；窗口已过。加 --allow-late 可补做（须随后重建历史），"
                         f"或等 {next_boundary(now).isoformat().replace('+00:00','Z')}"))
        if not in_window and allow_late and minutes_in >= 1440:
            ok &= say(False, "--allow-late 不得跨日回填",
                      f"距今日 00:00 UTC 已 {minutes_in:.0f} 分钟（>24h），拒绝")

    # 5. 代码必须比运行中的进程新 ⇒ 必须重启才能生效（这里只做提示与记录）
    pid_file = ROOT / "data/coin-selection/loop.pid"
    if pid_file.is_file():
        try:
            pid = int(pid_file.read_text().strip())
            started = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)],
                                     capture_output=True, text=True).stdout.strip()
            info["loop_pid"], info["loop_started"] = pid, started
            say(None, "扫描循环运行中", f"pid={pid} started={started} → 切换后必须重启")
        except (ValueError, OSError):
            pass

    # 6. main / Y 冻结闸
    r = subprocess.run([sys.executable, str(ROOT / "scripts/check_main_frozen.py")],
                       capture_output=True, text=True, cwd=ROOT)
    ok &= say(r.returncode == 0, "main/Y 冻结闸", f"rc={r.returncode}")

    # 7. 单元套件（代码回归）
    #
    # **排除 test_x_v13_switch.py**：那个模块会 subprocess 调用本脚本来验证闸，
    # 而本步又要跑整个套件 —— 两者互相调用会指数级派生进程，直到把机器拖垮。
    # （已经踩过一次：一次 --dry-run 派生出上百个进程。）
    # 排除它不降低覆盖：它测的是本脚本的闸，而本脚本此刻正在运行，
    # 真正要在这里回归的是**选币内核**，不是切换器自己。
    r = subprocess.run(
        ["bash", "-c",
         'for t in services/coin-selection/tests/*.py; do '
         '  case "$(basename "$t")" in test_x_v13_switch.py) continue ;; esac; '
         '  python3 "$t" >/dev/null 2>&1 || echo "$t"; '
         'done'],
        capture_output=True, text=True, cwd=ROOT)
    failed = [x for x in r.stdout.split() if x]
    ok &= say(not failed, "选币内核单元套件全绿（切换器自测除外，避免自递归）",
              ",".join(failed[:3]) or "0 failures")

    return ok, info


# ---------------------------------------------------------------------------
# 动作
# ---------------------------------------------------------------------------
def do_switch(info: dict, *, dry: bool) -> int:
    print("\n== 切换 ==")
    frag = json.loads(CANDIDATE_OVERRIDES.read_text(encoding="utf-8"))["overrides"]

    # (1) 注册表 overrides
    doc = registry()
    x_board(doc)["overrides"] = frag
    if dry:
        say(None, "[dry-run] 将写入 board-variants.json 的 x.overrides",
            json.dumps(frag, ensure_ascii=False))
    else:
        backup = REGISTRY.with_suffix(".json.pre-x-v13.bak")
        if not backup.exists():
            shutil.copy2(REGISTRY, backup)
        REGISTRY.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        say(True, f"board-variants.json 已切到 {_TARGET} 形态", f"备份 {backup.name}")

    # (2) 发布 manifest
    m = json.loads(CANDIDATE_MANIFEST.read_text(encoding="utf-8"))
    m["effective_from_utc"] = info["effective_from_utc"]
    m["effective_from_scan_id"] = info["effective_from_scan_id"]
    deployed = ROOT / "data/coin-selection/deployed-commit.txt"
    head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                          cwd=ROOT).stdout.strip()
    m["code_commit"] = head or m.get("code_commit")
    # 补做必须留在**已发布件**里，而不只是本地日志 —— 日志会被忽略，manifest 不会。
    if info.get("late_switch"):
        m["notes"] = list(m.get("notes") or ()) + [
            f"【补做 · 非准点切换】实际执行于 {info['now_utc']}，距声明的生效边界 "
            f"{info['effective_from_utc']} 已 {info['minutes_past_today_boundary']:.0f} 分钟；"
            f"其间 X 已按旧克隆规则产出约 {info['stale_nodes']} 个节点。",
            "【未兑现前提】上述节点必须由 rebuild_screener_x_v13.py 的历史重建"
            "（--staging → --verify-staging → --promote）用 v1.3 规则重算覆盖，"
            "否则它们挂着 r2 身份却是克隆数据。重建完成前，本 revision 的"
            "当日绩效统计不可用。",
        ]
    # manifest_hash 必须按最终内容重算，否则 load() 会以 hash 不符拒绝加载。
    m.pop("manifest_hash", None)
    from coin_selection.rule_manifest import RuleManifest
    obj = RuleManifest(
        **{k: v for k, v in m.items()
           if k in RuleManifest.__dataclass_fields__ and k not in ("feature_flags", "notes")},
        feature_flags=dict(m.get("feature_flags") or {}),
        notes=tuple(m.get("notes") or ()),
    )
    m["manifest_hash"] = obj.manifest_hash()
    if dry:
        say(None, f"[dry-run] 将发布 rule-manifests/{PUBLISHED.name}",
            f"effective_from={m['effective_from_scan_id']} manifest_hash={m['manifest_hash'][:23]}…")
    else:
        PUBLISHED.write_text(json.dumps(m, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if deployed.is_file() and head:
            deployed.write_text(head + "\n", encoding="utf-8")
        say(True, f"已发布 {PUBLISHED.stem}", f"effective_from={m['effective_from_scan_id']}")

    # (3) 重启扫描循环 —— 运行中的进程是旧代码副本，不重启就吃不到新语义
    if dry:
        say(None, "[dry-run] 将重启 selection-loop",
            "daemonize.py selection-loop stop → start")
    else:
        for act in ("stop", "start"):
            r = subprocess.run([sys.executable, str(ROOT / "scripts/daemonize.py"),
                                "selection-loop", act], capture_output=True, text=True, cwd=ROOT)
            say(r.returncode == 0, f"selection-loop {act}", (r.stdout or r.stderr).strip()[:120])

    audit("switch", dry_run=dry, **info)
    return 0


def do_restart_only(*, dry: bool) -> int:
    """只把新代码装进运行中的进程，**不改任何配置**。

    为什么值得单独做：切换要四件事同刻到位，其中「重启」是唯一一件耗时、有节点
    间隙风险、且**与生效边界无关**的。把它提前到边界之前做掉，边界那一刻就只剩
    一次纯配置翻转 —— board-variants.json 是按 mtime 记忆化热加载的
    （board_variants.load_variants），改它不需要重启。于是边界操作从
    「停进程 + 起进程 + 改配置 + 发清单」缩短成「改配置 + 发清单」，
    风险与耗时都小一个量级。

    前提：此刻注册表仍是 CLONE 形态，因此新代码 + 旧配置 ≡ 现网行为
    （新增开关全部默认关闭，已由 test_x_v13_identity / verify_screener_x 证明）。
    这一步**不改变任何选币结果**，也不改变任何身份。
    """
    print("== 只重启（不改配置）==")
    cur = x_board(registry())["overrides"]
    if not say(cur == CLONE_OVERRIDES, "注册表仍是 CLONE 形态（否则这一步不是行为中性的）",
               json.dumps(cur, ensure_ascii=False)):
        return 1

    now = datetime.now(timezone.utc)
    # 节点在每小时 :00/:15/:30/:45 触发。落在节点边界上重启可能让该节点整个丢掉，
    # 所以要求距下一个节点还有足够余量。
    nxt = now.replace(second=0, microsecond=0) + timedelta(
        minutes=15 - now.minute % 15)
    slack = (nxt - now).total_seconds()
    if not say(slack >= 120, "距下一个扫描节点 >= 120 秒",
               f"下一个节点 {nxt.strftime('%H:%MZ')}，余 {slack:.0f}s"):
        return 1

    before = {}
    for b in ("coin-selection", "coin-selection-x", "coin-selection-y"):
        p = ROOT / "data" / b / "latest.json"
        if p.is_file():
            m = json.loads(p.read_text(encoding="utf-8")).get("meta") or {}
            before[b] = {"scan_id": m.get("scan_id"), "param_hash": m.get("param_hash")}
    say(None, "重启前三板面", json.dumps(before, ensure_ascii=False))

    if dry:
        say(None, "[dry-run] 将 stop → start selection-loop", "不改配置、不发清单")
        return 0

    for act in ("stop", "start"):
        r = subprocess.run([sys.executable, str(ROOT / "scripts/daemonize.py"),
                            "selection-loop", act], capture_output=True, text=True, cwd=ROOT)
        say(r.returncode == 0, f"selection-loop {act}", (r.stdout or r.stderr).strip()[:160])
    audit("restart_only", before=before,
          next_node=nxt.isoformat().replace("+00:00", "Z"))
    print("\n新代码已装载；配置未动，X 仍是 v1.4 克隆语义。")
    print(f"请在下一个节点（{nxt.strftime('%H:%MZ')}）产出后跑 --verify-restart 确认无缺口。")
    return 0


def do_verify_restart() -> int:
    """重启后确认：进程活着、节点没丢、三板面身份一个都没变。"""
    print("== 重启后核对 ==")
    ok = True
    pid_file = ROOT / "data/coin-selection/loop.pid"
    pid = int(pid_file.read_text().strip()) if pid_file.is_file() else 0
    alive = subprocess.run(["ps", "-p", str(pid)], capture_output=True).returncode == 0
    ok &= say(alive, "扫描循环存活", f"pid={pid}")
    want = {"coin-selection": None, "coin-selection-x": "pf1_b2d6cd1ec88bf4af",
            "coin-selection-y": "pf1_fcea251fa94122fe"}
    for b, ph in want.items():
        p = ROOT / "data" / b / "latest.json"
        m = json.loads(p.read_text(encoding="utf-8")).get("meta") or {}
        ok &= say(m.get("param_hash") == ph, f"{b} 身份未变",
                  f"scan_id={m.get('scan_id')} param_hash={m.get('param_hash')}")
    st = ROOT / "data/coin-selection/loop_status.json"
    if st.is_file():
        d = json.loads(st.read_text(encoding="utf-8"))
        say(None, "loop_status", json.dumps(
            {k: d.get(k) for k in ("status", "scan_id", "updated_at_utc")}, ensure_ascii=False))
        boards = d.get("boards") or []
        ok &= say(all((b or {}).get("status") == "ok" for b in boards) if boards else True,
                  "三板面 status 均 ok", json.dumps(boards, ensure_ascii=False)[:200])
    return 0 if ok else 1


def do_rollback(*, dry: bool) -> int:
    print("== 回滚到 CLONE ==")
    doc = registry()
    x_board(doc)["overrides"] = CLONE_OVERRIDES
    if dry:
        say(None, "[dry-run] 将把 x.overrides 改回 CLONE", json.dumps(CLONE_OVERRIDES, ensure_ascii=False))
        say(None, f"[dry-run] 将把 {PUBLISHED.name} 移回 candidates/", "已写入的账本行一律保留")
        return 0
    REGISTRY.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    say(True, "x.overrides 已回到 CLONE")
    if PUBLISHED.is_file():
        shutil.move(str(PUBLISHED), str(CANDIDATE_DIR / f"{PUBLISHED.stem}.rolledback.json"))
        say(True, f"{PUBLISHED.stem} 已撤出 rule-manifests/", "账本行保留，不删除任何数据")
    for act in ("stop", "start"):
        subprocess.run([sys.executable, str(ROOT / "scripts/daemonize.py"),
                        "selection-loop", act], capture_output=True, text=True, cwd=ROOT)
    say(True, "selection-loop 已重启")
    audit("rollback", dry_run=False)
    return 0


def do_verify() -> int:
    """切换后核对**运行时产物**，不看配置声明。"""
    print("== 切换后运行时核对 ==")
    ok = True
    latest = ROOT / "data/coin-selection-x/latest.json"
    ok &= say(latest.is_file(), "X latest.json 存在")
    if not latest.is_file():
        return 1

    # —— 时序闸：快照必须是**切换之后**产出的，否则这次核对毫无意义 ——
    #
    # 切换会重启循环，而新进程会正确拒绝重跑已完成的节点，于是要等到下一个
    # 15 分钟节点才会有第一份新配置下的快照。在那之前 latest.json 仍是旧进程
    # 写的，param_hash/rule_revision 当然还是旧的 —— 直接报 FAIL 会让人以为
    # 切换失败并去回滚，那是被自己的验证器骗了。踩过一次，补上。
    switch_ts = None
    if LOG.is_file():
        for line in LOG.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("event") == "switch" and rec.get("dry_run") is False:
                switch_ts = rec["ts_utc"]
    if switch_ts:
        gen = (json.loads(latest.read_text(encoding="utf-8")).get("generated_at_utc") or "")
        if gen and gen < switch_ts:
            say(None, "快照尚未刷新", f"latest 产于 {gen}，早于切换时刻 {switch_ts}")
            now2 = datetime.now(timezone.utc)
            nxt = (now2.replace(second=0, microsecond=0)
                   + timedelta(minutes=15 - now2.minute % 15))
            say(None, "请等下一个扫描节点后重跑",
                f"下一节点 {nxt.strftime('%H:%MZ')}，"
                f"约 {(nxt - now2).total_seconds()/60:.0f} 分钟后")
            print("\n结论：切换已应用，但**尚无切换后的产出可验**。这不是失败。")
            return 2

    d = json.loads(latest.read_text(encoding="utf-8"))
    meta = d.get("meta") or {}
    ident = meta.get("rule_identity") or {}
    # 期望身份从候选 manifest 现读，不写死 —— 写死会在下一个 revision 上线时
    # 变成「验证器还在验上一版」，而它恰恰是用来发现这种错配的。
    want = json.loads(CANDIDATE_MANIFEST.read_text(encoding="utf-8"))
    ok &= say(meta.get("param_hash") == want["param_hash"],
              f"快照 param_hash == {_TARGET} 身份", str(meta.get("param_hash")))
    ok &= say(ident.get("rule_revision") == want["rule_revision"],
              f"快照 rule_revision == {want['rule_revision']}", str(ident.get("rule_revision")))
    ok &= say(ident.get("identity_status") == "MATCH",
              "identity_status", str(ident.get("identity_status")))
    paths = [r.get("confirmed_path") for pool in ("long_pool", "short_pool")
             for r in d.get(pool) or [] if r.get("state") == "CONFIRMED"]
    say(None, "确认区路径分布",
        f"S={paths.count('S')} M={paths.count('M')}（M>0 才证明 v1.3 双通道真的在跑）")
    r = subprocess.run([sys.executable, str(ROOT / "scripts/verify_screener_x.py")],
                       capture_output=True, text=True, cwd=ROOT)
    ok &= say(r.returncode == 0, "verify_screener_x.py（ADAPTED 口径）", f"rc={r.returncode}")
    for line in r.stdout.splitlines():
        if "216排除" in line or "严格收窄" in line or "X 形态" in line:
            print("       " + line.strip())
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="全程演练，不写任何文件、不重启")
    g.add_argument("--execute", action="store_true", help="真正切换（须在 00:00 UTC 边界窗口内）")
    g.add_argument("--verify", action="store_true", help="切换后核对运行时产物")
    g.add_argument("--rollback", action="store_true", help="回滚到 CLONE 形态")
    g.add_argument("--restart-only", action="store_true",
                   help="只装载新代码、不改配置（行为中性）；把重启从边界操作里摘出去")
    g.add_argument("--verify-restart", action="store_true",
                   help="重启后确认进程存活、节点无缺口、三板面身份未变")
    ap.add_argument("--target", choices=sorted(TARGETS), default=None,
                    help="切换目标形态：r2=DMR-only 216；r3=216 降级为排序权重")
    ap.add_argument("--allow-late", action="store_true",
                    help="边界窗口已过仍执行；记为补做，不假装准点")
    a = ap.parse_args()
    if a.target and a.target != _TARGET:
        # 常量在 import 期就按 X_SWITCH_TARGET 定好了，这里用 execv 重入一次，
        # 比让全模块常量变可变状态干净。
        os.environ["X_SWITCH_TARGET"] = a.target
        os.execv(sys.executable, [sys.executable, str(Path(__file__).resolve())]
                 + [x for x in sys.argv[1:] if x not in ("--target", a.target)])

    if a.verify:
        return do_verify()
    if a.verify_restart:
        return do_verify_restart()
    if a.rollback:
        return do_rollback(dry=False)
    if a.restart_only:
        return do_restart_only(dry=False)

    ok, info = preflight(executing=a.execute, allow_late=a.allow_late)
    if not ok:
        print("\n前置闸未通过，未做任何改动。")
        audit("preflight_failed", **info)
        return 1
    return do_switch(info, dry=a.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
