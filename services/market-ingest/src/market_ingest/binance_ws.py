"""Binance futures WebSocket consumer with silent-timeout watchdog.

Design note (v1.2 §8.3): after 2026-04-23, some legacy /ws routes can connect
but deliver no kline/markPrice/aggTrade data. We:
  1) prefer /stream combined endpoint
  2) treat no messages within silent_timeout as failure → reconnect
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlencode

from .store import HotStore

log = logging.getLogger("market_ingest.ws")

MessageHandler = Callable[[dict[str, Any]], Awaitable[None] | None]


class BinanceWsClient:
    def __init__(
        self,
        ws_base: str,
        store: HotStore,
        *,
        silent_timeout_sec: float = 10.0,
        stream_paths: tuple[str, ...] = ("/stream", "/ws"),
    ):
        self.ws_base = ws_base.rstrip("/")
        self.store = store
        self.silent_timeout_sec = silent_timeout_sec
        self.stream_paths = stream_paths
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run_mark_and_ticker(self) -> None:
        """Subscribe !markPrice@arr@1s and !miniTicker@arr via combined stream."""
        streams = ["!markPrice@arr@1s", "!miniTicker@arr"]
        await self._run_streams(streams)

    async def _run_streams(self, streams: list[str]) -> None:
        # Prefer websockets lib; fall back to error if missing
        try:
            import websockets  # type: ignore
        except ImportError as e:
            self.store.note_ws(connected=False, error=f"websockets not installed: {e}")
            log.error("install websockets: pip install websockets")
            while not self._stop.is_set():
                await asyncio.sleep(5)
            return

        path_idx = 0
        while not self._stop.is_set():
            path = self.stream_paths[path_idx % len(self.stream_paths)]
            if path == "/stream":
                url = f"{self.ws_base}/stream?{urlencode({'streams': '/'.join(streams)})}"
            else:
                # legacy: only first stream on /ws
                url = f"{self.ws_base}/ws/{streams[0]}"
            self.store.note_ws(stream_url=url, connected=False, error=None)
            log.info("ws connect %s", url)
            try:
                async with websockets.connect(
                    url,
                    ping_interval=20,
                    ping_timeout=20,
                    max_queue=1024,
                    close_timeout=5,
                ) as ws:
                    self.store.note_ws(connected=True, error=None)
                    last_msg = time.time()
                    while not self._stop.is_set():
                        timeout = self.silent_timeout_sec
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                        except asyncio.TimeoutError:
                            self.store.note_ws(
                                silent_timeouts=self.store.ws_stats.get("silent_timeouts", 0)
                                + 1,
                                connected=False,
                                error=f"silent >{timeout}s on {url}",
                            )
                            log.warning("ws silent timeout on %s — reconnect", url)
                            path_idx += 1
                            break
                        last_msg = time.time()
                        self.store.note_ws(
                            last_message_at=last_msg,
                            messages=self.store.ws_stats.get("messages", 0) + 1,
                            connected=True,
                        )
                        await self._dispatch(raw)
            except Exception as e:
                self.store.note_ws(
                    connected=False,
                    error=str(e),
                    reconnects=self.store.ws_stats.get("reconnects", 0) + 1,
                )
                log.exception("ws error: %s", e)
                path_idx += 1
                await asyncio.sleep(min(2 + path_idx, 15))

    async def _dispatch(self, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return
        # combined stream wrap: {"stream":"...", "data": ...}
        data = msg.get("data", msg)
        if isinstance(data, list):
            # markPrice array or miniTicker array
            for item in data:
                await self._one(item)
            return
        if isinstance(data, dict):
            await self._one(data)

    async def _one(self, item: dict[str, Any]) -> None:
        e = item.get("e")
        sym = item.get("s")
        if not sym:
            return
        if e == "markPriceUpdate" or ("p" in item and "i" in item and "r" in item and e is None):
            # mark price event fields: s, p (mark), i (index), r (funding)
            self.store.update_mark(
                sym,
                {
                    "mark": item.get("p"),
                    "index": item.get("i"),
                    "funding": item.get("r"),
                    "event": e or "markPrice",
                },
            )
            return
        if e in ("24hrMiniTicker", "24hrTicker") or "c" in item:
            self.store.update_ticker(
                sym,
                {
                    "last": item.get("c") or item.get("lastPrice"),
                    "event": e or "ticker",
                    "priceChangePercent": item.get("P"),
                    "quoteVolume": item.get("q"),
                    "volume": item.get("v"),
                    "raw": {
                        k: item.get(k)
                        for k in ("o", "h", "l", "v", "q", "P")
                        if k in item
                    },
                },
            )
