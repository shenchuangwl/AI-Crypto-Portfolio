#!/usr/bin/env python3
"""生成 / 校验 packages/config/mcap-216-v2.0.0-r1.json（ChatGpt_SOL5.6 文档A 附录A/B/C）。

    python3 scripts/gen_mcap_mapping_v2.py            # 重新生成冻结文件
    python3 scripts/gen_mcap_mapping_v2.py --check    # 只校验（CI 用，不写盘）

``--check`` 会依次断言：附录B 的全部不变量、生成器与冻结文件逐行相等、
规范序列化字节长度 39368、以及 mapping_hash == 文档A 附录C 的
``sha256:da14429d…``。任一不符即非零退出 —— 这就是「不能半表运行」的机器闸。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "coin-selection" / "src"))

from coin_selection import mcap_mapping as mm  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    try:
        for line in mm.verify_invariants():
            print(f"[PASS] {line}")
        if args.check:
            m = mm.load(strict=True)
            print(f"[PASS] 冻结文件与生成器逐行相等  rows={len(m.rows)}")
            print(f"[PASS] mcap_mapping_version = {m.mapping_version}")
            print(f"[PASS] mapping_hash = {m.mapping_hash}")
            return 0
        p = mm.dump()
        print(f"written {p}")
        return 0
    except mm.MappingError as e:
        print(f"[FAIL] {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
