from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "research_v200_dmr_optimizer.py"


def load_module():
    spec = importlib.util.spec_from_file_location("research_v200_dmr_optimizer", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def row(scan_id: str, price: float, *, dmr: bool, direction: str = "up") -> dict:
    return {
        "scan_id": scan_id,
        "timestamp": scan_id,
        "reset": False,
        "rows": {
            ("ABCUSDT", direction): {
                "price": price,
                "dmr": dmr,
                "score": 75.0,
                "ss": 60.0,
                "mom": 70.0,
                "dq": 80.0,
                "cons": 1.0,
                "ceiling": "DMR",
                "priority": 5,
                "resonance_k": 100,
            }
        },
    }


def test_signal_executes_on_next_node_not_signal_node():
    m = load_module()
    nodes = [
        row("20260101-000", 100.0, dmr=False),
        row("20260101-001", 110.0, dmr=True),
        row("20260101-002", 120.0, dmr=True),
        row("20260101-003", 121.0, dmr=False),
    ]

    trades = m.simulate(
        nodes,
        m.Strategy(entry_delay_nodes=1, target=None, stop=None, max_hold_nodes=None),
    )

    assert len(trades) == 1
    trade = trades[0]
    assert trade["signal_scan_id"] == "20260101-001"
    assert trade["enter_scan_id"] == "20260101-002"
    assert trade["enter_price"] == 120.0
    assert trade["exit_scan_id"] == "20260101-003"
    assert math.isclose(trade["gross_pnl"], 121.0 / 120.0 - 1.0)


def test_same_bar_target_and_stop_is_resolved_conservatively_to_stop():
    m = load_module()
    nodes = [
        row("20260101-000", 100.0, dmr=False),
        row("20260101-001", 100.0, dmr=True),
        row("20260101-002", 100.0, dmr=True),
        row("20260101-003", 100.0, dmr=True),
    ]
    nodes[2]["rows"][("ABCUSDT", "up")]["high"] = 103.0
    nodes[2]["rows"][("ABCUSDT", "up")]["low"] = 97.0

    trades = m.simulate(
        nodes,
        m.Strategy(entry_delay_nodes=0, target=0.02, stop=0.02, max_hold_nodes=8),
    )

    assert len(trades) == 1
    assert trades[0]["exit_reason"] == "STOP_AMBIGUOUS"
    assert math.isclose(trades[0]["gross_pnl"], -0.02)


def test_close_only_stop_uses_observed_price_not_optimistic_threshold():
    m = load_module()
    rec = {"price": 90.0, "high": None, "low": None}

    hit = m._barrier_exit(rec, direction="up", entry=100.0, target=None, stop=0.02)

    assert hit == (90.0, "STOP_CLOSE_GAP")


def test_metrics_include_payoff_profit_factor_cumulative_return_and_drawdown():
    m = load_module()
    metrics = m.metrics(
        [
            {"net_pnl": 0.10, "exit_scan_id": "20260101-001"},
            {"net_pnl": -0.05, "exit_scan_id": "20260101-002"},
            {"net_pnl": 0.20, "exit_scan_id": "20260101-003"},
        ]
    )

    assert metrics["n"] == 3
    assert math.isclose(metrics["win_rate"], 2 / 3)
    assert math.isclose(metrics["payoff_ratio"], 3.0)
    assert math.isclose(metrics["profit_factor"], 6.0)
    assert math.isclose(metrics["sum_return"], 0.25)
    assert math.isclose(metrics["compounded_return"], 1.10 * 0.95 * 1.20 - 1.0)
    assert metrics["max_drawdown"] > 0


def test_walk_forward_selects_on_train_and_reports_disjoint_test_folds():
    m = load_module()
    candidates = [
        m.Strategy(name="a", entry_delay_nodes=1, target=0.02, stop=0.01, max_hold_nodes=4),
        m.Strategy(name="b", entry_delay_nodes=1, target=0.03, stop=0.02, max_hold_nodes=8),
    ]
    trades_by_strategy = {
        "a": [
            {"enter_scan_id": "20260101-001", "exit_scan_id": "20260101-002", "net_pnl": 0.02},
            {"enter_scan_id": "20260102-001", "exit_scan_id": "20260102-002", "net_pnl": 0.02},
            {"enter_scan_id": "20260103-001", "exit_scan_id": "20260103-002", "net_pnl": -0.01},
            {"enter_scan_id": "20260104-001", "exit_scan_id": "20260104-002", "net_pnl": -0.01},
        ],
        "b": [
            {"enter_scan_id": "20260101-001", "exit_scan_id": "20260101-002", "net_pnl": 0.03},
            {"enter_scan_id": "20260102-001", "exit_scan_id": "20260102-002", "net_pnl": -0.02},
            {"enter_scan_id": "20260103-001", "exit_scan_id": "20260103-002", "net_pnl": 0.03},
            {"enter_scan_id": "20260104-001", "exit_scan_id": "20260104-002", "net_pnl": 0.03},
        ],
    }

    out = m.walk_forward(
        candidates,
        trades_by_strategy,
        train_days=2,
        test_days=1,
        step_days=1,
        min_train_trades=1,
    )

    assert len(out["folds"]) == 2
    for fold in out["folds"]:
        assert fold["train_to_day"] < fold["test_from_day"]
        assert fold["test_metrics"]["n"] == 1


def test_param_hash_filter_does_not_count_excluded_nodes_as_gaps(tmp_path):
    import json

    m = load_module()
    snap_dir = tmp_path / "snapshots"
    snap_dir.mkdir()

    def write(sid: str, ph: str):
        (snap_dir / f"{sid}.json").write_text(
            json.dumps(
                {
                    "meta": {
                        "scan_id": sid,
                        "scan_timestamp_utc": sid,
                        "parameter_version": "param-v2.0.0-screener-y",
                        "param_hash": ph,
                    },
                    "long_pool": [],
                    "short_pool": [],
                }
            )
        )

    write("20260101-000", "old")
    write("20260101-001", "old")
    write("20260101-002", "keep")
    write("20260101-003", "keep")

    nodes, coverage = m.load_nodes(snap_dir, param_hashes={"keep"})

    assert [n["scan_id"] for n in nodes] == ["20260101-002", "20260101-003"]
    assert coverage["missing_scan_ids"] == []
    assert coverage["coverage_ratio"] == 1.0


def test_strategy_grid_has_unique_names():
    m = load_module()
    strategies = m.strategy_grid(20.0, "focused")
    names = [s.name for s in strategies]

    assert len(names) == len(set(names))


def test_objective_prefers_target_ranges_before_raw_payoff():
    m = load_module()
    target_hit = {
        "n": 100,
        "win_rate": 0.55,
        "payoff_ratio": 1.5,
        "sum_return": 1.1,
        "compounded_return": 0.4,
        "max_drawdown": 0.2,
    }
    high_payoff_but_misses_win_target = {
        "n": 100,
        "win_rate": 0.30,
        "payoff_ratio": 4.5,
        "sum_return": 1.2,
        "compounded_return": 0.6,
        "max_drawdown": 0.3,
    }

    assert m._objective(target_hit) > m._objective(high_payoff_but_misses_win_target)


def test_rank_leaderboard_passes_sum_return_into_target_aware_objective():
    m = load_module()
    target_hit = {
        "strategy": "target",
        "n": 100,
        "win_rate": 0.55,
        "payoff_ratio": 1.5,
        "sum_return": 1.1,
        "compounded_return": 0.4,
        "max_drawdown": 0.2,
    }
    raw_payoff_winner = {
        "strategy": "raw",
        "n": 100,
        "win_rate": 0.30,
        "payoff_ratio": 4.5,
        "sum_return": 1.2,
        "compounded_return": 0.6,
        "max_drawdown": 0.3,
    }

    ranked = m.rank_leaderboard([raw_payoff_winner, target_hit], min_trades=40)

    assert ranked[0]["strategy"] == "target"


def test_open_position_is_force_closed_at_end_of_sample():
    m = load_module()
    nodes = [
        row("20260101-000", 100.0, dmr=False),
        row("20260101-001", 101.0, dmr=True),
        row("20260101-002", 102.0, dmr=True),
        row("20260101-003", 103.0, dmr=True),
    ]

    trades = m.simulate(nodes, m.Strategy(entry_delay_nodes=1, max_hold_nodes=96))

    assert len(trades) == 1
    assert trades[0]["enter_scan_id"] == "20260101-002"
    assert trades[0]["exit_scan_id"] == "20260101-003"
    assert trades[0]["exit_reason"] == "END_OF_SAMPLE"


def test_portfolio_metrics_enforces_concurrency_and_labels_realized_equity():
    m = load_module()
    trades = [
        {
            "symbol": "AUSDT",
            "direction": "up",
            "enter_scan_id": "20260101-001",
            "exit_scan_id": "20260101-003",
            "net_pnl": 0.10,
        },
        {
            "symbol": "BUSDT",
            "direction": "up",
            "enter_scan_id": "20260101-002",
            "exit_scan_id": "20260101-004",
            "net_pnl": 0.50,
        },
    ]

    out = m.portfolio_metrics(trades, concurrency=1)

    assert out["opened"] == 1
    assert out["skipped_by_concurrency"] == 1
    assert math.isclose(out["realized_equity_return"], 0.10)
    assert out["valuation"] == "realized_at_exit_only"


def test_parser_defines_identity_filters_used_by_run():
    m = load_module()
    args = m.build_parser().parse_args([])

    assert args.identity_statuses == "MATCH"
    assert args.rule_revisions == "y-v2.0.0-r3"
    assert args.param_hashes == "pf1_fcea251fa94122fe"


def test_walk_forward_candidates_are_not_selected_from_full_sample_ranking():
    m = load_module()
    candidates = [m.Strategy(name="a"), m.Strategy(name="b"), m.Strategy(name="c")]

    assert [x.name for x in m.select_walk_forward_candidates(candidates, 0)] == ["a", "b", "c"]
    assert [x.name for x in m.select_walk_forward_candidates(candidates, 2)] == ["a", "b"]


def test_internal_gap_cancels_pending_and_closes_open_position():
    m = load_module()
    nodes = [
        row("20260101-000", 100.0, dmr=False),
        row("20260101-001", 101.0, dmr=True),
        row("20260101-003", 95.0, dmr=True),
    ]

    assert m.simulate(nodes, m.Strategy(entry_delay_nodes=1, max_hold_nodes=96)) == []

    nodes = [
        row("20260101-000", 100.0, dmr=False),
        row("20260101-001", 101.0, dmr=True),
        row("20260101-002", 102.0, dmr=True),
        row("20260101-004", 95.0, dmr=True),
    ]
    trades = m.simulate(nodes, m.Strategy(entry_delay_nodes=1, max_hold_nodes=96))

    assert len(trades) == 1
    assert trades[0]["exit_reason"] == "GAP_BOUNDARY"
    assert trades[0]["exit_scan_id"] == "20260101-002"


def test_dedupe_equivalent_strategies_keeps_one_trade_stream():
    m = load_module()
    candidates = [m.Strategy(name="a"), m.Strategy(name="b"), m.Strategy(name="c")]
    same = [{"symbol": "A", "direction": "up", "enter_scan_id": "1", "exit_scan_id": "2", "net_pnl": 0.1}]
    different = [{"symbol": "B", "direction": "up", "enter_scan_id": "1", "exit_scan_id": "2", "net_pnl": 0.1}]

    unique, aliases = m.dedupe_equivalent_strategies(
        candidates,
        {"a": same, "b": same, "c": different},
    )

    assert [x.name for x in unique] == ["a", "c"]
    assert aliases == {"a": ["a", "b"], "c": ["c"]}


if __name__ == "__main__":
    # 本仓库用 stdlib 运行器（scripts/run-tests.sh 里 `python3 <file>`），没有 pytest。
    # 缺这个块 = 文件被当成空脚本执行，退出码 0，run-tests.sh 报 PASS
    # 却一个断言都没跑 —— 正是它掩盖了 CLI 缺 --identity-statuses 的缺陷。
    #
    # 少数用例用 pytest 的 tmp_path fixture，这里按签名注入一个临时目录，
    # 每个用例一份、跑完即删，行为与 pytest 一致。
    import inspect
    import shutil
    import tempfile
    from pathlib import Path as _P

    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        params = inspect.signature(fn).parameters
        if "tmp_path" in params:
            d = _P(tempfile.mkdtemp(prefix="dmropt-"))
            try:
                fn(tmp_path=d)
            finally:
                shutil.rmtree(d, ignore_errors=True)
        else:
            fn()
        print("ok", fn.__name__)
    print("all", len(tests))
