"""point-in-time 时钟与已收盘 K 线过滤 —— 全链路共用的唯一口径。

权威依据
--------
* 文档 A §7.2【上线阻断】：「所有路径必须改为 ``bar.close_time <= decision_time_utc``
  的共同过滤器；不能靠盲目删除最后一行猜测是否收盘。」
* 文档 A §8：「只使用决策时刻已经收盘且当时可获得的 K 线；每个输入保存 as_of_utc、
  首尾 closeTime 与数据源。」
* 文档 B §6 第 2 步、§12（未来 K 线 → 节点失败）、§23（P0 工作包「closed-bar/PIT 时钟」）。

改造前的现网问题（文档 A §7.2【现网事实】）
-------------------------------------------
* ``gate2.closes_from_klines`` **保留**正在形成的最后一根 1h K 线；
* ``mcap_timeframe.closes_from_klines`` **无条件丢弃**最后一根。

两者对「最后一根到底收没收盘」的判断都不看 ``closeTime``：前者可能读进未来数据，
后者在最后一根其实已经收盘时白白丢掉一根有效样本，于是 A～F 与 Gate2 用的是
**两个不同时点**的序列。本模块把判断统一到时间戳本身。

Binance kline 行格式
--------------------
``[openTime_ms, o, h, l, c, v, closeTime_ms, ...]``；``closeTime`` 是该根的最后一毫秒
（如 1h 的 08:00 根 closeTime = 08:59:59.999）。因此 ``decision_time=09:00:00Z`` 时
该根**已收盘**，判据 ``closeTime <= decision_time_ms`` 成立。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

log = logging.getLogger("coin_selection.pit")

KLINE_OPEN_TIME = 0
KLINE_CLOSE = 4
KLINE_CLOSE_TIME = 6


class FutureInputError(RuntimeError):
    """输入的 as_of 晚于 decision_time。文档 B §12【上线阻断】：节点失败，不得吞掉。"""


def to_epoch_ms(t: Any) -> Optional[int]:
    """把 datetime / ISO 字符串 / 秒 / 毫秒统一成 epoch 毫秒。无法解析返回 ``None``。"""
    if t is None:
        return None
    if isinstance(t, datetime):
        dt = t if t.tzinfo else t.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    if isinstance(t, bool):
        return None
    if isinstance(t, (int, float)):
        v = float(t)
        # 1e11 ≈ 1973 年的毫秒 / 5138 年的秒；用它区分秒与毫秒足够安全。
        return int(v if v > 1e11 else v * 1000)
    s = str(t).strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def bar_close_time_ms(bar: Sequence[Any]) -> Optional[int]:
    try:
        return to_epoch_ms(bar[KLINE_CLOSE_TIME])
    except (IndexError, TypeError):
        return None


def only_closed_bars(
    klines: Optional[Sequence[Any]],
    decision_time: Any,
    *,
    strict: bool = False,
) -> list[Any]:
    """保留 ``close_time <= decision_time`` 的 K 线（文档 A §7.2 的共同过滤器）。

    * ``decision_time`` 为 ``None`` → 不过滤（离线 fixtures / 历史回放的兼容路径）；
    * 行里没有 ``closeTime``（列数 < 7）→ **保守保留**，并由调用方的数据合同负责，
      因为无法证明它是未来数据；
    * ``strict=True`` 时，出现 ``close_time > decision_time`` 的行会抛
      :class:`FutureInputError` 而不是静默丢弃 —— 给严格重放使用（文档 B §12）。
    """
    rows = list(klines or [])
    if not rows:
        return []
    dt_ms = to_epoch_ms(decision_time)
    if dt_ms is None:
        return rows
    out: list[Any] = []
    dropped = 0
    for k in rows:
        ct = bar_close_time_ms(k)
        if ct is None:
            out.append(k)
            continue
        if ct <= dt_ms:
            out.append(k)
        else:
            dropped += 1
            if strict:
                raise FutureInputError(
                    f"kline close_time {ct} > decision_time {dt_ms}"
                )
    if dropped:
        log.debug("only_closed_bars dropped %d future bar(s)", dropped)
    return out


def closes_from_closed_klines(
    klines: Optional[Sequence[Any]],
    decision_time: Any = None,
    *,
    strict: bool = False,
    drop_incomplete_last_when_unknown: bool = False,
) -> list[float]:
    """已收盘收盘价序列。

    ``drop_incomplete_last_when_unknown=True`` 只在**既没有 decision_time 又没有
    closeTime 列**时才退回旧的「丢最后一根」启发式；这是给纯 fixtures 的兼容路径，
    生产必须传 decision_time（文档 A §7.2）。
    """
    rows = list(klines or [])
    if not rows:
        return []
    dt_ms = to_epoch_ms(decision_time)
    has_close_time = bar_close_time_ms(rows[-1]) is not None
    if dt_ms is not None and has_close_time:
        rows = only_closed_bars(rows, decision_time, strict=strict)
    elif drop_incomplete_last_when_unknown and len(rows) >= 2:
        rows = rows[:-1]
    out: list[float] = []
    for k in rows:
        try:
            c = float(k[KLINE_CLOSE])
        except (IndexError, TypeError, ValueError):
            continue
        if c > 0:
            out.append(c)
    return out


def bars_as_of(klines: Optional[Sequence[Any]]) -> dict[str, Any]:
    """输入血缘：首尾 closeTime 与根数（文档 A §8 / 文档 B §5.2 的 RawInputManifest）。"""
    rows = list(klines or [])
    if not rows:
        return {"bars": 0, "first_close_time_utc": None, "last_close_time_utc": None}

    def iso(ms: Optional[int]) -> Optional[str]:
        if ms is None:
            return None
        return (
            datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )

    return {
        "bars": len(rows),
        "first_close_time_utc": iso(bar_close_time_ms(rows[0])),
        "last_close_time_utc": iso(bar_close_time_ms(rows[-1])),
    }


def assert_no_future_input(as_of: Any, decision_time: Any, *, what: str = "input") -> None:
    """文档 B §12：``input.as_of > decision_time`` 是自动停止条件。"""
    a, d = to_epoch_ms(as_of), to_epoch_ms(decision_time)
    if a is None or d is None:
        return
    if a > d:
        raise FutureInputError(f"{what} as_of {as_of} > decision_time {decision_time}")


__all__ = [
    "FutureInputError",
    "KLINE_CLOSE",
    "KLINE_CLOSE_TIME",
    "KLINE_OPEN_TIME",
    "assert_no_future_input",
    "bar_close_time_ms",
    "bars_as_of",
    "closes_from_closed_klines",
    "only_closed_bars",
    "to_epoch_ms",
]
