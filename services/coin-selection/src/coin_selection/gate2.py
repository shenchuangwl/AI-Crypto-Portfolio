"""Gate 2 — basic market momentum (design v1.2 §15 simplified P1.5).

Not full multi-period model. For symbols that passed Gate1:
  - Pull 1h klines (limit=KLINE_LIMIT)
  - ROC 1h / 4h / 24h from closes
  - Simple RSI(14) on 1h
  - Direction scores up/down in 0-100
  - Consistency = share of periods with same sign

This is a temporary scorer to rank the liquidity pool before Gate3/4.
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

from .pit import closes_from_closed_klines, only_closed_bars

log = logging.getLogger("coin_selection.gate2")

# 1h bars fetched and cached per symbol.
#
# This is also the **rotating-staircase lookback**: Gate4 scores whatever this
# cache holds (`gate4.load_series` reads `<symbol>.ohlcv.json` written below),
# so the §27.3 window lives here, not in gate4.py.
#
# 48 bars (2 days) cannot physically contain the `N_step >= 2` structure that
# §27.4 requires. Measured over the live Gate1 pool (239 coins, scan
# 20260822-069): only 2.1% of coins have a 2-step staircase inside 48h, and the
# median coin needs a 168h lookback before one exists at all. That pinned 97.9%
# of the universe at the F2 cap `SS <= 45` — below the CONFIRMED gate
# `SS >= 50` — so CONFIRMED/DMR were starved by construction, not by market.
#
# Gate2's own momentum outputs are unchanged by the longer series: ROC(1/4/24)
# and RSI(14) are all tail-relative, so they read the same last bars as before.
KLINE_LIMIT = 168


@dataclass
class MomentumResult:
    symbol: str
    ret_1h: float
    ret_4h: float
    ret_24h: float
    rsi_1h: float
    momentum_score_up: float
    momentum_score_down: float
    consistency_up: float
    consistency_down: float
    reason: str
    data_source: str


def _rest_klines(
    base: str,
    symbol: str,
    interval: str = "1h",
    limit: int = KLINE_LIMIT,
    timeout: float = 15.0,
    retries: int = 2,
) -> list:
    q = urllib.parse.urlencode(
        {"symbol": symbol, "interval": interval, "limit": limit}
    )
    url = base.rstrip("/") + "/fapi/v1/klines?" + q
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "hermes-coin-selection/gate2", "Accept": "application/json"},
    )
    last: Optional[Exception] = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            last = e
            time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"klines {symbol}: {last}")


def closes_from_klines(klines: list, decision_time: Any = None) -> list[float]:
    """收盘价序列。

    【文档 A §7.2 / 文档 B §12【上线阻断】】统一使用 ``close_time <= decision_time``
    过滤未收盘 K 线；改造前本函数**保留**正在形成的最后一根，而 ``mcap_timeframe``
    **无条件丢弃**最后一根 —— 两条路径读的是两个不同时点的序列。现在两边都走
    :mod:`coin_selection.pit` 的同一个过滤器。

    ``decision_time=None`` 时保持旧行为（不过滤），供既有 fixtures 与离线工具使用。
    """
    return closes_from_closed_klines(klines, decision_time)


def ohlcv_from_klines(klines: list, decision_time: Any = None) -> dict[str, list[float]]:
    """OHLCV 序列，与 :func:`closes_from_klines` 同一个已收盘过滤器（文档 A §7.2）。"""
    o, h, l, c, v = [], [], [], [], []
    for k in only_closed_bars(klines, decision_time):
        try:
            o.append(float(k[1]))
            h.append(float(k[2]))
            l.append(float(k[3]))
            c.append(float(k[4]))
            v.append(float(k[5]))
        except (IndexError, TypeError, ValueError):
            continue
    return {"o": o, "h": h, "l": l, "c": c, "v": v}


def roc(closes: list[float], bars: int) -> float:
    if len(closes) <= bars or closes[-1 - bars] == 0:
        return 0.0
    return closes[-1] / closes[-1 - bars] - 1.0


def rsi(closes: list[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains = []
    losses = []
    for i in range(-period, 0):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag = sum(gains) / period
    al = sum(losses) / period
    if al == 0:
        return 100.0 if ag > 0 else 50.0
    rs = ag / al
    return 100.0 - (100.0 / (1.0 + rs))


def score_direction(ret_1h: float, ret_4h: float, ret_24h: float, rsi_v: float, up: bool) -> tuple[float, float]:
    """Return (score 0-100, consistency 0-1)."""
    rets = [ret_1h, ret_4h, ret_24h]
    weights = [0.25, 0.40, 0.35]
    # map return to 0-100 via tanh scale (~5% = strong)
    parts = []
    signs = []
    for r, w in zip(rets, weights):
        z = math.tanh(r / 0.05)  # -1..1
        if not up:
            z = -z
        parts.append(w * (50 + 50 * z))
        signs.append(1 if (r > 0 if up else r < 0) else 0)
    # RSI: up prefers ~62 tent, down prefers ~38
    if up:
        # tent peak 62 between 45-80
        if rsi_v <= 45:
            rsi_s = 30
        elif rsi_v >= 80:
            rsi_s = 40
        else:
            rsi_s = 100 - abs(rsi_v - 62) * 3
            rsi_s = max(20, min(100, rsi_s))
    else:
        if rsi_v >= 55:
            rsi_s = 30
        elif rsi_v <= 20:
            rsi_s = 40
        else:
            rsi_s = 100 - abs(rsi_v - 38) * 3
            rsi_s = max(20, min(100, rsi_s))
    base = sum(parts)
    score = 0.85 * base + 0.15 * rsi_s
    cons = sum(signs) / len(signs)
    return max(0.0, min(100.0, score)), cons


def evaluate_closes(symbol: str, closes: list[float], src: str = "rest") -> MomentumResult:
    if len(closes) < 26:
        return MomentumResult(
            symbol=symbol,
            ret_1h=0.0,
            ret_4h=0.0,
            ret_24h=0.0,
            rsi_1h=50.0,
            momentum_score_up=50.0,
            momentum_score_down=50.0,
            consistency_up=0.0,
            consistency_down=0.0,
            reason="insufficient_bars",
            data_source=src,
        )
    r1 = roc(closes, 1)
    r4 = roc(closes, 4)
    r24 = roc(closes, 24)
    rsi_v = rsi(closes, 14)
    up_s, up_c = score_direction(r1, r4, r24, rsi_v, True)
    dn_s, dn_c = score_direction(r1, r4, r24, rsi_v, False)
    return MomentumResult(
        symbol=symbol,
        ret_1h=r1,
        ret_4h=r4,
        ret_24h=r24,
        rsi_1h=rsi_v,
        momentum_score_up=up_s,
        momentum_score_down=dn_s,
        consistency_up=up_c,
        consistency_down=dn_c,
        reason="ok",
        data_source=src,
    )


class KlineCache:
    def __init__(self, cache_dir: Path, ttl_sec: int = 900):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.ttl_sec = ttl_sec

    def get(
        self,
        symbol: str,
        *,
        ignore_ttl: bool = False,
        want_limit: Optional[int] = None,
    ) -> Optional[list[float]]:
        """Read cached closes; ``want_limit`` guards the *window*.

        An entry written under a different kline limit is treated as a miss, so
        changing ``KLINE_LIMIT`` self-invalidates the cache instead of silently
        scoring some symbols on 48 bars and others on 168 within one scan.
        Entries predating the stamp carry no ``limit`` and are always a miss.
        Callers that only need a price series (``ret_since_anchor``) omit the
        argument and keep the previous behaviour.
        """
        p = self.cache_dir / f"{symbol}.json"
        if not p.is_file():
            return None
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
            if not ignore_ttl and time.time() - float(obj.get("ts", 0)) > self.ttl_sec:
                return None
            if want_limit is not None and int(obj.get("limit") or 0) != int(want_limit):
                return None
            return [float(x) for x in obj.get("closes", [])]
        except Exception:
            return None

    def set(
        self,
        symbol: str,
        closes: list[float],
        ohlcv: Optional[dict[str, list[float]]] = None,
        *,
        limit: int = KLINE_LIMIT,
    ) -> None:
        p = self.cache_dir / f"{symbol}.json"
        tmp = p.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {"ts": time.time(), "limit": int(limit), "closes": closes},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        os.replace(tmp, p)
        # Extended OHLCV for Gate4 formal staircase (optional)
        if ohlcv:
            p2 = self.cache_dir / f"{symbol}.ohlcv.json"
            tmp2 = p2.with_suffix(".tmp")
            payload = {"ts": time.time(), "limit": int(limit), **ohlcv}
            tmp2.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp2, p2)


def result_to_dict(r: MomentumResult) -> dict[str, Any]:
    return {
        "symbol": r.symbol,
        "ret_1h": r.ret_1h,
        "ret_4h": r.ret_4h,
        "ret_24h": r.ret_24h,
        "rsi_1h": r.rsi_1h,
        "momentum_score_up": r.momentum_score_up,
        "momentum_score_down": r.momentum_score_down,
        "consistency_up": r.consistency_up,
        "consistency_down": r.consistency_down,
        "reason": r.reason,
        "data_source": r.data_source,
    }


def run_gate2(
    symbols: list[str],
    *,
    fapi_rest: str = "https://fapi.binance.com",
    cache_dir: Optional[Path] = None,
    workers: int = 6,
    sleep_sec: float = 0.05,
    decision_time: Any = None,
) -> dict[str, MomentumResult]:
    """G2 动量与一致性。

    ``decision_time``（文档 A §7.2 / 文档 B §6 第 2 步）：本节点的决策时刻。
    传入后只使用 ``close_time <= decision_time`` 的 1h K 线，杜绝把未收盘的
    最后一根当成已收盘样本。缺省 ``None`` 保持旧行为，向后兼容。
    """
    cache = KlineCache(cache_dir or Path("data/coin-selection/kline1h_cache"))
    out: dict[str, MomentumResult] = {}

    def one(sym: str) -> MomentumResult:
        closes = cache.get(sym, want_limit=KLINE_LIMIT)
        src = "cache"
        if closes is None:
            kl = _rest_klines(fapi_rest, sym, interval="1h", limit=KLINE_LIMIT)
            kl = only_closed_bars(kl, decision_time)
            closes = closes_from_klines(kl)
            cache.set(sym, closes, ohlcv=ohlcv_from_klines(kl), limit=KLINE_LIMIT)
            src = "rest"
            if sleep_sec:
                time.sleep(sleep_sec)
        return evaluate_closes(sym, closes, src=src)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = {ex.submit(one, s): s for s in symbols}
        for i, fut in enumerate(as_completed(futs), 1):
            sym = futs[fut]
            try:
                out[sym] = fut.result()
            except Exception as e:
                log.warning("gate2 fail %s: %s", sym, e)
                out[sym] = MomentumResult(
                    symbol=sym,
                    ret_1h=0.0,
                    ret_4h=0.0,
                    ret_24h=0.0,
                    rsi_1h=50.0,
                    momentum_score_up=50.0,
                    momentum_score_down=50.0,
                    consistency_up=0.0,
                    consistency_down=0.0,
                    reason=f"fetch_error:{e}",
                    data_source="error",
                )
            if i % 20 == 0:
                log.info("gate2 progress %s/%s", i, len(symbols))
    return out
