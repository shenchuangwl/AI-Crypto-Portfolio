"""周期流通市值等级 —— 选币榜 `30m流通市值 / 2h流通市值 / 6h流通市值` 三列的事实源。

流通市值序列（当前周期下逐根 K 线）::

    流通市值_i = 流通供应量 × (第 i 根 K 线收盘价 / 合约乘数)

均线（"日"沿用既有命名，实际含义是**当前周期下的 K 线根数**）::

    6 日平均流通市值  = 最近 6  根当前周期 K 线的流通市值总和 / 6
    12 日平均流通市值 = 最近 12 根当前周期 K 线的流通市值总和 / 12
    26 日平均流通市值 = 最近 26 根当前周期 K 线的流通市值总和 / 26

等级定义（30m / 2h / 6h 三个周期使用**完全相同**的一套规则，
仅计算数据所属周期不同；上涨候选与下跌候选也共用同一套定义）::

    A：6 日平均流通市值 > 12 日平均流通市值 > 26 日平均流通市值 | 多头排列
    B：12 日平均流通市值 > 6 日平均流通市值 > 26 日平均流通市值 | 多头轻度回调
    C：12 日平均流通市值 > 26 日平均流通市值 > 6 日平均流通市值 | 多头重度回调
    D：12 日平均流通市值 < 26 日平均流通市值 < 6 日平均流通市值 | 空头重度回调
    E：12 日平均流通市值 < 6 日平均流通市值 < 26 日平均流通市值 | 空头轻度回调
    F：6 日平均流通市值 < 12 日平均流通市值 < 26 日平均流通市值 | 空头排列

六条严格不等式互斥且穷尽三个互不相等的均线的全部 6 种排列，
描述的是流通市值"增长 / 缩减"的强弱状态，不存在方向不对称问题。
K 线不足 26 根、或出现完全并列时**不判级**（``grade=None``，前端显示 `—`），
绝不新造等级名称，也绝不按方向拆成两套规则。

数据口径说明（必须与报告一致）：

* CoinGecko 只提供**当前**流通供应量，没有逐 K 线历史供应量。因此窗口内供应量取当前值，
  ``流通市值_i ∝ 收盘价_i``。等级只取决于三条均线的**大小关系**，而
  ``流通供应量 / 合约乘数`` 是一个正的公共因子，可以约去 —— 所以供应量缺失的合约
  （未过 G1、未映射 CoinGecko）仍能给出与有供应量时**完全一致**的等级，
  只是不发布绝对市值数值（``ma6/ma12/ma26 = None``，``supply_known=False``）。
* 只使用**已收盘**的 K 线（丢弃正在形成的最后一根），与 Gate1 日成交额口径一致，
  保证同一根 K 线内等级可复现、不抖动。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from .pit import closes_from_closed_klines  # 文档 A §7.2 的共同过滤器

log = logging.getLogger("coin_selection.mcap_tf")

#: 选币榜新增三列对应的周期，顺序即列顺序（30m流通市值 → 2h流通市值 → 6h流通市值）。
TIMEFRAMES: tuple[str, ...] = ("30m", "2h", "6h")

#: Binance fapi kline interval 字符串 → 秒。用于缓存 TTL（不用于对齐，K 线由交易所对齐）。
INTERVAL_SECONDS: dict[str, int] = {"30m": 1800, "2h": 7200, "6h": 21600}

#: 6 / 12 / 26 —— 三条均线的 K 线根数。
MA_PERIODS: tuple[int, int, int] = (6, 12, 26)

#: 判级所需的最少已收盘 K 线根数（= 最长均线周期）。
REQUIRED_BARS: int = 26

#: 请求根数：26 根已收盘 + 1 根正在形成 + 余量。
KLINE_LIMIT: int = 32

#: 前端渲染的列 id → 后端字段名。
GRADE_FIELD: dict[str, str] = {
    "30m": "mcap_grade_30m",
    "2h": "mcap_grade_2h",
    "6h": "mcap_grade_6h",
}

VALID_GRADES: tuple[str, ...] = ("A", "B", "C", "D", "E", "F")


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


def ttl_for(timeframe: str) -> int:
    """缓存 TTL：半根 K 线，且不短于一个 15m 扫描节点。

    30m→900s（每个节点刷新）、2h→3600s、6h→10800s。既保证等级在一根 K 线收盘后
    最多一个周期内更新，也把全宇宙的 REST 调用摊平在多个节点上。
    """
    override = _env_int("MCAP_TF_TTL_SEC", 0)
    if override > 0:
        return override
    return max(900, INTERVAL_SECONDS.get(timeframe, 1800) // 2)


@dataclass
class McapTfResult:
    """单个 (symbol, timeframe) 的周期流通市值等级结果。"""

    symbol: str
    timeframe: str
    bars: int
    ma6: Optional[float]
    ma12: Optional[float]
    ma26: Optional[float]
    grade: Optional[str]
    supply_known: bool
    reason: str
    data_source: str = "binance_klines"


# --------------------------------------------------------------------------
# 等级判定 —— 本文件唯一允许出现 A/B/C/D/E/F 语义的地方
# --------------------------------------------------------------------------


def grade_from_mas(
    ma6: Optional[float], ma12: Optional[float], ma26: Optional[float]
) -> Optional[str]:
    """按 §五 的等级定义原样判级。严格不等式，不做容差、不做方向区分。

    返回 ``A``–``F``；三条均线出现并列（无法落入任何一种严格排列）时返回 ``None``。
    """
    if ma6 is None or ma12 is None or ma26 is None:
        return None
    # A：6 日 > 12 日 > 26 日 | 多头排列
    if ma6 > ma12 > ma26:
        return "A"
    # B：12 日 > 6 日 > 26 日 | 多头轻度回调
    if ma12 > ma6 > ma26:
        return "B"
    # C：12 日 > 26 日 > 6 日 | 多头重度回调
    if ma12 > ma26 > ma6:
        return "C"
    # D：12 日 < 26 日 < 6 日 | 空头重度回调
    if ma12 < ma26 < ma6:
        return "D"
    # E：12 日 < 6 日 < 26 日 | 空头轻度回调
    if ma12 < ma6 < ma26:
        return "E"
    # F：6 日 < 12 日 < 26 日 | 空头排列
    if ma6 < ma12 < ma26:
        return "F"
    return None


# --------------------------------------------------------------------------
# 均线与市值序列
# --------------------------------------------------------------------------


def closes_from_klines(
    klines: list,
    *,
    drop_incomplete_last: bool = True,
    decision_time: Any = None,
) -> list[float]:
    """取收盘价（kline index 4）。

    【文档 A §7.2 / 文档 B §12【上线阻断】】口径统一：给了 ``decision_time`` 且行里带
    ``closeTime`` 时，按 ``close_time <= decision_time`` 判定是否已收盘 —— 不再靠
    「无条件丢最后一根」猜测。两个条件缺一时才退回旧启发式（由
    ``drop_incomplete_last`` 控制），以保持既有 fixtures 与离线工具的行为不变。
    """
    return closes_from_closed_klines(
        klines,
        decision_time,
        drop_incomplete_last_when_unknown=bool(drop_incomplete_last),
    )


def mcap_series(
    closes: list[float],
    *,
    circulating_supply: Optional[float],
    contract_multiplier: int = 1,
) -> tuple[list[float], bool]:
    """逐根 K 线的流通市值序列。

    返回 ``(series, supply_known)``。供应量缺失时用 ``supply=1`` 的等比序列占位 ——
    ``流通供应量 / 合约乘数`` 是正的公共因子，等级不变，只是绝对数值不可发布。
    """
    mult = int(contract_multiplier or 1) or 1
    supply: Optional[float]
    try:
        supply = float(circulating_supply) if circulating_supply is not None else None
    except (TypeError, ValueError):
        supply = None
    if supply is not None and (supply <= 0 or supply != supply):  # NaN / 非正
        supply = None
    factor = (supply / mult) if supply is not None else (1.0 / mult)
    return [c * factor for c in closes], supply is not None


def tail_mean(series: list[float], k: int) -> Optional[float]:
    """最近 k 根的算术平均；不足 k 根返回 None（绝不用更短窗口冒充）。"""
    if k <= 0 or len(series) < k:
        return None
    chunk = series[-k:]
    return sum(chunk) / float(k)


def evaluate_closes(
    symbol: str,
    timeframe: str,
    closes: list[float],
    *,
    circulating_supply: Optional[float],
    contract_multiplier: int = 1,
    data_source: str = "binance_klines",
) -> McapTfResult:
    """对一个 (symbol, timeframe) 计算 6/12/26 均线并判级。"""
    bars = len(closes)
    if bars < REQUIRED_BARS:
        return McapTfResult(
            symbol=symbol,
            timeframe=timeframe,
            bars=bars,
            ma6=None,
            ma12=None,
            ma26=None,
            grade=None,
            supply_known=False,
            reason=f"insufficient_bars<{REQUIRED_BARS}",
            data_source=data_source,
        )
    series, supply_known = mcap_series(
        closes,
        circulating_supply=circulating_supply,
        contract_multiplier=contract_multiplier,
    )
    ma6 = tail_mean(series, 6)
    ma12 = tail_mean(series, 12)
    ma26 = tail_mean(series, 26)
    grade = grade_from_mas(ma6, ma12, ma26)
    if grade is None:
        reason = "flat_or_tie"
    elif supply_known:
        reason = "ok"
    else:
        reason = "ok_supply_unknown"
    return McapTfResult(
        symbol=symbol,
        timeframe=timeframe,
        bars=bars,
        # 供应量未知时不发布绝对市值，只发布等级
        ma6=ma6 if supply_known else None,
        ma12=ma12 if supply_known else None,
        ma26=ma26 if supply_known else None,
        grade=grade,
        supply_known=supply_known,
        reason=reason,
        data_source=data_source,
    )


# --------------------------------------------------------------------------
# K 线拉取 + 缓存 + 限速
# --------------------------------------------------------------------------


class RateLimiter:
    """极简令牌桶，跨 worker 共享，避免 fapi 权重突刺。

    同包内的 ``long_returns``（1Week/1Month 两列）也复用它，只此一份实现。

    默认 20 req/s ≈ 1200 weight/min（klines limit<100 权重为 1），为同 IP 上并行跑的
    Gate1 / Gate2 以及 15 分钟循环留出 fapi 2400 weight/min 的一半余量。
    """

    def __init__(self, rate_per_sec: float):
        self.min_interval = 1.0 / rate_per_sec if rate_per_sec > 0 else 0.0
        self._lock = threading.Lock()
        self._next = 0.0

    def acquire(self) -> None:
        if self.min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            wait = self._next - now
            if wait < 0:
                wait = 0.0
                self._next = now
            self._next += self.min_interval
        if wait > 0:
            time.sleep(wait)


class TfKlineCache:
    """按周期分目录缓存收盘价序列。TTL 见 :func:`ttl_for`。"""

    def __init__(self, cache_dir: Path):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, symbol: str, timeframe: str) -> Path:
        d = self.cache_dir / timeframe
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{symbol}.json"

    def get(
        self, symbol: str, timeframe: str, *, ignore_ttl: bool = False
    ) -> Optional[list[float]]:
        p = self._path(symbol, timeframe)
        if not p.is_file():
            return None
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
            if not ignore_ttl and time.time() - float(obj.get("ts", 0)) > ttl_for(timeframe):
                return None
            return [float(x) for x in obj.get("closes", [])]
        except Exception:
            return None

    def set(self, symbol: str, timeframe: str, closes: list[float]) -> None:
        p = self._path(symbol, timeframe)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"ts": time.time(), "interval": timeframe, "closes": closes}),
            encoding="utf-8",
        )
        os.replace(tmp, p)


def _rest_klines(
    base: str,
    symbol: str,
    interval: str,
    *,
    limit: int = KLINE_LIMIT,
    timeout: float = 20.0,
    retries: int = 3,
) -> list:
    q = urllib.parse.urlencode({"symbol": symbol, "interval": interval, "limit": limit})
    url = base.rstrip("/") + "/fapi/v1/klines?" + q
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "hermes-coin-selection/mcap-tf",
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
            last = RuntimeError(f"HTTP {e.code} {symbol}/{interval}: {body[:160]}")
            if e.code in (418, 429, 503) and attempt + 1 < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise last from e
        except Exception as e:
            last = e
            if attempt + 1 < retries:
                time.sleep(0.8 * (attempt + 1))
                continue
            raise RuntimeError(f"klines {symbol}/{interval}: {e}") from e
    raise RuntimeError(f"klines {symbol}/{interval}: {last}")


def run_mcap_timeframes(
    symbols: Iterable[str],
    *,
    supplies: Optional[dict[str, Optional[float]]] = None,
    multipliers: Optional[dict[str, int]] = None,
    fapi_rest: str = "https://fapi.binance.com",
    cache_dir: Optional[Path] = None,
    timeframes: Iterable[str] = TIMEFRAMES,
    workers: Optional[int] = None,
    rate_per_sec: Optional[float] = None,
    decision_time: Any = None,
) -> tuple[dict[str, dict[str, McapTfResult]], dict[str, Any]]:
    """全宇宙 × 三周期的流通市值等级。

    ``supplies`` / ``multipliers`` 缺项不影响等级，只影响是否发布绝对市值数值。
    ``decision_time``（文档 A §7.2 / §8）：本节点决策时刻，只使用
    ``close_time <= decision_time`` 的 K 线；与 Gate2 共用同一个过滤器。
    返回 ``(results[symbol][timeframe], stats)``。
    """
    syms = [s for s in symbols if s]
    tfs = [t for t in timeframes if t in INTERVAL_SECONDS]
    supplies = supplies or {}
    multipliers = multipliers or {}
    cache = TfKlineCache(cache_dir or Path("data/coin-selection/kline_tf_cache"))
    workers = workers or _env_int("MCAP_TF_WORKERS", 10)
    limiter = RateLimiter(
        rate_per_sec if rate_per_sec is not None else _env_float("MCAP_TF_RATE_PER_SEC", 20.0)
    )

    stats: dict[str, Any] = {
        "enabled": True,
        "symbols": len(syms),
        "timeframes": list(tfs),
        "cache_hits": 0,
        "rest_calls": 0,
        "errors": 0,
        "graded": {t: 0 for t in tfs},
        "insufficient": {t: 0 for t in tfs},
        "unclassified": {t: 0 for t in tfs},
        "supply_known": 0,
        "grade_hist": {t: {g: 0 for g in VALID_GRADES} for t in tfs},
    }
    results: dict[str, dict[str, McapTfResult]] = {s: {} for s in syms}

    tasks: list[tuple[str, str]] = []
    for sym in syms:
        for tf in tfs:
            closes = cache.get(sym, tf)
            if closes is None:
                tasks.append((sym, tf))
            else:
                stats["cache_hits"] += 1
                results[sym][tf] = evaluate_closes(
                    sym,
                    tf,
                    closes,
                    circulating_supply=supplies.get(sym),
                    contract_multiplier=int(multipliers.get(sym) or 1),
                    data_source="cache",
                )

    log.info(
        "mcap_tf symbols=%s tf=%s cache_hit=%s rest_needed=%s workers=%s",
        len(syms),
        tfs,
        stats["cache_hits"],
        len(tasks),
        workers,
    )

    def one(sym: str, tf: str) -> McapTfResult:
        limiter.acquire()
        kl = _rest_klines(fapi_rest, sym, tf, limit=KLINE_LIMIT)
        closes = closes_from_klines(kl, decision_time=decision_time)
        cache.set(sym, tf, closes)
        return evaluate_closes(
            sym,
            tf,
            closes,
            circulating_supply=supplies.get(sym),
            contract_multiplier=int(multipliers.get(sym) or 1),
            data_source="rest",
        )

    if tasks:
        with ThreadPoolExecutor(max_workers=max(1, int(workers))) as ex:
            futs = {ex.submit(one, s, t): (s, t) for s, t in tasks}
            for i, fut in enumerate(as_completed(futs), 1):
                sym, tf = futs[fut]
                try:
                    results[sym][tf] = fut.result()
                    stats["rest_calls"] += 1
                except Exception as e:
                    stats["errors"] += 1
                    # 拉取失败时退回过期缓存，宁可用旧等级也不要伪造等级
                    stale = cache.get(sym, tf, ignore_ttl=True)
                    if stale:
                        results[sym][tf] = evaluate_closes(
                            sym,
                            tf,
                            stale,
                            circulating_supply=supplies.get(sym),
                            contract_multiplier=int(multipliers.get(sym) or 1),
                            data_source="cache_stale",
                        )
                    else:
                        log.warning("mcap_tf fetch fail %s/%s: %s", sym, tf, e)
                        results[sym][tf] = McapTfResult(
                            symbol=sym,
                            timeframe=tf,
                            bars=0,
                            ma6=None,
                            ma12=None,
                            ma26=None,
                            grade=None,
                            supply_known=False,
                            reason=f"fetch_error:{e}"[:120],
                            data_source="error",
                        )
                if i % 200 == 0:
                    log.info("mcap_tf progress %s/%s", i, len(tasks))

    for sym in syms:
        known = False
        for tf in tfs:
            r = results[sym].get(tf)
            if r is None:
                continue
            known = known or r.supply_known
            if r.grade in VALID_GRADES:
                stats["graded"][tf] += 1
                stats["grade_hist"][tf][r.grade] += 1
            elif r.bars < REQUIRED_BARS:
                stats["insufficient"][tf] += 1
            else:
                stats["unclassified"][tf] += 1
        if known:
            stats["supply_known"] += 1

    log.info("mcap_tf %s", {k: v for k, v in stats.items() if k != "grade_hist"})
    return results, stats


# --------------------------------------------------------------------------
# 板面字段
# --------------------------------------------------------------------------


def _round_mcap(v: Optional[float]) -> Optional[float]:
    if v is None:
        return None
    return round(float(v), 2)


def row_fields(per_tf: Optional[dict[str, McapTfResult]]) -> dict[str, Any]:
    """把三周期结果摊成板面行字段（三个扁平等级 + 一个明细对象供 tooltip 用）。

    板面每轮下发 1050 行且网关不压缩，所以明细只写"有信息量"的键：
    供应量未知时省掉三条 ``ma*``（本来就是 null），整行都没算过时明细直接为 ``{}``。
    等级字段本身永远存在（``None`` 表示判不出级），列结构因此在七个分区里完全一致。
    """
    per_tf = per_tf or {}
    out: dict[str, Any] = {}
    detail: dict[str, Any] = {}
    for tf in TIMEFRAMES:
        r = per_tf.get(tf)
        out[GRADE_FIELD[tf]] = r.grade if r else None
        if r is None:
            continue
        d: dict[str, Any] = {"grade": r.grade, "bars": r.bars, "reason": r.reason}
        if r.supply_known:
            d["supply_known"] = True
            d["ma6"] = _round_mcap(r.ma6)
            d["ma12"] = _round_mcap(r.ma12)
            d["ma26"] = _round_mcap(r.ma26)
        detail[tf] = d
    out["mcap_tf"] = detail
    return out


def result_to_dict(r: McapTfResult) -> dict[str, Any]:
    return {
        "symbol": r.symbol,
        "timeframe": r.timeframe,
        "bars": r.bars,
        "ma6": r.ma6,
        "ma12": r.ma12,
        "ma26": r.ma26,
        "grade": r.grade,
        "supply_known": r.supply_known,
        "reason": r.reason,
        "data_source": r.data_source,
    }
