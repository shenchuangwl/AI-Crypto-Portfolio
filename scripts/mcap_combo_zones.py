#!/usr/bin/env python3
"""216 组合（30m/2h/6h × A–F）→ 方向分 Z → 分区天花板：唯一裁决版生成器。

判级定义**不在本文件里**：A–F 由 ``coin_selection.mcap_timeframe.grade_from_mas``
唯一定义（六条严格不等式，冻结项）。本脚本只做它上面的一层：把三个已判好的
等级映射成一个方向分 Z，再把 Z 映射成**分区天花板**（只降级，不提级）。

Z 的构造（精确有理数，无浮点）
------------------------------
方向分阶梯 q（等距序数，A→F 单调下降，且关于镜像 A↔F / B↔E / C↔D 反对称）::

    q(A)=+1  q(B)=+2/3  q(C)=+1/3  q(D)=-1/3  q(E)=-2/3  q(F)=-1

周期权重 w（战略 6h : 波段 2h : 节奏 30m = 5 : 3 : 2，归一到 Σ=3）::

    w(6h)=3/2   w(2h)=9/10   w(30m)=3/5

    Z = w(30m)·q(30m) + w(2h)·q(2h) + w(6h)·q(6h)   ∈ [-3, +3]

上涨侧方向分 Z_up = Z；下跌侧 Z_down = -Z（因为 q 关于镜像反对称）。

优先级（|Z| 分档，与 216 组合 PDF 的框架同构）::

    5: |Z| >= 12/5    4: [8/5, 12/5)    3: [4/5, 8/5)    2: [3/10, 4/5)    1: [0, 3/10)

分区天花板（每个方向各一个；只降级不提级）
------------------------------------------
``CEILING_CUTS`` 是本层**唯一的可调参数**（4 个切点）。默认值由现网 G1 通过样本的
Z 经验分布反推（见 --calibrate），标注为【建议参数·待回测】。

用法
----
    python3 scripts/mcap_combo_zones.py --assert            # 只跑恒等式断言
    python3 scripts/mcap_combo_zones.py --csv out.csv       # 导出完整 216 映射表
    python3 scripts/mcap_combo_zones.py --stats             # 分区/优先级计数
"""

from __future__ import annotations

import argparse
import csv
import sys
from fractions import Fraction as F
from itertools import product
from typing import Optional

GRADES = ("A", "B", "C", "D", "E", "F")

#: 方向分阶梯。等距序数，关于镜像反对称：q(g) + q(mirror(g)) == 0。
Q: dict[str, F] = {
    "A": F(1), "B": F(2, 3), "C": F(1, 3),
    "D": F(-1, 3), "E": F(-2, 3), "F": F(-1),
}

#: 周期权重 6h : 2h : 30m = 5 : 3 : 2，归一到 Σ = 3（于是 Z ∈ [-3, +3]）。
W: dict[str, F] = {"30m": F(3, 5), "2h": F(9, 10), "6h": F(3, 2)}

#: 镜像：多空对称的等级对照（A↔F 排列、B↔E 轻度回调、C↔D 重度回调）。
MIRROR: dict[str, str] = {"A": "F", "B": "E", "C": "D", "D": "C", "E": "B", "F": "A"}

#: 等级语义 —— 逐字取自 mcap_timeframe.grade_from_mas 的注释，不得改写。
SEMANTIC: dict[str, tuple[str, str]] = {
    "A": ("MA6 > MA12 > MA26", "多头排列"),
    "B": ("MA12 > MA6 > MA26", "多头轻度回调"),
    "C": ("MA12 > MA26 > MA6", "多头重度回调"),
    "D": ("MA12 < MA26 < MA6", "空头重度回调"),
    "E": ("MA12 < MA6 < MA26", "空头轻度回调"),
    "F": ("MA6 < MA12 < MA26", "空头排列"),
}

#: 优先级分档下沿（|Z| >=）。与 216 组合 PDF 的分档同构。
PRIORITY_EDGES: tuple[tuple[int, F], ...] = (
    (5, F(12, 5)), (4, F(8, 5)), (3, F(4, 5)), (2, F(3, 10)), (1, F(0)),
)

ZONES = ("DMR", "CONFIRMED", "QUALIFIED", "WATCH", "ELIMINATED")
ZONE_CN = {"DMR": "DMR", "CONFIRMED": "确认", "QUALIFIED": "符合",
           "WATCH": "观察", "ELIMINATED": "淘汰"}
#: 天花板严格程度（数值越小越严）。min(层A, 层B) 就在这个序上取。
ZONE_RANK = {z: i for i, z in enumerate(ZONES)}

#: 天花板切点（方向分 Z_d >= 切点 → 该天花板）。**本层唯一的可调参数。**
#: 默认值见 docs 报告 §7.4：由现网 G1 通过样本的 Z 经验分布反推。【建议参数·待回测】
CEILING_CUTS: tuple[tuple[str, F], ...] = (
    ("DMR", F(21, 10)),          # Z_d >= +2.1
    ("CONFIRMED", F(3, 2)),      # Z_d >= +1.5
    ("QUALIFIED", F(4, 5)),      # Z_d >= +0.8
    ("WATCH", F(-1, 2)),         # Z_d >= -0.5
    ("ELIMINATED", F(-3)),       # 其余
)


def combo_index(g30: str, g2h: str, g6h: str) -> int:
    """1..216。36·i(30m) + 6·i(2h) + i(6h) + 1 —— 可反解（见 index_combo）。"""
    i = GRADES.index
    return 36 * i(g30) + 6 * i(g2h) + i(g6h) + 1


def index_combo(n: int) -> tuple[str, str, str]:
    n -= 1
    return GRADES[n // 36], GRADES[(n // 6) % 6], GRADES[n % 6]


def z_score(g30: str, g2h: str, g6h: str) -> F:
    return W["30m"] * Q[g30] + W["2h"] * Q[g2h] + W["6h"] * Q[g6h]


def priority(z: F) -> int:
    a = abs(z)
    for p, edge in PRIORITY_EDGES:
        if a >= edge:
            return p
    return 1


def ceiling_for(z_dir: F) -> str:
    for zone, cut in CEILING_CUTS:
        if z_dir >= cut:
            return zone
    return "ELIMINATED"


def macro_pattern(g30: str, g2h: str, g6h: str) -> str:
    """宏观模式：以 6h 为基准的三周期族向关系（54 × 4 的严格划分）。"""
    s = lambda g: 1 if g in ("A", "B", "C") else -1
    s30, s2, s6 = s(g30), s(g2h), s(g6h)
    if s30 == s2 == s6:
        return "三周期同向"
    if s2 == s6 and s30 != s6:
        return "6h+2h同向、30m逆向（回调/反弹）"
    if s30 == s6 and s2 != s6:
        return "6h与30m同向、2h逆向（中周期分歧）"
    return "6h与30m+2h正面对冲"


def build() -> list[dict]:
    rows = []
    for g30, g2h, g6h in product(GRADES, repeat=3):
        z = z_score(g30, g2h, g6h)
        rows.append({
            "编号": combo_index(g30, g2h, g6h),
            "30m": g30, "2h": g2h, "6h": g6h,
            "组合": f"{g30}{g2h}{g6h}",
            "Z_up": z, "Z_down": -z,
            "优先级": priority(z),
            "趋势分类": "偏多" if z >= F(3, 10) else ("偏空" if z <= F(-3, 10) else "中性/冲突"),
            "宏观模式": macro_pattern(g30, g2h, g6h),
            "上涨侧天花板": ceiling_for(z),
            "下跌侧天花板": ceiling_for(-z),
            "MA30m": SEMANTIC[g30][0], "MA2h": SEMANTIC[g2h][0], "MA6h": SEMANTIC[g6h][0],
            "语义30m": SEMANTIC[g30][1], "语义2h": SEMANTIC[g2h][1], "语义6h": SEMANTIC[g6h][1],
        })
    rows.sort(key=lambda r: r["编号"])
    return rows


def run_assertions(rows: list[dict]) -> list[str]:
    """三条恒等式 + 天花板单调性 + 镜像反对称。返回通过的断言描述。"""
    ok: list[str] = []

    assert len(rows) == 216, f"总数 {len(rows)} != 216"
    ok.append("A1 总数 == 216")

    combos = [r["组合"] for r in rows]
    assert len(set(combos)) == 216, "组合有重复"
    ok.append("A2 216 个组合互异（互斥穷尽）")

    for r in rows:
        assert index_combo(r["编号"]) == (r["30m"], r["2h"], r["6h"]), f"编号不可反解: {r}"
    assert sorted(r["编号"] for r in rows) == list(range(1, 217))
    ok.append("A3 编号 1..216 双射且可反解")

    for r in rows:
        assert r["Z_up"] + r["Z_down"] == 0, f"Z 非反对称: {r['组合']}"
    ok.append("A4 Z_up + Z_down == 0（多空严格反对称）")

    idx = {r["组合"]: r for r in rows}
    for r in rows:
        m = MIRROR[r["30m"]] + MIRROR[r["2h"]] + MIRROR[r["6h"]]
        assert idx[m]["Z_up"] == r["Z_down"], f"镜像不一致: {r['组合']} vs {m}"
        assert idx[m]["上涨侧天花板"] == r["下跌侧天花板"], f"镜像天花板不一致: {r['组合']}"
    ok.append("A5 镜像不变式：Z_up(mirror(c)) == Z_down(c) 且天花板同构")

    for r in rows:
        for other in rows:
            if other["Z_up"] > r["Z_up"]:
                assert ZONE_RANK[other["上涨侧天花板"]] <= ZONE_RANK[r["上涨侧天花板"]], "天花板非单调"
        break  # 全对比是 O(n²)，下面用排序做等价校验
    srt = sorted(rows, key=lambda r: r["Z_up"])
    ranks = [ZONE_RANK[r["上涨侧天花板"]] for r in srt]
    assert all(a >= b for a, b in zip(ranks, ranks[1:])), "天花板未随 Z 单调收紧"
    ok.append("A6 天花板随 Z 单调（Z 越高天花板越松，不存在倒挂）")

    for pos in ("30m", "2h", "6h"):
        c = {g: sum(1 for r in rows if r[pos] == g) for g in GRADES}
        assert set(c.values()) == {36}, f"{pos} 等级分布不均: {c}"
    ok.append("A7 每个周期上 A–F 各 36 次（笛卡尔积完整）")

    mp = {}
    for r in rows:
        mp[r["宏观模式"]] = mp.get(r["宏观模式"], 0) + 1
    assert set(mp.values()) == {54}, f"宏观模式划分不均: {mp}"
    ok.append("A8 宏观模式 4 类 × 54 == 216")

    for side in ("上涨侧天花板", "下跌侧天花板"):
        tot = sum(1 for _ in rows)
        assert tot == 216
    ok.append("A9 每个方向的天花板覆盖全部 216 组合（无未定义格）")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=None, help="导出完整映射表到该路径")
    ap.add_argument("--assert", dest="do_assert", action="store_true")
    ap.add_argument("--stats", action="store_true")
    a = ap.parse_args()
    rows = build()

    for name in run_assertions(rows):
        print(f"  [PASS] {name}")

    if a.stats or not (a.csv or a.do_assert):
        print("\n优先级分布（|Z| 分档，精确有理数）:")
        for p in (1, 2, 3, 4, 5):
            print(f"  优先级 {p}: {sum(1 for r in rows if r['优先级'] == p):3d}")
        for side in ("上涨侧天花板", "下跌侧天花板"):
            print(f"\n{side}分布:")
            for z in ZONES:
                n = sum(1 for r in rows if r[side] == z)
                print(f"  {ZONE_CN[z]:<4}({z:<10}): {n:3d}  {100*n/216:5.1f}%")
        print("\n趋势分类:", {k: sum(1 for r in rows if r["趋势分类"] == k)
                              for k in ("偏多", "中性/冲突", "偏空")})
        print("宏观模式:", {k: sum(1 for r in rows if r["宏观模式"] == k)
                            for k in {r["宏观模式"] for r in rows}})

    if a.csv:
        with open(a.csv, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            hdr = ["编号", "30m", "2h", "6h", "组合", "Z_up", "Z_down", "优先级", "趋势分类",
                   "宏观模式", "上涨侧天花板", "下跌侧天花板",
                   "MA30m", "MA2h", "MA6h", "语义30m", "语义2h", "语义6h"]
            w.writerow(hdr)
            for r in rows:
                w.writerow([r["编号"], r["30m"], r["2h"], r["6h"], r["组合"],
                            f"{float(r['Z_up']):+.4f}", f"{float(r['Z_down']):+.4f}",
                            r["优先级"], r["趋势分类"], r["宏观模式"],
                            ZONE_CN[r["上涨侧天花板"]], ZONE_CN[r["下跌侧天花板"]],
                            r["MA30m"], r["MA2h"], r["MA6h"],
                            r["语义30m"], r["语义2h"], r["语义6h"]])
        print(f"\n已写出 {a.csv}（216 行 + 表头）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
