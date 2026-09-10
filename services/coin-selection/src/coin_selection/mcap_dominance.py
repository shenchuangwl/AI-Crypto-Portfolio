"""v2.0.0「流通市值主导」层 —— 三周期 A–F 等级成为准入 / 退出 / 分区 / 排序的第一裁决。

定位
----
`mcap_effective` 负责 216 查表与 ``effective = max(base, ceiling)`` 的**只降不升**合并。
本模块负责其**之后**的三件事，把「天花板」升级为「主导指标」：

1. **维度谓词 P1/P2/P3**（文档 A §5.2）——动能只能**否决或软化**，不能把等级抬上去。
2. **SS × 流通市值互印证**（文档 A §6）——一致不加分，背离封顶。
3. **W_base / W_prio / W_K / W_combo / W_final / RankKey**（文档 A §5.6）——排序第一裁决。

三条不变量（单测钉住）
----------------------
* **INV-1 单调性**：本模块的每一步都只能**保持或降级**，永不升级。
  ``final_rank >= effective_rank >= base_rank``。
* **INV-2 排序上界**：``RankKey <= Score / 100``。文档 §5.6 只给了这句约束却没定义
  ``W_final``；本模块把它定义为 ``W_combo × Π(动态因子)`` 并钳制到
  ``[F_MIN, F_MAX]``，``F_MAX = 1.0 < RANK_NORM = 1.210`` 使该不变量自动成立。
* **INV-3 主导性**：排序元组的**第一个键**是分区（已含天花板合取），第二、三、四个键
  是天花板等级 / 方向 Z / 共振 K。动能 / SS / Score 只能出现在第五位之后。

``W_final`` 的定义（文档缺口，本轮补齐）
----------------------------------------
文档 A §5.6 引入 ``RankKey = (Score/100) × (W_final / 1.210)`` 却从未定义 ``W_final``，
只说「再乘动态因子」并要求「钳制后仍不得把排序键抬到 Score 之上」。本模块按
「流通市值主导、动能辅助」的原则定义：

    W_combo = W_base(effective_zone) × W_prio(priority) × W_K(resonance_k)
    W_final = clamp(W_combo × f_p2 × f_p3 × f_divergent × f_incomplete, F_MIN, F_MAX)

动态因子**全部 <= 1.0**，即动能与 SS 只能**下调**排序权重，不能上调 —— 这正是
「动能不得覆盖、替代或逆转流通市值主导判定」的机器表达。

授权
----
``mcap_zone_mode="on"`` **不足以**启用本层：还必须显式提供与
:data:`AUTHORIZATION_TOKEN` 精确相等的 ``mcap_zone_authorization``。
未授权时 :func:`assert_production_ready` 抛 :class:`McapDominanceError`，
调用方必须**拒绝以 v2.0.0 身份出数**，而不是静默降级（文档 B §8.2 / 本轮裁决三）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Optional

from .mcap_effective import (
    BYPASS_STATES,
    COMBO_COMPLETE,
    COMBO_INCOMPLETE,
    MODE_OFF,
    MODE_ON,
    MODE_SHADOW,
    ZONE_BY_RANK,
    ZONE_RANK,
    ZONES_EN,
    normalize_mode,
    zone_cn,
)

log = logging.getLogger("coin_selection.mcap_dominance")


class McapDominanceError(RuntimeError):
    """主导层不可用。**必须阻断以 v2.0.0 身份出数**，禁止降级为 v1.4.0 行为。"""


#: 显式授权令牌。写进 ``boards[key=y].overrides.settings.mcap_zone_authorization``。
#: 换令牌 = 一次需要人工确认的授权动作，且会改变 ``config_hash``（可审计）。
AUTHORIZATION_TOKEN = "y-v2.0.0-r3-mcap-dominant"

#: 受本层强制约束的参数版本前缀。主榜 v1.4.0 完全不受影响。
GOVERNED_PARAMETER_PREFIX = "param-v2.0.0-screener-y"

#: 文档 A §3.4：降一级链，**在观察止步**（淘汰不再往下）。
_DEMOTE_ONE: dict[str, str] = {
    "DMR": "CONFIRMED",
    "CONFIRMED": "QUALIFIED",
    "QUALIFIED": "WATCH",
    "WATCH": "WATCH",
    "ELIMINATED": "ELIMINATED",
}

#: 归一化分母。文档 A §5.6：``W_combo`` 的理论最大值 1.00×1.10×1.10 = 1.210。
RANK_NORM = 1.210

#: 动态因子**乘积**的钳制区间（文档缺口，本轮定义）。
#:
#: 注意这是对**因子乘积**的钳制，不是对 ``W_final`` 绝对值的钳制。
#: 若钳制绝对值，一个 ``W_combo`` 本就很小的行（如缺等级的观察行，
#: ``W_combo=0.182``）会被下限 0.30 **抬高**，等于动能/缺数把排序权重抬上去了——
#: 与「动态因子只能下调」直接冲突。改为钳制乘积后恒有 ``W_final <= W_combo``。
F_MIN = 0.30   # 因子乘积下限：动态因子最多砍掉 70% 的排序权重
F_MAX = 1.00   # 因子乘积上限：恒为 1，即**永远不能加分**

# —— 排序解释用的原因码 ——
RC_P1 = "Y5_P1_CONFIRM"
RC_P2 = "Y5_P2_SOFTEN"
RC_P3 = "Y5_P3_VETO"
RC_XC_ALIGNED = "Y5_XC_ALIGNED"
RC_XC_DIVERGENT = "Y5_XC_DIVERGENT"
RC_DOMINANT = "Y5_MCAP_DOMINANT"


@dataclass(frozen=True)
class DominanceConfig:
    """本层全部可调参数。**唯一机器入口**是 ``boards[key=y].overrides.settings``。

    所有阈值都带默认值；:meth:`validate` 会拒绝越界值（不静默夹紧 —— 夹紧会让
    「指纹记 A、实际用 B」的分叉重现，见审查报告 §7-P9）。
    """

    # —— 维度谓词 P1/P2/P3（文档 A §5.2；Z 单位，Z = Z10/10 ∈ [-3,+3]）——
    p1_mom_gte: float = 60.0
    p1_cons_eq: float = 1.0
    p2_z_gte: float = 1.5
    p2_mom_lt: float = 55.0
    p3_z_gte: float = 1.5
    p3_mom_opposite_gte: float = 70.0
    p3_cons_opposite_eq: float = 1.0

    # —— SS × 流通市值互印证（文档 A §6）——
    xc_aligned_ss_gte: float = 50.0
    xc_aligned_z_gte: float = 0.8
    xc_divergent_ss_gte: float = 55.0
    xc_divergent_z_lte: float = -0.8

    # —— 排序权重体系（文档 A §5.6）——
    w_base: Mapping[str, float] = field(
        default_factory=lambda: {
            "DMR": 1.00,
            "CONFIRMED": 0.85,
            "QUALIFIED": 0.70,
            "WATCH": 0.40,
            "ELIMINATED": 0.00,
        }
    )
    w_prio_base: float = 0.60
    w_prio_step: float = 0.10
    w_k: Mapping[int, float] = field(
        default_factory=lambda: {100: 1.10, 70: 1.00, 40: 0.85, 10: 0.65}
    )

    # —— W_final 动态因子（文档缺口，本轮定义；全部 <= 1.0，只降不升）——
    f_p2_soften: float = 0.85
    f_p3_veto: float = 0.50
    f_divergent: float = 0.80
    f_incomplete: float = 0.70

    #: DMR 区要求的**最差**方向天花板（文档矛盾点，本轮参数化）。
    #:
    #: 文档A §10.2/§14/§19 要求「direction ceiling 必须严格等于 DMR」，
    #: 而 §5 只写「ceiling 允许」—— ChatGpt_SOL5.6 自己的 notes #6 点名了这处不一致。
    #: 严格口径下每侧只有 12/216 个组合能给 DMR 天花板，实测样本掉 62%。
    #: 本字段把它变成可回测的自由度：``"DMR"`` = 严格（文档默认）、
    #: ``"CONFIRMED"`` = 允许确定级天花板（每侧 12+23=35/216）、以此类推。
    #: 放宽只影响 **DMR 精选**，不影响任何一区的准入 —— 分区仍由 max() 合并决定。
    dmr_ceiling_min: str = "DMR"

    #: DMR 精选的方向 Z10 **带通**区间（本轮实证新增，文档无此概念）。
    #:
    #: 文档A 假设「市值排列越强越好」，天花板阈值 18/10/5 是单调高通。
    #: 但在 16.8 天窗口上按 Z10 分档回测，结果是**非单调**的：
    #:     Z10>=18   n=1073  胜率37.00%  盈亏比1.519  盈利率-0.66%  PF 0.910
    #:     Z10 10~17 n= 282  胜率40.07%  盈亏比1.924  盈利率+0.57%  PF 1.286  ← 最优
    #:     Z10 5~9   n= 204  胜率36.76%  盈亏比0.815  盈利率-0.72%  PF 0.481
    #: 即「极端排列」（趋势末段）反而更差。本字段把高通改成带通，
    #: ``dmr_z10_max=None`` 时退化为文档口径（无上界）。
    #: **【建议参数·样本内】** 仅 16.8 天、n=282，未经样本外验证，
    #: 不得据此宣称达标（文档B §6.3 明令禁止挑段）。
    dmr_z10_min: Optional[int] = None
    dmr_z10_max: Optional[int] = None

    def validate(self) -> None:
        """越界即抛。参数错配必须显式失败，不得夹紧后继续跑。"""
        for name, lo, hi in (
            ("p1_mom_gte", 0.0, 100.0),
            ("p2_mom_lt", 0.0, 100.0),
            ("p3_mom_opposite_gte", 0.0, 100.0),
            ("xc_aligned_ss_gte", 0.0, 100.0),
            ("xc_divergent_ss_gte", 0.0, 100.0),
        ):
            v = float(getattr(self, name))
            if not lo <= v <= hi:
                raise McapDominanceError(f"{name}={v} 越界 [{lo},{hi}]")
        for name in ("p2_z_gte", "p3_z_gte", "xc_aligned_z_gte", "xc_divergent_z_lte"):
            v = float(getattr(self, name))
            if not -3.0 <= v <= 3.0:
                raise McapDominanceError(f"{name}={v} 越界 [-3,3]（Z 值域）")
        for name in ("f_p2_soften", "f_p3_veto", "f_divergent", "f_incomplete"):
            v = float(getattr(self, name))
            if not 0.0 < v <= 1.0:
                raise McapDominanceError(
                    f"{name}={v} 必须落在 (0,1] —— 动态因子只能下调排序权重"
                )
        missing = set(ZONES_EN) - set(self.w_base)
        if missing:
            raise McapDominanceError(f"w_base 缺少分区: {sorted(missing)}")
        if set(self.w_k) != {100, 70, 40, 10}:
            raise McapDominanceError(f"w_k 键必须恰为 {{100,70,40,10}}，实为 {sorted(self.w_k)}")
        if abs(float(self.w_base["ELIMINATED"])) > 1e-9:
            raise McapDominanceError("w_base[ELIMINATED] 必须为 0（文档 A §5.6 硬性）")
        for name in ("dmr_z10_min", "dmr_z10_max"):
            v = getattr(self, name)
            if v is not None and not -30 <= int(v) <= 30:
                raise McapDominanceError(f"{name}={v} 越界 [-30,30]（Z10 值域）")
        if (
            self.dmr_z10_min is not None
            and self.dmr_z10_max is not None
            and int(self.dmr_z10_min) > int(self.dmr_z10_max)
        ):
            raise McapDominanceError(
                f"dmr_z10_min={self.dmr_z10_min} > dmr_z10_max={self.dmr_z10_max}"
            )
        if self.dmr_ceiling_min not in ZONE_RANK:
            raise McapDominanceError(
                f"dmr_ceiling_min={self.dmr_ceiling_min!r} 必须是五区之一 {ZONES_EN}"
            )

    # —— 权重查表 ——
    def base_weight(self, zone: str) -> float:
        return float(self.w_base.get(zone, 0.0))

    def prio_weight(self, priority: Optional[int]) -> float:
        if priority is None:
            return self.w_prio_base + self.w_prio_step  # 缺失按优先级 1 处理
        return self.w_prio_base + self.w_prio_step * float(priority)

    def k_weight(self, k: Optional[int]) -> float:
        if k is None:
            return float(min(self.w_k.values()))
        return float(self.w_k.get(int(k), min(self.w_k.values())))

    def as_payload(self) -> dict[str, Any]:
        """进 ``config_hash`` 与快照 meta 的规范化表示（键排序、float 定长）。"""
        return {
            "schema": "mcap-dominance-v1",
            "p1": {"mom_gte": self.p1_mom_gte, "cons_eq": self.p1_cons_eq},
            "p2": {"z_gte": self.p2_z_gte, "mom_lt": self.p2_mom_lt},
            "p3": {
                "z_gte": self.p3_z_gte,
                "mom_opposite_gte": self.p3_mom_opposite_gte,
                "cons_opposite_eq": self.p3_cons_opposite_eq,
            },
            # 注意：``dmr_ceiling_min`` **不在这里**。
            # 它已由 board_variants._dominance_fp 写在 mcap_zone 顶层，
            # 那是先落地、且已随线上 pf1_fcea251fa94122fe 生效的权威位置。
            # 在这里再写一份 = 同一个值进指纹两次 = 指纹变号 = 追溯改写
            # 正在跑的那一段的身份（C14 会立刻红）。
            #
            # 带通未设置时**整键省略**：as_payload() 进 config_hash 与 param_hash，
            # 无条件加键会把已经生效的 pf1_fcea251fa94122fe 变号，等于追溯改写
            # 正在跑的那一段的身份（C14 会立刻红）。与 mcap_dominance 段本身
            # 在 mode="off" 时省略是同一条规则。
            **(
                {"dmr_z10_band": [self.dmr_z10_min, self.dmr_z10_max]}
                if (self.dmr_z10_min is not None or self.dmr_z10_max is not None)
                else {}
            ),
            "crosscheck": {
                "aligned_ss_gte": self.xc_aligned_ss_gte,
                "aligned_z_gte": self.xc_aligned_z_gte,
                "divergent_ss_gte": self.xc_divergent_ss_gte,
                "divergent_z_lte": self.xc_divergent_z_lte,
            },
            "rank": {
                "w_base": {k: float(self.w_base[k]) for k in ZONES_EN},
                "w_prio_base": self.w_prio_base,
                "w_prio_step": self.w_prio_step,
                "w_k": {str(k): float(self.w_k[k]) for k in sorted(self.w_k, reverse=True)},
                "norm": RANK_NORM,
                "f_min": F_MIN,
                "f_max": F_MAX,
                "factors": {
                    "p2_soften": self.f_p2_soften,
                    "p3_veto": self.f_p3_veto,
                    "divergent": self.f_divergent,
                    "incomplete": self.f_incomplete,
                },
            },
        }


#: ``overrides.settings`` 里可覆写的标量键 → :class:`DominanceConfig` 字段。
SETTINGS_FIELD_MAP: dict[str, str] = {
    "mcap_p1_mom_gte": "p1_mom_gte",
    "mcap_p1_cons_eq": "p1_cons_eq",
    "mcap_p2_z_gte": "p2_z_gte",
    "mcap_p2_mom_lt": "p2_mom_lt",
    "mcap_p3_z_gte": "p3_z_gte",
    "mcap_p3_mom_opposite_gte": "p3_mom_opposite_gte",
    "mcap_p3_cons_opposite_eq": "p3_cons_opposite_eq",
    "mcap_xc_aligned_ss_gte": "xc_aligned_ss_gte",
    "mcap_xc_aligned_z_gte": "xc_aligned_z_gte",
    "mcap_xc_divergent_ss_gte": "xc_divergent_ss_gte",
    "mcap_xc_divergent_z_lte": "xc_divergent_z_lte",
    "mcap_w_prio_base": "w_prio_base",
    "mcap_w_prio_step": "w_prio_step",
    "mcap_f_p2_soften": "f_p2_soften",
    "mcap_f_p3_veto": "f_p3_veto",
    "mcap_f_divergent": "f_divergent",
    "mcap_f_incomplete": "f_incomplete",
}


def config_from_settings(settings: Any) -> DominanceConfig:
    """从 ``SelectionSettings`` 解析本层配置并**立即校验**。"""
    kw: dict[str, Any] = {}
    for skey, fname in SETTINGS_FIELD_MAP.items():
        v = getattr(settings, skey, None)
        if v is not None:
            kw[fname] = float(v)
    cfg = DominanceConfig(**kw) if kw else DominanceConfig()
    for skey, fname in (("mcap_dmr_z10_min", "dmr_z10_min"),
                        ("mcap_dmr_z10_max", "dmr_z10_max")):
        v = getattr(settings, skey, None)
        if v is not None:
            cfg = replace(cfg, **{fname: int(v)})
    dcm = getattr(settings, "mcap_dmr_ceiling_min", None)
    if isinstance(dcm, str) and dcm:
        cfg = replace(cfg, dmr_ceiling_min=dcm.strip().upper())
    wb = getattr(settings, "mcap_w_base", None)
    wk = getattr(settings, "mcap_w_k", None)
    if isinstance(wb, Mapping) and wb:
        cfg = replace(cfg, w_base={str(k): float(v) for k, v in wb.items()})
    if isinstance(wk, Mapping) and wk:
        cfg = replace(cfg, w_k={int(k): float(v) for k, v in wk.items()})
    cfg.validate()
    return cfg


# ---------------------------------------------------------------------------
# 授权与生产可达性守卫
# ---------------------------------------------------------------------------
def is_governed(parameter_version: Optional[str]) -> bool:
    """该参数版本是否受本层强制约束（只有 v2.0.0 选币榜Y 受约束）。"""
    return str(parameter_version or "").startswith(GOVERNED_PARAMETER_PREFIX)


def authorization_ok(settings: Any) -> bool:
    token = getattr(settings, "mcap_zone_authorization", None)
    return isinstance(token, str) and token == AUTHORIZATION_TOKEN


def assert_production_ready(
    settings: Any,
    *,
    mode: str,
    mapping: Any,
    parameter_version: Optional[str],
) -> DominanceConfig:
    """生产入口守卫。**不满足即抛**，调用方必须拒绝出数而不是降级。

    检查四件事：
      1. 受约束版本必须 ``mcap_zone_mode == "on"``（``off`` / ``shadow`` 一律拒绝）；
      2. 必须持有精确匹配的授权令牌；
      3. 216 映射必须已加载且不变量成立（``mapping`` 非空）；
      4. 本层配置必须通过 :meth:`DominanceConfig.validate`。
    """
    if not is_governed(parameter_version):
        raise McapDominanceError(
            f"parameter_version={parameter_version!r} 不受主导层约束，不应调用本守卫"
        )
    m = normalize_mode(mode)
    if m != MODE_ON:
        raise McapDominanceError(
            f"mcap_zone_mode={m!r}：v2.0.0 的核心规则（三周期流通市值主导）未生效。"
            f"拒绝以 {parameter_version} 身份出数。"
            f"启用方式：boards[key=y].overrides.settings.mcap_zone_mode='on' "
            f"且 mcap_zone_authorization='{AUTHORIZATION_TOKEN}'"
        )
    if not authorization_ok(settings):
        got = getattr(settings, "mcap_zone_authorization", None)
        raise McapDominanceError(
            f"216 主导层未获显式授权：mcap_zone_authorization={got!r} "
            f"!= {AUTHORIZATION_TOKEN!r}。拒绝以 {parameter_version} 身份出数。"
        )
    if mapping is None:
        raise McapDominanceError(
            "216 映射未加载（load(strict=True) 失败或 mapping_hash 不符）。"
            "拒绝以半表 / 无表状态出数。"
        )
    return config_from_settings(settings)


# ---------------------------------------------------------------------------
# 分区代数（全部只降不升）
# ---------------------------------------------------------------------------
def cap_at(zone: str, cap: str) -> str:
    """把 ``zone`` 封顶到不优于 ``cap``。等价 ``max(rank)``，只降不升。"""
    return ZONE_BY_RANK[max(ZONE_RANK[zone], ZONE_RANK[cap])]


def demote_one(zone: str) -> str:
    """降一级，在观察止步（文档 A §3.4）。``ELIMINATED`` 输入原样返回。"""
    return _DEMOTE_ONE[zone]


def _z_of(row: Mapping[str, Any], direction: str) -> Optional[float]:
    """方向 Z（= Z10/10）。``mcap_effective`` 已按方向取过号。"""
    z10 = row.get(f"mcap_z10_{direction}")
    return None if z10 is None else float(z10) / 10.0


def _opposite(direction: str) -> str:
    return "down" if direction == "up" else "up"


def _f(row: Mapping[str, Any], key: str, default: float = 0.0) -> float:
    v = row.get(key)
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _cons_of(row: Mapping[str, Any], direction: str) -> float:
    return _f(row, f"consistency_{direction}", 0.0)


def _mom_of(row: Mapping[str, Any], direction: str) -> float:
    return _f(row, f"momentum_score_{direction}", 50.0)


def _ss_of(row: Mapping[str, Any], direction: str) -> float:
    return _f(row, f"ss_{direction}", 0.0)


# ---------------------------------------------------------------------------
# 主入口：谓词 + 互印证 + RankKey
# ---------------------------------------------------------------------------
def resolve_dominance(
    row: Mapping[str, Any],
    direction: str,
    *,
    cfg: DominanceConfig,
) -> dict[str, Any]:
    """在 ``mcap_effective`` 已写好方向字段的行上，算最终分区与排序键。**纯函数**。

    要求 ``row`` 已含 ``effective_zone_{d}`` / ``mcap_ceiling_zone_{d}`` /
    ``mcap_z10_{d}`` / ``mcap_priority_{d}`` / ``mcap_resonance_k_{d}``
    （由 :func:`mcap_effective.apply_effective_zone` 写入）。
    """
    d = direction
    codes: list[str] = []
    base = str(row.get(f"base_state_{d}") or row.get(f"state_{d}") or "NONE")
    eff_in = str(row.get(f"effective_zone_{d}") or base)

    out: dict[str, Any] = {
        "final_zone": eff_in,
        "final_zone_cn": zone_cn(eff_in),
        "predicate": None,
        "crosscheck": None,
        "w_base": 0.0,
        "w_prio": 0.0,
        "w_k": 0.0,
        "w_combo": 0.0,
        "w_final": 0.0,
        "rank_key": 0.0,
        "dominance_reason_codes": codes,
    }

    # 特殊态原样旁路：不进五区、不参与排序权重（文档 A §5）。
    if base in BYPASS_STATES:
        return out

    codes.append(RC_DOMINANT)
    zone = eff_in
    factors = 1.0

    z = _z_of(row, d)
    mom = _mom_of(row, d)
    cons = _cons_of(row, d)
    mom_opp = _mom_of(row, _opposite(d))
    cons_opp = _cons_of(row, _opposite(d))
    ss = _ss_of(row, d)
    status = str(row.get(f"mcap_combo_status_{d}") or "")

    # —— 维度谓词，优先级 P3 > P2 > P1（文档 A §5.2 末）——
    if z is not None:
        if (
            z >= cfg.p3_z_gte
            and mom_opp >= cfg.p3_mom_opposite_gte
            and abs(cons_opp - cfg.p3_cons_opposite_eq) <= 1e-9
        ):
            # P3 否决：市值方向强，但**反方向**动能与一致性同时爆表 → 封到观察。
            zone = cap_at(zone, "WATCH")
            factors *= cfg.f_p3_veto
            out["predicate"] = "P3_VETO"
            codes.append(RC_P3)
        elif z >= cfg.p2_z_gte and mom < cfg.p2_mom_lt:
            # P2 软化：市值方向强但本方向动能跟不上 → 降一级。
            zone = demote_one(zone)
            factors *= cfg.f_p2_soften
            out["predicate"] = "P2_SOFTEN"
            codes.append(RC_P2)
        elif mom >= cfg.p1_mom_gte and abs(cons - cfg.p1_cons_eq) <= 1e-9:
            # P1 确认：动能与一致性达标 → **取查表值，不加分**（辅助定位）。
            out["predicate"] = "P1_CONFIRM"
            codes.append(RC_P1)

    # —— SS × 流通市值互印证（文档 A §6）——
    if z is not None:
        if ss >= cfg.xc_divergent_ss_gte and z <= cfg.xc_divergent_z_lte:
            # 背离：楼梯很强但市值方向相反 → 封到符合，且排序打折。
            zone = cap_at(zone, "QUALIFIED")
            factors *= cfg.f_divergent
            out["crosscheck"] = "DIVERGENT"
            codes.append(RC_XC_DIVERGENT)
        elif ss >= cfg.xc_aligned_ss_gte and z >= cfg.xc_aligned_z_gte:
            # 一致：**不加分**（文档 A §6 明写 action: NONE）。
            out["crosscheck"] = "ALIGNED"
            codes.append(RC_XC_ALIGNED)

    # —— 等级缺失：已由 mcap_effective 封到观察，这里只再打排序折扣 ——
    if status == COMBO_INCOMPLETE:
        factors *= cfg.f_incomplete

    # INV-1：本层只能保持或降级。
    if ZONE_RANK[zone] < ZONE_RANK[eff_in]:  # pragma: no cover - 防御
        raise McapDominanceError(
            f"单调性破坏: {eff_in} -> {zone}（本层只允许保持或降级）"
        )
    out["final_zone"] = zone
    out["final_zone_cn"] = zone_cn(zone)
    if zone != eff_in:
        codes.append(f"Y5_FINAL_{zone}")

    # —— 排序权重体系（文档 A §5.6）——
    w_base = cfg.base_weight(zone)
    w_prio = cfg.prio_weight(row.get(f"mcap_priority_{d}"))
    w_k = cfg.k_weight(row.get(f"mcap_resonance_k_{d}"))
    w_combo = w_base * w_prio * w_k
    # 钳制的是**因子乘积**，不是 W_final 绝对值 —— 保证 W_final <= W_combo，
    # 即动能 / SS / 缺数只能下调排序权重，永远不能上调（裁决二·2）。
    w_final = w_combo * min(F_MAX, max(F_MIN, factors))
    if w_base <= 0.0:
        w_final = 0.0  # 淘汰的 W_base=0 是硬性的，钳制不得把它抬起来
    score = _f(row, f"score_{'up' if d == 'up' else 'down'}", 0.0)
    rank_key = (score / 100.0) * (w_final / RANK_NORM)

    # INV-2：排序键不得抬到 Score 之上。
    if rank_key > (score / 100.0) + 1e-12:  # pragma: no cover - 防御
        raise McapDominanceError(f"RankKey {rank_key} > Score/100 {score / 100.0}")

    out.update(
        w_base=w_base,
        w_prio=w_prio,
        w_k=w_k,
        w_combo=w_combo,
        w_final=w_final,
        rank_key=rank_key,
    )
    return out


def row_fields(res: Mapping[str, Any], direction: str) -> dict[str, Any]:
    """展开成写进快照行的方向字段（审计链路要求逐项可见）。"""
    d = direction
    return {
        f"final_zone_{d}": res["final_zone"],
        f"mcap_predicate_{d}": res["predicate"],
        f"mcap_crosscheck_{d}": res["crosscheck"],
        f"w_base_{d}": res["w_base"],
        f"w_prio_{d}": res["w_prio"],
        f"w_k_{d}": res["w_k"],
        f"w_combo_{d}": res["w_combo"],
        f"w_final_{d}": res["w_final"],
        f"rank_key_{d}": res["rank_key"],
        f"dominance_reason_codes_{d}": list(res["dominance_reason_codes"]),
    }


def apply_dominance(
    rows: list[dict[str, Any]],
    *,
    cfg: DominanceConfig,
    mode: str,
) -> dict[str, Any]:
    """对整块板面施加主导层。``mode="on"`` 时把 ``state_{d}`` 覆写为 ``final_zone``。

    ``shadow`` 模式算并落库但不改 ``state``（用于灰度取证）。
    """
    m = normalize_mode(mode)
    meta: dict[str, Any] = {
        "mode": m,
        "enabled": m == MODE_ON,
        "rows": 0,
        "p1": 0,
        "p2": 0,
        "p3": 0,
        "aligned": 0,
        "divergent": 0,
        "demoted_by_dominance": 0,
        "final_by_zone": {},
        "config": cfg.as_payload(),
    }
    if m == MODE_OFF:
        return meta

    by_zone: dict[str, int] = {}
    for r in rows:
        meta["rows"] += 1
        for d in ("up", "down"):
            eff_in = str(r.get(f"effective_zone_{d}") or r.get(f"state_{d}") or "NONE")
            res = resolve_dominance(r, d, cfg=cfg)
            r.update(row_fields(res, d))
            p = res["predicate"]
            if p == "P1_CONFIRM":
                meta["p1"] += 1
            elif p == "P2_SOFTEN":
                meta["p2"] += 1
            elif p == "P3_VETO":
                meta["p3"] += 1
            xc = res["crosscheck"]
            if xc == "ALIGNED":
                meta["aligned"] += 1
            elif xc == "DIVERGENT":
                meta["divergent"] += 1
            fz = res["final_zone"]
            if fz != eff_in:
                meta["demoted_by_dominance"] += 1
            by_zone[fz] = by_zone.get(fz, 0) + 1
            if m == MODE_ON and eff_in not in BYPASS_STATES:
                # 主导层生效：覆写展示 / 准入用的 state，**不反写状态机记忆**
                # （文档 A §10.2）。base_state_{d} 由 mcap_effective 保留。
                r[f"state_{d}"] = fz
                # —— DMR 前置：最终分区必须是确认区，且方向天花板不差于配置下限 ——
                # 严格口径（dmr_ceiling_min="DMR"）等价于文档A §10.2；放宽到
                # "CONFIRMED" 时允许确定级天花板的币进 DMR 精选。只影响 DMR，
                # 不影响任何一区的准入（分区仍由 max() 合并决定）。
                ceil = r.get(f"mcap_ceiling_zone_{d}")
                z10 = r.get(f"mcap_z10_{d}")
                band_ok = True
                if cfg.dmr_z10_min is not None:
                    band_ok = z10 is not None and int(z10) >= int(cfg.dmr_z10_min)
                if band_ok and cfg.dmr_z10_max is not None:
                    band_ok = z10 is not None and int(z10) <= int(cfg.dmr_z10_max)
                r[f"dmr_ceiling_ok_{d}"] = bool(
                    fz == "CONFIRMED"
                    and ceil is not None
                    and ZONE_RANK.get(str(ceil), 9) <= ZONE_RANK[cfg.dmr_ceiling_min]
                    and band_ok
                )
    meta["final_by_zone"] = by_zone
    return meta


# ---------------------------------------------------------------------------
# 排序：三周期等级第一裁决（INV-3）
# ---------------------------------------------------------------------------
def rank_tuple(row: Mapping[str, Any], direction: str) -> tuple:
    """v2.0.0 统一排序键。**前四位全部是流通市值维度**，动能只能做次级裁决。

    顺序（升序排列，越小越靠前）：

    ==== ================================ ======================================
    位次 键                                含义
    ==== ================================ ======================================
    1    ``ZONE_RANK[final_zone]``        分区准入资格（已含天花板 + 谓词合取）
    2    ``ZONE_RANK[mcap_ceiling_zone]`` 流通市值天花板确认强度
    3    ``-z``                           方向 Z（三周期加权，越大越强）
    4    ``-resonance_k``                 三周期结构共振度
    5    ``-rank_key``                    W_final 综合排序键（含 Score，辅助）
    6-8  ``-score, -ss, -mom``            旧版三键，降为次级裁决
    9    ``symbol``                       稳定排序兜底
    ==== ================================ ======================================
    """
    d = direction
    fz = str(row.get(f"final_zone_{d}") or row.get(f"state_{d}") or "ELIMINATED")
    ceil = str(row.get(f"mcap_ceiling_zone_{d}") or "ELIMINATED")
    z = _z_of(row, d)
    k = row.get(f"mcap_resonance_k_{d}")
    return (
        ZONE_RANK.get(fz, len(ZONES_EN)),
        ZONE_RANK.get(ceil, len(ZONES_EN)),
        -(z if z is not None else -3.0),
        -(float(k) if k is not None else 0.0),
        -_f(row, f"rank_key_{d}", 0.0),
        -_f(row, f"score_{d}", 0.0),
        -_ss_of(row, d),
        -_mom_of(row, d),
        str(row.get("symbol") or ""),
    )


def explain_rank(row: Mapping[str, Any], direction: str) -> dict[str, Any]:
    """排序可解释性：输出 RankKey 全部组成字段与命中规则。"""
    d = direction
    return {
        "symbol": row.get("symbol"),
        "direction": d,
        "base_state": row.get(f"base_state_{d}"),
        "mcap_combo_code": row.get("mcap_combo_code"),
        "mcap_combo_status": row.get(f"mcap_combo_status_{d}"),
        "mcap_z10": row.get(f"mcap_z10_{d}"),
        "mcap_priority": row.get(f"mcap_priority_{d}"),
        "mcap_resonance_k": row.get(f"mcap_resonance_k_{d}"),
        "mcap_ceiling_zone": row.get(f"mcap_ceiling_zone_{d}"),
        "effective_zone": row.get(f"effective_zone_{d}"),
        "predicate": row.get(f"mcap_predicate_{d}"),
        "crosscheck": row.get(f"mcap_crosscheck_{d}"),
        "final_zone": row.get(f"final_zone_{d}"),
        "w_base": row.get(f"w_base_{d}"),
        "w_prio": row.get(f"w_prio_{d}"),
        "w_k": row.get(f"w_k_{d}"),
        "w_combo": row.get(f"w_combo_{d}"),
        "w_final": row.get(f"w_final_{d}"),
        "rank_key": row.get(f"rank_key_{d}"),
        "score": row.get(f"score_{d}"),
        "ss": row.get(f"ss_{d}"),
        "momentum": row.get(f"momentum_score_{d}"),
        "reason_codes": (
            list(row.get(f"effective_reason_codes_{d}") or [])
            + list(row.get(f"dominance_reason_codes_{d}") or [])
        ),
    }


__all__ = [
    "AUTHORIZATION_TOKEN",
    "DominanceConfig",
    "F_MAX",
    "F_MIN",
    "GOVERNED_PARAMETER_PREFIX",
    "McapDominanceError",
    "RANK_NORM",
    "SETTINGS_FIELD_MAP",
    "apply_dominance",
    "assert_production_ready",
    "authorization_ok",
    "cap_at",
    "config_from_settings",
    "demote_one",
    "explain_rank",
    "is_governed",
    "rank_tuple",
    "resolve_dominance",
    "row_fields",
]
