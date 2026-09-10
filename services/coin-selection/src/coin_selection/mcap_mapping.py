"""216 规范归属 + 432 方向 ceiling 的**唯一机器事实源**（ChatGpt_SOL5.6 口径）。

权威依据
--------
* 文档 A：``docs/ChatGpt_SOL5.6_新五区币种选入标准报告_升级版_v2.0.0.md``
  §9.1（combo_no / Z10 枚举）、§9.2（结构共振度 K）、§9.4（方向 ceiling 生成器）、
  附录 A（216 行完整表）、附录 B（映射不变量）、附录 C（mapping_hash 规范序列化）。
* 文档 B：``docs/ChatGpt_SOL5.6_《选币榜Y》与《复盘选币》……整体设计流程方案计划.md``
  §5.1（mcap_mapping_version / mapping_hash 语义）、§20（映射测试矩阵）。

设计裁决（文档 A §9.3）
-----------------------
「修正版生成器为机器事实，Claude CSV 作期望快照，Hermes CSV 作消融基线，禁止拼表」。
本模块的生成器就是那个机器事实：**函数式重算**，不是读 CSV。生成结果再与冻结的
``packages/config/mcap-216-v2.0.0-r1.json`` 逐行核对，并核对文档 A 附录 C 的
``mapping_hash``；任一不符即 fail-closed，不允许半表运行（文档 A §14）。

与既有模块的关系
----------------
* ``mcap_combo``（第一轮 Hermes_Grok4.6 落地）已经实现了文档 A §9.1/§9.2/§9.4 的
  全部**功能字段**（Z10 / 优先级 / K / 两侧 ceiling），并经本轮逐行核对与附录 A
  **零差异**。因此本模块**直接复用它的纯函数**，不写第二套生成器（红线：同一件事
  只允许有一个实现）。
* ``mcap_zone``（第二轮 Claude_Opus5 落地）是另一套切点式天花板（每侧 13/22/32/72/77），
  与文档 A 的 12/23/29/53/99 不同口径。本轮按文档 A §4.1 的裁决顺序（本任务明确目标 >
  运行时事实 > 旧报告）以 ChatGpt_SOL5.6 为准，``mcap_zone`` 保留为消融基线，
  不再是《选币榜Y》有效区的事实源。切换开关见 ``mcap_effective.MODE_*``。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from itertools import product
from pathlib import Path
from typing import Any, Iterable, Optional

from .mcap_combo import (  # 单一生成器：不在本文件重写任何一条判定
    GRADES,
    MIRROR,
    Q,
    STRUCT,
    ZONE_CN,
    ZONE_EN,
    combo_no,
    index_combo,
    priority,
    resonance_k,
    z10,
    zone_long,
    zone_short,
)

log = logging.getLogger("coin_selection.mcap_mapping")

#: 文档 A §1 / 文档 B §1：本轮目标 mcap 映射的语义版本。
MCAP_MAPPING_VERSION = "mcap-216-v2.0.0-r1"

#: 文档 A 附录 C：hash payload 内部键 ``schema_version`` 是 JSON integer 1。
MAPPING_SCHEMA_VERSION = 1

#: 文档 A 附录 C / 文档 B §1：候选 mapping_hash（本轮已实测复算通过）。
EXPECTED_MAPPING_SHA256 = (
    "da14429d62614a22096fa15cc93a66c9465e4d62597737844e97c552dc6d1689"
)

#: 文档 A 附录 C：规范序列化后的字节长度，作为第二道防篡改校验。
EXPECTED_MAPPING_BYTES = 39368

MAPPING_FILE_NAME = "mcap-216-v2.0.0-r1.json"

REPO_ROOT = Path(
    os.environ.get("HERMES_ROOT", str(Path(__file__).resolve().parents[4]))
)

#: 文档 A 附录 A/C：行对象只允许这 11 个键，顺序无关（序列化时 sort_keys）。
ROW_FIELDS: tuple[str, ...] = (
    "combo_no",
    "m30",
    "h2",
    "h6",
    "z10",
    "priority",
    "resonance_k",
    "canonical_zone",
    "canonical_pool",
    "long_ceiling",
    "short_ceiling",
)

#: 文档 A 附录 C：枚举精确冻结，中文分区名不得替换为英文或池名。
ZONE_ENUM: tuple[str, ...] = ("DMR", "确定", "符合", "观察", "淘汰")
POOL_ENUM: tuple[str, ...] = ("UP", "DOWN")

#: 文档 A §5：业务等级 rank，越小越好。DMR=0 只是派生展示等级。
ZONE_RANK_CN: dict[str, int] = {z: i for i, z in enumerate(ZONE_ENUM)}

#: 文档 A §9.1：优先级整数边界 3/8/16/24 → 28/54/74/46/14。
PRIORITY_ABS_Z10 = {"p1_lt": 3, "p2_lt": 8, "p3_lt": 16, "p4_lt": 24, "p5_gte": 24}

#: 文档 A §9.1：q(A..F) = +3/+2/+1/-1/-2/-3（= 2·结构轴 + 动能轴）。
Q_INT: dict[str, int] = {g: int(Q[g]) for g in GRADES}

#: 文档 A §9.1：Z10 = 2·q30m + 3·q2h + 5·q6h。
WEIGHTS10 = {"30m": 2, "2h": 3, "6h": 5}

#: 文档 A 附录 B：期望分布，加载时逐项断言。
EXPECTED_PRIORITY_DIST = {1: 28, 2: 54, 3: 74, 4: 46, 5: 14}
EXPECTED_CANONICAL_DIST = {"DMR": 24, "确定": 46, "符合": 58, "观察": 70, "淘汰": 18}
EXPECTED_SIDE_DIST = {"DMR": 12, "确定": 23, "符合": 29, "观察": 53, "淘汰": 99}
EXPECTED_K_DIST = {100: 54, 70: 54, 40: 54, 10: 54}


class MappingError(RuntimeError):
    """映射不变量失败。文档 A §14：任一失败则关闭新层并告警，不能半表运行。"""


# ---------------------------------------------------------------------------
# 规范归属（canonical zone / pool）
# ---------------------------------------------------------------------------
def canonical_of(g30: str, g2: str, g6: str) -> tuple[str, str]:
    """规范分区与规范池（文档 A 附录 A 的「规范」列）。

    规则（与附录 A 全 216 行逐行一致，本轮已实测 0 差异）：

    1. 两侧 ceiling 取更好（rank 更小）的一侧，该侧即规范池；
    2. 两侧相同则进入 tie-break：
       * ``abs(Z10) >= 3``（优先级 ≥2，方向可判）→ 按 ``sign(Z10)`` 定池；
       * ``abs(Z10) < 3``（优先级 1，方向不可判）→ 退回 6h 战略轴 ``sign(s6)``。

    第 2 条的两段来自文档 A §8.1「6h 定战略方向和最高结构背景」：|Z10| 落在优先级 1
    区间时合成方向分本身没有方向含义，只能由 6h 结构轴给出规范池。

    【限制条件】规范池只用于索引/默认展示（文档 A §11）；**实际资格必须读方向
    ceiling**，任何调用方都不得用 canonical_pool 判定准入。
    """
    lo = ZONE_CN[zone_long(g30, g2, g6)]
    sh = ZONE_CN[zone_short(g30, g2, g6)]
    rl, rs = ZONE_RANK_CN[lo], ZONE_RANK_CN[sh]
    if rl < rs:
        return lo, "UP"
    if rs < rl:
        return sh, "DOWN"
    z = z10(g30, g2, g6)
    if abs(z) >= PRIORITY_ABS_Z10["p1_lt"]:
        return lo, ("UP" if z > 0 else "DOWN")
    return lo, ("UP" if STRUCT[g6] > 0 else "DOWN")


def row_for(g30: str, g2: str, g6: str) -> dict[str, Any]:
    """单行规范对象（文档 A 附录 C 的行形状，所有数字均为 JSON integer）。"""
    canon_zone, canon_pool = canonical_of(g30, g2, g6)
    return {
        "combo_no": combo_no(g30, g2, g6),
        "m30": g30,
        "h2": g2,
        "h6": g6,
        "z10": z10(g30, g2, g6),
        "priority": priority(g30, g2, g6),
        "resonance_k": resonance_k(g30, g2, g6),
        "canonical_zone": canon_zone,
        "canonical_pool": canon_pool,
        "long_ceiling": ZONE_CN[zone_long(g30, g2, g6)],
        "short_ceiling": ZONE_CN[zone_short(g30, g2, g6)],
    }


def build_rows() -> list[dict[str, Any]]:
    """216 行，按 ``combo_no`` 升序（文档 A 附录 C 要求的行顺序）。"""
    rows = [row_for(a, b, c) for a, b, c in product(GRADES, repeat=3)]
    rows.sort(key=lambda r: r["combo_no"])
    return rows


def build_payload(rows: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
    """文档 A 附录 C 冻结的 hash payload。

    【建议规则】payload 内部键保留 ``mapping_version``（冻结序列化合同的一部分）；
    RuleManifest 对外的无歧义字段名是 ``mcap_mapping_version``，两者取值必须精确相等，
    但与 ``asset_mapping_version``（CoinGecko 资产映射）没有任何别名关系（文档 B §5.1）。
    """
    return {
        "mapping_version": MCAP_MAPPING_VERSION,
        "schema_version": MAPPING_SCHEMA_VERSION,
        "order": ["30m", "2h", "6h"],
        "q": dict(Q_INT),
        "weights10": dict(WEIGHTS10),
        "priority_abs_z10": dict(PRIORITY_ABS_Z10),
        "rows": rows if rows is not None else build_rows(),
    }


def canonical_bytes(payload: dict[str, Any]) -> bytes:
    """文档 A 附录 C 的规范字节：UTF-8 + 键排序 + 无空白。

    payload 不含浮点数，因此 ``json.dumps(sort_keys=True, separators=(",", ":"))``
    与 JCS 口径一致。
    """
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def mapping_hash(payload: Optional[dict[str, Any]] = None) -> str:
    """``sha256:<hex>``，即 RuleManifest 的 ``mapping_hash`` 字段取值。"""
    blob = canonical_bytes(payload if payload is not None else build_payload())
    return "sha256:" + hashlib.sha256(blob).hexdigest()


# ---------------------------------------------------------------------------
# 不变量（文档 A 附录 B）
# ---------------------------------------------------------------------------
def verify_invariants(rows: Optional[list[dict[str, Any]]] = None) -> list[str]:
    """逐条断言文档 A 附录 B 的映射不变量。失败抛 :class:`MappingError`。"""
    rs = rows if rows is not None else build_rows()
    ok: list[str] = []

    def req(cond: bool, msg: str) -> None:
        if not cond:
            raise MappingError(msg)

    req(len(rs) == 216, f"len(rows)={len(rs)} != 216")
    ok.append("len(rows)=216")

    req(len({r["combo_no"] for r in rs}) == 216, "combo_no not unique")
    ok.append("unique(combo_no)=216")

    triples = {(r["m30"], r["h2"], r["h6"]) for r in rs}
    req(len(triples) == 216, "triples not unique")
    ok.append("unique(30m,2h,6h)=216")

    by_no = {r["combo_no"]: r for r in rs}
    for r in rs:
        m = (MIRROR[r["m30"]], MIRROR[r["h2"]], MIRROR[r["h6"]])
        mno = combo_no(*m)
        req(mno == 217 - r["combo_no"], f"combo_no(mirror) broken at {r['combo_no']}")
        mr = by_no[mno]
        req(mr["z10"] == -r["z10"], f"Z10(mirror) broken at {r['combo_no']}")
        req(mr["priority"] == r["priority"], f"priority(mirror) broken at {r['combo_no']}")
        req(mr["resonance_k"] == r["resonance_k"], f"K(mirror) broken at {r['combo_no']}")
        req(
            mr["long_ceiling"] == r["short_ceiling"],
            f"ceiling_short(x)!=ceiling_long(mirror(x)) at {r['combo_no']}",
        )
        req(
            mr["canonical_zone"] == r["canonical_zone"],
            f"canonical_zone(mirror) broken at {r['combo_no']}",
        )
    ok.append("combo_no/Z10/priority/K/ceiling/canonical_zone 镜像不变量全部成立")

    pdist: dict[int, int] = {}
    for r in rs:
        pdist[r["priority"]] = pdist.get(r["priority"], 0) + 1
    req(pdist == EXPECTED_PRIORITY_DIST, f"priority distribution {pdist}")
    ok.append("priority distribution P1..P5=28/54/74/46/14")

    kdist: dict[int, int] = {}
    for r in rs:
        kdist[r["resonance_k"]] = kdist.get(r["resonance_k"], 0) + 1
    req(kdist == EXPECTED_K_DIST, f"K distribution {kdist}")
    ok.append("resonance K distribution 100/70/40/10 = 54/54/54/54")

    cdist: dict[str, int] = {}
    for r in rs:
        cdist[r["canonical_zone"]] = cdist.get(r["canonical_zone"], 0) + 1
    req(cdist == EXPECTED_CANONICAL_DIST, f"canonical distribution {cdist}")
    ok.append("canonical distribution=24/46/58/70/18")

    for side in ("long_ceiling", "short_ceiling"):
        sdist: dict[str, int] = {}
        for r in rs:
            sdist[r[side]] = sdist.get(r[side], 0) + 1
        req(sdist == EXPECTED_SIDE_DIST, f"{side} distribution {sdist}")
    ok.append("each-side ceiling distribution=12/23/29/53/99")

    for r in rs:
        req(r["canonical_zone"] in ZONE_ENUM, f"bad canonical_zone {r}")
        req(r["canonical_pool"] in POOL_ENUM, f"bad canonical_pool {r}")
        req(r["long_ceiling"] in ZONE_ENUM, f"bad long_ceiling {r}")
        req(r["short_ceiling"] in ZONE_ENUM, f"bad short_ceiling {r}")
        req(set(r.keys()) == set(ROW_FIELDS), f"row field set drift at {r['combo_no']}")
        for k in ("combo_no", "z10", "priority", "resonance_k"):
            req(isinstance(r[k], int) and not isinstance(r[k], bool), f"{k} not int")
    ok.append("枚举与字段集合精确（DMR/确定/符合/观察/淘汰、UP/DOWN、11 个键、整数）")

    payload = build_payload(rs)
    blob = canonical_bytes(payload)
    req(
        len(blob) == EXPECTED_MAPPING_BYTES,
        f"canonical bytes {len(blob)} != {EXPECTED_MAPPING_BYTES}",
    )
    got = "sha256:" + hashlib.sha256(blob).hexdigest()
    req(
        got == "sha256:" + EXPECTED_MAPPING_SHA256,
        f"mapping_hash {got} != sha256:{EXPECTED_MAPPING_SHA256}",
    )
    ok.append(f"mapping_hash == sha256:{EXPECTED_MAPPING_SHA256[:16]}…（39368 字节）")
    return ok


# ---------------------------------------------------------------------------
# 加载 / 落盘
# ---------------------------------------------------------------------------
def mapping_path(root: Optional[Path] = None) -> Path:
    base = Path(root) if root is not None else REPO_ROOT
    return base / "packages" / "config" / MAPPING_FILE_NAME


class McapMapping:
    """已校验的 216/432 映射。构造成功即代表附录 B 全部不变量成立。"""

    __slots__ = ("rows", "by_no", "by_combo", "mapping_version", "mapping_hash", "source")

    def __init__(
        self,
        rows: list[dict[str, Any]],
        *,
        mapping_version: str,
        hash_str: str,
        source: str,
    ) -> None:
        self.rows = rows
        self.by_no = {r["combo_no"]: r for r in rows}
        self.by_combo = {f"{r['m30']}{r['h2']}{r['h6']}": r for r in rows}
        self.mapping_version = mapping_version
        self.mapping_hash = hash_str
        self.source = source

    # —— 查询 ——
    def row(self, g30: str, g2: str, g6: str) -> dict[str, Any]:
        return self.by_combo[f"{g30}{g2}{g6}"]

    def ceiling(self, g30: str, g2: str, g6: str, direction: str) -> str:
        """方向 ceiling（中文枚举）。``direction`` ∈ {up, down, long, short}。"""
        r = self.row(g30, g2, g6)
        key = "long_ceiling" if str(direction).lower() in ("up", "long") else "short_ceiling"
        return r[key]

    def as_payload(self) -> dict[str, Any]:
        return build_payload(self.rows)


def load(
    path: Optional[Path] = None,
    *,
    strict: bool = True,
    root: Optional[Path] = None,
) -> Optional[McapMapping]:
    """读冻结映射并全量校验。

    文档 A §14：「映射加载时必须断言 216 唯一、432 ceiling 完整、优先级分布正确、
    镜像成立、mapping_hash 匹配；任一失败则关闭新层并告警，不能半表运行。」

    * ``strict=True``（默认，生产在线路径用）：任一失败抛 :class:`MappingError`，
      调用方必须把有效区层降级为 off；
    * ``strict=False``（离线诊断用）：失败返回 ``None`` 并 WARN。
    """
    p = Path(path) if path is not None else mapping_path(root)
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
        payload = doc.get("payload") if isinstance(doc, dict) and "payload" in doc else doc
        rows = payload["rows"]
        version = str(payload.get("mapping_version") or "")
        if version != MCAP_MAPPING_VERSION:
            raise MappingError(
                f"mapping_version {version!r} != {MCAP_MAPPING_VERSION!r}"
            )
        verify_invariants(rows)
        # 生成器 == 冻结文件：设计裁决要求生成器是机器事实，文件只是它的冻结副本。
        gen = build_rows()
        if gen != rows:
            bad = next(
                (g["combo_no"] for g, r in zip(gen, rows) if g != r), None
            )
            raise MappingError(f"frozen mapping diverges from generator at combo_no={bad}")
        h = mapping_hash(build_payload(rows))
        return McapMapping(
            rows, mapping_version=version, hash_str=h, source=str(p)
        )
    except MappingError:
        if strict:
            raise
        log.warning("mcap mapping invariant failure at %s — layer disabled", p)
        return None
    except Exception as e:  # noqa: BLE001
        if strict:
            raise MappingError(f"cannot load mcap mapping {p}: {e}") from e
        log.warning("mcap mapping unreadable (%s): %s — layer disabled", p, e)
        return None


def from_generator() -> McapMapping:
    """不读盘，直接用生成器构造（单测与 CI 断言用）。"""
    rows = build_rows()
    verify_invariants(rows)
    return McapMapping(
        rows,
        mapping_version=MCAP_MAPPING_VERSION,
        hash_str=mapping_hash(build_payload(rows)),
        source="generator",
    )


def dump(path: Optional[Path] = None, *, root: Optional[Path] = None) -> Path:
    """把生成器结果冻结到 JSON。写入前先跑全部不变量。"""
    rows = build_rows()
    verify_invariants(rows)
    payload = build_payload(rows)
    doc = {
        "$comment": [
            "《选币榜Y》216 规范归属 + 432 方向 ceiling —— ChatGpt_SOL5.6 文档A 附录 A 冻结副本。",
            "唯一机器事实源是 coin_selection.mcap_mapping 的生成器；本文件是它的冻结快照，",
            "加载时逐行核对生成器输出 + 附录 B 全部不变量 + 附录 C 的 mapping_hash。",
            "payload 内部键 mapping_version 属于冻结序列化合同，不随外层字段改名。",
            "禁止手工编辑：改任何一行都必须重算 mapping_hash 并发新 mcap_mapping_version。",
        ],
        "mcap_mapping_version": MCAP_MAPPING_VERSION,
        "mapping_hash": mapping_hash(payload),
        "canonical_bytes": len(canonical_bytes(payload)),
        "source_document": (
            "docs/ChatGpt_SOL5.6_新五区币种选入标准报告_升级版_v2.0.0.md 附录A/附录B/附录C"
        ),
        "census": {
            "priority": {str(k): v for k, v in EXPECTED_PRIORITY_DIST.items()},
            "canonical": dict(EXPECTED_CANONICAL_DIST),
            "per_side_ceiling": dict(EXPECTED_SIDE_DIST),
            "resonance_k": {str(k): v for k, v in EXPECTED_K_DIST.items()},
        },
        "payload": payload,
    }
    p = Path(path) if path is not None else mapping_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(
        json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(tmp, p)
    return p


def iter_combos() -> Iterable[tuple[str, str, str]]:
    return product(GRADES, repeat=3)


__all__ = [
    "MCAP_MAPPING_VERSION",
    "EXPECTED_MAPPING_SHA256",
    "EXPECTED_MAPPING_BYTES",
    "ZONE_ENUM",
    "ZONE_RANK_CN",
    "ZONE_CN",
    "ZONE_EN",
    "MappingError",
    "McapMapping",
    "build_rows",
    "build_payload",
    "canonical_bytes",
    "canonical_of",
    "combo_no",
    "index_combo",
    "dump",
    "from_generator",
    "load",
    "mapping_hash",
    "mapping_path",
    "verify_invariants",
]
