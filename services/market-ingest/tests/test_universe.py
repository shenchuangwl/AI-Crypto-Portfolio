"""Offline unit checks for COIN USDT-M filter (no network)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from market_ingest.universe import (  # noqa: E402
    classify_market_kind,
    filter_universe,
    is_allowed_symbol,
    summarize_exchange_info,
)


def _sym(**kw):
    base = {
        "symbol": "BTCUSDT",
        "pair": "BTCUSDT",
        "baseAsset": "BTC",
        "quoteAsset": "USDT",
        "contractType": "PERPETUAL",
        "status": "TRADING",
        "underlyingType": "COIN",
        "pricePrecision": 1,
        "quantityPrecision": 3,
    }
    base.update(kw)
    return base


def test_coin_perp_allowed():
    assert is_allowed_symbol(_sym())
    assert classify_market_kind(_sym()) == "coin_perp"


def test_tradifi_is_contract_type():
    aapl = _sym(
        symbol="AAPLUSDT",
        baseAsset="AAPL",
        contractType="TRADIFI_PERPETUAL",
        underlyingType="COIN",
    )
    assert not is_allowed_symbol(aapl)
    assert classify_market_kind(aapl) == "tradifi_perp"


def test_missing_underlying_allowed():
    assert is_allowed_symbol(_sym(underlyingType=None))


def test_summarize():
    info = {
        "symbols": [
            _sym(),
            _sym(symbol="ETHUSDT", baseAsset="ETH"),
            _sym(
                symbol="AAPLUSDT",
                baseAsset="AAPL",
                contractType="TRADIFI_PERPETUAL",
            ),
            _sym(symbol="DEADUSDT", status="PENDING_TRADING"),
        ]
    }
    stats = summarize_exchange_info(info)
    assert stats["selected_count"] == 2
    assert stats["tradifi_perpetual_contract_type"] == 1
    uni = filter_universe(info)
    assert [s["symbol"] for s in uni] == ["BTCUSDT", "ETHUSDT"]


if __name__ == "__main__":
    test_coin_perp_allowed()
    test_tradifi_is_contract_type()
    test_missing_underlying_allowed()
    test_summarize()
    print("ok universe tests")
