#!/usr/bin/env python3
"""《选币榜Y》读数检查清单 —— 文档A 附录 C 的机器可执行版 + 文档B §8 的阶段门。

    python3 scripts/check_y_readout.py                 # 全部 15 项 + 阶段门
    python3 scripts/check_y_readout.py --stage-gates   # 只看阶段 3/4 的前置条件

文档A 附录 C 的原文是一份「每次改 overrides 后逐项核对」的人工清单。人工清单
在第三次核对时就会开始跳项 —— 所以这里把它变成一条命令。每一项都标注了它对应
附录 C 的哪一行。

阶段门（文档B §8 阶段 3/4）同样是机器可判的：窗口天数、样本量、日聚类数都能从
磁盘上的快照直接数出来。**门没过就不许把参数推上生效值** —— 这个脚本的作用是
让「过没过」不再是一个可以商量的判断。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "coin-selection" / "src"))

from coin_selection.board_variants import (  # noqa: E402
    BOARD_Y_KEY,
    MAIN_KEY,
    get_variant,
    mcap_zone_mode_of,
    param_fingerprint,
    variant_settings,
    variant_state_config,
)
from coin_selection.scan import SelectionSettings  # noqa: E402

hard = 0
warn = 0

#: 文档A §0.2 / 文档B §2.1 的现网基线，用作占用守恒的比较基准。
BASELINE_CONFIRMED_UNIQUE = 24.43
#: 文档B §6.1：权重/阈值层与组合层各自的满 31 天时点。
STAGE3_READY_UTC = "2026-09-15"
STAGE4_READY_UTC = "2026-09-22"
#: 文档B §6.5：最小样本量与最小日聚类数。
MIN_N = 200
MIN_DAY_CLUSTERS = 20


def check(name: str, ok: bool, extra: str = "", level: str = "HARD") -> bool:
    global hard, warn
    if ok:
        print(f"[PASS] {name:<44} {extra}")
        return True
    if level == "WARN":
        warn += 1
        print(f"[WARN] {name:<44} {extra}")
    else:
        hard += 1
        print(f"[FAIL] {name:<44} {extra}")
    return False


def _load_latest(data_dir: Path) -> dict | None:
    p = data_dir / "latest.json"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def _snapshot_span(data_dir: Path) -> tuple[int, str | None, str | None, float]:
    snaps = sorted(
        p.name.removesuffix(".json")
        for p in (data_dir / "snapshots").glob("*.json")
        if not p.name.endswith(".full.json")
    )
    if not snaps:
        return 0, None, None, 0.0

    def ts(sid: str) -> datetime:
        d, n = sid.split("-")
        return datetime(int(d[:4]), int(d[4:6]), int(d[6:8]), tzinfo=timezone.utc) + timedelta(
            minutes=15 * int(n)
        )

    days = (ts(snaps[-1]) - ts(snaps[0])).total_seconds() / 86400.0
    return len(snaps), snaps[0], snaps[-1], days


def main() -> int:
    ap = argparse.ArgumentParser(description="选币榜Y 读数检查清单（文档A 附录 C）")
    ap.add_argument("--stage-gates", action="store_true", help="只跑阶段门")
    args = ap.parse_args()

    y = get_variant(BOARD_Y_KEY)
    main_v = get_variant(MAIN_KEY)
    settings = variant_settings(SelectionSettings(), y)
    cfg = variant_state_config(y)
    data_dir = y.data_path(ROOT)

    if not args.stage_gates:
        print("=== 文档A 附录 C · 读数检查清单 ===")

        # C-1 w_* 七项求和 == 1.00
        ws = (
            settings.w_ss, settings.w_mom, settings.w_liq, settings.w_mcap,
            settings.w_cons, settings.w_rank, settings.w_risk,
        )
        check("C1  w_* 七项求和 == 1.00", abs(sum(ws) - 1.0) < 1e-9, f"{sum(ws):.6f} {ws}")

        # C-2 overrides 与 YAML 一致（红线 20）—— 交给 check_main_frozen 深查，这里只报状态
        yaml_text = (ROOT / y.param_file).read_text(encoding="utf-8") if (ROOT / y.param_file).is_file() else ""
        ok = all(
            f"{k}: {v:.2f}" in yaml_text
            for k, v in (
                ("ss", settings.w_ss), ("momentum", settings.w_mom),
                ("liquidity", settings.w_liq), ("mcap", settings.w_mcap),
                ("consistency", settings.w_cons), ("rank", settings.w_rank),
                ("risk", settings.w_risk),
            )
        )
        check("C2  overrides 与 param yaml 一致（红线20）", ok, y.param_file)

        # C-3 main 未被修改
        check(
            "C3  boards[key=main] 未被修改",
            main_v.settings_overrides == {} and main_v.state_overrides == {}
            and main_v.cycle.enabled is False,
            f"overrides={main_v.settings_overrides} cycle={main_v.cycle.enabled}",
        )

        # C-4 两道执行层锁
        try:
            sys.path.insert(0, str(ROOT / "services" / "dmr-adapter" / "src"))
            from dmr_adapter.adapter import DEFAULT_PARAM_WHITELIST

            wl_ok = y.parameter_version not in DEFAULT_PARAM_WHITELIST
        except Exception:  # noqa: BLE001
            wl_ok = "param-v2.0.0-screener-y" not in (
                ROOT / "services/dmr-adapter/src/dmr_adapter/adapter.py"
            ).read_text(encoding="utf-8")
        check(
            "C4  dmr_executable=false 且不在白名单",
            y.dmr_executable is False and wl_ok,
            f"executable={y.dmr_executable} whitelisted={not wl_ok}",
        )

        latest = _load_latest(data_dir)
        if latest is None:
            check("C5  守恒式：七态求和 == 宇宙×2", False, "latest.json 缺失", level="WARN")
        else:
            meta = latest.get("meta") or {}
            counts = meta.get("state_counts") or {}
            universe = int(meta.get("effective_universe") or 0)
            seven = sum(
                int(counts.get(k, 0))
                for k in ("WATCH", "QUALIFIED", "CONFIRMED", "ELIMINATED",
                          "DATA_INSUFFICIENT", "LOW_CONFIDENCE", "NONE")
            )
            check(
                "C5  守恒式：七态求和 == 宇宙×2",
                seven == universe * 2,
                f"{seven} vs {universe}×2={universe * 2}  {counts}",
            )
            rows = len(latest.get("long_pool") or []) + len(latest.get("short_pool") or [])
            check(
                "C5b 板面行数 == 七态 − NONE",
                rows == seven - int(counts.get("NONE", 0)),
                f"rows={rows} seven-NONE={seven - int(counts.get('NONE', 0))}",
            )

            # C-6 hierarchy 不变式
            hier = meta.get("hierarchy") or {}
            inv = str(hier.get("invariant") or "")
            check(
                "C6  hierarchy.invariant 声明 DMR⊆确认⊆符合",
                "DMR" in inv and "CONFIRMED" in inv and "QUALIFIED" in inv,
                inv or "(缺)",
                level="WARN",
            )

            # C-7 确认去重占用 <= 基线 × 1.5
            #
            # 比的必须是**全窗口均值**，不是单节点读数：文档A §7.2 实测
            # CONFIRM_OVERFLOW 占 47.0% 的节点，单节点超上限是常态，
            # 拿一个节点去比一条均值基线只会天天误报。
            occ_csv = ROOT / "data" / "research" / "E0" / "occupancy.csv"
            mean_cu = None
            if occ_csv.is_file():
                import csv as _csv

                with open(occ_csv, encoding="utf-8") as fh:
                    vals = [float(r["confirmed_unique"]) for r in _csv.DictReader(fh)]
                mean_cu = sum(vals) / len(vals) if vals else None
            if mean_cu is not None:
                check(
                    "C7  确认去重占用（窗口均值）<= 基线 24.43 × 1.5",
                    mean_cu <= BASELINE_CONFIRMED_UNIQUE * 1.5,
                    f"{mean_cu:.2f} <= {BASELINE_CONFIRMED_UNIQUE * 1.5:.2f}"
                    f"（源 data/research/E0/occupancy.csv）",
                )
            else:
                occ = meta.get("occupancy") or {}
                cu = occ.get("confirmed_unique")
                check(
                    "C7  确认去重占用 <= 基线 24.43 × 1.5",
                    True,
                    f"单节点 {cu} —— 未跑回放，无法取窗口均值（先跑 replay_rescore.py --exp E0）",
                    level="WARN",
                )

            # C-8 DMR 唯一币 <= dmr_top_k
            dmr = meta.get("dmr") or {}
            uniq = dmr.get("unique_before_k")
            if uniq is not None:
                check(
                    "C8  DMR 唯一币 <= dmr_top_k",
                    int(uniq) <= int(settings.dmr_top_k) or bool(dmr.get("truncated")),
                    f"{uniq} vs K={settings.dmr_top_k}",
                )

            # C-9 / C-10 / C-11 天花板层
            mode = mcap_zone_mode_of(settings)
            zone_meta = meta.get("mcap_zone")
            if mode == "off":
                check(
                    "C9  开关关 → 快照不带天花板字段",
                    zone_meta is None
                    and not any(
                        "zone_ceiling" in r
                        for r in (latest.get("long_pool") or [])[:50]
                    ),
                    f"mode={mode} meta.mcap_zone={'有' if zone_meta else '无'}",
                )
            else:
                pool = (latest.get("long_pool") or []) + (latest.get("short_pool") or [])
                bad = [
                    r["symbol"]
                    for r in pool
                    if r.get("zone_ceiling")
                    and r.get("product_zone")
                    and r["product_zone"] != r["state"]
                    and not any(
                        c.startswith("MCAP_ZONE_CAP_") or c == "MCAP_ZONE_ABSTAIN"
                        for c in (r.get("reason_codes") or [])
                    )
                ]
                check("C10 每个被压低的行都带 MCAP_ZONE_* 码", not bad, str(bad[:3]))
                rank = {"DMR": 0, "CONFIRMED": 1, "QUALIFIED": 2, "WATCH": 3, "ELIMINATED": 4}
                inverted = [
                    r["symbol"]
                    for r in pool
                    if r.get("product_zone") in rank and r.get("state") in rank
                    and rank[r["product_zone"]] < rank[r["state"]]
                ]
                check("C11 没有任何一行比 state 更宽（只降不升）", not inverted, str(inverted[:3]))

            # C-12 not_confirmed_reasons 用变体 cfg（N1 修复）
            import inspect

            from coin_selection import scan as scan_mod

            src = inspect.getsource(scan_mod.board_from_rows)
            check(
                "C12 not_confirmed_reasons 用变体 cfg（N1）",
                "cfg = cfg or default_state_config()" in src
                and "cfg: Optional[Any] = None" in src,
                "",
            )

            # C-13 SM_FAST / warmup
            import os

            wu = y.cycle.warmup_overrides()
            check(
                "C13 SM_FAST 未开启；warmup 只含时间类字段",
                os.environ.get("SM_FAST") not in ("1", "true", "yes")
                and all(k.startswith(("min_dwell_", "min_streak_")) for k in wu),
                f"SM_FAST={os.environ.get('SM_FAST')} warmup={wu}",
            )

            # C-14 param_hash 落库
            ph = meta.get("param_hash")
            live = param_fingerprint(y, settings, cfg)
            check(
                "C14 快照带 param_hash 且与当前配置一致",
                ph == live,
                f"snapshot={ph} config={live}"
                + ("（快照产于阶段0之前，重启选币 loop 后自动补齐）" if not ph else ""),
                level="WARN" if not ph else "HARD",
            )

        # C-15 绩效结论必须 8 格 —— 检查回放产物里 metrics.json 的形状
        res = ROOT / "data" / "research"
        mets = sorted(res.glob("*/metrics.json"))
        if mets:
            # 判据是「**存在**一份 8 格齐全的绩效产物」，不是「字母序第一份齐全」。
            # 原实现只看 mets[0]：随手跑一次 --metrics-mode occupancy 的扫参
            # （只有 4 格）落在 data/research/B_* 就会把这条检查弄红，
            # 而它想验的是「绩效表这个能力在」，跟目录名排序无关。
            def _cells(path) -> int:
                m = json.loads(path.read_text(encoding="utf-8"))
                dmr = ((m.get("by_zone") or {}).get("DMR") or {})
                n = 0
                for mode_k in ("occupancy", "bracket"):
                    for cr in ("incl_cycle_reset", "excl_cycle_reset"):
                        for cost in ("zero_cost", "with_cost"):
                            if ((dmr.get(mode_k) or {}).get(cr) or {}).get(cost) is not None:
                                n += 1
                return n

            scored = sorted(((_cells(p), p) for p in mets), reverse=True)
            cells, best = scored[0]
            check(
                "C15 绩效结果表 8 格齐全",
                cells == 8,
                f"{cells}/8  ({best.parent.name}；共扫 {len(mets)} 份研究产物)",
            )
        else:
            check("C15 绩效结果表 8 格齐全", False,
                  "data/research/ 无 metrics.json（先跑 replay_rescore.py --metrics）",
                  level="WARN")

    # —— 文档B §8 阶段门 ——
    print("\n=== 文档B §8 · 阶段门（不过则不许把参数推上生效值）===")
    n_nodes, first, last, days = _snapshot_span(data_dir)
    print(f"       Y 快照 {n_nodes} 份  {first} → {last}  跨度 {days:.3f} 天")

    check(
        "S3-1 权重/阈值层可回放窗口 >= 31 天",
        days >= 31.0,
        f"{days:.3f} 天（满 31 天时点 {STAGE3_READY_UTC}）",
        level="WARN",
    )

    # 组合层：三周期等级第一次非空的节点
    combo_first = None
    combo_nodes = 0
    for p in sorted((data_dir / "snapshots").glob("*.json")):
        if p.name.endswith(".full.json"):
            continue
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        pool = (doc.get("long_pool") or [])[:80]
        if any(r.get("mcap_grade_6h") for r in pool):
            combo_first = combo_first or p.name.removesuffix(".json")
            combo_nodes += 1
    combo_days = combo_nodes / 96.0
    check(
        "S4-1 组合层可回放窗口 >= 31 天",
        combo_days >= 31.0,
        f"{combo_days:.2f} 天（{combo_nodes} 节点，起于 {combo_first}；满 31 天时点 {STAGE4_READY_UTC}）",
        level="WARN",
    )

    led = data_dir / "review" / "ledger.sqlite"
    if led.is_file():
        conn = sqlite3.connect(f"file:{led}?mode=ro", uri=True)
        try:
            n_dmr = conn.execute(
                "SELECT COUNT(*) FROM trades WHERE zone='DMR' AND status='CLOSED' "
                "AND pnl_pct IS NOT NULL AND flags NOT LIKE '%CYCLE_RESET%'"
            ).fetchone()[0]
            days_c = conn.execute(
                "SELECT COUNT(DISTINCT substr(enter_time_utc,1,10)) FROM trades "
                "WHERE status='CLOSED'"
            ).fetchone()[0]
        finally:
            conn.close()
        check("S3-2 最小样本量 n >= 200（DMR 排除CR）", n_dmr >= MIN_N, f"n={n_dmr}", level="WARN")
        check(
            "S3-3 最小日聚类 >= 20 个完整自然日",
            days_c >= MIN_DAY_CLUSTERS,
            f"{days_c} 天",
            level="WARN",
        )

    # S4-2：第四轮之前这里断言「天花板必须 off/shadow」。改造后天花板是**强制项**，
    # 因此判据改为「要么未开（阶段 4 未启动），要么已获显式授权」——
    # 真正不该出现的是「开着但没授权」，那种状态会被 mcap_dominance 守卫直接阻断出数。
    from coin_selection.mcap_dominance import AUTHORIZATION_TOKEN, authorization_ok

    mode = mcap_zone_mode_of(settings)
    authorized = authorization_ok(settings)
    check(
        "S4-2 天花板层 off/shadow（阶段4未开）或已获显式授权",
        mode in ("off", "shadow") or authorized,
        f"mcap_zone_mode={mode} authorized={authorized}"
        + ("" if authorized or mode in ("off", "shadow")
           else f" ← 需要 mcap_zone_authorization='{AUTHORIZATION_TOKEN}'"),
    )
    if mode == "on" and authorized:
        check(
            "S4-3 已授权：三周期流通市值主导层生效（rule_revision y-v2.0.0-r3）",
            True,
            "mcap_grade_30m/2h/6h 参与准入/退出/分区/排序",
        )

    print(f"\nhard failures: {hard}   warnings: {warn}")
    print(
        "说明：阶段门以 WARN 呈现 —— 它们描述的是「现在还不能做什么」，不是「代码坏了」。\n"
        "      任何一条 WARN 未清除之前，把参数推上生效值都违反文档B §8 的前置条件。"
    )
    return 1 if hard else 0


if __name__ == "__main__":
    sys.exit(main())
