#!/usr/bin/env python3
"""阶段 2 A/B 对照：同一 SCAN、同一 G1–G4，主板 vs Y 新权重（组合门关）。

回放源必须是主板 ``*.full.json``（Y 不写 full）。产出写
``data/coin-selection-y/reports/{scan_id}.ab_weights.json``。

    python3 scripts/ab_y_weights.py --scan-id 20260831-016
    python3 scripts/ab_y_weights.py --latest
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "coin-selection" / "src"))

from coin_selection.board_variants import BOARD_Y_KEY, get_variant, variant_settings  # noqa: E402
from coin_selection.scan import SelectionSettings, composite_score  # noqa: E402


def _pct(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    ys = sorted(xs)
    i = min(len(ys) - 1, max(0, int(round((p / 100.0) * (len(ys) - 1)))))
    return ys[i]


def _occupancy(rows: list[dict]) -> dict[str, int]:
    c: Counter[str] = Counter()
    for r in rows:
        for st in (r.get("state_up"), r.get("state_down")):
            if st:
                c[str(st)] += 1
    return dict(c)


def _rescore(rows: list[dict], settings: SelectionSettings) -> list[dict]:
    out = []
    for raw in rows:
        r = dict(raw)
        liq = float(r.get("liquidity_score_abs") or 0)
        dq = float(r.get("data_quality_score") or 80)
        r["score_up"] = composite_score(
            settings,
            ss=float(r.get("ss_up") or 0),
            mom=float(r.get("momentum_score_up") or 50),
            liq=liq,
            mcap=float(r.get("mcap_momentum_score_up") or 50),
            cons=float(r.get("consistency_up") or 0),
            dq=dq,
        )
        r["score_down"] = composite_score(
            settings,
            ss=float(r.get("ss_down") or 0),
            mom=float(r.get("momentum_score_down") or 50),
            liq=liq,
            mcap=float(r.get("mcap_momentum_score_down") or 50),
            cons=float(r.get("consistency_down") or 0),
            dq=dq,
        )
        out.append(r)
    return out


def _side_scores(rows: list[dict]) -> list[float]:
    xs = []
    for r in rows:
        xs.append(float(r.get("score_up") or 0))
        xs.append(float(r.get("score_down") or 0))
    return xs


def _conservation(occ: dict[str, int], n_rows: int) -> bool:
    total = sum(occ.get(k, 0) for k in ("WATCH", "QUALIFIED", "CONFIRMED", "ELIMINATED", "NONE", "DATA_INSUFFICIENT", "LOW_CONFIDENCE"))
    return total == n_rows * 2


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan-id")
    ap.add_argument("--latest", action="store_true")
    ap.add_argument("--source-data-dir", default=str(ROOT / "data" / "coin-selection"))
    args = ap.parse_args()
    snaps = Path(args.source_data_dir) / "snapshots"
    if args.latest:
        fulls = sorted(snaps.glob("*.full.json"))
        if not fulls:
            print("no full.json", file=sys.stderr)
            return 2
        fp = fulls[-1]
        scan_id = fp.name[: -len(".full.json")]
    elif args.scan_id:
        scan_id = args.scan_id
        fp = snaps / f"{scan_id}.full.json"
    else:
        print("need --scan-id or --latest", file=sys.stderr)
        return 2
    if not fp.is_file():
        print(f"missing {fp}", file=sys.stderr)
        return 2
    full = json.loads(fp.read_text(encoding="utf-8"))
    rows = full.get("rows") or []
    main_s = SelectionSettings()
    y = get_variant(BOARD_Y_KEY)
    y_s = variant_settings(SelectionSettings(), y)
    main_rows = _rescore(rows, main_s)
    y_rows = _rescore(rows, y_s)
    main_scores = _side_scores(main_rows)
    y_scores = _side_scores(y_rows)
    report = {
        "scan_id": scan_id,
        "n_rows": len(rows),
        "main": {
            "parameter_version": main_s.parameter_version,
            "weights": {k: getattr(main_s, k) for k in ("w_ss", "w_mom", "w_liq", "w_mcap", "w_cons", "w_rank", "w_risk")},
            "score_p50": _pct(main_scores, 50),
            "score_p90": _pct(main_scores, 90),
            "occupancy_sides": _occupancy(main_rows),
            "conservation": _conservation(_occupancy(main_rows), len(rows)),
        },
        "y": {
            "parameter_version": y_s.parameter_version,
            "weights": {k: getattr(y_s, k) for k in ("w_ss", "w_mom", "w_liq", "w_mcap", "w_cons", "w_rank", "w_risk")},
            "score_p50": _pct(y_scores, 50),
            "score_p90": _pct(y_scores, 90),
            "occupancy_sides": _occupancy(y_rows),
            "conservation": _conservation(_occupancy(y_rows), len(rows)),
            "w_mcap_gt_w_mom": y_s.w_mcap > y_s.w_mom,
        },
        "note": "Score-only A/B on the same G1–G4 rows. Occupancy here is the SM state already on the full snapshot, not a re-run SM. Divergence of Score is the success signal. ENABLE_MCAP_ZONE remains 0.",
        "fork_expected": True,
    }
    out_dir = ROOT / "data" / "coin-selection-y" / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{scan_id}.ab_weights.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print("wrote", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
