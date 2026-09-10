"""有效区（effective_zone）规则内核 —— 《选币榜Y》与《复盘选币》的**单一事实来源**。

权威依据
--------
* 文档 A：``docs/ChatGpt_SOL5.6_新五区币种选入标准报告_升级版_v2.0.0.md``
  §5（业务五区与 rank 定义）、§9.5（null 与恢复）、§10.1（共同裁决顺序）、
  §10.2（分区规则表）、§13.2（每行血缘字段）、§14（可转代码伪代码）。
* 文档 B：``docs/ChatGpt_SOL5.6_《选币榜Y》与《复盘选币》……整体设计流程方案计划.md``
  §6（在线规则计算流程 6–9 步）、§8.1（在线/离线逐字段一致性清单）。

为什么必须只有这一份实现
------------------------
文档 B §3.1 的一致性等式要求「同一 input + 同一身份 ⇒ 同一 effective_zone / DMR」。
在线（``board_projection.project_board``）与离线（``review_replay`` / 规则重放）
**必须调用本模块的同一个纯函数**；任何一侧另写一遍合并逻辑都会形成口径漂移，
这正是文档 B §2 列为首要风险的「两处逻辑重复实现」。

三条不可违背的不变量（单测穷举钉住，见 tests/test_mcap_effective.py）
--------------------------------------------------------------------
1. **只降不升**：``effective_rank = max(base_rank, ceiling_rank)``，因此对四个业务
   ``base_state`` 恒有 ``rank(effective) >= rank(base)``（文档 A 附录 B）。
2. **特殊态旁路**：``NONE`` / ``DATA_INSUFFICIENT`` / ``LOW_CONFIDENCE`` 原样返回，
   不进入 max、不因 A～F 缺失的 WATCH fallback 获得展示资格（文档 A §5、§9.5）。
3. **DMR 是派生集合**：``DMR`` 只能作为 ceiling 出现，绝不是 ``base_state`` 输入，
   也不会成为 ``effective_zone`` 的取值（文档 A §5、§10.2）。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from .mcap_combo import STRUCT  # noqa: F401  (保留符号，便于调用方做结构轴解释)
from .mcap_mapping import (
    GRADES,
    MCAP_MAPPING_VERSION,
    McapMapping,
    ZONE_CN,
    ZONE_EN,
    combo_no as combo_no_of,
)

log = logging.getLogger("coin_selection.mcap_effective")

# ---------------------------------------------------------------------------
# 文档 A §5：业务等级固定为 DMR=0、确定=1、符合=2、观察=3、淘汰=4
# ---------------------------------------------------------------------------
ZONES_EN: tuple[str, ...] = ("DMR", "CONFIRMED", "QUALIFIED", "WATCH", "ELIMINATED")
ZONE_RANK: dict[str, int] = {z: i for i, z in enumerate(ZONES_EN)}
ZONE_BY_RANK: dict[int, str] = {i: z for i, z in enumerate(ZONES_EN)}

#: 文档 A §5：只有这四个业务 base_state 参与 max 合并；DMR 不是 base 输入。
BUSINESS_BASE_STATES: frozenset[str] = frozenset(
    {"CONFIRMED", "QUALIFIED", "WATCH", "ELIMINATED"}
)
BASE_RANK: dict[str, int] = {s: ZONE_RANK[s] for s in BUSINESS_BASE_STATES}

#: 文档 A §5 / §9.5：技术隔离态与未进入态必须旁路，不映射成业务五区。
BYPASS_STATES: frozenset[str] = frozenset(
    {"NONE", "DATA_INSUFFICIENT", "LOW_CONFIDENCE"}
)

#: 文档 A §9.5：任一等级 null 时保守 fallback ceiling 为 WATCH（观察）。
NULL_FALLBACK_CEILING = "WATCH"

#: 文档 A §13.2：combo_status 枚举。
COMBO_COMPLETE = "COMPLETE"
COMBO_INCOMPLETE = "INCOMPLETE"
COMBO_NOT_APPLICABLE = "NOT_APPLICABLE"

# —— 有效区层的三态开关（文档 A §17.2 / 文档 B §5.3）——
#: 恒等映射：一个有效区字段都不写，《选币榜Y》与冻结基线逐字段一致。
MODE_OFF = "off"
#: 影子：算并记录 combo / Z10 / K / ceiling / effective，但**不改 state、不改 DMR**。
MODE_SHADOW = "shadow"
#: 生效：ceiling 参与 Y 的展示分区与 DMR 派生（ENABLE_MCAP_EFFECTIVE_ZONE）。
MODE_ON = "on"
MODES: tuple[str, ...] = (MODE_OFF, MODE_SHADOW, MODE_ON)

# —— 文档 A §13.2 的 effective_reason_codes ——
RC_ON = "Y5_MCAP_ON"
RC_BYPASS = "Y5_BYPASS"
RC_COMBO = "Y5_COMBO_"  # + XYZ
RC_INCOMPLETE = "Y5_COMBO_INCOMPLETE"
RC_CEILING = "Y5_CEILING_"  # + EN zone
RC_EFFECTIVE = "Y5_EFFECTIVE_"  # + EN zone（仅在被降级时写）
RC_DMR_OK = "Y5_DMR_CEILING_OK"
RC_DMR_BLOCK = "Y5_DMR_CEILING_BLOCK"

REASON_CODE_PREFIXES: tuple[str, ...] = (
    RC_ON,
    RC_BYPASS,
    RC_COMBO,
    RC_INCOMPLETE,
    RC_CEILING,
    RC_EFFECTIVE,
    RC_DMR_OK,
    RC_DMR_BLOCK,
)

GRADE_KEYS: tuple[str, str, str] = (
    "mcap_grade_30m",
    "mcap_grade_2h",
    "mcap_grade_6h",
)


def normalize_mode(raw: Any) -> str:
    """未知取值一律降级为 ``off``：开关坏掉必须退化为现网行为，不是未定义行为。"""
    m = str(raw or "").strip().lower()
    if m in MODES:
        return m
    if m in ("1", "true", "yes"):
        return MODE_ON
    if m in ("", "0", "false", "no"):
        return MODE_OFF
    log.warning("unknown effective-zone mode %r — treating as off", raw)
    return MODE_OFF


def zone_cn(zone_en: Optional[str]) -> Optional[str]:
    return ZONE_CN.get(zone_en) if zone_en else None


def zone_from_rank(rank: int) -> str:
    return ZONE_BY_RANK[max(0, min(4, int(rank)))]


def merge_rank(base_state: str, ceiling: Optional[str]) -> str:
    """文档 A §5 / §14：``effective = zone_from_rank(max(BASE_RANK, ZONE_RANK))``。

    只允许对四个业务 ``base_state`` 调用；特殊态请先用 :func:`is_bypass` 旁路。
    ``ceiling is None`` 表示映射层未生效（不约束），返回 base 原值。
    """
    if base_state not in BASE_RANK:
        raise ValueError(f"merge_rank called with non-business base_state {base_state!r}")
    if ceiling is None:
        return base_state
    return zone_from_rank(max(BASE_RANK[base_state], ZONE_RANK[ceiling]))


def is_bypass(base_state: Optional[str]) -> bool:
    return str(base_state or "NONE") in BYPASS_STATES


def grades_of(row: dict[str, Any]) -> tuple[Optional[str], Optional[str], Optional[str]]:
    out = []
    for k in GRADE_KEYS:
        g = row.get(k)
        out.append(g if isinstance(g, str) and g in GRADES else None)
    return out[0], out[1], out[2]


#: DMR 前置所要求的方向 ceiling **最低档**。文档 A 自身在这里有矛盾：
#: §5 写「ceiling 允许」（即 ceiling 不低于 DMR 所需即可），§10.2/§14/§19 写
#: 「direction ceiling 必须 **等于** DMR」。ChatGpt_SOL5.6 版 notes #6 已把这条
#: 列为「措辞不一致」。本常量把它变成**可扫的参数**而不是硬编码的裁决：
#:   "DMR"        最严：ceiling 必须恰为 DMR（§10.2 字面）
#:   "CONFIRMED"  放宽一档：ceiling ∈ {DMR, 确定}（§5 的「允许」读法）
#:   "QUALIFIED"  再放宽一档
#: 生效值来自 ``SelectionSettings.mcap_dmr_ceiling_min``（缺省 "DMR"，即保持 §10.2）。
DEFAULT_DMR_CEILING_MIN = "DMR"


def resolve_side(
    row: dict[str, Any],
    direction: str,
    base_state: str,
    *,
    mapping: McapMapping,
    dmr_ceiling_min: str = DEFAULT_DMR_CEILING_MIN,
) -> dict[str, Any]:
    """算一侧的 combo / ceiling / effective。**纯函数**：不写 ``row``。

    严格实现文档 B §6 的第 6～8 步与文档 A §14 的 ``evaluate_y`` 分支顺序：

    1. ``base_state ∈ {NONE, DATA_INSUFFICIENT, LOW_CONFIDENCE}`` → 原样旁路，
       ``combo_status=NOT_APPLICABLE``、``ceiling=None``、``effective=base``；
    2. 三周期 grade 任一为 null → ``combo_no=None``、``combo_status=INCOMPLETE``、
       ``ceiling=WATCH``（文档 A §9.5 的保守 fallback）；
    3. 否则查 216 表拿方向 ceiling；
    4. ``effective = max(base_rank, ceiling_rank)``，只能保持或降级；
    5. ``dmr_ceiling_ok`` 仅当 ``base_state == CONFIRMED`` 且 ``ceiling == DMR``。
    """
    base = str(base_state or "NONE")
    codes: list[str] = []
    out: dict[str, Any] = {
        "base_state": base,
        "mcap_combo_no": None,
        "mcap_combo_code": None,
        "mcap_combo_status": COMBO_NOT_APPLICABLE,
        "mcap_z10": None,
        "mcap_priority": None,
        "mcap_resonance_k": None,
        "mcap_canonical_zone": None,
        "mcap_canonical_pool": None,
        "mcap_ceiling_zone": None,
        "mcap_ceiling_zone_cn": None,
        "effective_zone": base,
        "effective_zone_cn": zone_cn(base),
        "effective_downgraded": False,
        "dmr_ceiling_ok": False,
        "effective_reason_codes": codes,
    }

    g30, g2, g6 = grades_of(row)
    graded = None not in (g30, g2, g6)

    # —— 第 1 步：特殊态旁路（文档 A §5、§9.5、§10.2）——
    #
    # 「不进入业务五区、不进入 DMR、不因优秀 combo 或 null→WATCH fallback 获得展示资格」。
    # 组合信息仍然登记（审计需要），但 ceiling / effective 一律不写。
    if base not in BASE_RANK:
        codes.append(RC_BYPASS)
        if graded:
            r = mapping.row(g30, g2, g6)
            out.update(
                mcap_combo_no=r["combo_no"],
                mcap_combo_code=f"{g30}{g2}{g6}",
                mcap_z10=r["z10"],
                mcap_priority=r["priority"],
                mcap_resonance_k=r["resonance_k"],
                mcap_canonical_zone=ZONE_EN[r["canonical_zone"]],
                mcap_canonical_pool=r["canonical_pool"],
            )
            codes.append(f"{RC_COMBO}{g30}{g2}{g6}")
        return out

    codes.append(RC_ON)

    if graded:
        r = mapping.row(g30, g2, g6)
        ceiling_cn = mapping.ceiling(g30, g2, g6, direction)
        ceiling = ZONE_EN[ceiling_cn]
        out.update(
            mcap_combo_no=r["combo_no"],
            mcap_combo_code=f"{g30}{g2}{g6}",
            mcap_combo_status=COMBO_COMPLETE,
            mcap_z10=r["z10"] if str(direction).lower() in ("up", "long") else -r["z10"],
            mcap_priority=r["priority"],
            mcap_resonance_k=r["resonance_k"],
            mcap_canonical_zone=ZONE_EN[r["canonical_zone"]],
            mcap_canonical_pool=r["canonical_pool"],
        )
        codes.append(f"{RC_COMBO}{g30}{g2}{g6}")
    else:
        # —— 第 2 步：文档 A §9.5 —— 任一等级 null：不入 216、不生成伪第七等级，
        # 保守 fallback ceiling = 观察，并记录缺失周期。
        ceiling = NULL_FALLBACK_CEILING
        out["mcap_combo_status"] = COMBO_INCOMPLETE
        codes.append(RC_INCOMPLETE)
        missing = [
            tf
            for tf, g in zip(("30m", "2h", "6h"), (g30, g2, g6))
            if g is None
        ]
        codes.append("Y5_MISSING_" + "_".join(missing))

    out["mcap_ceiling_zone"] = ceiling
    out["mcap_ceiling_zone_cn"] = zone_cn(ceiling)
    codes.append(f"{RC_CEILING}{ceiling}")

    # —— 第 4 步：只降不升的合并 ——
    effective = merge_rank(base, ceiling)
    out["effective_zone"] = effective
    out["effective_zone_cn"] = zone_cn(effective)
    if effective != base:
        out["effective_downgraded"] = True
        codes.append(f"{RC_EFFECTIVE}{effective}")

    # —— 第 5 步：DMR 前置 ——
    # 判据由 ``dmr_ceiling_min`` 决定（见该常量的注释：文档 A §5 与 §10.2 口径不一）。
    # 缺省 "DMR" ⇒ 与改造前逐字节相同的行为。
    if base == "CONFIRMED":
        floor = ZONE_RANK.get(str(dmr_ceiling_min), ZONE_RANK["DMR"])
        if ZONE_RANK[ceiling] <= floor:
            out["dmr_ceiling_ok"] = True
            codes.append(RC_DMR_OK)
        else:
            codes.append(RC_DMR_BLOCK)
    return out


def row_fields(res: dict[str, Any], direction: str) -> dict[str, Any]:
    """把 :func:`resolve_side` 的结果展开成写进快照行的方向字段（文档 A §13.2）。"""
    d = direction
    return {
        f"mcap_combo_no_{d}": res["mcap_combo_no"],
        f"mcap_combo_status_{d}": res["mcap_combo_status"],
        f"mcap_z10_{d}": res["mcap_z10"],
        f"mcap_priority_{d}": res["mcap_priority"],
        f"mcap_resonance_k_{d}": res["mcap_resonance_k"],
        f"mcap_ceiling_zone_{d}": res["mcap_ceiling_zone"],
        f"effective_zone_{d}": res["effective_zone"],
        f"effective_reason_codes_{d}": list(res["effective_reason_codes"]),
        f"dmr_ceiling_ok_{d}": res["dmr_ceiling_ok"],
    }


def apply_effective_zone(
    rows: list[dict[str, Any]],
    *,
    mode: str,
    mapping: Optional[McapMapping],
    dmr_ceiling_min: str = DEFAULT_DMR_CEILING_MIN,
) -> dict[str, Any]:
    """给整块板面算有效区。``mode="off"`` 时**一个字段都不写**（恒等映射）。

    ``mode="on"`` 时才把 ``state_<dir>`` 覆写为 ``effective_zone_<dir>``；
    ``mode="shadow"`` 绝不动 ``state_<dir>``，也不影响 DMR 成员。

    返回一份可直接写进 ``meta.mcap_effective`` 的统计块（文档 B §8.1 的对账口径）。
    """
    meta: dict[str, Any] = {
        "mode": MODE_OFF,
        "mcap_mapping_version": None,
        "mapping_hash": None,
        "rows": 0,
        "graded": 0,
        "incomplete": 0,
        "bypassed": 0,
        "downgraded": 0,
        "dmr_ceiling_ok": 0,
        "downgrade_by_zone": {},
        "ceiling_by_zone": {},
    }
    m = normalize_mode(mode)
    if m == MODE_OFF or mapping is None:
        return meta

    meta["mode"] = m
    meta["mcap_mapping_version"] = mapping.mapping_version
    meta["mapping_hash"] = mapping.mapping_hash
    dn: dict[str, int] = {}
    cn: dict[str, int] = {}

    for r in rows:
        meta["rows"] += 1
        g30, g2, g6 = grades_of(r)
        if None not in (g30, g2, g6):
            meta["graded"] += 1
            r["mcap_combo_code"] = f"{g30}{g2}{g6}"
            r["mcap_combo_no"] = combo_no_of(g30, g2, g6)
        else:
            r["mcap_combo_code"] = None
            r["mcap_combo_no"] = None
        for direction in ("up", "down"):
            base = str(r.get(f"state_{direction}") or "NONE")
            res = resolve_side(
                r, direction, base, mapping=mapping, dmr_ceiling_min=dmr_ceiling_min
            )
            r.update(row_fields(res, direction))
            if res["mcap_combo_status"] == COMBO_NOT_APPLICABLE:
                meta["bypassed"] += 1
            elif res["mcap_combo_status"] == COMBO_INCOMPLETE:
                meta["incomplete"] += 1
            if res["mcap_ceiling_zone"]:
                cn[res["mcap_ceiling_zone"]] = cn.get(res["mcap_ceiling_zone"], 0) + 1
            if res["effective_downgraded"]:
                meta["downgraded"] += 1
                dn[res["effective_zone"]] = dn.get(res["effective_zone"], 0) + 1
            if res["dmr_ceiling_ok"]:
                meta["dmr_ceiling_ok"] += 1
            if m == MODE_ON and not is_bypass(base):
                # 有效区生效：只覆写展示用 state，**不反写状态机记忆**
                # （文档 A §10.2：「进入有效区」不反写 base 状态）。
                r[f"base_state_{direction}"] = base
                r[f"state_{direction}"] = res["effective_zone"]

    meta["downgrade_by_zone"] = dn
    meta["ceiling_by_zone"] = cn
    return meta


def meta_block(
    mode: str,
    mapping: Optional[McapMapping],
    *,
    dmr_ceiling_min: str = DEFAULT_DMR_CEILING_MIN,
) -> dict[str, Any]:
    """写进快照 ``meta.mcap_effective`` 的声明块（文档 B §8.1 身份层）。"""
    m = normalize_mode(mode)
    return {
        "mode": m,
        "enabled": m == MODE_ON,
        "shadow": m == MODE_SHADOW,
        "mcap_mapping_version": mapping.mapping_version if mapping else None,
        "mapping_hash": mapping.mapping_hash if mapping else None,
        "mapping_source": mapping.source if mapping else None,
        "null_fallback_ceiling": NULL_FALLBACK_CEILING,
        "bypass_states": sorted(BYPASS_STATES),
        "merge": "effective_rank=max(base_rank,ceiling_rank)",
        # 上报**实际生效值**，不是默认值 —— meta 是审计口径，写死默认会说谎。
        "dmr_ceiling_min": str(dmr_ceiling_min or DEFAULT_DMR_CEILING_MIN),
        "ruleset": MCAP_MAPPING_VERSION,
    }


__all__ = [
    "BASE_RANK",
    "BUSINESS_BASE_STATES",
    "BYPASS_STATES",
    "COMBO_COMPLETE",
    "COMBO_INCOMPLETE",
    "COMBO_NOT_APPLICABLE",
    "MODES",
    "MODE_OFF",
    "MODE_ON",
    "MODE_SHADOW",
    "DEFAULT_DMR_CEILING_MIN",
    "NULL_FALLBACK_CEILING",
    "ZONES_EN",
    "ZONE_BY_RANK",
    "ZONE_RANK",
    "apply_effective_zone",
    "grades_of",
    "is_bypass",
    "merge_rank",
    "meta_block",
    "normalize_mode",
    "resolve_side",
    "row_fields",
    "zone_cn",
    "zone_from_rank",
]
