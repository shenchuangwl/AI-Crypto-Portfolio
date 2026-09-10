"""Gate 1 — liquidity hard floor (design v1.2 §12).

Hard pass when ALL of:
  avg_turnover_6d  > 3_000_000 USD
  avg_turnover_12d > 3_000_000 USD
  avg_turnover_26d > 3_000_000 USD

Daily quote turnover from Binance 1d klines field quoteAssetVolume (index 7).
Supports cache, concurrent fetch, progress checkpoint, retries.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("coin_selection.gate1")

HARD_FLOOR_USD = 3_000_000.0
PERIODS = (6, 12, 26)


@dataclass
class LiquidityResult:
    symbol: str
    history_days: int
    daily_turnovers: list[float]
    avg_turnover_6d: Optional[float]
    avg_turnover_12d: Optional[float]
    avg_turnover_26d: Optional[float]
    t_min: Optional[float]
    t_geo: Optional[float]
    liquidity_hard_pass: bool
    liquidity_grade: str
    liquidity_score_abs: float
    reason: str
    data_source: str


def _rest_klines_1d(
    base: str,
    symbol: str,
    limit: int = 30,
    timeout: float = 20.0,
    retries: int = 3,
) -> list:
    q = urllib.parse.urlencode(
        {"symbol": symbol, "interval": "1d", "limit": limit}
    )
    url = base.rstrip("/") + "/fapi/v1/klines?" + q
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "hermes-coin-selection/p1", "Accept": "application/json"},
    )
    last_err: Optional[Exception] = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            if e.code in (418, 429, 503) and attempt + 1 < retries:
                time.sleep(1.5 * (attempt + 1))
                last_err = RuntimeError(f"HTTP {e.code} {symbol}: {body[:200]}")
                continue
            raise RuntimeError(f"HTTP {e.code} {symbol}: {body[:200]}") from e
        except Exception as e:
            last_err = e
            if attempt + 1 < retries:
                time.sleep(0.8 * (attempt + 1))
                continue
            raise RuntimeError(f"klines {symbol}: {e}") from e
    raise RuntimeError(f"klines {symbol}: {last_err}")


def parse_daily_quote_volumes(klines: list, *, drop_incomplete_last: bool = True) -> list[float]:
    if not klines:
        return []
    rows = list(klines)
    if drop_incomplete_last and len(rows) >= 2:
        rows = rows[:-1]
    out: list[float] = []
    for k in rows:
        try:
            out.append(float(k[7]))
        except (IndexError, TypeError, ValueError):
            continue
    return out


def grade_abc(avg6: float, avg12: float, avg26: float, tol: float = 0.005) -> str:
    a, b, c = avg6, avg12, avg26

    def gt(x: float, y: float) -> bool:
        return x > y * (1 + tol)

    def eq(x: float, y: float) -> bool:
        m = max(abs(x), abs(y), 1.0)
        return abs(x - y) / m <= tol

    if eq(a, b) or eq(b, c) or eq(a, c):
        return "UNCLASSIFIED"
    if gt(a, b) and gt(b, c):
        return "A"
    if gt(b, a) and gt(a, c):
        return "B"
    if gt(b, c) and gt(c, a):
        return "C"
    if gt(c, b) and gt(b, a):
        return "D"
    return "UNCLASSIFIED"


def abs_liquidity_score(t_geo: float, t_cap: float = 50_000_000.0) -> float:
    if t_geo <= HARD_FLOOR_USD:
        return 0.0
    num = math.log(t_geo) - math.log(HARD_FLOOR_USD)
    den = math.log(max(t_cap, HARD_FLOOR_USD * 1.01)) - math.log(HARD_FLOOR_USD)
    return max(0.0, min(100.0, 100.0 * num / den))


def evaluate_symbol_turnovers(
    symbol: str,
    daily: list[float],
    *,
    hard_floor: float = HARD_FLOOR_USD,
) -> LiquidityResult:
    n = len(daily)
    if n < 14:
        return LiquidityResult(
            symbol=symbol,
            history_days=n,
            daily_turnovers=daily[-26:],
            avg_turnover_6d=None,
            avg_turnover_12d=None,
            avg_turnover_26d=None,
            t_min=None,
            t_geo=None,
            liquidity_hard_pass=False,
            liquidity_grade="INSUFFICIENT",
            liquidity_score_abs=0.0,
            reason="history_days<14",
            data_source="binance_1d",
        )

    def avg_last(k: int) -> Optional[float]:
        if n < k:
            return None
        chunk = daily[-k:]
        return sum(chunk) / k

    a6, a12, a26 = avg_last(6), avg_last(12), avg_last(26)
    if a26 is None and n >= 14:
        a26 = sum(daily) / n

    vals = [v for v in (a6, a12, a26) if v is not None]
    if len(vals) < 3 or a6 is None or a12 is None or a26 is None:
        return LiquidityResult(
            symbol=symbol,
            history_days=n,
            daily_turnovers=daily[-26:],
            avg_turnover_6d=a6,
            avg_turnover_12d=a12,
            avg_turnover_26d=a26,
            t_min=min(vals) if vals else None,
            t_geo=None,
            liquidity_hard_pass=False,
            liquidity_grade="INSUFFICIENT",
            liquidity_score_abs=0.0,
            reason="incomplete_averages",
            data_source="binance_1d",
        )

    t_min = min(a6, a12, a26)
    t_geo = (a6 * a12 * a26) ** (1.0 / 3.0)
    hard = a6 > hard_floor and a12 > hard_floor and a26 > hard_floor
    if n < 26:
        hard = False
        reason = "history_days<26_isolated"
        grade = "INSUFFICIENT"
    elif not hard:
        reason = "below_3m_floor"
        grade = grade_abc(a6, a12, a26)
    else:
        reason = "pass"
        grade = grade_abc(a6, a12, a26)

    return LiquidityResult(
        symbol=symbol,
        history_days=n,
        daily_turnovers=daily[-26:],
        avg_turnover_6d=a6,
        avg_turnover_12d=a12,
        avg_turnover_26d=a26,
        t_min=t_min,
        t_geo=t_geo,
        liquidity_hard_pass=hard,
        liquidity_grade=grade,
        liquidity_score_abs=abs_liquidity_score(t_geo) if hard else 0.0,
        reason=reason,
        data_source="binance_1d",
    )


class TurnoverCache:
    def __init__(self, cache_dir: Path, ttl_sec: int = 12 * 3600):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.ttl_sec = ttl_sec

    def get(self, symbol: str) -> Optional[list[float]]:
        p = self.cache_dir / f"{symbol}.json"
        if not p.is_file():
            return None
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
            if time.time() - float(obj.get("ts", 0)) > self.ttl_sec:
                return None
            return [float(x) for x in obj.get("daily", [])]
        except Exception:
            return None

    def set(self, symbol: str, daily: list[float]) -> None:
        p = self.cache_dir / f"{symbol}.json"
        tmp = p.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"ts": time.time(), "daily": daily}, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(tmp, p)


def result_to_dict(r: LiquidityResult) -> dict[str, Any]:
    return {
        "symbol": r.symbol,
        "history_days": r.history_days,
        "avg_turnover_6d": r.avg_turnover_6d,
        "avg_turnover_12d": r.avg_turnover_12d,
        "avg_turnover_26d": r.avg_turnover_26d,
        "t_min": r.t_min,
        "t_geo": r.t_geo,
        "liquidity_hard_pass": r.liquidity_hard_pass,
        "liquidity_grade": r.liquidity_grade,
        "liquidity_score_abs": r.liquidity_score_abs,
        "reason": r.reason,
        "data_source": r.data_source,
        "aqv_6d_m": (r.avg_turnover_6d / 1e6) if r.avg_turnover_6d is not None else None,
        "aqv_12d_m": (r.avg_turnover_12d / 1e6) if r.avg_turnover_12d is not None else None,
        "aqv_26d_m": (r.avg_turnover_26d / 1e6) if r.avg_turnover_26d is not None else None,
    }


def run_gate1(
    symbols: list[str],
    *,
    fapi_rest: str = "https://fapi.binance.com",
    cache_dir: Optional[Path] = None,
    sleep_sec: float = 0.05,
    max_symbols: Optional[int] = None,
    hard_floor: float = HARD_FLOOR_USD,
    workers: int = 6,
    progress_path: Optional[Path] = None,
) -> dict[str, LiquidityResult]:
    cache = TurnoverCache(cache_dir or Path("data/coin-selection/turnover_cache"))
    out: dict[str, LiquidityResult] = {}
    syms = symbols[: max_symbols or len(symbols)]

    def one(sym: str) -> LiquidityResult:
        daily = cache.get(sym)
        src = "cache"
        if daily is None:
            kl = _rest_klines_1d(fapi_rest, sym, limit=30)
            daily = parse_daily_quote_volumes(kl)
            cache.set(sym, daily)
            src = "rest"
            if sleep_sec:
                time.sleep(sleep_sec)
        res = evaluate_symbol_turnovers(sym, daily, hard_floor=hard_floor)
        res.data_source = src
        return res

    # Prefer cache-first sequential classify, then concurrent REST for misses
    need_rest: list[str] = []
    for sym in syms:
        daily = cache.get(sym)
        if daily is not None:
            res = evaluate_symbol_turnovers(sym, daily, hard_floor=hard_floor)
            res.data_source = "cache"
            out[sym] = res
        else:
            need_rest.append(sym)

    log.info("gate1 cache_hit=%s rest_needed=%s workers=%s", len(out), len(need_rest), workers)

    done = len(out)
    total = len(syms)

    def write_progress() -> None:
        if not progress_path:
            return
        passed = sum(1 for r in out.values() if r.liquidity_hard_pass)
        payload = {
            "ts": time.time(),
            "done": len(out),
            "total": total,
            "passed": passed,
            "rest_pending": max(0, total - len(out)),
        }
        progress_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = progress_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, progress_path)

    write_progress()

    if need_rest:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            futs = {ex.submit(one, s): s for s in need_rest}
            for fut in as_completed(futs):
                sym = futs[fut]
                try:
                    out[sym] = fut.result()
                except Exception as e:
                    log.warning("gate1 fetch fail %s: %s", sym, e)
                    out[sym] = LiquidityResult(
                        symbol=sym,
                        history_days=0,
                        daily_turnovers=[],
                        avg_turnover_6d=None,
                        avg_turnover_12d=None,
                        avg_turnover_26d=None,
                        t_min=None,
                        t_geo=None,
                        liquidity_hard_pass=False,
                        liquidity_grade="INSUFFICIENT",
                        liquidity_score_abs=0.0,
                        reason=f"fetch_error:{e}",
                        data_source="error",
                    )
                done = len(out)
                if done % 25 == 0 or done == total:
                    log.info("gate1 progress %s/%s", done, total)
                    write_progress()

    write_progress()
    return out
