"""Offline Gate3 circulating-mcap + host selection tests (no network)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from coin_selection.gate3 import (  # noqa: E402
    CG_DEMO,
    CG_PRO,
    _cg_base,
    compute_circulating_mcap,
    evaluate_symbol,
    parse_multiplier,
    use_pro_host,
)


def test_parse_multiplier():
    assert parse_multiplier("BTC") == 1
    assert parse_multiplier("1000SHIB") == 1000
    assert parse_multiplier("1000BONK") == 1000
    assert parse_multiplier("1000000MOG") == 1_000_000


def test_compute_circulating_mcap_spot_like():
    # BTC: supply × mark
    mcap = compute_circulating_mcap(20_000_000, last_price=63000, mark_price=63000, contract_multiplier=1)
    assert mcap == 20_000_000 * 63000


def test_compute_circulating_mcap_pseudo_coin():
    # 1000SHIB last ≈ 0.0046 → underlying SHIB ≈ 4.6e-6
    supply = 589_239_588_785_163.0
    last = 0.0046
    mcap = compute_circulating_mcap(supply, last_price=last, contract_multiplier=1000)
    assert mcap is not None
    assert abs(mcap - supply * (last / 1000)) < 1e-3


def test_compute_circulating_mcap_missing():
    assert compute_circulating_mcap(None, last_price=1) is None
    assert compute_circulating_mcap(0, last_price=1) is None
    assert compute_circulating_mcap(10, last_price=None, mark_price=None) is None


def test_demo_host_is_default(monkeypatch=None):
    os.environ.pop("COINGECKO_USE_PRO", None)
    os.environ.pop("COINGECKO_BASE", None)
    assert use_pro_host() is False
    assert _cg_base() == CG_DEMO
    os.environ["COINGECKO_USE_PRO"] = "1"
    assert use_pro_host() is True
    assert _cg_base() == CG_PRO
    os.environ["COINGECKO_USE_PRO"] = "0"
    assert _cg_base() == CG_DEMO
    os.environ.pop("COINGECKO_USE_PRO", None)


def test_evaluate_symbol_computes_dual_mcap():
    mapping = {
        "coin_id": "bitcoin",
        "mapping_status": "MAPPED",
        "base": "BTC",
        "contract_multiplier": 1,
    }
    market = {"circulating_supply": 20_000_000, "market_cap": 1_260_000_000_000}
    r = evaluate_symbol(
        "BTCUSDT",
        mapping=mapping,
        market=market,
        last_price=63000,
        mark_price=63000,
        path_points=[],
        ret_24h=0.01,
    )
    assert r.supply_missing is False
    assert r.circulating_supply == 20_000_000
    assert r.market_cap_calculated == 20_000_000 * 63000
    assert r.market_cap_coingecko == 1_260_000_000_000
    assert r.gap_binance is not None
    assert r.gap_binance < 0.01


if __name__ == "__main__":
    test_parse_multiplier()
    test_compute_circulating_mcap_spot_like()
    test_compute_circulating_mcap_pseudo_coin()
    test_compute_circulating_mcap_missing()
    test_demo_host_is_default()
    test_evaluate_symbol_computes_dual_mcap()
    print("ok")
