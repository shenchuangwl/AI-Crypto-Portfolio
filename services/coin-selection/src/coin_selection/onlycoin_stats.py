"""《OnlyCoin · 来源候选回放》的 DMR 区历史统计 —— 唯一币种首次入选 → 完整交易日收线。

与《原复盘 · 进出账本》的关系（文档B §3.1「两个栏目共用同一套规则内核」）：

* **成员事实**只来自 OnlyCoin 独立账本（``review/onlycoin.sqlite``），也就是回放
  面板上方那三列名单的同一份投影。**不读 ``review/ledger.sqlite``**，因此这里
  不存在「套用原账本成交结果」的可能。
* **价格**只来自板面快照的打印价（``last_price`` → ``ref_price``），复用
  ``review_replay.print_px`` / ``index_rows`` —— 与原账本给 DMR 记停留价/退出价
  用的是同一个函数，不是第二套实现。
* **盈亏**复用 ``review_pnl.realized_pnl``（全仓库唯一一处方向感知盈亏实现）。
  它只吃「方向 + 停留价 + 退出价」，不含仓位 / 数量 / 本金 / 杠杆 / 手续费。
* **汇总**复用 ``review_ledger.summarize_trades``，因此胜率 / 盈亏比 / 中位数等
  指标与原 DMR 区逐字段同口径、同分母。

与原账本口径**唯一**的差别在「退出」的定义，这是需求指定的：原账本按标的真实
退出候选池的那一刻平账；本统计按**首次入选所属完整交易日的收线时点**平账
（00:00 UTC 换日之前的最后一个有效节点，即 ``-095`` / 23:45 UTC）。因此同一个币
在这里只会出现一次，而在原账本里可能有多笔进出。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from .review_pnl import realized_pnl
from .review_replay import coin_id_of, index_rows, print_px

UTC = timezone.utc

#: 15 分钟节点栅格，与 ``review_replay.NODES_PER_DAY`` 同一张栅格。
NODES_PER_DAY = 96
NODE_MINUTES = 15
#: 日线换线（00:00 UTC）之前的最后一个有效节点：``-095`` = 23:45 UTC。
LAST_NODE_SEQ = NODES_PER_DAY - 1

#: 收线节点缺快照 / 标的当刻不在板面时，向前回溯的**上限**（节点数）。
#: 原账本的 ``prev_node`` 回退只退一个节点（它是顺序回放，前一张板就在手里）；
#: 这里是随机存取，给一个有界的窗口，回溯到哪一个节点会逐行写进 ``exit_scan_id``，
#: 并打 ``EXIT_PRICE_PREV_NODE`` 旗标 —— 不会把回溯价伪装成收线价。
MAX_EXIT_BACKTRACK_NODES = 8

#: 单次请求允许读取的快照上限。实测一张约 3.3 MB / 62 ms（含建索引），320 张
#: ≈ 20 s —— 超过就直接拒绝并提示收窄区间，而不是让网关卡住几分钟再超时。
MAX_SNAPSHOT_READS = 320
#: 单个统计区间允许跨越的最大天数。14 天 × 约 20 个首次入选节点 ≈ 280 次读取，
#: 正好落在上面的预算内；再宽就必然撞预算，不如提前说清楚。
MAX_RANGE_DAYS = 14
#: 周期对比的最大列数，与前端 ``MAX_COMPARE_PERIODS`` 一致（主区间 + 3）。
MAX_COMPARE_RANGES = 3

DAY_END_RULE = (
    "首次入选所属 UTC 业务日（00:00 UTC 起算）的最后一个 15m 节点 -095 / 23:45 UTC；"
    "即日线换线前的最后有效时点，不使用标的实际退出候选池的时间"
)


class OnlyCoinStatsError(ValueError):
    """查询非法（400），与数据不可用（503）区分开。"""


def parse_ts(value: object) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(UTC)
    text = str(value or "")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise OnlyCoinStatsError(f"invalid timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise OnlyCoinStatsError("timestamp timezone required")
    return parsed.astimezone(UTC)


def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def day_start(day: str) -> datetime:
    try:
        return datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC)
    except (TypeError, ValueError) as exc:
        raise OnlyCoinStatsError("day must be YYYY-MM-DD") from exc


def scan_id_of(moment: datetime, seq: Optional[int] = None) -> str:
    """``YYYYMMDD-NNN``：某 UTC 日的第 N 个 15m 节点。"""
    d = moment.astimezone(UTC)
    if seq is None:
        seq = (d.hour * 60 + d.minute) // NODE_MINUTES
    return f"{d:%Y%m%d}-{seq:03d}"


def node_time(day: datetime, seq: int) -> datetime:
    return day.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
        minutes=NODE_MINUTES * seq
    )


# --------------------------------------------------------------------------
# 成员事实：OnlyCoin 账本的区间首次入选
# --------------------------------------------------------------------------

def first_entries(
    conn: Optional[sqlite3.Connection],
    *,
    start: datetime,
    end: datetime,
    cutoff: datetime,
) -> list[dict[str, Any]]:
    """区间 ``[start, end)`` 内、**按合约符号**去重的首次入选。

    投影规则逐条对齐 ``OnlyCoinLedger.daily()``：

    * 提交按 ``commit_seq`` 升序读取（= 逻辑扫描时间升序，账本本身禁止乱序写入）；
    * 只计入 ``available_at <= cutoff`` 的提交 —— 因果可用性，不是逻辑时间；
    * 同一符号只保留第一条，后续重复入选/换方向都不再新增成员；
    * ``directions`` 追加后续出现过的方向，但 ``first_direction`` 冻结在首条。

    与 ``daily()`` 的等价性由 ``test_onlycoin_stats`` 用整日区间逐字段断言。
    """
    if conn is None:
        return []
    days = []
    cursor = start.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    last = (end - timedelta(microseconds=1)).astimezone(UTC)
    while cursor <= last:
        days.append(cursor.date().isoformat())
        cursor += timedelta(days=1)
    if not days:
        return []
    placeholders = ",".join("?" for _ in days)
    rows = conn.execute(
        f"SELECT * FROM onlycoin_scan_commits WHERE business_date IN ({placeholders}) ORDER BY commit_seq",
        days,
    ).fetchall()

    members: dict[str, dict[str, Any]] = {}
    for r in rows:
        selected = parse_ts(r["selected_at"])
        if not (start <= selected < end):
            continue
        if parse_ts(r["available_at"]) > cutoff:
            continue
        batch = json.loads(r["batch_json"])
        for ordinal, c in enumerate(batch.get("candidates") or []):
            symbol = c.get("symbol")
            direction = c.get("direction")
            if not symbol or direction not in ("LONG", "SHORT"):
                continue
            existing = members.get(symbol)
            if existing is not None:
                if direction not in existing["directions"]:
                    existing["directions"].append(direction)
                existing["repeat_entries"] += 1
                continue
            member = dict(c)
            member.update(
                symbol=symbol,
                first_selected_at=r["selected_at"],
                first_available_at=r["available_at"],
                first_scan_id=r["scan_id"],
                first_direction=direction,
                directions=[direction],
                parameter_version=c.get("parameter_version", batch.get("parameter_version")),
                rule_identity=c.get("rule_identity", batch.get("rule_identity")),
                provenance=r["provenance"],
                first_ordinal=ordinal,
                availability_quality="COMMITTED" if r["provenance"] == "live_committed" else "PROXY",
                repeat_entries=0,
            )
            members[symbol] = member
    return list(members.values())


def days_with_commits(
    conn: Optional[sqlite3.Connection],
    *,
    start: datetime,
    end: datetime,
    cutoff: datetime,
) -> list[str]:
    """区间内**确有提交**的业务日 —— 「零行」是空窗口还是没数据，必须能分辨。"""
    if conn is None:
        return []
    # 时间比较一律在 Python 侧按 datetime 做：``available_at`` 带微秒而 ``cutoff``
    # 通常到秒，SQL 里的字符串比较会在 `'.' < 'Z'` 上判反一整秒的边界。
    rows = conn.execute(
        "SELECT business_date, selected_at, available_at FROM onlycoin_scan_commits ORDER BY commit_seq"
    ).fetchall()
    out = {
        r["business_date"]
        for r in rows
        if start <= parse_ts(r["selected_at"]) < end and parse_ts(r["available_at"]) <= cutoff
    }
    return sorted(out)


def dedupe_by_coin(entries: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """唯一币种去重：按项目既有的 ``coin_id_of``（canonical → underlying → symbol）。

    只按展示名称（symbol）去重会把同一资产的不同合约当成两个币；只按 symbol 不
    去重又会让同一资产重复计数。这里与原账本 ``summarize_trades`` 的 ``unique_coins``
    用同一个身份来源。
    """
    best: dict[str, dict[str, Any]] = {}
    for e in sorted(entries, key=lambda x: (str(x["first_selected_at"]), int(x.get("first_ordinal") or 0))):
        key = coin_id_of(e)
        if not key:
            key = str(e.get("symbol") or "").upper()
        kept = best.get(key)
        if kept is None:
            e = dict(e, coin_id=key)
            best[key] = e
            continue
        # 同一唯一币种的后续入选（含换合约、换方向）不另计，只记次数。
        kept["repeat_entries"] = int(kept.get("repeat_entries") or 0) + 1 + int(e.get("repeat_entries") or 0)
    return sorted(best.values(), key=lambda x: (str(x["first_selected_at"]), str(x["symbol"])))


# --------------------------------------------------------------------------
# 价格：板面快照打印价（与原账本给 DMR 记价的同一函数）
# --------------------------------------------------------------------------

class SnapshotReader:
    """按 scan_id 随机存取板面快照，带请求内缓存与读取预算。"""

    def __init__(self, snapshots: Path, *, budget: int = MAX_SNAPSHOT_READS):
        self.dir = Path(snapshots)
        self.budget = budget
        self.reads = 0
        self._cache: dict[str, Optional[dict[tuple[str, str], dict[str, Any]]]] = {}

    def index(self, scan_id: str) -> Optional[dict[tuple[str, str], dict[str, Any]]]:
        if scan_id in self._cache:
            return self._cache[scan_id]
        path = self.dir / f"{scan_id}.json"
        if not path.is_file():
            self._cache[scan_id] = None
            return None
        if self.reads >= self.budget:
            raise OnlyCoinStatsError(
                f"statistics range needs more than {self.budget} board snapshots; narrow the range"
            )
        self.reads += 1
        try:
            board = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._cache[scan_id] = None
            return None
        idx = index_rows(board)
        self._cache[scan_id] = idx
        return idx

    def row(self, scan_id: str, symbol: str, direction: str) -> Optional[dict[str, Any]]:
        idx = self.index(scan_id)
        if not idx:
            return None
        return idx.get((symbol, direction))


def _direction_of(pool_direction: str) -> str:
    """OnlyCoin 账本记 ``LONG`` / ``SHORT``；板面与盈亏内核用 ``up`` / ``down``。"""
    return "up" if pool_direction == "LONG" else "down"


def build_row(
    entry: dict[str, Any],
    reader: SnapshotReader,
    *,
    now: datetime,
) -> dict[str, Any]:
    """一条唯一币种的完整统计行（入选 → 该业务日收线）。"""
    symbol = str(entry["symbol"])
    pool = str(entry["first_direction"])
    direction = _direction_of(pool)
    enter_scan = str(entry["first_scan_id"])
    enter_ts = parse_ts(entry["first_selected_at"])
    day0 = enter_ts.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day0 + timedelta(days=1)
    flags: list[str] = []

    enter_row = reader.row(enter_scan, symbol, direction)
    enter_price, enter_src = print_px(enter_row)
    if enter_price is None:
        flags.append("MISSING_ENTRY_PRICE")

    # —— 退出：该业务日 -095（23:45 UTC）。日未走完 ⇒ 不平账。——
    day_complete = now >= day_end
    exit_scan: Optional[str] = None
    exit_ts: Optional[datetime] = None
    exit_price: Optional[float] = None
    exit_src: Optional[str] = None
    if not day_complete:
        flags.append("INCOMPLETE_DAY")
    else:
        enter_seq = int(enter_scan.rsplit("-", 1)[-1]) if "-" in enter_scan else 0
        for back in range(MAX_EXIT_BACKTRACK_NODES + 1):
            seq = LAST_NODE_SEQ - back
            if seq < enter_seq:
                break
            candidate = scan_id_of(day0, seq)
            row = reader.row(candidate, symbol, direction)
            px, src = print_px(row)
            if px is not None:
                exit_scan, exit_ts, exit_price, exit_src = candidate, node_time(day0, seq), px, src
                if back:
                    # 收线节点没有快照，或该刻标的已不在板面 —— 逐行标注，不冒充收线价。
                    flags.append("EXIT_PRICE_PREV_NODE")
                break
        if exit_price is None:
            flags.append("MISSING_EXIT_PRICE")
            exit_scan = scan_id_of(day0, LAST_NODE_SEQ)
            exit_ts = node_time(day0, LAST_NODE_SEQ)

    pnl_pct, pnl_sign = (None, None)
    if day_complete:
        pnl_pct, pnl_sign = realized_pnl(direction, enter_price, exit_price)

    dwell_minutes = None
    dwell_nodes = None
    if exit_ts is not None:
        dwell_minutes = max(0.0, (exit_ts - enter_ts).total_seconds() / 60.0)
        dwell_nodes = int(round(dwell_minutes / NODE_MINUTES))

    return {
        "coin_id": str(entry.get("coin_id") or coin_id_of(entry)),
        "symbol": symbol,
        "canonical_asset_id": entry.get("canonical_asset_id"),
        "underlying_asset": entry.get("underlying_asset"),
        "contract_multiplier": entry.get("contract_multiplier"),
        "pool": pool,
        "direction": direction,
        "zone": "DMR",
        # summarize_trades 只统计 CLOSED，OPEN 永不进任何盈亏汇总（R1）。
        # 未走完的业务日就是 OPEN —— 不完整的日子不许伪装成完整日统计。
        "status": "CLOSED" if day_complete else "OPEN",
        "business_date": day0.date().isoformat(),
        "day_complete": day_complete,
        "enter_scan_id": enter_scan,
        "enter_time_utc": iso(enter_ts),
        "enter_price": enter_price,
        "enter_price_source": enter_src,
        "exit_scan_id": exit_scan,
        "exit_time_utc": iso(exit_ts) if exit_ts else None,
        "exit_price": exit_price,
        "exit_price_source": exit_src,
        "dwell_minutes": dwell_minutes,
        "dwell_nodes": dwell_nodes,
        "pnl_pct": pnl_pct,
        "pnl_sign": pnl_sign,
        "first_available_at": entry.get("first_available_at"),
        "availability_quality": entry.get("availability_quality"),
        "directions": entry.get("directions") or [pool],
        "repeat_entries": int(entry.get("repeat_entries") or 0),
        "parameter_version": entry.get("parameter_version"),
        "flags": flags,
        "reason_codes": entry.get("reason_codes") or [],
        "mcap_combo": entry.get("mcap_combo_code"),
        "liquidity_grade": entry.get("liquidity_grade"),
    }


# --------------------------------------------------------------------------
# 区间统计
# --------------------------------------------------------------------------

def range_stats(
    conn: Optional[sqlite3.Connection],
    reader: SnapshotReader,
    *,
    ident: str,
    label: str,
    start: datetime,
    end: datetime,
    cutoff: datetime,
    now: datetime,
) -> dict[str, Any]:
    """一个**独立**统计区间：首次入选、去重、平账、汇总都只在它自己内部完成。"""
    from .review_ledger import summarize_trades

    if end <= start:
        raise OnlyCoinStatsError("range end must be after range start")
    span_days = (end - start).total_seconds() / 86400.0
    if span_days > MAX_RANGE_DAYS:
        raise OnlyCoinStatsError(f"range spans {span_days:.1f} days; limit is {MAX_RANGE_DAYS}")

    entries = dedupe_by_coin(first_entries(conn, start=start, end=end, cutoff=cutoff))
    rows = [build_row(e, reader, now=now) for e in entries]
    open_rows = sum(1 for r in rows if r["status"] == "OPEN")
    summary = summarize_trades(rows, include_open_count=open_rows, window_days=span_days, with_series=True)
    # 多空各自一张卡：同一个 summarize_trades 再跑一遍子集，不是第二套统计实现。
    summary["by_direction"] = {
        d: summarize_trades(
            [r for r in rows if r["direction"] == d],
            include_open_count=sum(1 for r in rows if r["direction"] == d and r["status"] == "OPEN"),
            window_days=span_days,
            with_series=False,
        )
        for d in ("up", "down")
    }
    return {
        "id": ident,
        "label": label,
        "from": iso(start),
        "to": iso(end),
        "cutoff": iso(cutoff),
        "rows": rows,
        "summary": summary,
        "unique_symbols": len({r["symbol"] for r in rows}),
        "incomplete_days": sorted({r["business_date"] for r in rows if not r["day_complete"]}),
        "days_with_commits": days_with_commits(conn, start=start, end=end, cutoff=cutoff),
        "span_days": span_days,
    }


def build_stats(
    *,
    ledger_conn: Optional[sqlite3.Connection],
    snapshots: Path,
    business_date: str,
    as_of: datetime,
    custom: Optional[tuple[datetime, datetime]] = None,
    compare: Iterable[tuple[str, datetime, datetime]] = (),
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """回放日 / 自定义起止 / 周期对比 → 一份完整统计载荷。"""
    clock = (now or datetime.now(UTC)).astimezone(UTC)
    day0 = day_start(business_date)
    day_end = day0 + timedelta(days=1)
    reader = SnapshotReader(snapshots)

    if custom is None:
        # 默认口径 = 用户刚刚回放的那一个业务日，可用性截至 as_of。
        # 这样上方三列名单与下方统计的成员集合逐字节相同。
        main_start, main_end, main_cutoff = day0, day_end, as_of
        main_label = f"回放业务日 {business_date}"
    else:
        main_start, main_end = custom
        main_cutoff = main_end
        main_label = "自定义起止"

    main = range_stats(
        ledger_conn,
        reader,
        ident="main",
        label=main_label,
        start=main_start,
        end=main_end,
        cutoff=main_cutoff,
        now=clock,
    )

    periods: list[dict[str, Any]] = []
    for i, (ident, start, end) in enumerate(compare):
        try:
            block = range_stats(
                ledger_conn,
                reader,
                ident=ident,
                label=f"周期 {i + 2}",
                start=start,
                end=end,
                cutoff=end,
                now=clock,
            )
            block.pop("rows", None)  # 对比列只要汇总；明细永远只画主区间的。
            periods.append(block)
        except OnlyCoinStatsError as exc:
            periods.append(
                {"id": ident, "label": f"周期 {i + 2}", "from": iso(start), "to": iso(end), "error": str(exc)}
            )

    return {
        "schema": "onlycoin-stats-v1",
        "board_key": "y",
        "parameter_version": "param-v2.0.0-screener-y",
        "business_date": business_date,
        "as_of": iso(as_of),
        "server_time": iso(clock),
        "node_grid": {
            "nodes_per_day": NODES_PER_DAY,
            "interval_minutes": NODE_MINUTES,
            "last_node_seq": LAST_NODE_SEQ,
            "day_boundary_utc": "00:00:00Z",
            "exit_rule": DAY_END_RULE,
        },
        "pnl_basis": {
            "pct": "realized_pnl(direction, 停留价, 退出价) —— 与原复盘账本同一函数",
            "value": "pnl_sign ∈ {1,0,-1}；项目内「盈亏数值」列即该方向感知符号",
            "excluded": ["仓位", "数量", "本金", "杠杆", "手续费", "资金费", "滑点"],
        },
        "main": main,
        "compare": periods,
        "snapshot_reads": reader.reads,
    }
