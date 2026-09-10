"""Contract universe filter for Binance USDT perpetual (v1.2 §8.2).

Hard facts (2026-08 research):
- Selection universe = quote USDT + contractType PERPETUAL + status TRADING
  + underlyingType in {COIN, missing}.
- TRADIFI_PERPETUAL is a **contractType**, not an underlyingType. Those
  symbols (e.g. AAPLUSDT) are already excluded by contractType == PERPETUAL.
- We still keep TRADIFI_PERPETUAL in the underlyingType exclude list so a
  future Binance schema change cannot leak stock-perps into Gate1.
"""

from __future__ import annotations

import hashlib
from typing import Any, Iterable, Optional

COIN_UNDERLYING = {None, "", "COIN"}
# Defensive: these must never enter the selection universe even if schema drifts.
EXCLUDE_UNDERLYING_DEFAULT = ("TRADIFI_PERPETUAL", "INDEX")


def classify_market_kind(symbol: dict[str, Any]) -> str:
    """Return a stable venue-class label for UI / later DEX/stock expansion."""
    ct = (symbol.get("contractType") or "").upper()
    ut = (symbol.get("underlyingType") or "").upper()
    if ct == "TRADIFI_PERPETUAL" or ut == "TRADIFI_PERPETUAL":
        return "tradifi_perp"
    if ct == "PERPETUAL" and ut in ("", "COIN"):
        return "coin_perp"
    if ct == "PERPETUAL":
        return f"perp_{ut.lower() or 'unknown'}"
    return (ct or "unknown").lower()


def is_allowed_symbol(
    symbol: dict[str, Any],
    *,
    quote_asset: str = "USDT",
    contract_type: str = "PERPETUAL",
    status: str = "TRADING",
    exclude_underlying_types: Iterable[str] = EXCLUDE_UNDERLYING_DEFAULT,
    stock_blacklist: Optional[set[str]] = None,
) -> bool:
    if symbol.get("quoteAsset") != quote_asset:
        return False
    if symbol.get("contractType") != contract_type:
        return False
    if symbol.get("status") != status:
        return False
    ut = symbol.get("underlyingType")
    if ut in set(exclude_underlying_types):
        return False
    # Allow missing underlyingType or COIN only (v1.2 COIN USDT-M)
    if ut not in COIN_UNDERLYING:
        return False
    sym = symbol.get("symbol") or ""
    if stock_blacklist and sym in stock_blacklist:
        return False
    return True


def compact_symbol(s: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": s["symbol"],
        "pair": s.get("pair"),
        "base_asset": s.get("baseAsset"),
        "quote_asset": s.get("quoteAsset"),
        "contract_type": s.get("contractType"),
        "status": s.get("status"),
        "underlying_type": s.get("underlyingType"),
        "underlying_sub_type": s.get("underlyingSubType"),
        "market_kind": classify_market_kind(s),
        "price_precision": s.get("pricePrecision"),
        "quantity_precision": s.get("quantityPrecision"),
        "onboard_date": s.get("onboardDate"),
    }


def filter_universe(
    exchange_info: dict[str, Any],
    **kwargs: Any,
) -> list[dict[str, Any]]:
    out = []
    for s in exchange_info.get("symbols") or []:
        if is_allowed_symbol(s, **kwargs):
            out.append(compact_symbol(s))
    out.sort(key=lambda x: x["symbol"])
    return out


def summarize_exchange_info(exchange_info: dict[str, Any]) -> dict[str, Any]:
    """Counts used by /v1/universe/stats and health (never guess)."""
    symbols = exchange_info.get("symbols") or []
    by_contract: dict[str, int] = {}
    by_underlying: dict[str, int] = {}
    usdt_perp_trading = 0
    coin_usdt_perp = 0
    tradifi_perp = 0
    for s in symbols:
        ct = s.get("contractType") or "MISSING"
        ut = s.get("underlyingType") or "MISSING"
        by_contract[ct] = by_contract.get(ct, 0) + 1
        by_underlying[ut] = by_underlying.get(ut, 0) + 1
        if (
            s.get("quoteAsset") == "USDT"
            and s.get("contractType") == "PERPETUAL"
            and s.get("status") == "TRADING"
        ):
            usdt_perp_trading += 1
            if s.get("underlyingType") in COIN_UNDERLYING:
                coin_usdt_perp += 1
        if s.get("contractType") == "TRADIFI_PERPETUAL":
            tradifi_perp += 1
    selected = filter_universe(exchange_info)
    return {
        "exchange_symbol_count": len(symbols),
        "usdt_perpetual_trading": usdt_perp_trading,
        "coin_usdt_perp_trading": coin_usdt_perp,
        "tradifi_perpetual_contract_type": tradifi_perp,
        "selected_count": len(selected),
        "by_contract_type": dict(sorted(by_contract.items())),
        "by_underlying_type": dict(sorted(by_underlying.items())),
        "note": (
            "TRADIFI_PERPETUAL is contractType, not underlyingType. "
            "Selection universe is COIN USDT-M PERPETUAL TRADING only."
        ),
    }


def contract_set_version(symbols: list[dict[str, Any]]) -> str:
    key = ",".join(s["symbol"] for s in symbols)
    return hashlib.sha1(key.encode("utf-8")).hexdigest()
