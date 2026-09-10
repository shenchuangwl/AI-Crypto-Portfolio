"""DMR adapter (S4): bridge selection candidates → third_party DMR package.

Does **not** rewrite DMR strategy code. Responsibilities:
  - Watch inbox JSON from coin-selection
  - Validate with contracts/python models when available
  - Apply §39.3 reject rules
  - Persist accepted messages for executor consumption
  - Optional: invoke a dry-run hook into DMR tree (import path only)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("dmr_adapter")

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


def _contracts_path() -> Path:
    contracts_py = HERMES_ROOT / "contracts" / "python"
    if str(contracts_py) not in sys.path:
        sys.path.insert(0, str(contracts_py))
    return contracts_py


def _load_contract_models():
    _contracts_path()
    try:
        from models import (  # type: ignore
            DmrCandidateMessage,
            dmr_should_reject,
        )

        return DmrCandidateMessage, dmr_should_reject
    except Exception as e:
        # 【ChatGpt_SOL5.6 文档B §18.6】合同模型不可用时**整批拒绝**，
        # 禁止退化到 local_reject 这种弱检查（那正是 Y 候选可能被吃掉的漏洞）。
        log.warning("contracts models unavailable: %s — batches will be rejected", e)
        return None, None


def _load_execution_guard():
    """正向 allow-only 谓词。adapter 与 executor 共用同一份实现（文档A §14）。"""
    _contracts_path()
    from execution_guard import (  # type: ignore
        REASON_CONTRACT_UNAVAILABLE,
        adapter_accept,
        build_accepted_envelope,
    )

    return adapter_accept, build_accepted_envelope, REASON_CONTRACT_UNAVAILABLE


def deployed_manifest() -> Optional[dict[str, Any]]:
    """已部署的执行身份。

    【文档A §14】``execution_allowed`` 要求 batch / candidate / deployed manifest
    三方逐字节一致。这里的事实源是**主榜自己解析出来的有效配置**，与
    ``coin_selection.scan.main_exec_identity`` 同一个函数，因此不存在第二份真值。
    环境变量 ``DMR_DEPLOYED_MANIFEST`` 可指向一份 JSON 用于灰度演练与测试。
    """
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
        log.warning("deployed manifest unavailable: %s — nothing will be accepted", e)
        return None


# v1.2 §39.3 ⑦: an unknown parameter_version must be rejected, otherwise a silent
# parameter drift in coin-selection would keep flowing into the executor. 计划 §9
# requires the new tag to be whitelisted at the same time it goes live.
DEFAULT_PARAM_WHITELIST = (
    "param-v1.4.0-staircase-confirm-dmr",
    "param-v1.3.0-dual-path-sticky",
    "param-v1.2.0-g1g2g3g4-path",
)


def parameter_whitelist() -> list[str]:
    raw = os.environ.get("DMR_PARAM_WHITELIST", "")
    items = [x.strip() for x in raw.split(",") if x.strip()]
    return items or list(DEFAULT_PARAM_WHITELIST)


def message_id(anchor_date: str, scan_id: str, symbol: str, direction: str) -> str:
    raw = f"{anchor_date}|{scan_id}|{symbol}|{direction}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def local_reject(msg: dict[str, Any], now: Optional[datetime] = None) -> Optional[str]:
    now = now or datetime.now(timezone.utc)
    if msg.get("state") != "CONFIRMED":
        return "state_not_confirmed"
    if msg.get("direction") == "NEUTRAL":
        return "neutral_direction"
    exp = msg.get("expires_at_utc")
    if exp:
        try:
            exp_dt = datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
            if now > exp_dt:
                return "expired"
        except Exception:
            pass
    if msg.get("data_mode") == "MISSING" or float(msg.get("data_confidence") or 0) < 60:
        return "data_quality"
    flags = msg.get("risk_flags") or []
    if any(str(f).startswith("H") for f in flags):
        return "hard_risk"
    cs = msg.get("circulating_supply")
    if cs is None or float(cs) <= 0:
        return "supply_missing"
    if str(msg.get("parameter_version") or "") not in parameter_whitelist():
        return "parameter_version"
    return None


class DmrAdapter:
    def __init__(
        self,
        inbox: Optional[Path] = None,
        outbox: Optional[Path] = None,
        accepted_dir: Optional[Path] = None,
    ):
        base = HERMES_ROOT / "data" / "dmr-adapter"
        self.inbox = inbox or base / "inbox"
        self.outbox = outbox or base / "outbox"
        self.accepted_dir = accepted_dir or base / "accepted"
        self.rejected_dir = base / "rejected"
        for d in (self.inbox, self.outbox, self.accepted_dir, self.rejected_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.DmrCandidateMessage, self.dmr_should_reject = _load_contract_models()
        self.param_whitelist = parameter_whitelist()
        # 正向 allow-only 执行守卫（文档A §14 / 文档B §18.6）。
        (
            self._adapter_accept,
            self._build_envelope,
            self._reason_contract_unavailable,
        ) = _load_execution_guard()
        self.deployed_manifest = deployed_manifest()

    def process_file(self, path: Path) -> dict[str, Any]:
        data = json.loads(path.read_text(encoding="utf-8"))
        candidates = data.get("candidates") or data.get("messages") or []
        # Allow single-message files
        if not candidates and "symbol" in data and "message_id" in data:
            candidates = [data]

        # —— 批次层执行身份（文档B §18.6）——
        #
        # batch 与 candidate 必须**双方**满足 main + 两个严格 true + 完整身份匹配。
        # 合同模型不可用 → 整批 CONTRACT_UNAVAILABLE 拒绝，不退化到弱检查。
        batch_identity = {
            k: data.get(k)
            for k in (
                "board_key",
                "dmr_executable",
                "consumable_by_dmr",
                "parameter_version",
                "rule_revision",
                "config_hash",
                "mcap_mapping_version",
                "mapping_hash",
                "code_commit",
            )
        }
        contract_ok = bool(self.DmrCandidateMessage and self.dmr_should_reject)

        accepted, rejected = [], []
        envelopes: list[dict[str, Any]] = []
        for raw in candidates:
            msg = dict(raw)
            # ensure message_id
            if not msg.get("message_id"):
                msg["message_id"] = message_id(
                    msg.get("anchor_date", ""),
                    msg.get("scan_id", ""),
                    msg.get("symbol", ""),
                    msg.get("direction", ""),
                )
            reason = None
            if self.DmrCandidateMessage and self.dmr_should_reject:
                try:
                    model = self.DmrCandidateMessage.model_validate(msg)
                    reason = self.dmr_should_reject(
                        model, parameter_whitelist=self.param_whitelist
                    )
                    msg = json.loads(model.model_dump_json())
                except Exception as e:
                    reason = f"schema:{e}"
            else:
                reason = local_reject(msg)

            # —— 执行守卫：业务 reject 之外的**第二道、正向**闸 ——
            #
            # 顺序刻意放在业务规则之后：先记录业务原因，再由安全谓词决定放行。
            # 任何一条不满足都写进 rejected，reason 前缀 GUARD: 便于负测断言。
            guard = self._adapter_accept(
                batch_identity,
                msg,
                self.deployed_manifest,
                whitelist=self.param_whitelist,
                contract_available=contract_ok,
            )
            if reason:
                rejected.append({"reason": reason, "message": msg})
            elif not guard:
                rejected.append({"reason": f"GUARD:{guard.reason}", "message": msg})
            else:
                accepted.append(msg)
                envelopes.append(
                    self._build_envelope(
                        batch_identity,
                        msg,
                        deployed_manifest=self.deployed_manifest,
                        accepted_at_utc=datetime.now(timezone.utc)
                        .replace(microsecond=0)
                        .isoformat()
                        .replace("+00:00", "Z"),
                    )
                )

        scan_id = data.get("scan_id") or (candidates[0].get("scan_id") if candidates else path.stem)
        out = {
            "scan_id": scan_id,
            "source": str(path),
            "accepted": accepted,
            "rejected": rejected,
            "board_key": data.get("board_key"),
            "guard": {
                "contract_available": contract_ok,
                "deployed_manifest": bool(self.deployed_manifest),
                "accepted": len(accepted),
                "envelopes": len(envelopes),
            },
            "dmr_root_exists": DMR_ROOT.is_dir(),
            "dmr_root": str(DMR_ROOT),
            "processed_at_utc": datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z"),
        }
        _atomic_write(self.outbox / f"{scan_id}.result.json", out)
        # accepted 目录写的是**信封**（含 batch identity + candidate + payload hash），
        # executor 会重新校验它（文档A §14 / 文档B §18.6）。
        for env in envelopes:
            _atomic_write(
                self.accepted_dir / f"{env['candidate']['message_id']}.json", env
            )
        for i, r in enumerate(rejected):
            mid = (r["message"].get("message_id") or f"rej-{i}")
            _atomic_write(self.rejected_dir / f"{mid}.json", r)

        # processed marker — keep inbox file, write .done
        path.with_suffix(path.suffix + ".done").write_text(
            json.dumps({"ok": True, "accepted": len(accepted), "rejected": len(rejected)}),
            encoding="utf-8",
        )
        return out

    def process_inbox(self, *, include_done: bool = False) -> list[dict[str, Any]]:
        results = []
        for path in sorted(self.inbox.glob("*.json")):
            if path.name.endswith(".done.json"):
                continue
            done = path.with_suffix(path.suffix + ".done")
            if done.exists() and not include_done:
                continue
            try:
                results.append(self.process_file(path))
            except Exception:
                log.exception("failed %s", path)
        return results

    def probe_dmr_package(self) -> dict[str, Any]:
        """Non-invasive probe of third_party DMR tree."""
        info: dict[str, Any] = {
            "dmr_root": str(DMR_ROOT),
            "exists": DMR_ROOT.is_dir(),
            "entries": [],
            "importable_hint": None,
        }
        if not DMR_ROOT.is_dir():
            return info
        info["entries"] = sorted(
            p.name for p in DMR_ROOT.iterdir() if not p.name.startswith(".")
        )[:40]
        # common entrypoints
        for name in ("main.py", "README_DMR.md", "requirements.txt", "bit"):
            info[f"has_{name.replace('.', '_')}"] = (DMR_ROOT / name).exists()
        info["importable_hint"] = (
            "Add DMR_ROOT to PYTHONPATH and call existing strategy mains; "
            "adapter only delivers candidate JSON under data/dmr-adapter/accepted/"
        )
        return info

    def seed_example_candidate(self) -> Path:
        """Copy contracts example into inbox for pipeline smoke test."""
        src = HERMES_ROOT / "contracts" / "examples" / "dmr-candidate.example.json"
        msg = json.loads(src.read_text(encoding="utf-8"))
        # refresh expiry so reject doesn't always expire in far future runs
        # keep sample times; reject helper uses message clock — for demo force future expiry
        msg["expires_at_utc"] = (
            datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
            .replace(
                datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            )
        )
        # bump expiry +1h from now for smoke
        from datetime import timedelta

        msg["expires_at_utc"] = (
            (datetime.now(timezone.utc) + timedelta(hours=1))
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
        msg["scan_timestamp_utc"] = (
            datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
        batch = {
            "scan_id": msg.get("scan_id", "smoke"),
            "candidates": [msg],
        }
        path = self.inbox / f"smoke-{int(time.time())}.json"
        _atomic_write(path, batch)
        return path


def _atomic_write(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    p = argparse.ArgumentParser(description="dmr-adapter")
    p.add_argument("--process-inbox", action="store_true")
    p.add_argument("--probe-dmr", action="store_true")
    p.add_argument("--seed-example", action="store_true", help="enqueue contracts example")
    p.add_argument("--watch", action="store_true", help="poll inbox every 5s")
    args = p.parse_args()
    adapter = DmrAdapter()

    if args.probe_dmr:
        print(json.dumps(adapter.probe_dmr_package(), indent=2, ensure_ascii=False))
    if args.seed_example:
        path = adapter.seed_example_candidate()
        print(json.dumps({"seeded": str(path)}, indent=2))
    if args.process_inbox or args.seed_example:
        results = adapter.process_inbox(include_done=False)
        print(json.dumps(results, indent=2, ensure_ascii=False))
    if args.watch:
        log.info("watching %s", adapter.inbox)
        while True:
            adapter.process_inbox()
            time.sleep(5)
    if not any([args.probe_dmr, args.seed_example, args.process_inbox, args.watch]):
        p.print_help()
        print("\nprobe:", json.dumps(adapter.probe_dmr_package(), indent=2))


if __name__ == "__main__":
    main()
