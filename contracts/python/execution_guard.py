"""正向 allow-only 执行安全谓词 —— adapter 与 executor **共用同一份实现**。

权威依据
--------
* 文档 A §14（``execution_allowed`` / ``adapter_accept`` / ``executor_accept`` 伪代码）、
  §4.2（冲突矩阵「Y 执行隔离」）、§17.1（必过验收）、§20（上线阻断清单）。
* 文档 B §18.6（adapter 与 executor 的正向执行安全契约）、§20（执行正例/负例矩阵）、
  §21.1（上线验收门槛最后一条）、§23（P0 工作包）。

为什么必须是「正向 allow-only」
-------------------------------
现网原实现只做「未知 parameter_version 就拒绝」这种**否定式**检查：改一下 inbox 路径、
或把 ``param-v2.0.0-screener-y`` 误加进白名单，《选币榜Y》的候选就会被执行层吃掉
（文档 A §2 第 5 条 / §4.2）。否定式清单永远列不全；因此本谓词改成正向白名单：

    **只有** batch 与 candidate 同时满足

        board_key == "main"
        dmr_executable is True
        consumable_by_dmr is True
        九个身份字段非空、类型严格、且与已部署 manifest 完全一致

    才返回 ``True``。missing / null / ``"true"`` 字符串 / 类型错误 / 未知枚举 /
    batch 与 candidate 不一致 / hash 篡改 —— 一律 ``False``。

两道边界都要跑
--------------
``adapter_accept`` 与 ``executor_accept`` 调用**同一个** :func:`execution_allowed`。
executor 不信任 adapter 的 accepted 目录，必须重跑谓词并校验 envelope 完整性
（文档 B §18.6）。环境白名单只能进一步**收紧**，绝不能替代或放宽本谓词。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Mapping, Optional

#: 文档 A §14：执行边界要求非空且严格类型的身份字段全集。
REQUIRED_EXEC_IDENTITY: tuple[str, ...] = (
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

#: 唯一允许执行的板面。文档 A §3：「Y 执行资格永久否」。
EXECUTABLE_BOARD_KEY = "main"

#: 必须严格为 Python ``True`` 的布尔字段（``"true"`` / ``1`` / ``"yes"`` 都不接受）。
STRICT_TRUE_FIELDS: tuple[str, ...] = ("dmr_executable", "consumable_by_dmr")

#: 参与 batch↔candidate↔manifest 三方一致性比较的身份字段。
IDENTITY_MATCH_FIELDS: tuple[str, ...] = (
    "parameter_version",
    "rule_revision",
    "config_hash",
    "mcap_mapping_version",
    "mapping_hash",
    "code_commit",
)

#: 文档 B §18.6：合同模型不可用时整批拒绝的原因码，禁止退化到弱检查。
REASON_CONTRACT_UNAVAILABLE = "CONTRACT_UNAVAILABLE"

ENVELOPE_SCHEMA = "dmr-accepted-envelope-v1"


class GuardResult:
    """谓词结果。``bool(result)`` 即放行与否；``reason`` 供审计与负测断言。"""

    __slots__ = ("allowed", "reason", "detail")

    def __init__(self, allowed: bool, reason: str = "", detail: Any = None) -> None:
        self.allowed = bool(allowed)
        self.reason = reason
        self.detail = detail

    def __bool__(self) -> bool:
        return self.allowed

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"GuardResult(allowed={self.allowed}, reason={self.reason!r})"


def _is_strict_true(v: Any) -> bool:
    """只有 Python ``True`` 通过。``1`` 是 int，``"true"`` 是 str，都拒绝。"""
    return v is True


def strict_required_non_null(
    obj: Optional[Mapping[str, Any]],
    keys: Iterable[str] = REQUIRED_EXEC_IDENTITY,
) -> GuardResult:
    """所有 required 字段必须存在、非 ``None``、类型严格。"""
    if not isinstance(obj, Mapping):
        return GuardResult(False, "NOT_A_MAPPING", type(obj).__name__)
    for k in keys:
        if k not in obj:
            return GuardResult(False, f"MISSING:{k}")
        v = obj[k]
        if v is None:
            return GuardResult(False, f"NULL:{k}")
        if k in STRICT_TRUE_FIELDS:
            if not isinstance(v, bool):
                return GuardResult(False, f"TYPE:{k}", type(v).__name__)
        else:
            if not isinstance(v, str) or not v.strip():
                return GuardResult(False, f"TYPE:{k}", type(v).__name__)
    return GuardResult(True, "OK")


def identities_match(
    batch: Mapping[str, Any],
    candidate: Mapping[str, Any],
    deployed_manifest: Optional[Mapping[str, Any]],
    *,
    keys: Iterable[str] = IDENTITY_MATCH_FIELDS,
) -> GuardResult:
    """batch / candidate / 已部署 manifest 三方逐字节相等。

    ``deployed_manifest is None`` 视为「没有已部署身份可比对」→ 拒绝。
    文档 A §4.2：没有代码/配置身份就不能严格复算，更不能放行执行。
    """
    if not isinstance(deployed_manifest, Mapping):
        return GuardResult(False, "NO_DEPLOYED_MANIFEST")
    for k in keys:
        bv, cv = batch.get(k), candidate.get(k)
        mv = deployed_manifest.get(k)
        if bv is None or cv is None or mv is None:
            return GuardResult(False, f"IDENTITY_NULL:{k}")
        if not (bv == cv == mv):
            return GuardResult(
                False, f"IDENTITY_MISMATCH:{k}", {"batch": bv, "candidate": cv, "manifest": mv}
            )
    return GuardResult(True, "OK")


def execution_allowed(
    batch: Optional[Mapping[str, Any]],
    candidate: Optional[Mapping[str, Any]],
    deployed_manifest: Optional[Mapping[str, Any]],
) -> GuardResult:
    """文档 A §14 的 ``execution_allowed`` —— adapter 与 executor 共用的安全不变量。"""
    r = strict_required_non_null(batch)
    if not r:
        return GuardResult(False, f"BATCH_{r.reason}", r.detail)
    r = strict_required_non_null(candidate)
    if not r:
        return GuardResult(False, f"CANDIDATE_{r.reason}", r.detail)

    assert batch is not None and candidate is not None  # 已由上面的严格检查保证
    if batch["board_key"] != EXECUTABLE_BOARD_KEY:
        return GuardResult(False, "BATCH_BOARD_NOT_MAIN", batch["board_key"])
    if candidate["board_key"] != EXECUTABLE_BOARD_KEY:
        return GuardResult(False, "CANDIDATE_BOARD_NOT_MAIN", candidate["board_key"])
    for who, obj in (("BATCH", batch), ("CANDIDATE", candidate)):
        for k in STRICT_TRUE_FIELDS:
            if not _is_strict_true(obj[k]):
                return GuardResult(False, f"{who}_{k.upper()}_NOT_TRUE", obj[k])
    return identities_match(batch, candidate, deployed_manifest)


def version_whitelisted(
    candidate: Mapping[str, Any], whitelist: Optional[Iterable[str]]
) -> GuardResult:
    """环境白名单**只能收紧**：白名单为空/None 时不放宽任何东西。"""
    if whitelist is None:
        return GuardResult(True, "NO_WHITELIST")
    wl = list(whitelist)
    pv = candidate.get("parameter_version")
    if pv in wl:
        return GuardResult(True, "OK")
    return GuardResult(False, "PARAMETER_VERSION_NOT_WHITELISTED", pv)


def adapter_accept(
    batch: Optional[Mapping[str, Any]],
    candidate: Optional[Mapping[str, Any]],
    deployed_manifest: Optional[Mapping[str, Any]],
    *,
    whitelist: Optional[Iterable[str]] = None,
    contract_available: bool = True,
) -> GuardResult:
    """adapter 侧准入。合同模型不可用 → 整批 ``CONTRACT_UNAVAILABLE`` 拒绝。"""
    if not contract_available:
        return GuardResult(False, REASON_CONTRACT_UNAVAILABLE)
    r = execution_allowed(batch, candidate, deployed_manifest)
    if not r:
        return r
    assert candidate is not None
    return version_whitelisted(candidate, whitelist)


# ---------------------------------------------------------------------------
# accepted envelope（文档 A §14 / 文档 B §18.6）
# ---------------------------------------------------------------------------
def _canonical(obj: Any) -> bytes:
    return json.dumps(
        obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def payload_hash(obj: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(obj)).hexdigest()


def build_accepted_envelope(
    batch_identity: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    deployed_manifest: Optional[Mapping[str, Any]] = None,
    accepted_at_utc: Optional[str] = None,
) -> dict[str, Any]:
    """adapter 写出的 accepted envelope：含 batch identity、candidate 与 payload hash。"""
    env: dict[str, Any] = {
        "schema": ENVELOPE_SCHEMA,
        "batch": dict(batch_identity),
        "candidate": dict(candidate),
        "deployed_manifest": dict(deployed_manifest) if deployed_manifest else None,
        "accepted_at_utc": accepted_at_utc,
    }
    env["payload_hash"] = payload_hash(
        {"batch": env["batch"], "candidate": env["candidate"]}
    )
    return env


def accepted_envelope_integrity_ok(envelope: Optional[Mapping[str, Any]]) -> GuardResult:
    """envelope 形状 + payload hash 校验。裸历史 candidate（无 envelope）一律拒绝。"""
    if not isinstance(envelope, Mapping):
        return GuardResult(False, "ENVELOPE_NOT_A_MAPPING")
    if envelope.get("schema") != ENVELOPE_SCHEMA:
        return GuardResult(False, "ENVELOPE_SCHEMA", envelope.get("schema"))
    for k in ("batch", "candidate", "payload_hash"):
        if not envelope.get(k):
            return GuardResult(False, f"ENVELOPE_MISSING:{k}")
    want = payload_hash(
        {"batch": envelope["batch"], "candidate": envelope["candidate"]}
    )
    if want != envelope["payload_hash"]:
        return GuardResult(False, "ENVELOPE_HASH_TAMPERED")
    return GuardResult(True, "OK")


def executor_accept(
    envelope: Optional[Mapping[str, Any]],
    deployed_manifest: Optional[Mapping[str, Any]],
) -> GuardResult:
    """executor 侧准入：不信任 accepted 目录，重跑同一谓词 + 校验 envelope 完整性。"""
    r = accepted_envelope_integrity_ok(envelope)
    if not r:
        return r
    assert envelope is not None
    return execution_allowed(
        envelope.get("batch"), envelope.get("candidate"), deployed_manifest
    )


__all__ = [
    "ENVELOPE_SCHEMA",
    "EXECUTABLE_BOARD_KEY",
    "IDENTITY_MATCH_FIELDS",
    "REASON_CONTRACT_UNAVAILABLE",
    "REQUIRED_EXEC_IDENTITY",
    "STRICT_TRUE_FIELDS",
    "GuardResult",
    "accepted_envelope_integrity_ok",
    "adapter_accept",
    "build_accepted_envelope",
    "execution_allowed",
    "executor_accept",
    "identities_match",
    "payload_hash",
    "strict_required_non_null",
    "version_whitelisted",
]
