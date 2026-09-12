"""Chinese USDT-M symbols must not be double-encoded into synthetic junk klines."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "services/api-gateway"))
import mock_server as gw  # noqa: E402


def test_decode_path_symbol_undoes_browser_encoding():
    raw = "%E9%BE%99%E8%99%BEUSDT"
    assert gw.decode_path_symbol(raw) == "龙虾USDT"
    assert gw.decode_path_symbol("%25E9%25BE%2599%25E8%2599%25BEUSDT") == "龙虾USDT"
    assert gw.decode_path_symbol("BTCUSDT") == "BTCUSDT"


def test_synthetic_intervals_share_one_price_path():
    bars_30 = gw.synthetic_klines("龙虾USDT", "30m", 20, last_price=0.12)
    bars_2h = gw.synthetic_klines("龙虾USDT", "2h", 20, last_price=0.12)
    bars_6h = gw.synthetic_klines("龙虾USDT", "6h", 20, last_price=0.12)
    assert bars_30 and bars_2h and bars_6h
    for bars in (bars_30, bars_2h, bars_6h):
        last = bars[-1]["close"]
        assert 0.05 < last < 0.4, last
    # Same seed + aggregation: last 6h close is a 15m close on the shared walk,
    # so it must sit on the same scale as 30m/2h (not 80 vs 33).
    lasts = [bars_30[-1]["close"], bars_2h[-1]["close"], bars_6h[-1]["close"]]
    assert max(lasts) / min(lasts) < 2.0, lasts


if __name__ == "__main__":
    test_decode_path_symbol_undoes_browser_encoding()
    test_synthetic_intervals_share_one_price_path()
    print("ok")
