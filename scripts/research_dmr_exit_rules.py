#!/usr/bin/env python3
"""【研究脚本 · 只读 · 用完可直接删除】DMR 退出条件原型回测（建议 1+2）。

    删除本文件不影响任何生产代码：不被任何模块 import，不写任何目录。

事实源：**只用选币榜Y 自己的真实产出**
--------------------------------------
    data/coin-selection-y/snapshots/*.json     真实板面（逐节点 dmr_selected / state /
                                               score / SS / 动能 / last_price / cycle）
    data/coin-selection-y/review/ledger.sqlite 真实账本（仅用于对账校验）

不用主榜快照、不用 data/research/tape.pkl、不重算任何指标。

方法：只改退出，不改进场
------------------------
**进场事件直接取真实 `dmr_selected` 由 false→true 的那一刻**，价格取该节点真实
`last_price`。因此「选了哪些币、什么时候选的、以什么价格选的」100% 是现网真实数据，
一个字节都不模拟 —— 这样才能把「退出规则」的效果单独隔离出来，
也不需要重实现 Top-K 排名（Top-K 竞争已隐含在真实进场事件里）。

四种退出规则
------------
    BASE      现行规则：真实 `dmr_selected` 由 true→false 即退出（= 复现现网）
    H(x)      建议 1 · 退出滞回：进场后只要 state 仍是 CONFIRMED 且 score >= x 就继续持有，
              忽略 Top-K 挤出。x < 70 才构成滞回（现行进出同为 70，无滞回）。
    D(n)      建议 2 · 最短停留：BASE 想退出时，若不足 n 个节点则继续持有到第 n 个节点。
    H(x)+D(n) 两者叠加。

任何规则下，24h 周期重置（00:00 UTC）都强制平仓并打 CYCLE_RESET —— 与生产一致，
且与账本口径一致地从统计中剔除。

盈亏口径与 review_pnl.realized_pnl 完全一致：
    up   : pct = exit/enter - 1
    down : pct = 1 - exit/enter
``pnl_pct`` 是小数，本报告一律 ×100 显示为百分数。

用法
----
    PYTHONPATH=services/coin-selection/src python3 scripts/research_dmr_exit_rules.py
    PYTHONPATH=... python3 scripts/research_dmr_exit_rules.py --from 20260901-000
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

from coin_selection.review_pnl import FLAT_EPS  # noqa: E402

Y = ROOT / "data" / "coin-selection-y"
TARGET = {"win": 0.50, "payoff": 1.0, "total": 1.0}   # 胜率>=50% / 盈亏比>=1 / 盈利率>=100%


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
        "n": n, "win": len(w) / n,
        "payoff": (aw / abs(al)) if al else float("nan"),
        "pf": (gw / gl) if gl else float("nan"),
        "total": sum(pnl), "avg": sum(pnl) / n,
    }


HDR = (f"{'退出规则':<30}{'n':>6}{'胜率':>9}{'盈亏比':>9}{'PF':>8}"
       f"{'盈利率':>11}{'平均':>9}{'中位停留':>9}")


def fmt(lab: str, tr: list[dict]) -> Optional[dict]:
    m = stats([t["pnl"] for t in tr])
    if not m["n"]:
        print(f"{lab:<30}{'—':>6}")
        return None
    dw = sorted(t["dwell"] for t in tr)
    med = dw[len(dw) // 2]
    ok = (m["win"] >= TARGET["win"] and m["payoff"] >= TARGET["payoff"]
          and m["total"] >= TARGET["total"])
    flags = ("胜" if m["win"] >= TARGET["win"] else "·") \
        + ("赔" if m["payoff"] >= TARGET["payoff"] else "·") \
        + ("利" if m["total"] >= TARGET["total"] else "·")
    tag = "  ✅三项达标" if ok else (f"  [{flags}]" if flags != "···" else "")
    print(f"{lab:<30}{m['n']:>6}{m['win'] * 100:>8.2f}%{m['payoff']:>9.3f}{m['pf']:>8.3f}"
          f"{m['total'] * 100:>10.2f}%{m['avg'] * 100:>8.3f}%{med:>8}节{tag}")
    return m


# --------------------------------------------------------------------------- 数据
def load_nodes(frm: Optional[str], to: Optional[str]) -> list[dict]:
    """按 scan_id 升序读全部真实 Y 快照，抽出模拟需要的最小字段。"""
    files = sorted((Y / "snapshots").glob("*.json"))
    out = []
    for f in files:
        sid = f.stem
        if frm and sid < frm:
            continue
        if to and sid > to:
            continue
        d = json.loads(f.read_text(encoding="utf-8"))
        rows: dict[tuple, dict] = {}
        for pool in ("long_pool", "short_pool"):
            for r in d.get(pool) or []:
                dirn = r.get("direction")
                rows[(r["symbol"], dirn)] = {
                    "state": r.get("state"),
                    "score": r.get("score_up") if dirn == "up" else r.get("score_down"),
                    "ss": r.get("staircase_score"),
                    "mom": r.get("momentum_score"),
                    "dmr": bool(r.get("dmr_selected")),
                    "px": r.get("last_price"),
                }
        cyc = (d.get("meta") or {}).get("cycle") or {}
        out.append({"sid": sid, "rows": rows, "reset": bool(cyc.get("reset_at_this_node"))})
    return out


# --------------------------------------------------------------------------- 模拟
def simulate(nodes: list[dict], *, exit_score: Optional[float], min_dwell: int) -> list[dict]:
    """进场 = 真实 dmr_selected 上升沿；退出规则由参数决定。返回已平仓交易。"""
    open_pos: dict[tuple, dict] = {}
    closed: list[dict] = []

    def close(key, node, px, reason, forced=False):
        p = open_pos.pop(key)
        if px is None or p["enter_px"] in (None, 0):
            return
        sym, dirn = key
        pct = (px / p["enter_px"] - 1.0) if dirn == "up" else (1.0 - px / p["enter_px"])
        closed.append({
            "symbol": sym, "direction": dirn, "enter_sid": p["sid"], "exit_sid": node["sid"],
            "enter_px": p["enter_px"], "exit_px": px, "pnl": 0.0 if abs(pct) < FLAT_EPS else pct,
            "dwell": p["n"], "reason": reason, "cycle_reset": forced,
        })

    for i, node in enumerate(nodes):
        rows = node["rows"]
        # —— 周期重置：先按当刻价强平全部持仓（与 cycle_reset 生产序一致）——
        if node["reset"] and open_pos:
            for key in list(open_pos):
                close(key, node, (rows.get(key) or {}).get("px"), "CYCLE_RESET", forced=True)
        # —— 已持仓的退出判定 ——
        for key in list(open_pos):
            p = open_pos[key]
            p["n"] += 1
            r = rows.get(key)
            if r is None:                       # 合约当刻不在板面
                close(key, node, p["last_px"], "VANISHED")
                continue
            p["last_px"] = r["px"]
            if min_dwell and p["n"] < min_dwell:
                continue                        # 建议 2：最短停留保护期内不退出
            if exit_score is None:
                if not r["dmr"]:                # BASE：真实 dmr_selected 落下
                    close(key, node, r["px"], "BASE_DMR_OFF")
            else:
                # 建议 1：滞回 —— 只要还在确认区且 score 未跌破 exit_score 就继续持有
                if r["state"] != "CONFIRMED":
                    close(key, node, r["px"], "LEFT_CONFIRMED")
                elif r["score"] is None or r["score"] < exit_score:
                    close(key, node, r["px"], f"SCORE<{exit_score:g}")
        # —— 进场：真实 dmr_selected 上升沿 ——
        prev = nodes[i - 1]["rows"] if i else {}
        for key, r in rows.items():
            if not r["dmr"] or key in open_pos:
                continue
            if (prev.get(key) or {}).get("dmr"):
                continue                        # 不是上升沿
            if r["px"] in (None, 0):
                continue
            open_pos[key] = {"sid": node["sid"], "enter_px": r["px"], "n": 0, "last_px": r["px"]}
    return closed


def valid(trades: list[dict]) -> list[dict]:
    """与账本同口径剔除：周期重置强平、合约消失。"""
    return [t for t in trades if not t["cycle_reset"] and t["reason"] != "VANISHED"]


# --------------------------------------------------------------------------- 对账
def reconcile(sim: list[dict]) -> None:
    """BASE 模拟 vs 真实账本 —— 对不上，后面所有结论都不成立。"""
    c = sqlite3.connect("file:" + str(Y / "review" / "ledger.sqlite") + "?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    real = {
        (r["symbol"], r["direction"], r["enter_scan_id"]): r
        for r in c.execute(
            "SELECT symbol,direction,enter_scan_id,exit_scan_id,enter_price,exit_price,pnl_pct,flags "
            "FROM trades WHERE zone='DMR' AND status='CLOSED' AND pnl_pct IS NOT NULL")
        if "CYCLE_RESET" not in (r["flags"] or "")
    }
    mine = {(t["symbol"], t["direction"], t["enter_sid"]): t for t in valid(sim)}
    both = set(real) & set(mine)
    px_ok = sum(1 for k in both
                if abs(float(real[k]["enter_price"]) - mine[k]["enter_px"])
                <= max(1e-12, abs(mine[k]["enter_px"]) * 1e-9))
    pnl_ok = sum(1 for k in both
                 if abs(float(real[k]["pnl_pct"]) - mine[k]["pnl"]) < 1e-9)
    exit_ok = sum(1 for k in both if real[k]["exit_scan_id"] == mine[k]["exit_sid"])
    print("\n" + "=" * 104)
    print("一、BASE 复现对账 —— 用真实快照重跑现行规则，与真实账本逐笔比对")
    print("=" * 104)
    print(f"  真实账本 DMR 有效交易      {len(real)}")
    print(f"  BASE 模拟产出              {len(mine)}")
    print(f"  两侧同键（币+方向+进场节点）  {len(both)}"
          f"   仅账本有 {len(set(real) - set(mine))}   仅模拟有 {len(set(mine) - set(real))}")
    if both:
        print(f"  进场价逐笔相等             {px_ok}/{len(both)}  ({px_ok / len(both) * 100:.2f}%)")
        print(f"  退出节点一致               {exit_ok}/{len(both)}  ({exit_ok / len(both) * 100:.2f}%)")
        print(f"  PnL 逐笔相等（1e-9）       {pnl_ok}/{len(both)}  ({pnl_ok / len(both) * 100:.2f}%)")
        rate = pnl_ok / len(both)
        print("\n  → " + ("✅ 模拟器可信，后续变体结果有效。"
                          if rate > 0.98 else
                          "❌ 复现率不足 98%，后续变体结果**不可采信**。"))


# --------------------------------------------------------------------------- 主
def main() -> int:
    ap = argparse.ArgumentParser(description="DMR 退出规则原型回测（研究用，可删）")
    ap.add_argument("--from", dest="frm", default=None)
    ap.add_argument("--to", dest="to", default=None)
    a = ap.parse_args()

    nodes = load_nodes(a.frm, a.to)
    print("=" * 104)
    print("《选币榜Y》v2.0.0 · DMR 退出规则原型回测（建议 1 滞回 + 建议 2 最短停留）")
    print(f"事实源：Y 自己的真实快照 {len(nodes)} 个节点  {nodes[0]['sid']} → {nodes[-1]['sid']}")
    print("进场事件 100% 取真实 dmr_selected 上升沿与真实 last_price，只改退出规则。")
    print("=" * 104)

    base = simulate(nodes, exit_score=None, min_dwell=0)
    reconcile(base)

    print("\n" + "=" * 104)
    print("二、退出规则对比（全部为真实进场事件；已剔除周期重置强平与合约消失）")
    print("=" * 104)
    print(f"\n{HDR}")
    fmt("BASE 现行（进出同为70，无滞回）", valid(base))

    print("\n  —— 建议 1：退出滞回（进场仍需 score>=70）——")
    for x in (68, 66, 64, 62, 60, 56):
        fmt(f"  H({x})  跌破 {x} 才退出", valid(simulate(nodes, exit_score=float(x), min_dwell=0)))

    print("\n  —— 建议 2：最短停留（BASE 退出规则 + 保护期）——")
    for n in (2, 3, 4, 5, 6, 8):
        fmt(f"  D({n})  至少持有 {n} 节点", valid(simulate(nodes, exit_score=None, min_dwell=n)))

    print("\n  —— 建议 1+2 叠加 ——")
    best = None
    for x in (66, 62, 56):
        for n in (3, 5, 8):
            tr = valid(simulate(nodes, exit_score=float(x), min_dwell=n))
            m = fmt(f"  H({x})+D({n})", tr)
            if m and m["win"] >= TARGET["win"] and m["payoff"] >= TARGET["payoff"] \
                    and m["total"] >= TARGET["total"]:
                if best is None or m["total"] > best[0]["total"]:
                    best = (m, f"H({x})+D({n})", tr)

    print("\n" + "=" * 104)
    print("三、上涨 / 下跌候选池分别表现（取盈利率最高的规则）")
    print("=" * 104)
    cands = [("BASE", None, 0)]
    for x in (66, 62, 56):
        for n in (0, 3, 5, 8):
            cands.append((f"H({x})+D({n})" if n else f"H({x})", float(x), n))
    for n in (3, 5, 8):
        cands.append((f"D({n})", None, n))
    scored = []
    for lab, x, n in cands:
        tr = valid(simulate(nodes, exit_score=x, min_dwell=n))
        m = stats([t["pnl"] for t in tr])
        if m["n"]:
            scored.append((m["total"], lab, x, n, tr))
    scored.sort(reverse=True, key=lambda s: s[0])
    for tot, lab, x, n, tr in scored[:3]:
        print(f"\n  === {lab} ===\n{HDR}")
        fmt(f"  {lab} 全部", tr)
        for dirn, dl in (("up", "上涨候选池"), ("down", "下跌候选池")):
            fmt(f"    {dl}", [t for t in tr if t["direction"] == dirn])

    print("\n" + "=" * 104)
    print("四、停留分布是否被推到 >=5 节点")
    print("=" * 104)
    print(f"\n{'规则':<22}{'n':>6}{'1节点':>8}{'2-4':>8}{'5-8':>8}{'9-24':>8}{'>=25':>8}{'>=5占比':>9}")
    for lab, x, n in cands[:9]:
        tr = valid(simulate(nodes, exit_score=x, min_dwell=n))
        if not tr:
            continue
        b = [0] * 5
        for t in tr:
            d = t["dwell"]
            b[0 if d <= 1 else 1 if d <= 4 else 2 if d <= 8 else 3 if d <= 24 else 4] += 1
        tot = len(tr)
        print(f"{lab:<22}{tot:>6}{b[0]:>8}{b[1]:>8}{b[2]:>8}{b[3]:>8}{b[4]:>8}"
              f"{(b[2] + b[3] + b[4]) / tot * 100:>8.1f}%")

    print("\n注：进场事件为真实数据；退出为规则模拟。滞回规则忽略 Top-K 挤出，")
    print("    因此并发持仓数会高于现网 K=16，落地时需同时规定并发上限。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
