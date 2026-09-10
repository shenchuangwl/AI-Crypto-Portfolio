"""《复盘选币》改读 final_zone —— 只在 00:00 UTC 边界之后生效，历史不重写。

裁决（用户 2026-09-04）：
  1. 账本向板面看齐：分区谓词读 ``final_zone``（板面显示的就是它），
     而不是 ``effective_zone``（主导层谓词之前的层 B）。
  2. 只从某个 00:00 UTC 边界起用新口径，**不重建历史账本**。

背景：``final_zone`` 由 da8b4d7 引入，但该提交没同步改 review_replay 的谓词，
账本一直读 effective_zone。两天窗口实测 0.90% 的行两者不同，差异恰好落在确认区与
DMR 的进出边界 —— 也就是复盘胜率的口径。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "services" / "coin-selection" / "src"))

from coin_selection.review_replay import (  # noqa: E402
    FINAL_ZONE_FROM_SCAN_ID,
    in_zone,
)

BEFORE = "20260904-095"      # 边界前最后一个节点
AT = FINAL_ZONE_FROM_SCAN_ID  # 边界那一刻
AFTER = "20260905-004"       # 边界后

# 一行被 P2 谓词从 CONFIRMED 软化到 QUALIFIED：层 B 说确认，层 C（板面）说符合。
SOFTENED = {
    "symbol": "AAAUSDT",
    "effective_zone": "CONFIRMED",
    "final_zone": "QUALIFIED",
    "dmr_selected": False,
}


def test_before_the_boundary_history_is_unchanged():
    """边界之前必须仍读 effective_zone —— 已验收的历史数字逐字节不变。"""
    assert in_zone(SOFTENED, "CONFIRMED", scan_id=BEFORE) is True
    assert in_zone(SOFTENED, "QUALIFIED", scan_id=BEFORE) is False


def test_from_the_boundary_the_ledger_follows_the_board():
    """边界当刻起改读 final_zone —— 板面显示什么，账本就统计什么。"""
    assert in_zone(SOFTENED, "CONFIRMED", scan_id=AT) is False
    assert in_zone(SOFTENED, "QUALIFIED", scan_id=AT) is True
    assert in_zone(SOFTENED, "CONFIRMED", scan_id=AFTER) is False
    assert in_zone(SOFTENED, "QUALIFIED", scan_id=AFTER) is True


def test_no_scan_id_keeps_the_legacy_reading():
    """不传 scan_id（其它调用方 / 老测试）保持旧行为，绝不隐式切换口径。"""
    assert in_zone(SOFTENED, "CONFIRMED") is True
    assert in_zone(SOFTENED, "QUALIFIED") is False


def test_dmr_always_reads_the_flag_on_both_sides_of_the_boundary():
    """DMR 是 CONFIRMED 的派生精选，两侧都只认 dmr_selected（文档A §5）。"""
    row = {"dmr_selected": True, "effective_zone": "CONFIRMED", "final_zone": "QUALIFIED"}
    for sid in (BEFORE, AT, AFTER, None):
        assert in_zone(row, "DMR", scan_id=sid) is True, sid


def test_rows_where_the_two_layers_agree_are_unaffected():
    """两层一致的行（绝大多数）跨边界行为不变。"""
    row = {"effective_zone": "WATCH", "final_zone": "WATCH", "dmr_selected": False}
    for sid in (BEFORE, AT, AFTER, None):
        assert in_zone(row, "WATCH", scan_id=sid) is True, sid
        assert in_zone(row, "CONFIRMED", scan_id=sid) is False, sid


def test_boundary_is_a_midnight_utc_node():
    """规则只在 00:00 UTC 周期边界生效（文档B §3.3）：节点号必须是 000。"""
    assert FINAL_ZONE_FROM_SCAN_ID.endswith("-000"), FINAL_ZONE_FROM_SCAN_ID


def test_falls_back_to_effective_when_final_zone_is_absent():
    """边界之后但快照没有 final_zone（主导层关闭 / 主榜）⇒ 回退 effective_zone。"""
    row = {"effective_zone": "WATCH", "dmr_selected": False}
    assert in_zone(row, "WATCH", scan_id=AFTER) is True


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print("ok", fn.__name__)
    print("all", len(tests))
