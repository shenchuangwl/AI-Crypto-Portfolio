#!/usr/bin/env python3
"""选币榜X v1.3.0 验收：与 main v1.4.0 复刻、shadow 四列、独立复盘闭环。
只读真实产物，不取行情不写三板面；后续唯一调参入口仍是 X overrides 同步 YAML。
输出可保存进完成报告，所有计数均重算；缺数据或不一致时非零退出，禁止伪造 PASS。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services/coin-selection/src"))
sys.path.insert(0, str(ROOT / "services/dmr-adapter/src"))
from coin_selection.board_variants import load_variants, param_fingerprint, variant_settings, variant_state_config
from coin_selection.scan import SelectionSettings
from coin_selection.review_replay import in_zone
from dmr_adapter.adapter import DEFAULT_PARAM_WHITELIST, parameter_whitelist


def load(path: Path) -> dict:
    """只读指定快照：X 与 main 原产物保持不动。"""
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    variants = load_variants(refresh=True)
    assert [v.key for v in variants] == ["main", "x", "y"]
    print("== 三板面注册/隔离 ==")
    for v in variants:
        print(v.key, v.parameter_version, v.api_prefix, v.web_route, v.data_dir,
              v.dmr_inbox, v.dmr_executable, v.cycle.enabled, str(v.ledger_path(ROOT)))
    for field in ("key", "parameter_version", "api_prefix", "web_route", "data_dir", "dmr_inbox"):
        assert len({getattr(v, field) for v in variants}) == 3, field
    x = next(v for v in variants if v.key == "x")
    main_v = next(v for v in variants if v.key == "main")
    # —— 按 X 实际生效的形态选择验收口径 ——
    #
    # 原来这里把「X 逐字段等于 main」写死。X 一旦采用历史 v1.3 四区语义，四区与
    # DMR 必然与 main 不同，那条断言会红；但正确的修法不是放宽成 contains ——
    # 那会把「零漂移」这条真正要保住的护栏一起拆掉。
    #
    #   CLONE    复刻期：保留全部逐字段等于 main 的强断言（下方原样不动）
    #   ADAPTED  适配期：四区改由 v1.3 裁决，因此不再与 main 比对；改为验证
    #            分层不变量 —— DMR ⊆ CONFIRMED、每个 DMR 成员的方向 ceiling==DMR、
    #            shadow 四列仍不参与分区/排序。
    #
    # 两种形态下**都不放宽**的：执行隔离、param_hash 与账本同源、shadow 模式、
    # 独立目录、cycle 关闭。
    assert not x.cycle.enabled and not x.dmr_executable
    MODE = (
        "ADAPTED"
        if x.state_overrides.get("selection_semantics") == "dual-path-v1.3"
        else "CLONE"
    )
    # —— 已获批形态从 candidates/ 的 overrides 片段**动态派生** ——
    #
    # 这里原先是逐版硬编码的白名单，每上线一个 revision 就要手改一次，
    # 已经因此红过三次（r2 / r3 / r4）。真正的事实源是 candidates/ 里那些
    # 经切换器发布过的 overrides 片段 —— 从它们派生，新增 revision 时零改动。
    # 仍然是**白名单**：不在集合内的形态照样判红，不放宽。
    approved = [{}] if MODE == "CLONE" else []
    approved_settings = [{"mcap_zone_mode": "shadow"}] if MODE == "CLONE" else []
    for frag in sorted((ROOT / "packages/config/candidates").glob("board-variants.x-*.json")):
        try:
            ov = json.loads(frag.read_text(encoding="utf-8"))["overrides"]
        except Exception:
            continue
        approved.append(ov.get("state_config") or {})
        approved_settings.append(ov.get("settings") or {})
    assert x.state_overrides in approved, (x.state_overrides, "不在已获批形态集合内")
    assert x.settings_overrides in approved_settings, (x.settings_overrides, "同上")
    print("== X 形态 ==", MODE)
    a = load(main_v.data_path(ROOT)/"latest.json")
    b = load(x.data_path(ROOT)/"latest.json")
    if MODE == "ADAPTED":
        # 适配期不强求与 main 同节点。
        #
        # 克隆期「同一节点逐字段复刻」是核心断言，所以要求两边 scan_id 相等。
        # 适配期 X 是独立裁决的板面，而且历史重建产出的 staging 数据天然落后于
        # 仍在推进的 main —— 拿「必须同节点」去卡它，会把一份完全正确的重建数据
        # 判成失败。改为只要求 X 的节点是 main 真实存在过的节点（不许凭空造节点），
        # 落后多少个节点如实打印出来。
        sid = b["meta"]["scan_id"]
        src = main_v.data_path(ROOT)/"snapshots"/f"{sid}.full.json"
        assert src.is_file(), f"X 的节点 {sid} 在 main 快照里不存在"
        lag = a["meta"]["scan_id"] != sid
        print(f"== X 节点 == {sid}" + (f"（main 已推进到 {a['meta']['scan_id']}，落后属正常）" if lag else ""))
        return verify_adapted(x, a, b, sid)
    sid = a["meta"]["scan_id"]
    assert sid == b["meta"]["scan_id"], (sid,b["meta"]["scan_id"])
    print("== 同一节点复刻 ==", sid)
    for pool in ("long_pool", "short_pool"):
        aa = {r["symbol"]:r for r in a[pool]}
        bb = {r["symbol"]:r for r in b[pool]}
        same_symbols = set(aa) == set(bb)
        same_state_rank = {s:(r["state"],r["rank"]) for s,r in aa.items()} == {
            s:(r["state"],r["rank"]) for s,r in bb.items()}
        print(pool, "rows",len(aa),len(bb),"same_symbols",same_symbols,"same_state_and_rank",same_state_rank)
        assert same_symbols and same_state_rank
        # X v1.3.0 复刻的共同字段必须全部相等，不能只抽状态/排名而漏掉停留价或时间。
        # 新增 shadow 审计字段不与缺省 main 比键数；复盘/未来 overrides 演进边界仍独立。
        differences = [(s,k) for s in aa for k in aa[s] if aa[s][k] != bb[s].get(k)]
        print(pool, "all_common_fields_mismatch", len(differences))
        assert not differences, differences[:10]
        for s in aa:
            assert all(aa[s].get(k)==bb[s].get(k) for k in
                ("score_up","score_down","dmr_selected","qualified_path","confirmed_path")),s
            for zone in ("WATCH","QUALIFIED","CONFIRMED","DMR"):
                assert in_zone(aa[s],zone,scan_id=sid) == in_zone(bb[s],zone,scan_id=sid),(s,zone)
        print(pool,"scores_DMR_paths_review_zones_equal",True)
    print("transitions_equal", a["transitions"] == b["transitions"],
          "counts", len(a["transitions"]), len(b["transitions"]))
    assert a["transitions"] == b["transitions"]
    raw = load(main_v.data_path(ROOT)/"snapshots"/f"{sid}.full.json")["rows"]
    for d,pool in (("up","long_pool"),("down","short_pool")):
        src = {r["symbol"]:r.get(f"state_{d}") for r in raw}
        mismatch = [r["symbol"] for r in b[pool] if r["state"] != src.get(r["symbol"])]
        print(f"state_{d}","mismatch",len(mismatch)); assert not mismatch
    return verify_common(x, a, b, sid)


def verify_adapted(x, a: dict, b: dict, sid: str) -> int:
    """适配期口径：四区由 v1.3 自行裁决，DMR 由「确认 ∩ 216 方向 ceiling」派生。

    这里**不再**拿 X 与 main 比四区 —— 那正是本轮要让它们分叉的地方。改为验证
    只属于 X 自己的分层不变量，其中任何一条不成立都说明适配没落到实处。
    """
    from coin_selection.mcap_mapping import load as load_mapping
    mapping = load_mapping(strict=True)
    RANK216 = (x.settings_overrides.get("dmr_selection_mode")
               == "confirmed-basic-rank216-v1.3")
    print("== v1.3 适配期分层验收 ==", sid,
          "（216 做排序权重）" if RANK216 else "（216 做准入）")
    total_conf = total_dmr = 0
    for pool, direction in (("long_pool","up"), ("short_pool","down")):
        rows = b[pool]
        conf = {r["symbol"] for r in rows if r["state"] == "CONFIRMED"}
        dmr = {r["symbol"] for r in rows if r.get("dmr_selected")}
        # C. DMR ⊆ CONFIRMED —— DMR 是确认区的派生标志，不是新状态。
        assert dmr <= conf, sorted(dmr - conf)[:10]
        key = "long_ceiling" if direction == "up" else "short_ceiling"
        if RANK216:
            # —— rank216：216 已退出准入，ceiling==DMR **不再**是不变量 ——
            #
            # 那正是本模式要取消的东西（天花板做准入实测为反向指标）。改验它
            # 真正承诺的：全同向优先入选 —— 若 DMR 里出现了非全同向成员，
            # 则所有全同向的确认成员必须都已在 DMR 内（否则排序键没生效）。
            def _aligned(r):
                c = str(r.get("mcap_combo_code") or "")
                return len(c) == 3 and c[0] == c[1] == c[2]
            dmr_rows = [r for r in rows if r.get("dmr_selected")]
            if any(not _aligned(r) for r in dmr_rows):
                missed = [r["symbol"] for r in rows
                          if r.get("state") == "CONFIRMED" and _aligned(r)
                          and not r.get("dmr_selected")]
                assert not missed, ("全同向确认成员未进 DMR，而非全同向的进了 —— "
                                    f"排序键未生效: {missed[:10]}")
            # 缺映射不得进 DMR（排序键要用它，fail-closed 仍然成立）。
            nomap = [r["symbol"] for r in dmr_rows
                     if not mapping.by_combo.get(r.get("mcap_combo_code"))]
            assert not nomap, nomap[:10]
        else:
            bad = []
            for r in rows:
                if not r.get("dmr_selected"):
                    continue
                combo = mapping.by_combo.get(r.get("mcap_combo_code"))
                if not combo or combo[key] != "DMR":
                    bad.append((r["symbol"], r.get("mcap_combo_code")))
            # 每个 DMR 成员的**方向** ceiling 必须真的是 DMR。
            assert not bad, bad[:10]
        # shadow 下有效区只报真实 state，final_zone 不得被写。
        assert all(r.get("effective_zone") in (None, r["state"]) for r in rows)
        assert all(r.get("final_zone") is None for r in rows)
        excluded = len(conf) - len(dmr)
        # 摘要必须如实反映**当前模式实际断言了什么**。
        # 原先无论哪个分支都打印「方向ceiling全为DMR=True」，可 rank216 根本没有
        # 这条断言（216 已退出准入）—— 在验收工具里，打印一条没验过的结论
        # 比不打印更糟：它会让人以为验过了。
        if RANK216:
            print(f"{pool} CONFIRMED={len(conf)} DMR={len(dmr)} "
                  f"未入选={excluded}（Top-K 截断，非 216 排除） "
                  f"DMR⊆CONFIRMED=True 全同向优先已生效=True")
        else:
            print(f"{pool} CONFIRMED={len(conf)} DMR={len(dmr)} 216排除={excluded} "
                  f"DMR⊆CONFIRMED=True 方向ceiling全为DMR=True")
        total_conf += len(conf); total_dmr += len(dmr)
    # 收窄性：DMR 为空只能证明包含，不足以证明严格收窄。这里要求真实节点上
    # 确实存在被排除的确认成员；若某节点恰好全部合格，用 --allow-no-exclusion 放行。
    if total_conf and total_dmr == total_conf and "--allow-no-exclusion" not in sys.argv:
        raise AssertionError(
            f"该节点 DMR 未排除任何确认成员（CONFIRMED={total_conf}），"
            "收窄性未获证明；确认属实可加 --allow-no-exclusion")
    print("严格收窄", f"CONFIRMED={total_conf} DMR={total_dmr} 排除={total_conf-total_dmr}")
    return verify_common(x, a, b, sid)


def verify_common(x, a: dict, b: dict, sid: str) -> int:
    """两种形态都必须成立的部分：shadow 四列、身份同源、账本、执行隔离。"""
    fields = ("mcap_combo_code","mcap_z10","mcap_resonance_k","mcap_ceiling_zone")
    rows = b["long_pool"]+b["short_pool"]
    print("four_columns_non_null",{k:sum(r.get(k) is not None for r in rows) for k in fields},"of",len(rows))
    # 缺 A–F 数据不伪造组合，特殊态 ceiling 允许 null；有有效组合的业务行必须四列齐全。
    valid = [r for r in rows if all(r.get(f"mcap_grade_{tf}") for tf in ("30m","2h","6h"))
             and r["state"] in ("WATCH","QUALIFIED","CONFIRMED","ELIMINATED")]
    assert valid and all(all(r.get(k) is not None for k in fields) for r in valid)
    print("four_columns_sample",{k:valid[0].get(k) for k in ("symbol",)+fields})
    assert b["meta"]["mcap_effective"]["mode"] == "shadow"
    ph = b["meta"]["param_hash"]
    expected = param_fingerprint(x,variant_settings(SelectionSettings(),x),variant_state_config(x))
    assert ph == expected
    print("param_hash", "main_snapshot",a["meta"].get("param_hash"),"x",ph,"expected",expected)
    db = sqlite3.connect(f"file:{x.ledger_path(ROOT)}?mode=ro",uri=True)
    hashes = db.execute("select param_hash,count(*) from trades group by param_hash").fetchall()
    pv = db.execute("select parameter_version,count(*) from trades group by parameter_version").fetchall()
    watermark = db.execute("select scan_id,ts from watermarks where k='scan'").fetchone()
    print("ledger_versions",pv,"ledger_hashes",hashes,"watermark",watermark)
    assert len(pv)==1 and pv[0][0]==x.parameter_version
    # —— 账本身份：克隆期是单段，适配期切换后必然是**两段** ——
    #
    # 原断言写死 len(hashes)==1，那是克隆期的不变量。切换那一刻起，账本里会同时
    # 存在旧克隆段与新 v1.3 段（实测 41314 + 7），这正是 param_hash 分段设计要的
    # 效果 —— 把它判成失败，等于要求「切换后账本必须假装没切过」。
    #
    # 但也不能放任：仍然强制 ① 只允许出现「已知的两个」身份，不许冒出第三个；
    # ② **最新一行**必须是当前身份，否则说明新配置没真正写进账本。
    # 历史重建（rebuild_screener_x_v13.py --promote）完成后会回到单段，
    # 那时下面这条依然成立。
    # 允许集合 = **所有已发布 revision 的 param_hash**，从 manifest 现取。
    # 原实现写死 {当前, 克隆} 两个 —— 第三次切换（r3）一上线立刻误报。
    # 已发布件才是「这个身份是否合法存在过」的权威来源，不该由脚本另记一份。
    from coin_selection.rule_manifest import list_published
    known = {m.param_hash for m in list_published("x") if m.param_hash} | {ph}
    seen = {h for h, _ in hashes}
    assert seen <= known, (f"账本出现未知参数身份: {seen - known}；"
                           f"已发布身份: {sorted(known)}")
    newest = db.execute(
        "select param_hash from trades order by enter_scan_id desc limit 1").fetchone()
    assert newest and newest[0] == ph, f"最新账本行身份 {newest} != 当前 {ph}"
    if len(hashes) > 1:
        print("segments", f"账本分 {len(hashes)} 段（切换后待历史重建）:",
              {h: n for h, n in hashes})
    assert watermark and watermark[0]==sid
    db.close()
    assert x.parameter_version not in DEFAULT_PARAM_WHITELIST and x.parameter_version not in parameter_whitelist()
    inbox = load(x.inbox_path(ROOT)/f"{sid}.candidates.json")
    assert inbox["dmr_executable"] is False and inbox["consumable_by_dmr"] is False
    assert all(m["dmr_executable"] is False and m["consumable_by_dmr"] is False for m in inbox["candidates"])
    print("execution_isolation",True,"DEFAULT_PARAM_WHITELIST",DEFAULT_PARAM_WHITELIST,
          "inbox_candidates",len(inbox["candidates"]))
    print("coingecko_credits_main",a["meta"].get("coingecko_credits"))
    print("coingecko_credits_x",b["meta"].get("coingecko_credits"))
    assert a["meta"]["coingecko_credits"]==b["meta"]["coingecko_credits"]
    print("coingecko_credits_equal",True)
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
