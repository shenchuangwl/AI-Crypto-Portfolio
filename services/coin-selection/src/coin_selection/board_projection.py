"""板面投影：把一轮扫描的 G1–G4 结果，按另一套参数版本重算成第二块板面。

这是「选币榜Y」（param-v2.0.0-screener-y）的落地方式。

为什么是投影，不是再跑一遍扫描
------------------------------
G1–G4 是**取数 + 冻结公式**（流动性 aqv、1h ROC/RSI、CoinGecko 供应量与市值路径、
旋转楼梯 SS）。真正属于「参数版本」的东西——打分权重、各区阈值、停留 / 连击、
DMR 区门槛与 Top-K——全部作用在这些指标**之上**。所以同一轮扫描取到的指标可以
喂给两套参数，得到两块独立板面：

  * 不多打一次 Binance fapi / CoinGecko 接口（额度与限频都不变）
  * 两块板面看到的是**同一时刻同一份行情**，参数差异不会被取数时差污染
  * 主板面（选币榜）的代码路径与产物一个字节都不动

投影出来的每一块板面都有自己完整的闭环：
``<data_dir>/state_machine.json`` · ``snapshots/`` · ``latest.json`` ·
``daily_unique.json`` · 独立 DMR inbox · 独立复盘账本 ``review/ledger.sqlite``。

硬约束
------
* 投影失败只记 WARN。**绝不允许**拖垮 15 分钟主扫描循环（同 P9 复盘账本约定）。
* 投影板面的 DMR inbox 与主 inbox 是不同目录，且其 ``parameter_version`` 不在
  ``DMR_PARAM_WHITELIST`` 内 —— 纸面执行层永远吃不到它。
* 投影只写自己的 ``data_dir``；``variant_settings`` 会强制改写 data_dir / dmr_inbox，
  即使有人在 overrides 里写错也覆盖不到主目录。
"""

from __future__ import annotations

import copy
import json
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .board_variants import (
    BoardVariant,
    mcap_zone_mode_of,
    param_fingerprint,
    variant_settings,
    variant_state_config,
)
from .cycle_reset import maybe_reset
from .daily_ledger import update_daily_unique
from .mcap_dominance import (
    McapDominanceError,
    apply_dominance as md_apply_dominance,
    assert_production_ready,
    is_governed as md_is_governed,
)
from .review_ledger import ingest_scan as ingest_review_scan
from .rule_manifest import (
    FLAG_EFFECTIVE_ZONE,
    FLAG_MCAP_ZONE,
    FLAG_TARGET_WEIGHTS,
    MAPPING_NOT_APPLICABLE,
    RULE_REVISION,
    TARGET_WEIGHTS,
    build_manifest,
    effective_manifest,
    identity_drift,
    semantic_drift,
)
from .state_machine import (
    StateMachineStore,
    apply_state_machine,
    backfill_enter_prices_from_snapshots,
    quantize_enter_times,
)

log = logging.getLogger("coin_selection.board_projection")


def record_onlycoin_batch(board_key: str, data_dir: Path, inbox: Path, scan_id: str) -> dict[str, Any]:
    """Observe the committed Y Top-K batch; never grant execution permission.

    Separate storage protects the existing occupancy history. Query freshness
    detects a failed hook; the supply gate must refuse a stale projection.
    """
    if board_key != 'y':
        return {'status': 'not_applicable'}
    store = None
    try:
        batch = json.loads((inbox / f'{scan_id}.candidates.json').read_text(encoding='utf-8'))
        from .onlycoin_ledger import OnlyCoinLedger
        store = OnlyCoinLedger(data_dir / 'review' / 'onlycoin.sqlite')
        return store.ingest_batch(
            batch, provenance='live_committed',
            available_at=datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        )
    except Exception as exc:
        log.warning('OnlyCoin projection unavailable scan=%s: %s', scan_id, exc)
        return {'status': 'error', 'scan_id': scan_id, 'error': str(exc)}
    finally:
        if store is not None:
            store.close()

#: 显式放行身份漂移的环境变量。值是**逗号分隔的字段名**，必须逐项列出要放行的漂移
#: 字段（不接受 "1" / "all" 这类通配）——放行是一次有名有姓的运维动作，不是开关。
#: 放行结果会写进快照 ``rule_identity.identity_drift_acknowledged``，因此永不静默。
_DRIFT_ACK_ENV = "Y_IDENTITY_DRIFT_ALLOW"


def _drift_ack() -> set[str]:
    """读取被显式放行的身份漂移字段名集合。"""
    raw = os.environ.get(_DRIFT_ACK_ENV, "") or ""
    return {t.strip() for t in raw.split(",") if t.strip()}


#: 状态机运行后写回 row 的字段。历史回放要先清掉，免得旧板面的停留时长被当成新板面的。
_SM_ROW_FIELDS = (
    "state_up_dwell_min",
    "state_up_streak",
    "state_up_enter_price",
    "qualified_path_up",
    "confirmed_path_up",
    "ready_confirm_up",
    "state_down_dwell_min",
    "state_down_streak",
    "state_down_enter_price",
    "qualified_path_down",
    "confirmed_path_down",
    "ready_confirm_down",
    "_sm_control",
)


def prepare_v13_rows(rows: list[dict[str, Any]], *, scan_id: str, now_ms: int) -> list[dict[str, Any]]:
    """Recompute G4 from node-bound PIT OHLCV; never read a mutable cache.

    `_v13_pit` is an explicit caller-supplied raw artifact, not a claim that
    legacy snapshots contain it. Missing/stale/gapped input blocks the node.
    """
    import math
    from .gate4 import evaluate_symbol, result_to_dict
    out = copy.deepcopy(rows)
    for row in out:
        pit = row.get('_v13_pit') or {}
        def incomplete(reason: str) -> None:
            raise ValueError(f"INPUT_INCOMPLETE:{row.get('symbol')}:{reason}")
        if pit.get('scan_id') != scan_id or pit.get('decision_time_ms') != now_ms:
            incomplete('node_identity')
        bars = pit.get('bars') or []
        if len(bars) < 48:
            incomplete('48_closed_bars_required')
        # Reject future data rather than silently slicing a contemporary cache.
        for b in bars:
            if not isinstance(b, dict) or not isinstance(b.get('close_time_ms'), int):
                incomplete('bar_schema')
            if b['close_time_ms'] >= now_ms:
                incomplete('unclosed_or_future_bar')
        bars = bars[-48:]
        for i, b in enumerate(bars):
            if b.get('open_time_ms') != b['close_time_ms'] - 3599999:
                incomplete('not_1h')
            if i and b['close_time_ms'] - bars[i-1]['close_time_ms'] != 3600000:
                incomplete('gap_or_duplicate')
            try:
                vals = {k: float(b[k]) for k in ('o','h','l','c','v')}
            except (KeyError, TypeError, ValueError):
                incomplete('ohlcv_missing')
            if not all(math.isfinite(v) for v in vals.values()) or min(vals[k] for k in ('o','h','l','c')) <= 0 or vals['v'] < 0:
                incomplete('ohlcv_invalid')
            if vals['h'] < max(vals['o'], vals['c'], vals['l']) or vals['l'] > min(vals['o'], vals['c']):
                incomplete('ohlcv_range')
        if bars[-1]['close_time_ms'] != (now_ms // 3600000) * 3600000 - 1:
            incomplete('stale_window')
        result = result_to_dict(evaluate_symbol(row['symbol'], {k: [float(b[k]) for b in bars] for k in ('o','h','l','c','v')}))
        row.update({k: v for k, v in result.items() if k not in ('symbol', 'reason')})
        row['g4_input_contract'] = 'pit-48-closed-1h-v1'
        row['g4_input_scan_id'] = scan_id
    return out


def rows_from_gates(
    variant: BoardVariant,
    base_settings: Any,
    *,
    symbols: list[dict[str, Any]],
    prices: dict[str, Any],
    g1: dict[str, Any],
    g2: dict[str, Any],
    g3: dict[str, Any],
    g4: dict[str, Any],
    mcap_tf: Optional[dict[str, Any]] = None,
    long_ret: Optional[dict[str, Any]] = None,
) -> list[dict[str, Any]]:
    """实时路径：用变体自己的权重，把本轮 G1–G4 指标重新组装成 rows。"""
    from .scan import build_rows  # 延迟导入：scan 会反过来调用本模块

    settings = variant_settings(base_settings, variant)
    return build_rows(
        symbols, prices, g1, g2, g3, g4, settings, mcap_tf=mcap_tf, long_ret=long_ret
    )


def rows_from_snapshot(
    variant: BoardVariant,
    base_settings: Any,
    rows_raw: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """回放路径：拿历史 ``*.full.json`` 里的 rows，按变体权重重算 Score。

    历史 rows 里已经带着**主板面**状态机跑完的结果（state_up / 停留 / 入点价…）。
    这些字段必须清空后由变体自己的状态机重新写，否则 v2.0.0 的账本里会混进
    v1.4.0 的停留时长——那就不是复盘 v2.0.0，而是给 v1.4.0 换了个标签。

    G1–G4 的原始指标（aqv / ROC / SS / 市值动量 / 数据质量）原样保留：它们是取数
    结果，不属于参数版本。
    """
    from .scan import composite_score  # 延迟导入，理由同上

    settings = variant_settings(base_settings, variant)
    out: list[dict[str, Any]] = []
    for raw in rows_raw:
        r = copy.deepcopy(raw)
        for f in _SM_ROW_FIELDS:
            r.pop(f, None)
        liq = float(r.get("liquidity_score_abs") or 0)
        dq = float(r.get("data_quality_score") or 80)
        r["score_up"] = composite_score(
            settings,
            ss=float(r.get("ss_up") or 0),
            mom=float(r.get("momentum_score_up") or 50),
            liq=liq,
            mcap=float(r.get("mcap_momentum_score_up") or 50),
            cons=float(r.get("consistency_up") or 0),
            dq=dq,
        )
        r["score_down"] = composite_score(
            settings,
            ss=float(r.get("ss_down") or 0),
            mom=float(r.get("momentum_score_down") or 50),
            liq=liq,
            mcap=float(r.get("mcap_momentum_score_down") or 50),
            cons=float(r.get("consistency_down") or 0),
            dq=dq,
        )
        # 临时态由状态机覆写；这里清空只是防止 skip_sm 分支拿旧板面的状态当结果。
        r["state_up"] = "NONE"
        r["state_down"] = "NONE"
        out.append(r)
    return out


def seed_state_machine(variant: BoardVariant, source: Path, *, root: Optional[Path] = None) -> bool:
    """首跑对齐：把主板面当刻的状态机拷给变体，作为它的起点。

    v2.0.0 现在是 v1.4.0 的 100% 复刻，但状态机是**有记忆的**——冷启动的选币榜Y
    第一轮只会有 WATCH，要十几个节点才追上选币榜。那会让「两榜结果一致」在最需要
    被验证的第一天不成立。所以首跑（且仅首跑，文件已存在就不动）继承一次主板面状态。

    等 v2.0.0 参数真正改动之后，两边的状态自然分叉——继承的只是那一刻的起点，
    不是持续同步。
    """
    target = variant.data_path(root) / "state_machine.json"
    if target.exists():
        return False
    if not Path(source).is_file():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(source), str(target))
    log.info("board %s seeded state_machine from %s", variant.key, source)
    return True


def adopt_enter_times(
    variant: BoardVariant,
    reference: Path,
    *,
    root: Optional[Path] = None,
) -> dict[str, int]:
    """把「什么时候进的这个区」从参考状态机补进变体的状态机。

    历史回放只能看到磁盘上现存的最早一个节点。在那之前就已经处于某个区的币，
    回放无从知道它究竟是什么时候进去的，只能把回放起点当作入区时刻 —— 于是
    「停留时长」被系统性地低估，而停留是 min_dwell 闸门的输入。

    这里只补两件事：``state_enter_ts`` 与 ``state_enter_price``，且**仅当两边
    当前处于同一个状态时**才补。状态本身一个字都不改：不同状态说明两套参数
    真的分叉了，那正是要保留的信息，不是要抹平的噪声。

    参考源通常是主板面的 ``state_machine.json``。等 v2.0.0 参数真正改动之后，
    状态会自然分叉，能被补齐的条目也就自然变少 —— 这个函数不会把分叉盖住。
    """
    ref_path = Path(reference)
    target = variant.data_path(root) / "state_machine.json"
    if not ref_path.is_file() or not target.is_file():
        return {"adopted": 0, "skipped_state_mismatch": 0, "missing": 0}
    ref = json.loads(ref_path.read_text(encoding="utf-8")).get("states") or {}
    doc = json.loads(target.read_text(encoding="utf-8"))
    states = doc.get("states") or {}
    adopted = mismatch = missing = 0
    for key, st in states.items():
        src = ref.get(key)
        if src is None:
            missing += 1
            continue
        if src.get("state") != st.get("state"):
            mismatch += 1
            continue
        changed = False
        # 只补缺，不覆盖：变体自己已经有入区戳，说明占用发生在回放窗口内，
        # 是它自己的入选时刻。主板面同状态但不同入点（例如后来又进了一次）
        # 绝不能写过来 —— 实测选币榜X 6 笔 OPEN 停留价被主板覆盖。
        if (not st.get("state_enter_ts")) and src.get("state_enter_ts"):
            st["state_enter_ts"] = src["state_enter_ts"]
            changed = True
        if st.get("state_enter_price") is None and src.get("state_enter_price") is not None:
            st["state_enter_price"] = src["state_enter_price"]
            changed = True
        if changed:
            adopted += 1
    if adopted:
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(target)
    log.info(
        "board %s adopted enter stamps: %s (state mismatch %s, absent in reference %s)",
        variant.key,
        adopted,
        mismatch,
        missing,
    )
    return {"adopted": adopted, "skipped_state_mismatch": mismatch, "missing": missing}


def project_board(
    variant: BoardVariant,
    base_settings: Any,
    rows: list[dict[str, Any]],
    *,
    anchor: datetime,
    scan_id: str,
    seq: int,
    now: datetime,
    universe_count: int,
    uni_ver: str,
    stats: dict[str, Any],
    generated_at: Optional[datetime] = None,
    root: Optional[Path] = None,
    seed_state_from: Optional[Path] = None,
    write_full: bool = False,
    ingest_ledger: bool = True,
    backfill_prices: bool = True,
    skip_sm: bool = False,
    force_cycle_reset: bool = False,
) -> dict[str, Any]:
    """把 rows 变成一块完整的变体板面并落盘（含复盘账本增量）。

    与 ``run_scan_cycle`` 的板面产出段落走**同一批函数**
    （``build_screener_snapshot`` / ``build_dmr_messages`` / ``rank_dmr_inbox`` /
    ``decorate_board``），差别只有传进去的 settings 与 StateConfig。
    """
    from .scan import (  # 延迟导入，避免 scan ↔ board_projection 循环
        _atomic_write,
        build_dmr_messages,
        build_screener_snapshot,
        decorate_board,
        rank_dmr_inbox,
    )

    settings = variant_settings(base_settings, variant, root=root)

    data_dir = Path(settings.data_dir or ".")
    data_dir.mkdir(parents=True, exist_ok=True)
    snaps = data_dir / "snapshots"
    snaps.mkdir(parents=True, exist_ok=True)

    if seed_state_from is not None:
        seed_state_machine(variant, Path(seed_state_from), root=root)

    # —— 时间区（24h 周期）——
    #
    # 必须排在状态机之前：重置会重写 state_machine.json，晚一步就会把刚跑完的
    # 本节点结果一起清掉。幂等键是周期标识，同一周期内重复扫描只清一次。
    #
    # 「选币榜」的 cycle.enabled=False，这里只会拿到 {"enabled": False}，
    # 一个文件都不碰 —— 它跨天连续持有的行为分毫未变。
    cycle_meta = maybe_reset(
        variant,
        data_dir=data_dir,
        now=now,
        scan_id=scan_id,
        parameter_version=settings.parameter_version,
        force=force_cycle_reset,
    )
    # 周期重建窗口内可以用一套更短的时间门槛（默认关闭，且只允许改 min_dwell_*/min_streak_*）。
    cfg = variant_state_config(
        variant, warmup=bool((cycle_meta.get("warmup") or {}).get("active"))
    )

    # —— 不可变规则身份（ChatGpt_SOL5.6 文档A §13.1 / 文档B §3.1–§3.2）——
    #
    # 一致性等式的左半边：同一 input + 同一身份 ⇒ 同一 Score / base / effective / DMR。
    # 三个功能开关（文档A §17.2）由**已解析的有效配置**推导，不另设第二个入口：
    #   ENABLE_MCAP_ZONE            天花板层 mode ∈ {shadow, on}
    #   ENABLE_MCAP_EFFECTIVE_ZONE  天花板层 mode == on（ceiling 影响展示与 DMR）
    #   ENABLE_TARGET_WEIGHTS       已解析权重 == 文档A §6.1 的目标七权重
    zone_mode_cfg = mcap_zone_mode_of(settings)
    target_weights_on = all(
        abs(float(getattr(settings, k, 0.0) or 0.0) - v) < 1e-9
        for k, v in TARGET_WEIGHTS.items()
    )
    feature_flags = {
        FLAG_MCAP_ZONE: zone_mode_cfg in ("shadow", "on"),
        FLAG_EFFECTIVE_ZONE: zone_mode_cfg == "on",
        FLAG_TARGET_WEIGHTS: bool(target_weights_on),
    }

    transitions: list[dict[str, Any]] = []
    # 展示区打戳需要它；状态机关闭时保持 None，decorate_board 会整段跳过。
    sm = None
    if settings.enable_state_machine and not skip_sm:
        # 文档A §4.2「Y state 身份」：state 文件必须自报**本板面**的参数版本，
        # 不再硬编码主榜 v1.4.0。
        sm = StateMachineStore(
            data_dir / "state_machine.json",
            parameter_version=str(settings.parameter_version or ""),
        )
        if quantize_enter_times(sm):
            sm.save()
        # 历史回放时关掉：冷启动的状态机由 transition 自己盖入点价，
        # 而这个兜底会把整个快照目录读一遍，逐节点调用就是 O(n^2)。
        if backfill_prices and backfill_enter_prices_from_snapshots(sm, snaps):
            sm.save()
        transitions = apply_state_machine(
            rows, sm, cfg, scan_id=scan_id, now_ts=now.timestamp()
        )

    # —— 216 组合天花板层（文档A §4 唯一裁决版，文档B 阶段 1–2）——
    #
    # 排在状态机之后：状态机先按自己的边算出 state，天花板再把它**单向压低**。
    # 它不改变状态机的任何一条边，因此 mode="off" 时本段是恒等映射，Y 板面与
    # 现网逐字节相同（红线第 14 条 / 测试 T11）。
    #
    # —— 失败语义（本轮裁决三：禁止静默降级）——
    #
    # 受 ``mcap_dominance.GOVERNED_PARAMETER_PREFIX`` 约束的板面（= 选币榜Y
    # param-v2.0.0-screener-y）：mode 不是 on、缺授权令牌、映射加载失败、
    # 配置越界 —— 任一发生都 **抛 McapDominanceError**，由 project_board 的调用方
    # 拒绝本板面出数。绝不允许「按 v1.4.0 规则跑却标 v2.0.0」。
    # 不受约束的板面（主榜 v1.4.0）保持原有的「异常只 WARN 并退化为不压制」。
    zone_mode = "off"
    zone_meta: dict[str, Any] = {}
    eff_meta: dict[str, Any] = {}
    ruleset = str(getattr(settings, "mcap_ruleset", "sol5.6") or "sol5.6").lower()
    # 层关闭时显式声明「不适用」而不是 null（文档A §14 的九字段全非空要求）。
    mcap_mapping_version = MAPPING_NOT_APPLICABLE
    mapping_hash = MAPPING_NOT_APPLICABLE
    # —— 唯一开关入口（本轮裁决三·6：删除第二入口）——
    #
    # 原先此处有一条 ``ENABLE_MCAP_ZONE`` 环境变量金丝雀：它能在
    # ``mcap_zone_mode="off"`` 时打开天花板层，而上面的 ``feature_flags`` 已按
    # ``off`` 算完 ⇒ config_hash 不变 ⇒ **身份说谎**（审查报告 §7-P3）。
    # 现已删除。开关只剩 ``overrides.settings.mcap_zone_mode`` 一个，
    # 且必须配 ``mcap_zone_authorization`` 令牌，两者都进 config_hash。
    governed = md_is_governed(settings.parameter_version)
    dom_cfg = None
    if governed:
        # 生产守卫：不满足即抛，调用方拒绝出数（不降级、不冒充 v2.0.0）。
        mapping_probe = None
        try:
            from . import mcap_mapping as _map_probe

            mapping_probe = _map_probe.load(strict=True)
        except Exception as e:  # noqa: BLE001
            raise McapDominanceError(
                f"216 映射加载失败，拒绝以 {settings.parameter_version} 身份出数: {e}"
            ) from e
        dom_cfg = assert_production_ready(
            settings,
            mode=zone_mode_cfg,
            mapping=mapping_probe,
            parameter_version=settings.parameter_version,
        )

    try:
        zone_mode = zone_mode_cfg

        if zone_mode != "off" and ruleset == "sol5.6":
            # —— ChatGpt_SOL5.6 口径（本轮默认）——
            #
            # 文档A §9.4 的方向 ceiling（每侧 12/23/29/53/99）+ §10.1 的合并顺序。
            # 映射加载 fail-closed：不变量/hash 任一不符即抛，由外层 except 退化为 off，
            # 绝不半表运行（文档A §14）。
            from . import mcap_effective as _eff
            from . import mcap_mapping as _map

            mapping = _map.load(strict=True)
            eff_meta = _eff.apply_effective_zone(
                rows, mode=zone_mode, mapping=mapping,
                dmr_ceiling_min=str(getattr(settings, "mcap_dmr_ceiling_min", "DMR") or "DMR"),
            )
            eff_meta = {
                **_eff.meta_block(
                    zone_mode, mapping,
                    dmr_ceiling_min=str(
                        getattr(settings, "mcap_dmr_ceiling_min", "DMR") or "DMR"
                    ),
                ),
                **eff_meta,
            }
            mcap_mapping_version = mapping.mapping_version
            mapping_hash = mapping.mapping_hash
            # —— 主导层：P1/P2/P3 + SS×市值互印证 + W_final/RankKey ——
            #
            # 排在 apply_effective_zone 之后：天花板先做 max() 合并，主导层再施加
            # 谓词与互印证（同样只降不升），最后算排序键。dom_cfg 为 None（未受约束
            # 的板面）时整段跳过，主榜行为逐字节不变。
            if dom_cfg is not None:
                dom_meta = md_apply_dominance(rows, cfg=dom_cfg, mode=zone_mode)
                eff_meta["dominance"] = dom_meta
            if zone_mode == "on":
                # 文档A §10.2「216条件：DMR 区 direction ceiling 必须 DMR」。
                for r in rows:
                    for d in ("up", "down"):
                        if r.get(f"mcap_ceiling_zone_{d}") is not None:
                            r[f"combo_block_dmr_{d}"] = not bool(
                                r.get(f"dmr_ceiling_ok_{d}")
                            )
        elif zone_mode != "off":
            # —— 第二轮 Claude_Opus5 切点式口径，保留为消融基线 ——
            from .mcap_zone import apply_mcap_ceiling, meta_block

            zone_meta = apply_mcap_ceiling(
                rows, mode=zone_mode, settings=settings, cfg=cfg
            )
            zone_meta = {**meta_block(zone_mode, settings), **zone_meta}
    except McapDominanceError:
        # 受约束板面的主导层故障：**向上抛**，绝不降级为 v1.4.0 行为。
        raise
    except Exception as e:  # noqa: BLE001
        if governed:
            # 受约束板面：任何天花板层异常都必须阻断出数（本轮裁决三）。
            raise McapDominanceError(
                f"board {variant.key} 主导层异常，拒绝以 "
                f"{settings.parameter_version} 身份出数: {e}"
            ) from e
        log.warning("board %s mcap zone skipped (degrading to off): %s", variant.key, e)
        zone_mode = "off"
        zone_meta = {}
        eff_meta = {}
        mcap_mapping_version = MAPPING_NOT_APPLICABLE
        mapping_hash = MAPPING_NOT_APPLICABLE
        feature_flags[FLAG_MCAP_ZONE] = False
        feature_flags[FLAG_EFFECTIVE_ZONE] = False

    # 参数指纹：两个栏目同源同参的机器保证（文档B §3.1）。算不出就写 None，
    # 不阻断扫描；但复盘回放遇到 None 必须拒绝出数。
    try:
        p_hash = param_fingerprint(variant, settings, cfg)
    except Exception as e:  # noqa: BLE001
        log.warning("board %s param fingerprint failed: %s", variant.key, e)
        p_hash = None

    # —— RuleManifest：文档A §13.1 的完整不可变身份 ——
    #
    # 与 param_hash 的分工：``param_hash`` 是第二轮落地的**粗粒度**参数指纹
    # （白名单固定，为账本连续性保持不变）；``config_hash`` 是文档A §13.1 要求的
    # 完整有效配置 hash（含三个功能开关与红线冻结段）。两者并存，不互为别名。
    rule_identity: Optional[dict[str, Any]] = None
    try:
        # —— revision 由**生效边界**解析，不由代码常量写死（文档 B §3.3）——
        #
        # 取 effective_from_scan_id <= 本节点 scan_id 的最晚一份已发布 manifest。
        # 于是「发一份 effective_from 落在未来周期边界的 manifest」就等于预约切换：
        # 到点自动生效，不需要改代码，也不需要卡着时间点重启进程。
        published = effective_manifest(variant.key, scan_id, root=root)
        revision = published.rule_revision if published else RULE_REVISION
        manifest = build_manifest(
            rule_revision=revision,
            board_key=variant.key,
            settings=settings,
            state_cfg=cfg,
            cycle=getattr(variant, "cycle", None),
            feature_flags=feature_flags,
            mcap_mapping_version=mcap_mapping_version,
            mapping_hash=mapping_hash,
            dmr_executable=bool(variant.dmr_executable),
            consumable_by_dmr=bool(variant.dmr_executable),
            param_hash=p_hash,
            effective_from_utc=anchor.strftime("%Y-%m-%dT00:00:00Z"),
            effective_from_scan_id=f"{anchor.strftime('%Y%m%d')}-000",
        )
        rule_identity = manifest.identity()
        rule_identity["mcap_ruleset"] = ruleset
        # config_hash / code_commit 永远现算（描述「这一轮实际用了什么」）；
        # 与已发布 manifest 的差异不是错误，而是「发布之后配置或代码动过」的信号，
        # 必须可见（文档 B §8.2 归类 IDENTITY_MISMATCH）。
        if published is not None:
            drift = identity_drift(published, rule_identity)
            rule_identity["published_manifest"] = published.rule_revision
            rule_identity["identity_status"] = "MATCH" if not drift else "DRIFT"
            if drift:
                rule_identity["identity_drift"] = drift
                log.warning(
                    "board %s identity drift vs published %s: %s",
                    variant.key,
                    published.rule_revision,
                    drift,
                )
                # —— 身份契约的硬性消费者（本轮裁决四）——
                #
                # 在此之前 DRIFT 只是一行 warning：板面照常出数，快照上却写着一份
                # 它并没有真正遵守的 rule_revision。对受约束板面（选币榜Y）而言，
                # 这正是「按另一套规则跑却冒充 v2.0.0」，与裁决三同类，必须阻断。
                #
                # 只有**语义**漂移阻断；code_commit 每次部署都变，继续只 WARN。
                sem = semantic_drift(drift)
                acked = _drift_ack()
                unacked = [k for k in sem if k not in acked]
                if sem:
                    rule_identity["identity_drift_semantic"] = sem
                if acked:
                    # 放行也必须留痕：快照上永远看得见「谁放行了哪几项」。
                    rule_identity["identity_drift_acknowledged"] = sorted(acked)
                if governed and unacked:
                    raise McapDominanceError(
                        f"board {variant.key} 身份漂移，拒绝以 "
                        f"{settings.parameter_version} 身份出数：已生效 manifest "
                        f"{published.rule_revision} 与本轮实际身份在 {unacked} 上不一致。"
                        f"（生效边界 {published.effective_from_utc}；"
                        f"确需带漂移出数时用 {_DRIFT_ACK_ENV}="
                        f"{','.join(sem)} 显式放行，放行记录会写进快照。）"
                    )
        else:
            rule_identity["published_manifest"] = None
            rule_identity["identity_status"] = "UNPUBLISHED"
        if settings.enable_state_machine and not skip_sm:
            # 文档A §16：state 保存 manifest 引用，重放时可判断这份记忆属于哪套规则。
            sm.identity = {
                k: rule_identity[k]
                for k in (
                    "rule_revision",
                    "config_hash",
                    "mcap_mapping_version",
                    "mapping_hash",
                    "code_commit",
                    "effective_from_scan_id",
                )
            }
            sm.save()
    except McapDominanceError:
        # 身份漂移阻断：**向上抛**。绝不能被下面那条「不许拖垮投影」吞掉。
        raise
    except Exception as e:  # noqa: BLE001 — 身份层同样不许拖垮投影
        if governed:
            # 受约束板面：算不出身份 = 无法证明自己是 v2.0.0。此前这里会把
            # rule_identity 置 None 后照常出数 —— 那是一块「无身份却挂 v2.0.0
            # 招牌」的快照，与静默降级同类，必须阻断（本轮裁决四）。
            raise McapDominanceError(
                f"board {variant.key} 身份层不可用，拒绝以 "
                f"{settings.parameter_version} 身份出数: {e}"
            ) from e
        log.warning("board %s rule manifest failed: %s", variant.key, e)
        rule_identity = None

    dmr_all = build_dmr_messages(
        rows, settings=settings, anchor=anchor, scan_id=scan_id, seq=seq, now=now, cfg=cfg
    )
    dmr_msgs, dmr_rank = rank_dmr_inbox(dmr_all, top_k=settings.dmr_top_k)
    # DMR 日去重只记录实际写入该板 inbox 的 Top-K 去重结果。
    daily_unique = update_daily_unique(
        data_dir,
        anchor.strftime("%Y-%m-%d"),
        rows,
        dmr_messages=dmr_msgs,
        dmr_inbox=Path(settings.dmr_inbox or data_dir / "dmr_inbox"),
    )

    board = build_screener_snapshot(
        settings,
        anchor=anchor,
        scan_id=scan_id,
        seq=seq,
        now=now,
        rows=rows,
        universe_count=universe_count,
        uni_ver=uni_ver,
        stats=stats,
        transitions=transitions,
        daily_unique=daily_unique,
        generated_at=generated_at,
        cfg=cfg,
        param_hash=p_hash,
        mcap_zone=zone_meta or None,
        mcap_effective=eff_meta or None,
        rule_identity=rule_identity,
    )

    occ, ctrl, alerts = decorate_board(
        board,
        rows=rows,
        dmr_msgs=dmr_msgs,
        dmr_rank=dmr_rank,
        daily_unique=daily_unique,
        settings=settings,
        cfg=cfg,
        # X v1.3.0 shadow 复刻 main v1.4.0 的入区三列，不额外打展示区戳。
        # 复盘读状态机戳；Y/on 原样，未来 X 仅通过 overrides 切 on 演进。
        sm=None if zone_mode == "shadow" else sm,
        now_ts=now.timestamp(),
    )
    # 板面自报家门：前端与账本都靠它区分「这是选币榜还是选币榜Y」。
    board["meta"]["board_key"] = variant.key
    board["meta"]["board_label"] = variant.label
    board["meta"]["board_projected"] = True
    board["meta"]["dmr_executable"] = variant.dmr_executable
    # 时间区状态：前端靠它显示周期起点 / 本周期第几节点 / 倒计时 / 「分区重建中」；
    # 复盘账本靠 reset_at_this_node 给该节点的强制退出打 CYCLE_RESET 旗标。
    board["meta"]["cycle"] = cycle_meta

    _atomic_write(snaps / f"{scan_id}.json", board)
    _atomic_write(data_dir / "latest.json", board)

    inbox = Path(settings.dmr_inbox or (data_dir / "dmr_inbox"))
    inbox.mkdir(parents=True, exist_ok=True)
    # —— 执行身份下发（文档A §14 / 文档B §18.6）——
    #
    # adapter 与 executor 的正向 allow-only 谓词要求 **batch 与 candidate 双方**
    # 都带 board_key / dmr_executable / consumable_by_dmr + 六个规则身份字段。
    # 缺任何一个都会被拒绝 —— 这正是我们要的：字段缺失 = 不放行。
    exec_identity: dict[str, Any] = {
        "board_key": variant.key,
        "dmr_executable": bool(variant.dmr_executable),
        "consumable_by_dmr": bool(variant.dmr_executable),
    }
    if rule_identity:
        for k in (
            "parameter_version",
            "rule_revision",
            "config_hash",
            "mcap_mapping_version",
            "mapping_hash",
            "code_commit",
        ):
            exec_identity[k] = rule_identity.get(k)
    for _m in dmr_msgs:
        _m.update(exec_identity)
    _atomic_write(
        inbox / f"{scan_id}.candidates.json",
        {
            **exec_identity,
            "scan_id": scan_id,
            "generated_at_utc": board["generated_at_utc"],
            "board_key": variant.key,
            "parameter_version": settings.parameter_version,
            "rule_identity": rule_identity,
            # 参数指纹（文档B §3.1）：inbox 也带一份，复盘回放据此核对 DMR 成员
            # 是不是同一次调参下产生的。
            "param_hash": p_hash,
            "candidates": dmr_msgs,
            "all_confirmed": dmr_all,
            "rank": dmr_rank,
            # 复盘账本要靠 inbox 还原 DMR 区成员；这份 inbox 同时是「不可执行」的声明。
            "consumable_by_dmr": variant.dmr_executable,
            "note": (
                "CONFIRMED only; Top-K unique coins. "
                f"board={variant.key} parameter_version={settings.parameter_version}; "
                "NOT consumable by dmr-adapter (separate inbox + not in DMR_PARAM_WHITELIST)"
            ),
        },
    )

    onlycoin = {'status': 'skipped'}
    if ingest_ledger:
        onlycoin = record_onlycoin_batch(variant.key, data_dir, inbox, scan_id)

    if write_full:
        _atomic_write(
            snaps / f"{scan_id}.full.json",
            {
                "status": "ok",
                "scan_id": scan_id,
                "board_key": variant.key,
                "parameter_version": settings.parameter_version,
                "universe_count": universe_count,
                "transitions": transitions,
                "dmr_confirmed": len(dmr_all),
                "dmr_inbox": len(dmr_msgs),
                "dmr_rank": dmr_rank,
                "occupancy": occ,
                "control": ctrl,
                "daily_unique": daily_unique,
                "rows": rows,
            },
        )

    ledger = {"status": "skipped"}
    if ingest_ledger:
        try:
            ledger = ingest_review_scan(data_dir, scan_id, inbox_dir=inbox)
        except Exception as e:  # noqa: BLE001 — 账本永远不许拖垮扫描
            log.warning("board %s review ledger ingest skipped: %s", variant.key, e)
            ledger = {"status": "error", "error": str(e)}

    return {
        "board": variant.key,
        "parameter_version": settings.parameter_version,
        "scan_id": scan_id,
        "data_dir": str(data_dir),
        "board_long": len(board["long_pool"]),
        "board_short": len(board["short_pool"]),
        "confirmed_unique": occ.get("confirmed_unique"),
        "dmr_messages": len(dmr_msgs),
        "transitions": len(transitions),
        "alerts": alerts,
        "review_ledger": ledger.get("status"),
        "onlycoin_ledger": onlycoin.get("status"),
        "cycle": cycle_meta,
        "param_hash": p_hash,
        "mcap_zone_mode": zone_mode,
    }


def project_secondary_boards(
    base_settings: Any,
    *,
    symbols: list[dict[str, Any]],
    prices: dict[str, Any],
    g1: dict[str, Any],
    g2: dict[str, Any],
    g3: dict[str, Any],
    g4: dict[str, Any],
    mcap_tf: Optional[dict[str, Any]],
    long_ret: Optional[dict[str, Any]],
    anchor: datetime,
    scan_id: str,
    seq: int,
    now: datetime,
    universe_count: int,
    uni_ver: str,
    stats: dict[str, Any],
    generated_at: Optional[datetime] = None,
    primary_state_path: Optional[Path] = None,
    skip_sm: bool = False,
) -> list[dict[str, Any]]:
    """主扫描的钩子：为每个已启用的次级板面各投影一块（当前只有选币榜Y）。

    单个板面失败只跳过它自己，其余照常；异常永远不冒泡到扫描循环。
    """
    from .board_variants import secondary_variants

    out: list[dict[str, Any]] = []
    for variant in secondary_variants():
        try:
            rows = rows_from_gates(
                variant,
                base_settings,
                symbols=symbols,
                prices=prices,
                g1=g1,
                g2=g2,
                g3=g3,
                g4=g4,
                mcap_tf=mcap_tf,
                long_ret=long_ret,
            )
            res = project_board(
                variant,
                base_settings,
                rows,
                anchor=anchor,
                scan_id=scan_id,
                seq=seq,
                now=now,
                universe_count=universe_count,
                uni_ver=uni_ver,
                stats=stats,
                generated_at=generated_at,
                seed_state_from=primary_state_path,
                skip_sm=skip_sm,
            )
            out.append(res)
            cyc = res.get("cycle") or {}
            log.info(
                "board %s projected scan=%s long=%s conf_unique=%s dmr=%s ledger=%s%s",
                variant.key,
                scan_id,
                res["board_long"],
                res["confirmed_unique"],
                res["dmr_messages"],
                res["review_ledger"],
                (
                    f" cycle={cyc.get('cycle_key')} node={cyc.get('node_in_cycle')}"
                    f"/{cyc.get('nodes_per_cycle')}"
                    + (" RESET" if cyc.get("reset_at_this_node") else "")
                )
                if cyc.get("enabled")
                else "",
            )
        except McapDominanceError as e:
            # 主导层未授权 / 未生效 / 配置越界 —— 本板面**不出数**。
            # 这是刻意的:宁可 Y 板面停更，也不允许它按 v1.4.0 规则跑却标 v2.0.0
            # （本轮裁决三）。快照 / inbox / 账本一个字节都不会写。
            log.error(
                "board %s REFUSED scan=%s (v2.0.0 主导层不可用，拒绝出数): %s",
                variant.key,
                scan_id,
                e,
            )
            out.append(
                {
                    "board": variant.key,
                    "status": "refused",
                    "reason": "MCAP_DOMINANCE_UNAVAILABLE",
                    "error": str(e),
                    "parameter_version": variant.parameter_version,
                }
            )
        except Exception as e:  # noqa: BLE001
            log.warning("board %s projection failed scan=%s: %s", variant.key, scan_id, e)
            out.append({"board": variant.key, "status": "error", "error": str(e)})
    return out
