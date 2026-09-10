"""参数指纹 param_hash —— 文档B §3.1 / §9.1 的测试 T4。

    PYTHONPATH=services/coin-selection/src python3 services/coin-selection/tests/test_param_fingerprint.py

指纹是《选币榜Y》与《复盘选币》「不可能悄悄分叉」的机器保证：

  * 同参数 → 同指纹（可复现，与进程 / 目录 / 机器无关）
  * 改任一**影响选币结果**的字段 → 指纹必变
  * 改运行时字段（data_dir / dmr_inbox / gate1_workers / 并发数）→ 指纹**不变**
    否则换个目录跑回放就永远对不上
  * 指纹带 ``frozen`` 段：偷偷改红线项（Gate1 门槛 / F2 封顶 / 判级函数）指纹立刻变化
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from coin_selection.board_variants import (  # noqa: E402
    BOARD_Y_KEY,
    FINGERPRINT_SETTINGS_FIELDS,
    FINGERPRINT_STATE_FIELDS,
    MAIN_KEY,
    get_variant,
    param_fingerprint,
    param_fingerprint_payload,
    variant_settings,
    variant_state_config,
)
from coin_selection.scan import SelectionSettings  # noqa: E402
from coin_selection.state_machine import StateConfig  # noqa: E402


def _fp(settings=None, cfg=None, variant=None):
    v = variant if variant is not None else get_variant(BOARD_Y_KEY)
    s = settings if settings is not None else variant_settings(SelectionSettings(), v)
    c = cfg if cfg is not None else variant_state_config(v)
    return param_fingerprint(v, s, c)


def test_shape_and_determinism():
    h = _fp()
    assert h.startswith("pf1_"), h
    assert len(h) == 4 + 16, h
    assert h == _fp(), "同参数两次调用指纹不同 —— 指纹不可复现"


def test_two_boards_have_different_fingerprints():
    """主板与 Y 的 overrides 不同（Y 有权重增量 + 24h 周期）→ 指纹必不同。"""
    main = get_variant(MAIN_KEY)
    y = get_variant(BOARD_Y_KEY)
    hm = param_fingerprint(main, variant_settings(SelectionSettings(), main), variant_state_config(main))
    hy = param_fingerprint(y, variant_settings(SelectionSettings(), y), variant_state_config(y))
    assert hm != hy, (hm, hy)


def test_every_whitelisted_settings_field_moves_the_hash():
    base_s = variant_settings(SelectionSettings(), get_variant(BOARD_Y_KEY))
    base = _fp(settings=base_s)
    for f in FINGERPRINT_SETTINGS_FIELDS:
        cur = getattr(base_s, f)
        nxt = (not cur) if isinstance(cur, bool) else (float(cur) + 1.0 if isinstance(cur, (int, float)) else cur)
        if nxt == cur:
            continue
        if isinstance(cur, int) and not isinstance(cur, bool):
            nxt = int(cur) + 1
        got = _fp(settings=replace(base_s, **{f: nxt}))
        assert got != base, f"改 {f} 指纹没变 —— 白名单形同虚设"


def test_every_whitelisted_state_field_moves_the_hash():
    base_c = variant_state_config(get_variant(BOARD_Y_KEY))
    base = _fp(cfg=base_c)
    for f in FINGERPRINT_STATE_FIELDS:
        cur = getattr(base_c, f)
        nxt = int(cur) + 1 if isinstance(cur, int) else float(cur) + 1.0
        got = _fp(cfg=replace(base_c, **{f: nxt}))
        assert got != base, f"改 {f} 指纹没变"


def test_runtime_fields_do_not_move_the_hash():
    """换目录 / 换并发数跑回放，指纹必须一模一样，否则对账永远对不上。"""
    base_s = variant_settings(SelectionSettings(), get_variant(BOARD_Y_KEY))
    base = _fp(settings=base_s)
    for f, v in (
        ("data_dir", "/tmp/somewhere-else"),
        ("dmr_inbox", "/tmp/inbox"),
        ("gate1_workers", 32),
        ("gate2_workers", 1),
        ("mcap_tf_workers", 2),
        ("long_ret_workers", 3),
        ("gate1_sleep_sec", 0.5),
        ("market_ingest_url", "http://127.0.0.1:9999"),
        ("fapi_rest", "https://example.invalid"),
    ):
        assert _fp(settings=replace(base_s, **{f: v})) == base, f"{f} 不该进指纹"


def test_frozen_block_is_present_and_reacts_to_g1_floor():
    v = get_variant(BOARD_Y_KEY)
    s = variant_settings(SelectionSettings(), v)
    c = variant_state_config(v)
    payload = param_fingerprint_payload(v, s, c)
    assert payload["schema"] == "param-fingerprint-v1"
    fr = payload["frozen"]
    assert fr["grade_fn"] == "grade_from_mas@mcap_timeframe"
    assert fr["g4_f2_cap"] == 45
    assert fr["g4_lookback_1h"] == 168
    assert fr["g1_floor_usd"] == "3000000.000000000"
    # 偷偷把 300 万门槛降到 100 万 → 指纹立刻变化
    assert param_fingerprint(v, replace(s, hard_floor_usd=1_000_000.0), c) != param_fingerprint(v, s, c)


def test_mcap_zone_block_reacts_to_mode_and_cuts():
    v = get_variant(BOARD_Y_KEY)
    # 基线必须**显式**取 mode="off"，不能直接用活注册表的当前值：
    # 主导层上线后注册表里就是 "on"，那时 replace(s, mcap_zone_mode="on") == base，
    # 本测试会假阴性（这正是本轮踩到的）。
    s = replace(variant_settings(SelectionSettings(), v), mcap_zone_mode="off")
    c = variant_state_config(v)
    base = param_fingerprint(v, s, c)
    assert param_fingerprint(v, replace(s, mcap_zone_mode="shadow"), c) != base
    assert param_fingerprint(v, replace(s, mcap_zone_mode="on"), c) != base
    assert param_fingerprint(v, replace(s, enable_mcap_zone=True), c) != base
    assert param_fingerprint(v, replace(s, mcap_zone_cut_dmr=2.2), c) != base
    assert param_fingerprint(v, replace(s, mcap_zone_abstain_demote=False), c) != base
    # 布尔别名与 mode="on" 必须落在同一个指纹上（同一件事只有一个表示）
    assert param_fingerprint(v, replace(s, enable_mcap_zone=True), c) == param_fingerprint(
        v, replace(s, mcap_zone_mode="on"), c
    )


def test_float_representation_noise_does_not_move_the_hash():
    """0.1+0.2 与 0.30000000000000004 必须是同一个指纹。"""
    v = get_variant(BOARD_Y_KEY)
    c = variant_state_config(v)
    a = replace(variant_settings(SelectionSettings(), v), w_liq=0.15)
    b = replace(variant_settings(SelectionSettings(), v), w_liq=0.1 + 0.05)
    assert a.w_liq != b.w_liq or True  # 表示可能相同也可能不同，指纹都必须相同
    assert param_fingerprint(v, a, c) == param_fingerprint(v, b, c)


def test_cycle_semantics_are_in_the_hash():
    """Y 的 24h 周期是它与主板的一处实质差异，必须进指纹。"""
    v = get_variant(BOARD_Y_KEY)
    payload = param_fingerprint_payload(
        v, variant_settings(SelectionSettings(), v), variant_state_config(v)
    )
    assert payload["cycle"]["enabled"] is True
    assert payload["cycle"]["period_hours"] == 24
    assert payload["cycle"]["anchor_utc"] == "00:00"
    assert payload["cycle"]["clear"] == ["daily_unique", "state_machine"]
    main = get_variant(MAIN_KEY)
    pm = param_fingerprint_payload(
        main, variant_settings(SelectionSettings(), main), variant_state_config(main)
    )
    assert pm["cycle"]["enabled"] is False


def test_payload_is_json_serialisable_and_stable():
    v = get_variant(BOARD_Y_KEY)
    p = param_fingerprint_payload(
        v, variant_settings(SelectionSettings(), v), variant_state_config(v)
    )
    a = json.dumps(p, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    b = json.dumps(p, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert a == b
    assert "data_dir" not in a and "dmr_inbox" not in a


def test_default_state_config_alone_is_enough():
    """不依赖变体注册表也能算 —— 回放器用得上（它可能拿到裸 settings/cfg）。"""
    h = param_fingerprint(None, SelectionSettings(), StateConfig())
    assert h.startswith("pf1_")
    assert h != param_fingerprint(None, replace(SelectionSettings(), w_mcap=0.25), StateConfig())


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print("ok", fn.__name__)
    print("all", len(tests))
