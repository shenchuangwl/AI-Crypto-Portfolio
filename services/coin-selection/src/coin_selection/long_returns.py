"""1Week / 1Month 涨跌幅 —— 选币榜「24h」与「锚点以来」之间两列的事实源。

计算口径**严格参照**既有的 1h / 4h / 24h 三列（``gate2.roc``），只是取数周期不同::

    涨跌幅 = 最新收盘价 / 回看 N 根之前的收盘价 - 1

    1h     : 1h K 线回看 1  根     （gate2.py）
    4h     : 1h K 线回看 4  根     （gate2.py）
    24h    : 1h K 线回看 24 根     （gate2.py）
    1Week  : 1d K 线回看 7  根     （本文件）
    1Month : 1d K 线回看 30 根     （本文件）

也就是说这两列同样是**滚动窗口**收益率（近 7 天 / 近 30 天），不是"本周 K 线开→收"
或"本自然月开→收"。这与 24h 表示"最近 24 小时"而不是"今天"完全一致。

与 gate2 一样**保留正在形成的最后一根 K 线**：它的收盘价就是当前最新价，所以分子
是实时价，分母是 7 / 30 天前那根已收盘 K 线的收盘价。丢掉最后一根会让"近 7 天"
变成"截至昨收的 7 天"，与 24h 列的口径就对不上了。

覆盖面：与 30m/2h/6h 流通市值等级三列一样按**全宇宙**计算，不按 Gate1 是否通过裁剪。
DMR / 确认 / 符合 / 观察 / 淘汰 / 数据不足 / 低置信度 七个分区共用同一张表格，
任何一个分区缺列都算回归。

K 线根数不足以回看 N 根时**返回 None**（前端显示 `—`），绝不用 0 冒充"没涨没跌"。
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from .mcap_timeframe import RateLimiter

log = logging.getLogger("coin_selection.long_ret")

#: 选币榜新增两列对应的周期，顺序即列顺序（1Week → 1Month）。
PERIODS: tuple[str, ...] = ("1w", "1mo")

#: 每个周期回看的 1d K 线根数。1Month 取 30 天，与 24h 取 24 小时同为整数倍滚动窗口。
LOOKBACK_BARS: dict[str, int] = {"1w": 7, "1mo": 30}

#: 前端渲染的列 id → 后端字段名。``1mo`` 不写作 ``1m``：``ret_15m`` 已经占用了"分钟"语义。
RET_FIELD: dict[str, str] = {"1w": "ret_1w", "1mo": "ret_1mo"}

#: 取数用的 K 线周期。
KLINE_INTERVAL: str = "1d"

#: 请求根数：回看 30 根 + 正在形成的 1 根 + 余量。
KLINE_LIMIT: int = 35

#: 缓存 TTL。分母是 7 / 30 天前那根**已收盘**日 K，一天之内根本不变；分子虽然是实时价，
#: 但放在 7 天 / 30 天的窗口里，1 小时的滞后对百分比的影响可以忽略。取 1 小时即可把
#: 全宇宙的 REST 调用从"每个 15 分钟节点一轮"摊薄成"每 4 个节点一轮"。
DEFAULT_TTL_SEC: int = 3600


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def ttl_sec() -> int:
    override = _env_int("LONG_RET_TTL_SEC", 0)
    return override if override > 0 else DEFAULT_TTL_SEC


@dataclass
class LongReturnResult:
    """单个合约的 1Week / 1Month 涨跌幅。"""

    symbol: str
    bars: int
    ret_1w: Optional[float]
    ret_1mo: Optional[float]
    reason: str
    data_source: str = "binance_klines"


# --------------------------------------------------------------------------
# 涨跌幅
# --------------------------------------------------------------------------


def closes_from_klines(klines: list) -> list[float]:
    """取收盘价（kline index 4）。**保留**正在形成的最后一根 —— 见模块文档。"""
    out: list[float] = []
    for k in klines or []:
        try:
            c = float(k[4])
        except (IndexError, TypeError, ValueError):
            continue
        if c > 0:
            out.append(c)
    return out


def pct_change(closes: list[float], bars: int) -> Optional[float]:
    """``closes[-1] / closes[-1-bars] - 1``；根数不足或基准为 0 时返回 ``None``。

    与 ``gate2.roc`` 同一个式子，唯一的区别是那里把"算不出"折叠成 ``0.0``
    （1h/4h/24h 是必填 number），这里如实返回 ``None``，让表格显示 `—`。
    """
    if bars <= 0 or len(closes) <= bars:
        return None
    base = closes[-1 - bars]
    if not base:
        return None
    return closes[-1] / base - 1.0


def evaluate_closes(
    symbol: str,
    closes: list[float],
    *,
    data_source: str = "binance_klines",
) -> LongReturnResult:
    """对一个合约的日线收盘价序列算两个周期的涨跌幅。"""
    bars = len(closes)
    r1w = pct_change(closes, LOOKBACK_BARS["1w"])
    r1mo = pct_change(closes, LOOKBACK_BARS["1mo"])
    if r1w is None and r1mo is None:
        reason = f"insufficient_bars<{LOOKBACK_BARS['1w'] + 1}"
    elif r1mo is None:
        reason = f"insufficient_bars<{LOOKBACK_BARS['1mo'] + 1}"
    else:
        reason = "ok"
    return LongReturnResult(
        symbol=symbol,
        bars=bars,
        ret_1w=r1w,
        ret_1mo=r1mo,
        reason=reason,
        data_source=data_source,
    )


# --------------------------------------------------------------------------
# K 线拉取 + 缓存
# --------------------------------------------------------------------------


class DailyKlineCache:
    """日线收盘价序列缓存。TTL 见 :func:`ttl_sec`。

    与 ``gate2.KlineCache`` 一样给条目打上 ``limit`` 戳：改 :data:`KLINE_LIMIT`
    时缓存自动失效，不会出现同一轮里有的合约按 35 根、有的按旧根数算。
    """

    def __init__(self, cache_dir: Path):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, symbol: str) -> Path:
        return self.cache_dir / f"{symbol}.json"

    def get(
        self,
        symbol: str,
        *,
        ignore_ttl: bool = False,
        want_limit: Optional[int] = KLINE_LIMIT,
    ) -> Optional[list[float]]:
        p = self._path(symbol)
        if not p.is_file():
            return None
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
            if not ignore_ttl and time.time() - float(obj.get("ts", 0)) > ttl_sec():
                return None
            if want_limit is not None and int(obj.get("limit") or 0) != int(want_limit):
                return None
            return [float(x) for x in obj.get("closes", [])]
        except Exception:
            return None

    def set(self, symbol: str, closes: list[float], *, limit: int = KLINE_LIMIT) -> None:
        p = self._path(symbol)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "ts": time.time(),
                    "interval": KLINE_INTERVAL,
                    "limit": int(limit),
                    "closes": closes,
                }
            ),
            encoding="utf-8",
        )
        os.replace(tmp, p)


def _rest_klines(
    base: str,
    symbol: str,
    *,
    limit: int = KLINE_LIMIT,
    timeout: float = 20.0,
    retries: int = 3,
) -> list:
    q = urllib.parse.urlencode(
        {"symbol": symbol, "interval": KLINE_INTERVAL, "limit": limit}
    )
    url = base.rstrip("/") + "/fapi/v1/klines?" + q
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "hermes-coin-selection/long-ret",
            "Accept": "application/json",
        },
    )
    last: Optional[Exception] = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            last = RuntimeError(f"HTTP {e.code} {symbol}/{KLINE_INTERVAL}: {body[:160]}")
            if e.code in (418, 429, 503) and attempt + 1 < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise last from e
        except Exception as e:
            last = e
            if attempt + 1 < retries:
                time.sleep(0.8 * (attempt + 1))
                continue
            raise RuntimeError(f"klines {symbol}/{KLINE_INTERVAL}: {e}") from e
    raise RuntimeError(f"klines {symbol}/{KLINE_INTERVAL}: {last}")


def run_long_returns(
    symbols: Iterable[str],
    *,
    fapi_rest: str = "https://fapi.binance.com",
    cache_dir: Optional[Path] = None,
    workers: Optional[int] = None,
    rate_per_sec: Optional[float] = None,
) -> tuple[dict[str, LongReturnResult], dict[str, Any]]:
    """全宇宙的 1Week / 1Month 涨跌幅。返回 ``(results[symbol], stats)``。"""
    syms = [s for s in symbols if s]
    cache = DailyKlineCache(cache_dir or Path("data/coin-selection/kline1d_cache"))
    workers = workers or _env_int("LONG_RET_WORKERS", 8)
    limiter = RateLimiter(
        rate_per_sec if rate_per_sec is not None else _env_float("LONG_RET_RATE_PER_SEC", 20.0)
    )

    stats: dict[str, Any] = {
        "enabled": True,
        "symbols": len(syms),
        "periods": list(PERIODS),
        "cache_hits": 0,
        "rest_calls": 0,
        "errors": 0,
        "computed": {p: 0 for p in PERIODS},
        "insufficient": {p: 0 for p in PERIODS},
    }
    results: dict[str, LongReturnResult] = {}

    todo: list[str] = []
    for sym in syms:
        closes = cache.get(sym)
        if closes is None:
            todo.append(sym)
        else:
            stats["cache_hits"] += 1
            results[sym] = evaluate_closes(sym, closes, data_source="cache")

    log.info(
        "long_ret symbols=%s cache_hit=%s rest_needed=%s workers=%s ttl=%ss",
        len(syms),
        stats["cache_hits"],
        len(todo),
        workers,
        ttl_sec(),
    )

    def one(sym: str) -> LongReturnResult:
        limiter.acquire()
        kl = _rest_klines(fapi_rest, sym, limit=KLINE_LIMIT)
        closes = closes_from_klines(kl)
        cache.set(sym, closes)
        return evaluate_closes(sym, closes, data_source="rest")

    if todo:
        with ThreadPoolExecutor(max_workers=max(1, int(workers))) as ex:
            futs = {ex.submit(one, s): s for s in todo}
            for i, fut in enumerate(as_completed(futs), 1):
                sym = futs[fut]
                try:
                    results[sym] = fut.result()
                    stats["rest_calls"] += 1
                except Exception as e:
                    stats["errors"] += 1
                    # 拉取失败时退回过期缓存：宁可用旧涨跌幅也不要伪造 0
                    stale = cache.get(sym, ignore_ttl=True, want_limit=None)
                    if stale:
                        results[sym] = evaluate_closes(
                            sym, stale, data_source="cache_stale"
                        )
                    else:
                        log.warning("long_ret fetch fail %s: %s", sym, e)
                        results[sym] = LongReturnResult(
                            symbol=sym,
                            bars=0,
                            ret_1w=None,
                            ret_1mo=None,
                            reason=f"fetch_error:{e}"[:120],
                            data_source="error",
                        )
                if i % 200 == 0:
                    log.info("long_ret progress %s/%s", i, len(todo))

    for sym in syms:
        r = results.get(sym)
        if r is None:
            continue
        for p in PERIODS:
            if getattr(r, RET_FIELD[p]) is None:
                stats["insufficient"][p] += 1
            else:
                stats["computed"][p] += 1

    log.info("long_ret %s", stats)
    return results, stats


# --------------------------------------------------------------------------
# 板面字段
# --------------------------------------------------------------------------


def row_fields(res: Optional[LongReturnResult]) -> dict[str, Any]:
    """摊成板面行字段。两个键**永远存在**（``None`` 表示算不出），
    列结构因此在七个分区、上涨/下跌两个候选池里完全一致。"""
    return {
        "ret_1w": res.ret_1w if res else None,
        "ret_1mo": res.ret_1mo if res else None,
    }


def result_to_dict(r: LongReturnResult) -> dict[str, Any]:
    return {
        "symbol": r.symbol,
        "bars": r.bars,
        "ret_1w": r.ret_1w,
        "ret_1mo": r.ret_1mo,
        "reason": r.reason,
        "data_source": r.data_source,
    }
