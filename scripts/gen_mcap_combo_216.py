#!/usr/bin/env python3
"""Generate packages/config/mcap_combo_216.json from the SOL §3.3 pure function.

Cross-diff against doc/Claude_Opus5_选币榜Y_216组合映射表.csv (上涨/下跌侧准入上限).

    python3 scripts/gen_mcap_combo_216.py
    python3 scripts/gen_mcap_combo_216.py --check
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "coin-selection" / "src"))

from coin_selection.mcap_combo import dump_mapping, generate_rows, mapping_path  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="diff against Claude CSV, do not write")
    ap.add_argument(
        "--csv",
        default=str(ROOT / "doc" / "Claude_Opus5_选币榜Y_216组合映射表.csv"),
    )
    args = ap.parse_args()
    rows = generate_rows()
    csv_path = Path(args.csv)
    mismatches = []
    if csv_path.is_file():
        with csv_path.open("r", encoding="utf-8-sig") as f:
            for cr in csv.DictReader(f):
                no = int(cr["序号"])
                r = rows[no - 1]
                if r["ceiling_long"] != cr["上涨侧准入上限"] or r["ceiling_short"] != cr["下跌侧准入上限"]:
                    mismatches.append(
                        (
                            no,
                            r["combo"],
                            r["ceiling_long"],
                            cr["上涨侧准入上限"],
                            r["ceiling_short"],
                            cr["下跌侧准入上限"],
                        )
                    )
        print(f"csv rows=216 mismatches={len(mismatches)}")
        for m in mismatches[:12]:
            print("  ", m)
    else:
        print("csv missing", csv_path)
    if args.check:
        return 1 if mismatches else 0
    path = dump_mapping()
    print("wrote", path, "rows", len(rows))
    return 1 if mismatches else 0


if __name__ == "__main__":
    raise SystemExit(main())
