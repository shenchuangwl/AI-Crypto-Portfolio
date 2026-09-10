"""216/432 映射（ChatGpt_SOL5.6 口径）单测。

权威：文档 A §9.1/§9.2/§9.4、附录 A（216 行）、附录 B（不变量）、附录 C（mapping_hash）；
文档 B §20「映射」行：216/432、hash、镜像、优先级、双 mapping 字段全部精确。

最强的一条断言在 :func:`test_generator_reproduces_document_appendix_a_row_by_row`：
直接解析文档 A 附录 A 的 216 行 Markdown 表，与生成器逐字段比对。**文档变了、
代码没跟上，这条会立刻红。**
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "services" / "coin-selection" / "src"))

from coin_selection import mcap_mapping as mm  # noqa: E402

DOC_A = ROOT / "docs" / "ChatGpt_SOL5.6_新五区币种选入标准报告_升级版_v2.0.0.md"
FROZEN = ROOT / "packages" / "config" / "mcap-216-v2.0.0-r1.json"

ROW_RE = re.compile(
    r"^\|\s*(\d+)\s*\|\s*([A-F]{3})\s*\|\s*([+-]?\d+)\s*\|\s*(\d)\s*\|\s*(\d+)\s*\|"
    r"\s*(\S+?)/(\S+?)\s*\|\s*(\S+?)\s*\|\s*(\S+?)\s*\|"
)


def _doc_rows() -> dict[int, dict]:
    lines = DOC_A.read_text(encoding="utf-8").splitlines()
    s = next(i for i, l in enumerate(lines) if l.startswith("## 附录 A"))
    e = next(i for i, l in enumerate(lines) if l.startswith("## 附录 B"))
    out: dict[int, dict] = {}
    for l in lines[s:e]:
        m = ROW_RE.match(l)
        if not m:
            continue
        no, combo, z, p, k, cz, cp, L, S = m.groups()
        out[int(no)] = {
            "combo": combo,
            "z10": int(z),
            "priority": int(p),
            "resonance_k": int(k),
            "canonical_zone": cz,
            "canonical_pool": "UP" if cp == "涨" else "DOWN",
            "long_ceiling": L,
            "short_ceiling": S,
        }
    return out


# ---------------------------------------------------------------------------
def test_invariants_appendix_b():
    checks = mm.verify_invariants()
    assert len(checks) >= 10, checks


def test_generator_reproduces_document_appendix_a_row_by_row():
    doc = _doc_rows()
    assert len(doc) == 216, len(doc)
    gen = {r["combo_no"]: r for r in mm.build_rows()}
    for no in range(1, 217):
        d, g = doc[no], gen[no]
        assert f"{g['m30']}{g['h2']}{g['h6']}" == d["combo"], no
        for f in (
            "z10",
            "priority",
            "resonance_k",
            "canonical_zone",
            "canonical_pool",
            "long_ceiling",
            "short_ceiling",
        ):
            assert g[f] == d[f], (no, f, g[f], d[f])


def test_mapping_hash_matches_document_appendix_c():
    payload = mm.build_payload()
    blob = mm.canonical_bytes(payload)
    assert len(blob) == mm.EXPECTED_MAPPING_BYTES, len(blob)
    assert mm.mapping_hash(payload) == "sha256:" + mm.EXPECTED_MAPPING_SHA256


def test_frozen_file_exists_and_matches_generator():
    assert FROZEN.is_file(), FROZEN
    doc = json.loads(FROZEN.read_text(encoding="utf-8"))
    assert doc["mcap_mapping_version"] == mm.MCAP_MAPPING_VERSION
    assert doc["mapping_hash"] == "sha256:" + mm.EXPECTED_MAPPING_SHA256
    assert doc["payload"]["rows"] == mm.build_rows()
    m = mm.load()
    assert m is not None and len(m.rows) == 216
    assert m.mapping_hash == "sha256:" + mm.EXPECTED_MAPPING_SHA256


def test_distributions():
    rows = mm.build_rows()
    assert Counter(r["priority"] for r in rows) == mm.EXPECTED_PRIORITY_DIST
    assert Counter(r["canonical_zone"] for r in rows) == mm.EXPECTED_CANONICAL_DIST
    assert Counter(r["long_ceiling"] for r in rows) == mm.EXPECTED_SIDE_DIST
    assert Counter(r["short_ceiling"] for r in rows) == mm.EXPECTED_SIDE_DIST
    assert Counter(r["resonance_k"] for r in rows) == mm.EXPECTED_K_DIST


def test_mirror_and_combo_numbering():
    rows = {r["combo_no"]: r for r in mm.build_rows()}
    for no, r in rows.items():
        assert mm.combo_no(r["m30"], r["h2"], r["h6"]) == no
        assert mm.index_combo(no) == (r["m30"], r["h2"], r["h6"])
        mirror_no = 217 - no
        mr = rows[mirror_no]
        assert mr["z10"] == -r["z10"]
        assert mr["long_ceiling"] == r["short_ceiling"]


def test_z10_formula_is_2_3_5_over_q_plus3_to_minus3():
    assert mm.Q_INT == {"A": 3, "B": 2, "C": 1, "D": -1, "E": -2, "F": -3}
    assert mm.WEIGHTS10 == {"30m": 2, "2h": 3, "6h": 5}
    from coin_selection.mcap_combo import z10

    for g30, g2, g6 in mm.iter_combos():
        want = 2 * mm.Q_INT[g30] + 3 * mm.Q_INT[g2] + 5 * mm.Q_INT[g6]
        assert z10(g30, g2, g6) == want


def test_priority_integer_boundaries_do_not_drift():
    """文档 A §9.1：边界 |Z10| = 3/8/16/24 各有 12/12/6/2 组，必须用整数比较。"""
    rows = mm.build_rows()
    for edge, want in ((3, 12), (8, 12), (16, 6), (24, 2)):
        assert sum(1 for r in rows if abs(r["z10"]) == edge) == want, edge


def test_ceiling_lookup_both_directions():
    m = mm.from_generator()
    assert m.ceiling("A", "A", "A", "up") == "DMR"
    assert m.ceiling("A", "A", "A", "down") == "淘汰"
    assert m.ceiling("F", "F", "F", "down") == "DMR"
    assert m.ceiling("A", "A", "F", "up") == "淘汰"  # V0a 正面对冲 + |Z10|<3
    assert m.ceiling("A", "A", "F", "down") == "淘汰"


def test_bad_mapping_is_fail_closed():
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "bad.json"
        doc = json.loads(FROZEN.read_text(encoding="utf-8"))
        doc["payload"]["rows"][0]["long_ceiling"] = "观察"  # 篡改一格
        p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        try:
            mm.load(p, strict=True)
            raise AssertionError("strict load must raise on a tampered mapping")
        except mm.MappingError:
            pass
        assert mm.load(p, strict=False) is None


def test_canonical_pool_is_only_an_index_never_eligibility():
    """规范池只做索引；实际资格必须读方向 ceiling（文档 A §11）。"""
    rows = mm.build_rows()
    # 例：no=22 ADD 规范池是「跌」，但 long ceiling 是淘汰、short ceiling 是观察。
    r = next(x for x in rows if x["combo_no"] == 22)
    assert r["canonical_pool"] == "DOWN"
    assert (r["long_ceiling"], r["short_ceiling"]) == ("淘汰", "观察")


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
