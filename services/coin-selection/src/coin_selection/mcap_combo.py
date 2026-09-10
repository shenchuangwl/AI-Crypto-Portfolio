"""216 流通市值组合天花板（选币榜Y To-Be，阶段 1 纯函数）。

权威：``docs/Hermes_Grok4.6_新五区币种选入标准报告_升级版_v2.0.0.md`` §4–§5。
生产口径每侧 DMR12 / 确定23 / 符合29 / 观察53 / 淘汰99（SOL / Claude CSV）。
Grok 10/32/66/82/26 **不是**本模块的默认。

本模块无 I/O 副作用（``load_mapping`` / ``dump_mapping`` 除外）、不改状态机边、
不进 ``raw`` Score。``ENABLE_MCAP_ZONE`` 的接线在阶段 3；阶段 1 即使有人误开
env，缺投影接入也不会改分区。

JSON 缺失 / 行数 ≠ 216 / 解析失败：``load_mapping`` 返回空表并记 WARN，
调用方必须当 ENABLE=0，不得抛到 15m 主循环。
"""

from __future__ import annotations

import json
import logging
import os
from itertools import product
from pathlib import Path
from typing import Any, Iterable, Optional

log = logging.getLogger("coin_selection.mcap_combo")

GRADES = "ABCDEF"
STRUCT: dict[str, int] = {"A": 1, "B": 1, "C": 1, "D": -1, "E": -1, "F": -1}
MOM: dict[str, int] = {"A": 1, "B": 0, "C": -1, "D": 1, "E": 0, "F": -1}
Q: dict[str, int] = {g: 2 * STRUCT[g] + MOM[g] for g in GRADES}
MIRROR: dict[str, str] = {"A": "F", "B": "E", "C": "D", "D": "C", "E": "B", "F": "A"}

# 产品分区英文（状态机 / 复盘）↔ 中文（216 表 / CSV）
ZONE_CN: dict[str, str] = {
    "DMR": "DMR",
    "CONFIRMED": "确定",
    "QUALIFIED": "符合",
    "WATCH": "观察",
    "ELIMINATED": "淘汰",
}
ZONE_EN: dict[str, str] = {v: k for k, v in ZONE_CN.items()}
ZONE_EN.update({k: k for k in ZONE_CN})  # English aliases also accepted

# rank: 越小越好。DMR 只作天花板身份；层 A 的 SM 没有第五态。
RANK: dict[str, int] = {
    "DMR": 0,
    "CONFIRMED": 1,
    "QUALIFIED": 2,
    "WATCH": 3,
    "ELIMINATED": 4,
}

W_BASE: dict[str, float] = {
    "DMR": 1.00,
    "CONFIRMED": 0.85,
    "QUALIFIED": 0.70,
    "WATCH": 0.40,
    "ELIMINATED": 0.00,
}
W_K: dict[int, float] = {100: 1.10, 70: 1.00, 40: 0.85, 10: 0.65}

COMBO_JSON_NAME = "mcap_combo_216.json"
_BULL = frozenset("ABC")
_AB = frozenset("AB")
_DE = frozenset("DE")

REPO_ROOT = Path(
    os.environ.get("HERMES_ROOT", str(Path(__file__).resolve().parents[4]))
)


def q_of(grade: str) -> int:
    return Q[grade]


def combo_no(g30: str, g2: str, g6: str) -> int:
    """1..216. ``36·idx(30m) + 6·idx(2h) + idx(6h) + 1``."""
    i = GRADES.index
    return 36 * i(g30) + 6 * i(g2) + i(g6) + 1


def index_combo(n: int) -> tuple[str, str, str]:
    n -= 1
    return GRADES[n // 36], GRADES[(n // 6) % 6], GRADES[n % 6]


def z10(g30: str, g2: str, g6: str) -> int:
    """整数合成分 ``Z10 = 2·q30 + 3·q2 + 5·q6``。展示 Z = Z10/10。"""
    return 2 * Q[g30] + 3 * Q[g2] + 5 * Q[g6]


def zs10(g30: str, g2: str, g6: str) -> int:
    """结构分量：``Zs10 = 2·s30 + 3·s2 + 5·s6``，满足 ``Z = 2·Zs + Zm``。"""
    return 2 * STRUCT[g30] + 3 * STRUCT[g2] + 5 * STRUCT[g6]


def zm10(g30: str, g2: str, g6: str) -> int:
    return 2 * MOM[g30] + 3 * MOM[g2] + 5 * MOM[g6]


def priority(g30: str, g2: str, g6: str) -> int:
    """``|Z|`` 分箱，精确有理数口径：14 / 46 / 74 / 54 / 28。禁止 PDF 32/54/72/44/14。"""
    a = abs(z10(g30, g2, g6))
    if a >= 24:  # |Z| ≥ 2.4
        return 5
    if a >= 16:  # 1.6
        return 4
    if a >= 8:  # 0.8
        return 3
    if a >= 3:  # 0.3
        return 2
    return 1


def resonance_k(g30: str, g2: str, g6: str) -> int:
    s30, s2, s6 = STRUCT[g30], STRUCT[g2], STRUCT[g6]
    if s6 == s2 == s30:
        return 100
    if s6 == s2 != s30:
        return 70
    if s6 == s30 != s2:
        return 40
    return 10  # s6 ≠ s2 == s30


def _norm_zone(z: Optional[str]) -> Optional[str]:
    if z is None:
        return None
    s = str(z).strip()
    if not s:
        return None
    if s in RANK:
        return s
    if s in ZONE_EN:
        return ZONE_EN[s]
    raise KeyError(f"unknown zone {z!r}")


def zone_long(g30: str, g2: str, g6: str) -> str:
    """上涨侧天花板。SOL §3.3 整数阈值，自上而下首个命中。"""
    s30, s2, s6 = STRUCT[g30], STRUCT[g2], STRUCT[g6]
    z = z10(g30, g2, g6)
    k = resonance_k(g30, g2, g6)
    # V0a 正面对冲且 |Z|<0.3（含 AAF / FFA）
    if k == 10 and abs(z) < 3:
        return "ELIMINATED"
    # V0b 6h 空且 2h 或 30m 也空
    if s6 < 0 and (s2 < 0 or s30 < 0):
        return "ELIMINATED"
    # L1 DMR
    if (
        s6 > 0
        and s2 > 0
        and s30 > 0
        and g6 in _AB
        and g2 in _AB
        and z >= 18
    ):
        return "DMR"
    # L2 确定
    if s6 > 0 and s2 > 0 and z >= 10 and (
        s30 > 0 or (g6 in _AB and g2 in _AB and g30 in _DE)
    ):
        return "CONFIRMED"
    # L3 符合
    if s6 > 0 and z >= 5 and (
        s2 > 0 or (s2 < 0 and s30 > 0 and g6 in _AB and g30 in _AB)
    ):
        return "QUALIFIED"
    return "WATCH"


def zone_short(g30: str, g2: str, g6: str) -> str:
    """下跌侧 = ``zone_long(mirror, mirror, mirror)``。"""
    return zone_long(MIRROR[g30], MIRROR[g2], MIRROR[g6])


def canon_of(g30: str, g2: str, g6: str) -> tuple[str, str]:
    """规范分区 = 两侧天花板中更好（rank 更小）的一侧；平手按 Z 符号选池。"""
    lo = zone_long(g30, g2, g6)
    sh = zone_short(g30, g2, g6)
    rl, rs = RANK[lo], RANK[sh]
    if rl < rs:
        return lo, "上涨"
    if rs < rl:
        return sh, "下跌"
    z = z10(g30, g2, g6)
    pool = "上涨" if z > 0 else "下跌"
    return lo, pool


def worse(zone_a: Optional[str], zone_b: Optional[str]) -> str:
    """最终分区 = rank 更大（更差）的一侧。``None`` 组合视为观察天花板。永不升。

    层 A 的 SM 没有 DMR 态：``worse(CONFIRMED, DMR) == CONFIRMED``
    （天花板 DMR 只解除阻挡，不能把确认「升」成一个新的 SM 态）。
    """
    a = _norm_zone(zone_a) or "WATCH"
    b = _norm_zone(zone_b) or "WATCH"
    # DMR as a ceiling against an SM state: treat DMR as CONFIRMED for the SM axis.
    a_sm = "CONFIRMED" if a == "DMR" else a
    b_sm = "CONFIRMED" if b == "DMR" else b
    return a_sm if RANK[a_sm] >= RANK[b_sm] else b_sm


def apply_hysteresis(
    sm_zone: str,
    ceiling: Optional[str],
    *,
    prev_product: Optional[str] = None,
    prev_streak: int = 0,
) -> tuple[str, int]:
    """不对称滞回：确定→符合立即；符合→观察 / 观察→淘汰 满 2 节点。

    返回 ``(product_zone, new_streak)``。不改 SM 边。G1/SM 已淘汰立即落地。
    ``ceiling is None``（缺级）视为观察天花板。
    """
    sm = _norm_zone(sm_zone) or "WATCH"
    if sm == "ELIMINATED":
        return "ELIMINATED", 0
    raw = worse(sm, ceiling)
    prev = _norm_zone(prev_product) or sm
    prev_r, raw_r = RANK[prev], RANK[raw]
    if raw_r <= prev_r:
        # 持平或天花板抬升（只解除阻挡）
        return raw, 0
    # 降级
    if prev_r <= RANK["CONFIRMED"]:
        # 确定 → 符合及以下：立即
        return raw, 0
    # 符合→观察、观察→淘汰：2 节点
    streak = int(prev_streak or 0) + 1
    if streak >= 2:
        return raw, 0
    return prev, streak


def w_prio(pri: int) -> float:
    return 0.60 + 0.10 * int(pri)


def row_dict(g30: str, g2: str, g6: str) -> dict[str, Any]:
    lo = zone_long(g30, g2, g6)
    sh = zone_short(g30, g2, g6)
    canon_z, canon_pool = canon_of(g30, g2, g6)
    pri = priority(g30, g2, g6)
    k = resonance_k(g30, g2, g6)
    return {
        "no": combo_no(g30, g2, g6),
        "g30": g30,
        "g2": g2,
        "g6": g6,
        "combo": f"{g30}{g2}{g6}",
        "Z10": z10(g30, g2, g6),
        "K": k,
        "pri": pri,
        "ceiling_long": ZONE_CN[lo],
        "ceiling_short": ZONE_CN[sh],
        "canon_zone": ZONE_CN[canon_z],
        "canon_pool": canon_pool,
        "W_base_long": W_BASE[lo],
        "W_base_short": W_BASE[sh],
        "W_prio": round(w_prio(pri), 2),
        "W_K": W_K[k],
    }


def generate_rows() -> list[dict[str, Any]]:
    rows = [row_dict(a, b, c) for a, b, c in product(GRADES, repeat=3)]
    rows.sort(key=lambda r: r["no"])
    return rows


def mapping_path(root: Optional[Path] = None) -> Path:
    base = Path(root) if root is not None else REPO_ROOT
    return base / "packages" / "config" / COMBO_JSON_NAME


def load_mapping(path: Optional[Path] = None) -> list[dict[str, Any]]:
    """读 216 表。坏表返回 ``[]`` 并 WARN，永不抛给扫描循环。"""
    p = Path(path) if path is not None else mapping_path()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        log.warning("mcap combo JSON missing at %s — treat ENABLE_MCAP_ZONE=0", p)
        return []
    except Exception as e:  # noqa: BLE001
        log.warning("mcap combo JSON unreadable (%s): %s — treat ENABLE=0", p, e)
        return []
    rows = raw["rows"] if isinstance(raw, dict) else raw
    if not isinstance(rows, list) or len(rows) != 216:
        log.warning(
            "mcap combo JSON %s has %s rows (want 216) — treat ENABLE=0",
            p,
            len(rows) if isinstance(rows, list) else type(rows).__name__,
        )
        return []
    return rows


def dump_mapping(path: Optional[Path] = None) -> Path:
    p = Path(path) if path is not None else mapping_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "$comment": [
            "选币榜Y 216 组合天花板（生产默认 SOL/Claude 12/23/29/53/99）。",
            "字母 = mcap_timeframe.grade_from_mas（禁止 PDF B/C/D 赋义）。",
            "本表不进 raw Score；只作天花板与排序键。ENABLE_MCAP_ZONE 默认 0。",
        ],
        "version": "mcap-combo-216-v1.0.0",
        "census_long": {"DMR": 12, "确定": 23, "符合": 29, "观察": 53, "淘汰": 99},
        "census_canon": {"DMR": 24, "确定": 46, "符合": 58, "观察": 70, "淘汰": 18},
        "rows": generate_rows(),
    }
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, p)
    return p


def lookup(
    g30: Optional[str],
    g2: Optional[str],
    g6: Optional[str],
    *,
    rows: Optional[Iterable[dict[str, Any]]] = None,
) -> Optional[dict[str, Any]]:
    """三周期都非空才查表；任一 ``None`` / 非法字母 → ``None``（观察天花板）。"""
    if not isinstance(g30, str) or not isinstance(g2, str) or not isinstance(g6, str):
        return None
    if g30 not in GRADES or g2 not in GRADES or g6 not in GRADES:
        return None
    table = list(rows) if rows is not None else load_mapping()
    if not table:
        # 无表时仍可用纯函数（阶段 1 单测 / 离线）；生产投影应先 load。
        return row_dict(g30, g2, g6)
    n = combo_no(g30, g2, g6)
    for r in table:
        if int(r.get("no") or 0) == n:
            return r
    return row_dict(g30, g2, g6)


SM_TO_ZONE = {
    "CONFIRMED": "CONFIRMED",
    "QUALIFIED": "QUALIFIED",
    "WATCH": "WATCH",
    "ELIMINATED": "ELIMINATED",
}

_HYST_FILE = "combo_hysteresis.json"


def _bool_env(name: str) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return False
    return raw.strip().lower() in ("1", "true", "yes", "on")


def zone_enabled_for_variant(settings: Any, *, variant_key: str = "") -> bool:
    """Y 才认 ENABLE_MCAP_ZONE env；主板永不被该 env 打开。"""
    if bool(getattr(settings, "enable_mcap_zone", False)):
        return True
    if variant_key == "y" and _bool_env("ENABLE_MCAP_ZONE"):
        return True
    return False


def _sm_zone(state: Optional[str]) -> Optional[str]:
    if not state:
        return None
    return SM_TO_ZONE.get(str(state))


def _load_hysteresis(data_dir: Path) -> dict[str, dict[str, Any]]:
    path = Path(data_dir) / _HYST_FILE
    if not path.is_file():
        return {}
    try:
        doc = json.loads(path.read_text(encoding="utf-8")) or {}
        return dict(doc.get("rows") or {})
    except Exception as e:  # noqa: BLE001
        log.warning("combo hysteresis unreadable (%s); starting empty", e)
        return {}


def _save_hysteresis(data_dir: Path, rows: dict[str, dict[str, Any]], scan_id: str) -> None:
    path = Path(data_dir) / _HYST_FILE
    payload = {
        "scan_id": scan_id,
        "rows": rows,
        "note": "216 combo demote streak; cleared on 24h cycle reset",
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def clear_hysteresis(data_dir: Path) -> None:
    path = Path(data_dir) / _HYST_FILE
    if path.is_file():
        try:
            path.unlink()
        except OSError as e:
            log.warning("combo hysteresis clear failed: %s", e)


def apply_to_board(
    rows: list[dict[str, Any]],
    *,
    enabled: bool,
    data_dir: Optional[Path] = None,
    scan_id: str = "",
    mapping: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """旁路合取：给每侧打 combo / ceiling / product_zone，挡 DMR。不改 SM 边。

    ``enabled=False`` 或 216 表不可用：不写字段，行为 ≡ 关开关。
    """
    out: dict[str, Any] = {
        "enabled": False,
        "demotes": [],
        "blocked_dmr": 0,
        "incomplete": 0,
        "reason": None,
    }
    if not enabled:
        return out
    table = list(mapping) if mapping is not None else load_mapping()
    if not table:
        out["reason"] = "mapping_unavailable"
        log.warning("ENABLE_MCAP_ZONE on but 216 mapping missing/invalid — treating as 0")
        return out
    hyst = _load_hysteresis(Path(data_dir)) if data_dir is not None else {}
    new_hyst: dict[str, dict[str, Any]] = {}
    demotes: list[dict[str, Any]] = []
    blocked = 0
    incomplete = 0
    for r in rows:
        g30, g2, g6 = r.get("mcap_grade_30m"), r.get("mcap_grade_2h"), r.get("mcap_grade_6h")
        info = lookup(g30, g2, g6, rows=table)
        combo = None if info is None else info.get("combo")
        z10v = None if info is None else info.get("Z10")
        kv = None if info is None else info.get("K")
        r["mcap_combo"] = combo
        r["mcap_z10"] = z10v
        r["mcap_k"] = kv
        if info is None:
            incomplete += 1
        for direction, state_key, ceil_key in (
            ("up", "state_up", "ceiling_long"),
            ("down", "state_down", "ceiling_short"),
        ):
            sm_state = r.get(state_key)
            smz = _sm_zone(sm_state)
            ceiling = None if info is None else ZONE_EN.get(info.get(ceil_key) or "", info.get(ceil_key))
            key = f"{r.get('symbol')}|{direction}"
            prev = hyst.get(key) or {}
            if smz is None:
                product = sm_state
                streak = 0
                reason = "isolated_or_none"
            else:
                product, streak = apply_hysteresis(
                    smz,
                    ceiling,
                    prev_product=prev.get("product"),
                    prev_streak=int(prev.get("streak") or 0),
                )
                reason = None
                if ceiling is None:
                    reason = "mcap_combo_incomplete"
                elif RANK[product] > RANK[smz if smz != "DMR" else "CONFIRMED"]:
                    reason = f"combo_cap_{ZONE_CN.get(product, product)}"
            r[f"product_zone_{direction}"] = product
            r[f"mcap_ceiling_{direction}"] = None if ceiling is None else ZONE_CN.get(ceiling, ceiling)
            r[f"combo_reason_{direction}"] = reason
            r[f"combo_demote_streak_{direction}"] = streak
            block = False
            if sm_state == "CONFIRMED":
                if ceiling != "DMR" or product not in ("CONFIRMED", "DMR"):
                    block = True
            r[f"combo_block_dmr_{direction}"] = block
            if block:
                blocked += 1
            if product and smz and RANK.get(str(product), 0) > RANK.get(smz, 0):
                demotes.append(
                    {
                        "symbol": r.get("symbol"),
                        "direction": direction,
                        "sm_state": sm_state,
                        "ceiling": None if ceiling is None else ZONE_CN.get(ceiling, ceiling),
                        "product_zone": ZONE_CN.get(str(product), product),
                        "streak": streak,
                        "reason": reason,
                    }
                )
            if smz is not None:
                new_hyst[key] = {"product": product, "streak": streak}
    if data_dir is not None:
        _save_hysteresis(Path(data_dir), new_hyst, scan_id)
    out.update(
        enabled=True,
        demotes=demotes,
        blocked_dmr=blocked,
        incomplete=incomplete,
    )
    return out


def write_demote_report(data_dir: Path, scan_id: str, meta: dict[str, Any]) -> Optional[Path]:
    if not meta.get("enabled"):
        return None
    reports = Path(data_dir) / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    path = reports / f"{scan_id}.combo_demote.json"
    payload = {
        "scan_id": scan_id,
        "incomplete": meta.get("incomplete"),
        "blocked_dmr": meta.get("blocked_dmr"),
        "demotes": meta.get("demotes") or [],
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def product_state_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts = {
        "WATCH": 0,
        "QUALIFIED": 0,
        "CONFIRMED": 0,
        "ELIMINATED": 0,
        "DATA_INSUFFICIENT": 0,
        "LOW_CONFIDENCE": 0,
        "NONE": 0,
    }
    for r in rows:
        for d in ("up", "down"):
            st = r.get(f"product_zone_{d}") or r.get(f"state_{d}")
            if st in counts:
                counts[st] += 1
            elif st:
                counts["NONE"] += 1
    return counts
