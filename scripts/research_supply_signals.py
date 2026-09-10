#!/usr/bin/env python3
"""【研究脚本 · 只读 · 用完可直接删除】真·含供应量信息的市值信号回测。

    删除本文件不影响任何生产代码：它不被任何模块 import，不写任何生产目录，
    只读 data/research/tape.pkl 与 data/research/<exp>/ledger-*.sqlite。

背景
----
现网 216 层用的「三周期流通市值均线排列（A–F）」在当前窗口上**等价于价格均线排列**：
实测 68.7% 的币种流通供应量在窗口内恒定，其余最大变动 0.90%，而
``市值 = 价格 × 供应量``，供应量恒定 ⇒ ``MA_k(市值) = 常数 × MA_k(价格)`` ⇒ 排列顺序相同。
所以它不是独立的市值信号（文档A SOL5.6 版 §2 第 6 条已预警）。

本脚本检验三类**真正含供应量信息**的量，看它们能否改善三要素：

  S1 市值分位 mcap_pctile
     截面分位（0=最小市值，1=最大）。规模因子 —— 它必须知道供应量才能算，
     而且是**跨币种相对**的，与单币自身趋势正交。

  S2 市值/成交额比 mcap_to_turnover
     ``市值 / 日均成交额``，量纲是「换手一遍需要多少天」。同时含供应量与成交量，
     是三个信号里唯一同时用到两侧信息的。高 = 盘子相对成交额过大（滞涨/流动性差）。

  S3 市值分位变动 mcap_pctile_delta_24h
     24 小时（96 节点）前后的截面分位之差。相对强弱 —— 与「自己涨没涨」不同，
     它问的是「相对于全市场，它的规模排名在往上还是往下走」。

  S4 供应量变动 supply_delta_24h（对照项）
     真正的供应量变化率。窗口内只有 31% 的币有变化且幅度 < 1%，
     预计无区分度 —— 放进来是为了**证伪**，不是为了找信号。

方法
----
不重跑状态机（那要 10 分钟/配置）。改用**信号分桶**：把 E0 基线回测里每一笔
DMR 已平仓交易，按其**进场节点**的信号取值分到五分位桶，逐桶算三要素。
若某个桶在胜率、盈亏比、盈利率上同时优于全样本，该信号才有筛选价值。

指标口径与 scripts/replay_rescore.py::occupancy_metrics 完全一致：
  胜率   = 盈利笔数 / 总笔数（含平局，平局阈值 FLAT_EPS）
  盈亏比 = 平均盈利 / |平均亏损|
  盈利率 = Σ pnl_pct（即《复盘选币》UI 的「合计」）
  PF     = Σ盈利 / Σ|亏损|
排除 CYCLE_RESET 强平、未平仓、缺价的交易。

用法
----
    PYTHONPATH=services/coin-selection/src python3 scripts/research_supply_signals.py
    PYTHONPATH=... python3 scripts/research_supply_signals.py --exp E0_live --zone DMR
"""

from __future__ import annotations

import argparse
import glob
import math
import pickle
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "coin-selection" / "src"))

from coin_selection.review_pnl import FLAT_EPS  # noqa: E402

#: Gate1 软分的两个常量（gate1.py:29,124）。用于把 liquidity_score_abs 反解成成交额。
#: S_abs = 100 * clip((ln T_geo - ln FLOOR) / (ln CAP - ln FLOOR), 0, 1)
G1_FLOOR_USD = 3_000_000.0
G1_CAP_USD = 50_000_000.0

NODES_PER_DAY = 96


def turnover_from_score(s_abs: Optional[float]) -> Optional[float]:
    """由 Gate1 软分反解日均成交额（几何平均 T_geo）。

    只在 ``0 < S_abs < 100`` 时可解：两端是被 clip 压平的，信息已丢失。
    """
    if s_abs is None:
        return None
    s = float(s_abs)
    if not (0.0 < s < 100.0):
        return None
    ln_lo, ln_hi = math.log(G1_FLOOR_USD), math.log(G1_CAP_USD)
    return math.exp(ln_lo + (s / 100.0) * (ln_hi - ln_lo))


def pctile_map(values: dict[str, float]) -> dict[str, float]:
    """截面分位：值越大分位越接近 1。并列取平均秩。"""
    if not values:
        return {}
    items = sorted(values.items(), key=lambda kv: kv[1])
    n = len(items)
    out: dict[str, float] = {}
    i = 0
    while i < n:
        j = i
        while j + 1 < n and items[j + 1][1] == items[i][1]:
            j += 1
        rank = (i + j) / 2.0
        p = rank / (n - 1) if n > 1 else 0.5
        for k in range(i, j + 1):
            out[items[k][0]] = p
        i = j + 1
    return out


def build_signals(tape_path: Path) -> dict[str, dict[str, dict[str, float]]]:
    """→ {scan_id: {symbol: {信号名: 值}}}。只读 tape。"""
    tape = pickle.load(open(tape_path, "rb"))
    nodes = tape["nodes"]
    print(f"[tape] {len(nodes)} 个节点  {tape.get('first')} → {tape.get('last')}", file=sys.stderr)

    # 第一遍：逐节点算市值、成交额、比值，并求截面分位
    per_node: dict[str, dict[str, dict[str, float]]] = {}
    order: list[str] = []
    for nd in nodes:
        sid = nd["scan_id"]
        order.append(sid)
        mcap: dict[str, float] = {}
        mtt: dict[str, float] = {}
        supply: dict[str, float] = {}
        for r in nd["rows"]:
            sym = r.get("symbol")
            sup = r.get("circulating_supply")
            px = r.get("last_price") or r.get("mark_price")
            if not sym or not sup or not px:
                continue
            cap = float(px) * float(sup)
            if cap <= 0:
                continue
            mcap[sym] = cap
            supply[sym] = float(sup)
            t = turnover_from_score(r.get("liquidity_score_abs"))
            if t and t > 0:
                mtt[sym] = cap / t          # 换手一遍所需天数
        mc_p = pctile_map(mcap)
        mtt_p = pctile_map(mtt)
        per_node[sid] = {
            sym: {
                "mcap_usd": mcap[sym],
                "mcap_pctile": mc_p.get(sym, float("nan")),
                "supply": supply.get(sym, float("nan")),
                **(
                    {"mcap_to_turnover": mtt[sym], "mtt_pctile": mtt_p.get(sym, float("nan"))}
                    if sym in mtt
                    else {}
                ),
            }
            for sym in mcap
        }

    # 第二遍：24h 前后的差分（分位变动 / 供应量变动）
    idx = {sid: i for i, sid in enumerate(order)}
    for sid in order:
        i = idx[sid]
        j = i - NODES_PER_DAY
        if j < 0:
            continue
        prev = per_node[order[j]]
        cur = per_node[sid]
        for sym, d in cur.items():
            p0 = prev.get(sym)
            if not p0:
                continue
            if not math.isnan(d["mcap_pctile"]) and not math.isnan(p0["mcap_pctile"]):
                d["mcap_pctile_delta_24h"] = d["mcap_pctile"] - p0["mcap_pctile"]
            s0, s1 = p0.get("supply"), d.get("supply")
            if s0 and s1 and not math.isnan(s0) and not math.isnan(s1) and s0 > 0:
                d["supply_delta_24h"] = s1 / s0 - 1.0
    return per_node


def load_trades(exp_dir: Path, zone: str) -> list[dict[str, Any]]:
    """从研究账本读该分区的已平仓交易（排除 CYCLE_RESET / 缺价）。"""
    files = glob.glob(str(exp_dir / "*.sqlite"))
    if not files:
        raise SystemExit(f"找不到账本: {exp_dir}/*.sqlite")
    c = sqlite3.connect("file:" + files[0] + "?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    rows = [
        dict(r)
        for r in c.execute(
            "SELECT symbol, direction, enter_scan_id, pnl_pct, flags "
            "FROM trades WHERE zone=? AND status='CLOSED' AND pnl_pct IS NOT NULL",
            (zone,),
        )
    ]
    out = [r for r in rows if "CYCLE_RESET" not in (r["flags"] or "")]
    print(f"[ledger] {files[0]}  {zone} 已平仓 {len(rows)} 笔，排除 CR 后 {len(out)} 笔", file=sys.stderr)
    return out


def stats(pnl: list[float]) -> dict[str, Any]:
    """与 replay_rescore.occupancy_metrics 同口径。"""
    n = len(pnl)
    if n == 0:
        return {"n": 0, "win_rate": None, "payoff": None, "pf": None, "total": None}
    wins = [p for p in pnl if p > FLAT_EPS]
    losses = [p for p in pnl if p < -FLAT_EPS]
    avg_w = sum(wins) / len(wins) if wins else 0.0
    avg_l = sum(losses) / len(losses) if losses else 0.0
    payoff = (avg_w / abs(avg_l)) if avg_l else (math.inf if avg_w else None)
    gross_w, gross_l = sum(wins), abs(sum(losses))
    pf = (gross_w / gross_l) if gross_l else (math.inf if gross_w else None)
    return {
        "n": n,
        "win_rate": len(wins) / n,
        "payoff": payoff,
        "pf": pf,
        "total": sum(pnl),          # 盈利率（合计），与《复盘选币》UI 的「合计」同口径
        "avg": sum(pnl) / n,
    }


def fmt(m: dict[str, Any]) -> str:
    if not m["n"]:
        return f"{'—':>6}"
    p = m["payoff"]
    f = m["pf"]
    return (
        f"{m['n']:>6}"
        f"{m['win_rate'] * 100:>9.2f}%"
        f"{(p if p is not None and p != math.inf else float('nan')):>9.3f}"
        f"{(f if f is not None and f != math.inf else float('nan')):>8.3f}"
        f"{m['total']:>10.2f}%"
        f"{m['avg']:>9.3f}%"
    )


HEADER = f"{'桶':<26}{'n':>6}{'胜率':>10}{'盈亏比':>9}{'PF':>8}{'盈利率':>11}{'平均':>10}"


def bucket_report(
    trades: list[dict[str, Any]],
    sig: dict[str, dict[str, dict[str, float]]],
    key: str,
    *,
    nbuckets: int = 5,
    by_direction: bool = False,
) -> None:
    """按信号五分位分桶，逐桶报三要素。"""
    vals: list[tuple[float, float, str]] = []   # (信号值, pnl, direction)
    miss = 0
    for t in trades:
        node = sig.get(t["enter_scan_id"]) or {}
        d = node.get(t["symbol"])
        v = (d or {}).get(key)
        if v is None or (isinstance(v, float) and math.isnan(v)):
            miss += 1
            continue
        vals.append((float(v), float(t["pnl_pct"]), t["direction"]))
    if not vals:
        print(f"\n### {key}：无可用样本（缺失 {miss}）")
        return
    vals.sort(key=lambda x: x[0])
    base = stats([p for _, p, _ in vals])
    print(f"\n### {key}   可用 {len(vals)} 笔（缺失 {miss}）")
    print(HEADER)
    print(f"{'全样本':<26}{fmt(base)}")
    n = len(vals)
    for b in range(nbuckets):
        lo, hi = n * b // nbuckets, n * (b + 1) // nbuckets
        seg = vals[lo:hi]
        if not seg:
            continue
        m = stats([p for _, p, _ in seg])
        lab = f"Q{b + 1} [{seg[0][0]:.4g}, {seg[-1][0]:.4g}]"
        flag = ""
        if (
            m["win_rate"] is not None
            and base["win_rate"] is not None
            and m["win_rate"] > base["win_rate"]
            and m["payoff"] is not None
            and base["payoff"] is not None
            and m["payoff"] > base["payoff"]
            and m["total"] > base["total"] / max(len(vals), 1) * m["n"]
        ):
            flag = "  ★三要素同向优于全样本"
        print(f"{lab:<26}{fmt(m)}{flag}")
    if by_direction:
        for dirn in ("up", "down"):
            sub = [(v, p) for v, p, dd in vals if dd == dirn]
            if not sub:
                continue
            print(f"  —— 仅 {dirn} 方向（{len(sub)} 笔）——")
            for b in range(nbuckets):
                lo, hi = len(sub) * b // nbuckets, len(sub) * (b + 1) // nbuckets
                seg = sub[lo:hi]
                if not seg:
                    continue
                m = stats([p for _, p in seg])
                print(f"  {'Q' + str(b + 1):<24}{fmt(m)}")


def filter_report(
    trades: list[dict[str, Any]],
    sig: dict[str, dict[str, dict[str, float]]],
    key: str,
    *,
    quantiles=(0.2, 0.4, 0.6, 0.8),
) -> None:
    """把信号当**准入过滤器**用：只保留 >= 某分位 / <= 某分位，看整体三要素。"""
    vals = []
    for t in trades:
        d = (sig.get(t["enter_scan_id"]) or {}).get(t["symbol"])
        v = (d or {}).get(key)
        if v is None or (isinstance(v, float) and math.isnan(v)):
            continue
        vals.append((float(v), float(t["pnl_pct"])))
    if not vals:
        return
    vals.sort(key=lambda x: x[0])
    base = stats([p for _, p in vals])
    n = len(vals)
    print(f"\n### {key} 作为准入过滤器")
    print(HEADER)
    print(f"{'不过滤（基线）':<26}{fmt(base)}")
    for q in quantiles:
        cut = int(n * q)
        hi = [p for _, p in vals[cut:]]
        lo = [p for _, p in vals[:n - cut]] if cut else None
        mh = stats(hi)
        tag = ""
        if (
            mh["n"]
            and mh["win_rate"] > base["win_rate"]
            and mh["payoff"] > base["payoff"]
            and mh["avg"] > base["avg"]
        ):
            tag = "  ★三要素全优"
        print(f"{f'只留 top {int((1-q)*100)}%':<26}{fmt(mh)}{tag}")
        if lo is not None:
            ml = stats(lo)
            tag2 = ""
            if (
                ml["n"]
                and ml["win_rate"] > base["win_rate"]
                and ml["payoff"] > base["payoff"]
                and ml["avg"] > base["avg"]
            ):
                tag2 = "  ★三要素全优"
            print(f"{f'只留 bottom {int((1-q)*100)}%':<26}{fmt(ml)}{tag2}")


def main() -> int:
    ap = argparse.ArgumentParser(description="真·含供应量信息的市值信号回测（研究用，可删）")
    ap.add_argument("--tape", default="data/research/tape.pkl")
    ap.add_argument("--exp", default="E0_live", help="研究账本目录名（data/research/<exp>）")
    ap.add_argument("--zone", default="DMR")
    ap.add_argument("--buckets", type=int, default=5)
    ap.add_argument("--by-direction", action="store_true")
    a = ap.parse_args()

    sig = build_signals(ROOT / a.tape)
    trades = load_trades(ROOT / "data" / "research" / a.exp, a.zone)

    print("=" * 96)
    print(f"信号分桶回测   实验={a.exp}  分区={a.zone}  桶数={a.buckets}")
    print("指标口径与 replay_rescore.occupancy_metrics 一致；已排除 CYCLE_RESET / 未平仓 / 缺价")
    print("盈利率 = Σ pnl_pct（《复盘选币》UI 的「合计」）；平均 = 每笔平均")
    print("=" * 96)

    for key in (
        "mcap_pctile",
        "mtt_pctile",
        "mcap_pctile_delta_24h",
        "supply_delta_24h",
    ):
        bucket_report(trades, sig, key, nbuckets=a.buckets, by_direction=a.by_direction)

    for key in ("mcap_pctile", "mtt_pctile", "mcap_pctile_delta_24h"):
        filter_report(trades, sig, key)

    print("\n注：★ 只表示该子集在本窗口上三要素同向更好，**不是**统计显著性。")
    print("    n 越小越可能是噪声；要下结论必须做样本外验证与置换检验。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
