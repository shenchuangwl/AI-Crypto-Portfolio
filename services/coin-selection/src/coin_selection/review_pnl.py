"""Direction-aware realized PnL for the review ledger.

Storage is a decimal fraction (0.0231 = +2.31%), same as screener ret_*.
Up: exit/stay − 1. Down: 1 − exit/stay. Sign: 1 / 0 / −1.
"""

from __future__ import annotations

from typing import Optional

FLAT_EPS = 1e-12


def _as_price(v: object) -> Optional[float]:
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if x != x or x == 0.0:  # NaN or zero (division guard)
        return None
    return x


def realized_pnl(
    direction: str,
    stay: object,
    exitp: object,
) -> tuple[Optional[float], Optional[int]]:
    s = _as_price(stay)
    e = _as_price(exitp)
    if s is None or e is None:
        return None, None
    if direction == "up":
        pct = e / s - 1.0
    elif direction == "down":
        pct = 1.0 - e / s
    else:
        return None, None
    if pct != pct:  # NaN
        return None, None
    if abs(pct) < FLAT_EPS:
        return 0.0, 0
    return pct, (1 if pct > 0 else -1)
