#!/usr/bin/env python3
"""【研究脚本 · 只读 · 用完可直接删除】216 组合的同期关系 vs 前瞻预测力，及准入门拦截分析。

    删除本文件不影响任何生产代码：不被任何模块 import，不写任何目录。

事实源：**只用选币榜Y 自己的真实快照** data/coin-selection-y/snapshots/*.json

回答三个问题
------------
Q1 同期：涨幅最大的币，此刻是不是 AAA？跌幅最大的是不是 FFF？
   —— 这几乎是定义使然（供应量恒定时，市值均线排列 ≡ 价格均线排列），
      但必须量化到底有多强。

Q2 前瞻：此刻是 AAA 的币，**接下来** N 个节点会不会涨？
   —— 这才是「能不能用来选币」的判据。同期强 ≠ 前瞻有效。

Q3 拦截：AAA / FFF 的币在进 DMR 的路上，被哪一道门挡住了？
   —— 当前顺序是 state(CONFIRMED) → 216天花板 → DQ → dmr_zone_ok(Score/SS/动能/一致性)。
      动能是硬门且排在 216 之前，一个完美 AAA 只要 M<65 就出局。

用法
----
    PYTHONPATH=services/coin-selection/src python3 scripts/research_combo_edge.py
    PYTHONPATH=... python3 scripts/research_combo_edge.py --stride 4   # 抽样加速
"""

from __future__ import annotations

import argparse
import json
import math
import statistics as st
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "coin-selection" / "src"))

from coin_selection import mcap_mapping as mm  # noqa: E402
from coin_selection.mcap_effective import ZONE_EN  # noqa: E402

Y = ROOT / "data" / "coin-selection-y"

#: 现网 DMR 门槛（state_machine.StateConfig 默认值，Y 的 state_config overrides 为空）
G = {"dmr_score": 70.0, "dmr_ss": 55.0, "dmr_mom": 65.0, "dmr_cons": 0.67, "dmr_dq": 70.0}
#: 状态机进确认区的门槛
C = {"score": 66.0, "ss": 50.0, "mom": 60.0, "cons": 0.67, "dq": 60.0}


def load(stride: int) -> tuple[list[str], dict[str, dict]]:
    """→ (节点序列, {scan_id: {(sym,dir): rec}})。只读真实 Y 快照。"""
    files = sorted((Y / "snapshots").glob("*.json"))
    sids: list[str] = []
    data: dict[str, dict] = {}
    for i, f in enumerate(files):
        d = json.loads(f.read_text(encoding="utf-8"))
        sid = f.stem
        rec: dict[tuple, dict] = {}
        for pool in ("long_pool", "short_pool"):
            for r in d.get(pool) or []:
                dirn = r.get("direction")
                g = (r.get("mcap_grade_30m"), r.get("mcap_grade_2h"), r.get("mcap_grade_6h"))
                rec[(r["symbol"], dirn)] = {
                    "combo": "".join(g) if all(g) else None,
                    "state": r.get("state"),
                    "score": r.get("score_up") if dirn == "up" else r.get("score_down"),
                    "ss": r.get("staircase_score"),
                    "mom": r.get("momentum_score"),
                    "cons": r.get("consistency_score"),
                    "dq": r.get("data_confidence"),
                    "dmr": bool(r.get("dmr_selected")),
                    "px": r.get("last_price"),
                    "ret24": r.get("ret_24h"),
                    "supply_missing": r.get("supply_missing"),
                    "dm": r.get("data_mode"),
                }
        sids.append(sid)
        data[sid] = rec
    return sids, data


def fwd(data: dict, sids: list[str], i: int, key: tuple, k: int) -> Optional[float]:
    """(sym,dir) 在第 i 个节点之后 k 个节点的**方向收益**（up: 涨为正；down: 跌为正）。"""
    if i + k >= len(sids):
        return None
    a = data[sids[i]].get(key)
    b = data[sids[i + k]].get(key)
    if not a or not b or not a["px"] or not b["px"]:
        return None
    r = b["px"] / a["px"] - 1.0
    return r if key[1] == "up" else -r


def desc(v: list[float]) -> str:
    if not v:
        return "—"
    v2 = sorted(v)
    return (f"n={len(v):<6} 均值={st.mean(v)*100:>+7.3f}%  中位={v2[len(v2)//2]*100:>+7.3f}%  "
            f"胜率={sum(1 for x in v if x>0)/len(v)*100:>5.1f}%")


def main() -> int:
    ap = argparse.ArgumentParser(description="216 组合同期/前瞻分析（研究用，可删）")
    ap.add_argument("--stride", type=int, default=1, help="节点抽样步长（1=全量）")
    ap.add_argument("--horizons", default="4,8,24,96")
    a = ap.parse_args()
    H = [int(x) for x in a.horizons.split(",")]

    mapping = mm.load(strict=True)
    sids, data = load(a.stride)
    print("=" * 104)
    print("《选币榜Y》216 组合：同期关系 vs 前瞻预测力 vs 准入门拦截")
    print(f"事实源：Y 自己的真实快照 {len(sids)} 个节点  {sids[0]} → {sids[-1]}")
    print("=" * 104)

    idx = list(range(0, len(sids), a.stride))

    # ---------------- Q1 同期 ----------------
    print("\n" + "=" * 104)
    print("Q1  同期关系：涨幅最大的币，此刻是不是 AAA？（用板面已有的 ret_24h）")
    print("=" * 104)
    by_combo_ret: dict[str, list[float]] = defaultdict(list)
    for i in idx:
        for key, r in data[sids[i]].items():
            if key[1] != "up" or not r["combo"] or r["ret24"] is None:
                continue
            by_combo_ret[r["combo"]].append(float(r["ret24"]))
    rank = sorted(((st.mean(v), k, len(v)) for k, v in by_combo_ret.items() if len(v) >= 200),
                  reverse=True)
    print(f"\n  按「过去 24h 涨幅均值」排序的组合（上涨侧，样本≥200）")
    print(f"  {'排名':<5}{'组合':<7}{'n':>7}{'过去24h均值':>13}")
    for r_, (mu, cb, n) in enumerate(rank[:6], 1):
        print(f"  {r_:<5}{cb:<7}{n:>7}{mu*100:>12.3f}%")
    print("  ...")
    for r_, (mu, cb, n) in enumerate(rank[-6:], len(rank) - 5):
        print(f"  {r_:<5}{cb:<7}{n:>7}{mu*100:>12.3f}%")
    aaa = next((x for x in rank if x[1] == "AAA"), None)
    fff = next((x for x in rank if x[1] == "FFF"), None)
    print(f"\n  AAA 排名 {[x[1] for x in rank].index('AAA')+1}/{len(rank)}" if aaa else "\n  AAA 样本不足")
    print(f"  FFF 排名 {[x[1] for x in rank].index('FFF')+1}/{len(rank)}（上涨侧，越靠后=跌得越多）" if fff else "")

    # ---------------- Q2 前瞻 ----------------
    print("\n" + "=" * 104)
    print("Q2  前瞻预测力：此刻是 AAA/FFF 的币，接下来会怎样？（方向收益：up 涨为正、down 跌为正）")
    print("=" * 104)
    for k in H:
        print(f"\n  —— 未来 {k} 个节点（{k*15/60:.1f} 小时）——")
        buckets: dict[str, list[float]] = defaultdict(list)
        for i in idx:
            for key, r in data[sids[i]].items():
                if not r["combo"]:
                    continue
                v = fwd(data, sids, i, key, k)
                if v is None:
                    continue
                buckets[f"{key[1]}:{r['combo']}"].append(v)
                buckets[f"{key[1]}:ALL"].append(v)
        for dirn, hero in (("up", "AAA"), ("down", "FFF")):
            allv = buckets.get(f"{dirn}:ALL") or []
            hv = buckets.get(f"{dirn}:{hero}") or []
            print(f"    {dirn:<5} 全体   {desc(allv)}")
            print(f"    {dirn:<5} {hero}    {desc(hv)}")
            if allv and hv:
                d = (st.mean(hv) - st.mean(allv)) * 100
                print(f"    {dirn:<5} {hero} 相对全体的超额: {d:>+7.3f} 个百分点"
                      f"  {'✅有正向前瞻力' if d > 0 else '❌无前瞻力（甚至为负）'}")

    # 最好/最差的前瞻组合（24 节点 = 6 小时）
    k = 24
    print(f"\n  —— 前瞻力最强 / 最弱的组合（{k} 节点 = 6 小时，样本≥300）——")
    fb: dict[str, list[float]] = defaultdict(list)
    for i in idx:
        for key, r in data[sids[i]].items():
            if not r["combo"]:
                continue
            v = fwd(data, sids, i, key, k)
            if v is not None:
                fb[f"{key[1]}:{r['combo']}"].append(v)
    rk = sorted(((st.mean(v), kk, len(v)) for kk, v in fb.items() if len(v) >= 300), reverse=True)
    print(f"  {'方向:组合':<14}{'n':>7}{'未来6h均值':>13}{'胜率':>9}")
    for mu, kk, n in rk[:8]:
        w = sum(1 for x in fb[kk] if x > 0) / n * 100
        print(f"  {kk:<14}{n:>7}{mu*100:>12.3f}%{w:>8.1f}%")
    print("  ...")
    for mu, kk, n in rk[-5:]:
        w = sum(1 for x in fb[kk] if x > 0) / n * 100
        print(f"  {kk:<14}{n:>7}{mu*100:>12.3f}%{w:>8.1f}%")

    # ---------------- Q3 拦截 ----------------
    print("\n" + "=" * 104)
    print("Q3  准入门拦截：AAA(上涨) / FFF(下跌) 的币被哪一道门挡在 DMR 之外？")
    print("=" * 104)
    print("     现网顺序：state==CONFIRMED → 216天花板 → DQ≥60 → dmr_zone_ok(DQ70/Score70/SS55/M65/C.67)")
    for dirn, hero in (("up", "AAA"), ("down", "FFF")):
        cnt = Counter()
        tot = 0
        for i in idx:
            for key, r in data[sids[i]].items():
                if key[1] != dirn or r["combo"] != hero:
                    continue
                tot += 1
                if r["dmr"]:
                    cnt["✅ 已入选 DMR"] += 1
                    continue
                # 逐门归因（按现网顺序，第一道未过的即为拦截门）
                if r["supply_missing"]:
                    cnt["缺流通供应量"] += 1
                elif (r["dm"] or "") != "LIVE":
                    cnt["data_mode != LIVE"] += 1
                elif r["state"] != "CONFIRMED":
                    s = r["score"] or 0
                    if s < C["score"]:
                        cnt[f"未进确认区：Score<{C['score']:g}"] += 1
                    elif (r["ss"] or 0) < C["ss"]:
                        cnt[f"未进确认区：SS<{C['ss']:g}"] += 1
                    elif (r["mom"] or 0) < C["mom"]:
                        cnt[f"未进确认区：动能<{C['mom']:g}"] += 1
                    elif (r["cons"] or 0) < C["cons"]:
                        cnt["未进确认区：一致性<1"] += 1
                    else:
                        cnt["未进确认区：停留/连击未满"] += 1
                elif (r["dq"] or 0) < G["dmr_dq"]:
                    cnt["DQ<70"] += 1
                elif (r["score"] or 0) < G["dmr_score"]:
                    cnt["★ DMR门：Score<70"] += 1
                elif (r["ss"] or 0) < G["dmr_ss"]:
                    cnt["★ DMR门：SS<55"] += 1
                elif (r["mom"] or 0) < G["dmr_mom"]:
                    cnt["★ DMR门：动能<65"] += 1
                elif (r["cons"] or 0) < G["dmr_cons"]:
                    cnt["★ DMR门：一致性<0.67"] += 1
                else:
                    cnt["过了全部门但未入选（Top-K 挤出 / 天花板否决）"] += 1
        print(f"\n  --- {dirn} 方向的 {hero} 共 {tot} 个「币×节点」样本 ---")
        for k2, v in cnt.most_common():
            print(f"    {k2:<44}{v:>7}  {v/max(tot,1)*100:>5.1f}%")

    print("\n注：Q1/Q2 用全部合约（不限 DMR）；Q3 用同一批样本按现网门槛顺序归因。")
    print("    前瞻收益未计手续费与滑点。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
