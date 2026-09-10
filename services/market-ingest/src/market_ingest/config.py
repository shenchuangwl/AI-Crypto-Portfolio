"""Market-ingest configuration (env overrides)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class Settings:
    # REST
    fapi_rest: str = os.environ.get("BINANCE_FAPI_REST", "https://fapi.binance.com")
    # WS base — post 2026-04-23 market streams use dedicated host; allow override
    # Override e.g. BINANCE_FAPI_WS=wss://fstream.binancefuture.com for some regions
    fapi_ws: str = os.environ.get(
        "BINANCE_FAPI_WS",
        os.environ.get("BINANCE_FUTURES_WS", "wss://fstream.binance.com"),
    )
    # Combined /stream only. Legacy /ws can connect then stay silent (2026-04+).
    # After silent timeout the service falls back to REST price poll, not /ws.
    ws_stream_paths: tuple[str, ...] = tuple(
        p
        for p in os.environ.get("BINANCE_FAPI_WS_PATHS", "/stream").split(",")
        if p.strip()
    ) or ("/stream",)

    quote_asset: str = "USDT"
    contract_type: str = "PERPETUAL"
    status: str = "TRADING"
    # underlyingType allow: COIN or missing.
    # TRADIFI_PERPETUAL is a contractType (already filtered above); listed
    # here only as a defensive underlyingType guard if Binance schema drifts.
    exclude_underlying_types: tuple[str, ...] = (
        "TRADIFI_PERPETUAL",
        "INDEX",
    )

    contract_ttl_sec: int = int(os.environ.get("CONTRACT_TTL_SEC", "900"))
    ws_silent_timeout_sec: float = float(os.environ.get("WS_SILENT_TIMEOUT_SEC", "12"))
    rest_timeout_sec: float = float(os.environ.get("REST_TIMEOUT_SEC", "20"))

    # Local hot store (file + memory). Redis optional later.
    data_dir: str = os.environ.get(
        "MARKET_INGEST_DATA_DIR",
        os.path.join(os.environ.get("HERMES_ROOT", "."), "data", "market-ingest"),
    )
    http_host: str = os.environ.get("MARKET_INGEST_HOST", "127.0.0.1")
    http_port: int = int(os.environ.get("MARKET_INGEST_PORT", "18100"))

    # Limit concurrent kline REST when bootstrapping
    kline_bootstrap_symbols: int = int(os.environ.get("KLINE_BOOTSTRAP_N", "5"))
    kline_interval: str = os.environ.get("KLINE_INTERVAL", "15m")
    kline_limit: int = int(os.environ.get("KLINE_LIMIT", "3"))

    # Subscribe markPrice + miniTicker for whole market (low weight)
    enable_ws: bool = os.environ.get("MARKET_INGEST_ENABLE_WS", "1") not in (
        "0",
        "false",
        "no",
    )


def get_settings() -> Settings:
    return Settings()
