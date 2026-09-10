#!/usr/bin/env python3
"""DMR-only historical optimizer for Screener-Y v2.0.0.

The script is research-only: it reads frozen Y snapshots and writes only under
``data/research``. Signals are formed at node t and executed no earlier than
node t+1 by default. No exchange or order endpoint is imported or called.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import statistics
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SNAPSHOTS = ROOT / "data" / "coin-selection-y" / "snapshots"
DEFAULT_OUT = ROOT / "data" / "research" / "v200-dmr-optimizer"
FLAT_EPS = 1e-12
ZONE_RANK = {"DMR": 0, "CONFIRMED": 1, "QUALIFIED": 2, "WATCH": 3, "ELIMINATED": 4}


@dataclass(frozen=True)
class Strategy:
    name: str = "baseline-next-node"
    direction: str = "both"
    entry_delay_nodes: int = 1
    ceiling_max_rank: int = 4
    min_priority: int = 1
    min_resonance_k: int = 0
    min_score: float = 0.0
    min_ss: float = 0.0
    min_mom: float = 0.0
    min_dq: float = 0.0
    min_cons: float = 0.0
    require_dmr_at_entry: bool = False
    target: Optional[float] = None
    stop: Optional[float] = None
    max_hold_nodes: Optional[int] = None
    exit_on_dmr_off: bool = True
    cost_bps: float = 0.0


def _safe_out(path: Path) -> Path:
    path = path if path.is_absolute() else ROOT / path
    path = path.resolve()
    allowed = (ROOT / "data" / "research").resolve()
    if path != allowed and not str(path).startswith(str(allowed) + "/"):
        raise ValueError(f"research output must be under {allowed}, got {path}")
    return path


def _number(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _scan_day(scan_id: str) -> str:
    return str(scan_id).split("-", 1)[0]


def _scan_index(scan_id: str) -> Optional[int]:
    try:
        day, seq = str(scan_id).split("-", 1)
        dt = datetime(int(day[:4]), int(day[4:6]), int(day[6:8]), tzinfo=timezone.utc)
        return int(dt.timestamp() // 86400) * 96 + int(seq)
    except (TypeError, ValueError, IndexError):
        return None


def _iso_for_scan(scan_id: str) -> str:
    idx = _scan_index(scan_id)
    if idx is None:
        return scan_id
    day_i, seq = divmod(idx, 96)
    dt = datetime.fromtimestamp(day_i * 86400, tz=timezone.utc) + timedelta(minutes=15 * seq)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _same_or_next_contiguous(prev_sid: str, cur_sid: str) -> bool:
    a, b = _scan_index(prev_sid), _scan_index(cur_sid)
    return a is not None and b is not None and b == a + 1


def _directional_return(direction: str, entry: float, exit_price: float) -> float:
    return exit_price / entry - 1.0 if direction == "up" else 1.0 - exit_price / entry


def _threshold_price(direction: str, entry: float, ret: float) -> float:
    return entry * (1.0 + ret) if direction == "up" else entry * (1.0 - ret)


def _candidate_from_row(row: dict[str, Any], direction: str) -> dict[str, Any]:
    z10 = _number(row.get("mcap_z10"))
    if z10 is not None and direction == "down":
        z10 = -z10
    return {
        "price": _number(row.get("last_price") or row.get("ref_price")),
        "open": _number(row.get("open_price") or row.get("open")),
        "high": _number(row.get("high_price") or row.get("high")),
        "low": _number(row.get("low_price") or row.get("low")),
        "dmr": bool(row.get("dmr_selected")),
        "score": _number(row.get("score_up") if direction == "up" else row.get("score_down")),
        "ss": _number(row.get("staircase_score")),
        "mom": _number(row.get("momentum_score")),
        "dq": _number(row.get("data_confidence")),
        "cons": _number(row.get("consistency_score")),
        "ceiling": str(row.get("mcap_ceiling_zone") or row.get("zone_ceiling") or "ELIMINATED"),
        "priority": int(row.get("mcap_priority") or 0),
        "resonance_k": int(row.get("mcap_resonance_k") or row.get("mcap_k") or 0),
        "z10": z10,
        "combo": row.get("mcap_combo_code") or row.get("combo_code"),
        "final_zone": row.get("final_zone") or row.get("effective_zone") or row.get("state"),
        "param_hash": None,
    }


def load_nodes(
    snapshots: Path,
    *,
    from_scan: Optional[str] = None,
    to_scan: Optional[str] = None,
    param_hashes: Optional[set[str]] = None,
    identity_statuses: Optional[set[str]] = None,
    rule_revisions: Optional[set[str]] = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    files = sorted(p for p in snapshots.glob("*.json") if not p.name.endswith(".full.json"))
    nodes: list[dict[str, Any]] = []
    versions: set[str] = set()
    hashes: set[str] = set()
    revisions: set[str] = set()
    statuses: set[str] = set()
    identity_mismatches = 0
    missing_nodes: list[str] = []
    previous: Optional[str] = None
    for path in files:
        sid = path.stem
        if from_scan and sid < from_scan:
            continue
        if to_scan and sid > to_scan:
            continue
        doc = json.loads(path.read_text(encoding="utf-8"))
        meta = doc.get("meta") or {}
        pv = str(meta.get("parameter_version") or "")
        ph = str(meta.get("param_hash") or "NULL")
        identity = meta.get("rule_identity") or {}
        revision = str(identity.get("rule_revision") or meta.get("rule_revision") or "LEGACY_INCOMPLETE")
        identity_status = str(identity.get("identity_status") or meta.get("identity_status") or "LEGACY_INCOMPLETE")
        if param_hashes and ph not in param_hashes:
            continue
        if identity_statuses and identity_status not in identity_statuses:
            continue
        if rule_revisions and revision not in rule_revisions:
            continue
        if pv:
            versions.add(pv)
        hashes.add(ph)
        revisions.add(revision)
        statuses.add(identity_status)
        if identity_status not in ("MATCH", "LEGACY_INCOMPLETE"):
            identity_mismatches += 1
        if previous and not _same_or_next_contiguous(previous, sid):
            a, b = _scan_index(previous), _scan_index(sid)
            if a is not None and b is not None and b > a + 1:
                for idx in range(a + 1, b):
                    day_i, seq = divmod(idx, 96)
                    dt = datetime.fromtimestamp(day_i * 86400, tz=timezone.utc)
                    missing_nodes.append(dt.strftime("%Y%m%d") + f"-{seq:03d}")
        previous = sid
        rows: dict[tuple[str, str], dict[str, Any]] = {}
        for pool_name in ("long_pool", "short_pool"):
            for row in doc.get(pool_name) or []:
                direction = str(row.get("direction") or ("up" if pool_name == "long_pool" else "down"))
                symbol = str(row.get("symbol") or "")
                if not symbol or direction not in ("up", "down"):
                    continue
                rec = _candidate_from_row(row, direction)
                rec["param_hash"] = ph
                rows[(symbol, direction)] = rec
        cycle = meta.get("cycle") or {}
        nodes.append(
            {
                "scan_id": sid,
                "timestamp": str(meta.get("scan_timestamp_utc") or _iso_for_scan(sid)),
                "reset": bool(cycle.get("reset_at_this_node")),
                "parameter_version": pv,
                "param_hash": ph,
                "rule_revision": revision,
                "identity_status": identity_status,
                "rows": rows,
            }
        )
    if nodes and versions != {"param-v2.0.0-screener-y"}:
        identity_mismatches += 1
    return nodes, {
        "snapshot_count": len(nodes),
        "first_scan": nodes[0]["scan_id"] if nodes else None,
        "last_scan": nodes[-1]["scan_id"] if nodes else None,
        "parameter_versions": sorted(versions),
        "param_hashes": sorted(hashes),
        "rule_revisions": sorted(revisions),
        "identity_statuses": sorted(statuses),
        "missing_scan_ids": missing_nodes,
        "coverage_ratio": len(nodes) / (len(nodes) + len(missing_nodes)) if nodes else 0.0,
        "identity_mismatches": identity_mismatches,
    }


def _passes(rec: dict[str, Any], strategy: Strategy, direction: str) -> bool:
    if strategy.direction in ("up", "down") and direction != strategy.direction:
        return False
    if ZONE_RANK.get(str(rec.get("ceiling")), 99) > strategy.ceiling_max_rank:
        return False
    checks = (
        (rec.get("priority"), strategy.min_priority),
        (rec.get("resonance_k"), strategy.min_resonance_k),
        (rec.get("score"), strategy.min_score),
        (rec.get("ss"), strategy.min_ss),
        (rec.get("mom"), strategy.min_mom),
        (rec.get("dq"), strategy.min_dq),
        (rec.get("cons"), strategy.min_cons),
    )
    for value, floor in checks:
        if floor and (value is None or float(value) < float(floor)):
            return False
    return True


def _barrier_exit(
    rec: dict[str, Any],
    *,
    direction: str,
    entry: float,
    target: Optional[float],
    stop: Optional[float],
) -> Optional[tuple[float, str]]:
    if target is None and stop is None:
        return None
    high = _number(rec.get("high"))
    low = _number(rec.get("low"))
    price = _number(rec.get("price"))
    target_hit = stop_hit = False
    if direction == "up":
        target_hit = bool(target is not None and high is not None and high >= entry * (1.0 + target))
        stop_hit = bool(stop is not None and low is not None and low <= entry * (1.0 - stop))
    else:
        target_hit = bool(target is not None and low is not None and low <= entry * (1.0 - target))
        stop_hit = bool(stop is not None and high is not None and high >= entry * (1.0 + stop))
    if target_hit and stop_hit:
        assert stop is not None
        return _threshold_price(direction, entry, -stop), "STOP_AMBIGUOUS"
    if stop_hit:
        assert stop is not None
        return _threshold_price(direction, entry, -stop), "STOP"
    if target_hit:
        assert target is not None
        return _threshold_price(direction, entry, target), "TARGET"
    if price is None:
        return None
    ret = _directional_return(direction, entry, price)
    if stop is not None and ret <= -stop:
        # Y snapshots retain only a node print, not an intranode fill path. If
        # that print has crossed the stop, using the threshold would invent a
        # better fill than the historical data supports.
        return price, "STOP_CLOSE_GAP"
    if target is not None and ret >= target:
        return _threshold_price(direction, entry, target), "TARGET_CLOSE"
    return None


def simulate(nodes: list[dict[str, Any]], strategy: Strategy) -> list[dict[str, Any]]:
    pending: dict[tuple[str, str], dict[str, Any]] = {}
    opened: dict[tuple[str, str], dict[str, Any]] = {}
    closed: list[dict[str, Any]] = []
    previous_rows: dict[tuple[str, str], dict[str, Any]] = {}
    previous_node: Optional[dict[str, Any]] = None

    def close(key: tuple[str, str], node: dict[str, Any], exit_price: float, reason: str) -> None:
        pos = opened.pop(key)
        gross = _directional_return(key[1], pos["enter_price"], exit_price)
        cost = strategy.cost_bps / 10000.0
        closed.append(
            {
                **pos,
                "symbol": key[0],
                "direction": key[1],
                "exit_scan_id": node["scan_id"],
                "exit_time_utc": node.get("timestamp"),
                "exit_price": exit_price,
                "exit_reason": reason,
                "gross_pnl": gross,
                "net_pnl": gross - cost,
                "dwell_nodes": pos["age"],
            }
        )

    for node_i, node in enumerate(nodes):
        rows = node["rows"]
        gap_boundary = bool(
            previous_node
            and not _same_or_next_contiguous(previous_node["scan_id"], node["scan_id"])
        )
        if gap_boundary and previous_node is not None:
            # A delayed order cannot leap over a missing scan and still be
            # called t+1. Mark existing positions at the last pre-gap print;
            # the unobserved gap-side move is not attributed to the strategy.
            pending.clear()
            for key in list(opened):
                rec = previous_rows.get(key) or {}
                px = _number(rec.get("price"))
                if px is not None:
                    close(key, previous_node, px, "GAP_BOUNDARY")
                else:
                    opened.pop(key)
        if node.get("reset"):
            pending.clear()
            for key in list(opened):
                rec = rows.get(key) or previous_rows.get(key) or {}
                px = _number(rec.get("price"))
                if px is not None:
                    close(key, node, px, "CYCLE_RESET")
                else:
                    opened.pop(key)

        for key, pend in list(pending.items()):
            if node_i < pend["execute_i"]:
                continue
            rec = rows.get(key)
            pending.pop(key)
            if rec is None or not _passes(rec, strategy, key[1]):
                continue
            if strategy.require_dmr_at_entry and not rec.get("dmr"):
                continue
            px = _number(rec.get("open") or rec.get("price"))
            if px is None or px <= 0 or key in opened:
                continue
            opened[key] = {
                "signal_scan_id": pend["signal_scan_id"],
                "signal_time_utc": pend["signal_time_utc"],
                "enter_scan_id": node["scan_id"],
                "enter_time_utc": node.get("timestamp"),
                "enter_price": px,
                "signal_features": pend["signal_features"],
                "age": 0,
            }

        for key in list(opened):
            pos = opened[key]
            pos["age"] += 1
            rec = rows.get(key)
            if rec is None:
                prev = previous_rows.get(key) or {}
                px = _number(prev.get("price"))
                if px is not None:
                    close(key, node, px, "VANISHED")
                else:
                    opened.pop(key)
                continue
            px = _number(rec.get("price"))
            if px is None:
                continue
            barrier = _barrier_exit(
                rec,
                direction=key[1],
                entry=pos["enter_price"],
                target=strategy.target,
                stop=strategy.stop,
            )
            if barrier is not None:
                close(key, node, barrier[0], barrier[1])
                continue
            if strategy.max_hold_nodes is not None and pos["age"] >= strategy.max_hold_nodes:
                close(key, node, px, "TIMEOUT")
                continue
            if strategy.exit_on_dmr_off and not rec.get("dmr"):
                close(key, node, px, "DMR_OFF")

        for key, rec in rows.items():
            prev_dmr = bool((previous_rows.get(key) or {}).get("dmr"))
            if not rec.get("dmr") or prev_dmr or key in opened or key in pending:
                continue
            if not _passes(rec, strategy, key[1]):
                continue
            delay = max(0, int(strategy.entry_delay_nodes))
            if delay == 0:
                px = _number(rec.get("price"))
                if px is not None and px > 0:
                    opened[key] = {
                        "signal_scan_id": node["scan_id"],
                        "signal_time_utc": node.get("timestamp"),
                        "enter_scan_id": node["scan_id"],
                        "enter_time_utc": node.get("timestamp"),
                        "enter_price": px,
                        "signal_features": dict(rec),
                        "age": 0,
                    }
            else:
                pending[key] = {
                    "signal_scan_id": node["scan_id"],
                    "signal_time_utc": node.get("timestamp"),
                    "signal_features": dict(rec),
                    "execute_i": node_i + delay,
                }
        previous_rows = rows
        previous_node = node
    # Avoid right-censoring: mark every still-open position at the final
    # observable node instead of silently dropping it from the statistics.
    if nodes:
        last_node = nodes[-1]
        last_rows = last_node["rows"]
        for key in list(opened):
            rec = last_rows.get(key) or previous_rows.get(key) or {}
            px = _number(rec.get("price"))
            if px is not None:
                close(key, last_node, px, "END_OF_SAMPLE")
    return [t for t in closed if t.get("exit_reason") not in ("CYCLE_RESET", "VANISHED")]


def metrics(trades: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows = sorted(list(trades), key=lambda t: (str(t.get("exit_scan_id") or ""), str(t.get("symbol") or "")))
    pnl = [float(t["net_pnl"]) for t in rows if t.get("net_pnl") is not None]
    if not pnl:
        return {
            "n": 0,
            "win_rate": None,
            "payoff_ratio": None,
            "profit_factor": None,
            "sum_return": 0.0,
            "compounded_return": 0.0,
            "max_drawdown": None,
            "avg_return": None,
            "avg_win": None,
            "avg_loss": None,
            "median_return": None,
        }
    wins = [x for x in pnl if x > FLAT_EPS]
    losses = [x for x in pnl if x < -FLAT_EPS]
    flats = len(pnl) - len(wins) - len(losses)
    avg_win = sum(wins) / len(wins) if wins else None
    avg_loss = sum(losses) / len(losses) if losses else None
    payoff = (avg_win / abs(avg_loss)) if avg_win is not None and avg_loss not in (None, 0.0) else None
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    pf = gross_profit / gross_loss if gross_loss else (math.inf if gross_profit else None)
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    for ret in pnl:
        equity *= max(0.0, 1.0 + ret)
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak if peak else 0.0)
    return {
        "n": len(pnl),
        "wins": len(wins),
        "losses": len(losses),
        "flats": flats,
        "win_rate": len(wins) / len(pnl),
        "payoff_ratio": payoff,
        "profit_factor": pf,
        "sum_return": sum(pnl),
        "compounded_return": equity - 1.0,
        "max_drawdown": max_dd,
        "avg_return": sum(pnl) / len(pnl),
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "median_return": statistics.median(pnl),
    }


def _objective(m: dict[str, Any]) -> tuple[float, float, float, float, float]:
    payoff = m.get("payoff_ratio")
    cumulative = m.get("sum_return")
    win = m.get("win_rate")
    dd = m.get("max_drawdown")
    in_target = bool(
        win is not None
        and 0.50 <= float(win) <= 1.0
        and payoff is not None
        and 1.0 <= float(payoff) <= 10.0
        and cumulative is not None
        and 1.0 <= float(cumulative) <= 10.0
    )
    return (
        1.0 if in_target else 0.0,
        -math.inf if payoff is None else min(float(payoff), 10.0),
        -math.inf if cumulative is None else float(cumulative),
        -math.inf if win is None else float(win),
        -(float(dd) if dd is not None else 1.0),
    )


def rank_leaderboard(
    leaderboard: Iterable[dict[str, Any]], *, min_trades: int
) -> list[dict[str, Any]]:
    """Filter and rank using the full target-aware metrics."""
    viable = [r for r in leaderboard if int(r.get("n") or 0) >= int(min_trades)]
    viable.sort(key=_objective, reverse=True)
    return viable


def select_walk_forward_candidates(
    candidates: list[Strategy], limit: int
) -> list[Strategy]:
    """Pre-register candidates without consulting full-sample outcomes."""
    n = int(limit or 0)
    return list(candidates if n <= 0 else candidates[:n])


def _trade_stream_digest(trades: Iterable[dict[str, Any]]) -> str:
    rows = [
        (
            str(t.get("symbol") or ""),
            str(t.get("direction") or ""),
            str(t.get("enter_scan_id") or ""),
            str(t.get("exit_scan_id") or ""),
            str(t.get("exit_reason") or ""),
            round(float(t.get("net_pnl") or 0.0), 12),
        )
        for t in trades
    ]
    return _json_digest(rows)


def dedupe_equivalent_strategies(
    candidates: list[Strategy],
    trades_by_strategy: dict[str, list[dict[str, Any]]],
) -> tuple[list[Strategy], dict[str, list[str]]]:
    """Collapse parameter variants that generate the same historical trades."""
    representatives: list[Strategy] = []
    aliases: dict[str, list[str]] = {}
    owner_by_digest: dict[str, str] = {}
    for strategy in candidates:
        digest = _trade_stream_digest(trades_by_strategy.get(strategy.name, []))
        owner = owner_by_digest.get(digest)
        if owner is None:
            owner_by_digest[digest] = strategy.name
            representatives.append(strategy)
            aliases[strategy.name] = [strategy.name]
        else:
            aliases[owner].append(strategy.name)
    return representatives, aliases


def portfolio_metrics(
    trades: Iterable[dict[str, Any]], *, concurrency: int
) -> dict[str, Any]:
    """Equal-slot realized equity with a hard concurrent-position cap.

    Valuation occurs at exits only because the snapshots do not contain a full
    intranode portfolio mark. The returned label prevents this from being
    misrepresented as a mark-to-market portfolio return.
    """
    cap = max(1, int(concurrency))
    ordered = sorted(
        list(trades),
        key=lambda t: (
            _scan_index(str(t.get("enter_scan_id") or "")) or -1,
            str(t.get("symbol") or ""),
            str(t.get("direction") or ""),
        ),
    )
    active: list[int] = []
    accepted: list[dict[str, Any]] = []
    skipped = 0
    for trade in ordered:
        enter_i = _scan_index(str(trade.get("enter_scan_id") or ""))
        exit_i = _scan_index(str(trade.get("exit_scan_id") or ""))
        if enter_i is None or exit_i is None:
            continue
        active = [x for x in active if x > enter_i]
        if len(active) >= cap:
            skipped += 1
            continue
        active.append(exit_i)
        accepted.append(trade)
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    for trade in sorted(accepted, key=lambda t: str(t.get("exit_scan_id") or "")):
        equity *= max(0.0, 1.0 + float(trade.get("net_pnl") or 0.0) / cap)
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak if peak else 0.0)
    return {
        "valuation": "realized_at_exit_only",
        "concurrency": cap,
        "opened": len(accepted),
        "skipped_by_concurrency": skipped,
        "realized_equity_return": equity - 1.0,
        "realized_max_drawdown": max_dd,
    }


def walk_forward(
    candidates: list[Strategy],
    trades_by_strategy: dict[str, list[dict[str, Any]]],
    *,
    train_days: int,
    test_days: int,
    step_days: int,
    min_train_trades: int,
) -> dict[str, Any]:
    days = sorted(
        {
            _scan_day(str(t.get("enter_scan_id") or t.get("exit_scan_id") or ""))
            for rows in trades_by_strategy.values()
            for t in rows
            if t.get("enter_scan_id") or t.get("exit_scan_id")
        }
    )
    folds: list[dict[str, Any]] = []
    start = 0
    while start + train_days + test_days <= len(days):
        train = set(days[start : start + train_days])
        test = set(days[start + train_days : start + train_days + test_days])
        scored: list[tuple[tuple[float, float, float, float, float], Strategy, dict[str, Any]]] = []
        for strategy in candidates:
            rows = [t for t in trades_by_strategy.get(strategy.name, []) if _scan_day(t["enter_scan_id"]) in train]
            m = metrics(rows)
            if m["n"] >= min_train_trades:
                scored.append((_objective(m), strategy, m))
        if scored:
            scored.sort(key=lambda x: (x[0], x[1].name), reverse=True)
            _, chosen, train_metrics = scored[0]
            test_rows = [
                t for t in trades_by_strategy.get(chosen.name, []) if _scan_day(t["enter_scan_id"]) in test
            ]
            folds.append(
                {
                    "train_from_day": days[start],
                    "train_to_day": days[start + train_days - 1],
                    "test_from_day": days[start + train_days],
                    "test_to_day": days[start + train_days + test_days - 1],
                    "selected_strategy": chosen.name,
                    "train_metrics": train_metrics,
                    "test_metrics": metrics(test_rows),
                }
            )
        start += max(1, step_days)
    return {"folds": folds, "aggregate_test": _aggregate_fold_trades(folds, trades_by_strategy)}


def _aggregate_fold_trades(
    folds: list[dict[str, Any]], trades_by_strategy: dict[str, list[dict[str, Any]]]
) -> dict[str, Any]:
    out: list[dict[str, Any]] = []
    for fold in folds:
        strategy = fold["selected_strategy"]
        for trade in trades_by_strategy.get(strategy, []):
            day = _scan_day(trade["enter_scan_id"])
            if fold["test_from_day"] <= day <= fold["test_to_day"]:
                out.append(trade)
    return metrics(out)


def strategy_grid(cost_bps: float, profile: str = "focused") -> list[Strategy]:
    directions = ("both", "up", "down")
    ceilings = (("all", 4), ("qual_or_better", 2), ("confirmed_or_better", 1), ("dmr_only", 0))
    if profile == "exhaustive":
        priorities = (1, 4, 5)
        resonances = (0, 70, 100)
    else:
        # Focused is the default because the production DMR pool already passed
        # the live rank/gate stack; these two filters are often collinear with
        # the ceiling and make an exhaustive replay unnecessarily expensive.
        priorities = (1, 5)
        resonances = (0,)
    targets: tuple[Optional[float], ...] = (None, 0.01, 0.02, 0.03, 0.05)
    stops: tuple[Optional[float], ...] = (None, 0.01, 0.02, 0.03)
    holds: tuple[Optional[int], ...] = (4, 8, 16, 32, 96)
    by_key: dict[tuple[Any, ...], Strategy] = {}
    for direction in directions:
        for ceiling_name, ceiling_rank in ceilings:
            for priority in priorities:
                for resonance in resonances:
                    for target in targets:
                        for stop in stops:
                            if target is None and stop is not None:
                                continue
                            for hold in holds:
                                key = (direction, ceiling_rank, priority, resonance, target, stop, hold)
                                if key in by_key:
                                    continue
                                name = (
                                    f"d={direction}|ceil={ceiling_name}|p={priority}|k={resonance}|"
                                    f"tp={target}|sl={stop}|h={hold}"
                                )
                                by_key[key] = Strategy(
                                    name=name,
                                    direction=direction,
                                    entry_delay_nodes=1,
                                    ceiling_max_rank=ceiling_rank,
                                    min_priority=priority,
                                    min_resonance_k=resonance,
                                    target=target,
                                    stop=stop,
                                    max_hold_nodes=hold,
                                    exit_on_dmr_off=target is None,
                                    cost_bps=cost_bps,
                                )
    return list(by_key.values())


def _json_digest(obj: Any) -> str:
    blob = json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    cols = sorted({k for row in rows for k in row})
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=cols)
        writer.writeheader()
        writer.writerows(rows)


def run(args: argparse.Namespace) -> dict[str, Any]:
    snapshots = Path(args.snapshots)
    snapshots = snapshots if snapshots.is_absolute() else ROOT / snapshots
    out = _safe_out(Path(args.out))
    out.mkdir(parents=True, exist_ok=True)
    param_hashes = {x.strip() for x in args.param_hashes.split(",") if x.strip()} if args.param_hashes else None
    identity_statuses = (
        {x.strip() for x in args.identity_statuses.split(",") if x.strip()}
        if args.identity_statuses
        else None
    )
    rule_revisions = (
        {x.strip() for x in args.rule_revisions.split(",") if x.strip()}
        if args.rule_revisions
        else None
    )
    nodes, coverage = load_nodes(
        snapshots,
        from_scan=args.from_scan,
        to_scan=args.to_scan,
        param_hashes=param_hashes,
        identity_statuses=identity_statuses,
        rule_revisions=rule_revisions,
    )
    candidates = strategy_grid(args.cost_bps, args.grid)
    if args.max_strategies:
        candidates = candidates[: args.max_strategies]
    trades_by_strategy: dict[str, list[dict[str, Any]]] = {}
    leaderboard: list[dict[str, Any]] = []
    for strategy in candidates:
        trades = simulate(nodes, strategy)
        trades_by_strategy[strategy.name] = trades
        by_direction = {
            direction: metrics([t for t in trades if t["direction"] == direction])
            for direction in ("up", "down")
        }
        m = metrics(trades)
        leaderboard.append(
            {
                "strategy": strategy.name,
                **asdict(strategy),
                **m,
                "up_n": by_direction["up"]["n"],
                "up_win_rate": by_direction["up"]["win_rate"],
                "up_payoff_ratio": by_direction["up"]["payoff_ratio"],
                "up_compounded_return": by_direction["up"]["compounded_return"],
                "down_n": by_direction["down"]["n"],
                "down_win_rate": by_direction["down"]["win_rate"],
                "down_payoff_ratio": by_direction["down"]["payoff_ratio"],
                "down_compounded_return": by_direction["down"]["compounded_return"],
            }
        )
    viable = rank_leaderboard(leaderboard, min_trades=args.min_trades)
    # Register walk-forward candidates before looking at full-sample outcomes.
    # Selecting them from `viable` would let the test day influence eligibility.
    wf_registered = select_walk_forward_candidates(candidates, args.wf_candidates)
    wf_candidates, equivalent_aliases = dedupe_equivalent_strategies(
        wf_registered, trades_by_strategy
    )
    wf = walk_forward(
        wf_candidates,
        trades_by_strategy,
        train_days=args.train_days,
        test_days=args.test_days,
        step_days=args.step_days,
        min_train_trades=args.min_train_trades,
    )
    best = viable[0] if viable else None
    best_trades = trades_by_strategy.get(best["strategy"], []) if best else []
    best_portfolio = portfolio_metrics(best_trades, concurrency=args.concurrency) if best else None
    result = {
        "schema": "v200-dmr-optimizer-v1",
        "research_only": True,
        "execution_note": "Signal_t -> Trade_t+1; no real trading or exchange call",
        "coverage": coverage,
        "constraints": {
            "cost_bps_round_trip": args.cost_bps,
            "min_trades": args.min_trades,
            "objective_priority": [
                "all_three_target_hit",
                "payoff_ratio",
                "sum_return",
                "win_rate",
                "max_drawdown",
            ],
            "same_bar_tp_sl": "STOP_AMBIGUOUS (conservative)",
            "cycle_reset_and_vanished": "excluded",
        },
        "searched_strategies": len(candidates),
        "viable_strategies": len(viable),
        "best_in_sample": best,
        "best_portfolio_realized": best_portfolio,
        "best_in_sample_by_direction": {
            direction: metrics([t for t in best_trades if t["direction"] == direction])
            for direction in ("up", "down")
        }
        if best
        else None,
        "walk_forward": wf,
        "walk_forward_registered": len(wf_registered),
        "walk_forward_unique_streams": len(wf_candidates),
        "walk_forward_equivalent_aliases": equivalent_aliases,
        "top20": viable[:20],
    }
    (out / "report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    _write_csv(out / "leaderboard.csv", leaderboard)
    _write_csv(out / "best_trades.csv", best_trades)
    manifest = {
        "argv": vars(args),
        "coverage": coverage,
        "result_digest": _json_digest(result),
        "leaderboard_digest": _json_digest(leaderboard),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    return {"out": str(out), **result}


#: 当前生效的不可变规则修订。研究脚本默认只搜同一 revision 的数据段，
#: 避免把 r2（天花板关）与 r3（主导层生效）混成一套结论（文档B §3.1）。
try:
    from coin_selection.rule_manifest import RULE_REVISION as _CURRENT_RULE_REVISION
except Exception:  # pragma: no cover - 独立运行时的兜底
    _CURRENT_RULE_REVISION = "y-v2.0.0-r3"

#: 当前生效的调参段指纹（board-variants.json 的 overrides 每变一次就换一个）。
#: 换配置后要同步改这里，否则研究脚本会继续搜旧段。
#: 核对方式：``jq -r .meta.param_hash data/coin-selection-y/latest.json``
_CURRENT_PARAM_HASH = "pf1_fcea251fa94122fe"


def build_parser() -> argparse.ArgumentParser:
    """CLI 定义单独成函数，才能被单测直接断言（否则 parser 埋在 main() 里不可测，
    ``run()`` 读了 parser 没定义的字段也发现不了 —— 这正是本轮修掉的那个缺陷）。"""
    parser = argparse.ArgumentParser(description="Historical-only v2.0.0 DMR optimizer")
    parser.add_argument("--snapshots", default=str(DEFAULT_SNAPSHOTS.relative_to(ROOT)))
    parser.add_argument("--out", default=str(DEFAULT_OUT.relative_to(ROOT)))
    parser.add_argument("--from-scan", default=None)
    parser.add_argument("--to-scan", default=None)
    # 默认只搜**当前生效**的调参段。原默认把 r2（天花板关，pf1_1b44…）与
    # r3（主导层生效，pf1_fcea…）一起收进来，那是文档B §3.1 禁止的跨段聚合：
    # 两段的规则不同，混算出的「最优参数」不对应任何一套真实规则。
    parser.add_argument(
        "--param-hashes",
        default=_CURRENT_PARAM_HASH,
        help=f"逗号分隔的 param_hash（默认 {_CURRENT_PARAM_HASH}，即当前生效段）；留空 = 不过滤",
    )
    # —— 身份过滤（文档B §3.1：不同身份段不得混算）——
    #
    # run() 一直在读这两个值，但 parser 之前没定义它们 —— CLI 一跑就 AttributeError。
    # 之所以没被发现：测试文件缺 __main__ 运行块，在本仓库的 stdlib 运行器下是空跑，
    # run-tests.sh 会报 PASS 却一个断言都没执行。两处已一并修好。
    #
    # 默认只收 identity_status=MATCH：DRIFT 段的身份与行为对不上，
    # 把它混进搜索样本会让「同一套参数」的结论建立在不同规则之上。
    parser.add_argument(
        "--identity-statuses",
        default="MATCH",
        help="逗号分隔；留空 = 不过滤。默认只用 MATCH，排除 DRIFT/UNPUBLISHED/LEGACY_INCOMPLETE",
    )
    # 默认钉在**当前生效的** revision 上（从 rule_manifest 派生，不写死字面量：
    # 递增 revision 时这里会自动跟上，不会悄悄退回搜索旧规则段的数据）。
    parser.add_argument(
        "--rule-revisions",
        default=_CURRENT_RULE_REVISION,
        help=f"逗号分隔的 rule_revision（默认 {_CURRENT_RULE_REVISION}）；留空 = 不过滤",
    )
    parser.add_argument("--cost-bps", type=float, default=20.0)
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--grid", choices=("focused", "exhaustive"), default="focused")
    parser.add_argument("--min-trades", type=int, default=60)
    parser.add_argument("--max-strategies", type=int, default=0)
    parser.add_argument("--wf-candidates", type=int, default=30)
    parser.add_argument("--train-days", type=int, default=2)
    parser.add_argument("--test-days", type=int, default=1)
    parser.add_argument("--step-days", type=int, default=1)
    parser.add_argument("--min-train-trades", type=int, default=20)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.max_strategies <= 0:
        args.max_strategies = None
    result = run(args)
    print(json.dumps({
        "out": result["out"],
        "coverage": result["coverage"],
        "searched_strategies": result["searched_strategies"],
        "best_in_sample": result["best_in_sample"],
        "walk_forward": result["walk_forward"],
    }, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
