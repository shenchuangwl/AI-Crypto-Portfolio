"""Gate 3 — CoinGecko mapping + circulating mcap path (design v1.2 §16–§24).

- Map binance symbol → coingecko_id (derivatives API + pseudo-coin overrides)
- Batch markets for circulating_supply / market_cap
- Dual mcap + quality
- **15m market-cap path**: persist scan-by-scan mcap_calc, compute Mono_up/down,
  path slope proxy, and mcap momentum scores (replaces pure ret_24h proxy)
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("coin_selection.gate3")

CG_DEMO = "https://api.coingecko.com/api/v3"
CG_PRO = "https://pro-api.coingecko.com/api/v3"


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "")


def use_pro_host() -> bool:
    """Demo keys must hit api.coingecko.com. Pro only when COINGECKO_USE_PRO=1."""
    return _env_flag("COINGECKO_USE_PRO", default=False)


def _cg_base() -> str:
    if os.environ.get("COINGECKO_BASE"):
        return os.environ["COINGECKO_BASE"]
    return CG_PRO if use_pro_host() else CG_DEMO


def _headers() -> dict[str, str]:
    h = {"User-Agent": "hermes-gate3/0.2", "Accept": "application/json"}
    key = os.environ.get("COINGECKO_API_KEY") or os.environ.get("CG_API_KEY")
    if key:
        if "pro-api" in _cg_base():
            h["x-cg-pro-api-key"] = key
        else:
            h["x-cg-demo-api-key"] = key
    return h


def has_coingecko_key() -> bool:
    return bool(os.environ.get("COINGECKO_API_KEY") or os.environ.get("CG_API_KEY"))


def _get_json(url: str, timeout: float = 45.0, retries: int = 4) -> Any:
    last: Optional[Exception] = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=_headers())
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            last = RuntimeError(f"HTTP {e.code}: {body[:200]}")
            if e.code in (429, 503) and attempt + 1 < retries:
                # longer backoff without key
                base_sleep = 3.0 if not has_coingecko_key() else 1.5
                time.sleep(base_sleep * (attempt + 1))
                continue
            raise last from e
        except Exception as e:
            last = e
            if attempt + 1 < retries:
                time.sleep(1.0 * (attempt + 1))
                continue
            raise RuntimeError(str(e)) from e
    raise RuntimeError(str(last))


def parse_multiplier(base_asset: str) -> int:
    b = (base_asset or "").upper()
    m = re.match(r"^(1000000|1M|1000)([A-Z0-9]+)$", b)
    if not m:
        return 1
    pref = m.group(1)
    if pref == "1M":
        return 1_000_000
    return int(pref)


def _f(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if x != x:  # NaN
        return None
    return x


def compute_circulating_mcap(
    circulating_supply: Optional[float],
    *,
    last_price: Optional[float] = None,
    mark_price: Optional[float] = None,
    contract_multiplier: int = 1,
) -> Optional[float]:
    """流动市值 = 流通供应量 × 标的价。1000SHIB 等合约价先除以 multiplier。"""
    supply = _f(circulating_supply)
    if supply is None or supply <= 0:
        return None
    px = _f(mark_price)
    if px is None or px == 0:
        px = _f(last_price)
    if px is None or px == 0:
        return None
    mult = int(contract_multiplier or 1) or 1
    return supply * (px / mult)


def load_overrides(paths: list[Path]) -> dict[str, str]:
    """Return base_asset -> coingecko_id."""
    out: dict[str, str] = {}
    for p in paths:
        if not p.is_file():
            continue
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning("override load fail %s: %s", p, e)
            continue
        # support either flat {BASE: id} or {BASE: {coingecko_id: ...}}
        for k, v in raw.items():
            if k.startswith("_"):
                continue
            if isinstance(v, str):
                out[k.upper()] = v
            elif isinstance(v, dict) and v.get("coingecko_id"):
                out[k.upper()] = str(v["coingecko_id"])
        log.info("loaded mapping overrides from %s (%s keys)", p, len(out))
    if os.environ.get("CG_MAPPING_OVERRIDES"):
        try:
            extra = json.loads(os.environ["CG_MAPPING_OVERRIDES"])
            for k, v in extra.items():
                if isinstance(v, str):
                    out[k.upper()] = v
                elif isinstance(v, dict) and v.get("coingecko_id"):
                    out[k.upper()] = str(v["coingecko_id"])
        except Exception:
            pass
    return out


@dataclass
class Gate3Result:
    symbol: str
    coingecko_id: Optional[str]
    mapping_status: str
    circulating_supply: Optional[float]
    market_cap_coingecko: Optional[float]
    market_cap_calculated: Optional[float]
    gap_binance: Optional[float]
    mcap_momentum_score_up: float
    mcap_momentum_score_down: float
    mono_up: float
    mono_down: float
    path_points: int
    data_quality_score: float
    supply_missing: bool
    reason: str


class MappingStore:
    def __init__(self, path: Path, ttl_sec: int = 24 * 3600):
        self.path = path
        self.ttl_sec = ttl_sec
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> Optional[dict[str, Any]]:
        if not self.path.is_file():
            return None
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def save(self, mapping: dict[str, dict[str, Any]]) -> None:
        tmp = self.path.with_suffix(".tmp")
        payload = {"ts": time.time(), "count": len(mapping), "mapping": mapping}
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)


class McapPathStore:
    """Append-only per-symbol mcap path within an anchor day (15m points)."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data: dict[str, Any] = {"anchor_date": None, "symbols": {}}
        self._load()

    def _load(self) -> None:
        if self.path.is_file():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                self.data = {"anchor_date": None, "symbols": {}}

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    def ensure_anchor(self, anchor_date: str) -> None:
        if self.data.get("anchor_date") != anchor_date:
            self.data = {"anchor_date": anchor_date, "symbols": {}}

    def append(
        self,
        symbol: str,
        *,
        scan_id: str,
        mcap: float,
        supply: float,
        ts: float,
    ) -> list[dict[str, Any]]:
        syms = self.data.setdefault("symbols", {})
        arr = syms.setdefault(symbol, [])
        # idempotent by scan_id
        if arr and arr[-1].get("scan_id") == scan_id:
            arr[-1] = {
                "scan_id": scan_id,
                "mcap": mcap,
                "supply": supply,
                "ts": ts,
            }
        else:
            arr.append(
                {"scan_id": scan_id, "mcap": mcap, "supply": supply, "ts": ts}
            )
            # keep at most 96 points / day
            if len(arr) > 96:
                del arr[: len(arr) - 96]
        return arr


def path_metrics(points: list[dict[str, Any]]) -> dict[str, float]:
    """Compute Mono_up/down, supply-adj log return, slope-ish score."""
    if len(points) < 2:
        return {
            "mono_up": 0.5,
            "mono_down": 0.5,
            "mcap_log_ret": 0.0,
            "adj_log_ret": 0.0,
            "path_points": float(len(points)),
        }
    mcaps = [float(p["mcap"]) for p in points if p.get("mcap")]
    supplies = [float(p.get("supply") or 0) for p in points]
    if len(mcaps) < 2:
        return {
            "mono_up": 0.5,
            "mono_down": 0.5,
            "mcap_log_ret": 0.0,
            "adj_log_ret": 0.0,
            "path_points": float(len(points)),
        }
    up = down = 0
    for i in range(1, len(mcaps)):
        if mcaps[i] > mcaps[i - 1]:
            up += 1
        elif mcaps[i] < mcaps[i - 1]:
            down += 1
    n = len(mcaps) - 1
    if n and (up + down) == 0:
        # flat path — neutral mono
        mono_up = mono_down = 0.5
    else:
        mono_up = up / n if n else 0.5
        mono_down = down / n if n else 0.5
    m0, mt = max(mcaps[0], 1e-12), max(mcaps[-1], 1e-12)
    mcap_log_ret = math.log(mt / m0)
    s0, st = max(supplies[0], 1e-12), max(supplies[-1], 1e-12)
    # supply-adjusted ≈ price path when mcap=S*P
    adj_log_ret = mcap_log_ret - math.log(st / s0)
    return {
        "mono_up": mono_up,
        "mono_down": mono_down,
        "mcap_log_ret": mcap_log_ret,
        "adj_log_ret": adj_log_ret,
        "path_points": float(len(mcaps)),
    }


def mcap_scores_from_path(metrics: dict[str, float], ret_24h_fallback: float = 0.0) -> tuple[float, float]:
    """Path-based mcap momentum scores; fallback to ret_24h if path too short."""
    pts = metrics.get("path_points") or 0
    if pts < 3:
        z = math.tanh((ret_24h_fallback or 0.0) / 0.08)
        up = 50 + 50 * z
        dn = 50 - 50 * z
        return max(0.0, min(100.0, up)), max(0.0, min(100.0, dn))

    adj = float(metrics.get("adj_log_ret") or 0.0)
    # normalize ~ ±8% day path
    z = math.tanh(adj / 0.08)
    mono_up = float(metrics.get("mono_up") or 0.5)
    mono_down = float(metrics.get("mono_down") or 0.5)
    # weights: path mono 0.55, adjusted return 0.45 (aligned with §24 spirit)
    up = 100 * (0.55 * mono_up + 0.45 * (0.5 + 0.5 * z))
    dn = 100 * (0.55 * mono_down + 0.45 * (0.5 - 0.5 * z))
    return max(0.0, min(100.0, up)), max(0.0, min(100.0, dn))


def fetch_binance_futures_mapping(
    cache_path: Path,
    override_paths: list[Path],
    force: bool = False,
) -> dict[str, dict[str, Any]]:
    store = MappingStore(cache_path)
    overrides = load_overrides(override_paths)

    if not force:
        cached = store.load()
        if cached and cached.get("mapping"):
            age = time.time() - float(cached.get("ts", 0))
            mapping = cached["mapping"]
            # always re-apply overrides on top of cache
            mapping = _apply_overrides(mapping, overrides)
            if age < store.ttl_sec:
                log.info(
                    "gate3 mapping cache hit count=%s age=%.0fs overrides=%s",
                    len(mapping),
                    age,
                    len(overrides),
                )
                store.save(mapping)
                return mapping
            log.info("gate3 mapping cache stale — refresh")

    base = _cg_base()
    url = (
        base
        + "/derivatives/exchanges/binance_futures?"
        + urllib.parse.urlencode({"include_tickers": "unexpired"})
    )
    data = _get_json(url, timeout=60)
    ticks = data.get("tickers") or []
    mapping: dict[str, dict[str, Any]] = {}
    for t in ticks:
        if (t.get("target") or "").upper() != "USDT":
            continue
        if (t.get("contract_type") or "").lower() != "perpetual":
            continue
        sym = (t.get("symbol") or "").upper()
        if not sym:
            continue
        if not sym.endswith("USDT"):
            sym = sym + "USDT"
        coin_id = t.get("coin_id")
        base_asset = (t.get("base") or "").upper()
        mult = parse_multiplier(base_asset)
        status = "MAPPED" if coin_id else "UNMAPPED"
        method = "direct_coin_id"
        if coin_id and re.match(r"^(1000|1000000|1m)", str(coin_id), re.I):
            status = "PSEUDO_COIN"
            method = "pseudo_pending"
        mapping[sym] = {
            "symbol": sym,
            "base": base_asset,
            "coin_id": coin_id,
            "contract_multiplier": mult,
            "mapping_status": status,
            "mapping_method": method,
            "index": t.get("index"),
            "last": t.get("last"),
        }
    mapping = _apply_overrides(mapping, overrides)
    store.save(mapping)
    log.info("gate3 mapping refreshed count=%s", len(mapping))
    return mapping


def _apply_overrides(
    mapping: dict[str, dict[str, Any]], overrides: dict[str, str]
) -> dict[str, dict[str, Any]]:
    for sym, row in list(mapping.items()):
        base = (row.get("base") or "").upper()
        # strip multiplier prefix for lookup
        und = base
        for pref in ("1000000", "1000", "1M"):
            if und.startswith(pref) and len(und) > len(pref):
                und = und[len(pref) :]
                break
        oid = overrides.get(base) or overrides.get(und)
        if not oid:
            continue
        prev = row.get("coin_id")
        row = dict(row)
        row["coin_id"] = oid
        row["mapping_status"] = "RESOLVED"
        row["mapping_method"] = "pseudo_coin_resolved" if prev != oid else "manual_override"
        row["override_from"] = prev
        mapping[sym] = row
    return mapping


def fetch_markets_batched(
    coin_ids: list[str],
    *,
    batch_size: int = 200,
    inter_batch_sleep: float = 2.2,
    cache_path: Optional[Path] = None,
) -> dict[str, dict[str, Any]]:
    cache: dict[str, dict[str, Any]] = {}
    if cache_path and cache_path.is_file():
        try:
            obj = json.loads(cache_path.read_text(encoding="utf-8"))
            if time.time() - float(obj.get("ts", 0)) < 900:
                cache = obj.get("markets") or {}
                log.info("gate3 markets cache hit %s", len(cache))
        except Exception:
            pass

    ids = []
    seen = set()
    for c in coin_ids:
        if c and c not in seen:
            seen.add(c)
            ids.append(c)
    need = [c for c in ids if c not in cache]
    # without API key, be gentler
    if not has_coingecko_key():
        inter_batch_sleep = max(inter_batch_sleep, 3.0)
        batch_size = min(batch_size, 150)

    base = _cg_base()
    out = dict(cache)
    for i in range(0, len(need), batch_size):
        chunk = need[i : i + batch_size]
        if not chunk:
            continue
        url = (
            base
            + "/coins/markets?"
            + urllib.parse.urlencode(
                {
                    "vs_currency": "usd",
                    "ids": ",".join(chunk),
                    "per_page": str(batch_size),
                    "page": "1",
                    "sparkline": "false",
                }
            )
        )
        try:
            rows = _get_json(url, timeout=60)
            if isinstance(rows, list):
                for r in rows:
                    if r.get("id"):
                        out[r["id"]] = r
            log.info(
                "gate3 markets batch %s-%s got %s key=%s",
                i,
                i + len(chunk),
                len(rows) if isinstance(rows, list) else 0,
                has_coingecko_key(),
            )
        except Exception as e:
            log.warning("gate3 markets batch fail: %s", e)
        if i + batch_size < len(need):
            time.sleep(inter_batch_sleep)

    if cache_path:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"ts": time.time(), "markets": out}, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(tmp, cache_path)
    return out


def evaluate_symbol(
    symbol: str,
    *,
    mapping: Optional[dict[str, Any]],
    market: Optional[dict[str, Any]],
    last_price: Optional[float],
    mark_price: Optional[float],
    path_points: list[dict[str, Any]],
    ret_24h: float = 0.0,
) -> Gate3Result:
    if not mapping or not mapping.get("coin_id"):
        return Gate3Result(
            symbol=symbol,
            coingecko_id=None,
            mapping_status=(mapping or {}).get("mapping_status") or "UNMAPPED",
            circulating_supply=None,
            market_cap_coingecko=None,
            market_cap_calculated=None,
            gap_binance=None,
            mcap_momentum_score_up=50.0,
            mcap_momentum_score_down=50.0,
            mono_up=0.5,
            mono_down=0.5,
            path_points=0,
            data_quality_score=20.0,
            supply_missing=True,
            reason="unmapped",
        )
    cid = mapping.get("coin_id")
    status = mapping.get("mapping_status") or "MAPPED"
    if status == "PSEUDO_COIN":
        return Gate3Result(
            symbol=symbol,
            coingecko_id=cid,
            mapping_status=status,
            circulating_supply=None,
            market_cap_coingecko=None,
            market_cap_calculated=None,
            gap_binance=None,
            mcap_momentum_score_up=50.0,
            mcap_momentum_score_down=50.0,
            mono_up=0.5,
            mono_down=0.5,
            path_points=0,
            data_quality_score=15.0,
            supply_missing=True,
            reason="pseudo_coin_unresolved",
        )

    supply = _f((market or {}).get("circulating_supply"))
    if supply is not None and supply <= 0:
        supply = None
    mcap_cg = _f((market or {}).get("market_cap"))
    if mcap_cg is not None and mcap_cg <= 0:
        mcap_cg = None

    if supply is None:
        return Gate3Result(
            symbol=symbol,
            coingecko_id=cid,
            mapping_status=status,
            circulating_supply=None,
            market_cap_coingecko=mcap_cg,
            market_cap_calculated=None,
            gap_binance=None,
            mcap_momentum_score_up=50.0,
            mcap_momentum_score_down=50.0,
            mono_up=0.5,
            mono_down=0.5,
            path_points=float(len(path_points)),
            data_quality_score=25.0,
            supply_missing=True,
            reason="supply_missing",
        )

    mult = int(mapping.get("contract_multiplier") or parse_multiplier(mapping.get("base") or "") or 1) or 1
    mcap_calc = compute_circulating_mcap(
        supply,
        last_price=last_price,
        mark_price=mark_price,
        contract_multiplier=mult,
    )
    gap = None
    if mcap_cg and mcap_calc and mcap_cg > 0:
        gap = abs(mcap_cg - mcap_calc) / mcap_cg

    metrics = path_metrics(path_points)
    up, dn = mcap_scores_from_path(metrics, ret_24h_fallback=ret_24h)

    dq = 92.0 if status == "RESOLVED" else 95.0
    if gap is not None:
        if gap > 0.20:
            dq = 40.0
            up = dn = 50.0
        elif gap > 0.05:
            dq = min(dq, 70.0)
    if metrics["path_points"] < 3:
        dq = min(dq, 80.0)

    return Gate3Result(
        symbol=symbol,
        coingecko_id=cid,
        mapping_status=status,
        circulating_supply=supply,
        market_cap_coingecko=mcap_cg,
        market_cap_calculated=mcap_calc,
        gap_binance=gap,
        mcap_momentum_score_up=up,
        mcap_momentum_score_down=dn,
        mono_up=float(metrics["mono_up"]),
        mono_down=float(metrics["mono_down"]),
        path_points=int(metrics["path_points"]),
        data_quality_score=dq,
        supply_missing=False,
        reason="ok" if metrics["path_points"] >= 3 else "path_warming",
    )


def result_to_dict(r: Gate3Result) -> dict[str, Any]:
    return {
        "symbol": r.symbol,
        "coingecko_id": r.coingecko_id,
        "mapping_status": r.mapping_status,
        "circulating_supply": r.circulating_supply,
        "market_cap_coingecko": r.market_cap_coingecko,
        "market_cap_calculated": r.market_cap_calculated,
        "gap_binance": r.gap_binance,
        "mcap_momentum_score_up": r.mcap_momentum_score_up,
        "mcap_momentum_score_down": r.mcap_momentum_score_down,
        "mono_up": r.mono_up,
        "mono_down": r.mono_down,
        "path_points": r.path_points,
        "data_quality_score": r.data_quality_score,
        "supply_missing": r.supply_missing,
        "reason": r.reason,
    }


def run_gate3(
    symbols: list[str],
    *,
    prices: dict[str, dict[str, Any]],
    ret_24h: dict[str, float],
    data_dir: Path,
    hermes_root: Optional[Path] = None,
    anchor_date: str,
    scan_id: str,
    force_mapping: bool = False,
) -> dict[str, Gate3Result]:
    data_dir.mkdir(parents=True, exist_ok=True)
    hermes_root = hermes_root or Path(
        os.environ.get(
            "HERMES_ROOT",
            str(Path(__file__).resolve().parents[4]),
        )
    )
    override_paths = [
        hermes_root / "packages" / "config" / "mapping_overrides.json",
        data_dir / "mapping_overrides.json",
    ]
    # also copy package overrides into data dir for runtime visibility
    pkg = override_paths[0]
    if pkg.is_file() and not (data_dir / "mapping_overrides.json").is_file():
        try:
            (data_dir / "mapping_overrides.json").write_text(
                pkg.read_text(encoding="utf-8"), encoding="utf-8"
            )
        except Exception:
            pass

    log.info(
        "gate3 host=%s key=%s symbols=%s",
        "pro" if use_pro_host() else "demo",
        has_coingecko_key(),
        len(symbols),
    )
    mapping = fetch_binance_futures_mapping(
        data_dir / "cg_mapping.json",
        override_paths=override_paths,
        force=force_mapping,
    )
    ids = []
    for sym in symbols:
        m = mapping.get(sym) or mapping.get(sym.upper())
        if m and m.get("coin_id") and m.get("mapping_status") != "PSEUDO_COIN":
            ids.append(m["coin_id"])
    markets = fetch_markets_batched(
        ids, cache_path=data_dir / "cg_markets_cache.json"
    )

    path_store = McapPathStore(data_dir / f"mcap_path_{anchor_date}.json")
    path_store.ensure_anchor(anchor_date)
    now_ts = time.time()

    out: dict[str, Gate3Result] = {}
    for sym in symbols:
        m = mapping.get(sym) or mapping.get(sym.upper())
        cid = (m or {}).get("coin_id")
        market = markets.get(cid) if cid else None
        p = prices.get(sym) or {}
        supply = _f((market or {}).get("circulating_supply"))
        if supply is not None and supply <= 0:
            supply = None
        if supply and m:
            mult = int(m.get("contract_multiplier") or parse_multiplier(m.get("base") or "") or 1) or 1
            mcap_calc = compute_circulating_mcap(
                supply,
                last_price=p.get("last"),
                mark_price=p.get("mark"),
                contract_multiplier=mult,
            )
            if mcap_calc is not None:
                path_store.append(
                    sym, scan_id=scan_id, mcap=mcap_calc, supply=supply, ts=now_ts
                )
        pts = (path_store.data.get("symbols") or {}).get(sym) or []
        out[sym] = evaluate_symbol(
            sym,
            mapping=m,
            market=market,
            last_price=p.get("last"),
            mark_price=p.get("mark"),
            path_points=pts,
            ret_24h=float(ret_24h.get(sym) or 0.0),
        )
    path_store.save()
    return out
