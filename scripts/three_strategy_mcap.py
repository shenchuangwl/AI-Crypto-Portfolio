#!/usr/bin/env python3
"""阶段 5 准入骨架：三策略 A/B/C 存在性检验（纸面，Y 仍不可执行）。

A = 现网 MC = P × N_now（等级 ≡ 价格均线，因 N 是公共因子）
B = 纯价格均线（丢掉供应）
C = point-in-time N_t —— CoinGecko 无历史 N 时标 unavailable，禁止用今天供应倒填。

本脚本只回答「流通市值状态有没有独立于价格均线的信息」，不填胜率/Sharpe。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan-id")
    args = ap.parse_args()
    report = {
        "scan_id": args.scan_id,
        "strategy_A": {
            "definition": "MC = P × N_now；等级 = grade_from_mas(MA of MC)",
            "status": "equivalent_to_price_ma_when_N_constant",
        },
        "strategy_B": {
            "definition": "grade_from_mas(MA of close) — drop supply factor",
            "status": "A_grades_must_equal_B_grades_on_same_closed_bars",
        },
        "strategy_C": {
            "definition": "point-in-time N_t per bar",
            "status": "unavailable",
            "reason": "CoinGecko has current circulating supply only; do not back-fill today's N",
        },
        "verdict": "C=unavailable; do not claim independent mcap-state alpha. Keep ENABLE_MCAP_ZONE=0 as the research default until a historical N series exists.",
        "win_rate": None,
        "payoff": None,
        "profit": None,
        "sharpe": None,
        "note": "Research filter bands 50–100% / 1–10 / 100–1000% are not a deliverable. Filling them here is a hard fail.",
    }
    out_dir = ROOT / "data" / "coin-selection-y" / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    name = f"{args.scan_id or 'latest'}.three_strategy.json"
    path = out_dir / name
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print("wrote", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
