#!/usr/bin/env python3
"""复盘账本验收闸 — C1..C14.

    python3 scripts/verify_review_ledger.py
    python3 scripts/verify_review_ledger.py --spot 5              # 额外抽检 N 笔回快照手算
    python3 scripts/verify_review_ledger.py --board y --check-param-hash

C14（文档B §3.1 规则 2/3）：账本行的 ``param_hash`` 必须与其 ``enter_scan_id``
对应快照的 ``param_hash`` 一致；同时检查账本里是否混着多套指纹 —— 混参的账本
**不可比**，《复盘选币》必须显示「参数已变更，历史不可比」而不是混着展示。

Reads the materialised ledger only (plus a few raw board snapshots for the
spot check). Never writes. Exit code 1 on any HARD failure; WARN never fails.
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "coin-selection" / "src"))

from coin_selection.review_ledger import ReviewLedger, default_ledger_path, summarize_trades  # noqa: E402
from coin_selection.review_pnl import realized_pnl  # noqa: E402
from coin_selection.review_replay import in_zone, index_rows, stay_px, print_px  # noqa: E402

hard = 0
warn = 0


def check(name: str, ok: bool, extra: str = "", level: str = "HARD") -> None:
    global hard, warn
    if ok:
        print(f"[PASS] {name:<38} {extra}")
        return
    if level == "WARN":
        warn += 1
        print(f"[WARN] {name:<38} {extra}")
    else:
        hard += 1
        print(f"[FAIL] {name:<38} {extra}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Review ledger acceptance gate")
    ap.add_argument("--data-dir", default=None)
    ap.add_argument("--board", default=None, help="main | y")
    ap.add_argument("--spot", type=int, default=6, help="rows to recompute from raw snapshots")
    ap.add_argument("--seed", type=int, default=20260824)
    ap.add_argument(
        "--check-param-hash",
        action="store_true",
        help="C14：账本 param_hash 与快照一致性 + 混参检测（文档B §3.1）",
    )
    args = ap.parse_args()

    if args.board:
        from coin_selection.board_variants import get_variant  # noqa: E402

        v = get_variant(args.board)
        if v is None:
            print(f"[FAIL] unknown board: {args.board}")
            return 1
        data_dir = v.data_path(ROOT)
    else:
        data_dir = Path(args.data_dir or (ROOT / "data" / "coin-selection"))
    db_path = default_ledger_path(data_dir)
    if not db_path.is_file():
        print(f"[FAIL] ledger missing: {db_path}")
        print("       run: python3 scripts/build_review_ledger.py --reset")
        return 1

    db = sqlite3.connect(str(db_path))
    db.row_factory = sqlite3.Row
    n_all = db.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    print(f"ledger {db_path}  rows={n_all}\n")

    # ---- C1 层级不变量：每条 DMR 停留必须被某条确认区停留包住 ----
    conf: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for r in db.execute(
        "SELECT symbol,direction,enter_time_utc,exit_time_utc FROM trades WHERE zone='CONFIRMED'"
    ):
        conf.setdefault((r["symbol"], r["direction"]), []).append(
            (r["enter_time_utc"], r["exit_time_utc"] or "9999")
        )
    bad_c1: list[str] = []
    n_dmr = 0
    for r in db.execute(
        "SELECT trade_id,symbol,direction,enter_time_utc,exit_time_utc FROM trades WHERE zone='DMR'"
    ):
        n_dmr += 1
        a, b = r["enter_time_utc"], r["exit_time_utc"] or "9999"
        if not any(c <= a and b <= d for c, d in conf.get((r["symbol"], r["direction"]), [])):
            bad_c1.append(r["trade_id"])
    check("C1 DMR 停留 ⊆ 确认区停留", not bad_c1, f"{n_dmr - len(bad_c1)}/{n_dmr} {bad_c1[:3]}")

    # ---- C2 交叉校验：确认区入场次数 vs .full.json 的 to_state=CONFIRMED（WARN） ----
    full_conf = 0
    fulls = sorted((data_dir / "snapshots").glob("*.full.json"))
    for p in fulls:
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        for t in doc.get("transitions") or []:
            if t.get("to_state") == "CONFIRMED":
                full_conf += 1
    led_conf = db.execute("SELECT COUNT(*) FROM trades WHERE zone='CONFIRMED'").fetchone()[0]
    if fulls:
        delta = abs(full_conf - led_conf)
        check(
            "C2 确认区进场 ≈ transitions",
            delta <= max(20, int(0.15 * max(full_conf, 1))),
            f"ledger={led_conf} transitions={full_conf} Δ={delta}",
            level="WARN",
        )

    # ---- C3/C4 盈亏重算与符号映射 ----
    bad_c3: list[str] = []
    bad_c4: list[str] = []
    for r in db.execute(
        "SELECT trade_id,direction,enter_price,exit_price,pnl_pct,pnl_sign "
        "FROM trades WHERE status='CLOSED'"
    ):
        pct, sign = realized_pnl(r["direction"], r["enter_price"], r["exit_price"])
        if (pct is None) != (r["pnl_pct"] is None):
            bad_c3.append(r["trade_id"])
        elif pct is not None and abs(pct - r["pnl_pct"]) > 1e-9:
            bad_c3.append(r["trade_id"])
        if r["pnl_pct"] is not None:
            want = 1 if r["pnl_pct"] > 0 else (-1 if r["pnl_pct"] < 0 else 0)
            if r["pnl_sign"] != want:
                bad_c4.append(r["trade_id"])
    check("C3 pnl_pct 与公式重算一致 <1e-9", not bad_c3, f"{bad_c3[:3]}")
    check("C4 pnl_sign == sign(pnl_pct)", not bad_c4, f"{bad_c4[:3]}")

    # ---- C5 汇总与逐条一致 ----
    rows = [
        {k: r[k] for k in r.keys()}
        for r in db.execute(
            "SELECT status,pnl_pct,pnl_sign,direction,zone,canonical_asset_id,symbol,"
            "dwell_minutes,dwell_nodes,flags,parameter_version FROM trades "
            "WHERE zone IN ('DMR','CONFIRMED')"
        )
    ]
    for r in rows:
        r["flags"] = json.loads(r["flags"] or "[]")
    s = summarize_trades(rows)
    manual = [float(r["pnl_pct"]) for r in rows if r["status"] == "CLOSED" and r["pnl_pct"] is not None]
    check(
        "C5 合计 == Σ 逐条 pnl_pct",
        abs(s["sum_pct"] - sum(manual)) < 1e-9,
        f"sum={s['sum_pct']:.6f} usable={s['usable']}",
    )
    check(
        "C5 胜率 == 盈利 / 有效",
        s["win_rate"] is None or abs(s["win_rate"] - s["win"] / max(s["usable"], 1)) < 1e-12,
        f"win={s['win']} usable={s['usable']}",
    )

    # ---- C6 未平仓隔离 ----
    open_with_pnl = db.execute(
        "SELECT COUNT(*) FROM trades WHERE status='OPEN' AND (pnl_pct IS NOT NULL OR pnl_sign IS NOT NULL)"
    ).fetchone()[0]
    check("C6 OPEN 不带已实现盈亏", open_with_pnl == 0, f"violations={open_with_pnl}")

    # ---- C7 计数口径：笔数 ≥ 唯一币，且可执行区确实 >1× ----
    bad_c7: list[str] = []
    ratios: list[str] = []
    for z in ("DMR", "CONFIRMED", "QUALIFIED", "WATCH", "ELIMINATED"):
        r = db.execute(
            "SELECT COUNT(*) n, COUNT(DISTINCT canonical_asset_id) c FROM trades "
            "WHERE zone=? AND status='CLOSED'",
            (z,),
        ).fetchone()
        if r["c"] and r["n"] < r["c"]:
            bad_c7.append(z)
        if r["c"]:
            ratios.append(f"{z} {r['n']}/{r['c']}={r['n'] / r['c']:.2f}×")
    check("C7 笔数 ≥ 唯一币（不去重）", not bad_c7, " · ".join(ratios))

    # ---- C7b 组合分区相加不去重 ----
    led = ReviewLedger(db_path, readonly=True)
    try:
        s_dmr = led.summarize(zones=["DMR"], start_utc=None, end_utc=None)
        s_cnf = led.summarize(zones=["CONFIRMED"], start_utc=None, end_utc=None)
        s_both = led.summarize(zones=["DMR", "CONFIRMED"], start_utc=None, end_utc=None)
        check(
            "C7b DMR+确认 笔数相加",
            s_both["trades"] == s_dmr["trades"] + s_cnf["trades"],
            f"{s_dmr['trades']}+{s_cnf['trades']}={s_both['trades']}",
        )
        check(
            "C7b 唯一币跨区去重",
            s_both["unique_coins"] <= s_dmr["unique_coins"] + s_cnf["unique_coins"],
            f"{s_both['unique_coins']} ≤ {s_dmr['unique_coins']}+{s_cnf['unique_coins']}",
        )
    finally:
        led.close()

    # ---- C8 幂等：重放最新节点不改变账本 ----
    wm = db.execute("SELECT json FROM watermarks WHERE k='scan'").fetchone()
    wm_sid = json.loads(wm["json"]).get("scan_id") if wm else None
    if wm_sid:
        before = db.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        from coin_selection.review_ledger import ingest_scan

        out = ingest_scan(data_dir, wm_sid)
        after = sqlite3.connect(str(db_path)).execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        check(
            "C8 重复 ingest 幂等",
            out.get("status") == "duplicate" and before == after,
            f"{out.get('status')} rows {before}→{after}",
        )

    # ---- C9 无时间穿越 ----
    bad_c9 = db.execute(
        "SELECT COUNT(*) FROM trades WHERE exit_time_utc IS NOT NULL AND exit_time_utc<=enter_time_utc"
    ).fetchone()[0]
    check("C9 退出时间 > 入选时间", bad_c9 == 0, f"violations={bad_c9}")

    # ---- C10 DMR 停留价来源 ----
    bad_c10 = db.execute(
        "SELECT COUNT(*) FROM trades WHERE zone='DMR' AND enter_price_source='state_enter_price'"
    ).fetchone()[0]
    check("C10 DMR 不用 state_enter_price", bad_c10 == 0, f"violations={bad_c10}")
    bad_c10b = db.execute(
        "SELECT COUNT(*) FROM trades WHERE zone IN ('CONFIRMED','QUALIFIED','WATCH') "
        "AND enter_price IS NOT NULL AND enter_price_source NOT IN ('state_enter_price','last_price','ref_price')"
    ).fetchone()[0]
    check("C10b 停留价来源在白名单内", bad_c10b == 0, f"violations={bad_c10b}")

    # ---- C11 数据质量：缺价比例 ----
    q = db.execute(
        "SELECT COUNT(*) n, SUM(CASE WHEN pnl_pct IS NULL THEN 1 ELSE 0 END) m "
        "FROM trades WHERE status='CLOSED'"
    ).fetchone()
    ratio = 1.0 - (q["m"] or 0) / max(q["n"], 1)
    check("C11 有效盈亏比 ≥95%", ratio >= 0.95, f"{ratio:.4%} missing={q['m']}/{q['n']}", level="WARN")

    # ---- C13 节点连续性：快照序号不得有洞 ----
    from coin_selection.review_replay import missing_nodes_between

    sids = sorted(
        p.name[:-5]
        for p in (data_dir / "snapshots").glob("*.json")
        if not p.name.endswith(".full.json")
    )
    holes = []
    # 节点前驱表：C12 回算 `prev_node` 定价的退出要用（见下）。
    prev_node_of = {b: a for a, b in zip(sids, sids[1:])}
    for a, b in zip(sids, sids[1:]):
        miss = missing_nodes_between(a, b)
        if miss:
            holes.append((a, b, miss))
    total_missing = sum(h[2] for h in holes)
    check(
        "C13 扫描节点序号无缺口",
        not holes,
        f"{len(sids)} 个节点，缺 {total_missing} 个："
        + " · ".join(f"{a}→{b}(缺{n})" for a, b, n in holes[:4]),
        level="WARN",
    )

    # ---- C12 抽检：回原始快照手算 ----
    if args.spot > 0:
        random.seed(args.seed)
        picked = db.execute(
            "SELECT * FROM trades WHERE status='CLOSED' AND zone IN ('DMR','CONFIRMED') "
            "ORDER BY RANDOM() LIMIT ?",
            (int(args.spot),),
        ).fetchall()
        bad_spot: list[str] = []
        for r in picked:
            ep = data_dir / "snapshots" / f"{r['enter_scan_id']}.json"
            xp = data_dir / "snapshots" / f"{r['exit_scan_id']}.json"
            if not ep.is_file() or not xp.is_file():
                continue
            e_idx = index_rows(json.loads(ep.read_text(encoding="utf-8")))
            x_idx = index_rows(json.loads(xp.read_text(encoding="utf-8")))
            e_row = e_idx.get((r["symbol"], r["direction"]))
            x_row = x_idx.get((r["symbol"], r["direction"]))
            if e_row is None:
                bad_spot.append(f"{r['trade_id']}:no-enter-row")
                continue
            want_stay, want_src = stay_px(e_row, r["zone"])
            want_exit, _ = print_px(x_row)
            if want_exit is None and r["exit_price_source"] == "prev_node":
                # 币在退出节点整个从板面消失了（board 只登载分区成员）。账本按
                # `close_record` 的约定用**上一个节点**的最后一次打印定价，
                # 抽检也必须按同一口径回算，否则这一支永远对不上。
                #
                # 「选币榜Y」的 24h 周期重置会把全部分区清空 —— 那一节点上所有
                # 还在分区里的币都走这一支，占它已平仓的约 23%。
                prev_sid = prev_node_of.get(r["exit_scan_id"])
                pp = data_dir / "snapshots" / f"{prev_sid}.json" if prev_sid else None
                if pp is not None and pp.is_file():
                    prev_idx = index_rows(json.loads(pp.read_text(encoding="utf-8")))
                    want_exit, _ = print_px(prev_idx.get((r["symbol"], r["direction"])))
            want_pct, _ = realized_pnl(r["direction"], want_stay, want_exit)
            ok = (
                (want_stay is None or abs((r["enter_price"] or 0) - want_stay) < 1e-12)
                and (want_exit is None or abs((r["exit_price"] or 0) - want_exit) < 1e-12)
                and (
                    (want_pct is None and r["pnl_pct"] is None)
                    or (want_pct is not None and r["pnl_pct"] is not None and abs(want_pct - r["pnl_pct"]) < 1e-9)
                )
            )
            # The entry node must actually be in-zone, and the exit node must not.
            if e_row is not None and not in_zone(e_row, r["zone"]) and r["zone"] != "DMR":
                ok = False
            if x_row is not None and in_zone(x_row, r["zone"]):
                ok = False
            if not ok:
                bad_spot.append(
                    f"{r['trade_id']} stay {r['enter_price']}≠{want_stay} exit {r['exit_price']}≠{want_exit}"
                )
            else:
                tag = " [周期重置强平]" if "CYCLE_RESET" in (r["flags"] or "") else ""
                print(
                    f"       spot {r['trade_id']:<46} {want_src or '-':<17} "
                    f"{r['enter_price']}→{r['exit_price']}  {(r['pnl_pct'] or 0) * 100:+.3f}%{tag}"
                )
        check("C12 抽检回快照手算一致", not bad_spot, "; ".join(bad_spot[:2]))

    # ---- C14 参数指纹一致性（文档B §3.1 规则 2/3）----
    if args.check_param_hash:
        cols = {r[1] for r in db.execute("PRAGMA table_info(trades)")}
        if "param_hash" not in cols:
            check(
                "C14 账本已迁移出 param_hash 列",
                False,
                "先跑 python3 scripts/migrate_ledger_v2.py --all",
            )
        else:
            rows = db.execute(
                "SELECT param_hash, COUNT(*) n, MIN(enter_scan_id) a, MAX(enter_scan_id) b "
                "FROM trades GROUP BY param_hash ORDER BY n DESC"
            ).fetchall()
            hashes = [(r["param_hash"], r["n"], r["a"], r["b"]) for r in rows]
            n_null = sum(n for h, n, _a, _b in hashes if not h)
            distinct = [h for h, _n, _a, _b in hashes if h]
            check(
                "C14a 账本已带 param_hash 列",
                True,
                f"{len(hashes)} 组：" + " · ".join(
                    f"{(h or 'NULL')}={n}" for h, n, _a, _b in hashes[:4]
                ),
            )
            # 旧行 NULL 是**预期**的（阶段 0 之前的快照根本没有指纹，回填出来的是假的）
            check(
                "C14b 阶段0之前的旧行保持 NULL（不回填）",
                True,
                f"NULL={n_null} 行",
                level="WARN" if n_null else "HARD",
            )
            check(
                "C14c 账本未混入多套参数指纹",
                len(distinct) <= 1,
                f"distinct={distinct[:4]}"
                + ("  → 《复盘选币》必须显示「参数已变更，历史不可比」" if len(distinct) > 1 else ""),
                level="WARN",
            )
            # 规则 2：逐行与其入场快照的指纹比对（抽样，避免读 1600 份快照）
            snap_dir = data_dir / "snapshots"
            sample = db.execute(
                "SELECT trade_id, enter_scan_id, param_hash FROM trades "
                "WHERE param_hash IS NOT NULL ORDER BY enter_scan_id DESC LIMIT 200"
            ).fetchall()
            bad_c14 = []
            for r in sample:
                sp = snap_dir / f"{r['enter_scan_id']}.json"
                if not sp.is_file():
                    continue
                try:
                    ph = (json.loads(sp.read_text(encoding="utf-8")).get("meta") or {}).get(
                        "param_hash"
                    )
                except Exception:  # noqa: BLE001
                    continue
                if ph and ph != r["param_hash"]:
                    bad_c14.append(r["trade_id"])
            check(
                "C14d 行 param_hash == 入场快照 param_hash",
                not bad_c14,
                f"抽样 {len(sample)} 行，不一致 {len(bad_c14)} {bad_c14[:3]}",
            )
            # 规则 3：latest.json 的指纹 vs 账本最新一批行
            latest = data_dir / "latest.json"
            if latest.is_file():
                try:
                    cur = (json.loads(latest.read_text(encoding="utf-8")).get("meta") or {}).get(
                        "param_hash"
                    )
                except Exception:  # noqa: BLE001
                    cur = None
                newest = db.execute(
                    "SELECT param_hash FROM trades ORDER BY enter_scan_id DESC LIMIT 1"
                ).fetchone()
                newest_h = newest["param_hash"] if newest else None
                check(
                    "C14e latest.json 指纹 == 账本最新行指纹",
                    (cur is None and newest_h is None) or cur == newest_h,
                    f"latest={cur} ledger_newest={newest_h}"
                    + ("（两者皆无指纹：快照产于阶段0之前）" if cur is None and newest_h is None else ""),
                    level="WARN",
                )

    print(f"\nhard failures: {hard}   warnings: {warn}")
    return 1 if hard else 0


if __name__ == "__main__":
    raise SystemExit(main())
