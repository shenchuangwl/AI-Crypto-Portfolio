"""Hot store: memory + atomic JSON files (Redis later)."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional


class HotStore:
    def __init__(self, data_dir: str):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.universe: list[dict[str, Any]] = []
        self.universe_version: str = ""
        self.universe_fetched_at: float = 0.0
        self.universe_stats: dict[str, Any] = {}
        self.tickers: dict[str, dict[str, Any]] = {}
        self.mark: dict[str, dict[str, Any]] = {}
        self.klines: dict[str, list] = {}
        self.ws_stats: dict[str, Any] = {
            "connected": False,
            "last_message_at": None,
            "messages": 0,
            "reconnects": 0,
            "silent_timeouts": 0,
            "stream_url": None,
            "error": None,
        }

    def set_universe(
        self,
        symbols: list[dict[str, Any]],
        version: str,
        stats: Optional[dict[str, Any]] = None,
    ) -> None:
        with self._lock:
            self.universe = symbols
            self.universe_version = version
            self.universe_fetched_at = time.time()
            if stats is not None:
                self.universe_stats = stats
            payload = {
                "version": version,
                "fetched_at": self.universe_fetched_at,
                "count": len(symbols),
                "stats": self.universe_stats,
                "symbols": symbols,
            }
            self._atomic_write("universe.json", payload)

    def update_ticker(self, symbol: str, payload: dict[str, Any]) -> None:
        with self._lock:
            self.tickers[symbol] = {**payload, "updated_at": time.time()}

    def update_mark(self, symbol: str, payload: dict[str, Any]) -> None:
        with self._lock:
            self.mark[symbol] = {**payload, "updated_at": time.time()}

    def set_klines(self, symbol: str, bars: list) -> None:
        with self._lock:
            self.klines[symbol] = bars

    def note_ws(self, **kwargs: Any) -> None:
        with self._lock:
            self.ws_stats.update(kwargs)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "universe_count": len(self.universe),
                "universe_version": self.universe_version,
                "universe_fetched_at": self.universe_fetched_at,
                "universe_stats": dict(self.universe_stats),
                "ticker_count": len(self.tickers),
                "mark_count": len(self.mark),
                "kline_symbols": len(self.klines),
                "ws": dict(self.ws_stats),
                "sample_symbols": [s["symbol"] for s in self.universe[:10]],
            }

    def get_universe_symbols(self) -> list[str]:
        with self._lock:
            return [s["symbol"] for s in self.universe]

    def export_prices(self) -> dict[str, Any]:
        with self._lock:
            out = {}
            for sym, t in self.tickers.items():
                m = self.mark.get(sym) or {}
                raw = t.get("raw") if isinstance(t.get("raw"), dict) else {}
                chg_pct = _f(
                    t.get("priceChangePercent")
                    or raw.get("priceChangePercent")
                    or raw.get("P")
                )
                out[sym] = {
                    "last": _f(t.get("last") or t.get("c") or t.get("lastPrice")),
                    "mark": _f(m.get("mark") or m.get("p") or m.get("markPrice")),
                    "index": _f(m.get("index") or m.get("i") or m.get("indexPrice")),
                    "funding": _f(m.get("funding") or m.get("r") or m.get("lastFundingRate")),
                    "chg_24h": (chg_pct / 100.0) if chg_pct is not None else None,
                    "quote_volume_24h": _f(
                        t.get("quoteVolume") or raw.get("quoteVolume") or raw.get("q")
                    ),
                    "volume_24h": _f(t.get("volume") or raw.get("volume") or raw.get("v")),
                    "ticker_updated_at": t.get("updated_at"),
                    "mark_updated_at": m.get("updated_at"),
                }
            # marks without ticker
            for sym, m in self.mark.items():
                if sym not in out:
                    out[sym] = {
                        "last": None,
                        "mark": _f(m.get("mark") or m.get("p") or m.get("markPrice")),
                        "index": _f(m.get("index") or m.get("i") or m.get("indexPrice")),
                        "funding": _f(m.get("funding") or m.get("r") or m.get("lastFundingRate")),
                        "chg_24h": None,
                        "quote_volume_24h": None,
                        "volume_24h": None,
                        "ticker_updated_at": None,
                        "mark_updated_at": m.get("updated_at"),
                    }
            path = self.data_dir / "prices.json"
            self._atomic_write_unlocked(path, {"updated_at": time.time(), "prices": out})
            return out

    def _atomic_write(self, name: str, obj: Any) -> None:
        path = self.data_dir / name
        self._atomic_write_unlocked(path, obj)

    def _atomic_write_unlocked(self, path: Path, obj: Any) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)


def _f(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
