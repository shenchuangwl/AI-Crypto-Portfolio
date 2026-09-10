"""不可变规则身份（RuleManifest）—— 一致性等式的左半边。

权威依据
--------
* 文档 A §13.1（不可变身份字段表）、§13.2（每行血缘）、§14（发布前断言）、§15（阶段）。
* 文档 B §3.1（一致性核心等式）、§3.2（必存身份表）、§5.1（manifest 定义与 hash 口径）、
  §5.2（RawInputManifest）、§5.3（功能开关写进 manifest）。

一致性等式（文档 B §3.1）
-------------------------
::

    同一 raw_input_manifest
      + 同一 parameter_version/rule_revision/config_hash
      + 同一 asset_mapping_version
      + 同一 mcap_mapping_version/mapping_hash
      + 同一 code_commit/indicator_version
      + 同一 data_contract_version/data_version
      + 同一 universe_policy_version/universe_version
      + 同一 scan_id/decision_time
    = 同一 component scores、Score、base_state、mcap ceiling、effective_zone、DMR 结果

任一身份字段不同即为不同规则段；API 与账本默认不得跨段聚合。

四对字段的粒度约定（文档 B §5.1，禁止互作别名）
-----------------------------------------------
============================  ========  ================================================
字段                          粒度      含义
============================  ========  ================================================
``data_contract_version``     规则段    原始输入 Schema、单位、null/as_of 语义的合同版本
``data_version``              节点      本节点 RawInputManifest / 内容集合的实例版本
``universe_policy_version``   规则段    纳入、排除、上市/退市处理政策版本
``universe_version``          节点      decision_time 当时成员快照的实例版本
============================  ========  ================================================

``asset_mapping_version`` 与 ``mcap_mapping_version``
-----------------------------------------------------
现网字段名 ``mapping_version=cg-map`` 只指 CoinGecko 资产映射，语义不明。本模块把它
单向迁移为 ``asset_mapping_version``；216/432 规则表用独立的 ``mcap_mapping_version``
+ ``mapping_hash``。两者**没有别名关系**（文档 B §5.1）。为兼容既有外部合同，
``mapping_version`` 作为只读遗留别名继续输出，但新 revision 的判定只读新字段。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

log = logging.getLogger("coin_selection.rule_manifest")

#: 文档 A §1 / 文档 B §1：本轮目标不可变规则修订。
RULE_REVISION = "y-v2.0.0-r3"

#: 文档 B §5.1：规则段级的输入合同版本与宇宙政策版本。
DATA_CONTRACT_VERSION = "raw-input-contract-v1"
UNIVERSE_POLICY_VERSION = "binance-usdt-perp-v1"

#: 文档 A §13.1：现网 `mapping_version=cg-map` 的无歧义新名。
DEFAULT_ASSET_MAPPING_VERSION = "cg-map"

#: 该规则段不使用 216/432 映射层时的**显式**取值。
#: 文档 A §14 要求执行身份九个字段全部非空；用 ``None`` 会让拒绝原因变成
#: 「字段缺失」而掩盖真正原因，因此改用一个可比较的哨兵串：它参与三方一致性比较，
#: 语义是「本规则段不含 mcap 映射」，而不是「不知道」。
MAPPING_NOT_APPLICABLE = "NOT_APPLICABLE"

#: 文档 A §1 / 文档 B §4.3：工作目录不是 Git 仓库时的诚实占位值。
#: 【上线阻断】严格重放上线前必须由 CI 注入真实 commit 或镜像 digest。
CODE_COMMIT_UNAVAILABLE = "UNAVAILABLE_WORKTREE_NOT_GIT"

#: 文档 A §17.2 / 文档 B §5.3：三个功能开关，默认全 0，只在 00:00 UTC 周期边界生效。
FLAG_MCAP_ZONE = "ENABLE_MCAP_ZONE"
FLAG_TARGET_WEIGHTS = "ENABLE_TARGET_WEIGHTS"
FLAG_EFFECTIVE_ZONE = "ENABLE_MCAP_EFFECTIVE_ZONE"
FEATURE_FLAGS: tuple[str, ...] = (
    FLAG_MCAP_ZONE,
    FLAG_TARGET_WEIGHTS,
    FLAG_EFFECTIVE_ZONE,
)

#: 文档 A §6.1 / 文档 B §6：目标七权重，和精确为 1。
TARGET_WEIGHTS: dict[str, float] = {
    "w_ss": 0.20,
    "w_mom": 0.20,
    "w_liq": 0.15,
    "w_mcap": 0.25,
    "w_cons": 0.10,
    "w_rank": 0.05,
    "w_risk": 0.05,
}

#: 文档 A §6.1：现网（主榜 v1.4.0）权重，作为零漂移对照。
CURRENT_WEIGHTS: dict[str, float] = {
    "w_ss": 0.30,
    "w_mom": 0.25,
    "w_liq": 0.15,
    "w_mcap": 0.10,
    "w_cons": 0.10,
    "w_rank": 0.05,
    "w_risk": 0.05,
}

#: 文档 A §14 / 文档 B §18.6：执行边界要求非空且严格类型的身份字段全集。
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

#: 参与 config_hash 的有效配置字段（补默认值后的解析结果，不是 YAML 文本）。
#: 文档 A §13.1：「config_hash 输入必须是已解析并填默认值后的有效配置」。
CONFIG_SETTINGS_FIELDS: tuple[str, ...] = (
    "parameter_version",
    "w_ss",
    "w_mom",
    "w_liq",
    "w_mcap",
    "w_cons",
    "w_rank",
    "w_risk",
    "dmr_top_k",
    "hard_floor_usd",
    "baseline_universe",
    "enable_gate2",
    "enable_gate3",
    "enable_gate4",
    "enable_state_machine",
    "enable_mcap_tf",
    "enable_long_ret",
)

#: DMR 裁决集选择器的默认值。权威定义在 board_variants.DMR_SELECTION_MODE_DEFAULT；
#: 此处保留一份字面量副本，避免 rule_manifest 反向 import board_variants 形成环。
#: 两处必须一致，test_x_v13_identity.py 有断言看着。
_DMR_MODE_DEFAULT = "strict-v1.4"

CONFIG_STATE_FIELDS: tuple[str, ...] = (
    "enter_watch",
    "exit_watch",
    "min_streak_watch",
    "min_dwell_watch",
    "enter_qualified",
    "enter_qualified_m",
    "ss_qualified",
    "ss_qualified_m",
    "mom_qualified",
    "mom_qualified_m",
    "exit_qualified",
    "min_streak_qualified",
    "min_dwell_qualified",
    "enter_confirmed",
    "ss_confirmed",
    "mom_confirmed",
    "cons_confirmed",
    "dq_confirm_floor",
    "exit_confirmed",
    "hold_ss",
    "dmr_score",
    "dmr_ss",
    "dmr_momentum",
    "dmr_consistency",
    "dmr_dq",
    "min_streak_confirmed",
    "min_dwell_confirmed",
    "scan_interval_min",
    "dmr_top_k",
)

#: 文档 A §20：红线冻结项写进 config_hash —— 有人偷改，身份立刻变化。
FROZEN_BLOCK: dict[str, Any] = {
    "grade_fn": "grade_from_mas@mcap_timeframe",
    "g1_windows_days": [6, 12, 26],
    "g1_floor_usd": 3000000,
    "g2_bars_1h": 168,
    "g2_roc_weights": ["0.25", "0.40", "0.35"],
    "g2_m_roc_rsi": ["0.85", "0.15"],
    "g4_f2_cap": 45,
    "score_cap": "min(raw, 40+0.6*DQ)",
    "s_rank_const": 50,
    "s_risk_const": 100,
}

REPO_ROOT = Path(
    os.environ.get("HERMES_ROOT", str(Path(__file__).resolve().parents[4]))
)

MANIFEST_DIR_NAME = "rule-manifests"


class ManifestError(RuntimeError):
    """身份校验失败。文档 B §8.2：无法归类的身份差异一律停止灰度。"""


# ---------------------------------------------------------------------------
# 规范序列化与 hash
# ---------------------------------------------------------------------------
def _norm(v: Any) -> Any:
    """浮点归一到 9 位小数字符串 —— 避免 0.1+0.2 的表示差异让 hash 抖动。"""
    if isinstance(v, bool):
        return bool(v)
    if isinstance(v, float):
        return f"{round(v, 9):.9f}"
    if isinstance(v, int):
        return int(v)
    if v is None:
        return None
    if isinstance(v, (list, tuple)):
        return [_norm(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _norm(x) for k, x in sorted(v.items())}
    return str(v)


def jcs_bytes(payload: Any) -> bytes:
    """JCS 等价：UTF-8 + 键排序 + 无空白。payload 内不得含裸浮点（先经 :func:`_norm`）。"""
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def sha256_jcs(payload: Any) -> str:
    return "sha256:" + hashlib.sha256(jcs_bytes(payload)).hexdigest()


def effective_config(
    settings: Any,
    state_cfg: Any,
    *,
    cycle: Any = None,
    feature_flags: Optional[dict[str, bool]] = None,
) -> dict[str, Any]:
    """解析、补默认值、单位规范化后的**有效配置**（config_hash 的输入）。

    刻意不收 ``data_dir`` / ``dmr_inbox`` / ``*_workers`` 等运行时字段：
    换个目录跑离线重放不应改变规则身份（文档 B §5.1）。
    """
    flags = dict(feature_flags or {})
    dominance = _mcap_dominance_config(settings)
    return {
        "schema": "effective-config-v1",
        # X现代v1.3复盘身份不可冒用克隆r1；默认main/Y身份原样保留。
        **({"selection_semantics": "dual-path-v1.3"}
           if getattr(state_cfg, "selection_semantics", "staircase-v1.4") == "dual-path-v1.3" else {}),
        # DMR 裁决集选择器：与 selection_semantics 同样条件式省略。
        # 它换的是**谁进 DMR**（实测同一节点 13 → 16 条，且排除 22 条确认边），
        # 不进 config_hash 就等于两套 DMR 规则共用一个规则身份 —— 复盘按
        # param_hash / config_hash 分段统计时会把两个体制混算（D2 明令禁止）。
        # 无条件加键会改掉 main/Y 已发布的 config_hash，因此只能省略式。
        **({"dmr_selection_mode": str(getattr(settings, "dmr_selection_mode", _DMR_MODE_DEFAULT))}
           if str(getattr(settings, "dmr_selection_mode", _DMR_MODE_DEFAULT)) != _DMR_MODE_DEFAULT else {}),
        # 动能下限同样条件式省略：0（历史口径）时整键不出现，main/Y 身份逐字节不变。
        **({"dmr_momentum_floor": f"{float(getattr(settings, 'dmr_momentum_floor', 0.0) or 0.0):.9f}"}
           if float(getattr(settings, 'dmr_momentum_floor', 0.0) or 0.0) > 0 else {}),
        # 确认区 PATH_M 三阈值：**偏离历史 v1.3 原值时才进身份**。
        # 无条件加键会改掉现网 r3 的 pf1_a86b486ddefe82cc —— 那是追溯改写已发布身份。
        **({"confirmed_path_m": {k: f"{float(getattr(state_cfg, k, d)):.9f}"
                                 for k, d in (("enter_confirmed_m", 70.0),
                                              ("ss_confirmed_m", 45.0),
                                              ("mom_confirmed_m", 70.0))}}
           if any(float(getattr(state_cfg, k, d)) != d
                  for k, d in (("enter_confirmed_m", 70.0), ("ss_confirmed_m", 45.0),
                               ("mom_confirmed_m", 70.0))) else {}),
        "settings": {k: _norm(getattr(settings, k, None)) for k in CONFIG_SETTINGS_FIELDS},
        "state_config": {
            k: _norm(getattr(state_cfg, k, None)) for k in CONFIG_STATE_FIELDS
        },
        "cycle": {
            "enabled": bool(getattr(cycle, "enabled", False)),
            "period_hours": int(getattr(cycle, "period_hours", 24) or 24),
            "anchor_utc": str(getattr(cycle, "anchor_utc", "00:00") or "00:00"),
            "clear": sorted(str(x) for x in (getattr(cycle, "clear", ()) or ())),
            "warmup": _norm(
                dict(cycle.warmup_overrides()) if cycle is not None else {}
            ),
        },
        "feature_flags": {k: bool(flags.get(k, False)) for k in FEATURE_FLAGS},
        # v2.0.0 流通市值主导层：授权令牌 + 谓词 / 互印证 / 排序权重。
        #
        # **层关闭时整个键不出现**（不是写 null / 哨兵）：JCS 对键的存在敏感，
        # 无条件加键会连带改掉改造前已发布的 y-v2.0.0-r1 / r2 的 config_hash，
        # 等于追溯改写已生效的历史身份。已发布的 manifest 只增不改（文档A §20）。
        **({"mcap_dominance": dominance} if dominance is not None else {}),
        "frozen": _norm(FROZEN_BLOCK),
    }


def _mcap_dominance_config(settings: Any) -> Optional[dict[str, Any]]:
    """主导层的有效配置片段。层关闭 → ``None``（调用方整键省略）。"""
    from .board_variants import mcap_zone_mode_of

    mode = str(mcap_zone_mode_of(settings))
    if mode == "off":
        return None
    from .mcap_dominance import config_from_settings

    return _norm(
        {
            "mode": mode,
            "authorization": str(
                getattr(settings, "mcap_zone_authorization", None) or ""
            ),
            "ruleset": str(getattr(settings, "mcap_ruleset", "sol5.6") or "sol5.6"),
            # DMR 前置的 ceiling 最低档直接决定 DMR 区成员，必须进规则身份 ——
            # 否则 ceiling=DMR 与 ceiling>=确定 会算出同一个 config_hash，
            # 两套实质不同的规则在账本里无法区分（文档B §3.1 的一致性等式会失效）。
            "dmr_ceiling_min": str(
                getattr(settings, "mcap_dmr_ceiling_min", "DMR") or "DMR"
            ),
            **config_from_settings(settings).as_payload(),
        }
    )


def config_hash(
    settings: Any,
    state_cfg: Any,
    *,
    cycle: Any = None,
    feature_flags: Optional[dict[str, bool]] = None,
) -> str:
    return sha256_jcs(
        effective_config(settings, state_cfg, cycle=cycle, feature_flags=feature_flags)
    )


# ---------------------------------------------------------------------------
# code_commit
# ---------------------------------------------------------------------------
#: 部署提交的事实源。放在 ``data/`` 下、**不进版本控制** —— 部署产物不该是源码，
#: 而且若它进了版本控制就永远比它记录的提交少一个（自指）。
DEPLOYED_COMMIT_FILE = "data/coin-selection/deployed-commit.txt"


def deployed_commit_path(root: Optional[Path] = None) -> Path:
    override = (os.environ.get("HERMES_DEPLOY_COMMIT_FILE") or "").strip()
    if override:
        return Path(override)
    return (Path(root) if root is not None else REPO_ROOT) / DEPLOYED_COMMIT_FILE


def resolve_code_commit(root: Optional[Path] = None) -> str:
    """按优先级解析代码身份；都不可得时返回诚实的占位值。

    优先级::

        1. CI / 容器注入的环境变量
        2. data/coin-selection/deployed-commit.txt   ← 「这次部署的是哪个提交」
        3. git rev-parse HEAD                         ← 开发态回落
        4. UNAVAILABLE_WORKTREE_NOT_GIT               ← 诚实占位

    **第 2 级必须放在模块里，不能只放在某个启动脚本里。** 否则会出现这样的事故：
    选币 loop 的 launcher 钉定了部署提交、写进 inbox 批次，而 dmr-adapter /
    dmr-executor 进程没有钉定、去读 `git HEAD`，两侧算出不同的 code_commit，
    正向 allow-only 谓词就会把**合法的主榜候选**也判成 `IDENTITY_MISMATCH` 拒绝掉。
    该缺陷已在第三轮实测中出现并由本函数修复（验收报告 §7.9.8）。

    文档 A §1 / 文档 B §4.3【上线阻断】：没有代码身份就不能做严格复算，
    但**不能因此伪造一个值** —— 占位值本身就是「不可严格重放」的证据。
    """
    for env in ("HERMES_CODE_COMMIT", "GIT_COMMIT", "CI_COMMIT_SHA", "IMAGE_DIGEST"):
        v = (os.environ.get(env) or "").strip()
        if v:
            return v
    dep = deployed_commit_path(root)
    try:
        if dep.is_file():
            v = dep.read_text(encoding="utf-8").strip()
            if v:
                return v
    except Exception as e:  # noqa: BLE001
        log.warning("deployed-commit file unreadable (%s): %s", dep, e)
    base = Path(root) if root is not None else REPO_ROOT
    if (base / ".git").exists():
        try:
            out = subprocess.run(
                ["git", "-C", str(base), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            sha = (out.stdout or "").strip()
            if out.returncode == 0 and sha:
                return sha
        except Exception as e:  # noqa: BLE001
            log.warning("git rev-parse failed: %s", e)
    return CODE_COMMIT_UNAVAILABLE


def code_commit_available(value: Optional[str]) -> bool:
    return bool(value) and value != CODE_COMMIT_UNAVAILABLE


# ---------------------------------------------------------------------------
# RuleManifest
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RuleManifest:
    """一个 revision 的不可变身份。发布后只增不改（文档 B §5.1）。"""

    board_key: str
    parameter_version: str
    rule_revision: str
    config_hash: str
    asset_mapping_version: str
    mcap_mapping_version: Optional[str]
    mapping_hash: Optional[str]
    code_commit: str
    indicator_version: str
    data_contract_version: str
    universe_policy_version: str
    effective_from_utc: str
    effective_from_scan_id: str
    feature_flags: dict[str, bool] = field(default_factory=dict)
    dmr_executable: bool = False
    consumable_by_dmr: bool = False
    param_hash: Optional[str] = None
    notes: tuple[str, ...] = ()

    # —— 序列化 ——
    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["notes"] = list(self.notes)
        d["feature_flags"] = dict(self.feature_flags)
        return d

    def identity(self) -> dict[str, Any]:
        """写进快照 / inbox / 账本的身份子集（文档 B §3.2）。"""
        return {
            "board_key": self.board_key,
            "parameter_version": self.parameter_version,
            "rule_revision": self.rule_revision,
            "config_hash": self.config_hash,
            "asset_mapping_version": self.asset_mapping_version,
            "mcap_mapping_version": self.mcap_mapping_version,
            "mapping_hash": self.mapping_hash,
            "code_commit": self.code_commit,
            "indicator_version": self.indicator_version,
            "data_contract_version": self.data_contract_version,
            "universe_policy_version": self.universe_policy_version,
            "effective_from_utc": self.effective_from_utc,
            "effective_from_scan_id": self.effective_from_scan_id,
            "dmr_executable": self.dmr_executable,
            "consumable_by_dmr": self.consumable_by_dmr,
            "param_hash": self.param_hash,
            "feature_flags": dict(self.feature_flags),
        }

    def manifest_hash(self) -> str:
        return sha256_jcs(_norm(self.to_dict()))

    # —— 断言 ——
    def assert_publishable(self, *, require_code_commit: bool = False) -> None:
        """发布前断言（文档 A §14 / 文档 B §21.1）。"""
        missing = [
            k
            for k in (
                "board_key",
                "parameter_version",
                "rule_revision",
                "config_hash",
                "asset_mapping_version",
                "indicator_version",
                "data_contract_version",
                "universe_policy_version",
                "effective_from_utc",
                "effective_from_scan_id",
            )
            if not getattr(self, k)
        ]
        if missing:
            raise ManifestError(f"manifest missing required identity: {missing}")
        if not self.effective_from_utc.endswith("T00:00:00Z"):
            # 文档 B §3.3：规则只在 00:00 UTC 周期边界生效。
            raise ManifestError(
                f"effective_from_utc must be a 00:00 UTC cycle boundary, got "
                f"{self.effective_from_utc!r}"
            )
        if require_code_commit and not code_commit_available(self.code_commit):
            raise ManifestError(
                "code_commit unavailable — strict replay/publish is blocked "
                "(文档 A §1 / 文档 B §4.3【上线阻断】)"
            )
        if self.mcap_mapping_version and not self.mapping_hash:
            raise ManifestError("mcap_mapping_version set but mapping_hash missing")


def build_manifest(
    *,
    board_key: str,
    settings: Any,
    state_cfg: Any,
    cycle: Any = None,
    feature_flags: Optional[dict[str, bool]] = None,
    mcap_mapping_version: Optional[str] = None,
    mapping_hash: Optional[str] = None,
    asset_mapping_version: str = DEFAULT_ASSET_MAPPING_VERSION,
    indicator_version: str = "g1g2g3g4-sm-dual-path",
    effective_from_utc: str = "2026-09-01T00:00:00Z",
    effective_from_scan_id: str = "20260901-000",
    dmr_executable: bool = False,
    consumable_by_dmr: bool = False,
    param_hash: Optional[str] = None,
    rule_revision: str = RULE_REVISION,
    code_commit: Optional[str] = None,
    root: Optional[Path] = None,
    notes: Iterable[str] = (),
) -> RuleManifest:
    flags = {k: bool((feature_flags or {}).get(k, False)) for k in FEATURE_FLAGS}
    return RuleManifest(
        board_key=board_key,
        parameter_version=str(getattr(settings, "parameter_version", "") or ""),
        rule_revision=rule_revision,
        config_hash=config_hash(
            settings, state_cfg, cycle=cycle, feature_flags=flags
        ),
        asset_mapping_version=asset_mapping_version,
        mcap_mapping_version=mcap_mapping_version,
        mapping_hash=mapping_hash,
        code_commit=code_commit or resolve_code_commit(root),
        indicator_version=indicator_version,
        data_contract_version=DATA_CONTRACT_VERSION,
        universe_policy_version=UNIVERSE_POLICY_VERSION,
        effective_from_utc=effective_from_utc,
        effective_from_scan_id=effective_from_scan_id,
        feature_flags=flags,
        dmr_executable=bool(dmr_executable),
        consumable_by_dmr=bool(consumable_by_dmr),
        param_hash=param_hash,
        notes=tuple(str(n) for n in notes),
    )


def manifest_dir(root: Optional[Path] = None) -> Path:
    base = Path(root) if root is not None else REPO_ROOT
    return base / "packages" / "config" / MANIFEST_DIR_NAME


def manifest_path(rule_revision: str = RULE_REVISION, root: Optional[Path] = None) -> Path:
    return manifest_dir(root) / f"{rule_revision}.json"


def publish(
    manifest: RuleManifest,
    *,
    path: Optional[Path] = None,
    root: Optional[Path] = None,
    overwrite: bool = False,
) -> Path:
    """把 manifest 冻结到磁盘。已存在且内容不同则拒绝（只增不改，文档 B §5.1）。"""
    p = Path(path) if path is not None else manifest_path(manifest.rule_revision, root)
    manifest.assert_publishable()
    doc = {
        "$comment": [
            "不可变 RuleManifest —— 《选币榜Y》一致性等式的左半边（文档 B §3.1/§5.1）。",
            "发布后只增不改：任何逻辑变化必须递增 rule_revision 并发新文件，",
            "禁止同名版本静默覆盖（文档 A §20【上线阻断】）。",
            "effective_from_* 只允许落在 00:00 UTC 周期边界（文档 B §3.3）。",
        ],
        "schema": "rule-manifest-v1",
        **manifest.to_dict(),
        "manifest_hash": manifest.manifest_hash(),
    }
    if p.exists() and not overwrite:
        try:
            old = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            old = None
        if isinstance(old, dict):
            old_cmp = {k: v for k, v in old.items() if k not in ("$comment",)}
            new_cmp = {k: v for k, v in doc.items() if k not in ("$comment",)}
            if old_cmp != new_cmp:
                raise ManifestError(
                    f"refusing to overwrite published manifest {p.name} with different "
                    f"content — bump rule_revision instead"
                )
            return p
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, p)
    return p


def load(
    rule_revision: str = RULE_REVISION,
    *,
    path: Optional[Path] = None,
    root: Optional[Path] = None,
) -> RuleManifest:
    p = Path(path) if path is not None else manifest_path(rule_revision, root)
    doc = json.loads(p.read_text(encoding="utf-8"))
    fields = {
        k: doc.get(k)
        for k in (
            "board_key",
            "parameter_version",
            "rule_revision",
            "config_hash",
            "asset_mapping_version",
            "mcap_mapping_version",
            "mapping_hash",
            "code_commit",
            "indicator_version",
            "data_contract_version",
            "universe_policy_version",
            "effective_from_utc",
            "effective_from_scan_id",
            "dmr_executable",
            "consumable_by_dmr",
            "param_hash",
        )
    }
    m = RuleManifest(
        **fields,  # type: ignore[arg-type]
        feature_flags=dict(doc.get("feature_flags") or {}),
        notes=tuple(doc.get("notes") or ()),
    )
    want = doc.get("manifest_hash")
    if want and want != m.manifest_hash():
        raise ManifestError(f"manifest_hash mismatch in {p}")
    return m


# ---------------------------------------------------------------------------
# 生效边界解析（文档 B §3.3：规则只在 00:00 UTC 周期边界生效）
# ---------------------------------------------------------------------------
def list_published(
    board_key: Optional[str] = None, *, root: Optional[Path] = None
) -> list["RuleManifest"]:
    """列出已发布的 manifest，按 ``effective_from_scan_id`` 升序。

    坏文件只 WARN 跳过 —— 一个损坏的历史 manifest 不该让在线扫描失败；
    但它也永远不会被选为生效版本，因为它压根进不了列表。
    """
    d = manifest_dir(root)
    if not d.is_dir():
        return []
    out: list[RuleManifest] = []
    for f in sorted(d.glob("*.json")):
        try:
            m = load(path=f)
        except Exception as e:  # noqa: BLE001
            log.warning("skip unreadable manifest %s: %s", f.name, e)
            continue
        if board_key is None or m.board_key == board_key:
            out.append(m)
    out.sort(key=lambda m: (m.effective_from_scan_id, m.rule_revision))
    return out


def effective_manifest(
    board_key: str, scan_id: str, *, root: Optional[Path] = None
) -> Optional["RuleManifest"]:
    """取在 ``scan_id`` 这一刻**已经生效**的那份 manifest。

    文档 B §3.3：「规则只在 00:00 UTC 周期边界生效，禁止周期中途切换。」
    因此判据就是 ``effective_from_scan_id <= scan_id``（两者都是 ``YYYYMMDD-NNN``，
    定长且左侧补零，字符串比较即时间序）。取满足条件里最晚的一份。

    这样 revision 的切换是**数据驱动**的：发布一份新 manifest 并把
    ``effective_from`` 设在未来的周期边界，到点自动生效，不需要改代码、
    也不需要卡着时间点重启进程。
    """
    cands = [
        m
        for m in list_published(board_key, root=root)
        if m.effective_from_scan_id <= str(scan_id)
    ]
    return cands[-1] if cands else None


#: 身份漂移的**语义**子集 —— 这些字段描述「这一轮到底跑了哪套规则」。
#:
#: 任一项与已生效 manifest 不符，就意味着板面正在用一套规则出数、却挂着另一套规则的
#: 身份。受 :data:`mcap_dominance.GOVERNED_PARAMETER_PREFIX` 约束的板面遇到这种漂移
#: **必须拒绝出数**（文档 B §8.2 IDENTITY_MISMATCH 的阻断化）。
#:
#: ``code_commit`` 刻意**不在**此列：它每次部署都会变，是「代码动过」的信号而不是
#: 「规则不同」的证据（见 fc026c7「消除身份随每次提交漂移」）。它继续只 WARN。
SEMANTIC_IDENTITY_FIELDS: tuple[str, ...] = (
    "parameter_version",
    "config_hash",
    "asset_mapping_version",
    "mcap_mapping_version",
    "mapping_hash",
    "indicator_version",
    "data_contract_version",
    "universe_policy_version",
)


def semantic_drift(drift: "list[str] | tuple[str, ...]") -> list[str]:
    """从 :func:`identity_drift` 的结果里挑出**语义**漂移字段（丢掉 code_commit）。"""
    return [k for k in drift if k in SEMANTIC_IDENTITY_FIELDS]


def identity_drift(
    manifest: "RuleManifest", live: dict[str, Any]
) -> list[str]:
    """已发布 manifest 与在线现算身份的逐字段比对，返回漂移字段名。

    ``config_hash`` / ``code_commit`` 永远**现算**（它们描述的是「这一轮实际用了
    什么」），manifest 描述的是「发布时冻结了什么」。两者不一致不是错误，而是
    「配置或代码在发布之后动过」的信号 —— 必须可见，所以写进快照的
    ``rule_identity.identity_status``（文档 B §8.2 的 IDENTITY_MISMATCH 归类）。
    """
    drift: list[str] = []
    for k in (
        "parameter_version",
        "config_hash",
        "asset_mapping_version",
        "mcap_mapping_version",
        "mapping_hash",
        "code_commit",
        "indicator_version",
        "data_contract_version",
        "universe_policy_version",
    ):
        want = getattr(manifest, k, None)
        got = live.get(k)
        if want != got:
            drift.append(k)
    return drift


# ---------------------------------------------------------------------------
# legacy 迁移（文档 B §19.1 步骤 3）
# ---------------------------------------------------------------------------
LEGACY_INCOMPLETE = "LEGACY_INCOMPLETE"


def migrate_legacy_identity(row: dict[str, Any]) -> dict[str, Any]:
    """把旧行的 ``mapping_version=cg-map`` 单向迁移为 ``asset_mapping_version``。

    缺失的 rule/config/mcap/code/data-contract/universe-policy 身份**保留 null**
    并标 ``identity_status=LEGACY_INCOMPLETE``；严禁用当前值反推补贴历史。
    """
    out = dict(row)
    if out.get("asset_mapping_version") in (None, ""):
        legacy = out.get("mapping_version")
        if legacy not in (None, ""):
            out["asset_mapping_version"] = legacy
    required = (
        "rule_revision",
        "config_hash",
        "mcap_mapping_version",
        "mapping_hash",
        "code_commit",
        "data_contract_version",
        "universe_policy_version",
    )
    if any(out.get(k) in (None, "") for k in required):
        out["identity_status"] = LEGACY_INCOMPLETE
    else:
        out.setdefault("identity_status", "COMPLETE")
    return out


def identities_match(
    a: dict[str, Any],
    b: dict[str, Any],
    *,
    keys: Iterable[str] = REQUIRED_EXEC_IDENTITY,
) -> bool:
    """逐字节相等比较；任一侧缺字段即 False（文档 B §8.1 身份层）。"""
    for k in keys:
        if k not in a or k not in b:
            return False
        if a[k] != b[k]:
            return False
    return True


__all__ = [
    "CODE_COMMIT_UNAVAILABLE",
    "DEPLOYED_COMMIT_FILE",
    "deployed_commit_path",
    "effective_manifest",
    "identity_drift",
    "SEMANTIC_IDENTITY_FIELDS",
    "semantic_drift",
    "list_published",
    "MAPPING_NOT_APPLICABLE",
    "CONFIG_SETTINGS_FIELDS",
    "CONFIG_STATE_FIELDS",
    "CURRENT_WEIGHTS",
    "DATA_CONTRACT_VERSION",
    "DEFAULT_ASSET_MAPPING_VERSION",
    "FEATURE_FLAGS",
    "FLAG_EFFECTIVE_ZONE",
    "FLAG_MCAP_ZONE",
    "FLAG_TARGET_WEIGHTS",
    "FROZEN_BLOCK",
    "LEGACY_INCOMPLETE",
    "REQUIRED_EXEC_IDENTITY",
    "RULE_REVISION",
    "TARGET_WEIGHTS",
    "UNIVERSE_POLICY_VERSION",
    "ManifestError",
    "RuleManifest",
    "build_manifest",
    "code_commit_available",
    "config_hash",
    "effective_config",
    "identities_match",
    "jcs_bytes",
    "load",
    "manifest_dir",
    "manifest_path",
    "migrate_legacy_identity",
    "publish",
    "resolve_code_commit",
    "sha256_jcs",
]
