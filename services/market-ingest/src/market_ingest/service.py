"""Market-ingest service orchestration + tiny HTTP status API."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from .binance_rest import (
    RestError,
    fetch_exchange_info,
    fetch_klines,
    fetch_premium_index,
    fetch_ticker_24hr,
)
from .binance_ws import BinanceWsClient
from .config import Settings, get_settings
from .store import HotStore
from .categories import attach_categories, fetch_spot_tags, load_cached_spot_tags, save_spot_tags
from .universe import contract_set_version, filter_universe, summarize_exchange_info

log = logging.getLogger("market_ingest")

# Binance USDT-M native intervals used by the terminal chart bar.
ALLOWED_KLINE_INTERVALS = frozenset({"15m", "30m", "1h", "2h", "4h", "6h", "1d"})


def normalize_klines(bars: Any) -> list[dict[str, Any]]:
    """Binance kline array → OHLCV dicts with unix-seconds `time`."""
    normalized: list[dict[str, Any]] = []
    for row in bars or []:
        if isinstance(row, (list, tuple)) and len(row) >= 6:
            normalized.append(
                {
                    "time": int(row[0]) // 1000,
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "volume": float(row[5]),
                }
            )
        elif isinstance(row, dict):
            t = row.get("time") or row.get("openTime") or 0
            if t > 10_000_000_000:
                t = int(t) // 1000
            normalized.append(
                {
                    "time": int(t),
                    "open": float(row.get("open") or row.get("o") or 0),
                    "high": float(row.get("high") or row.get("h") or 0),
                    "low": float(row.get("low") or row.get("l") or 0),
                    "close": float(row.get("close") or row.get("c") or 0),
                    "volume": float(row.get("volume") or row.get("v") or 0),
                }
            )
    return normalized


class MarketIngestService:
    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or get_settings()
        self.store = HotStore(self.settings.data_dir)
        self._ws: Optional[BinanceWsClient] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._http: Optional[ThreadingHTTPServer] = None

    def refresh_universe(self) -> dict[str, Any]:
        info = fetch_exchange_info(
            self.settings.fapi_rest, timeout=self.settings.rest_timeout_sec
        )
        symbols = filter_universe(
            info,
            quote_asset=self.settings.quote_asset,
            contract_type=self.settings.contract_type,
            status=self.settings.status,
            exclude_underlying_types=self.settings.exclude_underlying_types,
        )
        tag_path = self.store.data_dir / "spot_tags.json"
        spot_tags = load_cached_spot_tags(tag_path) or {}
        if not spot_tags:
            try:
                spot_tags = fetch_spot_tags(timeout=max(self.settings.rest_timeout_sec, 30.0))
                if spot_tags:
                    save_spot_tags(tag_path, spot_tags)
            except Exception as e:
                log.warning("spot tags refresh failed: %s", e)
                spot_tags = {}
        symbols = attach_categories(symbols, spot_tags)
        ver = contract_set_version(symbols)
        stats = summarize_exchange_info(info)
        tagged = sum(1 for s in symbols if s.get("categories"))
        stats["category_tagged"] = tagged
        stats["spot_tag_bases"] = len(spot_tags)
        self.store.set_universe(symbols, ver, stats=stats)
        log.info(
            "universe %s symbols (coin_usdt_perp=%s tradifi_ct=%s) version=%s",
            len(symbols),
            stats.get("coin_usdt_perp_trading"),
            stats.get("tradifi_perpetual_contract_type"),
            ver[:12],
        )
        return {"count": len(symbols), "version": ver, "stats": stats}

    def bootstrap_rest_prices(self) -> None:
        """Fill mark/ticker from REST (works even if WS blocked)."""
        try:
            prem = fetch_premium_index(
                self.settings.fapi_rest, timeout=self.settings.rest_timeout_sec
            )
            if isinstance(prem, list):
                for row in prem:
                    sym = row.get("symbol")
                    if not sym:
                        continue
                    self.store.update_mark(
                        sym,
                        {
                            "mark": row.get("markPrice"),
                            "index": row.get("indexPrice"),
                            "funding": row.get("lastFundingRate"),
                            "event": "rest_premiumIndex",
                        },
                    )
            log.info("premiumIndex rows=%s", len(prem) if isinstance(prem, list) else 0)
        except RestError as e:
            log.warning("premiumIndex failed: %s", e)

        try:
            tickers = fetch_ticker_24hr(
                self.settings.fapi_rest, timeout=self.settings.rest_timeout_sec
            )
            if isinstance(tickers, list):
                for row in tickers:
                    sym = row.get("symbol")
                    if not sym:
                        continue
                    self.store.update_ticker(
                        sym,
                        {
                            "last": row.get("lastPrice"),
                            "event": "rest_ticker24hr",
                            "priceChangePercent": row.get("priceChangePercent"),
                            "quoteVolume": row.get("quoteVolume"),
                            "volume": row.get("volume"),
                            "raw": {
                                "volume": row.get("volume"),
                                "quoteVolume": row.get("quoteVolume"),
                                "priceChangePercent": row.get("priceChangePercent"),
                            },
                        },
                    )
            log.info("ticker24hr rows=%s", len(tickers) if isinstance(tickers, list) else 0)
        except RestError as e:
            log.warning("ticker24hr failed: %s", e)

        self.store.export_prices()

    def bootstrap_klines(self) -> None:
        n = min(self.settings.kline_bootstrap_symbols, 5)
        syms = self.store.get_universe_symbols()[:n]
        ok = 0
        for sym in syms:
            try:
                self.fetch_klines_live(
                    sym,
                    interval=self.settings.kline_interval,
                    limit=self.settings.kline_limit,
                )
                ok += 1
            except Exception as e:
                log.warning("klines %s failed: %s", sym, e)
            time.sleep(0.05)
        log.info("klines bootstrap ok=%s/%s", ok, len(syms))

    def fetch_klines_live(
        self, symbol: str, interval: str = "15m", limit: int = 200
    ) -> list[dict[str, Any]]:
        """On-demand REST klines (normalized OHLCV). Cached by symbol+interval+limit."""
        cache_key = f"{symbol}:{interval}:{limit}"
        bars = fetch_klines(
            self.settings.fapi_rest,
            symbol,
            interval=interval,
            limit=max(1, min(int(limit), 1500)),
            timeout=min(self.settings.rest_timeout_sec, 15.0),
        )
        normalized = normalize_klines(bars)
        self.store.set_klines(cache_key, normalized)
        return normalized

    def start_http(self) -> None:
        service = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: Any) -> None:
                log.debug("http " + fmt, *args)

            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                path = parsed.path.rstrip("/") or "/"
                if path in ("/health", "/"):
                    return self._json(200, {"status": "ok", "service": "market-ingest"})
                if path == "/v1/status":
                    return self._json(200, service.store.snapshot())
                if path == "/v1/universe":
                    return self._json(
                        200,
                        {
                            "version": service.store.universe_version,
                            "count": len(service.store.universe),
                            "stats": service.store.universe_stats,
                            "symbols": service.store.universe,
                        },
                    )
                if path == "/v1/universe/stats":
                    return self._json(
                        200,
                        {
                            "version": service.store.universe_version,
                            "count": len(service.store.universe),
                            "stats": service.store.universe_stats,
                        },
                    )
                if path == "/v1/prices":
                    return self._json(200, {"prices": service.store.export_prices()})
                qs = parse_qs(parsed.query)
                if path == "/v1/klines":
                    sym = (qs.get("symbol") or [""])[0].upper()
                    interval = (qs.get("interval") or ["15m"])[0]
                    if interval not in ALLOWED_KLINE_INTERVALS:
                        return self._json(400, {"error": "bad_interval", "allowed": sorted(ALLOWED_KLINE_INTERVALS)})
                    try:
                        limit = max(1, min(int((qs.get("limit") or ["200"])[0]), 1500))
                    except ValueError:
                        limit = 200
                    if not sym:
                        return self._json(400, {"error": "symbol_required"})
                    try:
                        bars = service.fetch_klines_live(
                            sym, interval=interval, limit=limit
                        )
                    except Exception as e:
                        log.warning("live klines %s %s failed: %s", sym, interval, e)
                        cached = (
                            service.store.klines.get(f"{sym}:{interval}:{limit}") or []
                        )
                        return self._json(
                            200,
                            {
                                "symbol": sym,
                                "interval": interval,
                                "source": "cache" if cached else "empty",
                                "bars": cached,
                                "error": str(e),
                            },
                        )
                    return self._json(
                        200,
                        {
                            "symbol": sym,
                            "interval": interval,
                            "source": "binance_fapi",
                            "bars": bars,
                        },
                    )
                self._json(404, {"error": "not_found"})

            def _json(self, code: int, body: Any) -> None:
                raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(raw)

        self._http = ThreadingHTTPServer(
            (self.settings.http_host, self.settings.http_port), Handler
        )
        t = threading.Thread(target=self._http.serve_forever, daemon=True)
        t.start()
        log.info(
            "http status on http://%s:%s/v1/status",
            self.settings.http_host,
            self.settings.http_port,
        )

    async def run_async(self, once: bool = False) -> None:
        self.refresh_universe()
        self.bootstrap_rest_prices()
        self.bootstrap_klines()
        self.start_http()
        if once or not self.settings.enable_ws:
            log.info("once/no-ws mode — resting after bootstrap")
            if once:
                return
            while True:
                await asyncio.sleep(self.settings.contract_ttl_sec)
                try:
                    self.refresh_universe()
                    self.bootstrap_rest_prices()
                except Exception:
                    log.exception("refresh cycle failed")
            return

        self._ws = BinanceWsClient(
            self.settings.fapi_ws,
            self.store,
            silent_timeout_sec=self.settings.ws_silent_timeout_sec,
            stream_paths=self.settings.ws_stream_paths,
        )

        async def refresh_loop() -> None:
            """Universe TTL refresh + REST price poller when WS is silent."""
            rest_poll = float(os.environ.get("REST_PRICE_POLL_SEC", "15"))
            silent_limit = float(os.environ.get("WS_SILENT_FOR_REST_SEC", "10"))
            rest_poll = max(10.0, min(rest_poll, 60.0))
            last_rest = 0.0
            while True:
                await asyncio.sleep(min(3.0, rest_poll / 3))
                now = time.time()
                try:
                    if now - float(self.store.universe_fetched_at or 0) >= self.settings.contract_ttl_sec:
                        self.refresh_universe()
                    ws = self.store.ws_stats or {}
                    last_msg = ws.get("last_message_at")
                    silent = (
                        not last_msg
                        or (now - float(last_msg)) >= silent_limit
                        or not ws.get("connected")
                    )
                    poll = rest_poll
                    if int(ws.get("silent_timeouts") or 0) >= 3:
                        poll = min(poll, 12.0)
                    if silent and (now - last_rest) >= poll:
                        log.info("WS silent/degraded — REST price poll (every %.0fs)", poll)
                        self.bootstrap_rest_prices()
                        last_rest = now
                    elif not silent and (now - last_rest) >= self.settings.contract_ttl_sec:
                        self.store.export_prices()
                        last_rest = now
                except Exception:
                    log.exception("scheduled refresh/poll failed")

        await asyncio.gather(self._ws.run_mark_and_ticker(), refresh_loop())

    def run(self, once: bool = False) -> None:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        asyncio.run(self.run_async(once=once))
