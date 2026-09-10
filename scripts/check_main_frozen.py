#!/usr/bin/env python3
"""主板冻结闸 —— 文档B §9.3 的 CI 护栏（对应文档A 红线第 11 条、冻结回归 F5/F6）。

    python3 scripts/check_main_frozen.py

解析 ``packages/config/board-variants.json``，断言 ``boards[key=main]`` 与基线
逐键相等，同时把两道执行层锁一并钉住。任何一条不成立 → 退出码 1，CI 红。

为什么单独一个脚本而不是并进单测：CI 里它要能在**不装依赖、不导入服务代码**的
前提下跑（纯标准库读 JSON），这样「有人在 PR 里顺手动了 main 那一段」这件事
在最早的一步就被拦住。
"""

from __future__ import annotations

import ast
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
#: 与 coin_selection.board_variants 用同一个环境变量解析注册表，理由有二：
#:   1. 候选配置（尚未获批切换的形态）必须能在**不改生产注册表**的前提下过一遍本闸，
#:      否则「切换前先验证」这句话没有可执行的做法；
#:   2. 闸与库读同一份文件，才不会出现「库按 A 跑、闸按 B 判」。
#: 不设该变量时行为与改造前逐字节一致。
REGISTRY = Path(
    os.environ.get(
        "BOARD_VARIANTS_CONFIG", str(ROOT / "packages" / "config" / "board-variants.json")
    )
)

#: 主板基线。**这就是「不得改动」的机器可读版本**，改它 = 动生产选币榜。
MAIN_BASELINE = {
    "key": "main",
    "label": "选币榜",
    "parameter_version": "param-v1.4.0-staircase-confirm-dmr",
    "param_file": "packages/config/param-v1.4.0-staircase-confirm-dmr.yaml",
    "data_dir": "data/coin-selection",
    "dmr_inbox": "data/dmr-adapter/inbox",
    "api_prefix": "/api/v1/screener",
    "web_route": "/screener",
    "primary": True,
    "projected": False,
    "dmr_executable": True,
    "enabled": True,
}

#: 选币榜Y 的两道执行层锁（文档A §1.3）。这两条允许 Y 调参，但永远不许解锁执行。
Y_LOCKS = {
    "dmr_executable": False,
    "dmr_inbox": "data/dmr-adapter-y/inbox",
    "parameter_version": "param-v2.0.0-screener-y",
}

# 选币榜X v1.3.0 复刻 main v1.4.0，独立复盘闭环；未来只经 X overrides 演进。
# 解锁执行必须另行授权并同步本断言，不能顺带把观察板面放进纸面执行层。
X_LOCKS = {
    "dmr_executable": False,
    "dmr_inbox": "data/dmr-adapter-x/inbox",
    "parameter_version": "param-v1.3.0-screener-x",
}

fail = 0


def check(name: str, ok: bool, extra: str = "") -> None:
    global fail
    print(f"[{'PASS' if ok else 'FAIL'}] {name:<44} {extra}")
    if not ok:
        fail += 1


def main() -> int:
    doc = json.loads(REGISTRY.read_text(encoding="utf-8"))
    boards = {b["key"]: b for b in doc.get("boards") or []}

    main_b = boards.get("main")
    check("boards[key=main] 存在", main_b is not None)
    if main_b is None:
        return 1

    for k, want in MAIN_BASELINE.items():
        got = main_b.get(k)
        check(f"main.{k}", got == want, f"{got!r} (want {want!r})")

    ov = main_b.get("overrides") or {}
    check("main.overrides.settings 恒空", ov.get("settings") == {}, repr(ov.get("settings")))
    check("main.overrides.state_config 恒空", ov.get("state_config") == {}, repr(ov.get("state_config")))
    check("main.cycle.enabled 恒 false", (main_b.get("cycle") or {}).get("enabled") is False)

    y = boards.get("y")
    check("boards[key=y] 存在", y is not None)
    if y is not None:
        for k, want in Y_LOCKS.items():
            check(f"y.{k}", y.get(k) == want, f"{y.get(k)!r} (want {want!r})")
        check(
            "y 与 main 的 inbox 是不同目录",
            y.get("dmr_inbox") != main_b.get("dmr_inbox"),
            f"{y.get('dmr_inbox')} vs {main_b.get('dmr_inbox')}",
        )
        check(
            "y 与 main 的 data_dir 是不同目录",
            y.get("data_dir") != main_b.get("data_dir"),
            f"{y.get('data_dir')} vs {main_b.get('data_dir')}",
        )
        # 红线第 20 条：overrides 与 YAML 必须同一次提交同步改
        yaml_path = ROOT / (y.get("param_file") or "")
        if yaml_path.is_file():
            text = yaml_path.read_text(encoding="utf-8")
            ws = (y.get("overrides") or {}).get("settings") or {}
            pairs = (
                ("w_ss", "ss"),
                ("w_mom", "momentum"),
                ("w_liq", "liquidity"),
                ("w_mcap", "mcap"),
                ("w_cons", "consistency"),
                ("w_rank", "rank"),
                ("w_risk", "risk"),
            )
            bad = []
            for field, yaml_key in pairs:
                if field not in ws:
                    continue
                if f"{yaml_key}: {ws[field]:.2f}" not in text:
                    bad.append(f"{yaml_key}!={ws[field]}")
            check("红线20 overrides 与 YAML 权重同步", not bad, ",".join(bad))
            if ws:
                total = sum(float(ws.get(f, 0)) for f, _ in pairs)
                check("w_* 七项求和 == 1.00", abs(total - 1.0) < 1e-9, f"{total:.6f}")

    # X v1.3.0 复刻/复盘/未来独立演进的机器边界，main 基线与 Y 锁均不改。
    x = boards.get("x")
    check("boards[key=x] 存在", x is not None)
    if x is not None:
        for k, want in X_LOCKS.items():
            check(f"x.{k}", x.get(k) == want, f"{x.get(k)!r} (want {want!r})")
        # —— X 的 overrides 必须精确落在两套**已获批**的形态之一 ——
        #
        # 原断言是 `state_config == {}`（把「X 永远等于 main 克隆」写死）。X 一旦
        # 采用历史 v1.3 四区语义就必然非空，那条断言会红 —— 但正确的修法不是删掉
        # 它、改成宽松的 contains，那等于把「不许随手改 X 参数」这道闸一并拆了。
        #
        # 改为白名单式：只认下面两种形态，多一个键、少一个键、值不对都红。
        #   CLONE      现网形态：100% 复刻 main v1.4.0
        #   ADAPTED    历史 v1.3 四区 + 仅作用于 DMR 的 216 约束（待授权切换）
        #
        # 两个键必须同进同出：dmr_selection_mode=confirmed-basic-216-v1.3 的前提是
        # 四区已经是 dual-path-v1.3（build_dmr_messages 里也有同款守卫），
        # 只开一半属于「第三套语义」，此处直接判红。
        ov = x.get("overrides") or {}
        x_state = ov.get("state_config") or {}
        x_settings = ov.get("settings") or {}
        X_SHAPES = {
            "CLONE(v1.4 复刻)": ({}, {"mcap_zone_mode": "shadow"}),
            "ADAPTED(v1.3 四区 + DMR-only 216)": (
                {"selection_semantics": "dual-path-v1.3"},
                {"mcap_zone_mode": "shadow",
                 "dmr_selection_mode": "confirmed-basic-216-v1.3"},
            ),
            # r5：与 r4 同参，唯 216 改回安保准入（产品选择，以盈利率换盈亏比）。
            "R5(v1.3四区 + 确认区M>=75 + 216安保准入)": (
                {"selection_semantics": "dual-path-v1.3",
                 "mom_confirmed": 75, "mom_confirmed_m": 75},
                {"mcap_zone_mode": "shadow",
                 "dmr_selection_mode": "confirmed-basic-216-v1.3"},
            ),
            # r4：在 RANK216 基础上把确认区动能门槛提到 75/75（回测 +60% 收益）。
            "R4(v1.3四区 + 确认区M>=75 + 216排序)": (
                {"selection_semantics": "dual-path-v1.3",
                 "mom_confirmed": 75, "mom_confirmed_m": 75},
                {"mcap_zone_mode": "shadow",
                 "dmr_selection_mode": "confirmed-basic-rank216-v1.3"},
            ),
            # 216 退出 DMR 准入、改做排序权重（依据：天花板做准入实测为反向指标）。
            "RANK216(v1.3 四区 + 216 排序权重)": (
                {"selection_semantics": "dual-path-v1.3"},
                {"mcap_zone_mode": "shadow",
                 "dmr_selection_mode": "confirmed-basic-rank216-v1.3"},
            ),
        }
        shape = next(
            (name for name, (s, t) in X_SHAPES.items()
             if x_state == s and x_settings == t),
            None,
        )
        check(
            "x.overrides 形态属于已获批集合",
            shape is not None,
            f"state_config={x_state!r} settings={x_settings!r}"
            if shape is None else shape,
        )
        check("x.cycle.enabled 恒 false", (x.get("cycle") or {}).get("enabled") is False)
        for key in ("main", "y"):
            other = boards.get(key) or {}
            for field in ("data_dir", "dmr_inbox"):
                check(f"x 与 {key} 的 {field} 不同", bool(x.get(field)) and
                      (ROOT / x[field]).resolve() != (ROOT / (other.get(field) or "")).resolve())

    # 执行层白名单：不导入 dmr-adapter 也要能查（纯标准库，不读取 .env）。
    adapter = ROOT / "services" / "dmr-adapter" / "src" / "dmr_adapter" / "adapter.py"
    if adapter.is_file():
        src = adapter.read_text(encoding="utf-8")
        start = src.find("DEFAULT_PARAM_WHITELIST")
        window = src[start : start + 600] if start >= 0 else ""
        check(
            "param-v2.0.0-screener-y 不在 DMR_PARAM_WHITELIST",
            "param-v2.0.0-screener-y" not in window,
            "",
        )
        defaults = None
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and
                    t.id == "DEFAULT_PARAM_WHITELIST" for t in node.targets):
                defaults = ast.literal_eval(node.value)
        check("X 不在 DEFAULT_PARAM_WHITELIST", defaults is not None and
              X_LOCKS["parameter_version"] not in defaults)
        runtime = {v.strip() for v in os.environ.get("DMR_PARAM_WHITELIST", "").split(",")}
        check("X 不在运行期 DMR_PARAM_WHITELIST", X_LOCKS["parameter_version"] not in runtime)

    print(f"\nhard failures: {fail}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
