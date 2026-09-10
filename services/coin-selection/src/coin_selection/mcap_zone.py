"""216 组合分区天花板层 —— 文档A §4 的**唯一裁决版**（只降不升，可一键退化）。

权威
----
* 规则本体：``docs/Claude_Opus5_新五区币种选入标准报告_升级版_v2.0.0.md`` §3.4 / §4 / §5 / §6
* 落地与回测：``docs/Claude_Opus5_《选币榜Y》与《复盘选币》栏目……整体设计流程方案计划.md``
  §3（一致性契约）/ §8 阶段 1–2 / §9（测试 T8–T14、冻结回归 F1）
* 生成器与 9 条恒等式断言：``scripts/mcap_combo_zones.py``（本模块与它**逐格相等**，
  由测试 T9 钉住；两份实现之间不允许有第二套切点或第二套 Z）

判级定义**不在本文件里**：A–F 由 ``coin_selection.mcap_timeframe.grade_from_mas``
唯一定义（六条严格不等式，冻结项，红线第 12 条）。本模块只做它上面的一层。

三条不变式（单测穷举 2,160 例钉住，见 tests/test_mcap_zone.py）
--------------------------------------------------------------
1. **只降不升**：返回值的 ``ZONE_RANK`` 恒 ≥ 入参 state 的 ``ZONE_RANK``。
   任何「组合好就直接进 DMR」的写法都是越权（红线第 13 条）。
2. **开关关 = 恒等映射**：``mcap_zone_mode="off"`` 时本模块一个字段都不写，
   Y 板面与现网逐字节相同（红线第 14 条）。
3. **shadow 不改分区**：``mcap_zone_mode="shadow"`` 时新字段全写、``state_*`` 全不动。

与 ``mcap_combo.py`` 的关系
---------------------------
``mcap_combo`` 是上一轮（Grok4.6 / SOL 口径，每侧 12/23/29/53/99）的规则式天花板，
保留为纯函数库与既有单测的事实源，**不再由生产投影路径调用**。本模块是文档A §3.2
裁决后的唯一生产实现（每侧 13/22/32/72/77，由 4 个切点 + 精确有理数 Z 生成）。
两者共用同一个 Z（``mcap_combo.z10`` = 本模块 ``z10`` × 1，已由测试交叉核对）。
"""

from __future__ import annotations

import logging
from fractions import Fraction as F
from itertools import product
from typing import Any, Iterable, Optional

log = logging.getLogger("coin_selection.mcap_zone")

GRADES: tuple[str, ...] = ("A", "B", "C", "D", "E", "F")

#: 方向分阶梯 q —— 等距序数，关于镜像 A↔F / B↔E / C↔D 严格反对称（文档A §4.2）。
Q: dict[str, F] = {
    "A": F(1),
    "B": F(2, 3),
    "C": F(1, 3),
    "D": F(-1, 3),
    "E": F(-2, 3),
    "F": F(-1),
}

#: 周期权重 w —— 战略 6h : 波段 2h : 节奏 30m = 5 : 3 : 2，归一到 Σ=3 ⇒ Z ∈ [−3, +3]。
W: dict[str, F] = {"30m": F(3, 5), "2h": F(9, 10), "6h": F(3, 2)}

MIRROR: dict[str, str] = {"A": "F", "B": "E", "C": "D", "D": "C", "E": "B", "F": "A"}

#: 优先级分档下沿（|Z| >=），闭区间下沿。精确有理数，无浮点比较。
PRIORITY_EDGES: tuple[tuple[int, F], ...] = (
    (5, F(12, 5)),
    (4, F(8, 5)),
    (3, F(4, 5)),
    (2, F(3, 10)),
    (1, F(0)),
)

ZONES: tuple[str, ...] = ("DMR", "CONFIRMED", "QUALIFIED", "WATCH", "ELIMINATED")

#: 严格程度序：数值越小越松。``min(状态机分区, 天花板)`` 就在这个序上取 max(rank)。
ZONE_RANK: dict[str, int] = {z: i for i, z in enumerate(ZONES)}

ZONE_CN: dict[str, str] = {
    "DMR": "DMR",
    "CONFIRMED": "确认",
    "QUALIFIED": "符合",
    "WATCH": "观察",
    "ELIMINATED": "淘汰",
}

#: 天花板层作用的状态白名单。淘汰 / 数据不足 / 低置信 / NONE 不受影响（文档A §4.5）。
CAPPABLE_STATES: frozenset[str] = frozenset({"WATCH", "QUALIFIED", "CONFIRMED"})

#: 弃权降级的下界：在观察止步，绝不因一次 K 线接口抖动把正常币踢出板面（文档A §3.4）。
ABSTAIN_FLOOR = "WATCH"

#: 默认切点（文档A §4.4）。生产从 ``SelectionSettings`` 读，这里只是缺省。
DEFAULT_CUTS: dict[str, F] = {
    "DMR": F(21, 10),
    "CONFIRMED": F(3, 2),
    "QUALIFIED": F(4, 5),
    "WATCH": F(-1, 2),
}

# —— 文档A §5.5 的 8 个新 reason_codes（全部只写不读，前端只渲染）——
RC_ZONE_ON = "MCAP_ZONE_ON"
RC_ZONE_CAP = "MCAP_ZONE_CAP_"  # + <ZONE>
RC_ZONE_ABSTAIN = "MCAP_ZONE_ABSTAIN"
RC_STRUCT_NO_MOM = "MCAP_STRUCT_NO_MOM"
RC_MOM_CONTRADICT = "MCAP_MOM_CONTRADICT"
RC_SS_ALIGNED = "SS_MCAP_ALIGNED"
RC_SS_DIVERGENT = "SS_MCAP_DIVERGENT"
RC_COMBO = "COMBO_"  # + <XYZ>

NEW_REASON_CODES: tuple[str, ...] = (
    RC_ZONE_ON,
    RC_ZONE_CAP,
    RC_ZONE_ABSTAIN,
    RC_STRUCT_NO_MOM,
    RC_MOM_CONTRADICT,
    RC_SS_ALIGNED,
    RC_SS_DIVERGENT,
    RC_COMBO,
)


# ---------------------------------------------------------------------------
# 组合编号 / 方向分 Z（精确有理数，零自由参数）
# ---------------------------------------------------------------------------
def combo_index(g30: str, g2h: str, g6h: str) -> int:
    """1..216。``36·i(30m) + 6·i(2h) + i(6h) + 1``，可反解（断言 A3）。"""
    i = GRADES.index
    return 36 * i(g30) + 6 * i(g2h) + i(g6h) + 1


def index_combo(n: int) -> tuple[str, str, str]:
    n -= 1
    return GRADES[n // 36], GRADES[(n // 6) % 6], GRADES[n % 6]


def z_score(g30: str, g2h: str, g6h: str) -> F:
    """``Z = (3/5)·q(30m) + (9/10)·q(2h) + (3/2)·q(6h)``，精确有理数。"""
    return W["30m"] * Q[g30] + W["2h"] * Q[g2h] + W["6h"] * Q[g6h]


def z10(g30: str, g2h: str, g6h: str) -> int:
    """``Z × 10`` 的整数形（Z 的分母恒整除 10，可无损）。跨模块交叉核对用。"""
    z = z_score(g30, g2h, g6h) * 10
    assert z.denominator == 1, f"Z10 not integral for {g30}{g2h}{g6h}: {z}"
    return int(z)


def z_float(g30: str, g2h: str, g6h: str) -> float:
    return float(z_score(g30, g2h, g6h))


def priority(z: F) -> int:
    a = abs(z)
    for p, edge in PRIORITY_EDGES:
        if a >= edge:
            return p
    return 1


def macro_pattern(g30: str, g2h: str, g6h: str) -> str:
    """宏观模式：以 6h 为基准的三周期族向关系（严格 4 × 54）。"""

    def fam(g: str) -> int:
        return 1 if g in ("A", "B", "C") else -1

    s30, s2, s6 = fam(g30), fam(g2h), fam(g6h)
    if s30 == s2 == s6:
        return "三周期同向"
    if s2 == s6 and s30 != s6:
        return "6h+2h同向、30m逆向（回调/反弹）"
    if s30 == s6 and s2 != s6:
        return "6h与30m同向、2h逆向（中周期分歧）"
    return "6h与30m+2h正面对冲"


# ---------------------------------------------------------------------------
# 切点 → 天花板
# ---------------------------------------------------------------------------
def cuts_from_settings(settings: Any = None) -> dict[str, F]:
    """从 ``SelectionSettings`` 取 4 个切点；缺字段回落 :data:`DEFAULT_CUTS`。

    浮点切点用 ``Fraction(str(x))`` 精确化 —— ``Fraction(2.1)`` 会带上二进制误差，
    ``Fraction("2.1")`` 不会。切点比较必须精确，否则「落在切点上算哪一档」不确定。
    """
    if settings is None:
        return dict(DEFAULT_CUTS)
    out: dict[str, F] = {}
    for zone, attr in (
        ("DMR", "mcap_zone_cut_dmr"),
        ("CONFIRMED", "mcap_zone_cut_confirmed"),
        ("QUALIFIED", "mcap_zone_cut_qualified"),
        ("WATCH", "mcap_zone_cut_watch"),
    ):
        raw = getattr(settings, attr, None)
        if raw is None:
            out[zone] = DEFAULT_CUTS[zone]
            continue
        try:
            out[zone] = F(str(raw))
        except (ValueError, ZeroDivisionError):
            log.warning("bad %s=%r — using default %s", attr, raw, DEFAULT_CUTS[zone])
            out[zone] = DEFAULT_CUTS[zone]
    return out


def ceiling_for_z(z_dir: F, cuts: Optional[dict[str, F]] = None) -> str:
    """``Z_方向 >= 切点`` 自上而下首个命中；其余为淘汰。切点单调 ⇒ 天花板单调。"""
    c = cuts or DEFAULT_CUTS
    if z_dir >= c["DMR"]:
        return "DMR"
    if z_dir >= c["CONFIRMED"]:
        return "CONFIRMED"
    if z_dir >= c["QUALIFIED"]:
        return "QUALIFIED"
    if z_dir >= c["WATCH"]:
        return "WATCH"
    return "ELIMINATED"


def combo_ceiling(
    g30: str,
    g2h: str,
    g6h: str,
    direction: str,
    *,
    cuts: Optional[dict[str, F]] = None,
) -> str:
    """查 216 表：某个组合对某个方向的名义天花板。``direction`` ∈ {up, down}。"""
    z = z_score(g30, g2h, g6h)
    zd = z if direction == "up" else -z
    return ceiling_for_z(zd, cuts)


def build_table(cuts: Optional[dict[str, F]] = None) -> list[dict[str, Any]]:
    """完整 216 行映射表（与 ``scripts/mcap_combo_zones.py --csv`` 同构）。"""
    c = cuts or DEFAULT_CUTS
    rows: list[dict[str, Any]] = []
    for g30, g2h, g6h in product(GRADES, repeat=3):
        z = z_score(g30, g2h, g6h)
        rows.append(
            {
                "no": combo_index(g30, g2h, g6h),
                "g30": g30,
                "g2h": g2h,
                "g6h": g6h,
                "combo": f"{g30}{g2h}{g6h}",
                "z_up": z,
                "z_down": -z,
                "priority": priority(z),
                "trend": (
                    "偏多" if z >= F(3, 10) else ("偏空" if z <= F(-3, 10) else "中性/冲突")
                ),
                "macro": macro_pattern(g30, g2h, g6h),
                "ceiling_up": ceiling_for_z(z, c),
                "ceiling_down": ceiling_for_z(-z, c),
            }
        )
    rows.sort(key=lambda r: r["no"])
    return rows


def census(direction: str = "up", cuts: Optional[dict[str, F]] = None) -> dict[str, int]:
    """逐方向天花板计数。默认切点下为 13 / 22 / 32 / 72 / 77（文档A §4.4）。"""
    key = "ceiling_up" if direction == "up" else "ceiling_down"
    out = {z: 0 for z in ZONES}
    for r in build_table(cuts):
        out[r[key]] += 1
    return out


# ---------------------------------------------------------------------------
# 只降不升的核心算子
# ---------------------------------------------------------------------------
def worse(a: Optional[str], b: Optional[str]) -> str:
    """两个分区里更严的那个（rank 更大）。``None`` 视为不约束。"""
    if a is None:
        return str(b)
    if b is None:
        return str(a)
    return a if ZONE_RANK[a] >= ZONE_RANK[b] else b


def demote_one_level(state: str) -> str:
    """DMR→确认→符合→观察→观察（在观察止步）。淘汰保持淘汰。"""
    if state == "ELIMINATED":
        return "ELIMINATED"
    r = ZONE_RANK.get(state)
    if r is None:
        return state
    return ZONES[min(r + 1, ZONE_RANK[ABSTAIN_FLOOR])]


# ---------------------------------------------------------------------------
# 维度二服务维度三的三个谓词（文档A §5.2）+ SS×组合互印证（文档A §6）
# ---------------------------------------------------------------------------
def apply_dimension_predicates(
    cap: str,
    *,
    z_dir: F,
    mom: float,
    cons: float,
    mom_opposite: float,
    cons_opposite: float,
    cfg: Any,
    cuts: dict[str, F],
) -> tuple[str, list[str]]:
    """P3 > P2 > P1，互斥地作用在同一个 ``cap`` 上，**仍然只降不升**。

    P1 动量确认  M ≥ mom_confirmed ∧ C == 1.0        → cap 取查表值（不动）
    P2 动量不足  Z_d ≥ 确认切点 ∧ M < mom_qualified  → cap 降一级
    P3 动量否决  Z_d ≥ 确认切点 ∧ M_反 ≥ 70 ∧ C_反 == 1.0 → cap = min(cap, WATCH)
    """
    codes: list[str] = []
    cut_conf = cuts["CONFIRMED"]
    mom_conf = float(getattr(cfg, "mom_confirmed", 60))
    mom_qual = float(getattr(cfg, "mom_qualified", 55))
    # P3 —— 结构与动量正面打架，不给任何执行级别的天花板
    if z_dir >= cut_conf and float(mom_opposite) >= 70.0 and float(cons_opposite) >= 1.0:
        codes.append(RC_MOM_CONTRADICT)
        return worse(cap, "WATCH"), codes
    # P2 —— 市值结构排好了但价格动量没跟上，不给名义天花板
    if z_dir >= cut_conf and float(mom) < mom_qual:
        codes.append(RC_STRUCT_NO_MOM)
        return demote_one_level(cap), codes
    # P1 —— 动量确认：天花板取名义查表值，不做任何加成（只降不升的直接推论）
    if float(mom) >= mom_conf and float(cons) >= 1.0:
        return cap, codes
    return cap, codes


def apply_ss_crosscheck(
    cap: str,
    *,
    z_dir: F,
    ss: float,
    cfg: Any,
    cuts: dict[str, F],
) -> tuple[str, list[str]]:
    """互印证不加分；背离压到符合区（文档A §6.2 / §6.3）。"""
    codes: list[str] = []
    cut_qual = cuts["QUALIFIED"]
    ss_conf = float(getattr(cfg, "ss_confirmed", 50))
    dmr_ss = float(getattr(cfg, "dmr_ss", 55))
    if float(ss) >= ss_conf and z_dir >= cut_qual:
        codes.append(RC_SS_ALIGNED)  # 一致不加分，只留审计痕迹
    if float(ss) >= dmr_ss and z_dir <= -cut_qual:
        codes.append(RC_SS_DIVERGENT)
        return worse(cap, "QUALIFIED"), codes
    return cap, codes


# ---------------------------------------------------------------------------
# 行级入口
# ---------------------------------------------------------------------------
def resolve_row_side(
    row: dict[str, Any],
    direction: str,
    state: str,
    *,
    settings: Any = None,
    cfg: Any = None,
    cuts: Optional[dict[str, F]] = None,
) -> dict[str, Any]:
    """算一侧的天花板。**纯函数**：不写 row，返回一份结果字典。

    返回 ``{combo_code, z_score, combo_zone, zone_ceiling, final_state, reason_codes}``：

    * ``combo_zone``   —— 216 表查出的名义天花板（不含谓词调整）
    * ``zone_ceiling`` —— 实际生效的天花板（含弃权 / P1–P3 / SS 背离调整后）
    * ``final_state``  —— ``min(状态机分区, zone_ceiling)``，恒 ≥ 入参 state 的严格度
    """
    c = cuts if cuts is not None else cuts_from_settings(settings)
    g30 = row.get("mcap_grade_30m")
    g2h = row.get("mcap_grade_2h")
    g6h = row.get("mcap_grade_6h")
    graded = all(isinstance(g, str) and g in GRADES for g in (g30, g2h, g6h))
    codes: list[str] = []
    out: dict[str, Any] = {
        "combo_code": None,
        "z_score": None,
        "combo_zone": None,
        "zone_ceiling": None,
        "final_state": state,
        "reason_codes": codes,
    }

    # 淘汰 / 数据不足 / 低置信 / NONE 不受天花板影响（天花板只降不升，它们已在底）
    if state not in CAPPABLE_STATES:
        if graded:
            out["combo_code"] = f"{g30}{g2h}{g6h}"
            out["z_score"] = z_float(g30, g2h, g6h) * (1 if direction == "up" else -1)
            codes.append(f"{RC_COMBO}{g30}{g2h}{g6h}")
        return out

    codes.append(RC_ZONE_ON)

    if not graded:
        # §3.4 组合层弃权：不知道结构 → 不给加成，也不凭空判死；降一级、观察止步。
        codes.append(RC_ZONE_ABSTAIN)
        demote = True
        if settings is not None:
            demote = bool(getattr(settings, "mcap_zone_abstain_demote", True))
        if demote:
            capped = demote_one_level(state)
            out["zone_ceiling"] = capped
            out["final_state"] = worse(state, capped)
        return out

    combo = f"{g30}{g2h}{g6h}"
    z = z_score(g30, g2h, g6h)
    z_dir = z if direction == "up" else -z
    codes.append(f"{RC_COMBO}{combo}")
    out["combo_code"] = combo
    out["z_score"] = float(z_dir)

    nominal = ceiling_for_z(z_dir, c)
    out["combo_zone"] = nominal

    cap = nominal
    if cfg is not None:
        key_mom = "momentum_score_up" if direction == "up" else "momentum_score_down"
        key_cons = "consistency_up" if direction == "up" else "consistency_down"
        key_mom_o = "momentum_score_down" if direction == "up" else "momentum_score_up"
        key_cons_o = "consistency_down" if direction == "up" else "consistency_up"
        key_ss = "ss_up" if direction == "up" else "ss_down"
        cap, pcodes = apply_dimension_predicates(
            cap,
            z_dir=z_dir,
            mom=float(row.get(key_mom) or 50),
            cons=float(row.get(key_cons) or 0),
            mom_opposite=float(row.get(key_mom_o) or 50),
            cons_opposite=float(row.get(key_cons_o) or 0),
            cfg=cfg,
            cuts=c,
        )
        codes.extend(pcodes)
        cap, scodes = apply_ss_crosscheck(
            cap, z_dir=z_dir, ss=float(row.get(key_ss) or 0), cfg=cfg, cuts=c
        )
        codes.extend(scodes)

    out["zone_ceiling"] = cap
    final = worse(state, cap)
    out["final_state"] = final
    if ZONE_RANK[final] > ZONE_RANK[state]:
        codes.append(f"{RC_ZONE_CAP}{final}")
    return out


def apply_mcap_ceiling(
    rows: list[dict[str, Any]],
    *,
    mode: str = "off",
    settings: Any = None,
    cfg: Any = None,
) -> dict[str, Any]:
    """给整块板面打天花板。``mode="off"`` 时**一个字段都不写**（恒等映射）。

    写回 row 的字段（逐方向）::

        combo_code                 三字母组合，判不出级为 None（方向无关，写一次）
        z_score_<dir>              方向分 Z_方向 = ±Z
        combo_zone_<dir>           216 表查出的名义天花板
        zone_ceiling_<dir>         实际生效天花板（含弃权 / P1–P3 / SS 背离）
        product_zone_y_<dir>       min(状态机分区, zone_ceiling)
        mcap_zone_codes_<dir>      本侧新增的 reason_codes

    ``mode="on"`` 时额外把 ``state_<dir>`` 覆写为 ``product_zone_y_<dir>``；
    ``mode="shadow"`` 时**绝不**动 ``state_<dir>``（测试 T14 逐节点比对）。
    """
    meta: dict[str, Any] = {
        "mode": "off",
        "capped": 0,
        "abstained": 0,
        "graded": 0,
        "ungraded": 0,
        "cap_by_zone": {},
        "cuts": {},
    }
    if mode not in ("shadow", "on"):
        return meta

    cuts = cuts_from_settings(settings)
    meta["mode"] = mode
    meta["cuts"] = {k: float(v) for k, v in cuts.items()}
    cap_by_zone: dict[str, int] = {}

    for r in rows:
        g30 = r.get("mcap_grade_30m")
        g2h = r.get("mcap_grade_2h")
        g6h = r.get("mcap_grade_6h")
        if all(isinstance(g, str) and g in GRADES for g in (g30, g2h, g6h)):
            meta["graded"] += 1
            r["combo_code"] = f"{g30}{g2h}{g6h}"
        else:
            meta["ungraded"] += 1
            r["combo_code"] = None
        for direction in ("up", "down"):
            state_key = f"state_{direction}"
            state = str(r.get(state_key) or "NONE")
            res = resolve_row_side(
                r, direction, state, settings=settings, cfg=cfg, cuts=cuts
            )
            r[f"z_score_{direction}"] = res["z_score"]
            r[f"combo_zone_{direction}"] = res["combo_zone"]
            r[f"zone_ceiling_{direction}"] = res["zone_ceiling"]
            r[f"product_zone_y_{direction}"] = res["final_state"]
            r[f"mcap_zone_codes_{direction}"] = list(res["reason_codes"])
            if RC_ZONE_ABSTAIN in res["reason_codes"]:
                meta["abstained"] += 1
            if res["final_state"] != state:
                meta["capped"] += 1
                cap_by_zone[res["final_state"]] = cap_by_zone.get(res["final_state"], 0) + 1
            if mode == "on":
                r[state_key] = res["final_state"]

    meta["cap_by_zone"] = cap_by_zone
    return meta


def meta_block(mode: str, settings: Any = None) -> dict[str, Any]:
    """写进快照 ``meta.mcap_zone`` 的声明块（文档B §3.1 表）。"""
    cuts = cuts_from_settings(settings)
    return {
        "mode": mode,
        "enabled": mode == "on",
        "cuts": {
            "dmr": float(cuts["DMR"]),
            "confirmed": float(cuts["CONFIRMED"]),
            "qualified": float(cuts["QUALIFIED"]),
            "watch": float(cuts["WATCH"]),
        },
        "abstain_demote": bool(getattr(settings, "mcap_zone_abstain_demote", True)),
        "ladder_version": "q-equal-step-v1",
        "tf_weights_version": "w-5-3-2-v1",
        "generator": "scripts/mcap_combo_zones.py",
        "census_per_side": census("up", cuts),
    }


def run_assertions(cuts: Optional[dict[str, F]] = None) -> list[str]:
    """文档A §4.1 的 9 条恒等式，在代码侧再跑一遍（测试 T9 调用）。"""
    c = cuts or DEFAULT_CUTS
    rows = build_table(c)
    ok: list[str] = []

    assert len(rows) == 216
    ok.append("A1 总数 == 216")

    assert len({r["combo"] for r in rows}) == 216
    ok.append("A2 216 个组合互异（互斥穷尽）")

    for r in rows:
        assert index_combo(r["no"]) == (r["g30"], r["g2h"], r["g6h"])
    assert sorted(r["no"] for r in rows) == list(range(1, 217))
    ok.append("A3 编号 1..216 双射且可反解")

    for r in rows:
        assert r["z_up"] + r["z_down"] == 0
    ok.append("A4 Z_up + Z_down == 0（多空严格反对称）")

    idx = {r["combo"]: r for r in rows}
    for r in rows:
        m = MIRROR[r["g30"]] + MIRROR[r["g2h"]] + MIRROR[r["g6h"]]
        assert idx[m]["z_up"] == r["z_down"]
        assert idx[m]["ceiling_up"] == r["ceiling_down"]
    ok.append("A5 镜像不变式：Z_up(mirror(c)) == Z_down(c) 且天花板同构")

    srt = sorted(rows, key=lambda r: r["z_up"])
    ranks = [ZONE_RANK[r["ceiling_up"]] for r in srt]
    assert all(a >= b for a, b in zip(ranks, ranks[1:]))
    ok.append("A6 天花板随 Z 单调（Z 越高天花板越松，不存在倒挂）")

    for pos in ("g30", "g2h", "g6h"):
        counts = {g: sum(1 for r in rows if r[pos] == g) for g in GRADES}
        assert set(counts.values()) == {36}, counts
    ok.append("A7 每个周期上 A–F 各 36 次（笛卡尔积完整）")

    macro: dict[str, int] = {}
    for r in rows:
        macro[r["macro"]] = macro.get(r["macro"], 0) + 1
    assert len(macro) == 4 and set(macro.values()) == {54}, macro
    ok.append("A8 宏观模式 4 类 × 54 == 216")

    for d in ("up", "down"):
        cen = census(d, c)
        assert sum(cen.values()) == 216, cen
        assert all(v >= 0 for v in cen.values())
    ok.append("A9 每个方向的天花板覆盖全部 216 组合（无未定义格）")
    return ok


def iter_combos() -> Iterable[tuple[str, str, str]]:
    return product(GRADES, repeat=3)
