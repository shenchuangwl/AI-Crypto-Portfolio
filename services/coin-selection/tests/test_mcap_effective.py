"""有效区规则内核单测 —— 文档 A §17.1 的必过验收逐条落地。

对应验收条款
------------
* 「对 4 个实际业务 base_state × 216 × 2 = **1728** 个样例，
  ``effective_rank >= base_rank`` 全部成立」；
* 「另对 NONE/DATA_INSUFFICIENT/LOW_CONFIDENCE × (216+null) × 2 = **1302** 个样例
  断言原样旁路」；
* 「五级展示 lattice 的 2160 例只作为数学附加测试，不称 base_state 测试」；
* 「DMR 始终是 CONFIRMED 子集」；
* 「三个 flags 关闭时，除新增审计元数据外，Y 业务输出与冻结基线逐字段一致」。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "services" / "coin-selection" / "src"))

from coin_selection import mcap_effective as me  # noqa: E402
from coin_selection import mcap_mapping as mm  # noqa: E402

MAP = mm.from_generator()


def _row(g30=None, g2=None, g6=None, **extra):
    r = {"mcap_grade_30m": g30, "mcap_grade_2h": g2, "mcap_grade_6h": g6}
    r.update(extra)
    return r


# ---------------------------------------------------------------------------
# 1728 业务样例：只降不升
# ---------------------------------------------------------------------------
def test_1728_business_samples_never_upgrade():
    n = 0
    for base in sorted(me.BUSINESS_BASE_STATES):
        for r in MAP.rows:
            for d in ("up", "down"):
                res = me.resolve_side(
                    _row(r["m30"], r["h2"], r["h6"]), d, base, mapping=MAP
                )
                n += 1
                assert (
                    me.ZONE_RANK[res["effective_zone"]] >= me.BASE_RANK[base]
                ), (base, r["combo_no"], d, res["effective_zone"])
                assert res["effective_zone"] != "DMR"  # DMR 绝不是 effective 取值
    assert n == 1728, n


def test_effective_equals_max_of_base_and_ceiling():
    for base in sorted(me.BUSINESS_BASE_STATES):
        for r in MAP.rows[:60]:
            for d in ("up", "down"):
                res = me.resolve_side(
                    _row(r["m30"], r["h2"], r["h6"]), d, base, mapping=MAP
                )
                want = me.zone_from_rank(
                    max(me.BASE_RANK[base], me.ZONE_RANK[res["mcap_ceiling_zone"]])
                )
                assert res["effective_zone"] == want


# ---------------------------------------------------------------------------
# 1302 特殊态样例：原样旁路
# ---------------------------------------------------------------------------
def test_1302_special_state_samples_bypass_unchanged():
    n = 0
    combos = [(r["m30"], r["h2"], r["h6"]) for r in MAP.rows] + [(None, None, None)]
    assert len(combos) == 217
    for base in sorted(me.BYPASS_STATES):
        for g in combos:
            for d in ("up", "down"):
                res = me.resolve_side(_row(*g), d, base, mapping=MAP)
                n += 1
                assert res["effective_zone"] == base
                assert res["mcap_ceiling_zone"] is None
                assert res["mcap_combo_status"] == me.COMBO_NOT_APPLICABLE
                assert res["dmr_ceiling_ok"] is False
                assert me.RC_BYPASS in res["effective_reason_codes"]
    assert n == 1302, n


def test_null_grade_never_promotes_a_special_state_to_watch():
    """文档 A §5：特殊态不因 null→WATCH fallback 获得展示资格。"""
    for base in sorted(me.BYPASS_STATES):
        res = me.resolve_side(_row("A", None, "A"), "up", base, mapping=MAP)
        assert res["effective_zone"] == base
        assert res["mcap_ceiling_zone"] is None


# ---------------------------------------------------------------------------
# null 与恢复（文档 A §9.5）
# ---------------------------------------------------------------------------
def test_null_grade_gives_watch_ceiling_and_incomplete_status():
    for g in (("A", None, "A"), (None, "A", "A"), ("A", "A", None), (None, None, None)):
        res = me.resolve_side(_row(*g), "up", "CONFIRMED", mapping=MAP)
        assert res["mcap_combo_no"] is None
        assert res["mcap_combo_status"] == me.COMBO_INCOMPLETE
        assert res["mcap_ceiling_zone"] == "WATCH"
        assert res["effective_zone"] == "WATCH"
        assert me.RC_INCOMPLETE in res["effective_reason_codes"]


def test_incomplete_never_fabricates_a_seventh_grade():
    res = me.resolve_side(_row("A", "Z", "A"), "up", "WATCH", mapping=MAP)
    assert res["mcap_combo_no"] is None
    assert res["mcap_combo_status"] == me.COMBO_INCOMPLETE


def test_ceiling_recovery_restores_effective_within_the_same_node():
    """ceiling 恢复后 effective 可当轮回到已挣得的 base 上限（文档 A §9.5）。"""
    down = me.resolve_side(_row("A", None, "A"), "up", "CONFIRMED", mapping=MAP)
    assert down["effective_zone"] == "WATCH"
    up = me.resolve_side(_row("A", "A", "A"), "up", "CONFIRMED", mapping=MAP)
    assert up["effective_zone"] == "CONFIRMED"  # 不是升级，是解除封顶


# ---------------------------------------------------------------------------
# DMR 派生（文档 A §10.2）
# ---------------------------------------------------------------------------
def test_dmr_precondition_requires_confirmed_base_and_dmr_ceiling():
    ok = me.resolve_side(_row("A", "A", "A"), "up", "CONFIRMED", mapping=MAP)
    assert ok["dmr_ceiling_ok"] is True
    assert me.RC_DMR_OK in ok["effective_reason_codes"]

    # base 不是 CONFIRMED → 永远不可能进 DMR
    for base in ("QUALIFIED", "WATCH", "ELIMINATED"):
        r = me.resolve_side(_row("A", "A", "A"), "up", base, mapping=MAP)
        assert r["dmr_ceiling_ok"] is False

    # ceiling 不是 DMR → 明确写 BLOCK 码
    blocked = me.resolve_side(_row("A", "D", "A"), "up", "CONFIRMED", mapping=MAP)
    assert blocked["dmr_ceiling_ok"] is False
    assert me.RC_DMR_BLOCK in blocked["effective_reason_codes"]


def test_dmr_ceiling_count_is_12_per_side():
    for d, key in (("up", "long_ceiling"), ("down", "short_ceiling")):
        n = sum(
            1
            for r in MAP.rows
            if me.resolve_side(
                _row(r["m30"], r["h2"], r["h6"]), d, "CONFIRMED", mapping=MAP
            )["dmr_ceiling_ok"]
        )
        assert n == 12, (d, n)


# ---------------------------------------------------------------------------
# 五级 lattice（数学附加，2160 例）
# ---------------------------------------------------------------------------
def test_lattice_2160_max_is_monotone():
    n = 0
    for display in me.ZONES_EN:
        for r in MAP.rows:
            for d in ("up", "down"):
                ceiling = MAP.ceiling(r["m30"], r["h2"], r["h6"], d)
                en = mm.ZONE_EN[ceiling]
                merged = me.zone_from_rank(
                    max(me.ZONE_RANK[display], me.ZONE_RANK[en])
                )
                n += 1
                assert me.ZONE_RANK[merged] >= me.ZONE_RANK[display]
    assert n == 2160, n


# ---------------------------------------------------------------------------
# 开关关 = 恒等映射（文档 A §17.1）
# ---------------------------------------------------------------------------
def test_mode_off_writes_nothing():
    rows = [
        _row("A", "A", "A", symbol="AAAUSDT", state_up="CONFIRMED", state_down="WATCH")
    ]
    before = {k: dict(r) for k, r in enumerate(rows)}
    meta = me.apply_effective_zone(rows, mode="off", mapping=MAP)
    assert meta["mode"] == "off"
    assert rows[0] == before[0], set(rows[0]) - set(before[0])


def test_unknown_mode_degrades_to_off_not_undefined():
    for bad in ("ON!", "enabled", "2", object()):
        assert me.normalize_mode(bad) == "off"
    assert me.normalize_mode("shadow") == "shadow"
    assert me.normalize_mode("ON") == "on"


def test_shadow_never_changes_state():
    rows = [
        _row("A", "D", "D", symbol="ADDUSDT", state_up="CONFIRMED", state_down="WATCH")
    ]
    meta = me.apply_effective_zone(rows, mode="shadow", mapping=MAP)
    assert meta["mode"] == "shadow"
    assert rows[0]["state_up"] == "CONFIRMED"  # base 不动
    assert rows[0]["effective_zone_up"] == "ELIMINATED"  # 但有效区已算出
    assert rows[0]["mcap_ceiling_zone_up"] == "ELIMINATED"


def test_on_overwrites_state_but_keeps_base_state():
    rows = [
        _row("A", "D", "D", symbol="ADDUSDT", state_up="CONFIRMED", state_down="WATCH")
    ]
    me.apply_effective_zone(rows, mode="on", mapping=MAP)
    assert rows[0]["base_state_up"] == "CONFIRMED"
    assert rows[0]["state_up"] == "ELIMINATED"
    assert rows[0]["effective_zone_up"] == "ELIMINATED"


def test_on_does_not_touch_special_states():
    rows = [
        _row(
            "A",
            "A",
            "A",
            symbol="XUSDT",
            state_up="DATA_INSUFFICIENT",
            state_down="NONE",
        )
    ]
    me.apply_effective_zone(rows, mode="on", mapping=MAP)
    assert rows[0]["state_up"] == "DATA_INSUFFICIENT"
    assert rows[0]["state_down"] == "NONE"
    assert "base_state_up" not in rows[0]


def test_merge_rank_refuses_non_business_base():
    try:
        me.merge_rank("NONE", "WATCH")
        raise AssertionError("merge_rank must refuse a non-business base_state")
    except ValueError:
        pass


def test_meta_block_carries_mapping_identity():
    b = me.meta_block("shadow", MAP)
    assert b["mcap_mapping_version"] == mm.MCAP_MAPPING_VERSION
    assert b["mapping_hash"] == "sha256:" + mm.EXPECTED_MAPPING_SHA256
    assert b["merge"] == "effective_rank=max(base_rank,ceiling_rank)"
    assert sorted(b["bypass_states"]) == [
        "DATA_INSUFFICIENT",
        "LOW_CONFIDENCE",
        "NONE",
    ]


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    fails = 0
    for t in tests:
        try:
            t()
            print(f"ok {t.__name__}")
        except AssertionError as e:
            fails += 1
            print(f"FAIL {t.__name__}: {e}")
    print(f"all {len(tests)}" if not fails else f"{fails} failed")
    raise SystemExit(1 if fails else 0)
