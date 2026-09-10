"""State machine with time + score hysteresis.

Production: param-v1.4.0-staircase-confirm-dmr.
WATCH / ELIMINATED / HARD_FAIL / no-skip / no SM_FAST stay frozen.
QUALIFIED keeps PATH_S OR PATH_M; CONFIRMED requires hard staircase PATH_S only.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

log = logging.getLogger("coin_selection.state_machine")

CONS_ONE_EPS = 1e-9
SCAN_INTERVAL_SEC = 900  # 15m — the only cadence the board is allowed to speak in
BASELINE_UNIVERSE = 526
N_IMPULSE_FLOOR = 8
LIVE_RATIO_FLOOR = 0.80
UNIVERSE_DEV_STRESS = 0.10


@dataclass
class StateConfig:
    # X现代v1.3适配的显式能力；默认保持main/Y，候选overrides才启用，复盘同一内核。
    selection_semantics: str = "staircase-v1.4"

    def __post_init__(self):
        if self.selection_semantics not in ("staircase-v1.4", "dual-path-v1.3"):
            raise ValueError("未知选币规则语义")

    # ── WATCH (frozen v1.2) ──
    enter_watch: float = 45
    exit_watch: float = 40
    min_streak_watch: int = 2
    min_dwell_watch: int = 30  # minutes in WATCH before QUAL upgrade

    # ── QUALIFIED dual-path ──
    enter_qualified: float = 58  # PATH_S score
    enter_qualified_m: float = 62
    ss_qualified: float = 50
    ss_qualified_m: float = 45
    mom_qualified: float = 55
    mom_qualified_m: float = 65
    exit_qualified: float = 52
    min_streak_qualified: int = 2  # pass_qualified streak while WATCH
    min_dwell_qualified: int = 15  # minutes in QUALIFIED before CONF upgrade

    # ── CONFIRMED: hard rotating-staircase only ──
    enter_confirmed: float = 66
    ss_confirmed: float = 50
    mom_confirmed: float = 60
    cons_confirmed: float = 0.67
    dq_confirm_floor: float = 60
    exit_confirmed: float = 56
    hold_ss: float = 50

    # ── 确认区 PATH_M（仅 dual-path-v1.3 生效）──
    #
    # 历史 v1.3.0 冻结值 70/45/70（param-v1.3.0-dual-path-sticky.yaml:60），
    # 已由该窗口 43 条新晋样本实测贴边验证（score 70.02 / ss 45 / mom 70.85）。
    #
    # 为什么要配置化：**符合区**的 PATH_M 一直是正经字段
    # （enter_qualified_m / ss_qualified_m / mom_qualified_m），而**确认区**的同一组
    # 阈值原先是写死的字面量。这个不对称让「确认区 M 门槛」既不能经 overrides 调，
    # 也不出现在指纹的 state_config 段，回测时只能改代码 —— 正是它挡住了
    # 「确认区 M>=75 完整版」的验证（实测 PATH_M 成员 22.3% 落在 [70,75) 被放行）。
    #
    # 默认值 = 历史 v1.3.0 原值，因此**默认行为逐字节不变**；
    # 只有偏离默认时才进身份指纹（见 board_variants / rule_manifest 的条件式省略）。
    enter_confirmed_m: float = 70
    ss_confirmed_m: float = 45
    mom_confirmed_m: float = 70

    # ── DMR zone: stricter derived subset of CONFIRMED ──
    dmr_score: float = 70
    dmr_ss: float = 55
    dmr_momentum: float = 65
    dmr_consistency: float = 0.67
    dmr_dq: float = 70
    min_streak_confirmed: int = 2
    min_dwell_confirmed: int = 60  # unused for upgrade (legacy field)

    scan_interval_min: int = 15
    dmr_top_k: int = 16
    n_impulse_floor: int = N_IMPULSE_FLOOR
    live_ratio_floor: float = LIVE_RATIO_FLOOR
    # —— 确认区去重占用的展示 / 告警目标带 ——
    #
    # **不进任何选币谓词**：只驱动 CONFIRM_UNDERFILLED / CONFIRM_OVERFLOW 与页头颜色。
    # 选币榜与选币榜Y 共用。前端镜像在 apps/web/src/shared/config/occupancy.ts。
    #
    # 上界 40 的依据（实测，快照全窗口，两个体制分开统计）：
    #
    #   主导层关闭段  n=1748  中位 27  p75 46  p90 56  p95 61  p99 82  max 104
    #   主导层生效段  n= 238  中位 16  p75 21  p90 27  p95 30  p99 31  max  32
    #
    # 40 是**体制回退探测器**，不是逐节点的溢出报警：
    #   * 生效段 max=32，因此正常运行时它**不会**触发 —— 这是刻意的；
    #   * 一旦占用回到 40 以上，说明 216 主导层实质失效（映射降级为 off、
    #     授权丢失、或天花板不再压制），板面退回主导层关闭段的量级。
    #     那是需要人介入的**体制事件**，正是该报的那一类。
    #
    # 为什么不取 p95=30 做上界：那会把生效段 3.8% 的正常节点报成异常，
    # 而这些节点并没有任何需要人介入的事情发生 —— 报警会被训练成噪音。
    #
    # 【与文档的差异 · 已知】文档A(Opus5) §2.3 有一条守恒约束
    # 「确认侧占用 <= 24.49 × 1.5 = 36.7」。40 越过了它 3.3。
    # 该约束是在**主导层关闭**的样本上标定的（当时中位 27 / max 104，本就长期越界），
    # 用它约束生效段并不成立。若将来要恢复该口径，应在生效段样本上重新标定，
    # 而不是直接套用旧基数。
    #
    # 下界 10 沿用改造前的值：DMR Top-K=16，确认区占用长期低于 10 意味着
    # DMR 池填不满（CONFIRM_UNDERFILLED），这一侧的语义未变。
    #
    # 【身份】这两项**不进** config_hash / param_hash（见 rule_manifest.CONFIG_STATE_FIELDS
    # 与 board_variants.FINGERPRINT_STATE_FIELDS 的显式白名单）。理由是它们不改变
    # 任何一行的选币结果，因此改它们不会让历史数据变得不可比。
    # 代价是：通过 overrides.state_config 改它们**不留身份痕迹**，
    # 所以 param-v2.0.0-screener-y.yaml 里有一份显式登记，改动请同步那里。
    occupancy_lo: int = 10
    occupancy_hi: int = 40


@dataclass
class SymState:
    symbol: str
    direction: str  # up|down
    state: str = "NONE"
    state_enter_ts: float = 0.0
    consecutive_pass: int = 0
    consecutive_fail: int = 0
    last_score: float = 0.0
    last_scan_id: str = ""
    confirmed_count: int = 0
    # Last/mark when this side first entered QUALIFIED or CONFIRMED (reset on leave / re-enter).
    state_enter_price: Optional[float] = None
    last_path: Optional[str] = None  # S | M | None (audit)
    # —— 展示区三元组（board 上用户真正看到的那个区）——
    #
    # 上面的 state_enter_* 跟随 **base_state**（层 A 状态机）。天花板 / 主导层生效后，
    # 板面显示的是 final_zone（层 C），两者可以不同 —— 实测 20260903-045 某币
    # base_state=CONFIRMED 而 final_zone=QUALIFIED，用户看到它离开确认区又回来，
    # 但 state_enter_ts 从未重置、停留时长一路累加、停留价格停在最初那次。
    #
    # 这里独立跟踪「展示区」的进入时刻与进入价，使「入选时间 / 停留时间 / 停留价格」
    # 三列与用户看到的分区严格对应。**不动 state_enter_***：复盘账本把它当入场价
    # 回退源（review_replay），改动会重写历史盈亏。
    disp_zone: Optional[str] = None
    disp_enter_ts: float = 0.0
    disp_enter_price: Optional[float] = None


_CLOCK_TS: Optional[float] = None


def set_clock(ts: Optional[float]) -> None:
    """Inject virtual now for multi-scan simulation; None = wall clock."""
    global _CLOCK_TS
    _CLOCK_TS = ts


def _now_ts() -> float:
    return float(_CLOCK_TS) if _CLOCK_TS is not None else time.time()


def floor_to_node(ts: float, interval_sec: int = SCAN_INTERVAL_SEC) -> float:
    """Snap an epoch second to the 15-minute scan grid it belongs to.

    ``86400 % 900 == 0`` and the Unix epoch starts at UTC midnight, so a plain
    floor over absolute seconds lands on exactly the same node that
    ``anchor + scan_sequence * 900`` names — no per-day arithmetic needed.
    """
    return float(int(ts) // interval_sec * interval_sec)


def quantize_enter_times(
    store: "StateMachineStore", interval_sec: int = SCAN_INTERVAL_SEC
) -> int:
    """One-shot migration: pull off-grid ``state_enter_ts`` back onto the grid.

    Rows stamped by an off-cadence run (``--force`` at 17:52, a restart, a manual
    scan) carry that drift forever, which is what surfaces as `38m` / `1h2m` in
    the 停留时间 column. Flooring is the right direction: it is the same node the
    row's ``scan_id`` already claimed.
    """
    n = 0
    for st in store.states.values():
        ts = st.state_enter_ts
        if not ts:
            continue
        snapped = floor_to_node(ts, interval_sec)
        if snapped != ts:
            st.state_enter_ts = snapped
            n += 1
    return n


def dwell_minutes(st: SymState, now_ts: Optional[float] = None) -> float:
    """Dwell in minutes, always an exact multiple of the 15m scan cadence.

    Both ends are snapped to the grid so a tick produced off-cadence cannot leak
    a 1–4 minute drift into 停留时间, and so a boundary gate like
    ``dwell >= 30`` can never near-miss at 29.98 and cost a whole node.
    """
    if not st.state_enter_ts:
        return 0.0
    now = float(now_ts) if now_ts is not None else _now_ts()
    return max(0.0, (floor_to_node(now) - floor_to_node(st.state_enter_ts)) / 60.0)


class StateMachineStore:
    """状态机记忆的持久化。

    【文档 A §4.2 / §13.1【上线阻断】】改造前 ``save()`` 把 ``parameter_version``
    **硬编码**成主榜的 ``param-v1.4.0-staircase-confirm-dmr``，于是《选币榜Y》的
    state 文件自报 v1.4.0，而它的 snapshot / ledger 是 v2 —— 同一实例出现两种身份，
    严格重放无法判断这份记忆属于哪套规则。现在改为**实例身份**：由调用方在构造时
    传入本板面真实的 ``parameter_version`` 与 manifest 引用（``identity``），
    缺省仍写主榜值以保持既有主榜文件逐字节不变。
    """

    def __init__(
        self,
        path: Path,
        *,
        parameter_version: str = "param-v1.4.0-staircase-confirm-dmr",
        identity: Optional[dict[str, Any]] = None,
    ):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.parameter_version = str(
            parameter_version or "param-v1.4.0-staircase-confirm-dmr"
        )
        #: RuleManifest 身份引用（rule_revision / config_hash / mapping_hash / code_commit…）。
        #: 文档 A §16：state 必须保存 manifest 引用，旧状态只读备份。
        self.identity: dict[str, Any] = dict(identity or {})
        self.states: dict[str, SymState] = {}
        self._load()

    def _key(self, symbol: str, direction: str) -> str:
        return f"{symbol}|{direction}"

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            obj = json.loads(self.path.read_text(encoding="utf-8"))
            for k, v in (obj.get("states") or {}).items():
                payload = dict(v)
                payload.setdefault("state_enter_price", None)
                payload.setdefault("last_path", None)
                known = {f.name for f in SymState.__dataclass_fields__.values()}  # type: ignore[attr-defined]
                payload = {kk: vv for kk, vv in payload.items() if kk in known}
                self.states[k] = SymState(**payload)
        except Exception as e:
            log.warning("state load failed: %s", e)

    def save(self) -> None:
        payload: dict[str, Any] = {
            "ts": _now_ts(),
            # 实例身份，不再硬编码主榜版本（文档 A §4.2 冲突矩阵「Y state 身份」）。
            "parameter_version": self.parameter_version,
            "states": {k: asdict(v) for k, v in self.states.items()},
        }
        if self.identity:
            payload["rule_identity"] = dict(self.identity)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def get(self, symbol: str, direction: str) -> SymState:
        k = self._key(symbol, direction)
        if k not in self.states:
            self.states[k] = SymState(symbol=symbol, direction=direction)
        return self.states[k]


def _as_price(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if x != x:  # NaN
        return None
    return x


# Listed board states: freeze first-print last/mark when the stay starts.
_LISTED_STATES = ("WATCH", "QUALIFIED", "CONFIRMED")


def _enter_state(st: SymState, state: str, last_price: Optional[float] = None) -> None:
    """Stamp enter time; freeze first-print price for 观察/符合/确认.

    The stamp is snapped to the 15m grid so every dwell is an exact multiple of
    the scan cadence even when this tick was produced by an off-cadence run.
    """
    st.state = state
    st.state_enter_ts = floor_to_node(_now_ts())
    if state in _LISTED_STATES:
        st.state_enter_price = _as_price(last_price)
    else:
        st.state_enter_price = None


def cons_is_one(c: Any) -> bool:
    try:
        return abs(float(c) - 1.0) <= CONS_ONE_EPS
    except (TypeError, ValueError):
        return False


def path_qualified(
    score: float,
    ss: float,
    momentum: float,
    consistency: float,
    supply_missing: bool,
    cfg: StateConfig,
) -> Optional[str]:
    """Return 'S' | 'M' | None. Prefer S when both match. Spec: no third mouth."""
    if supply_missing:
        return None
    path_s = (
        score >= cfg.enter_qualified
        and ss >= cfg.ss_qualified
        and momentum >= cfg.mom_qualified
    )
    path_m = (
        score >= cfg.enter_qualified_m
        and ss >= cfg.ss_qualified_m
        and momentum >= cfg.mom_qualified_m
        and cons_is_one(consistency)
    )
    if path_s:
        return "S"
    if path_m:
        return "M"
    return None


def path_confirmed(
    score: float,
    ss: float,
    momentum: float,
    consistency: float,
    dq: float,
    supply_missing: bool,
    cfg: StateConfig,
    data_mode: str = "LIVE",
) -> Optional[str]:
    """CONFIRMED hard gate: real rotating staircase is mandatory."""
    if supply_missing or dq < cfg.dq_confirm_floor or (data_mode or "") == "MISSING":
        return None
    path_s = (
        score >= cfg.enter_confirmed
        and ss >= cfg.ss_confirmed
        and momentum >= cfg.mom_confirmed
        and float(consistency) >= cfg.cons_confirmed
    )
    # X确认M恢复历史70/45/70/C1；S优先，不放宽main/Y硬楼梯。
    if path_s:
        return "S"
    if cfg.selection_semantics == "dual-path-v1.3":
        if (score >= cfg.enter_confirmed_m and ss >= cfg.ss_confirmed_m
                and momentum >= cfg.mom_confirmed_m and cons_is_one(consistency)):
            return "M"
    return None


def hold_ok(
    score: float,
    ss: float,
    momentum: float,
    consistency: float,
    cfg: StateConfig,
) -> bool:
    """CONFIRMED can only persist while the hard staircase remains present."""
    # X复盘/实时共享历史粘滞析取，默认v1.4保持原样。
    if cfg.selection_semantics == "dual-path-v1.3":
        return score >= cfg.exit_confirmed and (ss >= 45 or (cons_is_one(consistency) and momentum >= 62))
    return score >= cfg.exit_confirmed and ss >= cfg.hold_ss


def dmr_zone_ok(
    score: float,
    ss: float,
    momentum: float,
    consistency: float,
    dq: float,
    supply_missing: bool,
    data_mode: str,
    cfg: StateConfig,
) -> bool:
    """DMR zone: stricter, executable subset of staircase CONFIRMED."""
    return (
        not supply_missing
        and (data_mode or "") == "LIVE"
        and dq >= cfg.dmr_dq
        and score >= cfg.dmr_score
        and ss >= cfg.dmr_ss
        and momentum >= cfg.dmr_momentum
        and float(consistency) >= cfg.dmr_consistency
    )


def pass_watch(score: float, cfg: StateConfig) -> bool:
    return score >= cfg.enter_watch


def scan_control_flags(
    rows: list[dict[str, Any]],
    *,
    cfg: Optional[StateConfig] = None,
    baseline: int = BASELINE_UNIVERSE,
) -> dict[str, Any]:
    cfg = cfg or default_state_config()
    g1 = [r for r in rows if r.get("liquidity_hard_pass") is True]
    g1_syms = {r["symbol"] for r in g1}
    live_syms = {
        r["symbol"] for r in g1 if (r.get("data_mode") or "LIVE") == "LIVE"
    }
    live_ratio = (len(live_syms) / max(len(g1_syms), 1)) if g1_syms else 1.0
    n_impulse = 0
    for r in g1:
        if float(r.get("momentum_score_up") or 0) >= 70 and cons_is_one(
            r.get("consistency_up") or 0
        ):
            n_impulse += 1
        if float(r.get("momentum_score_down") or 0) >= 70 and cons_is_one(
            r.get("consistency_down") or 0
        ):
            n_impulse += 1
    uni = len({r.get("symbol") for r in rows if r.get("symbol")})
    universe_dev = abs(uni - baseline) / max(baseline, 1)
    data_stress = live_ratio < cfg.live_ratio_floor
    # Only treat universe stress on near-full scans (unit tests / partial
    # boards must not freeze CONFIRMED). Spec: |N-526|/526 > 10%.
    universe_stress = uni >= int(baseline * 0.5) and universe_dev > UNIVERSE_DEV_STRESS
    return {
        "n_impulse": n_impulse,
        "live_ratio": live_ratio,
        "universe_dev": universe_dev,
        "universe_count": uni,
        "g1_unique": len(g1_syms),
        "freeze_new_confirm": bool(data_stress or universe_stress),
        "low_breadth": n_impulse < cfg.n_impulse_floor,
        "data_stress": data_stress,
        "universe_stress": universe_stress,
    }


def occupancy_from_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    sides = {"WATCH": 0, "QUALIFIED": 0, "CONFIRMED": 0, "ELIMINATED": 0}
    conf_syms: set[str] = set()
    qual_syms: set[str] = set()
    ready: list[dict[str, Any]] = []
    for r in rows:
        for d, sk in (("up", "state_up"), ("down", "state_down")):
            st = r.get(sk)
            if st in sides:
                sides[st] += 1
            if st == "CONFIRMED":
                conf_syms.add(r["symbol"])
            if st == "QUALIFIED":
                qual_syms.add(r["symbol"])
                flag = r.get(f"ready_confirm_{d}")
                if flag:
                    ready.append(
                        {
                            "symbol": r["symbol"],
                            "direction": d,
                            "score": r.get(f"score_{d}"),
                        }
                    )
    return {
        "sides": sides,
        "confirmed_unique": len(conf_syms),
        "qualified_unique": len(qual_syms),
        "ready_confirm": ready,
    }


def transition_one(
    st: SymState,
    *,
    score: float,
    ss: float,
    momentum: float,
    consistency: float,
    dq: float,
    supply_missing: bool,
    hard_fail: bool,
    cfg: StateConfig,
    scan_id: str,
    last_price: Optional[float] = None,
    freeze_new_confirm: bool = False,
    data_mode: str = "LIVE",
    hard_pass: Optional[bool] = True,
) -> tuple[SymState, Optional[str]]:
    """Return updated state and transition reason if changed.

    ``hard_pass`` carries 计划 §6.3 N1 (``liquidity_hard_pass == true``). It is a
    *necessary* condition for QUALIFIED→CONFIRMED: an unknown/None G1 verdict
    (e.g. ``--skip-gate1``) must never mint a CONFIRMED offer.
    """
    prev = st.state
    reason = None

    q_path = path_qualified(score, ss, momentum, consistency, supply_missing, cfg)
    c_path = path_confirmed(
        score, ss, momentum, consistency, dq, supply_missing, cfg, data_mode=data_mode
    )

    # hard fail / data
    if hard_fail:
        if st.state != "ELIMINATED":
            _enter_state(st, "ELIMINATED")
            st.consecutive_pass = 0
            st.consecutive_fail = 0
            st.last_path = None
            reason = "HARD_FAIL"
        st.last_score = score
        st.last_scan_id = scan_id
        return st, reason

    if supply_missing and st.state in ("NONE", "WATCH", "QUALIFIED", "CONFIRMED"):
        if st.state == "CONFIRMED":
            _enter_state(st, "QUALIFIED", last_price)
            st.last_path = q_path
            reason = "SUPPLY_MISSING_DEMOTE"
        elif st.state == "NONE":
            _enter_state(st, "DATA_INSUFFICIENT")
            reason = "SUPPLY_MISSING"

    if st.state == "DATA_INSUFFICIENT" and not supply_missing:
        _enter_state(st, "NONE")
        reason = "DATA_RECOVERED"

    if st.state == "CONFIRMED" and (
        dq < cfg.dq_confirm_floor or (data_mode or "") == "MISSING"
    ):
        _enter_state(st, "QUALIFIED", last_price)
        st.consecutive_pass = 0
        st.consecutive_fail = 0
        st.last_path = q_path
        reason = "DQ_DEMOTE"
        st.last_score = score
        st.last_scan_id = scan_id
        return st, reason

    # Hard staircase invariant: legacy/non-structural CONFIRMED sides are invalid
    # immediately and must fall back to QUALIFIED before any DMR selection.
    if cfg.selection_semantics == "staircase-v1.4" and st.state == "CONFIRMED" and ss < cfg.ss_confirmed:
        _enter_state(st, "QUALIFIED", last_price)
        st.consecutive_pass = 0
        st.consecutive_fail = 0
        st.last_path = q_path
        reason = "STAIRCASE_MISSING_DEMOTE"
        st.last_score = score
        st.last_scan_id = scan_id
        return st, reason

    def _pass_watch() -> bool:
        return pass_watch(score, cfg)

    def _pass_qualified() -> bool:
        return q_path is not None

    def _pass_confirmed() -> bool:
        return c_path is not None

    def _hold() -> bool:
        return hold_ok(score, ss, momentum, consistency, cfg)

    # update streak
    if st.state in ("NONE", "DATA_INSUFFICIENT", "ELIMINATED"):
        if _pass_watch():
            st.consecutive_pass += 1
            st.consecutive_fail = 0
        else:
            st.consecutive_pass = 0
    elif st.state == "WATCH":
        # frozen v1.2 WATCH counters
        if _pass_qualified():
            st.consecutive_pass += 1
            st.consecutive_fail = 0
        elif score < cfg.exit_watch:
            st.consecutive_fail += 1
            st.consecutive_pass = 0
        else:
            st.consecutive_fail = 0
            if _pass_watch():
                st.consecutive_pass = max(st.consecutive_pass, 1)
    elif st.state == "QUALIFIED":
        if _pass_confirmed():
            st.consecutive_pass += 1
            st.consecutive_fail = 0
        elif score < cfg.exit_qualified:
            st.consecutive_fail += 1
            st.consecutive_pass = 0
        else:
            st.consecutive_fail = 0
    elif st.state == "CONFIRMED":
        if not _hold():
            st.consecutive_fail += 1
            st.consecutive_pass = 0
        else:
            st.consecutive_fail = 0
            st.consecutive_pass += 1

    dwell = dwell_minutes(st)

    # transitions (time gate first) — at most one level per call
    if st.state in ("NONE", "DATA_INSUFFICIENT", "ELIMINATED"):
        need = cfg.min_streak_watch
        if st.consecutive_pass >= need and _pass_watch():
            _enter_state(st, "WATCH", last_price)
            st.consecutive_pass = 0
            st.last_path = None
            reason = "ENTER_WATCH"
    elif st.state == "WATCH":
        if st.consecutive_fail >= 2 and score < cfg.exit_watch:
            _enter_state(st, "ELIMINATED")
            st.consecutive_fail = 0
            st.last_path = None
            reason = "EXIT_WATCH"
        elif (
            dwell >= cfg.min_dwell_watch
            and st.consecutive_pass >= cfg.min_streak_qualified
            and _pass_qualified()
        ):
            _enter_state(st, "QUALIFIED", last_price)
            st.consecutive_pass = 0
            st.last_path = q_path
            reason = "ENTER_QUALIFIED"
    elif st.state == "QUALIFIED":
        if st.consecutive_fail >= 2 and score < cfg.exit_qualified:
            _enter_state(st, "WATCH", last_price)
            st.consecutive_fail = 0
            st.last_path = None
            reason = "DEMOTE_QUALIFIED"
        elif (
            not freeze_new_confirm
            and hard_pass is True  # N1: liquidity_hard_pass must be a hard True
            and dwell >= cfg.min_dwell_qualified
            and st.consecutive_pass >= cfg.min_streak_confirmed
            and _pass_confirmed()
        ):
            _enter_state(st, "CONFIRMED", last_price)
            st.consecutive_pass = 0
            st.confirmed_count += 1
            st.last_path = c_path
            reason = "ENTER_CONFIRMED"
    elif st.state == "CONFIRMED":
        if st.consecutive_fail >= 2:
            _enter_state(st, "QUALIFIED", last_price)
            st.consecutive_fail = 0
            st.last_path = q_path
            reason = "DEMOTE_CONFIRMED"

    st.last_score = score
    st.last_scan_id = scan_id
    if st.state == "QUALIFIED" and q_path:
        st.last_path = q_path
    elif st.state == "CONFIRMED" and c_path:
        st.last_path = c_path
    if prev != st.state and reason is None:
        reason = f"{prev}->{st.state}"
    return st, reason if prev != st.state else None


def default_state_config() -> StateConfig:
    """Production dual-path defaults; SM_FAST=1 loosens dwell/streak for lab demos only."""
    if os.environ.get("SM_FAST") in ("1", "true", "yes"):
        return StateConfig(
            min_streak_watch=1,
            min_streak_qualified=1,
            min_streak_confirmed=1,
            min_dwell_watch=0,
            min_dwell_qualified=0,
            min_dwell_confirmed=0,
            dq_confirm_floor=50,
        )
    return StateConfig()


def apply_state_machine(
    rows: list[dict[str, Any]],
    store: StateMachineStore,
    cfg: Optional[StateConfig] = None,
    scan_id: str = "",
    now_ts: Optional[float] = None,
) -> list[dict[str, Any]]:
    cfg = cfg or default_state_config()
    if now_ts is not None:
        set_clock(float(now_ts))
    flags = scan_control_flags(rows, cfg=cfg)
    freeze = bool(flags.get("freeze_new_confirm"))
    transitions = []
    for r in rows:
        sym = r["symbol"]
        for direction, score_key, ss_key, mom_key, cons_key in (
            (
                "up",
                "score_up",
                "ss_up",
                "momentum_score_up",
                "consistency_up",
            ),
            (
                "down",
                "score_down",
                "ss_down",
                "momentum_score_down",
                "consistency_down",
            ),
        ):
            st = store.get(sym, direction)
            from_state = st.state
            hard = r.get("liquidity_hard_pass") is False
            px = r.get("last_price")
            if px is None:
                px = r.get("mark_price")
            score = float(r.get(score_key) or 0)
            ss = float(r.get(ss_key) or 0)
            mom = float(r.get(mom_key) or 0)
            cons = float(r.get(cons_key) or 0)
            dq = float(r.get("data_quality_score") or 100)
            supply_missing = bool(r.get("supply_missing"))
            data_mode = str(r.get("data_mode") or "LIVE")
            kwargs = dict(
                score=score,
                ss=ss,
                momentum=mom,
                consistency=cons,
                dq=dq,
                supply_missing=supply_missing,
                hard_fail=hard,
                cfg=cfg,
                scan_id=scan_id,
                last_price=px,
                freeze_new_confirm=freeze,
                data_mode=data_mode,
                hard_pass=(r.get("liquidity_hard_pass") is True),
            )
            # —— 节点幂等守卫（同一个 scan_id 只许推进一次）——
            #
            # consecutive_pass / consecutive_fail 是按**扫描执行次数**累加的，不是按
            # 节点累加。生产跑 `--loop --force`，而 --force 短路了 scan.py 里唯一的
            # 每节点锁（`if not force and not locks.acquire(scan_id)`），
            # data/coin-selection/locks/ 至今一个文件都没落过。于是一次重启就会把同一个
            # scan_id 再跑一遍，连击 +2：本该跨两个 15 分钟节点、看两次**独立**行情才
            # 确认的币，一个节点就进了确认区。
            #
            # 实测 20260901-004：第一次执行 conf_unique=0（连击挡住了），01:02:08Z 重启后
            # 第二次执行同一节点 conf_unique=8 —— 比 75 分钟下限早了整整一个节点。
            #
            # 复盘账本早有同款水位线守卫（review_ledger.py 的 `sid <= prev_sid` ⇒
            # duplicate），状态机缺的就是这一道。
            #
            # 守卫放在这里、而不是 transition_one 内部：下面 SM_FAST 的级联是**刻意**用
            # 同一个 scan_id 连调 4 次的，放到下层会把它一并杀掉（且没有任何测试会红）。
            replayed = bool(scan_id) and st.last_scan_id == scan_id

            # Production: one level per scan. SM_FAST: cascade up to CONFIRMED in-lab.
            reasons = []
            sm_fast = os.environ.get("SM_FAST") in ("1", "true", "yes")
            for _ in range(0 if replayed else (4 if sm_fast else 1)):
                st, reason = transition_one(st, **kwargs)
                if reason:
                    reasons.append(reason)
                else:
                    break
                if st.state in ("WATCH", "QUALIFIED") and sm_fast:
                    st.consecutive_pass = max(st.consecutive_pass, 1)
            reason = reasons[-1] if reasons else None
            q_path = path_qualified(score, ss, mom, cons, supply_missing, cfg)
            c_path = path_confirmed(
                score, ss, mom, cons, dq, supply_missing, cfg, data_mode=data_mode
            )
            ready = (
                st.state == "QUALIFIED"
                and c_path is not None
                and (r.get("liquidity_hard_pass") is True)
                and (
                    dwell_minutes(st) < cfg.min_dwell_qualified
                    or st.consecutive_pass < cfg.min_streak_confirmed
                )
            )
            if direction == "up":
                r["state_up"] = st.state
                r["state_up_dwell_min"] = dwell_minutes(st)
                r["state_up_streak"] = st.consecutive_pass
                r["state_up_enter_price"] = st.state_enter_price
                r["qualified_path_up"] = q_path
                r["confirmed_path_up"] = (
                    (st.last_path or c_path) if st.state == "CONFIRMED" else None
                )
                r["ready_confirm_up"] = ready
            else:
                r["state_down"] = st.state
                r["state_down_dwell_min"] = dwell_minutes(st)
                r["state_down_streak"] = st.consecutive_pass
                r["state_down_enter_price"] = st.state_enter_price
                r["qualified_path_down"] = q_path
                r["confirmed_path_down"] = (
                    (st.last_path or c_path) if st.state == "CONFIRMED" else None
                )
                r["ready_confirm_down"] = ready
            if reason:
                transitions.append(
                    {
                        "symbol": sym,
                        "direction": direction,
                        "from_state": from_state,
                        "to_state": st.state,
                        "reason": reason,
                        "score": st.last_score,
                        "path": reasons,
                        "confirm_path": st.last_path,
                    }
                )
    r0 = rows[0] if rows else None
    if r0 is not None:
        r0.setdefault("_sm_control", flags)
    store.save()
    return transitions


#: 展示区取值：DMR 是 CONFIRMED 的派生精选（读 dmr_selected），其余读 final_zone。
#: 与复盘账本 review_replay 的 zone 谓词同源。
def display_zone_of(row: dict[str, Any]) -> Optional[str]:
    """一行（板面 pool 行）在界面上**实际显示**的分区。

      1. ``dmr_selected`` 为真 ⇒ ``DMR``（CONFIRMED 的派生精选，文档A §5）
      2. ``final_zone``（主导层最终分区 —— 板面 state 的来源）
      3. ``state``（主导层关闭时的回退；主榜走这一支，行为不变）
    """
    if bool(row.get("dmr_selected")):
        return "DMR"
    fz = row.get("final_zone")
    if fz:
        return str(fz)
    st = row.get("state")
    return str(st) if st else None


def stamp_display_zone(
    store: "StateMachineStore",
    pools: Iterable[Iterable[dict[str, Any]]],
    *,
    now_ts: Optional[float] = None,
) -> int:
    """给「展示区」打进入时刻 / 进入价，并覆写行上的三列。

    展示区一变（含 DMR 的每一次进出）就重新打戳 —— 这正是「退出后重新进入必须把
    进入时间刷新为当前时间」的要求；展示区不变则保持原戳，停留时长自然累加。

    **只写 ``zone_enter_*`` 三个新字段，绝不碰 ``state_enter_*``**：复盘账本
    (``review_replay.stay_px``) 把 ``state_enter_price`` 当入场价来源之一，覆写它
    会直接改写《复盘选币》的入场价与盈亏。前端读 ``zone_enter_*``，缺失时回退
    ``state_enter_*``（主榜与主导层关闭时就是这条回退路径）。

    返回本轮重新打戳的边数。
    """
    ts = floor_to_node(_now_ts() if now_ts is None else float(now_ts))
    stamped = 0
    for pool in pools:
        for r in pool:
            sym = r.get("symbol")
            d = r.get("direction")
            if not sym or d not in ("up", "down"):
                continue
            zone = display_zone_of(r)
            if zone is None:
                continue
            st = store.get(sym, d)
            if st.disp_zone is None:
                # —— 首次遇见这条边（新字段上线 / 周期重置后 / 新币）——
                #
                # 不能一律打「现在」：那会让**全部**行在升级后的第一个节点集体
                # 归零（实测 20260904-044 一次性把 1044 行的停留都清成 0）。
                # 展示区与状态机分区一致时，旧戳本来就是对的，直接继承；
                # 只有两者已经不一致（正是本次要修的那类行）才没有可信旧值，
                # 此时才用当前节点。
                inherit = bool(st.state) and zone == st.state and st.state_enter_ts > 0
                st.disp_zone = zone
                st.disp_enter_ts = st.state_enter_ts if inherit else ts
                if inherit:
                    st.disp_enter_price = st.state_enter_price
                else:
                    px = r.get("last_price")
                    if px is None:
                        px = r.get("ref_price")
                    st.disp_enter_price = _as_price(px)
                stamped += 1
            elif st.disp_zone != zone:
                # 进入了一个**新的**展示区：时刻与价格一起重置。
                st.disp_zone = zone
                st.disp_enter_ts = ts
                px = r.get("last_price")
                if px is None:
                    px = r.get("ref_price")
                st.disp_enter_price = _as_price(px)
                stamped += 1
            # **另立三个字段，绝不覆写 state_enter_***：
            # review_replay.stay_px 把 ``state_enter_price`` 当入场价来源之一，
            # 覆写它会直接改写《复盘选币》的入场价与盈亏（实测已污染 42 笔）。
            enter = datetime.fromtimestamp(st.disp_enter_ts, tz=timezone.utc)
            r["zone_enter_time_utc"] = enter.isoformat().replace("+00:00", "Z")
            r["zone_duration_minutes"] = max(0.0, (ts - st.disp_enter_ts) / 60.0)
            r["zone_enter_price"] = st.disp_enter_price
    return stamped


def backfill_enter_prices_from_snapshots(
    store: StateMachineStore,
    snapshots_dir: Path,
) -> int:
    """Fill missing 观察/符合/确认 enter prices from historical board snapshots.

    Uses the first board print of the *current* state whose scan time is at/after
    ``state_enter_ts`` (fallback: closest matching print). Does not overwrite a
    price already stamped by a live transition.
    """
    targets = [
        st
        for st in store.states.values()
        if st.state in _LISTED_STATES and st.state_enter_price is None
    ]
    if not targets or not snapshots_dir.is_dir():
        return 0

    need = {(st.symbol, st.direction, st.state) for st in targets}
    enter_ts = {(st.symbol, st.direction, st.state): st.state_enter_ts for st in targets}
    # earliest print at/after enter; fallback = earliest print of that state
    found: dict[tuple[str, str, str], tuple[float, float]] = {}
    fallback: dict[tuple[str, str, str], tuple[float, float]] = {}
    boards = sorted(
        p
        for p in snapshots_dir.glob("*.json")
        if not p.name.endswith(".full.json")
    )
    for p in boards:
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        meta = obj.get("meta") or {}
        ts_raw = meta.get("scan_timestamp_utc") or obj.get("generated_at_utc") or ""
        try:
            scan_ts = datetime.fromisoformat(
                str(ts_raw).replace("Z", "+00:00")
            ).timestamp()
        except Exception:
            continue
        for row in (obj.get("long_pool") or []) + (obj.get("short_pool") or []):
            key = (row.get("symbol"), row.get("direction"), row.get("state"))
            if key not in need:
                continue
            px = _as_price(row.get("last_price"))
            if px is None:
                px = _as_price(row.get("ref_price"))
            if px is None:
                continue
            prev_fb = fallback.get(key)
            if prev_fb is None or scan_ts < prev_fb[0]:
                fallback[key] = (scan_ts, px)
            entered = float(enter_ts.get(key) or 0.0)
            if entered and scan_ts + 90 < entered:
                continue
            prev = found.get(key)
            if prev is None or scan_ts < prev[0]:
                found[key] = (scan_ts, px)

    n = 0
    for st in targets:
        key = (st.symbol, st.direction, st.state)
        hit = found.get(key) or fallback.get(key)
        if hit is None:
            continue
        st.state_enter_price = hit[1]
        n += 1
    return n
