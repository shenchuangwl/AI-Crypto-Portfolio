"""216 mcap-combo ceiling — SOL/Claude production census (phase 1, ENABLE=0).

Authority: docs/Hermes_Grok4.6_新五区币种选入标准报告_升级版_v2.0.0.md §4–§5
           + doc/Claude_Opus5_选币榜Y_216组合映射表.csv

This test must fail until ``coin_selection.mcap_combo`` and
``packages/config/mcap_combo_216.json`` exist. It does not import scan /
board_projection — phase 1 is a side-effect-free library.
"""

from __future__ import annotations

import csv
import json
import sys
from collections import Counter
from itertools import product
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from coin_selection.mcap_combo import (  # noqa: E402
    COMBO_JSON_NAME,
    GRADES,
    MIRROR,
    ZONE_CN,
    ZONE_EN,
    apply_hysteresis,
    canon_of,
    combo_no,
    index_combo,
    load_mapping,
    mapping_path,
    priority,
    q_of,
    resonance_k,
    worse,
    z10,
    zs10,
    zm10,
    zone_long,
    zone_short,
)

ROOT = Path(__file__).resolve().parents[3]
CSV_PATH = ROOT / "doc" / "Claude_Opus5_选币榜Y_216组合映射表.csv"
JSON_PATH = ROOT / "packages" / "config" / "mcap_combo_216.json"

LONG_COUNTS = {"DMR": 12, "CONFIRMED": 23, "QUALIFIED": 29, "WATCH": 53, "ELIMINATED": 99}
CANON_COUNTS = {"DMR": 24, "CONFIRMED": 46, "QUALIFIED": 58, "WATCH": 70, "ELIMINATED": 18}
PRI_COUNTS = {5: 14, 4: 46, 3: 74, 2: 54, 1: 28}

LONG_DMR = {
    "AAA", "AAB", "ABA", "ABB", "BAA", "BAB", "BBA", "BBB", "CAA", "CAB", "CBA", "CBB",
}


def _all_combos():
    return list(product(GRADES, repeat=3))


def test_module_constants():
    assert GRADES == "ABCDEF"
    assert q_of("A") == 3 and q_of("B") == 2 and q_of("C") == 1
    assert q_of("D") == -1 and q_of("E") == -2 and q_of("F") == -3
    assert MIRROR["A"] == "F" and MIRROR["B"] == "E" and MIRROR["C"] == "D"


def test_combo_numbering_is_bijective():
    seen = set()
    for g30, g2, g6 in _all_combos():
        n = combo_no(g30, g2, g6)
        assert 1 <= n <= 216
        seen.add(n)
        assert index_combo(n) == (g30, g2, g6)
    assert seen == set(range(1, 217))
    assert combo_no("A", "A", "A") == 1
    assert combo_no("A", "A", "F") == 6  # AAF
    assert combo_no("F", "F", "A") == 211  # FFA
    assert combo_no("F", "F", "F") == 216


def test_z10_identity_and_priority_census():
    pri = Counter()
    for g30, g2, g6 in _all_combos():
        z = z10(g30, g2, g6)
        assert z == 2 * q_of(g30) + 3 * q_of(g2) + 5 * q_of(g6)
        assert z == 2 * zs10(g30, g2, g6) + zm10(g30, g2, g6)
        pri[priority(g30, g2, g6)] += 1
    assert pri == PRI_COUNTS
    # AAA: q=3,3,3 → Z10 = 6+9+15 = 30, |Z|=3.0 → pri 5
    assert z10("A", "A", "A") == 30
    assert priority("A", "A", "A") == 5
    # AAF: Z10 = 2*3 + 3*3 + 5*(-3) = 6+9-15 = 0 → pri 1
    assert z10("A", "A", "F") == 0
    assert priority("A", "A", "F") == 1


def test_resonance_k_four_bins():
    # s6=s2=s30 → 100
    assert resonance_k("A", "B", "C") == 100
    # s6=s2 ≠ s30 → 70
    assert resonance_k("F", "A", "B") == 70
    # s6=s30 ≠ s2 → 40
    assert resonance_k("A", "F", "A") == 40
    # s6 ≠ s2=s30 → 10
    assert resonance_k("A", "A", "F") == 10
    k_counts = Counter(resonance_k(a, b, c) for a, b, c in _all_combos())
    assert k_counts == {100: 54, 70: 54, 40: 54, 10: 54}


def test_per_side_census_12_23_29_53_99():
    long_c = Counter(zone_long(a, b, c) for a, b, c in _all_combos())
    short_c = Counter(zone_short(a, b, c) for a, b, c in _all_combos())
    assert long_c == LONG_COUNTS, long_c
    assert short_c == LONG_COUNTS, short_c
    assert sum(long_c.values()) == 216


def test_short_is_mirror_of_long():
    for g30, g2, g6 in _all_combos():
        assert zone_short(g30, g2, g6) == zone_long(MIRROR[g30], MIRROR[g2], MIRROR[g6])
        assert zone_long(g30, g2, g6) == zone_short(MIRROR[g30], MIRROR[g2], MIRROR[g6])


def test_aaf_ffa_bidirectional_elim():
    assert combo_no("A", "A", "F") == 6
    assert combo_no("F", "F", "A") == 211
    assert zone_long("A", "A", "F") == "ELIMINATED"
    assert zone_short("A", "A", "F") == "ELIMINATED"
    assert zone_long("F", "F", "A") == "ELIMINATED"
    assert zone_short("F", "F", "A") == "ELIMINATED"


def test_long_dmr_whitelist_is_the_twelve():
    got = {f"{a}{b}{c}" for a, b, c in _all_combos() if zone_long(a, b, c) == "DMR"}
    assert got == LONG_DMR


def test_canon_census_24_46_58_70_18():
    c = Counter(canon_of(a, b, c)[0] for a, b, c in _all_combos())
    assert c == CANON_COUNTS, c


def test_worse_never_promotes():
    assert worse("CONFIRMED", "QUALIFIED") == "QUALIFIED"
    assert worse("WATCH", "DMR") == "WATCH"
    assert worse("DMR", "ELIMINATED") == "ELIMINATED"
    assert worse("QUALIFIED", "CONFIRMED") == "QUALIFIED"
    assert worse(None, "DMR") == "WATCH"  # null combo → 观察天花板
    assert worse("CONFIRMED", None) == "WATCH"
    # aliases
    assert worse("确定", "符合") == "QUALIFIED"
    assert worse("观察", "DMR") == "WATCH"
    # equal
    assert worse("WATCH", "WATCH") == "WATCH"
    # SM CONFIRMED + DMR ceiling stays CONFIRMED (DMR is a flag, not a promotion of SM)
    assert worse("CONFIRMED", "DMR") == "CONFIRMED"


def test_hysteresis_asymmetric():
    # HARD_FAIL / SM already eliminated: immediate, no streak
    z, s = apply_hysteresis("ELIMINATED", "DMR", prev_product="CONFIRMED", prev_streak=0)
    assert z == "ELIMINATED" and s == 0

    # 确定 → 符合: immediate
    z, s = apply_hysteresis("CONFIRMED", "QUALIFIED", prev_product="CONFIRMED", prev_streak=0)
    assert z == "QUALIFIED" and s == 0

    # 符合 → 观察: 2 nodes
    z, s = apply_hysteresis("QUALIFIED", "WATCH", prev_product="QUALIFIED", prev_streak=0)
    assert z == "QUALIFIED" and s == 1
    z, s = apply_hysteresis("QUALIFIED", "WATCH", prev_product="QUALIFIED", prev_streak=1)
    assert z == "WATCH" and s == 0

    # 观察 → 淘汰: 2 nodes
    z, s = apply_hysteresis("WATCH", "ELIMINATED", prev_product="WATCH", prev_streak=0)
    assert z == "WATCH" and s == 1
    z, s = apply_hysteresis("WATCH", "ELIMINATED", prev_product="WATCH", prev_streak=1)
    assert z == "ELIMINATED" and s == 0

    # ceiling lift unblocks immediately (SM still CONFIRMED)
    z, s = apply_hysteresis("CONFIRMED", "DMR", prev_product="QUALIFIED", prev_streak=1)
    assert z == "CONFIRMED" and s == 0

    # layer A WATCH + ceiling DMR never promotes
    z, s = apply_hysteresis("WATCH", "DMR", prev_product="WATCH", prev_streak=0)
    assert z == "WATCH"

    # null combo → 观察 ceiling
    z, s = apply_hysteresis("WATCH", None, prev_product="WATCH", prev_streak=0)
    assert z == "WATCH"


def test_json_exists_216_unique_and_matches_pure_functions():
    assert JSON_PATH.is_file(), f"missing {JSON_PATH}"
    rows = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    if isinstance(rows, dict):
        rows = rows["rows"]
    assert len(rows) == 216
    triples = [(r["g30"], r["g2"], r["g6"]) for r in rows]
    assert len(set(triples)) == 216
    loaded = load_mapping()
    assert len(loaded) == 216
    assert mapping_path().resolve() == JSON_PATH.resolve() or mapping_path().name == COMBO_JSON_NAME
    for r in rows:
        g30, g2, g6 = r["g30"], r["g2"], r["g6"]
        assert r["no"] == combo_no(g30, g2, g6)
        assert r["combo"] == f"{g30}{g2}{g6}"
        assert r["Z10"] == z10(g30, g2, g6)
        assert r["K"] == resonance_k(g30, g2, g6)
        assert r["pri"] == priority(g30, g2, g6)
        assert ZONE_EN[r["ceiling_long"]] == zone_long(g30, g2, g6)
        assert ZONE_EN[r["ceiling_short"]] == zone_short(g30, g2, g6)
        canon_z, canon_pool = canon_of(g30, g2, g6)
        assert ZONE_EN[r["canon_zone"]] == canon_z
        assert r["canon_pool"] == canon_pool


def test_json_matches_claude_csv_side_ceilings():
    assert CSV_PATH.is_file(), f"missing {CSV_PATH}"
    with CSV_PATH.open("r", encoding="utf-8-sig") as f:
        csv_rows = list(csv.DictReader(f))
    assert len(csv_rows) == 216
    js = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    if isinstance(js, dict):
        js = js["rows"]
    by_no = {int(r["no"]): r for r in js}
    for cr in csv_rows:
        no = int(cr["序号"])
        r = by_no[no]
        g30, g2, g6 = cr["30m状态"], cr["2h状态"], cr["6h状态"]
        assert r["g30"] == g30 and r["g2"] == g2 and r["g6"] == g6
        assert r["ceiling_long"] == cr["上涨侧准入上限"], (no, r["ceiling_long"], cr["上涨侧准入上限"])
        assert r["ceiling_short"] == cr["下跌侧准入上限"], (no, r["ceiling_short"], cr["下跌侧准入上限"])
        assert r["canon_zone"] == cr["规范分区"], (no, r["canon_zone"], cr["规范分区"])


def test_zone_cn_roundtrip():
    for en, cn in ZONE_CN.items():
        assert ZONE_EN[cn] == en


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in tests:
        try:
            fn()
        except Exception as e:  # noqa: BLE001 — runner prints then continues
            failed += 1
            print("FAIL", fn.__name__, type(e).__name__, e)
        else:
            print("ok", fn.__name__)
    print("failed" if failed else "all", failed if failed else len(tests))
    raise SystemExit(1 if failed else 0)
