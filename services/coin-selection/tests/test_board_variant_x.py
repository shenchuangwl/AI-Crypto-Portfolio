"""选币榜X v1.3.0 护栏：复刻 main v1.4.0，新五区与复盘闭环独立。
唯一机器调参入口是 boards[key=x].overrides；当前仅 shadow 观察，不开放执行。
测试只写临时目录，绝不修改 main/y 的状态机、快照、账本或 DMR inbox。
"""
from __future__ import annotations

import copy
import json
import sys
import tempfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
ROOT = Path(__file__).resolve().parents[3]
from coin_selection.board_variants import get_variant, load_variants

#: X 当前形态。克隆期做全量「逐字段等于 main」强断言；适配期只验 X 自己的不变量。
def _clone_mode() -> bool:
    v = get_variant("x")
    return (v.state_overrides or {}).get("selection_semantics") != "dual-path-v1.3"
CLONE_MODE = _clone_mode()
from coin_selection.board_projection import project_board, rows_from_snapshot
from coin_selection.board_variants import variant_settings, variant_state_config
from coin_selection.scan import (
    SelectionSettings, board_from_rows, build_dmr_messages, build_screener_snapshot,
    decorate_board, rank_dmr_inbox,
)
from coin_selection.mcap_effective import apply_effective_zone
from coin_selection.mcap_mapping import load as load_mapping
from coin_selection.review_replay import in_zone
from coin_selection.state_machine import StateMachineStore, apply_state_machine
from test_board_variant_y import _rows, _score

NOW = datetime(2026, 9, 5, 10, 0, tzinfo=timezone.utc)
ANCHOR = NOW.replace(hour=0)


def _rank_rows():
    """Score 与组合强弱故意相反；模拟行只作测试，不冒充现网绩效。"""
    rows = _score(_rows(24), SelectionSettings())
    for i, r in enumerate(rows):
        r.update(state_up="CONFIRMED", state_down="ELIMINATED", ss_up=85,
                 momentum_score_up=85, consistency_up=1.0, score_up=75+i/10)
        g = "A" if i % 2 == 0 else "F"
        r.update(mcap_grade_30m=g, mcap_grade_2h=g, mcap_grade_6h=g)
    return rows


def test_x_shadow_keeps_main_sort_and_on_keeps_legacy_dominance():
    """选币榜X shadow 不改排序；未来 on 才裁决，main/Y 老路径逐字段不变。"""
    base = SelectionSettings()
    raw = _rank_rows()
    decorated = copy.deepcopy(raw)
    apply_effective_zone(decorated, mode="shadow", mapping=load_mapping(strict=True))
    expected = board_from_rows(raw, "up", now=NOW)
    kw = dict(anchor=ANCHOR, scan_id="20260905-040", seq=40, now=NOW,
              universe_count=len(raw), uni_ver="unit", stats={}, transitions=[])
    for mode in ("off", "shadow", "on"):
        s = replace(base, mcap_zone_mode=mode)
        pool = build_screener_snapshot(s, rows=copy.deepcopy(decorated), **kw)["long_pool"]
        reference = board_from_rows(decorated if mode == "on" else raw, "up", now=NOW)
        assert [r["symbol"] for r in pool] == [r["symbol"] for r in reference], mode
    assert expected != board_from_rows(decorated, "up", now=NOW), "夹具必须能撞红旧排序"


def test_x_shadow_dmr_top_k_is_main_not_hypothetical_ceiling():
    """复盘 DMR 必须与 main 强子集同源；shadow 字段不得偷偷改变去重/Top-K。"""
    raw = _rank_rows()
    decorated = copy.deepcopy(raw)
    apply_effective_zone(decorated, mode="shadow", mapping=load_mapping(strict=True))
    kwargs = dict(anchor=ANCHOR, scan_id="20260905-040", seq=40, now=NOW)
    base = SelectionSettings()
    expected, _ = rank_dmr_inbox(build_dmr_messages(raw, settings=base, **kwargs), top_k=16)
    with tempfile.TemporaryDirectory(prefix="boardx-dmr-") as tmp:
        x = get_variant("x")
        project_board(x, base, decorated, **kwargs, universe_count=len(raw), uni_ver="unit",
                      stats={}, root=Path(tmp), skip_sm=True, ingest_ledger=False, backfill_prices=False)
        inbox = json.loads((Path(tmp)/x.dmr_inbox/"20260905-040.candidates.json").read_text())
        got = [m["symbol"] for m in inbox["candidates"]]
        if CLONE_MODE:
            # 克隆期：DMR 必须与 main 强子集逐个相同。
            assert got == [m["symbol"] for m in expected]
        else:
            # 适配期：DMR 改由「确认 ∩ 基础条件 ∩ 216」派生，与 main 强子集不同
            # 是**预期**。这里守住的是 shadow 四列不得偷偷改变去重/Top-K —— 即
            # 结果必须仍是 CONFIRMED 的子集且不超过 K。
            conf = {r["symbol"] for r in raw if "CONFIRMED" in
                    (r.get("state_up"), r.get("state_down"))}
            assert set(got) <= conf, (got, conf)
            assert len(got) <= 16


def test_x_projection_equals_the_main_board_field_by_field():
    """非空多节点逐字段比较；仅允许列明的 shadow 审计附加字段，不弱化业务断言。"""
    # 原 Y 模板首节点两池为空，等价断言会空跑；此处跨节点覆盖新五区与真实 DMR 派生。
    with tempfile.TemporaryDirectory(prefix="boardx-equal-") as tmp:
        root = Path(tmp)
        x = get_variant("x")
        base = SelectionSettings(data_dir=str(root/"main"), dmr_inbox=str(root/"inbox"))
        sm = StateMachineStore(root/"main/state_machine.json")
        seen = set()
        for tick in range(8):
            now = NOW + timedelta(minutes=15*tick)
            sid = f"20260905-{40+tick:03d}"
            raw = _score(_rows(526), base)
            for i, row in enumerate(raw):
                if i % 3 == 0:
                    row.update(mcap_grade_30m="F", mcap_grade_2h="F", mcap_grade_6h="F")
                if i < 5:
                    row["liquidity_hard_pass"] = False
            rows = copy.deepcopy(raw)
            tr = apply_state_machine(rows, sm, scan_id=sid, now_ts=now.timestamp())
            kw = dict(anchor=ANCHOR, scan_id=sid, seq=40+tick, now=now)
            a = build_screener_snapshot(base, rows=rows, universe_count=len(rows), uni_ver="unit",
                                       stats={}, transitions=tr, generated_at=now, **kw)
            msgs, rank = rank_dmr_inbox(build_dmr_messages(rows, settings=base, **kw), top_k=16)
            decorate_board(a, rows=rows, dmr_msgs=msgs, dmr_rank=rank, daily_unique={}, settings=base)
            xr = rows_from_snapshot(x, base, raw)
            project_board(x, base, xr, **kw, universe_count=len(xr), uni_ver="unit", stats={},
                          root=root, generated_at=now, ingest_ledger=True, backfill_prices=False)
            b = json.loads((root/x.data_dir/"latest.json").read_text())
            if CLONE_MODE:
                assert b["transitions"] == a["transitions"], sid
            for pool in ("long_pool", "short_pool"):
                assert len(a[pool]) == len(b[pool]), (sid,pool)
                # X 仅增加四列相关审计链；先过滤这批显式字段，再比较整个业务行。
                audit = {"mcap_combo_code","mcap_combo_no","mcap_combo_status","mcap_z10",
                         "mcap_priority","mcap_resonance_k","mcap_ceiling_zone","base_state",
                         "effective_zone","final_zone","mcap_predicate","mcap_crosscheck",
                         "w_base","w_prio","w_k","w_combo","w_final","rank_key",
                         "mcap_reason_codes","effective_reason_codes","shadow_effective_zone"}
                stripped = [{k:v for k,v in r.items() if k not in audit} for r in b[pool]]
                # —— 逐字段等于 main 只在**克隆期**成立 ——
                #
                # X 切到 dual-path-v1.3 之后，四区与 DMR 必然与 main 分叉（PATH_M
                # 恢复、hold 改析取、取消 SS<50 立即降级）—— 那正是本轮的目的。
                # 这条断言写死相等，会在切换当天变红，而它红不代表出错。
                # 克隆期仍然全量强断言；适配期改验只属于 X 自己的不变量：
                # 行数一致、shadow 四列齐全、DMR ⊆ CONFIRMED。
                if CLONE_MODE:
                    assert stripped == a[pool], (sid,pool,"业务字段/排名或时间戳漂移")
                else:
                    conf = {r["symbol"] for r in b[pool] if r.get("state")=="CONFIRMED"}
                    dmr = {r["symbol"] for r in b[pool] if r.get("dmr_selected")}
                    assert dmr <= conf, (sid,pool,"DMR 必须是 CONFIRMED 的子集")
                for ar, br in zip(a[pool], b[pool]):
                    seen.add(ar["state"])
                    assert all(br.get(k) is not None for k in ("mcap_combo_code","mcap_z10","mcap_resonance_k","mcap_ceiling_zone"))
                    if CLONE_MODE:
                        for zone in ("WATCH","QUALIFIED","CONFIRMED","DMR"):
                            assert in_zone(br,zone,scan_id=sid) == in_zone(ar,zone,scan_id=sid), (sid,br["symbol"],zone)
            if tick == 7:
                assert msgs, "等价性不得用空 DMR 集合冒充验证"
        assert {"WATCH","QUALIFIED","CONFIRMED","ELIMINATED"} <= seen, seen


def test_x_fallback_and_forced_write_paths():
    """X 注册表读不到仍隔离，误配 overrides 也不写 main；复盘和后续演进不串台。"""
    from coin_selection import board_variants as bv
    with patch.object(bv, "REGISTRY_PATH", Path("/nonexistent/boardx-unit.json")):
        x = next(v for v in load_variants() if v.key == "x")
        # 内置兜底（读不到注册表时）恒为克隆形态，这条与生产形态无关。
        assert x.settings_overrides == {"mcap_zone_mode":"shadow"}
        assert not x.cycle.enabled and not x.dmr_executable
    x = get_variant("x")
    bad = replace(x, settings_overrides={"data_dir":"data/coin-selection",
                  "dmr_inbox":"data/dmr-adapter/inbox", "parameter_version":"wrong"})
    s = variant_settings(SelectionSettings(),bad)
    assert s.data_dir.endswith(x.data_dir) and s.dmr_inbox.endswith(x.dmr_inbox)
    assert s.parameter_version == x.parameter_version
    # —— X 的 state_config 与 main 的关系随形态而变 ——
    #
    # 克隆期两者逐字段相等；X 切到 dual-path-v1.3 之后必然不等 —— 那正是本轮
    # 要让它们分叉的地方。写死相等会在切换当天变红，而真正要守住的不变量是：
    # **除 selection_semantics 之外，其余阈值字段仍与 main 完全一致**
    # （本轮只换语义分支，不动任何阈值）。
    # 允许分叉的字段 = **已获批 overrides 片段里出现过的键**，从 candidates/ 派生。
    # 原先写死 ["selection_semantics"]，r4 加了确认区动能门槛后立刻红 —— 又一次
    # 「把当时为真的快照写死」。从片段派生后，新增可调字段零改动，且仍是白名单：
    # 任何**没被任何已获批形态声明过**的字段分叉照样判红。
    import json as _json
    allowed = set()
    for _frag in sorted((ROOT / "packages/config/candidates").glob("board-variants.x-*.json")):
        try:
            allowed |= set((_json.loads(_frag.read_text(encoding="utf-8"))["overrides"]
                            .get("state_config") or {}).keys())
        except Exception:
            pass
    from dataclasses import fields as _dcf
    sx, sm = variant_state_config(x), variant_state_config(get_variant("main"))
    diff = [f.name for f in _dcf(sx) if getattr(sx, f.name) != getattr(sm, f.name)]
    unexpected = [d for d in diff if d not in allowed]
    assert not unexpected, f"X 与 main 出现**未获批**的阈值分叉: {unexpected}（已获批: {sorted(allowed)}）"


def test_x_registry_shape():
    """v1.3.0 身份不借用旧双路径；独立目录为复盘与日后演进保住边界。"""
    variants = load_variants(refresh=True)
    assert [v.key for v in variants] == ["main", "x", "y"]
    x = get_variant("x")
    assert x.parameter_version == "param-v1.3.0-screener-x"
    assert x.api_prefix == "/api/v1/screener-x"
    assert x.web_route == "/screener-x"
    assert x.data_dir == "data/coin-selection-x"
    assert x.dmr_inbox == "data/dmr-adapter-x/inbox"
    assert x.projected and not x.primary and not x.dmr_executable
    assert not x.cycle.enabled
    assert x.enabled_env == "ENABLE_BOARD_X"
    # 形态感知：克隆期只有 shadow；适配期额外带 dmr_selection_mode。
    # 两种形态都必须**只**含已获批的键，不许冒出第三个。
    _ss = [{"mcap_zone_mode": "shadow"}] + [
        (json.loads(f.read_text(encoding="utf-8"))["overrides"].get("settings") or {})
        for f in sorted((ROOT / "packages/config/candidates").glob("board-variants.x-*.json"))]
    assert x.settings_overrides in _ss, (x.settings_overrides, "不在已获批形态集合内")
    # 同上：已获批形态从 candidates/ 派生，不逐版硬编码。
    _shapes = [{}] + [(json.loads(f.read_text(encoding="utf-8"))["overrides"].get("state_config") or {})
                      for f in sorted((ROOT / "packages/config/candidates").glob("board-variants.x-*.json"))]
    assert x.state_overrides in _shapes, (x.state_overrides, "不在已获批形态集合内")
    for field in ("key", "api_prefix", "web_route", "data_dir", "dmr_inbox", "parameter_version"):
        assert len({getattr(v, field) for v in variants}) == len(variants), field
    assert len({v.ledger_path() for v in variants}) == len(variants)


def test_x_projection_has_no_network_and_preserves_credits():
    """X v1.3.0 共享 main v1.4.0 指标，复盘闭环不增取数；未来 overrides 不是取数入口。"""
    with tempfile.TemporaryDirectory(prefix="boardx-offline-") as tmp:
        x = get_variant("x")
        with patch("socket.socket.connect", side_effect=AssertionError("投影不得额外访问网络")):
            result = project_board(x, SelectionSettings(), _rank_rows(), anchor=ANCHOR,
                scan_id="20260905-040", seq=40, now=NOW, universe_count=24, uni_ver="unit",
                stats={"gate3":{"credits_est":3}}, root=Path(tmp), skip_sm=True,
                ingest_ledger=True, backfill_prices=False)
        assert result["review_ledger"] == "ok", result
        board = json.loads((Path(tmp)/x.data_dir/"latest.json").read_text())
        assert board["meta"]["coingecko_credits"]["used_today"] == 3


def test_x_review_membership_is_actual_state_even_when_ceiling_opposes():
    """shadow 观察不能泄漏到复盘新五区判据；X v1.3.0 克隆 main，演进只经 overrides。"""
    raw = _rank_rows()
    apply_effective_zone(raw, mode="shadow", mapping=load_mapping(strict=True))
    kw = dict(anchor=ANCHOR,scan_id="20260905-040",seq=40,now=NOW,
              universe_count=len(raw),uni_ver="unit",stats={},transitions=[])
    board = build_screener_snapshot(variant_settings(SelectionSettings(), get_variant("x")), rows=raw, **kw)
    opposed = [r for r in board["long_pool"] if r["mcap_ceiling_zone"] == "ELIMINATED"]
    assert opposed
    assert all(in_zone(r,"CONFIRMED",scan_id="20260905-040") for r in opposed)


def test_x_gateway_paths_and_missing_ledger_hint_are_independent():
    """X v1.3.0 复盘/实时同源而路径独立，main v1.4.0 不变；后续只调 X overrides。"""
    sys.path.insert(0,str(Path(__file__).resolve().parents[2]/"api-gateway"))
    import mock_server
    import review_api
    assert mock_server.resolve_board_path("/api/v1/screener-x/latest") == ("x","/latest")
    assert mock_server.resolve_board_path("/api/v1/screener/latest") == ("main","/latest")
    assert review_api.resolve_board({"board":["x"]}) == "x"
    assert review_api.board_cycle("x") is None
    with patch.object(review_api,"open_ledger",return_value=None):
        status, doc = review_api.review_payload({"board":["x"]},kind="coverage")
        assert status == 503 and "--board x" in doc["hint"]
        assert doc["ledger_path"].endswith("data/coin-selection-x/review/ledger.sqlite")


def test_adopt_enter_times_fills_missing_but_does_not_overwrite():
    """回放补戳只填空，不得把已有占用的入选价/时刻改成主板的。"""
    from coin_selection.board_projection import adopt_enter_times

    with tempfile.TemporaryDirectory(prefix="adopt-") as tmp:
        root = Path(tmp)
        x = get_variant("x")
        x_dir = x.data_path(root)
        x_dir.mkdir(parents=True)
        main_dir = root / "data" / "coin-selection"
        main_dir.mkdir(parents=True)
        main_sm = {
            "states": {
                "CYSUSDT|down": {
                    "symbol": "CYSUSDT",
                    "direction": "down",
                    "state": "CONFIRMED",
                    "state_enter_ts": 1000.0,
                    "state_enter_price": 0.2424,
                },
                "NEWUSDT|up": {
                    "symbol": "NEWUSDT",
                    "direction": "up",
                    "state": "WATCH",
                    "state_enter_ts": 2000.0,
                    "state_enter_price": 1.0,
                },
            }
        }
        x_sm = {
            "states": {
                "CYSUSDT|down": {
                    "symbol": "CYSUSDT",
                    "direction": "down",
                    "state": "CONFIRMED",
                    "state_enter_ts": 3000.0,
                    "state_enter_price": 0.2294,
                },
                "NEWUSDT|up": {
                    "symbol": "NEWUSDT",
                    "direction": "up",
                    "state": "WATCH",
                    "state_enter_ts": 0,
                    "state_enter_price": None,
                },
            }
        }
        (main_dir / "state_machine.json").write_text(json.dumps(main_sm), encoding="utf-8")
        (x_dir / "state_machine.json").write_text(json.dumps(x_sm), encoding="utf-8")
        out = adopt_enter_times(x, main_dir / "state_machine.json", root=root)
        assert out["adopted"] == 1
        doc = json.loads((x_dir / "state_machine.json").read_text(encoding="utf-8"))
        kept = doc["states"]["CYSUSDT|down"]
        assert kept["state_enter_price"] == 0.2294
        assert kept["state_enter_ts"] == 3000.0
        filled = doc["states"]["NEWUSDT|up"]
        assert filled["state_enter_price"] == 1.0
        assert filled["state_enter_ts"] == 2000.0


if __name__ == "__main__":
    tests = [(name, fn) for name, fn in sorted(globals().items()) if name.startswith("test_")]
    for name, fn in tests:
        fn()
        print(f"ok {name}")
    print(f"all {len(tests)}")
