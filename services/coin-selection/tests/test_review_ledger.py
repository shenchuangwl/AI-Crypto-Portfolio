"""SQLite review ledger: incremental ingest + summary query."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from coin_selection.review_ledger import ReviewLedger, ingest_scan  # noqa: E402


def _row(symbol, direction, state, *, last, enter=None, dmr=False):
    return {
        "symbol": symbol,
        "direction": direction,
        "state": state,
        "canonical_asset_id": symbol.replace("USDT", "").lower(),
        "underlying_asset": symbol.replace("USDT", ""),
        "contract_multiplier": 1,
        "last_price": last,
        "ref_price": last,
        "state_enter_price": enter,
        "dmr_selected": dmr,
        "score_up": 70.0 if direction == "up" else 40.0,
        "score_down": 70.0 if direction == "down" else 40.0,
        "direction_confidence": 0.8,
        "liquidity_grade": "A",
        "reason_codes": ["G1_PASS"],
        "risk_flags": [],
    }


def _board(scan_id, ts, rows):
    return {
        "meta": {
            "scan_id": scan_id,
            "scan_timestamp_utc": ts,
            "parameter_version": "param-v1.4.0-staircase-confirm-dmr",
        },
        "long_pool": [r for r in rows if r["direction"] == "up"],
        "short_pool": [r for r in rows if r["direction"] == "down"],
    }


def test_incremental_matches_replay(tmp_path: Path | None = None):
    root = tmp_path or Path("/tmp/review_ledger_test")
    root.mkdir(parents=True, exist_ok=True)
    db = root / "ledger.sqlite"
    if db.exists():
        db.unlink()
    led = ReviewLedger(db)
    boards = [
        _board("s1", "2026-08-22T00:00:00Z", [_row("HYPEUSDT", "up", "WATCH", last=10.0, enter=10.0)]),
        _board("s2", "2026-08-22T00:15:00Z", [_row("HYPEUSDT", "up", "CONFIRMED", last=10.5, enter=10.5)]),
        _board("s3", "2026-08-22T00:30:00Z", [_row("HYPEUSDT", "up", "CONFIRMED", last=11.0, enter=10.5)]),
        _board("s4", "2026-08-22T00:45:00Z", [_row("HYPEUSDT", "up", "QUALIFIED", last=11.55, enter=11.55)]),
    ]
    for b in boards:
        led.ingest_board(b)
    trades = led.query_trades(zones=["CONFIRMED"], include_open=True)
    closed = [t for t in trades if t["status"] == "CLOSED"]
    assert len(closed) == 1
    assert closed[0]["trade_id"] == "HYPEUSDT|up|CONFIRMED|s2"
    assert closed[0]["pnl_sign"] == 1
    summary = led.summarize(
        zones=["CONFIRMED"],
        direction="both",
        start_utc="2026-08-22T00:00:00Z",
        end_utc="2026-08-22T00:45:00Z",
        attribution="exit",
    )
    assert summary["trades"] == 1
    assert summary["win"] == 1
    assert led.watermark()["scan_id"] == "s4"


def test_idempotent_reingest(tmp_path: Path | None = None):
    root = tmp_path or Path("/tmp/review_ledger_test2")
    root.mkdir(parents=True, exist_ok=True)
    db = root / "ledger.sqlite"
    if db.exists():
        db.unlink()
    led = ReviewLedger(db)
    b = _board("s1", "2026-08-22T00:00:00Z", [_row("AAAUSDT", "down", "CONFIRMED", last=1.0, enter=1.0)])
    led.ingest_board(b)
    led.ingest_board(b)
    trades = led.query_trades(zones=["CONFIRMED"], include_open=True)
    assert len(trades) == 1
    assert trades[0]["status"] == "OPEN"


def test_dmr_plus_confirmed_trades_add(tmp_path: Path | None = None):
    root = tmp_path or Path("/tmp/review_ledger_test3")
    root.mkdir(parents=True, exist_ok=True)
    db = root / "ledger.sqlite"
    if db.exists():
        db.unlink()
    led = ReviewLedger(db)
    boards = [
        _board("s1", "2026-08-22T00:00:00Z", [_row("BBBUSDT", "up", "CONFIRMED", last=100.0, enter=100.0, dmr=False)]),
        _board("s2", "2026-08-22T00:15:00Z", [_row("BBBUSDT", "up", "CONFIRMED", last=108.0, enter=100.0, dmr=True)]),
        _board("s3", "2026-08-22T00:30:00Z", [_row("BBBUSDT", "up", "CONFIRMED", last=110.0, enter=100.0, dmr=False)]),
    ]
    for b in boards:
        led.ingest_board(b)
    s_dmr = led.summarize(zones=["DMR"], direction="both", attribution="exit")
    s_conf = led.summarize(zones=["CONFIRMED"], direction="both", attribution="exit")
    s_both = led.summarize(zones=["DMR", "CONFIRMED"], direction="both", attribution="exit")
    assert s_dmr["trades"] == 1
    assert s_conf["trades"] == 0  # still OPEN
    assert s_both["trades"] == 1  # open excluded from trades
    both_all = led.query_trades(zones=["DMR", "CONFIRMED"], include_open=True)
    assert len(both_all) == 2


def _write_board(snaps: Path, board: dict) -> None:
    snaps.mkdir(parents=True, exist_ok=True)
    (snaps / f"{board['meta']['scan_id']}.json").write_text(
        json.dumps(board, ensure_ascii=False), encoding="utf-8"
    )


def test_ingest_scan_catches_up_skipped_nodes(tmp_path: Path | None = None):
    """A skipped node must be replayed, not jumped over.

    Diffing is adjacent-node based, so feeding n4 while the watermark sits at
    n1 would erase everything that entered and exited inside n2/n3.
    """
    root = tmp_path or Path("/tmp/review_ledger_test4")
    if root.exists():
        shutil.rmtree(root)
    (root / "snapshots").mkdir(parents=True, exist_ok=True)
    snaps = root / "snapshots"
    boards = [
        _board("20260824-001", "2026-08-24T00:00:00Z", [_row("CCCUSDT", "up", "WATCH", last=10.0, enter=10.0)]),
        _board("20260824-002", "2026-08-24T00:15:00Z", [_row("CCCUSDT", "up", "CONFIRMED", last=10.0, enter=10.0)]),
        _board("20260824-003", "2026-08-24T00:30:00Z", [_row("CCCUSDT", "up", "CONFIRMED", last=12.0, enter=10.0)]),
        _board("20260824-004", "2026-08-24T00:45:00Z", [_row("CCCUSDT", "up", "WATCH", last=11.0, enter=11.0)]),
    ]
    for b in boards:
        _write_board(snaps, b)

    ingest_scan(root, "20260824-001", inbox_dir=root / "inbox")
    # Nodes 002 and 003 are never handed over individually — the hook only fires
    # for 004 (loop restart, failed ingest, whatever).
    out = ingest_scan(root, "20260824-004", inbox_dir=root / "inbox")
    assert out["status"] == "ok", out
    assert out["catchup"] == 3, out

    led = ReviewLedger(root / "review" / "ledger.sqlite", readonly=True)
    try:
        closed = [t for t in led.query_trades(zones=["CONFIRMED"], include_open=True) if t["status"] == "CLOSED"]
        assert len(closed) == 1, closed
        t = closed[0]
        assert t["enter_scan_id"] == "20260824-002", t
        assert t["exit_scan_id"] == "20260824-004", t
        assert t["dwell_nodes"] == 2, t
        assert abs(t["pnl_pct"] - 0.1) < 1e-12, t
        assert led.watermark()["scan_id"] == "20260824-004"
    finally:
        led.close()


def test_ingest_scan_refuses_oversized_gap(tmp_path: Path | None = None):
    root = tmp_path or Path("/tmp/review_ledger_test5")
    if root.exists():
        shutil.rmtree(root)
    snaps = root / "snapshots"
    for i in range(1, 8):
        _write_board(
            snaps,
            _board(f"20260824-{i:03d}", f"2026-08-24T0{i}:00:00Z", [_row("DDDUSDT", "up", "WATCH", last=1.0, enter=1.0)]),
        )
    ingest_scan(root, "20260824-001", inbox_dir=root / "inbox")
    out = ingest_scan(root, "20260824-007", inbox_dir=root / "inbox", max_catchup=2)
    assert out["status"] == "gap_too_large", out
    led = ReviewLedger(root / "review" / "ledger.sqlite", readonly=True)
    try:
        # Watermark unmoved: staleness is reported, never papered over.
        assert led.watermark()["scan_id"] == "20260824-001"
    finally:
        led.close()


def test_param_version_change_flags_the_stay(tmp_path: Path | None = None):
    root = tmp_path or Path("/tmp/review_ledger_test6")
    root.mkdir(parents=True, exist_ok=True)
    db = root / "ledger.sqlite"
    if db.exists():
        db.unlink()
    led = ReviewLedger(db)
    b1 = _board("s1", "2026-08-22T00:00:00Z", [_row("EEEUSDT", "up", "CONFIRMED", last=10.0, enter=10.0)])
    b1["meta"]["parameter_version"] = "param-v1.3.0-dual-path-sticky"
    b2 = _board("s2", "2026-08-22T00:15:00Z", [_row("EEEUSDT", "up", "CONFIRMED", last=11.0, enter=10.0)])
    b3 = _board("s3", "2026-08-22T00:30:00Z", [_row("EEEUSDT", "up", "WATCH", last=11.0, enter=11.0)])
    for b in (b1, b2, b3):
        led.ingest_board(b)
    t = [x for x in led.query_trades(zones=["CONFIRMED"], include_open=True) if x["status"] == "CLOSED"][0]
    # Entry version is kept (that is the standard it was picked under)…
    assert t["parameter_version"] == "param-v1.3.0-dual-path-sticky", t
    # …and the stay is marked as straddling a switch.
    assert "PARAM_VERSION_CHANGED" in t["flags"], t
    led.close()


def test_sort_and_total_are_server_side(tmp_path: Path | None = None):
    root = tmp_path or Path("/tmp/review_ledger_test7")
    root.mkdir(parents=True, exist_ok=True)
    db = root / "ledger.sqlite"
    if db.exists():
        db.unlink()
    led = ReviewLedger(db)
    # Three coins whose worst loss is the LAST one by entry time, so a
    # page-local sort with limit=1 would return the wrong row.
    prices = {"AAAUSDT": (10.0, 10.5), "BBBUSDT": (10.0, 9.5), "CCCUSDT": (10.0, 8.0)}
    rows_in = []
    for sym, (a, _) in prices.items():
        rows_in.append(_row(sym, "up", "CONFIRMED", last=a, enter=a))
    led.ingest_board(_board("s1", "2026-08-22T00:00:00Z", rows_in))
    led.ingest_board(
        _board(
            "s2",
            "2026-08-22T00:15:00Z",
            [_row(sym, "up", "WATCH", last=b, enter=b) for sym, (_, b) in prices.items()],
        )
    )
    kw = dict(zones=["CONFIRMED"], attribution="exit")
    assert led.count_trades(**kw) == 3
    worst = led.query_trades(sort="pnl_pct", desc=False, limit=1, **kw)
    assert worst[0]["symbol"] == "CCCUSDT", worst
    best = led.query_trades(sort="pnl_pct", desc=True, limit=1, **kw)
    assert best[0]["symbol"] == "AAAUSDT", best
    # Unknown sort keys fall back to the documented default, never to SQL.
    dflt = led.query_trades(sort="; DROP TABLE trades; --", limit=3, **kw)
    assert len(dflt) == 3
    led.close()


def test_summary_extra_metrics(tmp_path: Path | None = None):
    root = tmp_path or Path("/tmp/review_ledger_test8")
    root.mkdir(parents=True, exist_ok=True)
    db = root / "ledger.sqlite"
    if db.exists():
        db.unlink()
    led = ReviewLedger(db)
    led.ingest_board(
        _board(
            "s1",
            "2026-08-22T00:00:00Z",
            [
                _row("AAAUSDT", "up", "CONFIRMED", last=10.0, enter=10.0),
                _row("BBBUSDT", "up", "CONFIRMED", last=10.0, enter=10.0),
            ],
        )
    )
    led.ingest_board(
        _board(
            "s2",
            "2026-08-22T00:15:00Z",
            [
                _row("AAAUSDT", "up", "WATCH", last=11.0, enter=11.0),  # +10%
                _row("BBBUSDT", "up", "WATCH", last=9.5, enter=9.5),  # −5%
            ],
        )
    )
    s = led.summarize(zones=["CONFIRMED"], start_utc="2026-08-22T00:00:00Z", end_utc="2026-08-22T00:15:00Z")
    assert s["trades"] == 2 and s["usable"] == 2
    assert abs(s["avg_win_pct"] - 0.10) < 1e-12, s["avg_win_pct"]
    assert abs(s["avg_loss_pct"] + 0.05) < 1e-12, s["avg_loss_pct"]
    assert abs(s["profit_factor"] - 2.0) < 1e-12, s["profit_factor"]
    assert s["max_drawdown_pct"] is not None and s["max_drawdown_pct"] <= 0
    assert s["data_quality"] == 1.0
    assert sum(b["count"] for b in s["dwell_histogram"]) == 2
    assert len(s["equity_curve"]) == 2
    assert "control_benchmark" in s and s["control_benchmark"]["trades"] == 0
    assert set(s["by_parameter_version"]) == {"param-v1.4.0-staircase-confirm-dmr"}
    led.close()


def test_only_flagged_filter(tmp_path: Path | None = None):
    root = tmp_path or Path("/tmp/review_ledger_test9")
    root.mkdir(parents=True, exist_ok=True)
    db = root / "ledger.sqlite"
    if db.exists():
        db.unlink()
    led = ReviewLedger(db)
    # First board = window left edge → every occupant gets TRUNCATED_ENTER.
    led.ingest_board(_board("s1", "2026-08-22T00:00:00Z", [_row("AAAUSDT", "up", "CONFIRMED", last=10.0, enter=10.0)]))
    led.ingest_board(
        _board(
            "s2",
            "2026-08-22T00:15:00Z",
            [
                _row("AAAUSDT", "up", "WATCH", last=11.0, enter=11.0),
                _row("BBBUSDT", "up", "CONFIRMED", last=20.0, enter=20.0),
            ],
        )
    )
    led.ingest_board(_board("s3", "2026-08-22T00:30:00Z", [_row("BBBUSDT", "up", "WATCH", last=21.0, enter=21.0)]))
    all_t = led.query_trades(zones=["CONFIRMED"], limit=50)
    flagged = led.query_trades(zones=["CONFIRMED"], only_flagged=True, limit=50)
    assert len(all_t) == 2, all_t
    assert len(flagged) == 1 and flagged[0]["symbol"] == "AAAUSDT", flagged
    assert "TRUNCATED_ENTER" in flagged[0]["flags"]
    led.close()


def test_out_of_order_board_cannot_resurrect_a_closed_trade(tmp_path: Path | None = None):
    """An older board must be refused, not replayed.

    `_upsert` is INSERT OR REPLACE keyed on trade_id; replaying an already
    consumed node re-emits its occupants as OPEN and would overwrite the CLOSED
    row — silently deleting a realized PnL.
    """
    root = tmp_path or Path("/tmp/review_ledger_test10")
    root.mkdir(parents=True, exist_ok=True)
    db = root / "ledger.sqlite"
    if db.exists():
        db.unlink()
    led = ReviewLedger(db)
    b1 = _board("s1", "2026-08-22T00:00:00Z", [_row("GGGUSDT", "up", "CONFIRMED", last=10.0, enter=10.0)])
    b2 = _board("s2", "2026-08-22T00:15:00Z", [_row("GGGUSDT", "up", "WATCH", last=12.0, enter=12.0)])
    led.ingest_board(b1)
    led.ingest_board(b2)
    before = [t for t in led.query_trades(zones=["CONFIRMED"], include_open=True)]
    assert len(before) == 1 and before[0]["status"] == "CLOSED" and before[0]["pnl_sign"] == 1

    out = led.ingest_board(b1)  # stale replay
    assert out["status"] == "duplicate", out
    after = [t for t in led.query_trades(zones=["CONFIRMED"], include_open=True)]
    assert after == before, after
    assert led.watermark()["scan_id"] == "s2"
    led.close()


def test_open_count_honours_dwell_and_flag_filters(tmp_path: Path | None = None):
    root = tmp_path or Path("/tmp/review_ledger_test11")
    root.mkdir(parents=True, exist_ok=True)
    db = root / "ledger.sqlite"
    if db.exists():
        db.unlink()
    led = ReviewLedger(db)
    led.ingest_board(_board("s1", "2026-08-22T00:00:00Z", [_row("HHHUSDT", "up", "CONFIRMED", last=10.0, enter=10.0)]))
    led.ingest_board(
        _board(
            "s2",
            "2026-08-22T00:15:00Z",
            [
                _row("HHHUSDT", "up", "CONFIRMED", last=10.0, enter=10.0),
                _row("IIIUSDT", "up", "CONFIRMED", last=20.0, enter=20.0),
            ],
        )
    )
    kw = dict(zones=["CONFIRMED"], start_utc="2026-08-22T00:00:00Z", end_utc="2026-08-22T00:15:00Z")
    assert led.summarize(**kw)["open_trades"] == 2
    # IIIUSDT has only 1 node of dwell, so a ≥2-node filter must drop it from
    # the open count too — not just from the grid.
    assert led.summarize(min_dwell_nodes=2, **kw)["open_trades"] == 1
    led.close()


def test_contained_is_closed_only_without_an_upper_bound(tmp_path: Path | None = None):
    root = tmp_path or Path("/tmp/review_ledger_test12")
    root.mkdir(parents=True, exist_ok=True)
    db = root / "ledger.sqlite"
    if db.exists():
        db.unlink()
    led = ReviewLedger(db)
    led.ingest_board(_board("s1", "2026-08-22T00:00:00Z", [_row("JJJUSDT", "up", "CONFIRMED", last=10.0, enter=10.0)]))
    led.ingest_board(
        _board(
            "s2",
            "2026-08-22T00:15:00Z",
            [
                _row("JJJUSDT", "up", "WATCH", last=11.0, enter=11.0),
                _row("KKKUSDT", "up", "CONFIRMED", last=20.0, enter=20.0),  # still open
            ],
        )
    )
    rows = led.query_trades(
        zones=["CONFIRMED"], attribution="contained", include_open=True,
        start_utc="2026-08-22T00:00:00Z", end_utc=None, limit=50,
    )
    assert [r["symbol"] for r in rows] == ["JJJUSDT"], rows
    led.close()


NEW_COLUMNS = ("param_hash", "combo_code", "combo_zone", "zone_ceiling", "z_score")


def test_t5_migration_is_idempotent_and_leaves_old_rows_null():
    """账本迁移 v2（文档B §10.2 / 测试 T5）。

    * 旧库（没有这 5 列）ALTER 之后，旧行全部 NULL —— **不回填**：``param_hash``
      在阶段 0 之前的快照里根本不存在，回填出来的是假的。
    * 幂等：重复打开同一个库不得报错、不得重复 ALTER。
    * 两条新索引存在。
    """
    import sqlite3
    import tempfile
    from pathlib import Path

    from coin_selection.review_ledger import ReviewLedger

    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "old.sqlite"
        # 手工造一个「阶段 0 之前」的旧库：只有基础列
        conn = sqlite3.connect(str(path))
        conn.execute(
            "CREATE TABLE trades ("
            "trade_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, canonical_asset_id TEXT NOT NULL,"
            "underlying_asset TEXT, contract_multiplier INTEGER, direction TEXT NOT NULL,"
            "zone TEXT NOT NULL, status TEXT NOT NULL, enter_scan_id TEXT NOT NULL,"
            "enter_time_utc TEXT NOT NULL, exit_scan_id TEXT, exit_time_utc TEXT,"
            "dwell_minutes REAL, dwell_nodes INTEGER, enter_price REAL, enter_price_source TEXT,"
            "exit_price REAL, exit_price_source TEXT, pnl_pct REAL, pnl_sign INTEGER,"
            "score REAL, score_opposite REAL, direction_confidence REAL, liquidity_grade TEXT,"
            "mcap_grade_30m TEXT, mcap_grade_2h TEXT, mcap_grade_6h TEXT,"
            "ret_1h REAL, ret_4h REAL, ret_24h REAL, ret_1w REAL, ret_1mo REAL,"
            "ret_since_anchor REAL, risk_flags TEXT, reason_codes TEXT,"
            "qualified_path TEXT, confirmed_path TEXT, parameter_version TEXT, flags TEXT)"
        )
        conn.execute(
            "INSERT INTO trades (trade_id,symbol,canonical_asset_id,direction,zone,status,"
            "enter_scan_id,enter_time_utc) VALUES "
            "('t1','AAAUSDT','aaa','up','CONFIRMED','CLOSED','20260815-001','2026-08-15T00:15:00Z')"
        )
        conn.commit()
        conn.close()

        led = ReviewLedger(path)
        cols = {r[1] for r in led._conn.execute("PRAGMA table_info(trades)")}
        for c in NEW_COLUMNS:
            assert c in cols, c
        idx = {r[0] for r in led._conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert "idx_trades_param_hash" in idx
        assert "idx_trades_combo" in idx
        row = led._conn.execute(
            "SELECT " + ",".join(NEW_COLUMNS) + " FROM trades WHERE trade_id='t1'"
        ).fetchone()
        assert all(v is None for v in row), dict(zip(NEW_COLUMNS, row))
        led.close()

        # 幂等：再开一次不炸、不重复 ALTER
        led2 = ReviewLedger(path)
        cols2 = {r[1] for r in led2._conn.execute("PRAGMA table_info(trades)")}
        assert cols2 == cols
        led2.close()


def test_t5_fresh_ledger_has_the_new_columns_from_create_table():
    import tempfile
    from pathlib import Path

    from coin_selection.review_ledger import TRADE_COLS, ReviewLedger

    with tempfile.TemporaryDirectory() as d:
        led = ReviewLedger(Path(d) / "new.sqlite")
        cols = [r[1] for r in led._conn.execute("PRAGMA table_info(trades)")]
        for c in NEW_COLUMNS:
            assert c in cols, c
        # TRADE_COLS 与表结构必须同步，否则 INSERT 的位置参数会错位
        assert set(TRADE_COLS) <= set(cols), set(TRADE_COLS) - set(cols)
        led.close()


def test_t5_new_columns_round_trip():
    import tempfile
    from pathlib import Path

    from coin_selection.review_ledger import ReviewLedger

    with tempfile.TemporaryDirectory() as d:
        led = ReviewLedger(Path(d) / "rt.sqlite")
        led._upsert(
            [
                {
                    "trade_id": "x1",
                    "symbol": "AAAUSDT",
                    "canonical_asset_id": "aaa",
                    "underlying_asset": "AAA",
                    "contract_multiplier": 1,
                    "direction": "up",
                    "zone": "CONFIRMED",
                    "status": "CLOSED",
                    "enter_scan_id": "20260901-001",
                    "enter_time_utc": "2026-09-01T00:15:00Z",
                    "param_hash": "pf1_deadbeefdeadbeef",
                    "combo_code": "ABC",
                    "combo_zone": "CONFIRMED",
                    "zone_ceiling": "QUALIFIED",
                    "z_score": 1.25,
                }
            ]
        )
        led._conn.commit()
        r = led._conn.execute(
            "SELECT " + ",".join(NEW_COLUMNS) + " FROM trades WHERE trade_id='x1'"
        ).fetchone()
        assert r[0] == "pf1_deadbeefdeadbeef"
        assert r[1] == "ABC" and r[2] == "CONFIRMED" and r[3] == "QUALIFIED"
        assert abs(r[4] - 1.25) < 1e-9
        led.close()


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print("ok", fn.__name__)
    print("all", len(tests))
