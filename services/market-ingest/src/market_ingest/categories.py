"""Sector / category labels for the quotes 分类 column.

Sources already in-hand (no extra paid API):
- Binance fapi `underlyingSubType` (Layer-1 / Meme / DeFi / …)
- Binance public spot product `tags` (Layer1_Layer2, Payments, mining-zone, …)

Rendered like TradingView: comma-separated Chinese tags, e.g.
「加密货币, 第一层级, 工作量证明」.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Any, Iterable, Optional

log = logging.getLogger("market_ingest.categories")

SPOT_PRODUCTS_URL = (
    "https://www.binance.com/bapi/asset/v2/public/asset-service/product/get-products"
    "?includeEtf=true"
)

# Operational / listing-status tags — not sector descriptions.
_SKIP_TAGS = {
    "",
    "seed",
    "launchpool",
    "launchpad",
    "hodler",
    "monitoring",
    "innovation-zone",
    "innovation_zone",
    "newlisting",
    "new-listing",
    "megadrop",
    "bstocks",
    "tcommodities",
}

# English / Binance slug → Chinese (TradingView-style).
_ZH: dict[str, str] = {
    "cryptocurrency": "加密货币",
    "layer-1": "第一层级",
    "layer1": "第一层级",
    "layer 1": "第一层级",
    "layer-2": "第二层级",
    "layer2": "第二层级",
    "layer 2": "第二层级",
    "layer1_layer2": "第一层级 / 第二层级",
    "smart-contract": "智能合约平台",
    "smart contract platform": "智能合约平台",
    "pow": "工作量证明",
    "proof of work": "工作量证明",
    "pos": "权益证明",
    "proof of stake": "权益证明",
    "mining-zone": "挖矿",
    "mining": "挖矿",
    "payments": "付款",
    "payment": "付款",
    "defi": "去中心化金融",
    "infrastructure": "基础设施",
    "ai": "人工智能",
    "meme": "迷因",
    "memes": "迷因",
    "gaming": "游戏",
    "nft": "NFT",
    "metaverse": "元宇宙",
    "storage": "存储",
    "storage-zone": "存储",
    "rwa": "真实世界资产",
    "privacy": "隐私",
    "stablecoin": "稳定币",
    "asset-backed-stablecoin": "资产支持的稳定币",
    "cex": "中心化交易所",
    "dex": "去中心化交易所",
    "exchange-token": "交易所代币",
    "fan_token": "粉丝代币",
    "fan-token": "粉丝代币",
    "bsc": "BSC 生态",
    "solana": "Solana 生态",
    "alpha": "Alpha",
    "chinese": "中文社区",
}

# Preferred display order (TV-like: 加密货币 first, then sector, then L1).
_ORDER = [
    "加密货币",
    "稳定币",
    "资产支持的稳定币",
    "智能合约平台",
    "第一层级",
    "第二层级",
    "第一层级 / 第二层级",
    "工作量证明",
    "权益证明",
    "挖矿",
    "付款",
    "去中心化金融",
    "去中心化交易所",
    "中心化交易所",
    "交易所代币",
    "迷因",
    "隐私",
    "人工智能",
    "基础设施",
    "游戏",
    "NFT",
    "元宇宙",
    "存储",
    "真实世界资产",
    "粉丝代币",
    "BSC 生态",
    "Solana 生态",
    "Alpha",
    "中文社区",
]


def _norm(raw: Any) -> str:
    return str(raw or "").strip()


def zh_label(raw: Any) -> Optional[str]:
    s = _norm(raw)
    if not s:
        return None
    key = s.lower().replace("_", "-") if s.lower() in ("pow", "pos", "defi", "nft", "ai", "rwa", "cex", "dex") else s.lower()
    # keep underscore form for Layer1_Layer2 / fan_token
    alt = s.lower()
    if alt in _SKIP_TAGS or key in _SKIP_TAGS:
        return None
    if alt in _ZH:
        return _ZH[alt]
    if key in _ZH:
        return _ZH[key]
    # title-case leftovers stay as-is (e.g. unknown Binance subtype)
    return s if s[0].isupper() or any("\u4e00" <= ch <= "\u9fff" for ch in s) else s


def _iter_raw(values: Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        return [values] if values.strip() else []
    if isinstance(values, (list, tuple, set)):
        out: list[str] = []
        for v in values:
            out.extend(_iter_raw(v))
        return out
    return [str(values)]


def compose_categories(
    *,
    underlying_sub_type: Any = None,
    spot_tags: Any = None,
    extra: Any = None,
    always_crypto: bool = True,
) -> list[str]:
    labels: list[str] = []
    seen: set[str] = set()

    def add(lab: Optional[str]) -> None:
        if not lab or lab in seen:
            return
        seen.add(lab)
        labels.append(lab)

    if always_crypto:
        add("加密货币")
    for raw in _iter_raw(underlying_sub_type) + _iter_raw(spot_tags) + _iter_raw(extra):
        add(zh_label(raw))

    # Expand combined L1/L2 into more TV-like wording when subtype already says Layer-1.
    if "第一层级" in seen and "第一层级 / 第二层级" in seen:
        labels = [x for x in labels if x != "第一层级 / 第二层级"]
        seen.discard("第一层级 / 第二层级")

    rank = {name: i for i, name in enumerate(_ORDER)}
    labels.sort(key=lambda x: (rank.get(x, 80), x))
    return labels


def format_categories(labels: Iterable[str]) -> str:
    labs = [x for x in labels if x]
    return "，".join(labs) if labs else "—"


def fetch_spot_tags(timeout: float = 25.0) -> dict[str, list[str]]:
    """base_asset → spot product tags. Public, no key."""
    req = urllib.request.Request(
        SPOT_PRODUCTS_URL,
        headers={"User-Agent": "hermes-market-ingest/0.1", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        log.warning("spot products fetch failed: %s", e)
        return {}
    rows = body.get("data") if isinstance(body, dict) else None
    if not isinstance(rows, list):
        return {}
    out: dict[str, list[str]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        if (row.get("q") or "").upper() != "USDT":
            continue
        base = (row.get("b") or "").upper()
        if not base:
            continue
        tags = row.get("tags") or []
        if isinstance(tags, str):
            tags = [tags]
        if not isinstance(tags, list):
            continue
        clean = [str(t) for t in tags if t]
        if base not in out or len(clean) > len(out[base]):
            out[base] = clean
    log.info("spot tags bases=%s", len(out))
    return out


def load_cached_spot_tags(path: Any, ttl_sec: int = 24 * 3600) -> Optional[dict[str, list[str]]]:
    try:
        from pathlib import Path

        p = Path(path)
        if not p.is_file():
            return None
        obj = json.loads(p.read_text(encoding="utf-8"))
        if time.time() - float(obj.get("ts") or 0) > ttl_sec:
            return None
        tags = obj.get("tags")
        return tags if isinstance(tags, dict) else None
    except Exception:
        return None


def save_spot_tags(path: Any, tags: dict[str, list[str]]) -> None:
    from pathlib import Path

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(
        json.dumps({"ts": time.time(), "count": len(tags), "tags": tags}, ensure_ascii=False),
        encoding="utf-8",
    )
    os.replace(tmp, p)


def attach_categories(
    symbols: list[dict[str, Any]],
    spot_tags: Optional[dict[str, list[str]]] = None,
) -> list[dict[str, Any]]:
    spot_tags = spot_tags or {}
    out: list[dict[str, Any]] = []
    for s in symbols:
        row = dict(s)
        base = (row.get("base_asset") or "").upper()
        # 1000SHIB → SHIB for spot tag lookup
        und = base
        for pref in ("1000000", "1000", "1M"):
            if und.startswith(pref) and len(und) > len(pref):
                und = und[len(pref) :]
                break
        tags = spot_tags.get(base) or spot_tags.get(und) or []
        labels = compose_categories(
            underlying_sub_type=row.get("underlying_sub_type"),
            spot_tags=tags,
        )
        row["spot_tags"] = tags
        row["categories"] = labels
        row["category"] = format_categories(labels)
        out.append(row)
    return out
