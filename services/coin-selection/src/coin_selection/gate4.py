"""Gate 4 — rotating staircase score (design v1.2 §27–§28, formalizable temp).

Formal structure matching frozen interface:
  SS = 100 * (0.18 c_steps + 0.10 c_duration + 0.14 c_pivot + 0.14 c_retr
            + 0.14 c_vol + 0.12 c_mom + 0.09 c_adx + 0.09 c_r2) * penalties

Uses 1h OHLCV when available (from kline cache extended), else closes-only proxies.
Status: PROVISIONAL_REPLACEABLE but formula weights match §28.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("coin_selection.gate4")


@dataclass
class StairResult:
    symbol: str
    ss_up: float
    ss_down: float
    steps_up: int
    steps_down: int
    r2: float
    components_up: dict[str, float]
    components_down: dict[str, float]
    reason: str


def load_series(cache_dir: Path, symbol: str) -> dict[str, list[float]]:
    """Prefer full OHLCV cache; fall back to closes-only Gate2 cache."""
    # extended cache written by gate2 if present
    p_full = cache_dir / f"{symbol}.ohlcv.json"
    if p_full.is_file():
        try:
            obj = json.loads(p_full.read_text(encoding="utf-8"))
            return {
                "o": [float(x) for x in obj.get("o", [])],
                "h": [float(x) for x in obj.get("h", [])],
                "l": [float(x) for x in obj.get("l", [])],
                "c": [float(x) for x in obj.get("c", [])],
                "v": [float(x) for x in obj.get("v", [])],
            }
        except Exception:
            pass
    p = cache_dir / f"{symbol}.json"
    if p.is_file():
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
            c = [float(x) for x in obj.get("closes", [])]
            return {"o": c[:], "h": c[:], "l": c[:], "c": c, "v": [1.0] * len(c)}
        except Exception:
            pass
    return {"o": [], "h": [], "l": [], "c": [], "v": []}


def atr_pct(h: list[float], l: list[float], c: list[float], n: int = 14) -> float:
    if len(c) < n + 1:
        return 0.02
    trs = []
    for i in range(-n, 0):
        tr = max(
            h[i] - l[i],
            abs(h[i] - c[i - 1]),
            abs(l[i] - c[i - 1]),
        )
        trs.append(tr / max(c[i], 1e-12))
    return max(0.005, sum(trs) / len(trs))


def pivots(c: list[float], thr: float) -> list[tuple[int, float, str]]:
    if len(c) < 5:
        return []
    pts: list[tuple[int, float, str]] = []
    last_i, last_p = 0, c[0]
    direction = 0
    for i in range(1, len(c)):
        p = c[i]
        chg = (p - last_p) / last_p if last_p else 0
        if direction == 0:
            if chg >= thr:
                direction = 1
                pts.append((last_i, last_p, "L"))
            elif chg <= -thr:
                direction = -1
                pts.append((last_i, last_p, "H"))
            continue
        if direction == 1:
            if p >= last_p:
                last_i, last_p = i, p
            elif (last_p - p) / last_p >= thr:
                pts.append((last_i, last_p, "H"))
                direction = -1
                last_i, last_p = i, p
        else:
            if p <= last_p:
                last_i, last_p = i, p
            elif (p - last_p) / last_p >= thr:
                pts.append((last_i, last_p, "L"))
                direction = 1
                last_i, last_p = i, p
    if direction == 1:
        pts.append((last_i, last_p, "H"))
    elif direction == -1:
        pts.append((last_i, last_p, "L"))
    return pts


def r2_log(c: list[float]) -> float:
    n = len(c)
    if n < 5:
        return 0.0
    ys = [math.log(max(x, 1e-12)) for x in c]
    xs = list(range(n))
    mx = (n - 1) / 2.0
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    denx = sum((x - mx) ** 2 for x in xs)
    deny = sum((y - my) ** 2 for y in ys)
    if denx <= 0 or deny <= 0:
        return 0.0
    r = num / math.sqrt(denx * deny)
    return max(0.0, min(1.0, r * r))


def adx_proxy(h: list[float], l: list[float], c: list[float], n: int = 14) -> float:
    """Lightweight DX proxy 0-100 (not full Wilder ADX)."""
    if len(c) < n + 2:
        return 20.0
    up_moves = []
    down_moves = []
    for i in range(-n, 0):
        up = max(0.0, h[i] - h[i - 1])
        dn = max(0.0, l[i - 1] - l[i])
        if up > dn:
            down_moves.append(0.0)
            up_moves.append(up)
        elif dn > up:
            up_moves.append(0.0)
            down_moves.append(dn)
        else:
            up_moves.append(0.0)
            down_moves.append(0.0)
    trs = []
    for i in range(-n, 0):
        trs.append(
            max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
        )
    atr = sum(trs) / n if n else 1.0
    if atr <= 0:
        return 20.0
    di_p = 100 * (sum(up_moves) / n) / atr
    di_m = 100 * (sum(down_moves) / n) / atr
    s = di_p + di_m
    if s <= 0:
        return 0.0
    dx = 100 * abs(di_p - di_m) / s
    return max(0.0, min(100.0, dx))


def structure_up(pts: list[tuple[int, float, str]], v: list[float]) -> dict[str, float]:
    highs = [p for p in pts if p[2] == "H"]
    lows = [p for p in pts if p[2] == "L"]
    steps = 0
    retrs = []
    amps = []
    stage_vol = []
    for i in range(1, min(len(highs), len(lows))):
        if highs[i][1] > highs[i - 1][1] * 1.002 and lows[i][1] >= lows[i - 1][1]:
            steps += 1
            prev_low = lows[i - 1][1]
            h = highs[i - 1][1]
            l = lows[i][1]
            if h > prev_low:
                retrs.append((h - l) / (h - prev_low))
            amps.append(abs(h - prev_low) / max(prev_low, 1e-12))
            # volume advance vs platform (indices)
            i0, i1 = lows[i - 1][0], highs[i - 1][0]
            i2 = lows[i][0]
            if 0 <= i0 < i1 < len(v) and i1 < i2 < len(v):
                adv = sum(v[i0 : i1 + 1]) / max(1, i1 - i0 + 1)
                plat = sum(v[i1 : i2 + 1]) / max(1, i2 - i1 + 1)
                if plat > 0:
                    stage_vol.append(adv / plat)
    c_steps = min(1.0, max(0.0, (steps - 1) / 2.0)) if steps >= 1 else 0.0
    if steps >= 2:
        c_steps = min(1.0, (steps - 1) / 2.0)
    else:
        c_steps = 0.0 if steps == 0 else 0.25
    if retrs:
        # tent 0.15-0.382-0.618
        def tent(x: float) -> float:
            if x < 0.15 or x > 0.618:
                return 0.0
            if x <= 0.382:
                return (x - 0.15) / (0.382 - 0.15)
            return 1.0 - (x - 0.382) / (0.618 - 0.382)

        c_retr = sum(tent(x) for x in retrs) / len(retrs)
    else:
        c_retr = 0.25
    if amps and sum(amps) > 0:
        mu = sum(amps) / len(amps)
        var = sum((a - mu) ** 2 for a in amps) / len(amps)
        c_pivot = max(0.0, min(1.0, 1.0 - math.sqrt(var) / (mu + 1e-9)))
    else:
        c_pivot = 0.3
    if stage_vol:
        c_vol = max(0.0, min(1.0, (sum(stage_vol) / len(stage_vol) - 1.1) / 0.9))
    else:
        c_vol = 0.4
    # duration proxy: last step span
    if len(pts) >= 2:
        dur = (pts[-1][0] - pts[0][0]) / max(len(v), 1)
        c_duration = max(0.0, min(1.0, (dur - 0.3) / 0.7))
    else:
        c_duration = 0.2
    return {
        "steps": float(steps),
        "c_steps": c_steps,
        "c_retr": c_retr,
        "c_pivot": c_pivot,
        "c_vol": c_vol,
        "c_duration": c_duration,
    }


def structure_down(pts: list[tuple[int, float, str]], v: list[float]) -> dict[str, float]:
    highs = [p for p in pts if p[2] == "H"]
    lows = [p for p in pts if p[2] == "L"]
    steps = 0
    bounces = []
    amps = []
    stage_vol = []
    for i in range(1, min(len(highs), len(lows))):
        if lows[i][1] < lows[i - 1][1] * 0.998 and highs[i][1] <= highs[i - 1][1]:
            steps += 1
            h_prev = highs[i - 1][1]
            l = lows[i - 1][1]
            h = highs[i][1]
            if h_prev > l:
                bounces.append((h - l) / (h_prev - l))
            amps.append(abs(h_prev - l) / max(h_prev, 1e-12))
            i0, i1 = highs[i - 1][0], lows[i - 1][0]
            i2 = highs[i][0]
            if 0 <= i0 < i1 < len(v) and i1 < i2 < len(v):
                decline = sum(v[i0 : i1 + 1]) / max(1, i1 - i0 + 1)
                bounce = sum(v[i1 : i2 + 1]) / max(1, i2 - i1 + 1)
                if bounce > 0:
                    stage_vol.append(decline / bounce)
    c_steps = min(1.0, (steps - 1) / 2.0) if steps >= 2 else (0.25 if steps == 1 else 0.0)

    def tent(x: float) -> float:
        if x < 0.15 or x > 0.618:
            return 0.0
        if x <= 0.382:
            return (x - 0.15) / (0.382 - 0.15)
        return 1.0 - (x - 0.382) / (0.618 - 0.382)

    c_retr = sum(tent(x) for x in bounces) / len(bounces) if bounces else 0.25
    if amps and sum(amps) > 0:
        mu = sum(amps) / len(amps)
        var = sum((a - mu) ** 2 for a in amps) / len(amps)
        c_pivot = max(0.0, min(1.0, 1.0 - math.sqrt(var) / (mu + 1e-9)))
    else:
        c_pivot = 0.3
    if stage_vol:
        c_vol = max(0.0, min(1.0, (sum(stage_vol) / len(stage_vol) - 1.1) / 0.9))
    else:
        c_vol = 0.4
    if len(pts) >= 2:
        dur = (pts[-1][0] - pts[0][0]) / max(len(v), 1)
        c_duration = max(0.0, min(1.0, (dur - 0.3) / 0.7))
    else:
        c_duration = 0.2
    return {
        "steps": float(steps),
        "c_steps": c_steps,
        "c_retr": c_retr,
        "c_pivot": c_pivot,
        "c_vol": c_vol,
        "c_duration": c_duration,
    }


def compose_ss(comp: dict[str, float], c_mom: float, c_adx: float, c_r2: float, steps: int) -> float:
    # §28 weights
    raw = 100.0 * (
        0.18 * comp["c_steps"]
        + 0.10 * comp["c_duration"]
        + 0.14 * comp["c_pivot"]
        + 0.14 * comp["c_retr"]
        + 0.14 * comp["c_vol"]
        + 0.12 * c_mom
        + 0.09 * c_adx
        + 0.09 * c_r2
    )
    # F2: no real staircase without 2 steps
    if steps < 2:
        raw = min(raw, 45.0)
    return max(0.0, min(100.0, raw))


def evaluate_symbol(symbol: str, series: dict[str, list[float]]) -> StairResult:
    c = series.get("c") or []
    h = series.get("h") or c
    l = series.get("l") or c
    v = series.get("v") or [1.0] * len(c)
    if len(c) < 20:
        z = {
            "c_steps": 0.0,
            "c_duration": 0.0,
            "c_pivot": 0.0,
            "c_retr": 0.0,
            "c_vol": 0.0,
            "c_mom": 0.0,
            "c_adx": 0.0,
            "c_r2": 0.0,
        }
        return StairResult(symbol, 0.0, 0.0, 0, 0, 0.0, z, z, "insufficient_bars")

    thr = max(0.02, min(0.05, 1.5 * atr_pct(h, l, c)))
    pts = pivots(c, thr)
    up = structure_up(pts, v)
    dn = structure_down(pts, v)
    r2 = r2_log(c)
    c_r2 = max(0.0, min(1.0, (r2 - 0.45) / 0.40))
    adx = adx_proxy(h, l, c)
    c_adx = max(0.0, min(1.0, (adx - 18.0) / 14.0))
    # momentum continuity proxy: last third vs first third slope same sign
    n = len(c)
    s1 = c[n // 3] / max(c[0], 1e-12) - 1
    s2 = c[-1] / max(c[2 * n // 3], 1e-12) - 1
    c_mom_up = 1.0 if s1 > 0 and s2 > 0 else (0.6 if s2 > 0 else 0.2)
    c_mom_dn = 1.0 if s1 < 0 and s2 < 0 else (0.6 if s2 < 0 else 0.2)

    ss_up = compose_ss(up, c_mom_up, c_adx if s2 >= 0 else c_adx * 0.7, c_r2, int(up["steps"]))
    ss_dn = compose_ss(dn, c_mom_dn, c_adx if s2 <= 0 else c_adx * 0.7, c_r2, int(dn["steps"]))

    cu = {
        "c_steps": up["c_steps"],
        "c_duration": up["c_duration"],
        "c_pivot": up["c_pivot"],
        "c_retr": up["c_retr"],
        "c_vol": up["c_vol"],
        "c_mom": c_mom_up,
        "c_adx": c_adx,
        "c_r2": c_r2,
    }
    cd = {
        "c_steps": dn["c_steps"],
        "c_duration": dn["c_duration"],
        "c_pivot": dn["c_pivot"],
        "c_retr": dn["c_retr"],
        "c_vol": dn["c_vol"],
        "c_mom": c_mom_dn,
        "c_adx": c_adx,
        "c_r2": c_r2,
    }
    return StairResult(
        symbol,
        ss_up,
        ss_dn,
        int(up["steps"]),
        int(dn["steps"]),
        r2,
        cu,
        cd,
        "ok",
    )


def result_to_dict(r: StairResult) -> dict[str, Any]:
    return {
        "symbol": r.symbol,
        "ss_up": r.ss_up,
        "ss_down": r.ss_down,
        "steps_up": r.steps_up,
        "steps_down": r.steps_down,
        "r2": r.r2,
        "components_up": r.components_up,
        "components_down": r.components_down,
        "reason": r.reason,
    }


def run_gate4(symbols: list[str], *, kline_cache_dir: Path) -> dict[str, StairResult]:
    out: dict[str, StairResult] = {}
    for sym in symbols:
        series = load_series(kline_cache_dir, sym)
        out[sym] = evaluate_symbol(sym, series)
    return out
