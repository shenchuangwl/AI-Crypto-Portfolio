"""「选币榜Y」(param-v2.0.0-screener-y) 变体护栏。

三件事必须成立，缺一条这套「独立复刻」就是假的：

  1. **隔离**：Y 的 data_dir / DMR inbox / 复盘账本与选币榜完全分开，
     且 overrides 写错也覆盖不到主目录；Y 的候选不可被 dmr-adapter 消费。
  2. **等价**：overrides 为空时，同一批指标 + 同一份状态机起点，
     Y 投影出的板面与主板面**逐行逐字段**相同（只允许版本 / 板面标识不同）。
  3. **可演进**：往 overrides 里写一个值就能让 Y 的参数真的变，而主板面不变。
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from coin_selection.board_projection import project_board, rows_from_snapshot
from coin_selection.board_variants import (
    BOARD_Y_KEY,
    MAIN_KEY,
    get_variant,
    load_variants,
    main_variant,
    secondary_variants,
    variant_settings,
    variant_state_config,
)
from coin_selection.scan import (
    SelectionSettings,
    build_dmr_messages,
    build_screener_snapshot,
    composite_score,
    decorate_board,
    rank_dmr_inbox,
)
from coin_selection.daily_ledger import update_daily_unique
from coin_selection.state_machine import (
    StateMachineStore,
    apply_state_machine,
    default_state_config,
)

ANCHOR = datetime(2026, 8, 25, 0, 0, 0, tzinfo=timezone.utc)
NOW = datetime(2026, 8, 25, 4, 15, 0, tzinfo=timezone.utc)
SCAN_ID = "20260825-017"
SEQ = 17


def _rows(n: int = 24) -> list[dict]:
    """一批覆盖 淘汰/观察/符合/确认/DMR 全谱的合成行。"""
    out = []
    for i in range(n):
        strength = i / max(n - 1, 1)
        out.append(
            {
                "symbol": f"T{i:02d}USDT",
                "base_asset": f"T{i:02d}",
                "underlying_type": "COIN",
                "last_price": 1.0 + i,
                "mark_price": 1.0 + i,
                "liquidity_hard_pass": True,
                "liquidity_grade": "A",
                "liquidity_score_abs": 40 + 55 * strength,
                "aqv_6d_m": 10.0,
                "aqv_12d_m": 9.0,
                "aqv_26d_m": 8.0,
                "history_days": 30,
                "ret_1h": 0.01,
                "ret_4h": 0.02,
                "ret_24h": 0.03,
                "ret_1w": 0.04,
                "ret_1mo": 0.05,
                "ret_since_anchor": 0.01,
                "momentum_score_up": 30 + 65 * strength,
                "momentum_score_down": 95 - 65 * strength,
                "consistency_up": 1.0 if strength > 0.55 else 0.33,
                "consistency_down": 0.0,
                "mcap_momentum_score_up": 45 + 40 * strength,
                "mcap_momentum_score_down": 50,
                "ss_up": 20 + 70 * strength,
                "ss_down": 10,
                "steps_up": 3 if strength > 0.5 else 1,
                "steps_down": 0,
                "data_quality_score": 95.0,
                "supply_missing": False,
                "circulating_supply": 1e8,
                "market_cap_calculated": 1e8,
                "coingecko_id": f"t{i:02d}",
                "data_mode": "LIVE",
                "mcap_grade_30m": "A",
                "mcap_grade_2h": "B",
                "mcap_grade_6h": "C",
                "score_up": 0.0,
                "score_down": 0.0,
                "state_up": "NONE",
                "state_down": "NONE",
            }
        )
    return out


def _score(rows: list[dict], settings) -> list[dict]:
    for r in rows:
        for d, ss, mom, mc, cons in (
            ("up", "ss_up", "momentum_score_up", "mcap_momentum_score_up", "consistency_up"),
            ("down", "ss_down", "momentum_score_down", "mcap_momentum_score_down", "consistency_down"),
        ):
            r[f"score_{d}"] = composite_score(
                settings,
                ss=float(r[ss]),
                mom=float(r[mom]),
                liq=float(r["liquidity_score_abs"]),
                mcap=float(r[mc]),
                cons=float(r[cons]),
                dq=float(r["data_quality_score"]),
            )
    return rows


def _run_primary(tmp: Path, settings: SelectionSettings, rows: list[dict], cfg=None) -> dict:
    """把 run_scan_cycle 的板面产出段落复现在一个临时目录里（主板面口径）。"""
    data_dir = Path(settings.data_dir)
    (data_dir / "snapshots").mkdir(parents=True, exist_ok=True)
    sm = StateMachineStore(data_dir / "state_machine.json")
    transitions = apply_state_machine(rows, sm, cfg, scan_id=SCAN_ID, now_ts=NOW.timestamp())
    daily = update_daily_unique(data_dir, ANCHOR.strftime("%Y-%m-%d"), rows)
    board = build_screener_snapshot(
        settings,
        anchor=ANCHOR,
        scan_id=SCAN_ID,
        seq=SEQ,
        now=NOW,
        rows=rows,
        universe_count=len(rows),
        uni_ver="uni-test",
        stats={},
        transitions=transitions,
        daily_unique=daily,
        generated_at=NOW,
    )
    dmr_all = build_dmr_messages(
        rows, settings=settings, anchor=ANCHOR, scan_id=SCAN_ID, seq=SEQ, now=NOW, cfg=cfg
    )
    dmr_msgs, dmr_rank = rank_dmr_inbox(dmr_all, top_k=settings.dmr_top_k)
    decorate_board(
        board,
        rows=rows,
        dmr_msgs=dmr_msgs,
        dmr_rank=dmr_rank,
        daily_unique=daily,
        settings=settings,
        cfg=cfg,
    )
    return board


# ---------------------------------------------------------------- 1. 隔离
def test_registry_shape():
    keys = [v.key for v in load_variants()]
    assert MAIN_KEY in keys and BOARD_Y_KEY in keys
    assert main_variant().key == MAIN_KEY
    # X v1.3.0 复刻 main v1.4.0、独立复盘和调参；Y 既有断言不得删除。
    secondary = [v.key for v in secondary_variants()]
    assert BOARD_Y_KEY in secondary and secondary == ["x", BOARD_Y_KEY]

    y = get_variant(BOARD_Y_KEY)
    m = get_variant(MAIN_KEY)
    assert m.parameter_version == "param-v1.4.0-staircase-confirm-dmr"
    assert y.parameter_version == "param-v2.0.0-screener-y"
    # 路由 / API / 数据目录 / inbox / 账本 —— 五条路径没有一条重叠
    assert y.web_route != m.web_route
    assert y.api_prefix != m.api_prefix
    assert y.data_dir != m.data_dir
    assert y.dmr_inbox != m.dmr_inbox
    assert y.ledger_path() != m.ledger_path()
    # 纸面执行层永远吃不到 Y
    assert m.dmr_executable is True
    assert y.dmr_executable is False
    assert y.projected is True


def test_overrides_can_never_redirect_writes_into_the_main_board():
    y = get_variant(BOARD_Y_KEY)
    hijacked = type(y)(**{**y.__dict__, "settings_overrides": {
        "data_dir": "data/coin-selection",
        "dmr_inbox": "data/dmr-adapter/inbox",
        "parameter_version": "param-v1.4.0-staircase-confirm-dmr",
    }})
    s = variant_settings(SelectionSettings(), hijacked)
    assert s.parameter_version == "param-v2.0.0-screener-y"
    assert s.data_dir.endswith("data/coin-selection-y")
    assert s.dmr_inbox.endswith("data/dmr-adapter-y/inbox")


# ---------------------------------------------------------------- 2. 等价
def test_y_refuses_to_run_without_mcap_dominance_authorization():
    """【v2.0.0 新契约】未授权 ⇒ Y **拒绝出数**，不得退回 v1.4.0 行为。

    取代旧的「Y = 主榜逐字段克隆」测试：那条契约建立在
    ``mcap_zone_mode="off"`` 之上，而 v2.0.0 已把三周期流通市值主导层
    定为强制项（本轮裁决三·6）。``off`` / ``shadow`` / 缺令牌一律阻断。
    """
    from coin_selection.mcap_dominance import AUTHORIZATION_TOKEN, McapDominanceError

    y = get_variant(BOARD_Y_KEY)
    tmp = Path(tempfile.mkdtemp(prefix="boardy-refuse-"))
    try:
        for overrides, why in (
            ({}, "mode 缺省 off"),
            ({"mcap_zone_mode": "on"}, "缺授权令牌"),
            ({"mcap_zone_mode": "on", "mcap_zone_authorization": "nope"}, "令牌不符"),
            ({"mcap_zone_mode": "shadow", "mcap_zone_authorization": AUTHORIZATION_TOKEN},
             "shadow 不是生产态"),
        ):
            v = type(y)(**{**y.__dict__, "settings_overrides": overrides, "state_overrides": {}})
            base = SelectionSettings(data_dir=str(tmp / "main"))
            rows = rows_from_snapshot(v, base, _score(_rows(), base))
            try:
                project_board(
                    v, base, rows, anchor=ANCHOR, scan_id=SCAN_ID, seq=SEQ, now=NOW,
                    universe_count=len(rows), uni_ver="uni-test", stats={},
                    generated_at=NOW, root=tmp, seed_state_from=None,
                    ingest_ledger=False, backfill_prices=False,
                )
            except McapDominanceError:
                pass
            else:
                raise AssertionError(f"未阻断: {why} ({overrides})")
            # 阻断必须是**彻底的**：一个字节都不许落盘
            assert not (tmp / v.data_dir / "latest.json").exists(), f"{why}: 竟然写了快照"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _publish_manifest_for(v, base, tmp, **identity_overrides):
    """把一份「已生效」manifest 冻结进 tmp 树，供漂移比对用。"""
    from coin_selection.rule_manifest import build_manifest, publish
    from coin_selection.board_variants import variant_settings, variant_state_config

    s = variant_settings(base, v)
    cfg = variant_state_config(v)
    kw = dict(
        board_key=v.key,
        settings=s,
        state_cfg=cfg,
        cycle=getattr(v, "cycle", None),
        feature_flags={"ENABLE_MCAP_ZONE": True, "ENABLE_MCAP_EFFECTIVE_ZONE": True,
                       "ENABLE_TARGET_WEIGHTS": True},
        mcap_mapping_version="mcap-216-v2.0.0-r1",
        mapping_hash="sha256:" + "0" * 64,
        rule_revision="y-v2.0.0-rTEST",
        effective_from_utc="2026-08-25T00:00:00Z",
        effective_from_scan_id="20260825-000",
        code_commit="0" * 40,
        root=tmp,
    )
    kw.update(identity_overrides)
    m = build_manifest(**kw)
    publish(m, root=tmp)
    return m


def _run_y(v, tmp):
    """跑一轮 Y 投影，返回 project_board 的结果（异常原样抛出）。"""
    base = SelectionSettings(data_dir=str(tmp / "main"))
    rows = rows_from_snapshot(v, base, _score(_rows(), base))
    return project_board(
        v, base, rows, anchor=ANCHOR, scan_id=SCAN_ID, seq=SEQ, now=NOW,
        universe_count=len(rows), uni_ver="uni-test", stats={},
        generated_at=NOW, root=tmp, seed_state_from=None,
        ingest_ledger=False, backfill_prices=False,
    )


def test_y_refuses_to_run_under_semantic_identity_drift():
    """【本轮裁决四】已生效 manifest 与本轮真实身份语义不符 ⇒ Y **拒绝出数**。

    这条钉住的是「身份契约有硬性消费者」：在此之前 identity_status=DRIFT
    只是一行 warning，板面照常出数、快照上却写着一份它并没有遵守的
    rule_revision —— 与「按 v1.4.0 跑却标 v2.0.0」同类，必须阻断。
    """
    from coin_selection.mcap_dominance import AUTHORIZATION_TOKEN, McapDominanceError

    y = get_variant(BOARD_Y_KEY)
    tmp = Path(tempfile.mkdtemp(prefix="boardy-drift-"))
    try:
        v = type(y)(**{**y.__dict__, "settings_overrides": {
            **y.settings_overrides,
            "mcap_zone_mode": "on",
            "mcap_zone_authorization": AUTHORIZATION_TOKEN,
        }})
        # 冻结一份 mapping_hash 与本轮不符的 manifest ⇒ 语义漂移
        _publish_manifest_for(v, SelectionSettings(data_dir=str(tmp / "main")), tmp)
        try:
            _run_y(v, tmp)
        except McapDominanceError as e:
            assert "身份漂移" in str(e), str(e)
        else:
            raise AssertionError("语义身份漂移未阻断")
        # 阻断必须彻底：一个字节都不许落盘
        assert not (tmp / v.data_dir / "latest.json").exists(), "漂移下竟然写了快照"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_code_commit_drift_alone_does_not_block():
    """``code_commit`` 每次部署都变，是「代码动过」而不是「规则不同」⇒ 只 WARN。

    否则每提交一次板面就停更一次（见 fc026c7「消除身份随每次提交漂移」）。
    """
    from coin_selection.mcap_dominance import AUTHORIZATION_TOKEN
    from coin_selection import board_projection as bp
    from coin_selection.rule_manifest import resolve_code_commit

    y = get_variant(BOARD_Y_KEY)
    tmp = Path(tempfile.mkdtemp(prefix="boardy-commitdrift-"))
    try:
        v = type(y)(**{**y.__dict__, "settings_overrides": {
            **y.settings_overrides,
            "mcap_zone_mode": "on",
            "mcap_zone_authorization": AUTHORIZATION_TOKEN,
        }})
        base = SelectionSettings(data_dir=str(tmp / "main"))
        # 先跑一轮拿到本轮真实身份，再据此冻结一份「只有 code_commit 不同」的 manifest
        probe = _run_y(v, tmp)
        snap = json.loads((tmp / v.data_dir / "latest.json").read_text(encoding="utf-8"))
        live = snap["meta"]["rule_identity"]
        shutil.rmtree(tmp / v.data_dir, ignore_errors=True)
        _publish_manifest_for(
            v, base, tmp,
            mcap_mapping_version=live["mcap_mapping_version"],
            mapping_hash=live["mapping_hash"],
            code_commit="f" * 40,          # 唯一的差异
        )
        res = _run_y(v, tmp)               # 不得抛
        snap = json.loads((tmp / v.data_dir / "latest.json").read_text(encoding="utf-8"))
        ri = snap["meta"]["rule_identity"]
        assert ri["identity_status"] == "DRIFT", ri["identity_status"]
        assert ri["identity_drift"] == ["code_commit"], ri.get("identity_drift")
        assert "identity_drift_semantic" not in ri, ri.get("identity_drift_semantic")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_semantic_drift_can_be_explicitly_acknowledged():
    """放行必须**指名道姓**且留痕：env 列出字段名，快照写 acknowledged。"""
    import os
    from coin_selection.mcap_dominance import AUTHORIZATION_TOKEN
    from coin_selection.board_projection import _DRIFT_ACK_ENV

    y = get_variant(BOARD_Y_KEY)
    tmp = Path(tempfile.mkdtemp(prefix="boardy-ack-"))
    prev = os.environ.get(_DRIFT_ACK_ENV)
    try:
        v = type(y)(**{**y.__dict__, "settings_overrides": {
            **y.settings_overrides,
            "mcap_zone_mode": "on",
            "mcap_zone_authorization": AUTHORIZATION_TOKEN,
        }})
        _publish_manifest_for(v, SelectionSettings(data_dir=str(tmp / "main")), tmp)
        os.environ[_DRIFT_ACK_ENV] = "config_hash,mapping_hash,mcap_mapping_version"
        _run_y(v, tmp)                     # 放行后不得抛
        snap = json.loads((tmp / v.data_dir / "latest.json").read_text(encoding="utf-8"))
        ri = snap["meta"]["rule_identity"]
        assert ri["identity_status"] == "DRIFT"
        assert ri.get("identity_drift_acknowledged"), ri
    finally:
        if prev is None:
            os.environ.pop(_DRIFT_ACK_ENV, None)
        else:
            os.environ[_DRIFT_ACK_ENV] = prev
        shutil.rmtree(tmp, ignore_errors=True)


def test_y_projection_equals_the_main_board_field_by_field():
    """投影链路本身仍是逐字段克隆（主导层与权重增量都清空时）。

    这条只钉「投影管道没有偷偷改字段」，**不再**是 v2.0.0 的业务契约：
    现网 Y 必须开主导层，那时板面与主榜本就不同（见上一条与 test_y_weights.py）。
    """
    y = get_variant(BOARD_Y_KEY)
    y = type(y)(**{**y.__dict__, "settings_overrides": {}, "state_overrides": {}})
    # 克隆等价只在主导层关闭时成立；用 main 的参数版本绕开 v2.0.0 守卫。
    y = type(y)(**{**y.__dict__, "parameter_version": "param-v1.4.0-staircase-confirm-dmr"})
    tmp = Path(tempfile.mkdtemp(prefix="boardy-"))
    try:
        main_settings = SelectionSettings(data_dir=str(tmp / "main"))
        main_board = _run_primary(tmp, main_settings, _score(_rows(), main_settings))

        base = SelectionSettings(data_dir=str(tmp / "main"))
        y_rows = rows_from_snapshot(y, base, _score(_rows(), base))
        project_board(
            y,
            base,
            y_rows,
            anchor=ANCHOR,
            scan_id=SCAN_ID,
            seq=SEQ,
            now=NOW,
            universe_count=len(y_rows),
            uni_ver="uni-test",
            stats={},
            generated_at=NOW,
            root=tmp,               # 把 data/coin-selection-y 落在临时目录下
            seed_state_from=None,
            ingest_ledger=False,
            backfill_prices=False,
        )
        y_board = json.loads((tmp / y.data_dir / "latest.json").read_text(encoding="utf-8"))

        # 版本与板面标识本来就该不同，其余必须一模一样
        drop_meta = {"parameter_version", "board_key", "board_label", "board_projected",
                     "dmr_executable"}
        assert y_board["meta"]["parameter_version"] == "param-v1.4.0-staircase-confirm-dmr"
        assert y_board["meta"]["board_key"] == BOARD_Y_KEY
        for k, v in main_board["meta"].items():
            if k in drop_meta:
                continue
            assert y_board["meta"].get(k) == v, f"meta.{k} 不一致: {y_board['meta'].get(k)} != {v}"
        assert y_board["long_pool"] == main_board["long_pool"]
        assert y_board["short_pool"] == main_board["short_pool"]
        assert y_board["transitions"] == main_board["transitions"]
        assert y_board["expires_at_utc"] == main_board["expires_at_utc"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_rows_from_snapshot_reproduces_scores_and_clears_stale_sm_fields():
    y = get_variant(BOARD_Y_KEY)
    # 权重分叉后，回放必须用变体自己的 SelectionSettings 重算 Score，而不是主板分数。
    y_settings = variant_settings(SelectionSettings(), y)
    base = SelectionSettings()
    rows = _score(_rows(6), base)
    expected = _score(_rows(6), y_settings)
    for r in rows:                       # 假装是历史板面：带着上一套状态机的痕迹
        r["state_up"] = "CONFIRMED"
        r["state_up_dwell_min"] = 999.0
        r["state_up_enter_price"] = 0.5
        r["_sm_control"] = {"n_impulse": 1}
    out = rows_from_snapshot(y, base, rows)
    for a, b, e in zip(rows, out, expected):
        assert abs(b["score_up"] - e["score_up"]) < 1e-12
        assert abs(b["score_down"] - e["score_down"]) < 1e-12
        assert "state_up_dwell_min" not in b
        assert "state_up_enter_price" not in b
        assert "_sm_control" not in b
        assert b["state_up"] == "NONE"
        if abs(y_settings.w_mcap - base.w_mcap) > 1e-12:
            assert abs(a["score_up"] - b["score_up"]) > 1e-9 or abs(a["score_down"] - b["score_down"]) > 1e-9
    assert rows[0]["state_up"] == "CONFIRMED"   # 原 list 不被就地改写


# ------------------------------------------------------------ 3. 可演进
def test_overrides_move_y_without_touching_main():
    y = get_variant(BOARD_Y_KEY)
    tuned = type(y)(**{
        **y.__dict__,
        "settings_overrides": {"dmr_top_k": 12, "w_ss": 0.5},
        "state_overrides": {"enter_confirmed": 90, "dmr_score": 95},
    })
    s = variant_settings(SelectionSettings(), tuned)
    cfg = variant_state_config(tuned)
    assert s.dmr_top_k == 12 and abs(s.w_ss - 0.5) < 1e-12
    assert cfg.enter_confirmed == 90 and cfg.dmr_score == 95
    # 主板面纹丝不动
    assert SelectionSettings().dmr_top_k == 16
    assert abs(SelectionSettings().w_ss - 0.30) < 1e-12
    assert default_state_config().enter_confirmed == 66
    assert default_state_config().dmr_score == 70


def test_unknown_override_is_ignored_not_fatal():
    y = get_variant(BOARD_Y_KEY)
    bogus = type(y)(**{**y.__dict__, "settings_overrides": {"no_such_field": 1}})
    s = variant_settings(SelectionSettings(), bogus)
    assert s.parameter_version == "param-v2.0.0-screener-y"


def test_t3_not_confirmed_reasons_follow_the_variant_cfg():
    """缺陷 N1 回归（文档B §2.3 / 测试 T3）。

    ``board_from_rows`` 曾在内部写死 ``default_state_config()``，于是 Y 板面每一行
    的「为什么没进确认」都是用 **v1.4.0 阈值**算的。overrides 全空时无害；一旦
    Y 用上自己的阈值，这一列立刻开始说谎。

    这里用一个 Score 卡在两套 enter_confirmed 之间的行：变体 cfg 必须让它多出
    一条 score 原因，主板 cfg 则不应该有。
    """
    from dataclasses import replace

    from coin_selection.scan import board_from_rows
    from coin_selection.state_machine import default_state_config

    base = default_state_config()          # enter_confirmed = 66
    strict = replace(base, enter_confirmed=80.0, mom_confirmed=90.0)

    row = {
        "symbol": "AAAUSDT",
        "base_asset": "AAA",
        "state_up": "QUALIFIED",
        "state_down": "ELIMINATED",
        "score_up": 70.0,
        "score_down": 10.0,
        "ss_up": 60.0,
        "ss_down": 0.0,
        "momentum_score_up": 70.0,
        "momentum_score_down": 0.0,
        "consistency_up": 1.0,
        "consistency_down": 0.0,
        "mcap_momentum_score_up": 60.0,
        "mcap_momentum_score_down": 40.0,
        "data_quality_score": 90.0,
        "supply_missing": False,
        "liquidity_hard_pass": True,
        "data_mode": "LIVE",
        "state_up_dwell_min": 60,
        "state_up_streak": 3,
        "last_price": 1.0,
    }

    loose = board_from_rows([dict(row)], "up", cfg=base)[0]
    tight = board_from_rows([dict(row)], "up", cfg=strict)[0]

    assert loose["not_confirmed_reasons"] != tight["not_confirmed_reasons"], (
        "变体 cfg 没有透传到 not_confirmed_reasons —— N1 又回来了"
    )
    assert any("score" in r for r in tight["not_confirmed_reasons"]), tight["not_confirmed_reasons"]
    assert not any("score" in r for r in loose["not_confirmed_reasons"]), loose["not_confirmed_reasons"]

    # reason_codes 里的 G2OK / SS2 同样必须跟着 cfg 走，而不是硬编码 60 / 50
    assert "G2OK" in loose["reason_codes"]
    assert "G2OK" not in tight["reason_codes"], tight["reason_codes"]


def test_t3_default_cfg_keeps_the_old_behaviour():
    """不传 cfg 时逐字段等同修复前 —— 主板的产物一个字节都不能变。"""
    from coin_selection.scan import board_from_rows
    from coin_selection.state_machine import default_state_config

    row = {
        "symbol": "BBBUSDT",
        "base_asset": "BBB",
        "state_up": "WATCH",
        "state_down": "WATCH",
        "score_up": 50.0,
        "score_down": 50.0,
        "ss_up": 55.0,
        "ss_down": 55.0,
        "momentum_score_up": 65.0,
        "momentum_score_down": 65.0,
        "consistency_up": 1.0,
        "consistency_down": 1.0,
        "mcap_momentum_score_up": 50.0,
        "mcap_momentum_score_down": 50.0,
        "data_quality_score": 80.0,
        "liquidity_hard_pass": True,
        "data_mode": "LIVE",
        "last_price": 2.0,
    }
    a = board_from_rows([dict(row)], "up")
    b = board_from_rows([dict(row)], "up", cfg=default_state_config())
    assert a == b


def test_t4_param_hash_is_reachable_from_the_variant():
    from coin_selection.board_variants import param_fingerprint
    from coin_selection.scan import SelectionSettings

    y = get_variant(BOARD_Y_KEY)
    h = param_fingerprint(y, variant_settings(SelectionSettings(), y), variant_state_config(y))
    assert h.startswith("pf1_") and len(h) == 20


if __name__ == "__main__":
    test_registry_shape()
    print("ok test_registry_shape")
    test_overrides_can_never_redirect_writes_into_the_main_board()
    print("ok test_overrides_can_never_redirect_writes_into_the_main_board")
    test_y_refuses_to_run_without_mcap_dominance_authorization()
    print("ok test_y_refuses_to_run_without_mcap_dominance_authorization")
    test_y_refuses_to_run_under_semantic_identity_drift()
    print("ok test_y_refuses_to_run_under_semantic_identity_drift")
    test_code_commit_drift_alone_does_not_block()
    print("ok test_code_commit_drift_alone_does_not_block")
    test_semantic_drift_can_be_explicitly_acknowledged()
    print("ok test_semantic_drift_can_be_explicitly_acknowledged")
    test_y_projection_equals_the_main_board_field_by_field()
    print("ok test_y_projection_equals_the_main_board_field_by_field")
    test_rows_from_snapshot_reproduces_scores_and_clears_stale_sm_fields()
    print("ok test_rows_from_snapshot_reproduces_scores_and_clears_stale_sm_fields")
    test_overrides_move_y_without_touching_main()
    print("ok test_overrides_move_y_without_touching_main")
    test_unknown_override_is_ignored_not_fatal()
    print("ok test_unknown_override_is_ignored_not_fatal")
    test_t3_not_confirmed_reasons_follow_the_variant_cfg()
    print("ok test_t3_not_confirmed_reasons_follow_the_variant_cfg")
    test_t3_default_cfg_keeps_the_old_behaviour()
    print("ok test_t3_default_cfg_keeps_the_old_behaviour")
    test_t4_param_hash_is_reachable_from_the_variant()
    print("ok test_t4_param_hash_is_reachable_from_the_variant")
    print("all 13")
