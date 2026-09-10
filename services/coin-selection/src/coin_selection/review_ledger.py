"""SQLite review ledger: incremental occupancy replay + query API.

P9: ingest failures must never raise into the scan loop — callers wrap
``ingest_scan`` / ``ingest_board``. This module itself raises on programmer
error so unit tests stay strict.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from .review_replay import (
    OccupancyReplayer,
    ZONES,
    index_from_slim,
    overlay_dmr_from_inbox,
    slim_index,
)

log = logging.getLogger("coin_selection.review_ledger")

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
  trade_id TEXT PRIMARY KEY,
  symbol TEXT NOT NULL,
  canonical_asset_id TEXT NOT NULL,
  underlying_asset TEXT,
  contract_multiplier INTEGER,
  direction TEXT NOT NULL,
  zone TEXT NOT NULL,
  status TEXT NOT NULL,
  enter_scan_id TEXT NOT NULL,
  enter_time_utc TEXT NOT NULL,
  exit_scan_id TEXT,
  exit_time_utc TEXT,
  dwell_minutes REAL,
  dwell_nodes INTEGER,
  enter_price REAL,
  enter_price_source TEXT,
  exit_price REAL,
  exit_price_source TEXT,
  pnl_pct REAL,
  pnl_sign INTEGER,
  score REAL,
  score_opposite REAL,
  direction_confidence REAL,
  liquidity_grade TEXT,
  mcap_grade_30m TEXT,
  mcap_grade_2h TEXT,
  mcap_grade_6h TEXT,
  ret_1h REAL,
  ret_4h REAL,
  ret_24h REAL,
  ret_1w REAL,
  ret_1mo REAL,
  ret_since_anchor REAL,
  risk_flags TEXT,
  reason_codes TEXT,
  qualified_path TEXT,
  confirmed_path TEXT,
  parameter_version TEXT,
  flags TEXT,
  mcap_combo TEXT,
  product_zone TEXT,
  mcap_k INTEGER,
  param_hash TEXT,
  combo_code TEXT,
  combo_zone TEXT,
  zone_ceiling TEXT,
  z_score REAL
);
CREATE INDEX IF NOT EXISTS ix_trades_zone_exit ON trades(zone, direction, exit_time_utc);
CREATE INDEX IF NOT EXISTS ix_trades_coin_enter ON trades(canonical_asset_id, enter_time_utc);
CREATE INDEX IF NOT EXISTS ix_trades_status ON trades(status);
-- 新列上的索引**不放这里**：SCHEMA 对已存在的老表是 no-op（CREATE TABLE IF NOT
-- EXISTS），但 CREATE INDEX 会立刻去找 param_hash 这一列并报错。它们必须排在
-- ALTER TABLE 之后 —— 见 ReviewLedger.MIGRATED_INDEXES。
CREATE TABLE IF NOT EXISTS watermarks (
  k TEXT PRIMARY KEY,
  scan_id TEXT,
  ts TEXT,
  json TEXT
);
CREATE TABLE IF NOT EXISTS coverage (
  zone TEXT PRIMARY KEY,
  first_scan TEXT,
  first_ts TEXT,
  last_scan TEXT,
  last_ts TEXT
);
"""

TRADE_COLS = [
    "trade_id",
    "symbol",
    "canonical_asset_id",
    "underlying_asset",
    "contract_multiplier",
    "direction",
    "zone",
    "status",
    "enter_scan_id",
    "enter_time_utc",
    "exit_scan_id",
    "exit_time_utc",
    "dwell_minutes",
    "dwell_nodes",
    "enter_price",
    "enter_price_source",
    "exit_price",
    "exit_price_source",
    "pnl_pct",
    "pnl_sign",
    "score",
    "score_opposite",
    "direction_confidence",
    "liquidity_grade",
    "mcap_grade_30m",
    "mcap_grade_2h",
    "mcap_grade_6h",
    "ret_1h",
    "ret_4h",
    "ret_24h",
    "ret_1w",
    "ret_1mo",
    "ret_since_anchor",
    "risk_flags",
    "reason_codes",
    "qualified_path",
    "confirmed_path",
    "parameter_version",
    "flags",
    "mcap_combo",
    "product_zone",
    "mcap_k",
    # —— 文档B §10.2 的 5 个新列（全部 nullable、无 DEFAULT；旧行保持 NULL）——
    "param_hash",     # 参数指纹（§3.1）。阶段 0 之前的快照没有它，回填出来的是假的。
    "combo_code",     # 'AAB' 等三字母；判不出级为 NULL
    "combo_zone",     # 该组合在该方向的名义天花板（216 查表值）
    "zone_ceiling",   # 实际生效的天花板（含弃权 / P1–P3 / SS 背离调整后）
    "z_score",        # 方向分 Z_direction
]


def _dumps(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False)


def _loads_list(v: Any) -> list:
    if not v:
        return []
    if isinstance(v, list):
        return v
    try:
        x = json.loads(v)
        return x if isinstance(x, list) else []
    except Exception:
        return []


def _row_tuple(t: dict[str, Any]) -> tuple:
    out = []
    for c in TRADE_COLS:
        v = t.get(c)
        if c in ("risk_flags", "reason_codes", "flags"):
            v = _dumps(v or [])
        out.append(v)
    return tuple(out)


def _row_dict(r: sqlite3.Row) -> dict[str, Any]:
    d = {k: r[k] for k in r.keys()}
    for k in ("risk_flags", "reason_codes", "flags"):
        d[k] = _loads_list(d.get(k))
    return d


SORT_COLS = {
    "enter_time_utc": "COALESCE(enter_time_utc,'')",
    "exit_time_utc": "COALESCE(exit_time_utc,'')",
    "dwell_minutes": "COALESCE(dwell_minutes,-1)",
    "dwell_nodes": "COALESCE(dwell_nodes,-1)",
    "pnl_pct": "pnl_pct",
    "pnl_sign": "pnl_sign",
    "score": "COALESCE(score,-1)",
    "direction_confidence": "COALESCE(direction_confidence,-1)",
    "symbol": "symbol",
    "zone": "zone",
    "enter_price": "COALESCE(enter_price,-1)",
    "exit_price": "COALESCE(exit_price,-1)",
}


def order_clause(sort: str, desc: bool) -> str:
    """Whitelisted ORDER BY. Unknown keys fall back to the documented default
    (§6.4: entry time ascending, symbol as the stable tiebreak)."""
    col = SORT_COLS.get(str(sort or ""), SORT_COLS["enter_time_utc"])
    dirn = "DESC" if desc else "ASC"
    # NULL pnl (OPEN / missing price) must sink to the bottom either way so a
    # "worst loss" sort never leads with rows that have no PnL at all.
    nulls = " NULLS LAST" if col in ("pnl_pct", "pnl_sign") else ""
    tail = "COALESCE(enter_time_utc,'') ASC, symbol ASC"
    return f"{col} {dirn}{nulls}, {tail}"


class ReviewLedger:
    """Occupancy ledger.

    ``readonly=True`` is the query path (api-gateway): it skips replayer
    rehydration, which otherwise re-reads every OPEN row (~1k) on each HTTP
    request even though no ingest will happen.
    """

    def __init__(self, path: Path, *, readonly: bool = False):
        self.path = Path(path)
        self.readonly = bool(readonly)
        # X v1.3.0 对齐审计不得重写现有 v1.4.0 克隆账本；未来独立演进也必须
        # 由选币写者完成。只读消费用 SQLite 强制 ro，不创建目录、不切 journal。
        # 不用 immutable=1：生产 WAL 写者提交的新节点仍须被复盘实时看到。
        if self.readonly:
            self._conn = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)
            self._conn.execute("PRAGMA query_only=ON")
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.path))
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.row_factory = sqlite3.Row
        if not self.readonly:
            self._conn.executescript(SCHEMA)
            self._ensure_combo_columns()
            self._conn.commit()
        self._rp = OccupancyReplayer()
        if not self.readonly:
            self._hydrate_open()

    #: 迁移清单：``(列名, SQL 类型)``。幂等 —— 已存在的列跳过（文档B §10.2）。
    #: 全部 nullable、无 DEFAULT，旧行保持 NULL，UI 上显示为「—」。
    MIGRATED_COLUMNS: tuple[tuple[str, str], ...] = (
        ("mcap_combo", "TEXT"),
        ("product_zone", "TEXT"),
        ("mcap_k", "INTEGER"),
        ("param_hash", "TEXT"),
        ("combo_code", "TEXT"),
        ("combo_zone", "TEXT"),
        ("zone_ceiling", "TEXT"),
        ("z_score", "REAL"),
    )

    #: 迁移后要补建的索引（``IF NOT EXISTS``，幂等）。
    MIGRATED_INDEXES: tuple[str, ...] = (
        "CREATE INDEX IF NOT EXISTS idx_trades_param_hash ON trades(param_hash)",
        "CREATE INDEX IF NOT EXISTS idx_trades_combo ON trades(combo_code, zone, direction)",
    )

    def _ensure_combo_columns(self) -> None:
        """ALTER 兼容：克隆期账本没有组合列，阶段 0 之前的账本没有 param_hash。

        幂等：先查 ``PRAGMA table_info(trades)``，已存在的列跳过。
        ``SELECT *`` 的消费方一律按列名取值（现网已是），位置变化不影响它们。
        """
        cur = self._conn.execute("PRAGMA table_info(trades)")
        have = {row[1] for row in cur.fetchall()}
        added = [c for c, _ in self.MIGRATED_COLUMNS if c not in have]
        for col, decl in self.MIGRATED_COLUMNS:
            if col not in have:
                self._conn.execute(f"ALTER TABLE trades ADD COLUMN {col} {decl}")
        for stmt in self.MIGRATED_INDEXES:
            self._conn.execute(stmt)
        if added:
            log.info("review ledger migrated: added columns %s", ", ".join(added))

    def close(self) -> None:
        self._conn.close()

    def watermark(self) -> dict[str, Any]:
        cur = self._conn.execute("SELECT json FROM watermarks WHERE k='scan'")
        row = cur.fetchone()
        if not row:
            return {}
        try:
            return json.loads(row["json"])
        except Exception:
            return {}

    def _hydrate_open(self) -> None:
        cur = self._conn.execute("SELECT * FROM trades WHERE status='OPEN'")
        opens = [_row_dict(r) for r in cur.fetchall()]
        if opens:
            self._rp.load_open(opens)
        wm = self.watermark()
        if wm.get("ts"):
            # A watermark means this is a continuation, not the ledger's first
            # board — regardless of whether any stay happened to be open.
            self._rp.first = False
            self._rp.last_ts = wm.get("ts")
            self._rp.prev_sid = wm.get("scan_id")
            from .review_replay import parse_iso

            self._rp.prev_ts = parse_iso(wm.get("ts"))
            slim = wm.get("prev_index") or {}
            if slim:
                self._rp.prev_idx = index_from_slim(slim)

    def _set_watermark(self, board: dict[str, Any]) -> None:
        meta = board.get("meta") or {}
        prev = self.watermark()
        payload = {
            "scan_id": meta.get("scan_id"),
            "ts": meta.get("scan_timestamp_utc"),
            "parameter_version": meta.get("parameter_version"),
            "nodes_ingested": int(prev.get("nodes_ingested") or 0) + 1,
            "prev_index": slim_index(board),
        }
        self._conn.execute(
            "INSERT OR REPLACE INTO watermarks(k, scan_id, ts, json) VALUES(?,?,?,?)",
            ("scan", payload["scan_id"], payload["ts"], json.dumps(payload, ensure_ascii=False)),
        )

    def ingest_board(
        self,
        board: dict[str, Any],
        *,
        inbox_doc: Optional[dict[str, Any]] = None,
        zones: Iterable[str] = ZONES,
    ) -> dict[str, Any]:
        board = overlay_dmr_from_inbox(dict(board), inbox_doc)
        meta = board.get("meta") or {}
        sid = str(meta.get("scan_id") or "")
        wm = self.watermark()
        prev_sid = wm.get("scan_id")
        if prev_sid is not None and sid <= str(prev_sid):
            # Replaying an already-consumed node would diff a stale prev_index
            # and re-emit its occupants as OPEN; `INSERT OR REPLACE` would then
            # overwrite their CLOSED rows and destroy the realized PnL. Refuse.
            # `scan_id` is zero-padded, so lexical order is chronological order.
            return {"status": "duplicate", "scan_id": sid, "watermark": prev_sid}
        if tuple(zones) != self._rp.zones:
            self._rp = OccupancyReplayer(zones)
            self._hydrate_open()
        emitted = self._rp.step(board)
        # Persist closes + current OPEN snapshot (OPEN upsert keeps dwell fresh).
        to_write = [t for t in emitted if t.get("status") == "CLOSED"] + self._rp.open_trades()
        self._upsert(to_write)
        self._set_watermark(board)
        self._conn.commit()
        return {"status": "ok", "scan_id": sid, "emitted": len(emitted)}

    def _upsert(self, trades: list[dict[str, Any]]) -> None:
        sql = (
            "INSERT OR REPLACE INTO trades ("
            + ",".join(TRADE_COLS)
            + ") VALUES ("
            + ",".join("?" * len(TRADE_COLS))
            + ")"
        )
        self._conn.executemany(sql, [_row_tuple(t) for t in trades])

    def _filters(
        self,
        *,
        zones: Optional[list[str]],
        direction: str,
        symbols: Optional[list[str]],
        start_utc: Optional[str],
        end_utc: Optional[str],
        attribution: str,
        include_open: bool,
        min_dwell_nodes: int,
        status: Optional[str],
        only_flagged: bool = False,
        parameter_versions: Optional[list[str]] = None,
        param_hashes: Optional[list[str]] = None,
        exclude_flags: Optional[list[str]] = None,
    ) -> tuple[str, list[Any]]:
        where = ["1=1"]
        args: list[Any] = []
        if zones:
            where.append(f"zone IN ({','.join('?' * len(zones))})")
            args.extend(zones)
        if direction in ("up", "down"):
            where.append("direction=?")
            args.append(direction)
        if symbols:
            up = [s.strip().upper() for s in symbols if s.strip()]
            if up:
                where.append(
                    f"(UPPER(canonical_asset_id) IN ({','.join('?' * len(up))}) "
                    f"OR UPPER(symbol) IN ({','.join('?' * len(up))}))"
                )
                args.extend(up)
                args.extend(up)
        if not include_open:
            where.append("status='CLOSED'")
        if status:
            where.append("status=?")
            args.append(status)
        if min_dwell_nodes:
            where.append("dwell_nodes>=?")
            args.append(int(min_dwell_nodes))
        if parameter_versions:
            # 交易记的是**入点**版本 —— 那是它被选中时所依据的标准。
            # 按版本筛选 = 「只看这套选币标准自己的成绩」，跨版本混算没有意义（§14.4）。
            where.append(f"parameter_version IN ({','.join('?' * len(parameter_versions))})")
            args.extend(parameter_versions)
        if param_hashes:
            # —— 同一 parameter_version 内部的**调参分段**（文档B §3.1）——
            #
            # parameter_version 管「哪套标准」，一整个版本周期内不变；param_hash 管
            # 「这套标准的哪一次调参」。选币榜Y 的 216 主导层上线前后，
            # parameter_version 都是 param-v2.0.0-screener-y，**只有 param_hash 变**
            # （pf1_1b44aba95de8ffe8 → pf1_cc81ad3ac5397f17）。没有这个过滤器，
            # 《复盘选币》会把「天花板关」与「天花板主导」两段混算成一个胜率。
            #
            # 'null' / 'NULL' 作为一个可选值，用来单独捞出阶段 0 之前没有指纹的历史行
            # （账本里 97% 是这种）—— 它们不可与任何一段合并统计。
            want_null = any(str(h).lower() == "null" for h in param_hashes)
            real = [h for h in param_hashes if str(h).lower() != "null"]
            clauses = []
            if real:
                clauses.append(f"param_hash IN ({','.join('?' * len(real))})")
                args.extend(real)
            if want_null:
                clauses.append("param_hash IS NULL")
            if clauses:
                where.append("(" + " OR ".join(clauses) + ")")
        if only_flagged:
            # Data-quality lens (Claude §12.4「仅显示异常」): rows carrying any
            # ReviewFlag — missing price, VANISHED, GAP, truncated, cross-version.
            where.append("flags IS NOT NULL AND flags NOT IN ('[]','')")
        if exclude_flags:
            # 剔除带某些旗标的交易。主用途：把 24h 周期重置的强制平仓（CYCLE_RESET）
            # 从「选币榜Y」的成绩里拿掉 —— 那不是策略自己的退出决策，
            # 混进胜率与均值就没法回答「v2.0.0 的选币标准好不好」。
            # flags 存的是 JSON 数组字符串，匹配带引号的整词，避免 CYCLE_RESET 误伤前缀相同的旗标。
            for f in exclude_flags:
                where.append("(flags IS NULL OR flags NOT LIKE ?)")
                args.append(f'%"{f}"%')
        time_col = {
            "exit": "exit_time_utc",
            "enter": "enter_time_utc",
            "contained": None,
        }.get(attribution, "exit_time_utc")
        if attribution == "contained":
            # "Fully contained" means both ends fall inside the window, which
            # only a CLOSED trade can satisfy — unconditionally, not just when
            # the caller supplied an upper bound.
            where.append("status='CLOSED'")
            if start_utc:
                where.append("enter_time_utc>=?")
                args.append(start_utc)
            if end_utc:
                where.append("exit_time_utc<=?")
                args.append(end_utc)
        else:
            col = time_col or "exit_time_utc"
            if start_utc:
                where.append(f"({col}>=? OR ({col} IS NULL AND enter_time_utc>=? AND status='OPEN'))")
                args.append(start_utc)
                args.append(start_utc)
            if end_utc:
                where.append(f"({col}<=? OR ({col} IS NULL AND status='OPEN' AND enter_time_utc<=?))")
                args.append(end_utc)
                args.append(end_utc)
            if attribution == "exit" and not include_open:
                where.append("exit_time_utc IS NOT NULL")
        return " AND ".join(where), args

    def query_trades(
        self,
        *,
        zones: Optional[list[str]] = None,
        direction: str = "both",
        symbols: Optional[list[str]] = None,
        start_utc: Optional[str] = None,
        end_utc: Optional[str] = None,
        attribution: str = "exit",
        include_open: bool = False,
        min_dwell_nodes: int = 0,
        only_flagged: bool = False,
        exclude_flags: Optional[list[str]] = None,
        parameter_versions: Optional[list[str]] = None,
        param_hashes: Optional[list[str]] = None,
        sort: str = "enter_time_utc",
        desc: bool = False,
        limit: int = 500,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        where, args = self._filters(
            zones=zones,
            direction=direction,
            symbols=symbols,
            start_utc=start_utc,
            end_utc=end_utc,
            attribution=attribution,
            include_open=include_open,
            min_dwell_nodes=min_dwell_nodes,
            status=None,
            only_flagged=only_flagged,
            exclude_flags=exclude_flags,
            parameter_versions=parameter_versions,
            param_hashes=param_hashes,
        )
        # Sorting must happen in SQL, not on the returned page: a window can hold
        # more trades than `limit`, and sorting one page client-side would answer
        # "biggest loss" with the biggest loss *of the first 500 by entry time*.
        sql = (
            f"SELECT * FROM trades WHERE {where} "
            f"ORDER BY {order_clause(sort, desc)} "
            "LIMIT ? OFFSET ?"
        )
        args.extend([int(limit), int(offset)])
        cur = self._conn.execute(sql, args)
        return [_row_dict(r) for r in cur.fetchall()]

    def count_trades(
        self,
        *,
        zones: Optional[list[str]] = None,
        direction: str = "both",
        symbols: Optional[list[str]] = None,
        start_utc: Optional[str] = None,
        end_utc: Optional[str] = None,
        attribution: str = "exit",
        include_open: bool = False,
        min_dwell_nodes: int = 0,
        only_flagged: bool = False,
        exclude_flags: Optional[list[str]] = None,
        parameter_versions: Optional[list[str]] = None,
        param_hashes: Optional[list[str]] = None,
    ) -> int:
        """Rows matching the filter, ignoring limit/offset — lets the UI say
        \"showing 500 of 5396\" instead of silently truncating."""
        where, args = self._filters(
            zones=zones,
            direction=direction,
            symbols=symbols,
            start_utc=start_utc,
            end_utc=end_utc,
            attribution=attribution,
            include_open=include_open,
            min_dwell_nodes=min_dwell_nodes,
            status=None,
            only_flagged=only_flagged,
            exclude_flags=exclude_flags,
            parameter_versions=parameter_versions,
            param_hashes=param_hashes,
        )
        cur = self._conn.execute(f"SELECT COUNT(*) AS n FROM trades WHERE {where}", args)
        return int(cur.fetchone()["n"])

    def summarize(
        self,
        *,
        zones: Optional[list[str]] = None,
        direction: str = "both",
        symbols: Optional[list[str]] = None,
        start_utc: Optional[str] = None,
        end_utc: Optional[str] = None,
        attribution: str = "exit",
        min_dwell_nodes: int = 0,
        only_flagged: bool = False,
        exclude_flags: Optional[list[str]] = None,
        parameter_versions: Optional[list[str]] = None,
        param_hashes: Optional[list[str]] = None,
        control_zones: Iterable[str] = ("ELIMINATED", "DATA_INSUFFICIENT", "LOW_CONFIDENCE"),
    ) -> dict[str, Any]:
        """Aggregates are always CLOSED-only (R1). There is deliberately no
        ``include_open`` switch: an unrealized stay has no realized PnL to add,
        and ``open_trades`` reports the open population separately."""
        where, args = self._filters(
            zones=zones,
            direction=direction,
            symbols=symbols,
            start_utc=start_utc,
            end_utc=end_utc,
            attribution=attribution,
            include_open=False,
            min_dwell_nodes=min_dwell_nodes,
            status="CLOSED",
            only_flagged=only_flagged,
            exclude_flags=exclude_flags,
            parameter_versions=parameter_versions,
            param_hashes=param_hashes,
        )
        cur = self._conn.execute(
            f"SELECT * FROM trades WHERE {where}",
            args,
        )
        rows = [_row_dict(r) for r in cur.fetchall()]
        window_days = _days_between(start_utc, end_utc)
        open_n = self._open_count(
            zones=zones, direction=direction, symbols=symbols, start_utc=start_utc, end_utc=end_utc,
            min_dwell_nodes=min_dwell_nodes, only_flagged=only_flagged,
            parameter_versions=parameter_versions, exclude_flags=exclude_flags,
        )
        out = summarize_trades(rows, include_open_count=open_n, window_days=window_days)

        # Per-zone cards. Series are dropped here: the merged card already
        # carries the curve, and N zones x 240 points would bloat the payload.
        by_zone: dict[str, Any] = {}
        for z in zones or []:
            by_zone[z] = summarize_trades(
                [r for r in rows if r.get("zone") == z],
                include_open_count=self._open_count(
                    zones=[z], direction=direction, symbols=symbols, start_utc=start_utc, end_utc=end_utc,
                    min_dwell_nodes=min_dwell_nodes, only_flagged=only_flagged,
                    parameter_versions=parameter_versions, exclude_flags=exclude_flags,
                ),
                window_days=window_days,
                with_series=False,
            )
        out["by_zone"] = by_zone
        out["by_direction"] = {
            "up": summarize_trades(
                [r for r in rows if r.get("direction") == "up"],
                include_open_count=self._open_count(
                    zones=zones, direction="up", symbols=symbols, start_utc=start_utc, end_utc=end_utc,
                    min_dwell_nodes=min_dwell_nodes, only_flagged=only_flagged,
                    parameter_versions=parameter_versions, exclude_flags=exclude_flags,
                ),
                window_days=window_days,
                with_series=False,
            ),
            "down": summarize_trades(
                [r for r in rows if r.get("direction") == "down"],
                include_open_count=self._open_count(
                    zones=zones, direction="down", symbols=symbols, start_utc=start_utc, end_utc=end_utc,
                    min_dwell_nodes=min_dwell_nodes, only_flagged=only_flagged,
                    parameter_versions=parameter_versions, exclude_flags=exclude_flags,
                ),
                window_days=window_days,
                with_series=False,
            ),
        }

        # Cross-version aggregates are not comparable (§14.4): report the split
        # so the UI can warn instead of averaging two different standards.
        pv_rows: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            pv_rows.setdefault(str(r.get("parameter_version") or "—"), []).append(r)
        by_pv: dict[str, Any] = {}
        total = len(rows) or 1
        for pv, rs in sorted(pv_rows.items()):
            seg = summarize_trades(rs, window_days=window_days, with_series=False)
            seg["trade_share"] = len(rs) / total
            seg["from_utc"] = min((x.get("enter_time_utc") or "" for x in rs), default=None)
            seg["to_utc"] = max(
                (x.get("exit_time_utc") or x.get("enter_time_utc") or "" for x in rs), default=None
            )
            by_pv[pv] = seg
        out["by_parameter_version"] = by_pv
        out["parameter_versions"] = sorted(pv_rows.keys())

        # Chip numbers: same window/direction/dwell filter, one count per zone,
        # so a chip always states how many trades the *current* cross-section holds.
        chip: dict[str, int] = {}
        for z in ZONES:
            w, a = self._filters(
                zones=[z],
                direction=direction,
                symbols=symbols,
                start_utc=start_utc,
                end_utc=end_utc,
                attribution=attribution,
                include_open=False,
                min_dwell_nodes=min_dwell_nodes,
                status="CLOSED",
                only_flagged=only_flagged,
                exclude_flags=exclude_flags,
                parameter_versions=parameter_versions,
            param_hashes=param_hashes,
            )
            n = self._conn.execute(f"SELECT COUNT(*) AS n FROM trades WHERE {w}", a).fetchone()["n"]
            chip[z] = int(n)
        out["chip_counts"] = chip

        # 对照组相对表现 (§15.2): the number that says whether the selection
        # standard beats "no standard at all". Always computed on the control
        # zones over the same window, whether or not the user selected them.
        ctrl_where, ctrl_args = self._filters(
            zones=list(control_zones),
            direction=direction,
            symbols=symbols,
            start_utc=start_utc,
            end_utc=end_utc,
            attribution=attribution,
            include_open=False,
            min_dwell_nodes=min_dwell_nodes,
            status="CLOSED",
            only_flagged=only_flagged,
            exclude_flags=exclude_flags,
            parameter_versions=parameter_versions,
            param_hashes=param_hashes,
        )
        ctrl_rows = [
            _row_dict(r)
            for r in self._conn.execute(f"SELECT * FROM trades WHERE {ctrl_where}", ctrl_args).fetchall()
        ]
        ctrl = summarize_trades(ctrl_rows, window_days=window_days, with_series=False)
        sel_avg = out.get("avg_pct")
        ctl_avg = ctrl.get("avg_pct")
        out["control_benchmark"] = {
            "zones": list(control_zones),
            "trades": ctrl["trades"],
            "avg_pct": ctl_avg,
            "win_rate": ctrl.get("win_rate"),
            "edge_avg_pct": (sel_avg - ctl_avg) if (sel_avg is not None and ctl_avg is not None) else None,
            "edge_win_rate": (
                out["win_rate"] - ctrl["win_rate"]
                if (out.get("win_rate") is not None and ctrl.get("win_rate") is not None)
                else None
            ),
            "note": "对照组盈亏是反向基准，不代表可执行策略",
        }
        return out

    def _open_count(self, **kw: Any) -> int:
        where, args = self._filters(
            zones=kw.get("zones"),
            direction=kw.get("direction") or "both",
            symbols=kw.get("symbols"),
            start_utc=kw.get("start_utc"),
            end_utc=kw.get("end_utc"),
            # Open stays are attributed by entry time — they have no exit yet.
            attribution="enter",
            include_open=True,
            min_dwell_nodes=int(kw.get("min_dwell_nodes") or 0),
            status="OPEN",
            only_flagged=bool(kw.get("only_flagged")),
            parameter_versions=kw.get("parameter_versions"),
        )
        cur = self._conn.execute(f"SELECT COUNT(*) AS n FROM trades WHERE {where}", args)
        return int(cur.fetchone()["n"])

    def coverage(self) -> dict[str, Any]:
        """Physical窗口 + 每区可用起点 + 字段可用起点 + 参数版本分段.

        Everything here is derived from the ledger itself — no snapshot re-scan
        — so the coverage banner costs one round of cheap aggregates.
        """
        wm = self.watermark()
        row = self._conn.execute(
            "SELECT MIN(enter_time_utc) AS first_ts, "
            "MAX(COALESCE(exit_time_utc, enter_time_utc)) AS last_ts, COUNT(*) AS n FROM trades"
        ).fetchone()
        zone_from: dict[str, Any] = {}
        for z in ZONES:
            c = self._conn.execute(
                "SELECT MIN(enter_time_utc) AS a, MAX(COALESCE(exit_time_utc, enter_time_utc)) AS b, "
                "COUNT(*) AS n FROM trades WHERE zone=?",
                (z,),
            ).fetchone()
            zone_from[z] = {"from": c["a"], "to": c["b"], "rows": int(c["n"] or 0)}

        # Field availability: schema drift is real (dmr_selected / mcap_grade_* /
        # *_path all appeared mid-window). Derived from the frozen entry values
        # already stored per trade, so no snapshot pass is needed.
        field_availability: dict[str, Any] = {}
        for field, expr in (
            ("state_enter_price", "enter_price_source='state_enter_price'"),
            ("dmr_selected", "zone='DMR'"),
            ("mcap_grade_30m", "mcap_grade_30m IS NOT NULL"),
            ("mcap_grade_2h", "mcap_grade_2h IS NOT NULL"),
            ("mcap_grade_6h", "mcap_grade_6h IS NOT NULL"),
            ("qualified_path", "qualified_path IS NOT NULL"),
            ("confirmed_path", "confirmed_path IS NOT NULL"),
            ("ret_1w", "ret_1w IS NOT NULL"),
            ("ret_1mo", "ret_1mo IS NOT NULL"),
        ):
            c = self._conn.execute(
                f"SELECT MIN(enter_time_utc) AS t, MIN(enter_scan_id) AS s FROM trades WHERE {expr}"
            ).fetchone()
            field_availability[field] = {"from_utc": c["t"], "from_scan_id": c["s"]}

        segments = [
            {
                "version": r["parameter_version"],
                "from_utc": r["a"],
                "to_utc": r["b"],
                "first_exit_utc": r["d"],
                "last_exit_utc": r["c"],
                "trades": int(r["n"] or 0),
            }
            for r in self._conn.execute(
                "SELECT parameter_version, MIN(enter_time_utc) AS a, MAX(enter_time_utc) AS b, "
                "MAX(COALESCE(exit_time_utc, enter_time_utc)) AS c, MIN(exit_time_utc) AS d, "
                "COUNT(*) AS n "
                "FROM trades WHERE parameter_version IS NOT NULL AND parameter_version<>'' "
                "GROUP BY parameter_version ORDER BY a"
            ).fetchall()
        ]

        # —— 调参分段（param_hash）——
        #
        # parameter_version 管「哪套标准」，一整个版本周期内不变；param_hash 管
        # 「这套标准的哪一次调参」。选币榜Y 的 216 主导层上线前后 parameter_version
        # 都是 param-v2.0.0-screener-y，**只有 param_hash 变** —— 没有这份分段，
        # 《复盘选币》页面会把「天花板关」与「天花板主导」两段混算成一个胜率，
        # 那正是文档B §3.1 禁止的跨段聚合。
        #
        # NULL 段（阶段 0 之前没有指纹的历史行）单独成一段，hash 记为 None：
        # 它不可与任何一段合并统计，但必须可见 —— 隐藏它会让「总数对不上」。
        hash_segments = [
            {
                "param_hash": r["ph"],
                "parameter_version": r["pv"],
                "from_utc": r["a"],
                "to_utc": r["b"],
                "first_exit_utc": r["d"],
                "last_exit_utc": r["c"],
                "trades": int(r["n"] or 0),
            }
            for r in self._conn.execute(
                "SELECT param_hash AS ph, MIN(parameter_version) AS pv, "
                "MIN(enter_time_utc) AS a, MAX(enter_time_utc) AS b, "
                "MAX(COALESCE(exit_time_utc, enter_time_utc)) AS c, MIN(exit_time_utc) AS d, "
                "COUNT(*) AS n FROM trades GROUP BY param_hash ORDER BY a"
            ).fetchall()
        ]

        status_counts = {
            r["status"]: int(r["n"])
            for r in self._conn.execute("SELECT status, COUNT(*) AS n FROM trades GROUP BY status")
        }

        return {
            "watermark_scan_id": wm.get("scan_id"),
            "watermark_ts": wm.get("ts"),
            "nodes_ingested": wm.get("nodes_ingested"),
            "first_ts": row["first_ts"],
            "last_ts": row["last_ts"] or wm.get("ts"),
            "trade_rows": row["n"],
            "closed_rows": status_counts.get("CLOSED", 0),
            "open_rows": status_counts.get("OPEN", 0),
            "zone_available": zone_from,
            "field_availability": field_availability,
            "parameter_segments": segments,
            "param_hash_segments": hash_segments,
            "parameter_version": wm.get("parameter_version"),
        }


def _days_between(a: Optional[str], b: Optional[str]) -> Optional[float]:
    try:
        da = datetime.fromisoformat(str(a).replace("Z", "+00:00")) if a else None
        db = datetime.fromisoformat(str(b).replace("Z", "+00:00")) if b else None
    except (TypeError, ValueError):
        return None
    if da is None or db is None:
        return None
    d = (db - da).total_seconds() / 86400.0
    return d if d > 0 else None


def _median(xs: list[float]) -> Optional[float]:
    if not xs:
        return None
    ys = sorted(xs)
    m = len(ys) // 2
    return ys[m] if len(ys) % 2 else (ys[m - 1] + ys[m]) / 2.0


DWELL_BUCKETS = ((1, 1), (2, 2), (3, 3), (4, 7), (8, 15), (16, 31), (32, 10**9))


def _dwell_histogram(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Node-count buckets. Exposes the DMR Top-K churn (≈25% of DMR trades last
    exactly one 15m node) instead of burying it inside an average."""
    out = []
    for lo, hi in DWELL_BUCKETS:
        n = sum(1 for r in rows if lo <= int(r.get("dwell_nodes") or 0) <= hi)
        out.append({"from_nodes": lo, "to_nodes": None if hi >= 10**9 else hi, "count": n})
    return out


def _equity_curve(rows: list[dict[str, Any]], *, max_points: int = 240) -> tuple[list[dict[str, Any]], Optional[float]]:
    """Cumulative pnl_pct ordered by realization time, plus max drawdown.

    Answers "is this zone steadily positive or riding one outlier" — the
    single number a sum/average cannot show. Downsampled so a 5k-row WATCH
    query stays a small payload; the drawdown is computed on the *full*
    series before downsampling.
    """
    pts = [
        (r.get("exit_time_utc") or r.get("enter_time_utc") or "", float(r["pnl_pct"]))
        for r in rows
        if r.get("pnl_pct") is not None
    ]
    if not pts:
        return [], None
    pts.sort(key=lambda x: x[0])
    cum = 0.0
    peak = 0.0
    dd = 0.0
    series: list[tuple[str, float]] = []
    for ts, v in pts:
        cum += v
        peak = max(peak, cum)
        dd = min(dd, cum - peak)
        series.append((ts, cum))
    step = max(1, len(series) // max_points)
    thin = series[::step]
    if thin[-1] != series[-1]:
        thin.append(series[-1])
    return (
        [{"at_utc": t, "cum_pnl_pct": round(c, 10)} for t, c in thin],
        dd if dd < 0 else 0.0,
    )


def summarize_trades(
    rows: list[dict[str, Any]],
    *,
    include_open_count: int = 0,
    window_days: Optional[float] = None,
    with_series: bool = True,
) -> dict[str, Any]:
    """Aggregate a filtered CLOSED set.

    Denominators are deliberately three different numbers and all three are
    reported: ``trades`` (every closed record, §6.1 main figure), ``usable``
    (records with a computable PnL — the divisor for win rate / averages), and
    ``open_trades`` (never in any PnL figure, R1).
    """
    closed = [r for r in rows if r.get("status") == "CLOSED"]
    usable = [r for r in closed if r.get("pnl_pct") is not None]
    pcts = [float(r["pnl_pct"]) for r in usable]
    wins = [p for p in pcts if p > 0]
    losses = [p for p in pcts if p < 0]
    win = sum(1 for r in usable if int(r.get("pnl_sign") or 0) == 1)
    flat = sum(1 for r in usable if int(r.get("pnl_sign") or 0) == 0)
    loss = sum(1 for r in usable if int(r.get("pnl_sign") or 0) == -1)
    pos = sum(wins)
    neg = abs(sum(losses))
    coins = {str(r.get("canonical_asset_id") or r.get("symbol") or "") for r in closed}
    coins.discard("")
    up_n = sum(1 for r in closed if r.get("direction") == "up")
    dn_n = sum(1 for r in closed if r.get("direction") == "down")
    missing = len(closed) - len(usable)

    flag_counts: dict[str, int] = {}
    for r in closed:
        for f in r.get("flags") or []:
            flag_counts[str(f)] = flag_counts.get(str(f), 0) + 1

    dwell_min = [float(r.get("dwell_minutes") or 0) for r in closed]
    avg_dwell = (sum(dwell_min) / len(dwell_min)) if dwell_min else None

    curve, max_dd = ([], None)
    if with_series:
        curve, max_dd = _equity_curve(usable)

    turnover = None
    if window_days and window_days > 0 and coins:
        turnover = len(closed) / len(coins) / float(window_days)

    return {
        # —— 三口径计数（§6.1）——
        "trades": len(closed),
        "usable": len(usable),
        "missing_px": missing,
        "unique_coins": len(coins),
        "open_trades": include_open_count,
        "up": up_n,
        "down": dn_n,
        # —— 需求必备 ——
        "win": win,
        "flat": flat,
        "loss": loss,
        "win_rate": (win / len(usable)) if usable else None,
        "sum_pct": sum(pcts) if pcts else 0.0,
        "avg_pct": (sum(pcts) / len(pcts)) if pcts else None,
        # —— §15.2 补充指标 ——
        "median_pct": _median(pcts),
        "max_pct": max(pcts) if pcts else None,
        "min_pct": min(pcts) if pcts else None,
        "avg_win_pct": (pos / len(wins)) if wins else None,
        "avg_loss_pct": (-neg / len(losses)) if losses else None,
        "profit_factor": (pos / neg) if neg > 0 else None,
        "avg_dwell_minutes": avg_dwell,
        "median_dwell_minutes": _median(dwell_min),
        "dwell_histogram": _dwell_histogram(closed) if with_series else [],
        "equity_curve": curve,
        "max_drawdown_pct": max_dd,
        "turnover_per_coin_per_day": turnover,
        # —— 数据质量 ——
        "data_quality": (len(usable) / len(closed)) if closed else None,
        "flag_counts": flag_counts,
    }


def default_ledger_path(data_dir: Path) -> Path:
    return Path(data_dir) / "review" / "ledger.sqlite"


MAX_CATCHUP_NODES = 240  # ≈2.5 days of 15m nodes


def _board_ids_between(snaps: Path, after: Optional[str], upto: str) -> list[str]:
    """scan_ids present on disk in (after, upto], ascending.

    `scan_id` is `YYYYMMDD-NNN`, zero-padded, so lexical order == chronological
    order and we never need to parse 886 x 2.5 MB files to sequence them.
    """
    out = []
    for path in snaps.glob("*.json"):
        if path.name.endswith(".full.json"):
            continue
        sid = path.name[:-5]
        if sid > upto:
            continue
        if after is not None and sid <= after:
            continue
        out.append(sid)
    return sorted(out)


def _read_json(path: Path) -> Optional[dict[str, Any]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def ingest_scan(
    data_dir: Path,
    scan_id: str,
    *,
    inbox_dir: Optional[Path] = None,
    max_catchup: int = MAX_CATCHUP_NODES,
) -> dict[str, Any]:
    """Append on-disk boards up to and including ``scan_id``. Never raises (P9).

    Catch-up matters: the diff engine compares *adjacent* nodes, so feeding it
    node N when the watermark still sits at N−3 would silently erase every
    enter/exit that happened inside the skipped nodes and corrupt dwell counts.
    That is not hypothetical — it is exactly what a loop restart or a missed
    hook produces. So we replay the whole missing run instead of jumping.

    Beyond ``max_catchup`` nodes we refuse rather than guess: the watermark
    stays put, the API reports ``stale``, and the operator rebuilds with
    ``scripts/build_review_ledger.py``.
    """
    led = None
    try:
        data_dir = Path(data_dir)
        snaps = data_dir / "snapshots"
        if inbox_dir is None:
            inbox_dir = data_dir.parent / "dmr-adapter" / "inbox"
        inbox_dir = Path(inbox_dir)

        led = ReviewLedger(default_ledger_path(data_dir))
        wm = led.watermark()
        prev_sid = wm.get("scan_id")

        if prev_sid is None:
            # Cold ledger: only take the requested node. A full backfill inside
            # the 15m scan loop would block it for ~80s; that is the build
            # script's job.
            pending = [scan_id] if (snaps / f"{scan_id}.json").is_file() else []
        else:
            if scan_id <= prev_sid:
                return {"status": "duplicate", "scan_id": scan_id, "watermark": prev_sid}
            pending = _board_ids_between(snaps, prev_sid, scan_id)

        if not pending:
            return {"status": "missing_board", "scan_id": scan_id}
        if len(pending) > max_catchup:
            log.warning(
                "review ledger too far behind (watermark=%s target=%s missing=%d) — "
                "run scripts/build_review_ledger.py --reset",
                prev_sid,
                scan_id,
                len(pending),
            )
            return {
                "status": "gap_too_large",
                "scan_id": scan_id,
                "watermark": prev_sid,
                "missing": len(pending),
            }
        if len(pending) > 1:
            log.info("review ledger catching up %d nodes (%s → %s)", len(pending), prev_sid, scan_id)

        ok = 0
        for sid in pending:
            board = _read_json(snaps / f"{sid}.json")
            if board is None:
                log.warning("review ingest skipped unreadable board %s", sid)
                continue
            inbox_doc = _read_json(inbox_dir / f"{sid}.candidates.json")
            out = led.ingest_board(board, inbox_doc=inbox_doc)
            if out.get("status") == "ok":
                ok += 1
        return {"status": "ok", "scan_id": scan_id, "ingested": ok, "catchup": len(pending)}
    except Exception as e:
        log.warning("review ingest failed scan=%s: %s", scan_id, e)
        return {"status": "error", "scan_id": scan_id, "error": str(e)}
    finally:
        if led is not None:
            try:
                led.close()
            except Exception:
                pass
