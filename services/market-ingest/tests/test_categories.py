"""Offline category composition (no network)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from market_ingest.categories import attach_categories, compose_categories, format_categories, zh_label


def test_btc_like_tv():
    labs = compose_categories(underlying_sub_type=["PoW"], spot_tags=["Payments", "mining-zone"])
    assert "加密货币" in labs
    assert "工作量证明" in labs
    assert "付款" in labs
    assert "挖矿" in labs
    text = format_categories(labs)
    assert "加密货币" in text and "工作量证明" in text


def test_doge_meme():
    labs = compose_categories(underlying_sub_type=["Meme"], spot_tags=["Meme", "mining-zone", "Seed"])
    assert "迷因" in labs
    assert "挖矿" in labs
    assert "Seed" not in labs  # operational tag skipped
    assert labs.count("迷因") == 1


def test_layer1_not_duplicated():
    labs = compose_categories(underlying_sub_type=["Layer-1"], spot_tags=["Layer1_Layer2", "pos"])
    assert "第一层级" in labs
    assert "第一层级 / 第二层级" not in labs
    assert "权益证明" in labs


def test_attach_pseudo_coin_lookup():
    rows = attach_categories(
        [{"symbol": "1000SHIBUSDT", "base_asset": "1000SHIB", "underlying_sub_type": ["Meme"]}],
        spot_tags={"SHIB": ["Meme"]},
    )
    assert rows[0]["category"].startswith("加密货币")
    assert "迷因" in rows[0]["categories"]


def test_zh_skip_empty():
    assert zh_label("") is None
    assert zh_label("Seed") is None
    assert zh_label("DeFi") == "去中心化金融"


if __name__ == "__main__":
    test_btc_like_tv()
    test_doge_meme()
    test_layer1_not_duplicated()
    test_attach_pseudo_coin_lookup()
    test_zh_skip_empty()
    print("ok")
