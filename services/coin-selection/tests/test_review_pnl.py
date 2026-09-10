"""realized_pnl: direction-aware closed-trade return (review ledger)."""

from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from coin_selection.review_pnl import realized_pnl  # noqa: E402


def test_up_profit():
    pct, sign = realized_pnl("up", stay=100.0, exitp=110.0)
    assert pct is not None and abs(pct - 0.10) < 1e-12
    assert sign == 1


def test_down_profit_when_price_falls():
    pct, sign = realized_pnl("down", stay=100.0, exitp=90.0)
    assert pct is not None and abs(pct - 0.10) < 1e-12
    assert sign == 1


def test_down_loss_when_price_rises():
    pct, sign = realized_pnl("down", stay=100.0, exitp=110.0)
    assert pct is not None and abs(pct - (-0.10)) < 1e-12
    assert sign == -1


def test_flat_maps_to_zero():
    pct, sign = realized_pnl("up", stay=50.0, exitp=50.0)
    assert pct == 0.0
    assert sign == 0


def test_zero_stay_is_null():
    assert realized_pnl("up", stay=0.0, exitp=1.0) == (None, None)


def test_missing_price_is_null():
    assert realized_pnl("up", stay=None, exitp=1.0) == (None, None)
    assert realized_pnl("down", stay=1.0, exitp=None) == (None, None)


def test_nan_is_null():
    assert realized_pnl("up", stay=math.nan, exitp=1.0) == (None, None)


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print("ok", fn.__name__)
    print("all", len(tests))
