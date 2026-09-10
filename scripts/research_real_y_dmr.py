#!/usr/bin/env python3
"""【研究脚本 · 只读 · 用完可直接删除】选币榜Y **真实数据**的 DMR 区回测。

    删除本文件不影响任何生产代码：不被任何模块 import，不写任何目录。

与 scripts/research_supply_signals.py 的**根本区别**
-----------------------------------------------
那个脚本用的是 ``data/research/tape.pkl`` —— 由**主榜** ``data/coin-selection/snapshots``
压缩而来，DMR 成员是按 v2.0.0 参数**重算**的，不是选币榜Y 真实发布过的。

本脚本只读两样东西，全部是选币榜Y 自己的真实产出：

    data/coin-selection-y/review/ledger.sqlite   真实成交（进出场价、PnL、旗标）
    data/coin-selection-y/snapshots/*.json       真实板面（三周期 A–F 等级）

三周期 A–F 等级是**当时真实取到并落库的**（每节点 524 个合约 × 3 周期，
判级引擎 mcap_timeframe.grade_from_mas，unclassified=0）。216 组合号 / Z10 /
共振 K / 方向 ceiling 由**冻结映射表** mcap-216-v2.0.0-r1 查表得到 ——
表是确定性的、mapping_hash 已校验，所以由真实等级查出的归因同样是真实归因，
不是模拟。

样本分段（按 param_hash，即「这套标准的哪一次调参」）
--------------------------------------------------
    A  param_hash IS NULL          阶段 0 之前，无参数指纹的历史
    B  pf1_1b44aba95de8ffe8        v2.0.0 目标七权重，216 主导层**关**
    C  pf1_fcea251fa94122fe        216 主导层**生效**（2026-09-02 切换后）

指标口径与 replay_rescore.occupancy_metrics 一致：
    胜率   = 盈利笔数 / 总笔数（含平局，阈值 FLAT_EPS）
    盈亏比 = 平均盈利 / |平均亏损|
    盈利率 = Σ pnl_pct（《复盘选币》UI 的「合计」）
    PF     = Σ盈利 / Σ|亏损|
排除 CYCLE_RESET 强平、未平仓、缺价。

用法
----
    PYTHONPATH=services/coin-selection/src python3 scripts/research_real_y_dmr.py
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "coin-selection" / "src"))

from coin_selection import mcap_mapping as mm  # noqa: E402
from coin_selection.mcap_effective import ZONE_EN, ZONE_RANK  # noqa: E402
from coin_selection.review_pnl import FLAT_EPS  # noqa: E402

Y_DIR = ROOT / "data" / "coin-selection-y"
SEGMENTS = [
    ("C", "pf1_fcea251fa94122fe", "216 主导层生效"),
    ("B", "pf1_1b44aba95de8ffe8", "v2.0.0 权重·层关"),
    ("A", None, "无指纹历史"),
]


def stats(pnl: list[float]) -> dict[str, Any]:
    n = len(pnl)
    if n == 0:
        return {"n": 0}
    wins = [p for p in pnl if p > FLAT_EPS]
    losses = [p for p in pnl if p < -FLAT_EPS]
    aw = sum(wins) / len(wins) if wins else 0.0
    al = sum(losses) / len(losses) if losses else 0.0
    gw, gl = sum(wins), abs(sum(losses))
    return {
        "n": n,
        "win_rate": len(wins) / n,
        "payoff": (aw / abs(al)) if al else float("nan"),
        "pf": (gw / gl) if gl else float("nan"),
        "total": sum(pnl),
        "avg": sum(pnl) / n,
        "n_win": len(wins),
        "n_loss": len(losses),
    }


HDR = f"{'子集':<30}{'n':>5}{'胜率':>9}{'盈亏比':>9}{'PF':>8}{'盈利率':>10}"


def show(lab: str, pnl: list[float], base: Optional[dict] = None) -> Optional[dict]:
    m = stats(pnl)
    if not m["n"]:
        print(f"{lab:<30}{'—':>5}")
        return None
    star = ""
    if base and base.get("n"):
        # 本轮目标函数：只看盈亏比与盈利率
        if m["payoff"] > base["payoff"] and m["total"] > base["total"]:
            star = "  ★盈亏比+盈利率双优"
    print(
        f"{lab:<30}{m['n']:>5}{m['win_rate'] * 100:>8.2f}%"
        f"{m['payoff']:>9.3f}{m['pf']:>8.3f}{m['total']:>9.2f}%{star}"
    )
    return m


def load_real_trades(zone: str) -> list[dict[str, Any]]:
    """只读选币榜Y 自己的账本。"""
    p = Y_DIR / "review" / "ledger.sqlite"
    c = sqlite3.connect("file:" + str(p) + "?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    rows = [
        dict(r)
        for r in c.execute(
            "SELECT symbol, direction, enter_scan_id, exit_scan_id, pnl_pct, flags, "
            "param_hash, enter_price_source, exit_price_source, dwell_nodes "
            "FROM trades WHERE zone=? AND status='CLOSED' AND pnl_pct IS NOT NULL",
            (zone,),
        )
    ]
    keep = [r for r in rows if "CYCLE_RESET" not in (r["flags"] or "")]
    print(
        f"[真实账本] {p}\n"
        f"           {zone} 已平仓且有价 {len(rows)} 笔，排除 CYCLE_RESET 后 {len(keep)} 笔",
        file=sys.stderr,
    )
    return keep


def grades_at(scan_id: str, cache: dict[str, dict[str, tuple]]) -> dict[str, tuple]:
    """读该节点的**真实 Y 快照**，取每个合约的三周期 A–F 等级。"""
    if scan_id in cache:
        return cache[scan_id]
    f = Y_DIR / "snapshots" / f"{scan_id}.json"
    out: dict[str, tuple] = {}
    if f.is_file():
        d = json.loads(f.read_text(encoding="utf-8"))
        for pool in ("long_pool", "short_pool"):
            for r in d.get(pool) or []:
                g = (
                    r.get("mcap_grade_30m"),
                    r.get("mcap_grade_2h"),
                    r.get("mcap_grade_6h"),
                )
                if all(g):
                    out[r["symbol"]] = g
    cache[scan_id] = out
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="选币榜Y 真实 DMR 数据回测（研究用，可删）")
    ap.add_argument("--zone", default="DMR")
    a = ap.parse_args()

    mapping = mm.load(strict=True)
    trades = load_real_trades(a.zone)
    cache: dict[str, dict[str, tuple]] = {}

    # 用真实快照里的真实等级，查冻结映射表拿 216 归因
    enriched: list[dict[str, Any]] = []
    no_grade = 0
    for t in trades:
        g = grades_at(t["enter_scan_id"], cache).get(t["symbol"])
        if not g:
            no_grade += 1
            continue
        row = mapping.row(*g)
        ceil_cn = mapping.ceiling(*g, t["direction"])
        z10 = row["z10"] if t["direction"] == "up" else -row["z10"]
        enriched.append(
            {
                **t,
                "combo": "".join(g),
                "z10": z10,
                "k": row["resonance_k"],
                "priority": row["priority"],
                "ceiling": ZONE_EN[ceil_cn],
                "pnl": float(t["pnl_pct"]),
            }
        )
    print(
        f"[真实快照] 成功回填 216 归因 {len(enriched)} 笔；"
        f"三周期等级缺失/快照已裁剪 {no_grade} 笔\n",
        file=sys.stderr,
    )

    print("=" * 86)
    print("选币榜Y · 真实数据 DMR 回测（事实源：Y 自己的账本 + Y 自己的快照）")
    print("216 归因 = 真实三周期等级 → 冻结映射表 mcap-216-v2.0.0-r1 查表（确定性，非模拟）")
    print("=" * 86)

    print("\n### 一、按参数分段（真实成交）")
    print(HDR)
    seg_rows: dict[str, list[dict]] = {}
    for tag, ph, lab in SEGMENTS:
        sub = [
            r
            for r in enriched
            if (r["param_hash"] is None if ph is None else r["param_hash"] == ph)
        ]
        seg_rows[tag] = sub
        show(f"{tag}  {lab}", [r["pnl"] for r in sub])

    # 主导层生效段 + 权重段合并 = v2.0.0 参数下的全部真实成交
    v2 = seg_rows["B"] + seg_rows["C"]
    print()
    base = show("B+C  v2.0.0 参数全部真实成交", [r["pnl"] for r in v2])

    if not base or base["n"] < 20:
        print("\n[!] v2.0.0 真实样本不足，后续分段仅供观察，不构成结论。")

    print("\n### 二、真实数据上，216 天花板的区分度（样本 = B+C）")
    print(HDR)
    for z in ("DMR", "CONFIRMED", "QUALIFIED", "WATCH", "ELIMINATED"):
        sub = [r["pnl"] for r in v2 if r["ceiling"] == z]
        if sub:
            show(f"  天花板 = {z}", sub, base)
    print()
    for lab, f in (
        ("  天花板 ∈ {DMR}", lambda r: r["ceiling"] == "DMR"),
        ("  天花板 ∈ {DMR,确定}", lambda r: ZONE_RANK[r["ceiling"]] <= 1),
        ("  天花板 ∈ {DMR,确定,符合}", lambda r: ZONE_RANK[r["ceiling"]] <= 2),
    ):
        show(lab, [r["pnl"] for r in v2 if f(r)], base)

    print("\n### 三、真实数据上，共振 K 与优先级的区分度（样本 = B+C）")
    print(HDR)
    for k in (100, 70, 40, 10):
        sub = [r["pnl"] for r in v2 if r["k"] == k]
        if sub:
            show(f"  共振 K = {k}", sub, base)
    print()
    for p in (5, 4, 3, 2, 1):
        sub = [r["pnl"] for r in v2 if r["priority"] == p]
        if sub:
            show(f"  优先级 = {p}（|Z10| 越大越高）", sub, base)

    print("\n### 四、主导层生效段（C）逐笔明细 —— 真实成交，全部列出")
    c = sorted(seg_rows["C"], key=lambda r: r["enter_scan_id"])
    if c:
        print(
            f"{'合约':<14}{'向':<5}{'进场节点':<15}{'组合':<6}{'Z':>6}{'K':>5}"
            f"{'天花板':<11}{'停留':>5}{'PnL':>9}"
        )
        for r in c:
            print(
                f"{r['symbol']:<14}{r['direction']:<5}{r['enter_scan_id']:<15}"
                f"{r['combo']:<6}{r['z10'] / 10:>+6.1f}{r['k']:>5}"
                f"{r['ceiling']:<11}{r['dwell_nodes'] or 0:>5}{r['pnl']:>+8.3f}%"
            )
        m = stats([r["pnl"] for r in c])
        print(
            f"\n  合计 {m['n']} 笔：盈 {m['n_win']} / 亏 {m['n_loss']}，"
            f"胜率 {m['win_rate'] * 100:.2f}%，盈亏比 {m['payoff']:.3f}，"
            f"盈利率 {m['total']:+.3f}%，PF {m['pf']:.3f}"
        )
    else:
        print("  （暂无已平仓成交）")

    print("\n注：n 很小的行只作观察，不构成统计结论。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
