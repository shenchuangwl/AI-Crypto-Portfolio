"""Binance USD-M REST helpers (stdlib urllib)."""

from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional


class RestError(RuntimeError):
    def __init__(self, status: int, body: str, url: str):
        super().__init__(f"HTTP {status} {url}: {body[:300]}")
        self.status = status
        self.body = body
        self.url = url


def _urlopen(url: str, timeout: float) -> Any:
    ctx = ssl.create_default_context()
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "hermes-market-ingest/0.1",
            "Accept": "application/json",
        },
        method="GET",
    )
    return urllib.request.urlopen(req, timeout=timeout, context=ctx)


def rest_get_json(
    base: str,
    path: str,
    params: Optional[dict[str, Any]] = None,
    timeout: float = 20.0,
) -> Any:
    q = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
    url = base.rstrip("/") + path + (("?" + q) if q else "")
    try:
        with _urlopen(url, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RestError(e.code, body, url) from e
    except urllib.error.URLError as e:
        raise RestError(0, str(e.reason if hasattr(e, "reason") else e), url) from e
    except TimeoutError as e:
        raise RestError(0, f"timeout: {e}", url) from e


def fetch_exchange_info(base: str, timeout: float = 20.0) -> dict:
    return rest_get_json(base, "/fapi/v1/exchangeInfo", timeout=timeout)


def fetch_ticker_24hr(base: str, timeout: float = 20.0) -> list:
    return rest_get_json(base, "/fapi/v1/ticker/24hr", timeout=timeout)


def fetch_klines(
    base: str,
    symbol: str,
    interval: str = "15m",
    limit: int = 3,
    timeout: float = 20.0,
) -> list:
    return rest_get_json(
        base,
        "/fapi/v1/klines",
        params={"symbol": symbol, "interval": interval, "limit": limit},
        timeout=timeout,
    )


def fetch_premium_index(base: str, timeout: float = 20.0) -> list:
    """Mark / index / funding for all symbols."""
    return rest_get_json(base, "/fapi/v1/premiumIndex", timeout=timeout)


def now_ms() -> int:
    return int(time.time() * 1000)
