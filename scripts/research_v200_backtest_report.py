#!/usr/bin/env python3
"""【研究脚本 · 只读 · 用完可直接删除】v2.0.0 DMR 区回测验证与参数优化分析。

    删除本文件不影响任何生产代码：不被任何模块 import，不写任何目录。

事实源（全部是选币榜Y 自己的真实产出，不用主榜、不用 tape 重算）
--------------------------------------------------------------
    data/coin-selection-y/review/ledger.sqlite   真实成交
    data/coin-selection-y/snapshots/*.json       真实板面（三周期 A–F 等级、DMR 成员、价格）

量纲（关键）
------------
``pnl_pct`` 是**小数**不是百分数：``review_pnl.realized_pnl`` 里 ``pct = exit/enter - 1``。
本报告一律按「× 100 = 百分数」输出。
  胜率   = 盈利笔数 / 有效笔数（含平局，阈值 FLAT_EPS）
  盈亏比 = 平均盈利 / |平均亏损|
  盈利率 = Σ pnl_pct × 100%（即《复盘选币》UI 的「合计」）
  PF     = Σ盈利 / Σ|亏损|

用法
----
    PYTHONPATH=services/coin-selection/src python3 scripts/research_v200_backtest_report.py
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import statistics as st
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "coin-selection" / "src"))

from coin_selection import mcap_mapping as mm  # noqa: E402
from coin_selection.mcap_effective import ZONE_EN, ZONE_RANK  # noqa: E402
from coin_selection.review_pnl import FLAT_EPS  # noqa: E402

Y = ROOT / "data" / "coin-selection-y"
#: v2.0.0 参数下的两个真实调参段（param_hash）。
PH_LAYER_ON = "pf1_fcea251fa94122fe"   # 216 主导层生效
PH_WEIGHTS = "pf1_1b44aba95de8ffe8"    # v2.0.0 目标七权重，层关

TARGET = {"win": (0.50, 1.00), "payoff": (1.0, 10.0), "total": (1.0, 10.0)}


# --------------------------------------------------------------------------- 指标
def stats(pnl: list[float]) -> dict[str, Any]:
    n = len(pnl)
    if n == 0:
        return {"n": 0}
    w = [p for p in pnl if p > FLAT_EPS]
    l = [p for p in pnl if p < -FLAT_EPS]
    aw = sum(w) / len(w) if w else 0.0
    al = sum(l) / len(l) if l else 0.0
    gw, gl = sum(w), abs(sum(l))
    return {
        "n": n,
        "win": len(w) / n,
        "payoff": (aw / abs(al)) if al else float("nan"),
        "pf": (gw / gl) if gl else float("nan"),
        "total": sum(pnl),
        "avg": sum(pnl) / n,
        "nw": len(w),
        "nl": len(l),
        "nf": n - len(w) - len(l),
    }


HDR = f"{'子集':<34}{'n':>6}{'胜率':>9}{'盈亏比':>9}{'PF':>8}{'盈利率':>11}{'平均':>9}"


def hit(m: dict) -> str:
    """三项是否落进目标区间。"""
    if not m.get("n"):
        return ""
    f = []
    f.append("胜" if TARGET["win"][0] <= m["win"] <= TARGET["win"][1] else "·")
    f.append("赔" if TARGET["payoff"][0] <= m["payoff"] <= TARGET["payoff"][1] else "·")
    f.append("利" if TARGET["total"][0] <= m["total"] <= TARGET["total"][1] else "·")
    s = "".join(f)
    return "  ✅达标" if s == "胜赔利" else (f"  [{s}]" if s != "···" else "")


def show(lab: str, pnl: list[float]) -> Optional[dict]:
    m = stats(pnl)
    if not m["n"]:
        print(f"{lab:<34}{'—':>6}")
        return None
    print(
        f"{lab:<34}{m['n']:>6}{m['win'] * 100:>8.2f}%{m['payoff']:>9.3f}"
        f"{m['pf']:>8.3f}{m['total'] * 100:>10.2f}%{m['avg'] * 100:>8.3f}%{hit(m)}"
    )
    return m


# --------------------------------------------------------------------------- 数据
def load(zone: str = "DMR") -> tuple[list[dict], dict[str, int]]:
    """读真实账本，返回 (有效样本, 剔除原因计数)。"""
    c = sqlite3.connect("file:" + str(Y / "review" / "ledger.sqlite") + "?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    raw = [dict(r) for r in c.execute("SELECT * FROM trades WHERE zone=?", (zone,))]
    drop: dict[str, int] = Counter()
    out = []
    for r in raw:
        fl = r["flags"] or ""
        if r["status"] != "CLOSED":
            drop["未平仓（OPEN，尚在区内）"] += 1
            continue
        if r["pnl_pct"] is None:
            drop["缺进场或退出价（无法计算盈亏）"] += 1
            continue
        if "CYCLE_RESET" in fl:
            drop["24h 周期重置强平（非策略自身退出）"] += 1
            continue
        if "VANISHED" in fl:
            drop["合约中途消失（下架/改名）"] += 1
            continue
        if "TRUNCATED_ENTER" in fl:
            drop["进场早于账本起点（停留被低估）"] += 1
            continue
        if "GAP_BEFORE_EXIT" in fl:
            drop["退出前有节点缺口（退出价不可信）"] += 1
            continue
        r["pnl"] = float(r["pnl_pct"])
        out.append(r)
    return out, dict(drop), len(raw)


_snap_cache: dict[str, Optional[dict]] = {}


def snap(scan_id: str) -> Optional[dict]:
    if scan_id not in _snap_cache:
        f = Y / "snapshots" / f"{scan_id}.json"
        _snap_cache[scan_id] = json.loads(f.read_text(encoding="utf-8")) if f.is_file() else None
    return _snap_cache[scan_id]


def _board_px(row: dict, zone: str):
    """板面行的进场价，**复用生产口径**（review_replay.stay_px / print_px）。

    这里刻意不自己实现一遍取价规则：复盘对账的意义就是拿账本和板面用
    **同一条规则**核对，各写一份必然再次分叉。
    """
    from coin_selection.review_replay import stay_px  # noqa: PLC0415

    return stay_px(row, str(zone or ""))


def enrich(trades: list[dict], mapping) -> tuple[list[dict], int]:
    """用真实快照回填 216 归因（真实等级 → 冻结映射表查表）。"""
    ok, miss = [], 0
    for t in trades:
        d = snap(t["enter_scan_id"])
        if not d:
            miss += 1
            continue
        g = None
        for pool in ("long_pool", "short_pool"):
            for r in d.get(pool) or []:
                if r["symbol"] == t["symbol"] and r.get("direction") == t["direction"]:
                    g = (r.get("mcap_grade_30m"), r.get("mcap_grade_2h"), r.get("mcap_grade_6h"))
                    # —— 进场价必须按**分区**取，不能一律用 state_enter_price ——
                    #
                    # DMR 不是状态机的一个状态，而是 CONFIRMED 的派生精选：
                    # 它没有「入区价」，进场价是入选那一节点的成交价。
                    # 生产复盘的权威口径在 review_replay.stay_px()：
                    #   zone ∈ LISTED_STAY(WATCH/QUALIFIED/CONFIRMED) → state_enter_price
                    #   其余（DMR）                                    → last_price → ref_price
                    # 改前这里对 DMR 也读 state_enter_price，于是把「拿错字段」
                    # 报成了「账本价格不一致」，产生大量假告警（解读报告 §9）。
                    t["_board_price"], t["_board_price_src"] = _board_px(r, t["zone"])
                    t["_board_state"] = r.get("state")
                    t["_board_dmr"] = r.get("dmr_selected")
                    break
            if g:
                break
        if not g or not all(g):
            miss += 1
            continue
        row = mapping.row(*g)
        t["combo"] = "".join(g)
        t["z10"] = row["z10"] if t["direction"] == "up" else -row["z10"]
        t["k"] = row["resonance_k"]
        t["prio"] = row["priority"]
        t["ceiling"] = ZONE_EN[mapping.ceiling(*g, t["direction"])]
        ok.append(t)
    return ok, miss


# --------------------------------------------------------------------------- 一致性
def consistency(trades: list[dict]) -> None:
    print("\n" + "=" * 100)
    print("一、数据一致性验证 —— 《选币榜Y》板面  ↔  《复盘选币》账本")
    print("=" * 100)
    n = miss_snap = 0
    dmr_ok = dmr_bad = 0
    px_ok = px_bad = px_na = 0
    bad_px: list[tuple] = []
    for t in trades:
        d = snap(t["enter_scan_id"])
        if not d:
            miss_snap += 1
            continue
        n += 1
        if t.get("_board_dmr") is True:
            dmr_ok += 1
        else:
            dmr_bad += 1
        bp, ep = t.get("_board_price"), t.get("enter_price")
        if bp is None or ep is None:
            px_na += 1
        elif abs(float(bp) - float(ep)) <= max(1e-12, abs(float(ep)) * 1e-9):
            px_ok += 1
        else:
            px_bad += 1
            if len(bad_px) < 5:
                bad_px.append((t["symbol"], t["enter_scan_id"], bp, ep))
    print(f"\n  可核对样本                {n}（另有 {miss_snap} 笔快照已被 31 天保留策略裁剪）")
    print(f"  入选币种一致（板面 dmr_selected=true）  {dmr_ok}/{n}  不一致 {dmr_bad}")
    print(f"  入选价格逐笔相等（板面按分区取价 == 账本 enter_price）  {px_ok}/{n}"
          f"  不等 {px_bad}  一侧为空 {px_na}")
    print(f"    取价口径：WATCH/QUALIFIED/CONFIRMED → state_enter_price；"
          f"DMR → last_price → ref_price（review_replay.stay_px）")
    for s, sid, a, b in bad_px:
        print(f"    差异样本 {s} @{sid}: 板面={a} 账本={b}")
    # 时间字段自洽
    bad_t = [t for t in trades if t["exit_time_utc"] and t["enter_time_utc"]
             and t["exit_time_utc"] <= t["enter_time_utc"]]
    print(f"  时间自洽（退出时间 > 入选时间）        {len(trades) - len(bad_t)}/{len(trades)}"
          f"  违反 {len(bad_t)}")
    onnode = sum(1 for t in trades if (t["dwell_nodes"] or 0) >= 1)
    print(f"  停留落在 15 分钟栅格上                {onnode}/{len(trades)}")


# --------------------------------------------------------------------------- 主
def main() -> int:
    ap = argparse.ArgumentParser(description="v2.0.0 DMR 回测验证与优化分析（研究用，可删）")
    ap.add_argument("--zone", default="DMR")
    a = ap.parse_args()
    mapping = mm.load(strict=True)

    trades, drop, n_raw = load(a.zone)
    print("=" * 100)
    print(f"《选币榜Y》v2.0.0 · {a.zone} 区回测验证与参数优化分析")
    print("事实源：Y 自己的账本 + Y 自己的快照。pnl_pct 为小数，本报告统一 ×100 显示为百分数。")
    print("=" * 100)
    print(f"\n二、样本口径\n  账本 {a.zone} 区总记录        {n_raw}")
    for k, v in sorted(drop.items(), key=lambda kv: -kv[1]):
        print(f"    剔除 {k:<32} {v}")
    print(f"  有效样本                    {len(trades)}")

    trades, miss = enrich(trades, mapping)
    print(f"  其中可回填 216 归因          {len(trades)}（{miss} 笔快照缺失/等级不全）")

    consistency(trades)

    v2 = [t for t in trades if t["param_hash"] in (PH_LAYER_ON, PH_WEIGHTS)]
    legacy = [t for t in trades if t["param_hash"] is None]

    print("\n" + "=" * 100)
    print("三、核心指标（真实成交）")
    print("=" * 100)
    print(f"\n{HDR}")
    show("全部有效样本", [t["pnl"] for t in trades])
    show("  └ v2.0.0 参数段（B+C）", [t["pnl"] for t in v2])
    show("  └ 主导层生效段（C）", [t["pnl"] for t in trades if t["param_hash"] == PH_LAYER_ON])
    show("  └ 无指纹历史段（A）", [t["pnl"] for t in legacy])

    print(f"\n  —— 上涨 / 下跌候选池分别统计（全部有效样本）——\n{HDR}")
    for dirn, lab in (("up", "上涨候选池"), ("down", "下跌候选池")):
        show(lab, [t["pnl"] for t in trades if t["direction"] == dirn])
    print(f"\n  —— 上涨 / 下跌候选池（仅 v2.0.0 段）——\n{HDR}")
    for dirn, lab in (("up", "上涨候选池 v2.0.0"), ("down", "下跌候选池 v2.0.0")):
        show(lab, [t["pnl"] for t in v2 if t["direction"] == dirn])

    print("\n" + "=" * 100)
    print("四、关键驱动因子分析（全部有效样本，n 更大更可信）")
    print("=" * 100)

    print(f"\n### 4.1 停留时长（退出条件的直接体现）\n{HDR}")
    BK = [(1, 1, "1 节点 = 15 分钟"), (2, 4, "2–4 节点 ≤ 1 小时"),
          (5, 8, "5–8 节点 ≤ 2 小时"), (9, 24, "9–24 节点 ≤ 6 小时"), (25, 10**9, "≥25 节点")]
    for lo, hi, lab in BK:
        show(f"  停留 {lab}", [t["pnl"] for t in trades if lo <= (t["dwell_nodes"] or 0) <= hi])
    print("  【注意】停留时长只有**退出后**才知道，不能作为准入过滤器（会构成前视偏差）。")
    print("        它指向的可行动作是**改退出条件**，让仓位不要在 15–60 分钟内被churn 掉。")

    print(f"\n### 4.2 216 天花板（准入可用：进场时即可知）\n{HDR}")
    for z in ("DMR", "CONFIRMED", "QUALIFIED", "WATCH", "ELIMINATED"):
        sub = [t["pnl"] for t in trades if t["ceiling"] == z]
        if sub:
            show(f"  天花板 = {z}", sub)
    for lab, f in (("  天花板 ∈ {DMR}", lambda t: t["ceiling"] == "DMR"),
                   ("  天花板 ∈ {DMR,确定}", lambda t: ZONE_RANK[t["ceiling"]] <= 1),
                   ("  天花板 ∈ {DMR,确定,符合}", lambda t: ZONE_RANK[t["ceiling"]] <= 2)):
        show(lab, [t["pnl"] for t in trades if f(t)])

    print(f"\n### 4.3 三周期共振 K（准入可用）\n{HDR}")
    for k in (100, 70, 40, 10):
        sub = [t["pnl"] for t in trades if t["k"] == k]
        if sub:
            show(f"  K = {k}", sub)

    print(f"\n### 4.4 优先级 |Z10|（准入可用）\n{HDR}")
    for p in (5, 4, 3, 2, 1):
        sub = [t["pnl"] for t in trades if t["prio"] == p]
        if sub:
            show(f"  优先级 = {p}", sub)

    print(f"\n### 4.5 进场时的 Score / SS / 动能分位（准入可用）\n{HDR}")
    for fld, lab in (("score", "Score"), ("direction_confidence", "方向置信")):
        vals = sorted([t for t in trades if t.get(fld) is not None], key=lambda t: t[fld])
        if len(vals) < 50:
            continue
        n = len(vals)
        for b in range(5):
            seg = vals[n * b // 5:n * (b + 1) // 5]
            show(f"  {lab} Q{b + 1} [{seg[0][fld]:.1f},{seg[-1][fld]:.1f}]",
                 [t["pnl"] for t in seg])

    print("\n" + "=" * 100)
    print("五、准入过滤器组合搜索（只用进场时可知的字段，无前视）")
    print("=" * 100)
    base = stats([t["pnl"] for t in trades])
    cands: list[tuple] = []
    CEIL = [("不限", lambda t: True), ("≤DMR", lambda t: t["ceiling"] == "DMR"),
            ("≤确定", lambda t: ZONE_RANK[t["ceiling"]] <= 1),
            ("≤符合", lambda t: ZONE_RANK[t["ceiling"]] <= 2)]
    KS = [("不限", lambda t: True), ("K≥70", lambda t: t["k"] >= 70), ("K=100", lambda t: t["k"] == 100)]
    PR = [("不限", lambda t: True), ("P≥4", lambda t: t["prio"] >= 4), ("P=5", lambda t: t["prio"] == 5)]
    DR = [("双向", lambda t: True), ("仅上涨", lambda t: t["direction"] == "up"),
          ("仅下跌", lambda t: t["direction"] == "down")]
    for cl, cf in CEIL:
        for kl, kf in KS:
            for pl, pf_ in PR:
                for dl, df in DR:
                    seg = [t["pnl"] for t in trades if cf(t) and kf(t) and pf_(t) and df(t)]
                    if len(seg) < 60:
                        continue
                    m = stats(seg)
                    cands.append((m, f"天花板{cl} · {kl} · {pl} · {dl}"))
    print(f"\n  基线（不过滤）  n={base['n']}  胜率 {base['win']*100:.2f}%  "
          f"盈亏比 {base['payoff']:.3f}  盈利率 {base['total']*100:+.2f}%")
    for title, key in (("按盈亏比排序 Top6", lambda x: x[0]["payoff"]),
                       ("按盈利率排序 Top6", lambda x: x[0]["total"]),
                       ("按胜率排序 Top6", lambda x: x[0]["win"])):
        print(f"\n  === {title} ===\n{HDR}")
        for m, lab in sorted(cands, key=key, reverse=True)[:6]:
            print(f"{lab:<34}{m['n']:>6}{m['win']*100:>8.2f}%{m['payoff']:>9.3f}"
                  f"{m['pf']:>8.3f}{m['total']*100:>10.2f}%{m['avg']*100:>8.3f}%{hit(m)}")

    print("\n" + "=" * 100)
    print("六、目标区间达成情况（胜率 50–100% / 盈亏比 1–10 / 盈利率 100–1000%）")
    print("=" * 100)
    best = [(m, lab) for m, lab in cands
            if TARGET["win"][0] <= m["win"] and TARGET["payoff"][0] <= m["payoff"] <= TARGET["payoff"][1]
            and TARGET["total"][0] <= m["total"]]
    if best:
        print(f"\n  三项同时达标的准入组合：{len(best)} 个\n{HDR}")
        for m, lab in sorted(best, key=lambda x: -x[0]["total"])[:10]:
            print(f"{lab:<34}{m['n']:>6}{m['win']*100:>8.2f}%{m['payoff']:>9.3f}"
                  f"{m['pf']:>8.3f}{m['total']*100:>10.2f}%{m['avg']*100:>8.3f}%  ✅")
    else:
        print("\n  **没有任何准入组合能让三项同时落进目标区间。**")
        near = sorted(cands, key=lambda x: -(x[0]["total"]))[:3]
        print(f"  最接近的三个（按盈利率）：\n{HDR}")
        for m, lab in near:
            print(f"{lab:<34}{m['n']:>6}{m['win']*100:>8.2f}%{m['payoff']:>9.3f}"
                  f"{m['pf']:>8.3f}{m['total']*100:>10.2f}%{m['avg']*100:>8.3f}%{hit(m)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
