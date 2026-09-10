#!/usr/bin/env python3
"""【研究脚本 · 只读 · 用完可直接删除】按专业文档的规则设定回测 216 分区。

    删除本文件不影响任何生产代码：不被任何模块 import，不写任何目录。

规则来源（严格照抄，不自创）
--------------------------
doc/ChatGpt_SOL5.6_PRO_选币榜Y流通市值状态组合分区与选入规则体系设计.md

  §二 趋势分类（多数原则）
      多头信号 = {A,B,C}，空头信号 = {D,E,F}
      多头数>=2 且 空头数==0            → 上升组合
      空头数>=2 且 多头数==0            → 下降组合
      否则：6h 多头 且 多头数>空头数     → 上升组合
            6h 空头 且 空头数>多头数     → 下降组合
            其余                        → 中性

  §三/§六 分区准入（自严至宽，唯一归属）
      DMR   三周期完全同向且强度最大：AAA（上涨）/ FFF（下跌）
      确定   至少两周期绝对同向，第三周期不违背主趋势：
             两个 A + 第三个 ∈{B,C}  /  两个 F + 第三个 ∈{D,E}
             「出现与主趋势相反的周期则不纳入」
      符合   趋势方向一致但强度较弱：三周期同族（全 {A,B,C} 或全 {D,E,F}）
      观察   信号冲突或趋势不明确（多空并存）
      淘汰   矛盾最严重：A 与 F 同现

  §九 可证伪断言（本脚本要验证的就是它）
      「DMR 区和确定区信号的胜率和收益率，应明显高于符合区和观察区」

事实源：**只用选币榜Y 自己的真实快照** data/coin-selection-y/snapshots/*.json
成交额硬门（维度一）沿用现网 liquidity_hard_pass，不放宽。

用法
----
    PYTHONPATH=services/coin-selection/src python3 scripts/research_doc_rules_backtest.py
"""

from __future__ import annotations

import argparse
import json
import statistics as st
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "coin-selection" / "src"))

Y = ROOT / "data" / "coin-selection-y"
BULL = frozenset("ABC")
BEAR = frozenset("DEF")


# --------------------------------------------------------------------------- 文档规则
def classify(combo: str) -> str:
    """§二 趋势分类（多数原则）。combo = 30m|2h|6h 三字母。"""
    g30, g2, g6 = combo[0], combo[1], combo[2]
    nb = sum(1 for g in (g30, g2, g6) if g in BULL)
    ns = 3 - nb
    if nb >= 2 and ns == 0:
        return "上升"
    if ns >= 2 and nb == 0:
        return "下降"
    if g6 in BULL and nb > ns:
        return "上升"
    if g6 in BEAR and ns > nb:
        return "下降"
    return "中性"


def doc_zone(combo: str) -> str:
    """§三/§六 分区准入，自严至宽，唯一归属。"""
    g = (combo[0], combo[1], combo[2])
    has_a, has_f = "A" in g, "F" in g
    # 淘汰：矛盾最严重 —— A 与 F 同现
    if has_a and has_f:
        return "淘汰"
    # DMR：三周期完全同向且强度最大
    if combo == "AAA" or combo == "FFF":
        return "DMR"
    nA, nF = g.count("A"), g.count("F")
    others = [x for x in g if x not in ("A",)] if nA == 2 else None
    # 确定：两个 A + 第三个∈{B,C}；或两个 F + 第三个∈{D,E}
    if nA == 2 and all(x in BULL for x in g):
        return "确定"
    if nF == 2 and all(x in BEAR for x in g):
        return "确定"
    # 符合：三周期同族
    if all(x in BULL for x in g) or all(x in BEAR for x in g):
        return "符合"
    # 观察：多空并存但无 A/F 极端冲突
    return "观察"


# --------------------------------------------------------------------------- 数据
def load() -> tuple[list[str], dict[str, dict]]:
    files = sorted((Y / "snapshots").glob("*.json"))
    sids: list[str] = []
    data: dict[str, dict] = {}
    for f in files:
        d = json.loads(f.read_text(encoding="utf-8"))
        rec: dict[tuple, dict] = {}
        for pool in ("long_pool", "short_pool"):
            for r in d.get(pool) or []:
                dirn = r.get("direction")
                g = (r.get("mcap_grade_30m"), r.get("mcap_grade_2h"), r.get("mcap_grade_6h"))
                if not all(g):
                    continue
                rec[(r["symbol"], dirn)] = {
                    "combo": "".join(g),
                    "px": r.get("last_price"),
                    "hard": r.get("liquidity_grade") is not None,
                    "mom": r.get("momentum_score"),
                    "ss": r.get("staircase_score"),
                    "score": r.get("score_up") if dirn == "up" else r.get("score_down"),
                    "dmr": bool(r.get("dmr_selected")),
                    "state": r.get("state"),
                }
        sids.append(f.stem)
        data[f.stem] = rec
    return sids, data


def fwd(data, sids, i, key, k) -> Optional[float]:
    if i + k >= len(sids):
        return None
    a, b = data[sids[i]].get(key), data[sids[i + k]].get(key)
    if not a or not b or not a["px"] or not b["px"]:
        return None
    r = b["px"] / a["px"] - 1.0
    return r if key[1] == "up" else -r


def m(v: list[float]) -> dict:
    if not v:
        return {"n": 0}
    w = [x for x in v if x > 0]
    l = [x for x in v if x < 0]
    aw = sum(w) / len(w) if w else 0.0
    al = sum(l) / len(l) if l else 0.0
    return {
        "n": len(v), "win": len(w) / len(v),
        "payoff": (aw / abs(al)) if al else float("nan"),
        "pf": (sum(w) / abs(sum(l))) if l else float("nan"),
        "mean": st.mean(v), "total": sum(v),
    }


HDR = f"{'分区 / 池':<22}{'n':>8}{'胜率':>9}{'盈亏比':>9}{'PF':>8}{'均值':>10}{'合计':>12}"


def row(lab: str, v: list[float]) -> Optional[dict]:
    s = m(v)
    if not s["n"]:
        print(f"{lab:<22}{'—':>8}")
        return None
    print(f"{lab:<22}{s['n']:>8}{s['win']*100:>8.2f}%{s['payoff']:>9.3f}{s['pf']:>8.3f}"
          f"{s['mean']*100:>9.3f}%{s['total']*100:>11.1f}%")
    return s


def main() -> int:
    ap = argparse.ArgumentParser(description="按专业文档规则回测 216 分区（研究用，可删）")
    ap.add_argument("--stride", type=int, default=2)
    ap.add_argument("--horizon", type=int, default=24, help="前瞻节点数（24=6小时）")
    ap.add_argument("--hard-gate", action="store_true", default=True)
    a = ap.parse_args()

    sids, data = load()
    idx = list(range(0, len(sids), a.stride))
    print("=" * 100)
    print("按《选币榜Y 流通市值状态组合分区与选入规则体系设计》(PRO 版) 的规则回测")
    print(f"事实源：Y 自己的真实快照 {len(sids)} 节点  {sids[0]} → {sids[-1]}")
    print(f"前瞻视界 {a.horizon} 节点 = {a.horizon*15/60:.1f} 小时；成交额硬门沿用现网 Gate1")
    print("=" * 100)

    # 分区枚举核对
    from itertools import product
    allc = ["".join(p) for p in product("ABCDEF", repeat=3)]
    zc = Counter(doc_zone(c) for c in allc)
    cc = Counter(classify(c) for c in allc)
    print(f"\n  216 组合按文档规则的分区分布: {dict(zc)}  合计 {sum(zc.values())}")
    print(f"  216 组合按文档规则的趋势分类: {dict(cc)}  合计 {sum(cc.values())}")

    # 收集前瞻收益
    buckets: dict[str, list[float]] = defaultdict(list)
    for i in idx:
        for key, r in data[sids[i]].items():
            if a.hard_gate and not r["hard"]:
                continue
            v = fwd(data, sids, i, key, a.horizon)
            if v is None:
                continue
            z, cl = doc_zone(r["combo"]), classify(r["combo"])
            dirn = key[1]
            # 文档 §五：上升组合 → 上涨池；下降组合 → 下跌池；中性不入池
            inpool = (cl == "上升" and dirn == "up") or (cl == "下降" and dirn == "down")
            buckets["ALL"].append(v)
            if inpool:
                buckets[f"{z}"].append(v)
                buckets[f"{z}|{dirn}"].append(v)

    print("\n" + "=" * 100)
    print("一、文档 §九 的可证伪断言：DMR / 确定 应「明显高于」符合 / 观察")
    print("=" * 100)
    print(f"\n{HDR}")
    row("全体（对照基线）", buckets["ALL"])
    print()
    order = ["DMR", "确定", "符合", "观察", "淘汰"]
    got = {}
    for z in order:
        s = row(f"  {z} 区（入池）", buckets.get(z, []))
        if s:
            got[z] = s
    print(f"\n  —— 分方向 ——\n{HDR}")
    for z in order:
        for d in ("up", "down"):
            k = f"{z}|{d}"
            if buckets.get(k):
                row(f"  {z} · {'上涨池' if d=='up' else '下跌池'}", buckets[k])

    print("\n  === 断言检验 ===")
    ok = True
    if "DMR" in got and "符合" in got:
        for hi in ("DMR", "确定"):
            for lo in ("符合", "观察"):
                if hi in got and lo in got:
                    dw = (got[hi]["win"] - got[lo]["win"]) * 100
                    dm = (got[hi]["mean"] - got[lo]["mean"]) * 100
                    good = dw > 0 and dm > 0
                    ok &= good
                    print(f"    {hi} vs {lo}: 胜率差 {dw:+6.2f}pp  均值差 {dm:+7.3f}pp"
                          f"   {'✅符合断言' if good else '❌违反断言'}")
    print(f"\n  → 文档断言{'成立' if ok else '**不成立**'}")

    print("\n" + "=" * 100)
    print("二、多视界稳健性（同一分区在不同持有期）")
    print("=" * 100)
    print(f"\n{'分区':<10}" + "".join(f"{f'{h}节点/{h*15//60}h':>18}" for h in (4, 8, 24, 96)))
    for z in order:
        cells = []
        for h in (4, 8, 24, 96):
            vs = []
            for i in idx:
                for key, r in data[sids[i]].items():
                    if a.hard_gate and not r["hard"]:
                        continue
                    if doc_zone(r["combo"]) != z:
                        continue
                    cl = classify(r["combo"])
                    if not ((cl == "上升" and key[1] == "up") or (cl == "下降" and key[1] == "down")):
                        continue
                    v = fwd(data, sids, i, key, h)
                    if v is not None:
                        vs.append(v)
            s = m(vs)
            cells.append(f"{s['mean']*100:+7.3f}%/{s['win']*100:4.1f}%" if s["n"] else "     —      ")
        print(f"{z:<10}" + "".join(f"{c:>18}" for c in cells))
    print("\n  单元格 = 均值 / 胜率")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
