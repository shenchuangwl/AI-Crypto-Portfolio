"""Pydantic v2 models for screener REST + DMR §39 messages.

Aligns with contracts/json-schema/*.schema.json and design v1.2.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


class Direction(str, Enum):
    up = "up"
    down = "down"


class DmrDirection(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    NEUTRAL = "NEUTRAL"


class ScreenerState(str, Enum):
    NONE = "NONE"
    WATCH = "WATCH"
    QUALIFIED = "QUALIFIED"
    CONFIRMED = "CONFIRMED"
    ELIMINATED = "ELIMINATED"
    DATA_INSUFFICIENT = "DATA_INSUFFICIENT"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"


class DataMode(str, Enum):
    LIVE = "LIVE"
    CACHE_FRESH = "CACHE_FRESH"
    CACHE_STALE = "CACHE_STALE"
    SUPPLY_CACHE_PRICE_LIVE = "SUPPLY_CACHE_PRICE_LIVE"
    MISSING = "MISSING"
    BACKFILL = "BACKFILL"


class LiquidityGrade(str, Enum):
    A = "A"
    B = "B"
    C = "C"
    D = "D"
    UNCLASSIFIED = "UNCLASSIFIED"


class McapTfGrade(str, Enum):
    """周期流通市值等级（30m / 2h / 6h 共用同一套定义，不按方向拆分）。

    A：6 日平均流通市值 > 12 日平均流通市值 > 26 日平均流通市值 | 多头排列
    B：12 日平均流通市值 > 6 日平均流通市值 > 26 日平均流通市值 | 多头轻度回调
    C：12 日平均流通市值 > 26 日平均流通市值 > 6 日平均流通市值 | 多头重度回调
    D：12 日平均流通市值 < 26 日平均流通市值 < 6 日平均流通市值 | 空头重度回调
    E：12 日平均流通市值 < 6 日平均流通市值 < 26 日平均流通市值 | 空头轻度回调
    F：6 日平均流通市值 < 12 日平均流通市值 < 26 日平均流通市值 | 空头排列

    "日" = 当前周期下的 K 线根数。无法判级时字段为 ``None``，禁止新造等级名称。
    """

    A = "A"
    B = "B"
    C = "C"
    D = "D"
    E = "E"
    F = "F"


class McapTfDetail(BaseModel):
    """单周期流通市值均线明细（tooltip / 审计用）。"""

    grade: Optional[McapTfGrade] = None
    ma6: Optional[float] = None
    ma12: Optional[float] = None
    ma26: Optional[float] = None
    bars: int = 0
    supply_known: bool = False
    reason: str = ""


class MarketCapTier(str, Enum):
    T1 = "T1"
    T2 = "T2"
    T3 = "T3"
    T4 = "T4"
    T5 = "T5"


class CoinGeckoCredits(BaseModel):
    used_today: int = Field(ge=0)
    month_est: int = Field(ge=0)
    month_cap: int = Field(ge=0)
    utilization: float = Field(ge=0)
    alert_level: str  # ok | warn | critical


class ScanMeta(BaseModel):
    system_version: str
    anchor_date: str
    scan_id: str
    scan_sequence: int = Field(ge=0, le=95)
    scan_timestamp_utc: datetime
    effective_universe: int = Field(ge=0)
    baseline_universe: int = Field(ge=0)
    regime: float
    regime_label: str
    data_mode: DataMode
    state_counts: dict[str, int] = Field(default_factory=dict)
    coingecko_credits: CoinGeckoCredits
    parameter_version: str
    mapping_version: str
    data_version: str
    indicator_version: Optional[str] = None
    occupancy: Optional[dict[str, Any]] = None
    daily_unique: Optional[dict[str, Any]] = None
    control: Optional[dict[str, Any]] = None
    dmr: Optional[dict[str, Any]] = None
    alert: Optional[str] = None
    alerts: list[str] = Field(default_factory=list)
    # —— 文档B §3.1 一致性契约：参数指纹 + 天花板层声明块（旧快照没有这两个键）——
    param_hash: Optional[str] = None
    mcap_zone: Optional[dict[str, Any]] = None


class CandidateRow(BaseModel):
    rank: int = Field(ge=1)
    symbol: str
    underlying_asset: str
    canonical_asset_id: str
    contract_multiplier: int = Field(ge=1)
    direction: Direction
    state: ScreenerState
    state_enter_time_utc: datetime
    state_duration_minutes: float = Field(ge=0)
    state_enter_price: Optional[float] = None

    score_up: float
    score_down: float
    direction_confidence: float = Field(ge=0, le=1)
    liquidity_score: float
    liquidity_grade: LiquidityGrade
    # 选币榜「等级」与「1h」之间的三列：30m/2h/6h 周期流通市值等级（A–F，无法判级为 None）
    mcap_grade_30m: Optional[McapTfGrade] = None
    mcap_grade_2h: Optional[McapTfGrade] = None
    mcap_grade_6h: Optional[McapTfGrade] = None
    mcap_tf: dict[str, McapTfDetail] = Field(default_factory=dict)
    momentum_score: float
    mcap_momentum_score: float
    staircase_score: float
    consistency_score: float
    rank_velocity_score: float
    risk_score: float
    data_confidence: float

    ret_15m: float
    ret_1h: float
    ret_4h: float
    ret_24h: float
    # 选币榜「24h」与「锚点以来」之间的两列：近 7 天 / 近 30 天涨跌幅
    # （口径同 1h/4h/24h，只是取数周期不同；K 线不足回看根数时为 None）
    ret_1w: Optional[float] = None
    ret_1mo: Optional[float] = None
    ret_since_anchor: float

    aqv_6d_m: float
    aqv_12d_m: float
    aqv_26d_m: float

    circulating_supply: Optional[float] = None
    market_cap_coingecko: Optional[float] = None
    market_cap_calculated: Optional[float] = None
    supply_source: str
    data_mode: DataMode
    supply_as_of_utc: Optional[datetime] = None
    mapping_confidence: float = Field(ge=0, le=1)
    risk_flags: list[str] = Field(default_factory=list)
    reason_codes: list[str] = Field(default_factory=list)
    not_confirmed_reasons: list[str] = Field(default_factory=list)
    qualified_path: Optional[str] = None
    confirmed_path: Optional[str] = None
    ready_confirm: Optional[bool] = None
    dmr_selected: Optional[bool] = None
    mcap_combo: Optional[str] = None
    mcap_z10: Optional[int] = None
    mcap_k: Optional[int] = None
    mcap_ceiling: Optional[str] = None
    product_zone: Optional[str] = None
    combo_reason: Optional[str] = None
    # —— 文档A §4 唯一裁决版天花板层的行级字段（文档B §3.2，全部 optional）——
    combo_code: Optional[str] = None
    z_score: Optional[float] = None
    combo_zone: Optional[str] = None
    zone_ceiling: Optional[str] = None

    ref_price: Optional[float] = None
    last_price: Optional[float] = None
    price_change_since_scan: Optional[float] = None
    market_rank: Optional[int] = None
    tier_rank: Optional[int] = None
    market_cap_tier: Optional[MarketCapTier] = None
    coingecko_coin_id: Optional[str] = None


class TransitionEvent(BaseModel):
    id: str
    at_utc: datetime
    symbol: str
    from_state: ScreenerState
    to_state: ScreenerState
    direction: Direction
    reason_codes: list[str] = Field(default_factory=list)


class ScreenerSnapshot(BaseModel):
    """GET /screener/latest body."""

    meta: ScanMeta
    long_pool: list[CandidateRow]
    short_pool: list[CandidateRow]
    transitions: list[TransitionEvent]
    generated_at_utc: datetime
    expires_at_utc: datetime
    attribution: str = "Powered by CoinGecko"


class OhlcvBar(BaseModel):
    time: int  # unix seconds
    open: float
    high: float
    low: float
    close: float
    volume: float


class SymbolPathNode(BaseModel):
    scan_sequence: int
    scan_timestamp_utc: datetime
    state: ScreenerState
    score_up: float
    score_down: float
    ret_since_anchor: float


class SymbolScreenerDetail(BaseModel):
    current: Optional[CandidateRow] = None
    path: list[SymbolPathNode]
    meta: ScanMeta


class DmrCandidateMessage(BaseModel):
    """Design v1.2 §39 DMR candidate message."""

    message_id: str
    system_version: str
    anchor_date: str
    scan_id: str
    scan_sequence: int = Field(ge=0, le=95)
    scan_timestamp_utc: datetime
    generated_at_utc: datetime
    expires_at_utc: datetime
    symbol: str
    underlying_asset: str
    canonical_asset_id: str
    contract_multiplier: int = Field(ge=1)
    direction: DmrDirection
    state: ScreenerState
    state_duration_minutes: float = Field(ge=0)
    direction_confidence: float = Field(ge=0, le=1)
    total_score: float
    liquidity_score: float
    momentum_score: float
    market_cap_momentum_score: float
    staircase_score: float
    trend_consistency_score: float
    rank_velocity_score: float
    risk_score: float
    data_confidence: float
    market_rank: int
    tier_rank: int
    market_cap_tier: MarketCapTier
    coingecko_coin_id: Optional[str] = None
    circulating_supply: Optional[float] = None
    market_cap_calculated: Optional[float] = None
    market_cap_coingecko: Optional[float] = None
    risk_flags: list[str] = Field(default_factory=list)
    data_mode: DataMode
    reason_codes: list[str] = Field(default_factory=list)
    indicator_version: str
    parameter_version: str
    mapping_version: str
    data_version: str
    attribution: str = "Powered by CoinGecko"
    confirm_path: Optional[str] = None
    dmr_selected: Optional[bool] = None
    dmr_truncated: Optional[bool] = None
    # —— 执行身份（ChatGpt_SOL5.6 文档A §14 / 文档B §18.6）——
    #
    # 正向 allow-only 谓词读的九个字段中，candidate 侧的这六个 + 上面的
    # parameter_version。全部 Optional：旧消息缺字段时仍可解析进只读归档层，
    # 但 execution_guard 会因为「缺失/None」直接拒绝执行 —— 缺字段 = 不放行。
    board_key: Optional[str] = None
    dmr_executable: Optional[bool] = None
    consumable_by_dmr: Optional[bool] = None
    rule_revision: Optional[str] = None
    config_hash: Optional[str] = None
    mcap_mapping_version: Optional[str] = None
    mapping_hash: Optional[str] = None
    code_commit: Optional[str] = None
    #: 文档B §5.1：``mapping_version=cg-map`` 的无歧义新名（只增不改，旧名保留）。
    asset_mapping_version: Optional[str] = None


def board_direction_to_dmr(d: Direction) -> DmrDirection:
    return DmrDirection.LONG if d == Direction.up else DmrDirection.SHORT


def dmr_should_reject(
    msg: DmrCandidateMessage,
    *,
    now: Optional[datetime] = None,
    require_confirmed: bool = True,
    min_score: Optional[float] = None,
    parameter_whitelist: Optional[list[str]] = None,
) -> Optional[str]:
    """Return reject reason code, or None if consumable (§39.3)."""
    now = now or datetime.now(timezone.utc)
    if require_confirmed and msg.state != ScreenerState.CONFIRMED:
        return "state_not_confirmed"
    if msg.direction == DmrDirection.NEUTRAL:
        return "neutral_direction"
    exp = msg.expires_at_utc
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    if now > exp:
        return "expired"
    if msg.data_mode == DataMode.MISSING or msg.data_confidence < 60:
        return "data_quality"
    if any(f.startswith("H") for f in msg.risk_flags):
        return "hard_risk"
    if msg.circulating_supply is None or msg.circulating_supply <= 0:
        return "supply_missing"
    if parameter_whitelist is not None and msg.parameter_version not in parameter_whitelist:
        return "parameter_version"
    if min_score is not None and msg.total_score < min_score:
        return "score_floor"
    return None


def candidate_row_to_dmr_message(
    row: CandidateRow,
    meta: ScanMeta,
    *,
    message_id: str,
    generated_at: Optional[datetime] = None,
    expires_at: Optional[datetime] = None,
    total_score: Optional[float] = None,
) -> DmrCandidateMessage:
    """Map a board row into a §39 DMR message (adapter helper)."""
    generated_at = generated_at or datetime.now(timezone.utc)
    expires_at = expires_at or meta.scan_timestamp_utc
    # caller should set expires = scan_ts + 15m; keep explicit if provided
    score = total_score
    if score is None:
        score = row.score_up if row.direction == Direction.up else row.score_down
    return DmrCandidateMessage(
        message_id=message_id,
        system_version=meta.system_version,
        anchor_date=meta.anchor_date,
        scan_id=meta.scan_id,
        scan_sequence=meta.scan_sequence,
        scan_timestamp_utc=meta.scan_timestamp_utc,
        generated_at_utc=generated_at,
        expires_at_utc=expires_at,
        symbol=row.symbol,
        underlying_asset=row.underlying_asset,
        canonical_asset_id=row.canonical_asset_id,
        contract_multiplier=row.contract_multiplier,
        direction=board_direction_to_dmr(row.direction),
        state=row.state,
        state_duration_minutes=row.state_duration_minutes,
        direction_confidence=row.direction_confidence,
        total_score=score,
        liquidity_score=row.liquidity_score,
        momentum_score=row.momentum_score,
        market_cap_momentum_score=row.mcap_momentum_score,
        staircase_score=row.staircase_score,
        trend_consistency_score=row.consistency_score * 100
        if row.consistency_score <= 1
        else row.consistency_score,
        rank_velocity_score=row.rank_velocity_score,
        risk_score=row.risk_score,
        data_confidence=row.data_confidence,
        market_rank=row.market_rank or row.rank,
        tier_rank=row.tier_rank or row.rank,
        market_cap_tier=row.market_cap_tier or MarketCapTier.T3,
        coingecko_coin_id=row.coingecko_coin_id,
        circulating_supply=row.circulating_supply,
        market_cap_calculated=row.market_cap_calculated,
        market_cap_coingecko=row.market_cap_coingecko,
        risk_flags=list(row.risk_flags),
        data_mode=row.data_mode,
        reason_codes=list(row.reason_codes),
        indicator_version=meta.indicator_version or "ind-unknown",
        parameter_version=meta.parameter_version,
        mapping_version=meta.mapping_version,
        data_version=meta.data_version,
    )
