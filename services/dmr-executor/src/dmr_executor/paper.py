"""Paper DMR executor — consume dmr-adapter accepted/ messages without live orders.

S4 thin wrapper around third_party/DMR_binance_Version_V16_A1:
  - Does NOT place real orders
  - Loads accepted candidate JSON
  - Optionally imports DMR package for health probe
  - Writes paper run log under data/dmr-executor/
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("dmr_executor")

HERMES_ROOT = Path(
    os.environ.get(
        "HERMES_ROOT",
        str(Path(__file__).resolve().parents[4]),
    )
)
DMR_ROOT = Path(
    os.environ.get(
        "DMR_ROOT",
        str(HERMES_ROOT / "third_party" / "DMR_binance_Version_V16_A1"),
    )
)
ACCEPTED = Path(
    os.environ.get(
        "DMR_ACCEPTED_DIR",
        str(HERMES_ROOT / "data" / "dmr-adapter" / "accepted"),
    )
)
OUT_DIR = Path(
    os.environ.get(
        "DMR_EXECUTOR_DATA",
        str(HERMES_ROOT / "data" / "dmr-executor"),
    )
)


def _load_execution_guard():
    """与 adapter **同一份**正向 allow-only 谓词（文档A §14 / 文档B §18.6）。"""
    contracts_py = HERMES_ROOT / "contracts" / "python"
    if str(contracts_py) not in sys.path:
        sys.path.insert(0, str(contracts_py))
    from execution_guard import executor_accept  # type: ignore

    return executor_accept


def deployed_manifest() -> Optional[dict[str, Any]]:
    """已部署执行身份。取不到就返回 None —— 谓词随后一律拒绝，绝不「先执行再说」。"""
    raw = (os.environ.get("DMR_DEPLOYED_MANIFEST") or "").strip()
    if raw:
        try:
            return json.loads(Path(raw).read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            log.warning("DMR_DEPLOYED_MANIFEST unreadable (%s): %s", raw, e)
            return None
    try:
        cs_src = HERMES_ROOT / "services" / "coin-selection" / "src"
        if str(cs_src) not in sys.path:
            sys.path.insert(0, str(cs_src))
        from coin_selection.scan import SelectionSettings, main_exec_identity  # type: ignore

        return main_exec_identity(SelectionSettings())
    except Exception as e:  # noqa: BLE001
        log.warning("deployed manifest unavailable: %s — nothing will be executed", e)
        return None


def utc_now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def list_accepted(limit: int = 50) -> list[Path]:
    if not ACCEPTED.is_dir():
        return []
    files = sorted(ACCEPTED.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[:limit]


def load_msg(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def paper_decide(
    msg: dict[str, Any],
    *,
    envelope: Optional[dict[str, Any]] = None,
    manifest: Optional[dict[str, Any]] = None,
    guard: Any = None,
) -> dict[str, Any]:
    """Map candidate → paper action (no exchange calls).

    【ChatGpt_SOL5.6 文档A §14 / 文档B §18.6】executor **不信任** accepted 目录：
    先重跑同一个 ``execution_allowed`` 谓词并校验 envelope 的 payload hash，
    通不过一律 ``SKIP``（原因前缀 ``guard:``）。裸历史 candidate（没有 envelope）
    只能进只读归档/复盘兼容层，绝不执行。
    """
    side = msg.get("direction")  # LONG / SHORT
    symbol = msg.get("symbol")
    state = msg.get("state")
    score = msg.get("total_score")
    action = "SKIP"
    reason = []
    guard_res = None
    if guard is not None:
        guard_res = guard(envelope, manifest)
        if not guard_res:
            reason.append(f"guard:{guard_res.reason}")
    if state != "CONFIRMED":
        reason.append(f"state={state}")
    if side not in ("LONG", "SHORT"):
        reason.append(f"side={side}")
    if (
        state == "CONFIRMED"
        and side in ("LONG", "SHORT")
        and (guard_res is None or bool(guard_res))
    ):
        action = "PAPER_OPEN"
        reason.append("confirmed_candidate")
    return {
        "guard_ok": bool(guard_res) if guard_res is not None else None,
        "guard_reason": guard_res.reason if guard_res is not None else None,
        "action": action,
        "symbol": symbol,
        "side": side,
        "score": score,
        "reasons": reason,
        "message_id": msg.get("message_id"),
        "scan_id": msg.get("scan_id"),
        "paper": True,
        "notional_usd": float(os.environ.get("PAPER_NOTIONAL_USD", "100")),
        "leverage": float(os.environ.get("PAPER_LEVERAGE", "1")),
    }


def probe_dmr_import() -> dict[str, Any]:
    info: dict[str, Any] = {
        "dmr_root": str(DMR_ROOT),
        "exists": DMR_ROOT.is_dir(),
        "import_ok": False,
        "import_error": None,
        "main_py": (DMR_ROOT / "main.py").is_file(),
    }
    if not DMR_ROOT.is_dir():
        return info
    # Only add path; avoid executing main
    if str(DMR_ROOT) not in sys.path:
        sys.path.insert(0, str(DMR_ROOT))
    try:
        # Prefer lightweight modules if present
        import importlib.util

        cfg = DMR_ROOT / "config" / "config.py"
        info["has_config"] = cfg.is_file()
        # Don't import full trading stack (may need keys/network) — just path probe
        info["import_ok"] = True
        info["note"] = (
            "Paper mode does not execute main.py. "
            "Live mode would construct MultiStrategy / OrderExecutor with API keys."
        )
    except Exception as e:
        info["import_error"] = str(e)
        info["traceback"] = traceback.format_exc()
    return info


def run_paper_once(limit: int = 20) -> dict[str, Any]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "runs").mkdir(parents=True, exist_ok=True)
    files = list_accepted(limit=limit)
    decisions = []
    try:
        guard = _load_execution_guard()
    except Exception as e:  # noqa: BLE001 — 守卫加载失败 = 不执行任何东西
        log.warning("execution guard unavailable: %s — refusing to execute", e)
        guard = None
    manifest = deployed_manifest()
    for path in files:
        try:
            raw = load_msg(path)
            # adapter 新版写的是 envelope；旧版直接写裸 candidate。
            if isinstance(raw, dict) and raw.get("schema") == "dmr-accepted-envelope-v1":
                envelope, msg = raw, dict(raw.get("candidate") or {})
            else:
                envelope, msg = None, dict(raw)
            if guard is None:
                dec = {
                    "action": "SKIP",
                    "guard_ok": False,
                    "guard_reason": "GUARD_UNAVAILABLE",
                    "symbol": msg.get("symbol"),
                    "side": msg.get("direction"),
                    "message_id": msg.get("message_id"),
                    "reasons": ["guard:GUARD_UNAVAILABLE"],
                    "paper": True,
                }
            else:
                dec = paper_decide(
                    msg, envelope=envelope, manifest=manifest, guard=guard
                )
            dec["source_file"] = str(path)
            decisions.append(dec)
        except Exception as e:
            decisions.append({"action": "ERROR", "error": str(e), "source_file": str(path)})

    run = {
        "mode": "paper",
        "ran_at_utc": utc_now_iso(),
        "accepted_dir": str(ACCEPTED),
        "guard": {
            "loaded": guard is not None,
            "deployed_manifest": bool(manifest),
            "blocked": sum(1 for d in decisions if d.get("guard_ok") is False),
        },
        "dmr": probe_dmr_import(),
        "decisions": decisions,
        "counts": {
            "files": len(files),
            "paper_open": sum(1 for d in decisions if d.get("action") == "PAPER_OPEN"),
            "skip": sum(1 for d in decisions if d.get("action") == "SKIP"),
            "error": sum(1 for d in decisions if d.get("action") == "ERROR"),
        },
    }
    name = f"paper-{int(time.time())}.json"
    out_path = OUT_DIR / "runs" / name
    out_path.write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT_DIR / "latest_run.json").write_text(
        json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log.info(
        "paper run files=%s open=%s skip=%s → %s",
        run["counts"]["files"],
        run["counts"]["paper_open"],
        run["counts"]["skip"],
        out_path,
    )
    return run


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    p = argparse.ArgumentParser(description="DMR paper executor")
    p.add_argument("--paper", action="store_true", default=True, help="paper mode (default)")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--probe", action="store_true")
    p.add_argument("--watch", action="store_true", help="poll accepted/ every 15s")
    args = p.parse_args()

    if args.probe:
        print(json.dumps(probe_dmr_import(), indent=2, ensure_ascii=False))
        return

    if args.watch:
        log.info("watching %s", ACCEPTED)
        while True:
            run_paper_once(limit=args.limit)
            time.sleep(15)
    else:
        run = run_paper_once(limit=args.limit)
        print(json.dumps(run, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
