"""Occupancy replay: adjacent board snapshots → ReviewTrade dicts.

Fact source is board-row zone predicates, not truncated `transitions`.
DMR is `dmr_selected`, not a state-machine state. Stay price for DMR is the
last/ref print at DMR entry — never CONFIRMED's `state_enter_price`.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from .review_pnl import realized_pnl

log = logging.getLogger("coin_selection.review_replay")

ZONES = (
    "DMR",
    "CONFIRMED",
    "QUALIFIED",
    "WATCH",
    "ELIMINATED",
    "DATA_INSUFFICIENT",
    "LOW_CONFIDENCE",
)

LISTED_STAY = ("WATCH", "QUALIFIED", "CONFIRMED")

#: 《复盘选币》改读 ``final_zone`` 的生效边界（只在 00:00 UTC 周期边界切换，文档B §3.3）。
#:
#: 背景：``final_zone``（主导层 P1/P2/P3 + SS×市值互印证之后的最终分区，也就是板面
#: 真正显示给用户的那个区）由 da8b4d7 引入，但该提交没有同步改本文件的分区谓词，
#: 于是账本一直读 ``effective_zone``（谓词之前的层 B）。两天窗口实测 0.90% 的行两者
#: 不同（QUALIFIED→WATCH 1103 / CONFIRMED→QUALIFIED 182 / CONFIRMED→WATCH 5），
#: 差异恰好落在确认区与 DMR 的进出边界上 —— 也就是复盘胜率的口径。
#:
#: 裁决：账本向板面看齐（读 final_zone），但**只对边界之后的节点生效**，历史不重写。
#: 边界之前的节点继续按 ``effective_zone`` 回放，因此已验收的历史数字逐字节不变。
FINAL_ZONE_FROM_SCAN_ID = "20260905-000"

NODES_PER_DAY = 96
# Wall-clock backstop, only used when a board's scan_id is unparseable. The node
# sequence below is the primary signal: 32 minutes could never distinguish "one
# node is missing" (30 min apart) from "the loop restarted" — and it silently
# missed all three real gaps in the production window.
GAP_MINUTES = 32.0


def parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def node_index(scan_id: Optional[str]) -> Optional[int]:
    """Absolute 15m-node index for a ``YYYYMMDD-NNN`` scan_id.

    ``seq`` restarts at 0 every 00:00 UTC anchor, so comparing sequences alone
    breaks across midnight; folding the date in makes the index monotonic.
    """
    if not scan_id or "-" not in str(scan_id):
        return None
    day, _, seq = str(scan_id).partition("-")
    try:
        d = datetime(int(day[0:4]), int(day[4:6]), int(day[6:8]))
        return d.toordinal() * NODES_PER_DAY + int(seq)
    except (TypeError, ValueError):
        return None


def missing_nodes_between(prev_scan_id: Optional[str], cur_scan_id: Optional[str]) -> int:
    """How many scan nodes have no snapshot between two consecutive boards.

    Exact, unlike a minutes threshold: `20260824-045 → 20260824-047` is
    unambiguously one lost node, while a restart that rewrites the *same* node
    is unambiguously zero.
    """
    a, b = node_index(prev_scan_id), node_index(cur_scan_id)
    if a is None or b is None or b <= a:
        return 0
    return b - a - 1


def coin_id_of(row: dict[str, Any]) -> str:
    return (
        str(row.get("canonical_asset_id") or row.get("underlying_asset") or row.get("symbol") or "")
        .strip()
        .upper()
    )


def in_zone(row: dict[str, Any], zone: str, scan_id: Optional[str] = None) -> bool:
    """产品分区谓词 ——《复盘选币》读分区的**唯一**入口。

    读取优先级（ChatGpt_SOL5.6 文档 B §3.1「两个栏目共用同一套规则内核」）：

    0. ``final_zone``      主导层最终分区 —— **板面显示的就是它**。仅在
       ``scan_id >= FINAL_ZONE_FROM_SCAN_ID`` 时启用（见该常量的说明）；
    1. ``effective_zone``  第三轮有效区内核（``mcap_effective``）的输出。它已经是
       ``max(base_rank, ceiling_rank)`` 的结果，**服务端算好、复盘直接读**，
       前端与复盘都不得再自行合并一次（否则就是两处实现、必然漂移）；
    2. ``product_zone``    第一/二轮天花板层的输出（保留兼容）；
    3. ``state``           克隆期快照没有上面两个字段时的回退。

    DMR 永远读 ``dmr_selected``：它是 CONFIRMED 的派生精选，不是状态机第五态
    （文档 A §5）。天花板生效时该旗标已在服务端含 ceiling 合取。
    """
    if zone == "DMR":
        return bool(row.get("dmr_selected"))
    # 选币榜Y 给展示区打了 zone_enter_*：DMR 是独立展示区，进出都会重打停留价。
    # 账本的确认/符合/观察占用必须跟这块展示戳走，否则 OPEN 停留价对不上选币榜。
    # v1.4.0 / 选币榜X 从不写这个键 —— DMR 仍是 CONFIRMED 的派生精选，不拆占用。
    if zone in LISTED_STAY and row.get("zone_enter_price") is not None:
        from .state_machine import display_zone_of

        dz = display_zone_of(row)
        if dz is not None:
            return dz == zone
    # 生效边界之后：以 final_zone 为准（板面显示什么，账本就统计什么）。
    # 边界之前：保持 effective_zone，历史逐字节不变。
    if scan_id and str(scan_id) >= FINAL_ZONE_FROM_SCAN_ID:
        fz = row.get("final_zone")
        if fz:
            return str(fz) == zone
    ez = row.get("effective_zone")
    if ez:
        return str(ez) == zone
    pz = row.get("product_zone")
    if pz:
        # 中文天花板名也可能被写进账本行，统一到英文状态机名。
        aliases = {
            "确定": "CONFIRMED",
            "符合": "QUALIFIED",
            "观察": "WATCH",
            "淘汰": "ELIMINATED",
            "DMR": "CONFIRMED",
        }
        mapped = aliases.get(str(pz), str(pz))
        return mapped == zone
    return row.get("state") == zone


def _num(v: object) -> Optional[float]:
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if x != x:
        return None
    return x


def print_px(row: Optional[dict[str, Any]]) -> tuple[Optional[float], Optional[str]]:
    if not row:
        return None, None
    for src in ("last_price", "ref_price"):
        x = _num(row.get(src))
        if x is not None and x != 0.0:
            return x, src
    return None, None


def stay_px(row: dict[str, Any], zone: str) -> tuple[Optional[float], Optional[str]]:
    """Freeze the stay print for this occupancy zone.

    Listed zones (WATCH / QUALIFIED / CONFIRMED): the UI 停留价格 is
    ``zone_enter_price`` once the board stamps display-zone occupancy
    (选币榜Y / 216 主导层). That stamp follows the *display* zone, which
    is DMR when ``dmr_selected`` — so it is only a valid stay for the
    occupancy whose name equals the display zone. Otherwise (and on
    v1.4.0 / 选币榜X, which never write the key) fall back to
    ``state_enter_price``.

    DMR is not a listed SM state: stay is last/ref at the DMR-enter node,
    never CONFIRMED's ``state_enter_price``.
    """
    if zone in LISTED_STAY:
        from .state_machine import display_zone_of

        if display_zone_of(row) == zone:
            x = _num(row.get("zone_enter_price"))
            if x is not None and x != 0.0:
                return x, "zone_enter_price"
        x = _num(row.get("state_enter_price"))
        if x is not None and x != 0.0:
            return x, "state_enter_price"
    px, src = print_px(row)
    return px, src


def slim_index(board: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Compact (symbol|direction) → last/ref/state for VANISHED close."""
    out: dict[str, dict[str, Any]] = {}
    for key, row in index_rows(board).items():
        out[f"{key[0]}|{key[1]}"] = {
            "symbol": key[0],
            "direction": key[1],
            "last_price": row.get("last_price"),
            "ref_price": row.get("ref_price"),
            "state": row.get("state"),
            "dmr_selected": row.get("dmr_selected"),
            "product_zone": row.get("product_zone"),
            "zone_ceiling": row.get("zone_ceiling"),
        }
    return out


def index_from_slim(slim: dict[str, dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for k, v in (slim or {}).items():
        if "|" not in k:
            continue
        sym, d = k.rsplit("|", 1)
        out[(sym, d)] = v
    return out


def index_rows(board: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for pool in (board.get("long_pool") or [], board.get("short_pool") or []):
        for r in pool:
            sym = r.get("symbol")
            d = r.get("direction")
            if not sym or d not in ("up", "down"):
                continue
            out[(str(sym), str(d))] = r
    return out


def trade_id(symbol: str, direction: str, zone: str, enter_scan: str) -> str:
    return f"{symbol}|{direction}|{zone}|{enter_scan}"


def dwell_minutes(enter_ts: Optional[str], exit_ts: Optional[str]) -> float:
    a, b = parse_iso(enter_ts), parse_iso(exit_ts)
    if a is None or b is None:
        return 0.0
    return max(0.0, (b - a).total_seconds() / 60.0)


def open_record(
    row: dict[str, Any],
    zone: str,
    meta: dict[str, Any],
    *,
    truncated: bool,
) -> dict[str, Any]:
    direction = str(row.get("direction") or "up")
    symbol = str(row.get("symbol"))
    enter_scan = str(meta.get("scan_id") or "")
    stay, stay_src = stay_px(row, zone)
    flags: list[str] = []
    if truncated:
        flags.append("TRUNCATED_ENTER")
    if stay is None:
        flags.append("MISSING_ENTRY_PRICE")
        if _num(row.get("state_enter_price")) == 0.0:
            flags.append("ZERO_ENTRY_PRICE")
    # `_review_flags` is written by the DMR inbox overlay onto the shared board
    # row. The same row is also opened as CONFIRMED/QUALIFIED/WATCH, so the
    # marker has to be scoped or it claims those stays came from the inbox too.
    if zone == "DMR":
        for f in row.get("_review_flags") or []:
            if f not in flags:
                flags.append(str(f))
    score = _num(row.get("score_up") if direction == "up" else row.get("score_down")) or 0.0
    opp = _num(row.get("score_down") if direction == "up" else row.get("score_up")) or 0.0
    return {
        "trade_id": trade_id(symbol, direction, zone, enter_scan),
        "symbol": symbol,
        "canonical_asset_id": coin_id_of(row) or symbol.replace("USDT", "").upper(),
        "underlying_asset": str(row.get("underlying_asset") or symbol.replace("USDT", "")),
        "contract_multiplier": int(row.get("contract_multiplier") or 1),
        "direction": direction,
        "zone": zone,
        "status": "OPEN",
        "enter_scan_id": enter_scan,
        "enter_time_utc": meta.get("scan_timestamp_utc"),
        "exit_scan_id": None,
        "exit_time_utc": None,
        "dwell_minutes": 0.0,
        "dwell_nodes": 1,
        "enter_price": stay,
        "enter_price_source": stay_src,
        "exit_price": None,
        "exit_price_source": None,
        "pnl_pct": None,
        "pnl_sign": None,
        "score": score,
        "score_opposite": opp,
        "direction_confidence": _num(row.get("direction_confidence")) or 0.0,
        "liquidity_grade": row.get("liquidity_grade") or "UNCLASSIFIED",
        "mcap_grade_30m": row.get("mcap_grade_30m"),
        "mcap_grade_2h": row.get("mcap_grade_2h"),
        "mcap_grade_6h": row.get("mcap_grade_6h"),
        "ret_1h": _num(row.get("ret_1h")),
        "ret_4h": _num(row.get("ret_4h")),
        "ret_24h": _num(row.get("ret_24h")),
        "ret_1w": _num(row.get("ret_1w")),
        "ret_1mo": _num(row.get("ret_1mo")),
        "ret_since_anchor": _num(row.get("ret_since_anchor")),
        "risk_flags": list(row.get("risk_flags") or []),
        "reason_codes": list(row.get("reason_codes") or []),
        "qualified_path": row.get("qualified_path"),
        "confirmed_path": row.get("confirmed_path"),
        "parameter_version": str(meta.get("parameter_version") or ""),
        "flags": flags,
        # —— 天花板层归因：两套字段名兼容 ——
        #
        # 第一/二轮的 opus5 切点式路径（mcap_zone.py）写 combo_code / zone_ceiling /
        # z_score / mcap_k / product_zone；第四轮的 sol5.6 主导层（mcap_effective +
        # mcap_dominance）写 mcap_combo_code / mcap_ceiling_zone / mcap_z10 /
        # mcap_resonance_k / final_zone。两者语义对应但命名不同 —— 只读旧名会让
        # 主导层生效后的真实交易在账本里**丢掉全部 216 归因**（实测 20 笔全空）。
        # 这里按「旧名优先、新名兜底」映射，两条路径的历史都能入账且不互相覆盖。
        "mcap_combo": row.get("mcap_combo") or row.get("mcap_combo_no"),
        "product_zone": row.get("product_zone") or row.get("final_zone"),
        "mcap_k": row.get("mcap_k") if row.get("mcap_k") is not None
        else row.get("mcap_resonance_k"),
        # —— 文档B §10.2 的 5 个新列 ——
        # param_hash 取自快照 meta：它记录的是**这一笔进场时**生效的那次调参。
        # 旧快照没有这个键 → None → 账本旧行保持 NULL，绝不回填（回填出来的是假的）。
        "param_hash": (meta.get("param_hash") or None),
        "combo_code": row.get("combo_code") or row.get("mcap_combo_code"),
        "combo_zone": row.get("combo_zone") or row.get("effective_zone"),
        "zone_ceiling": row.get("zone_ceiling") or row.get("mcap_ceiling_zone"),
        # z_score 统一为 Z（值域 [-3,+3]）：opus5 路径直接写 Z，
        # sol5.6 路径写的是整数 Z10（[-30,+30]），入账前除以 10 对齐量纲。
        "z_score": _num(row.get("z_score"))
        if row.get("z_score") is not None
        else (
            None if row.get("mcap_z10") is None else _num(row.get("mcap_z10")) / 10.0
        ),
        "_enter_pv": str(meta.get("parameter_version") or ""),
        "_enter_ph": (meta.get("param_hash") or None),
    }


def close_record(
    rec: dict[str, Any],
    *,
    exit_scan: Optional[str],
    exit_ts: Optional[str],
    exit_px: Optional[float],
    exit_src: Optional[str],
    extra_flags: Iterable[str] = (),
    gap: bool = False,
) -> dict[str, Any]:
    out = dict(rec)
    flags = list(out.get("flags") or [])
    for f in extra_flags:
        if f not in flags:
            flags.append(f)
    if gap and "GAP_BEFORE_EXIT" not in flags:
        flags.append("GAP_BEFORE_EXIT")
    enter_pv = out.get("_enter_pv") or out.get("parameter_version")
    if enter_pv:
        out["parameter_version"] = enter_pv
    enter_ph = out.get("_enter_ph") or out.get("param_hash")
    if enter_ph:
        out["param_hash"] = enter_ph
    stay = out.get("enter_price")
    if stay is None and "MISSING_ENTRY_PRICE" not in flags:
        flags.append("MISSING_ENTRY_PRICE")
    if exit_px is None and "MISSING_EXIT_PRICE" not in flags:
        flags.append("MISSING_EXIT_PRICE")
    pct, sign = realized_pnl(out["direction"], stay, exit_px)
    out.update(
        {
            "status": "CLOSED",
            "exit_scan_id": exit_scan,
            "exit_time_utc": exit_ts,
            "exit_price": exit_px,
            "exit_price_source": exit_src,
            "pnl_pct": pct,
            "pnl_sign": sign,
            "dwell_minutes": dwell_minutes(out.get("enter_time_utc"), exit_ts),
            "flags": flags,
        }
    )
    out.pop("_enter_pv", None)
    out.pop("_enter_ph", None)
    return out


def mark_param_change(rec: dict[str, Any], cur_pv: Optional[str]) -> None:
    """Flag a stay that straddles a `parameter_version` switch.

    The trade keeps its *entry* version (that is the standard it was picked
    under); the flag tells the UI the stay itself is not comparable across
    versions. Sticky: once set it survives into the CLOSED row.
    """
    enter_pv = rec.get("_enter_pv") or rec.get("parameter_version")
    if not cur_pv or not enter_pv or cur_pv == enter_pv:
        return
    flags = rec.setdefault("flags", [])
    if "PARAM_VERSION_CHANGED" not in flags:
        flags.append("PARAM_VERSION_CHANGED")


def mark_param_hash_change(rec: dict[str, Any], cur_ph: Optional[str]) -> None:
    """跨 ``param_hash`` 的停留打标（文档B §3.1）。

    ``parameter_version`` 管「哪套标准」，一整个版本周期内不变；``param_hash``
    管「这套标准的哪一次调参」—— overrides 每调一次它就变。两者语义相同（黏性、
    跟随入场版本），只是粒度不同，所以并存。

    旧快照没有 ``param_hash``（阶段 0 之前），此时 ``cur_ph`` 为 None：**不打标**。
    「没有指纹」不是「指纹变了」。
    """
    enter_ph = rec.get("_enter_ph") or rec.get("param_hash")
    if not cur_ph or not enter_ph or cur_ph == enter_ph:
        return
    flags = rec.setdefault("flags", [])
    if "PARAM_HASH_CHANGED" not in flags:
        flags.append("PARAM_HASH_CHANGED")


def public_open(rec: dict[str, Any], end_ts: Optional[str]) -> dict[str, Any]:
    out = dict(rec)
    out["dwell_minutes"] = dwell_minutes(out.get("enter_time_utc"), end_ts)
    out.pop("_enter_pv", None)
    out.pop("_enter_ph", None)
    return out


class OccupancyReplayer:
    """Stateful walker. Incremental ingest restores OPEN trades into `opened`."""

    def __init__(self, zones: Iterable[str] = ZONES):
        self.zones = tuple(zones)
        self.opened: dict[str, dict[tuple[str, str], dict[str, Any]]] = {
            z: {} for z in self.zones
        }
        self.prev_idx: Optional[dict[tuple[str, str], dict[str, Any]]] = None
        self.prev_ts: Optional[datetime] = None
        self.prev_sid: Optional[str] = None
        self.first = True
        self.last_ts: Optional[str] = None
        self.last_missing_nodes = 0

    def load_open(self, trades: Iterable[dict[str, Any]]) -> None:
        for t in trades:
            if t.get("status") != "OPEN":
                continue
            zone = t.get("zone")
            if zone not in self.opened:
                continue
            key = (str(t["symbol"]), str(t["direction"]))
            rec = dict(t)
            rec["_enter_pv"] = rec.get("parameter_version")
            self.opened[zone][key] = rec
        self.first = False

    def step(self, board: dict[str, Any]) -> list[dict[str, Any]]:
        """Apply one board. Returns CLOSED trades emitted this step + still-open updates."""
        meta = board.get("meta") or {}
        sid = str(meta.get("scan_id") or "")
        ts = meta.get("scan_timestamp_utc")
        cur_pv = str(meta.get("parameter_version") or "")
        cur_ph = meta.get("param_hash") or None
        dt = parse_iso(ts)

        # Primary signal: a hole in the node sequence. Exact and restart-proof.
        missing = missing_nodes_between(self.prev_sid, sid)
        gap = missing > 0
        if gap:
            log.info("review replay: %d node(s) missing between %s and %s", missing, self.prev_sid, sid)
        elif self.prev_ts is not None and dt is not None and self.prev_sid and sid:
            pass  # sequence is intact — a short/long wall interval is just cadence jitter
        elif self.prev_ts is not None and dt is not None:
            # Backstop for boards whose scan_id could not be parsed.
            gap = (dt - self.prev_ts).total_seconds() / 60.0 > GAP_MINUTES
        self.last_missing_nodes = missing
        idx = index_rows(board)
        emitted: list[dict[str, Any]] = []
        truncated = self.first

        # 时间区（24h 周期）重置节点：这一节点的退出是**周期强平**，不是策略自己走的。
        # 不打标记的话，「选币榜Y」每天会往账本里注入上千笔与策略无关的平仓，
        # 胜率与均值就此失真。`meta.cycle` 只有启用了周期的板面才有 ——
        # 「选币榜」的板面（历史的与将来的）都没有这个键，这段分支对它恒为 False。
        cycle_reset = bool((meta.get("cycle") or {}).get("reset_at_this_node"))
        cycle_flags: tuple[str, ...] = ("CYCLE_RESET",) if cycle_reset else ()
        # 重置节点上，币会整个从板面消失（清空后它不在任何分区里，而板面只登载分区成员）。
        # 那不是 VANISHED 想表达的「板面莫名其妙丢了这一行」这类数据异常 —— 原因已知，
        # 所以用 CYCLE_RESET **取代** VANISHED，别让「仅显示异常」被每日强平淹掉。
        vanished_flags: tuple[str, ...] = ("CYCLE_RESET",) if cycle_reset else ("VANISHED",)

        for zone in self.zones:
            book = self.opened[zone]
            for key in list(book.keys()):
                rec = book[key]
                mark_param_change(rec, cur_pv)
                mark_param_hash_change(rec, cur_ph)
                row = idx.get(key)
                if row is None:
                    prev_row = self.prev_idx.get(key) if self.prev_idx else None
                    px, src = print_px(prev_row)
                    closed = close_record(
                        rec,
                        exit_scan=sid,
                        exit_ts=ts,
                        exit_px=px,
                        exit_src="prev_node" if src else None,
                        extra_flags=vanished_flags,
                        gap=gap,
                    )
                    emitted.append(closed)
                    del book[key]
                    continue
                if not in_zone(row, zone, scan_id=sid):
                    px, src = print_px(row)
                    closed = close_record(
                        rec,
                        exit_scan=sid,
                        exit_ts=ts,
                        exit_px=px,
                        exit_src=src,
                        extra_flags=cycle_flags,
                        gap=gap,
                    )
                    emitted.append(closed)
                    del book[key]
                    continue
                rec["dwell_nodes"] = int(rec.get("dwell_nodes") or 1) + 1
            for key, row in idx.items():
                if not in_zone(row, zone, scan_id=sid):
                    continue
                if key in book:
                    continue
                rec = open_record(row, zone, meta, truncated=truncated)
                book[key] = rec
                emitted.append(public_open(rec, ts))

        self.prev_idx = idx
        self.prev_ts = dt
        self.prev_sid = sid
        self.last_ts = ts
        self.first = False
        return emitted

    def open_trades(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for zone in self.zones:
            for rec in self.opened[zone].values():
                out.append(public_open(rec, self.last_ts))
        return out


def replay_boards(
    boards: Iterable[dict[str, Any]],
    *,
    zones: Iterable[str] = ZONES,
) -> list[dict[str, Any]]:
    rp = OccupancyReplayer(zones)
    closed: list[dict[str, Any]] = []
    for board in boards:
        for t in rp.step(board):
            if t.get("status") == "CLOSED":
                closed.append(t)
    return closed + rp.open_trades()


def overlay_dmr_from_inbox(board: dict[str, Any], inbox_doc: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Fill dmr_selected from inbox when the board snapshot predates the field."""
    long_pool = list(board.get("long_pool") or [])
    short_pool = list(board.get("short_pool") or [])
    board = {**board, "long_pool": long_pool, "short_pool": short_pool}
    sample = (long_pool or short_pool or [{}])[0]
    if "dmr_selected" in sample:
        # v1.4+ boards carry the flag themselves — the board is the UI's own
        # source of truth, so never second-guess it from the inbox.
        return board
    if not inbox_doc:
        return board
    rank = inbox_doc.get("rank") or {}
    cands = inbox_doc.get("candidates") or []
    if not rank.get("inbox_k") and not any(c.get("dmr_selected") for c in cands):
        return board
    keys = set()
    for c in cands:
        sym = c.get("symbol")
        raw = str(c.get("direction") or "").upper()
        direction = "up" if raw in ("LONG", "UP") else "down" if raw in ("SHORT", "DOWN") else None
        if sym and direction:
            keys.add((str(sym), direction))
    # Copy each row before writing: the caller's board dict is also handed to
    # other consumers (and to a second overlay pass in the build script).
    for pool_name, pool in (("long_pool", long_pool), ("short_pool", short_pool)):
        out_rows = []
        for row in pool:
            hit = (str(row.get("symbol")), str(row.get("direction"))) in keys
            row = {**row, "dmr_selected": hit}
            if hit:
                flags = list(row.get("_review_flags") or [])
                if "DMR_MEMBER_FROM_INBOX" not in flags:
                    flags.append("DMR_MEMBER_FROM_INBOX")
                row["_review_flags"] = flags
            out_rows.append(row)
        board[pool_name] = out_rows
    return board


def load_board(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def iter_board_paths(snapshots_dir: Path) -> list[Path]:
    return sorted(
        p for p in snapshots_dir.glob("*.json") if not p.name.endswith(".full.json")
    )


def replay_snapshot_dir(
    snapshots_dir: Path,
    *,
    inbox_dir: Optional[Path] = None,
    zones: Iterable[str] = ZONES,
) -> list[dict[str, Any]]:
    rp = OccupancyReplayer(zones)
    closed: list[dict[str, Any]] = []
    for p in iter_board_paths(snapshots_dir):
        board = load_board(p)
        sid = (board.get("meta") or {}).get("scan_id") or p.name.replace(".json", "")
        inbox_doc = None
        if inbox_dir is not None:
            ip = inbox_dir / f"{sid}.candidates.json"
            if ip.is_file():
                try:
                    inbox_doc = json.loads(ip.read_text(encoding="utf-8"))
                except Exception as e:
                    log.warning("inbox read failed %s: %s", ip.name, e)
        for t in rp.step(overlay_dmr_from_inbox(board, inbox_doc)):
            if t.get("status") == "CLOSED":
                closed.append(t)
    return closed + rp.open_trades()
