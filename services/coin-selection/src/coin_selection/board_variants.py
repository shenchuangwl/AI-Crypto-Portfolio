"""板面变体（board variant）注册表 —— 「选币榜」/「选币榜Y」的唯一解析入口。

一个变体 = 一套参数版本 + 一份独立数据目录 + 一组独立 API 前缀 / 前端路由。

    main → 选币榜    param-v1.4.0-staircase-confirm-dmr   data/coin-selection
    y    → 选币榜Y   param-v2.0.0-screener-y              data/coin-selection-y

配置事实源是 ``packages/config/board-variants.json``；本模块只负责把它翻译成
:class:`~coin_selection.scan.SelectionSettings` 与
:class:`~coin_selection.state_machine.StateConfig` 的实例。

为什么参数增量放 JSON 而不是写死在代码里：v2.0.0 的细则还没定，定下来之后应当
**只改配置**就能让选币榜Y 走上新参数，而 v1.4.0 的选币榜一个字节都不用动。

硬约束（与 README「硬约束」一节同级）：
  1. ``main`` 条目是冻结项。任何新增变体都不得改写它 —— 改它 = 动生产选币榜。
  2. ``dmr_executable=false`` 的变体，其 DMR inbox 必须是独立目录，且它的
     ``parameter_version`` 不得进 ``DMR_PARAM_WHITELIST``。选币榜Y 两条都满足。
  3. 变体投影失败必须只记 WARN：绝不允许拖垮 15 分钟主扫描循环。
"""

from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("coin_selection.board_variants")

#: 仓库根。本文件位于 services/coin-selection/src/coin_selection/ 下，向上 4 层。
REPO_ROOT = Path(
    os.environ.get("HERMES_ROOT", str(Path(__file__).resolve().parents[4]))
)

REGISTRY_PATH = Path(
    os.environ.get(
        "BOARD_VARIANTS_CONFIG",
        str(REPO_ROOT / "packages" / "config" / "board-variants.json"),
    )
)

MAIN_KEY = "main"
# 选币榜X v1.3.0 当前复刻 main v1.4.0；独立复盘闭环，日后只从 X overrides 演进。
BOARD_X_KEY = "x"
#: 选币榜Y 的变体 key。仅此一处出现字面量，其余代码一律引用该常量。
BOARD_Y_KEY = "y"

#: 扫描节拍。板面只用 15m 栅格说话（README「时间栅格」），周期长度必须是它的整数倍。
SCAN_INTERVAL_SEC = 900

#: warmup 允许改写的 StateConfig 字段白名单。
#:
#: 只放**时间类**门槛。周期重置后分区要从零重建，慢是慢在停留与连击上，
#: 那是可以谈的；N1–N4、F2、300 万美元硬门槛、SS / 一致性 / 数据质量阈值不可谈 ——
#: 放宽它们等于换了一套选币标准，而不是「重建得快一点」。
#: 这也**不是** SM_FAST：跨级级联由 SM_FAST 环境变量控制，这里碰不到。
WARMUP_ALLOWED_FIELDS = frozenset(
    {
        "min_dwell_watch",
        "min_dwell_qualified",
        "min_dwell_confirmed",
        "min_streak_watch",
        "min_streak_qualified",
        "min_streak_confirmed",
    }
)


@dataclass(frozen=True)
class CycleConfig:
    """一个板面的时间区（周期）管理。

    ``enabled=False`` = 完全没有周期概念，分区成员跨天连续持有 —— 这是「选币榜」
    一直以来的行为。「选币榜Y」用 ``enabled=True, period_hours=24, anchor_utc="00:00"``：
    周期起点就是系统既有的每日锚点 00:00 UTC（``scan_id`` 的 000 号节点），不另造标准。
    """

    enabled: bool = False
    period_hours: int = 24
    anchor_utc: str = "00:00"
    #: 周期节点要清空的东西。见 board-variants.json 的注释。
    clear: tuple[str, ...] = ("state_machine", "daily_unique")
    #: 清空前把分区名单存一份到 <data_dir>/cycle/ 供审计
    archive_close: bool = True
    warmup_enabled: bool = False
    warmup_nodes: int = 0
    warmup_state_config: dict[str, Any] = field(default_factory=dict)

    # ---- 周期算术 -------------------------------------------------------
    def anchor_minutes(self) -> int:
        """``"00:00"`` → 0；``"08:30"`` → 510。解析不了就按 0 处理。"""
        raw = str(self.anchor_utc or "00:00").strip()
        try:
            hh, mm = raw.split(":")[:2]
            return (int(hh) % 24) * 60 + (int(mm) % 60)
        except (ValueError, IndexError):
            log.warning("bad cycle anchor_utc %r; falling back to 00:00", raw)
            return 0

    def period_seconds(self) -> int:
        secs = int(self.period_hours or 24) * 3600
        if secs <= 0 or secs % SCAN_INTERVAL_SEC:
            log.warning(
                "cycle period_hours=%r is not a multiple of the 15m grid; using 24h",
                self.period_hours,
            )
            return 24 * 3600
        return secs

    def nodes_per_cycle(self) -> int:
        return self.period_seconds() // SCAN_INTERVAL_SEC

    def cycle_start(self, now: datetime) -> datetime:
        """``now`` 所属周期的起点（含）。

        以 UTC 纪元 + ``anchor_utc`` 为基准整齐切分，所以 24h/00:00 得到的就是
        当天 00:00 UTC —— 与 ``scan.resolve_anchor_00utc`` 逐秒相同（有单测钉住）。
        """
        base = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(
            minutes=self.anchor_minutes()
        )
        n = math.floor((now - base).total_seconds() / self.period_seconds())
        return base + timedelta(seconds=n * self.period_seconds())

    def cycle_end(self, now: datetime) -> datetime:
        return self.cycle_start(now) + timedelta(seconds=self.period_seconds())

    def cycle_key(self, now: datetime) -> str:
        """周期身份标识，例 ``20260825T0000Z``。重置的幂等键就是它。"""
        return self.cycle_start(now).strftime("%Y%m%dT%H%MZ")

    def node_in_cycle(self, now: datetime) -> int:
        """本周期内的第几个 15m 节点（0 = 周期起点那一节点）。"""
        delta = (now - self.cycle_start(now)).total_seconds()
        return int(delta // SCAN_INTERVAL_SEC)

    def in_warmup(self, now: datetime) -> bool:
        return (
            self.enabled
            and self.warmup_enabled
            and self.warmup_nodes > 0
            and self.node_in_cycle(now) < self.warmup_nodes
        )

    def warmup_overrides(self) -> dict[str, Any]:
        """过白名单后的 warmup 增量。被拒的字段只告警，不炸掉扫描。"""
        out: dict[str, Any] = {}
        for k, v in (self.warmup_state_config or {}).items():
            if k in WARMUP_ALLOWED_FIELDS:
                out[k] = v
            else:
                log.warning(
                    "cycle warmup override rejected (not a time gate, must stay frozen): %s",
                    k,
                )
        return out

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "period_hours": self.period_hours,
            "anchor_utc": self.anchor_utc,
            "clear": list(self.clear),
            "archive_close": self.archive_close,
            "nodes_per_cycle": self.nodes_per_cycle() if self.enabled else None,
            "warmup": {
                "enabled": self.warmup_enabled,
                "nodes": self.warmup_nodes,
                "state_config": self.warmup_overrides(),
            },
        }


CYCLE_OFF = CycleConfig(enabled=False)


@dataclass(frozen=True)
class BoardVariant:
    """一个板面变体的完整定义（路径均为相对仓库根的 POSIX 路径）。"""

    key: str
    label: str
    parameter_version: str
    data_dir: str
    dmr_inbox: str
    api_prefix: str
    web_route: str
    param_file: str = ""
    short_label: str = ""
    primary: bool = False
    #: True = 复用主扫描已取到的 G1–G4 指标做投影，不额外打交易所接口。
    projected: bool = False
    #: False = 该变体的 DMR 候选禁止被 dmr-adapter 消费（选币榜Y 即为 False）。
    dmr_executable: bool = False
    enabled: bool = True
    enabled_env: str = ""
    #: v2.0.0 调参的唯一机器入口：SelectionSettings 字段增量。
    settings_overrides: dict[str, Any] = field(default_factory=dict)
    #: v2.0.0 调参的唯一机器入口：StateConfig 字段增量。
    state_overrides: dict[str, Any] = field(default_factory=dict)
    #: 时间区管理。选币榜是 CYCLE_OFF（无重置，冻结）；选币榜Y 是 24h/00:00 UTC。
    cycle: CycleConfig = field(default_factory=lambda: CycleConfig(enabled=False))

    # ---- 路径解析 -------------------------------------------------------
    def data_path(self, root: Optional[Path] = None) -> Path:
        return Path(root or REPO_ROOT) / self.data_dir

    def inbox_path(self, root: Optional[Path] = None) -> Path:
        return Path(root or REPO_ROOT) / self.dmr_inbox

    def ledger_path(self, root: Optional[Path] = None) -> Path:
        """该变体自己的复盘账本 —— v1.4.0 与 v2.0.0 从不共用一个 sqlite 文件。"""
        return self.data_path(root) / "review" / "ledger.sqlite"

    def is_enabled(self) -> bool:
        """配置 enabled 与环境变量开关的合取；env 显式取 0/false/no 可临时关停。"""
        if not self.enabled:
            return False
        if self.enabled_env:
            raw = os.environ.get(self.enabled_env)
            if raw is not None and raw.strip().lower() in ("0", "false", "no", "off"):
                return False
        return True

    def to_public_dict(self) -> dict[str, Any]:
        """给 /api/v1/boards 与前端用的可序列化视图（不含内部路径以外的秘密）。"""
        return {
            "key": self.key,
            "label": self.label,
            "short_label": self.short_label or self.label,
            "parameter_version": self.parameter_version,
            "param_file": self.param_file,
            "api_prefix": self.api_prefix,
            "web_route": self.web_route,
            "data_dir": self.data_dir,
            "dmr_inbox": self.dmr_inbox,
            "primary": self.primary,
            "projected": self.projected,
            "dmr_executable": self.dmr_executable,
            "enabled": self.is_enabled(),
            "settings_overrides": dict(self.settings_overrides),
            "state_config_overrides": dict(self.state_overrides),
            "cycle": self.cycle.to_public_dict(),
        }

    def cycle_dir(self, root: Optional[Path] = None) -> Path:
        """周期收官存档目录（每个周期一份分区名单，只读审计用）。"""
        return self.data_path(root) / "cycle"

    def cycle_state_path(self, root: Optional[Path] = None) -> Path:
        """周期水位：记录最后一次重置发生在哪个周期，重置的幂等键。"""
        return self.data_path(root) / "cycle_state.json"


# —— 配置缺失时的兜底：网关 / 扫描器绝不能因为少一个 JSON 就整个不工作 ——
_FALLBACK: tuple[BoardVariant, ...] = (
    BoardVariant(
        key=MAIN_KEY,
        label="选币榜",
        short_label="选币榜",
        parameter_version="param-v1.4.0-staircase-confirm-dmr",
        data_dir="data/coin-selection",
        dmr_inbox="data/dmr-adapter/inbox",
        api_prefix="/api/v1/screener",
        web_route="/screener",
        primary=True,
        projected=False,
        dmr_executable=True,
    ),
    # 注册表失读也不能借 main 目录/版本：X 的 shadow 观察与两道执行锁必须保住。
    BoardVariant(
        key=BOARD_X_KEY,
        label="选币榜X",
        short_label="选币榜X",
        parameter_version="param-v1.3.0-screener-x",
        param_file="packages/config/param-v1.3.0-screener-x.yaml",
        data_dir="data/coin-selection-x",
        dmr_inbox="data/dmr-adapter-x/inbox",
        api_prefix="/api/v1/screener-x",
        web_route="/screener-x",
        primary=False,
        projected=True,
        dmr_executable=False,
        enabled_env="ENABLE_BOARD_X",
        settings_overrides={"mcap_zone_mode": "shadow"},
        cycle=CycleConfig(enabled=False),
    ),
    BoardVariant(
        key=BOARD_Y_KEY,
        label="选币榜Y",
        short_label="选币榜Y",
        parameter_version="param-v2.0.0-screener-y",
        data_dir="data/coin-selection-y",
        dmr_inbox="data/dmr-adapter-y/inbox",
        api_prefix="/api/v1/screener-y",
        web_route="/screener-y",
        primary=False,
        projected=True,
        dmr_executable=False,
        enabled_env="ENABLE_BOARD_Y",
        # 注册表读不到时也必须保住 24h 重置：少了它，选币榜Y 会悄悄退化成
        # 「跨天连续持有」——正是这个栏目要解决的问题。
        cycle=CycleConfig(enabled=True, period_hours=24, anchor_utc="00:00"),
    ),
)

_cache: Optional[tuple[float, tuple[BoardVariant, ...]]] = None


def _parse(doc: dict[str, Any]) -> tuple[BoardVariant, ...]:
    out: list[BoardVariant] = []
    for raw in doc.get("boards") or []:
        if not isinstance(raw, dict) or not raw.get("key"):
            continue
        ov = raw.get("overrides") or {}
        cyc = raw.get("cycle") or {}
        wu = cyc.get("warmup") or {}
        cycle = CycleConfig(
            enabled=bool(cyc.get("enabled")),
            period_hours=int(cyc.get("period_hours") or 24),
            anchor_utc=str(cyc.get("anchor_utc") or "00:00"),
            clear=tuple(cyc.get("clear") or ("state_machine", "daily_unique")),
            archive_close=bool(cyc.get("archive_close", True)),
            warmup_enabled=bool(wu.get("enabled")),
            warmup_nodes=int(wu.get("nodes") or 0),
            warmup_state_config=dict(wu.get("state_config") or {}),
        )
        out.append(
            BoardVariant(
                key=str(raw["key"]),
                label=str(raw.get("label") or raw["key"]),
                short_label=str(raw.get("short_label") or ""),
                parameter_version=str(raw.get("parameter_version") or ""),
                param_file=str(raw.get("param_file") or ""),
                data_dir=str(raw.get("data_dir") or ""),
                dmr_inbox=str(raw.get("dmr_inbox") or ""),
                api_prefix=str(raw.get("api_prefix") or ""),
                web_route=str(raw.get("web_route") or ""),
                primary=bool(raw.get("primary")),
                projected=bool(raw.get("projected")),
                dmr_executable=bool(raw.get("dmr_executable")),
                enabled=bool(raw.get("enabled", True)),
                enabled_env=str(raw.get("enabled_env") or ""),
                settings_overrides=dict(ov.get("settings") or {}),
                state_overrides=dict(ov.get("state_config") or {}),
                cycle=cycle,
            )
        )
    return tuple(out)


def load_variants(*, refresh: bool = False) -> tuple[BoardVariant, ...]:
    """读注册表（按 mtime 记忆化）。读不到就用内置兜底，绝不抛。"""
    global _cache
    try:
        mtime = REGISTRY_PATH.stat().st_mtime
    except OSError:
        return _FALLBACK
    if not refresh and _cache and _cache[0] == mtime:
        return _cache[1]
    try:
        doc = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        variants = _parse(doc)
    except Exception as e:  # noqa: BLE001 — 配置坏了也不能让扫描/网关躺下
        log.warning("board-variants.json unreadable (%s); using built-in fallback", e)
        return _FALLBACK
    if not variants:
        return _FALLBACK
    _cache = (mtime, variants)
    return variants


def get_variant(key: str) -> Optional[BoardVariant]:
    for v in load_variants():
        if v.key == key:
            return v
    return None


def main_variant() -> BoardVariant:
    for v in load_variants():
        if v.primary:
            return v
    return _FALLBACK[0]


def secondary_variants(*, only_enabled: bool = True) -> tuple[BoardVariant, ...]:
    """非主板面（当前只有选币榜Y）。主扫描跑完后逐个投影。"""
    out = [v for v in load_variants() if not v.primary]
    if only_enabled:
        out = [v for v in out if v.is_enabled()]
    return tuple(out)


def variant_for_parameter_version(pv: str) -> Optional[BoardVariant]:
    for v in load_variants():
        if v.parameter_version == pv:
            return v
    return None


# ---------------------------------------------------------------------------
# 参数派生：把变体的 overrides 打进 SelectionSettings / StateConfig
# ---------------------------------------------------------------------------
def _apply_overrides(obj: Any, overrides: dict[str, Any], what: str) -> Any:
    """dataclasses.replace 的宽容版：未知字段只告警，不炸掉整轮扫描。"""
    if not overrides:
        return obj
    known = {f for f in getattr(obj, "__dataclass_fields__", {})}
    clean = {}
    for k, v in overrides.items():
        if k in known:
            clean[k] = v
        else:
            log.warning("board variant %s override ignored (unknown field): %s", what, k)
    return replace(obj, **clean) if clean else obj


def variant_settings(base: Any, variant: BoardVariant, *, root: Optional[Path] = None) -> Any:
    """由主扫描的 settings 派生出变体自己的 settings。

    强制改写三项（不受 overrides 影响，防止误配把 Y 的产物写回 main 的目录）：
    ``parameter_version`` / ``data_dir`` / ``dmr_inbox``。
    """
    derived = _apply_overrides(base, variant.settings_overrides, f"{variant.key}.settings")
    # —— 配置期拒绝未知的 DMR 裁决集选择器 ——
    #
    # ``_apply_overrides`` 只挡「字段名不存在」，挡不住「字段名对、取值是错的」。
    # 没有这道校验，注册表里写 dmr_selection_mode="confirmed-216"（少个后缀）会被
    # 静默接受，一路走到 build_dmr_messages 才抛 ValueError，而那里的异常又被
    # project_secondary_boards 的通吃 except 吞成一条 warning —— 板面直接少一个
    # 节点的 DMR，没有任何人看得见。StateConfig.selection_semantics 走
    # __post_init__ 已经是这个待遇（当场 ValueError），这里补齐对称的一半。
    mode = str(getattr(derived, "dmr_selection_mode", DMR_SELECTION_MODE_DEFAULT))
    if mode not in DMR_SELECTION_MODES:
        raise ValueError(
            f"board variant {variant.key}.settings.dmr_selection_mode={mode!r} 非法；"
            f"合法取值：{DMR_SELECTION_MODES}"
        )
    return replace(
        derived,
        parameter_version=variant.parameter_version,
        data_dir=str(variant.data_path(root)),
        dmr_inbox=str(variant.inbox_path(root)),
    )


def variant_state_config(
    variant: BoardVariant, base: Any = None, *, warmup: bool = False
) -> Any:
    """变体的状态机配置；overrides 为空时逐字段等同 v1.4.0 生产默认值。

    ``warmup=True`` 时再叠一层周期重建增量 —— 只会动 min_dwell_* / min_streak_*，
    见 :data:`WARMUP_ALLOWED_FIELDS`。
    """
    from .state_machine import default_state_config

    cfg = base if base is not None else default_state_config()
    cfg = _apply_overrides(cfg, variant.state_overrides, f"{variant.key}.state_config")
    if warmup:
        cfg = _apply_overrides(
            cfg, variant.cycle.warmup_overrides(), f"{variant.key}.cycle.warmup"
        )
    return cfg


# ---------------------------------------------------------------------------
# 参数指纹 param_hash —— 文档B §3.1 的 G1「两个栏目不可能悄悄分叉」的机器保证
# ---------------------------------------------------------------------------
#
# 《选币榜Y》与《复盘选币》必须同源同参。``parameter_version`` 管「哪套标准」，
# 一整个版本周期内不变；``param_hash`` 管「这套标准的哪一次调参」—— overrides
# 每调一次它就变。两者并存，缺一不可。
#
# 白名单是**显式**的：只收影响选币结果的字段。``data_dir`` / ``dmr_inbox`` /
# ``gate1_workers`` 这类运行时字段一律不进，否则换个目录跑回放就永远对不上。

# ---------------------------------------------------------------------------
# DMR 裁决集选择器 —— 唯一合法取值表（配置期校验 + 身份指纹共用同一个常量）
# ---------------------------------------------------------------------------
#
# 放在 board_variants 而不是 scan：本模块不 import scan / rule_manifest，
# 三方（scan 的 dataclass 默认值、rule_manifest 的 effective_config、
# 本模块的 param_fingerprint_payload）都能安全引用它，不形成环。
#
#   strict-v1.4               现网口径：确认 ∩ dmr_zone_ok（70/55/65/.67/70/LIVE）
#   confirmed-basic-216-v1.3  选币榜X 的历史 v1.3 口径：
#                             确认 ∩ 基础消息数据条件 ∩ 216 方向 ceiling==DMR
#
# **默认值必须保持 strict-v1.4**：它是 main/Y 的现网行为，也是身份省略式的基准。
DMR_SELECTION_MODE_DEFAULT = "strict-v1.4"
#   confirmed-basic-rank216-v1.3
#                             同上，但 216 **不再做准入**，改为排序权重。
#                             起因：staging 全量重建（2139 节点 / 39,329 笔）实测发现
#                             方向天花板作为准入筛选是**反向指标** —— 确认区里
#                             ceil=DMR 的那批合计 -15.1%，而 ceil=WATCH 的那批
#                             +188.8%。用它做准入等于在筛掉好票。
#                             但 216 的另一部分是有效的：三周期全同向（AAA/FFF）
#                             合计 +161.0%、盈亏比 3.21，显著优于混合组合。
#                             因此把「全同向」提为排序首键，天花板退出准入。
DMR_SELECTION_MODES: tuple[str, ...] = (
    DMR_SELECTION_MODE_DEFAULT,
    "confirmed-basic-216-v1.3",
    "confirmed-basic-rank216-v1.3",
)

#: 需要 X 身份 + dual-path + shadow 三件齐备的 DMR 模式（两者共用同一套守卫）。
DMR_MODES_REQUIRING_X_V13: tuple[str, ...] = (
    "confirmed-basic-216-v1.3",
    "confirmed-basic-rank216-v1.3",
)

#: 参与指纹的 SelectionSettings 字段（顺序无关，序列化时按 key 排序）。
FINGERPRINT_SETTINGS_FIELDS: tuple[str, ...] = (
    "w_ss",
    "w_mom",
    "w_liq",
    "w_mcap",
    "w_cons",
    "w_rank",
    "w_risk",
    "dmr_top_k",
    "hard_floor_usd",
    "baseline_universe",
    "enable_gate2",
    "enable_gate3",
    "enable_gate4",
    "enable_state_machine",
    "enable_mcap_tf",
    "enable_long_ret",
)

#: 参与指纹的 StateConfig 字段。Score 类 / SS 类 / M 类 / C 类 / DQ 类 / 时间类全收。
FINGERPRINT_STATE_FIELDS: tuple[str, ...] = (
    "enter_watch",
    "exit_watch",
    "min_streak_watch",
    "min_dwell_watch",
    "enter_qualified",
    "enter_qualified_m",
    "ss_qualified",
    "ss_qualified_m",
    "mom_qualified",
    "mom_qualified_m",
    "exit_qualified",
    "min_streak_qualified",
    "min_dwell_qualified",
    "enter_confirmed",
    "ss_confirmed",
    "mom_confirmed",
    "cons_confirmed",
    "dq_confirm_floor",
    "exit_confirmed",
    "hold_ss",
    "dmr_score",
    "dmr_ss",
    "dmr_momentum",
    "dmr_consistency",
    "dmr_dq",
    "min_streak_confirmed",
    "min_dwell_confirmed",
    "scan_interval_min",
    "dmr_top_k",
)

#: 指纹里的冻结段版本号。改 A–F 判级函数 / Gate4 封顶 / 回看根数 就必须改这里，
#: 于是任何人偷偷动红线项，指纹立刻变化，回放会拒绝出数。
FROZEN_GRADE_FN = "grade_from_mas@mcap_timeframe"
FROZEN_G4_F2_CAP = 45
FROZEN_G4_LOOKBACK_1H = 168

#: 天花板层的两个冻结子版本（A–F 阶梯与 5:3:2 周期权重，见文档A §4.2）。
MCAP_LADDER_VERSION = "q-equal-step-v1"
MCAP_TF_WEIGHTS_VERSION = "w-5-3-2-v1"

PARAM_FINGERPRINT_SCHEMA = "param-fingerprint-v1"


def _norm_fp(v: Any) -> Any:
    """浮点归一到 9 位小数后转字符串 —— 免得 0.1+0.2 的表示差异让指纹抖动。"""
    if isinstance(v, bool):
        return bool(v)
    if isinstance(v, float):
        return f"{round(v, 9):.9f}"
    if isinstance(v, int):
        return int(v)
    if v is None:
        return None
    return str(v)


def param_fingerprint_payload(
    variant: Optional[BoardVariant],
    settings: Any,
    state_cfg: Any,
) -> dict[str, Any]:
    """指纹的原文（可读、可 diff）。``param_fingerprint`` 只是它的 sha256 前 16 位。"""
    cyc = getattr(variant, "cycle", None) if variant is not None else None
    pv = (
        getattr(variant, "parameter_version", None)
        if variant is not None
        else getattr(settings, "parameter_version", None)
    )
    return {
        "schema": PARAM_FINGERPRINT_SCHEMA,
        # X适配身份独立，默认main/Y整个键省略以保持已发布指纹不变。
        **({"selection_semantics": "dual-path-v1.3"}
           if getattr(state_cfg, "selection_semantics", "staircase-v1.4") == "dual-path-v1.3" else {}),
        # —— DMR 裁决集的选择器，与上一条同样条件式省略 ——
        #
        # ``dmr_selection_mode`` 决定 DMR 成员由哪一套谓词产生：
        #   strict-v1.4              确认 ∩ dmr_zone_ok(70/55/65/.67/70/LIVE)
        #   confirmed-basic-216-v1.3 确认 ∩ 基础消息数据条件 ∩ 216 方向 ceiling==DMR
        # 两者在真实节点上给出**不同的 DMR 成员集**（实测 20260906-039：13 → 16，
        # 且 216 侧排除了 22 条确认边）。它必须进身份，否则复盘按 param_hash 分段时
        # 会把两套规则的成交合并统计 —— 那正是 D2「不能用同一 hash 制造一致性」禁止的。
        #
        # **默认值时整键省略**：直接把字段加进 FINGERPRINT_SETTINGS_FIELDS 会让
        # main 的 pf1_ddc6f8d09708942c 与 Y 的 pf1_fcea251fa94122fe 一起变号
        # （已实测），等于追溯改写已发布身份。省略式使 main/Y 逐字节不变。
        **({"dmr_selection_mode": str(getattr(settings, "dmr_selection_mode", DMR_SELECTION_MODE_DEFAULT))}
           if str(getattr(settings, "dmr_selection_mode", DMR_SELECTION_MODE_DEFAULT)) != DMR_SELECTION_MODE_DEFAULT else {}),
        # 动能下限同样条件式省略：0（历史口径）时整键不出现，main/Y 身份逐字节不变。
        **({"dmr_momentum_floor": f"{float(getattr(settings, 'dmr_momentum_floor', 0.0) or 0.0):.9f}"}
           if float(getattr(settings, 'dmr_momentum_floor', 0.0) or 0.0) > 0 else {}),
        # 确认区 PATH_M 三阈值：**偏离历史 v1.3 原值时才进身份**。
        # 无条件加键会改掉现网 r3 的 pf1_a86b486ddefe82cc —— 那是追溯改写已发布身份。
        **({"confirmed_path_m": {k: f"{float(getattr(state_cfg, k, d)):.9f}"
                                 for k, d in (("enter_confirmed_m", 70.0),
                                              ("ss_confirmed_m", 45.0),
                                              ("mom_confirmed_m", 70.0))}}
           if any(float(getattr(state_cfg, k, d)) != d
                  for k, d in (("enter_confirmed_m", 70.0), ("ss_confirmed_m", 45.0),
                               ("mom_confirmed_m", 70.0))) else {}),
        "parameter_version": str(pv or ""),
        "settings": {
            k: _norm_fp(getattr(settings, k, None)) for k in FINGERPRINT_SETTINGS_FIELDS
        },
        "state_config": {
            k: _norm_fp(getattr(state_cfg, k, None)) for k in FINGERPRINT_STATE_FIELDS
        },
        "cycle": {
            "enabled": bool(getattr(cyc, "enabled", False)),
            "period_hours": int(getattr(cyc, "period_hours", 24) or 24),
            "anchor_utc": str(getattr(cyc, "anchor_utc", "00:00") or "00:00"),
            "clear": sorted(str(x) for x in (getattr(cyc, "clear", ()) or ())),
            "warmup": {
                k: _norm_fp(v)
                for k, v in sorted(
                    (cyc.warmup_overrides() if cyc is not None else {}).items()
                )
            },
        },
        "mcap_zone": {
            "mode": str(mcap_zone_mode_of(settings)),
            "cuts": [
                _norm_fp(float(getattr(settings, "mcap_zone_cut_dmr", 2.1))),
                _norm_fp(float(getattr(settings, "mcap_zone_cut_confirmed", 1.5))),
                _norm_fp(float(getattr(settings, "mcap_zone_cut_qualified", 0.8))),
                _norm_fp(float(getattr(settings, "mcap_zone_cut_watch", -0.5))),
            ],
            "abstain_demote": bool(getattr(settings, "mcap_zone_abstain_demote", True)),
            "ladder_version": MCAP_LADDER_VERSION,
            "tf_weights_version": MCAP_TF_WEIGHTS_VERSION,
            # v2.0.0 主导层：授权令牌 + 谓词 / 互印证 / 排序权重全量进指纹。
            # 令牌进指纹 ⇒ 「谁在什么时候授权开启了 216 主导层」在账本里可追溯；
            # 阈值进指纹 ⇒ 改任何一个都会让历史与现网不可比（这正是我们要的）。
            #
            # **层关闭时整键省略**：param_hash 是账本连续性的锚，无条件加键会让
            # 改造前已入账的 pf1_1b44aba95de8ffe8 变号、历史整段变「不可比」。
            **_dominance_fp(settings),
        },
        "frozen": {
            "grade_fn": FROZEN_GRADE_FN,
            "g1_floor_usd": _norm_fp(float(getattr(settings, "hard_floor_usd", 3_000_000))),
            "g4_f2_cap": FROZEN_G4_F2_CAP,
            "g4_lookback_1h": FROZEN_G4_LOOKBACK_1H,
        },
    }


def _dominance_fp(settings: Any) -> dict[str, Any]:
    """主导层参数的指纹片段，**已展开成可 ``**`` 合并的键值对**。

    ``mcap_zone_mode == "off"`` → 返回空 dict，即整键省略，
    使「层关着」的 ``param_hash`` 与本次改造前**逐字节一致**（账本连续性）。
    """
    if str(mcap_zone_mode_of(settings)) == "off":
        return {}
    from .mcap_dominance import config_from_settings

    payload = config_from_settings(settings).as_payload()

    def _walk(o: Any) -> Any:
        if isinstance(o, dict):
            return {k: _walk(o[k]) for k in sorted(o)}
        if isinstance(o, list):
            return [_walk(x) for x in o]
        if isinstance(o, float):
            return _norm_fp(o)
        return o

    return {
        "authorization": str(
            getattr(settings, "mcap_zone_authorization", None) or ""
        ),
        # DMR 前置的 ceiling 最低档：它直接决定 DMR 成员，必须进指纹，
        # 否则 ceiling=DMR 与 ceiling>=确定 两套配置会算出同一个 param_hash，
        # 账本里两段实验无法区分（这正是本轮扫参时踩到的坑）。
        "dmr_ceiling_min": str(getattr(settings, "mcap_dmr_ceiling_min", "DMR") or "DMR"),
        "dominance": _walk(payload),
    }


def mcap_zone_mode_of(settings: Any) -> str:
    """天花板层的三态开关：``off`` / ``shadow`` / ``on``（文档B 阶段 2）。

    ``mcap_zone_mode`` 是权威字段；布尔 ``enable_mcap_zone`` 是它的向后兼容别名
    （True ⇒ ``on``）。任何无法识别的取值一律降级为 ``off`` —— 开关坏掉时必须
    退化为「现网行为」，不是「未定义行为」。
    """
    raw = getattr(settings, "mcap_zone_mode", None)
    mode = str(raw or "").strip().lower()
    if mode in ("off", "shadow", "on"):
        if mode == "off" and bool(getattr(settings, "enable_mcap_zone", False)):
            return "on"
        return mode
    if raw:
        log.warning("unknown mcap_zone_mode %r — treating as off", raw)
    return "on" if bool(getattr(settings, "enable_mcap_zone", False)) else "off"


def param_fingerprint(
    variant: Optional[BoardVariant],
    settings: Any,
    state_cfg: Any,
) -> str:
    """``pf1_<sha256[:16]>``。两个栏目共用这一个函数，不允许有第二份实现。"""
    import hashlib

    payload = param_fingerprint_payload(variant, settings, state_cfg)
    blob = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return "pf1_" + hashlib.sha256(blob).hexdigest()[:16]
