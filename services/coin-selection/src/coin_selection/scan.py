"""Coin-selection scan: Gate1–4 + dual-path state machine → board + DMR candidates.

Production parameter: param-v1.4.0-staircase-confirm-dmr
  G1 liquidity hard floor $3M (6/12/26d) — frozen
  G2 momentum (1h ROC/RSI) — frozen formula
  G3 CoinGecko map + supply + mcap path — frozen formula
  G4 provisional staircase SS (F2: steps<2 → SS≤45) — frozen
  SM: QUALIFIED may use PATH_S/PATH_M; CONFIRMED requires staircase PATH_S only
  DMR zone: stricter LIVE staircase subset of CONFIRMED, unique-symbol Top-K=16
  regime is hardcoded 0.5 — not a live thermometer
"""

from __future__ import annotations

import hashlib
import json
import logging
import fcntl
import os
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from .daily_ledger import update_daily_unique
from .review_ledger import ingest_scan as ingest_review_scan
from .envutil import load_dotenv
from .gate1 import HARD_FLOOR_USD, result_to_dict as g1_dict, run_gate1
from .gate2 import KlineCache, result_to_dict as g2_dict, roc, run_gate2
from .gate3 import (
    has_coingecko_key,
    parse_multiplier,
    result_to_dict as g3_dict,
    run_gate3,
    use_pro_host,
)
from .gate4 import result_to_dict as g4_dict, run_gate4
from .long_returns import (
    PERIODS as LONG_RET_PERIODS,
    row_fields as long_ret_row_fields,
    run_long_returns,
)
from .mcap_timeframe import (
    TIMEFRAMES as MCAP_TIMEFRAMES,
    row_fields as mcap_tf_row_fields,
    run_mcap_timeframes,
)
from .state_machine import (
    StateMachineStore,
    apply_state_machine,
    backfill_enter_prices_from_snapshots,
    cons_is_one,
    default_state_config,
    dmr_zone_ok,
    floor_to_node,
    occupancy_from_rows,
    path_confirmed,
    path_qualified,
    quantize_enter_times,
    scan_control_flags,
)

log = logging.getLogger("coin_selection")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def resolve_anchor_00utc(now: Optional[datetime] = None) -> datetime:
    now = now or utc_now()
    return datetime(now.year, now.month, now.day, 0, 0, 0, tzinfo=timezone.utc)


def scan_sequence(now: datetime, anchor: datetime) -> int:
    return int((now - anchor).total_seconds() // 900)


def make_scan_id(anchor: datetime, seq: int) -> str:
    return f"{anchor.strftime('%Y%m%d')}-{seq:03d}"


def node_time(anchor: datetime, seq: int) -> datetime:
    """Canonical timestamp of scan node ``seq`` — the only time the board publishes.

    ``scan_id`` was always quantized (``seq = ⌊(now-anchor)/900⌋``) but the wall
    clock was not, so an off-cadence run (``--force``, a restart, a slow wake)
    used to stamp e.g. 17:52:14 into node 071 whose real node is 17:45:00. Every
    dwell computed against that stamp inherited the drift permanently.
    """
    return anchor + timedelta(seconds=seq * 900)


# Loop catch-up tunables. The floor keeps a degenerate "scan finishes instantly"
# configuration (every gate skipped) from spinning the CPU.
LOOP_CATCHUP_FLOOR_SEC = 2.0
LOOP_CATCHUP_ALERT = 4


def pending_node_id(now: Optional[datetime] = None) -> str:
    """``scan_id`` of the node the wall clock is in *right now*."""
    now = now or utc_now()
    anchor = resolve_anchor_00utc(now)
    return make_scan_id(anchor, scan_sequence(now, anchor))


def node_overrun(scanned_scan_id: Optional[str], now: Optional[datetime] = None) -> Optional[str]:
    """The node that still needs scanning because the last scan overran into it.

    A scan stamps the node it **started** in. When it finishes inside a later
    node, that later node has no snapshot yet — and the loop's old "sleep to the
    next boundary" arithmetic would jump straight past it, losing it for good.
    Three nodes were lost exactly this way before this guard existed:
    ``20260815-020``, ``20260817-005``, ``20260824-046``.

    ``scan_id`` is ``YYYYMMDD-NNN`` with a zero-padded sequence, so a plain
    string compare is chronological and survives the 00:00 UTC anchor rollover
    (``20260825-000`` > ``20260824-095``).

    Returns the pending node id, or ``None`` when nothing was skipped.
    """
    if not scanned_scan_id:
        return None
    pending = pending_node_id(now)
    return pending if pending > str(scanned_scan_id) else None


@dataclass
class SelectionSettings:
    hermes_root: Path = field(
        default_factory=lambda: Path(
            os.environ.get(
                "HERMES_ROOT",
                str(Path(__file__).resolve().parents[4]),
            )
        )
    )
    market_ingest_url: str = os.environ.get(
        "MARKET_INGEST_URL", "http://127.0.0.1:18100"
    )
    market_ingest_data: Optional[str] = os.environ.get("MARKET_INGEST_DATA_DIR")
    data_dir: Optional[str] = None
    system_version: str = "v1.2"
    parameter_version: str = "param-v1.4.0-staircase-confirm-dmr"
    dmr_top_k: int = int(os.environ.get("DMR_TOP_K", "16"))
    baseline_universe: int = 526
    dmr_inbox: Optional[str] = None
    fapi_rest: str = os.environ.get("BINANCE_FAPI_REST", "https://fapi.binance.com")
    gate1_max_symbols: Optional[int] = (
        int(os.environ["GATE1_MAX_SYMBOLS"])
        if os.environ.get("GATE1_MAX_SYMBOLS")
        else None
    )
    gate1_workers: int = int(os.environ.get("GATE1_WORKERS", "8"))
    gate1_sleep_sec: float = float(os.environ.get("GATE1_SLEEP_SEC", "0.04"))
    hard_floor_usd: float = float(
        os.environ.get("GATE1_HARD_FLOOR_USD", str(HARD_FLOOR_USD))
    )
    enable_gate2: bool = os.environ.get("ENABLE_GATE2", "1") not in ("0", "false", "no")
    enable_gate3: bool = os.environ.get("ENABLE_GATE3", "1") not in ("0", "false", "no")
    enable_gate4: bool = os.environ.get("ENABLE_GATE4", "1") not in ("0", "false", "no")
    # 选币榜 30m/2h/6h 流通市值等级列。
    #
    # 主榜 v1.4.0：确实只做板面展示 —— 不参与 G1-G4 打分，也不进状态机。
    # 选币榜Y v2.0.0：**恰恰相反**，这三列是准入 / 退出 / 分区 / 排序的第一裁决
    # （mcap_effective 的 max(base,ceiling) + mcap_dominance 的 P1/P2/P3 与 RankKey）。
    # 本开关只管「要不要取这三列」，不代表它们下游无人消费。
    enable_mcap_tf: bool = os.environ.get("ENABLE_MCAP_TF", "1") not in (
        "0",
        "false",
        "no",
    )
    # 选币榜 1Week / 1Month 涨跌幅列（同样只做展示，不进 Score / 状态机 / DMR）
    enable_long_ret: bool = os.environ.get("ENABLE_LONG_RET", "1") not in (
        "0",
        "false",
        "no",
    )
    enable_state_machine: bool = os.environ.get("ENABLE_STATE_MACHINE", "1") not in (
        "0",
        "false",
        "no",
    )
    # 选币榜Y 216 组合天花板（只降不升）。默认 False：主板与未开 Y 都不受影响。
    # env ENABLE_MCAP_ZONE 只在变体 key=y 时由 zone_enabled_for_variant 读取，
    # 不得在这里默认读 env，否则会污染主板 SelectionSettings。
    enable_mcap_zone: bool = False
    # —— 文档A §4.7 / 文档B 阶段 1–2：216 组合天花板层的唯一配置入口 ——
    #
    # ``mcap_zone_mode`` 是权威三态开关（文档B 阶段 2 用它取代布尔量）：
    #   off    缺省。函数是恒等映射，Y 板面与现网**逐字节相同**（红线第 14 条）。
    #   shadow 算并落库（zone_ceiling / combo_code / z_score / MCAP_ZONE_* 码），
    #          但**不改 state、不改 DMR 成员** —— 用真实实时数据积累「开了会怎样」。
    #   on     天花板生效：最终分区 = min(状态机分区, 组合天花板)，只降不升。
    # 布尔 ``enable_mcap_zone`` 保留为向后兼容别名（True ⇒ on）。
    mcap_zone_mode: str = "off"
    # Independent X-only DMR constraint; never applies ceiling to four states.
    dmr_selection_mode: str = "strict-v1.4"
    #: X v1.3 两种 DMR 模式下的**动能下限**。0 = 关闭（历史 v1.3.0 原样：DMR 无质量门）。
    #:
    #: 历史 v1.3.0 的 DMR 是「确认区直接吃」，没有任何分数/动能门槛，因此复刻时
    #: 默认必须是 0 —— 一开就不是 v1.3.0 了。但实测（staging 2139 节点）动能与
    #: 收益单调相关且 75 是分水岭：确认区 M<75 合计 -249.7%，M>=75 合计 +219.4%。
    #: 该字段让「加不加、加多少」成为可回测的配置，而不是改代码。
    #: 非 0 时进身份指纹（换了门槛就是换了规则，账本必须能分段）。
    dmr_momentum_floor: float = 0.0
    #: 4 个切点是本层**唯一**的可调自由度，其余全部由文档A §4.2 的 Z 决定。
    #: 【建议参数·待回测】—— 未经样本外验证不得据此宣称达标。
    mcap_zone_cut_dmr: float = 2.1
    mcap_zone_cut_confirmed: float = 1.5
    mcap_zone_cut_qualified: float = 0.8
    mcap_zone_cut_watch: float = -0.5
    #: 三周期任一 grade=None（信息缺失）→ 降一级、在观察止步（文档A §3.4）。
    mcap_zone_abstain_demote: bool = True
    # —— ChatGpt_SOL5.6 文档 A §9.4 / §10 的有效区口径（本轮新增）——
    #
    # ``mcap_ruleset`` 选择 216 天花板用哪一套裁决：
    #   "sol5.6"  ChatGpt_SOL5.6 文档A §9.4 生成器 + 附录A 冻结表（每侧 12/23/29/53/99），
    #             合并公式 effective_rank = max(base_rank, ceiling_rank)，
    #             三周期任一 null → ceiling=WATCH（文档A §9.5）。**本轮默认口径。**
    #   "opus5"   第二轮 Claude_Opus5 的切点式天花板（每侧 13/22/32/72/77），
    #             保留为消融基线，只能通过显式配置选用。
    #
    # 开关仍然只有 ``mcap_zone_mode`` 一个（off / shadow / on），因此缺省
    # ``mcap_zone_mode="off"`` 时两套口径都不写任何字段，板面逐字段退化为现网。
    mcap_ruleset: str = "sol5.6"
    # —— v2.0.0「流通市值主导」层（coin_selection.mcap_dominance）——
    #
    # ``mcap_zone_authorization`` 是 216 主导层的**显式人工授权令牌**。
    # ``mcap_zone_mode="on"`` 单独**不足以**启用主导层：令牌必须精确等于
    # ``mcap_dominance.AUTHORIZATION_TOKEN``。缺失或不符时，受约束的
    # ``param-v2.0.0-screener-y`` 板面会**拒绝出数**（不降级、不冒充 v2.0.0）。
    # 令牌进 config_hash，因此授权动作本身可审计、可追溯。
    mcap_zone_authorization: Optional[str] = None
    # DMR 前置要求的方向 ceiling 最低档（"DMR" | "CONFIRMED" | "QUALIFIED"）。
    # 文档A §5「ceiling 允许」与 §10.2「必须等于 DMR」口径不一，这里参数化。
    mcap_dmr_ceiling_min: str = "DMR"
    # 维度谓词 P1/P2/P3（文档A §5.2）。留 None = 用 DominanceConfig 的默认值。
    mcap_p1_mom_gte: Optional[float] = None
    mcap_p1_cons_eq: Optional[float] = None
    mcap_p2_z_gte: Optional[float] = None
    mcap_p2_mom_lt: Optional[float] = None
    mcap_p3_z_gte: Optional[float] = None
    mcap_p3_mom_opposite_gte: Optional[float] = None
    mcap_p3_cons_opposite_eq: Optional[float] = None
    # SS × 流通市值互印证（文档A §6）
    mcap_xc_aligned_ss_gte: Optional[float] = None
    mcap_xc_aligned_z_gte: Optional[float] = None
    mcap_xc_divergent_ss_gte: Optional[float] = None
    mcap_xc_divergent_z_lte: Optional[float] = None
    # 排序权重体系 W_base/W_prio/W_K 与 W_final 动态因子（文档A §5.6 + 本轮补齐）
    mcap_w_base: Optional[dict] = None
    mcap_w_k: Optional[dict] = None
    mcap_w_prio_base: Optional[float] = None
    mcap_w_prio_step: Optional[float] = None
    mcap_f_p2_soften: Optional[float] = None
    mcap_f_p3_veto: Optional[float] = None
    mcap_f_divergent: Optional[float] = None
    mcap_f_incomplete: Optional[float] = None
    # DMR 精选要求的最差方向天花板："DMR"（文档A §10.2 严格口径）/ "CONFIRMED" / …
    # 见 mcap_dominance.DominanceConfig.dmr_ceiling_min（文档自身在此处不一致）。
    mcap_dmr_ceiling_min: Optional[str] = None
    # DMR 精选的方向 Z10 带通区间（实证新增；None = 退化为文档的单调高通口径）
    mcap_dmr_z10_min: Optional[int] = None
    mcap_dmr_z10_max: Optional[int] = None
    gate2_workers: int = int(os.environ.get("GATE2_WORKERS", "8"))
    mcap_tf_workers: int = int(os.environ.get("MCAP_TF_WORKERS", "10"))
    # 冒烟 / 实验室用：限制参与三列计算的合约数（生产留空 = 全宇宙）
    mcap_tf_max_symbols: Optional[int] = (
        int(os.environ["MCAP_TF_MAX_SYMBOLS"])
        if os.environ.get("MCAP_TF_MAX_SYMBOLS")
        else None
    )
    long_ret_workers: int = int(os.environ.get("LONG_RET_WORKERS", "8"))
    # 冒烟 / 实验室用：限制参与两列计算的合约数（生产留空 = 全宇宙）
    long_ret_max_symbols: Optional[int] = (
        int(os.environ["LONG_RET_MAX_SYMBOLS"])
        if os.environ.get("LONG_RET_MAX_SYMBOLS")
        else None
    )
    # scoring weights v1.2-ish: SS 30, mom 25, liq 15, mcap 10, cons 10, rank 5, risk 5
    w_ss: float = 0.30
    w_mom: float = 0.25
    w_liq: float = 0.15
    w_mcap: float = 0.10
    w_cons: float = 0.10
    w_rank: float = 0.05
    w_risk: float = 0.05

    def __post_init__(self) -> None:
        # Env overrides let a smoke/replay run write to a scratch tree instead of
        # clobbering the live board, state_machine.json and DMR inbox.
        if self.data_dir is None:
            self.data_dir = os.environ.get("COIN_SELECTION_DATA_DIR") or str(
                self.hermes_root / "data" / "coin-selection"
            )
        if self.market_ingest_data is None:
            self.market_ingest_data = str(self.hermes_root / "data" / "market-ingest")
        if self.dmr_inbox is None:
            self.dmr_inbox = os.environ.get("DMR_INBOX_DIR") or str(
                self.hermes_root / "data" / "dmr-adapter" / "inbox"
            )
        self.dmr_top_k = max(12, min(20, int(self.dmr_top_k or 16)))


class FileScanLock:
    def __init__(self, lock_dir: Path):
        self.lock_dir = lock_dir
        self.lock_dir.mkdir(parents=True, exist_ok=True)

    #: 完成台账里保留多少个最近节点。96 个/天，400 ≈ 4 天，足够覆盖任何重启窗口。
    COMPLETED_KEEP = 400

    def acquire(self, scan_id: str, ttl_sec: int = 3600) -> bool:
        """**进行中**锁：另一个进程正在扫这个节点就返回 False。

        这把锁刻意可以被 ``--force`` 绕过 —— 那正是 ``--force`` 存在的理由：
        进程崩了会留下陈旧锁，运维必须能越过它把循环拉起来。
        它**不表示**「这个节点已经跑完了」，那是 :meth:`is_complete` 的职责。
        """
        path = self.lock_dir / f"scan_{scan_id}.lock"
        now = time.time()
        if path.exists():
            try:
                meta = json.loads(path.read_text(encoding="utf-8"))
                if now - float(meta.get("ts", 0)) < ttl_sec:
                    return False
            except Exception:
                pass
        path.write_text(json.dumps({"ts": now, "pid": os.getpid()}), encoding="utf-8")
        return True

    # —— 完成台账（--force 不得绕过）——
    #
    # 「陈旧的进行中锁」与「这个节点已经跑完了」是两件事，此前混为一谈：只有前者，
    # 而且 --force 把它整个短路（生产启动脚本硬编码 --force，locks/ 至今 0 个文件）。
    # 于是一次重启就能把同一个 scan_id 重跑一遍，状态机连击 +2 —— 实测 20260901-004。
    #
    # 完成台账只回答后者，并且**故意不受 --force 影响**：重跑一个已完成的节点从来
    # 不是运维想要的，它只会污染状态机与快照。确需重跑（回补、离线重放）时用
    # ``COIN_SELECTION_ALLOW_RERUN=1`` 显式放行，日志会写明。
    @property
    def _completed_path(self) -> Path:
        return self.lock_dir / "completed.json"

    def _completed(self) -> list[str]:
        try:
            return list(json.loads(self._completed_path.read_text(encoding="utf-8")) or [])
        except Exception:
            return []

    def is_complete(self, scan_id: str) -> bool:
        return str(scan_id) in set(self._completed())

    def mark_complete(self, scan_id: str) -> None:
        """把 ``scan_id`` 记进完成台账。**并发安全**（读-改-写全程持排他锁）。

        为什么需要锁：``mark_complete`` 是读-改-写。两个进程同时跑（loop 正在跑、
        运维又手工起了一次 ``--force`` 补节点）时，后写的会用自己那份陈旧快照
        覆盖先写的，**丢掉的正是"这个节点已经跑完了"的记录** —— 于是重跑守卫失效，
        又回到状态机连击 +2 的老问题。

        锁加在独立的 ``completed.lock`` 上，不能加在 ``completed.json`` 自己身上：
        ``os.replace`` 换的是 inode，锁会跟着旧 inode 一起被丢掉。

        临时文件名带 pid：固定名 ``completed.tmp`` 会让两个写者互相截断对方写到
        一半的内容，``os.replace`` 于是可能把半个 JSON 提交上去。
        """
        lock_path = self.lock_dir / "completed.lock"
        with open(lock_path, "a+", encoding="utf-8") as fh:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            except OSError as e:  # noqa: BLE001 - 某些文件系统不支持 flock
                # 退化为无锁写：单进程下行为不变，多进程下退回改造前的语义。
                # 不阻断扫描 —— 完成台账是保护性设施，不该成为新的故障点。
                log.warning("completed ledger lock unavailable (%s); writing unlocked", e)
            ids = [x for x in self._completed() if x != str(scan_id)]
            ids.append(str(scan_id))
            ids = ids[-self.COMPLETED_KEEP :]
            tmp = self._completed_path.with_name(f"completed.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(ids), encoding="utf-8")
            os.replace(tmp, self._completed_path)


def http_get_json(url: str, timeout: float = 10.0) -> Any:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def load_universe(settings: SelectionSettings) -> tuple[list[dict[str, Any]], str, str]:
    try:
        body = http_get_json(settings.market_ingest_url.rstrip("/") + "/v1/universe")
        symbols = body.get("symbols") or []
        if symbols:
            return symbols, body.get("version") or "", "market-ingest-http"
    except Exception as e:
        log.warning("market-ingest http universe failed: %s", e)
    path = Path(settings.market_ingest_data or "") / "universe.json"
    if path.is_file():
        body = json.loads(path.read_text(encoding="utf-8"))
        return body.get("symbols") or [], body.get("version") or "", "market-ingest-file"
    return [], "", "empty"


def load_prices(settings: SelectionSettings) -> dict[str, Any]:
    try:
        body = http_get_json(settings.market_ingest_url.rstrip("/") + "/v1/prices")
        return body.get("prices") or {}
    except Exception:
        path = Path(settings.market_ingest_data or "") / "prices.json"
        if path.is_file():
            return (json.loads(path.read_text(encoding="utf-8"))).get("prices") or {}
    return {}


#: 主榜的不可变规则修订。主榜参数在文档A §3 里被冻结，因此这里是一个固定串；
#: 《选币榜Y》用的是 rule_manifest.RULE_REVISION（y-v2.0.0-r1）。
MAIN_RULE_REVISION = "main-v1.4.0-r1"


def composite_score(
    settings: SelectionSettings,
    *,
    ss: float,
    mom: float,
    liq: float,
    mcap: float,
    cons: float,
    risk: float = 100.0,
    dq: float = 100.0,
) -> float:
    raw = (
        settings.w_ss * ss
        + settings.w_mom * mom
        + settings.w_liq * liq
        + settings.w_mcap * mcap
        + settings.w_cons * (cons * 100.0)
        + settings.w_rank * 50.0
        + settings.w_risk * risk
    )
    # data quality cap: 40 + 0.6*dq
    cap = 40.0 + 0.6 * dq
    return max(0.0, min(100.0, min(raw, cap)))


def main_exec_identity(settings: "SelectionSettings") -> dict[str, Any]:
    """主榜（唯一可执行板面）的执行身份块。

    【ChatGpt_SOL5.6 文档A §14 / 文档B §18.6】adapter 与 executor 的正向 allow-only
    谓词要求 batch 与 candidate 双方都带这九个字段，且与已部署 manifest 完全一致。
    这里只**声明身份**，不改变主榜任何一条打分或状态机规则。
    """
    from .board_variants import get_variant, variant_state_config
    from .rule_manifest import (
        FEATURE_FLAGS,
        MAPPING_NOT_APPLICABLE,
        config_hash,
        resolve_code_commit,
    )
    from .state_machine import default_state_config

    # 与 packages/config/rule-manifests/main-v1.4.0-r1.json 的发布口径**逐参数相同**：
    # 同一个 config_hash 输入（variant 的 cycle + 三个开关全 False），否则三方一致性
    # 比较会因为「同一套配置算出两个 hash」而永远失败。
    variant = get_variant("main")
    cfg = variant_state_config(variant) if variant is not None else default_state_config()
    return {
        "board_key": "main",
        "dmr_executable": True,
        "consumable_by_dmr": True,
        "parameter_version": str(settings.parameter_version or ""),
        "rule_revision": MAIN_RULE_REVISION,
        "config_hash": config_hash(
            settings,
            cfg,
            cycle=getattr(variant, "cycle", None),
            feature_flags={k: False for k in FEATURE_FLAGS},
        ),
        # 主榜不使用 216/432 映射层 —— 显式声明「不适用」，不是「未知」。
        "mcap_mapping_version": MAPPING_NOT_APPLICABLE,
        "mapping_hash": MAPPING_NOT_APPLICABLE,
        "code_commit": resolve_code_commit(),
    }


def build_rows(
    symbols: list[dict[str, Any]],
    prices: dict[str, Any],
    g1: dict[str, Any],
    g2: dict[str, Any],
    g3: dict[str, Any],
    g4: dict[str, Any],
    settings: SelectionSettings,
    mcap_tf: Optional[dict[str, Any]] = None,
    long_ret: Optional[dict[str, Any]] = None,
) -> list[dict[str, Any]]:
    rows = []
    mcap_tf = mcap_tf or {}
    long_ret = long_ret or {}
    for s in symbols:
        sym = s["symbol"]
        p = prices.get(sym) or {}
        a = g1.get(sym) or {}
        b = g2.get(sym) or {}
        c = g3.get(sym) or {}
        d = g4.get(sym) or {}
        hard = bool(a.get("liquidity_hard_pass"))
        liq = float(a.get("liquidity_score_abs") or 0)
        m_up = float(b.get("momentum_score_up") or 50)
        m_dn = float(b.get("momentum_score_down") or 50)
        c_up = float(b.get("consistency_up") or 0)
        c_dn = float(b.get("consistency_down") or 0)
        mc_up = float(c.get("mcap_momentum_score_up") or 50)
        mc_dn = float(c.get("mcap_momentum_score_down") or 50)
        ss_up = float(d.get("ss_up") or 0)
        ss_dn = float(d.get("ss_down") or 0)
        dq = float(c.get("data_quality_score") or (80 if hard else 40))
        supply_missing = bool(c.get("supply_missing", True)) if c else True
        score_up = composite_score(
            settings, ss=ss_up, mom=m_up, liq=liq, mcap=mc_up, cons=c_up, dq=dq
        )
        score_dn = composite_score(
            settings, ss=ss_dn, mom=m_dn, liq=liq, mcap=mc_dn, cons=c_dn, dq=dq
        )
        # provisional states before SM (SM will overwrite)
        if not hard:
            su = sd = "ELIMINATED" if a else "NONE"
        elif supply_missing and not c:
            su = sd = "WATCH" if score_up >= 45 else "ELIMINATED"
        else:
            su = "WATCH" if score_up >= 45 else "ELIMINATED"
            sd = "WATCH" if score_dn >= 45 else "ELIMINATED"
        rows.append(
            {
                "symbol": sym,
                "base_asset": s.get("base_asset"),
                "underlying_type": s.get("underlying_type"),
                "last_price": p.get("last"),
                "mark_price": p.get("mark"),
                "index_price": p.get("index"),
                "funding_rate": p.get("funding"),
                "liquidity_hard_pass": hard if a else None,
                "liquidity_grade": a.get("liquidity_grade"),
                "aqv_6d_m": a.get("aqv_6d_m"),
                "aqv_12d_m": a.get("aqv_12d_m"),
                "aqv_26d_m": a.get("aqv_26d_m"),
                "liquidity_score_abs": liq,
                "gate1_reason": a.get("reason"),
                "history_days": a.get("history_days"),
                "ret_1h": b.get("ret_1h"),
                "ret_4h": b.get("ret_4h"),
                "ret_24h": b.get("ret_24h"),
                # 1Week / 1Month 涨跌幅：纯展示列，不进 Score / 状态机 / DMR
                **long_ret_row_fields(long_ret.get(sym)),
                "ret_since_anchor": b.get("ret_since_anchor"),
                "rsi_1h": b.get("rsi_1h"),
                "momentum_score_up": m_up,
                "momentum_score_down": m_dn,
                "consistency_up": c_up,
                "consistency_down": c_dn,
                "coingecko_id": c.get("coingecko_id"),
                "circulating_supply": c.get("circulating_supply"),
                "market_cap_coingecko": c.get("market_cap_coingecko"),
                "market_cap_calculated": c.get("market_cap_calculated"),
                "mcap_momentum_score_up": mc_up,
                "mcap_momentum_score_down": mc_dn,
                "mono_up": c.get("mono_up"),
                "mono_down": c.get("mono_down"),
                "path_points": c.get("path_points"),
                "data_quality_score": dq,
                "supply_missing": supply_missing,
                "mapping_status": c.get("mapping_status"),
                "gate3_reason": c.get("reason"),
                "ss_up": ss_up,
                "ss_down": ss_dn,
                "steps_up": d.get("steps_up"),
                "steps_down": d.get("steps_down"),
                "score_up": score_up,
                "score_down": score_dn,
                "state_up": su,
                "state_down": sd,
                "data_mode": "LIVE" if p else "MISSING",
                # 30m/2h/6h 周期流通市值等级：纯展示列，不进 Score / 状态机 / DMR
                **mcap_tf_row_fields(mcap_tf.get(sym)),
            }
        )
    return rows


def not_confirmed_reasons_for(
    *,
    state: str,
    score: float,
    ss: float,
    mom: float,
    cons: float,
    dq: float,
    supply_missing: bool,
    dwell: float,
    streak: int,
    data_mode: str = "LIVE",
    cfg: Optional[Any] = None,
    hard_pass: Optional[bool] = True,
) -> list[str]:
    """Split the old dwell_or_streak bucket into auditable codes."""
    if state == "CONFIRMED":
        return []
    cfg = cfg or default_state_config()
    reasons: list[str] = []
    if hard_pass is not True:
        reasons.append("liquidity")  # §6.3 N1
    if supply_missing:
        reasons.append("supply_missing")
    c_path = path_confirmed(
        score, ss, mom, cons, dq, supply_missing, cfg, data_mode=data_mode
    )
    if c_path is None:
        if dq < cfg.dq_confirm_floor:
            reasons.append("dq")
        if ss < cfg.ss_confirmed:
            reasons.append("staircase")
        if mom < cfg.mom_confirmed:
            reasons.append("momentum")
        if float(cons) < cfg.cons_confirmed:
            reasons.append("consistency")
        if score < cfg.enter_confirmed:
            reasons.append("score")
        if (data_mode or "") == "MISSING":
            reasons.append("data_mode")
    if state == "WATCH":
        if dwell < cfg.min_dwell_watch:
            reasons.append("dwell")
        if streak < cfg.min_streak_qualified:
            reasons.append("streak")
        if path_qualified(score, ss, mom, cons, supply_missing, cfg) is None and "ss" not in reasons:
            if ss < cfg.ss_qualified:
                reasons.append("ss")
            if mom < cfg.mom_qualified:
                reasons.append("momentum")
    elif state == "QUALIFIED":
        if dwell < cfg.min_dwell_qualified:
            reasons.append("dwell")
        if streak < cfg.min_streak_confirmed:
            reasons.append("streak")
    # unique preserve order
    out: list[str] = []
    for x in reasons:
        if x not in out:
            out.append(x)
    return out or (["dwell"] if state in ("WATCH", "QUALIFIED") else [])


def rank_dmr_inbox(
    msgs: list[dict[str, Any]],
    *,
    top_k: int = 16,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Same-scan CONFIRMED → one direction/symbol → Top-K unique coins.

    排序（v2.0.0 流通市值主导，``mcap_ceiling_zone`` 存在时启用）::

        (天花板等级, -Z, -共振K, -RankKey, -Score, -SS, -M, symbol)

    前三键全部是三周期流通市值维度；``Score``/``SS``/``M`` 降为次级裁决，
    ``symbol`` 只作稳定排序兜底（文档A §5.6 + 本轮裁决二·5）。
    主榜 v1.4.0 的消息不带这些字段，自动退回旧三键 ``(-Score,-SS,-M,symbol)``。

    K legal range [12, 20]; default 16.
    """
    k = max(12, min(20, int(top_k)))
    confirmed = [m for m in msgs if m.get("state") == "CONFIRMED"]

    def _key(m: dict[str, Any]) -> tuple:
        legacy = (
            -float(m.get("total_score") or 0),
            -float(m.get("staircase_score") or 0),
            -float(m.get("momentum_score") or 0),
            str(m.get("symbol") or ""),
        )
        # X v1.3.0 shadow 只观察；复盘 DMR 复刻 main v1.4.0 的去重/Top-K。
        # main/y 没有此标记，行为不变；未来 X on 只由 overrides 开启。
        # —— rank216：216 作为**排序权重**而非准入（DMR_RANK_216 标记）——
        #
        # 只取 216 里经实测有预测力的那一部分：**三周期全同向**（AAA / FFF）。
        # staging 2139 节点实测：全同向合计 +161.0%、盈亏比 3.21，
        # 混合组合合计 -477.7%、盈亏比 1.98。而方向天花板整体是反向指标，
        # 因此**不**把 ceiling 放进排序键 —— 那会把反向信号重新引回来。
        #
        # 只加一个二值首键：全同向排前面，其余按旧三键。这样 216 影响的是
        # 「谁排前面」，不再是「谁能进」，池子填充度不受影响。
        if "DMR_RANK_216" in (m.get("reason_codes") or ()):
            combo = str(m.get("mcap_combo_code") or "")
            aligned = 0 if (len(combo) == 3 and combo[0] == combo[1] == combo[2]) else 1
            # 补齐到与另外两个分支相同的 4 元前缀：同一次 sort 里三种形状混用会
            # 按位比较到不同语义的字段上（int 排名位 vs float Z 值）。实际同一板面
            # 模式一致不会混，但形状对齐是零成本的，不留这种隐患。
            return (aligned, 0.0, 0.0, 0.0) + legacy

        ceil = None if m.get("mcap_zone_mode") == "shadow" else m.get("mcap_ceiling_zone")
        if ceil is None:
            # 主榜 / 主导层未启用：(9,9,0,0) 前缀对所有行相同，等价于旧三键排序。
            return (9, 0.0, 0.0, 0.0) + legacy
        from .mcap_effective import ZONE_RANK as _ZR

        z10 = m.get("mcap_z10")
        return (
            _ZR.get(str(ceil), 9),
            -(float(z10) / 10.0 if z10 is not None else -3.0),
            -float(m.get("mcap_resonance_k") or 0),
            -float(m.get("rank_key") or 0),
        ) + legacy

    confirmed.sort(key=_key)
    best: dict[str, dict[str, Any]] = {}
    for m in confirmed:
        sym = m.get("symbol")
        if not sym or sym in best:
            continue
        best[sym] = m
    unique = list(best.values())
    # 去重之后必须用**同一把**尺子再排一次：这一步既决定 inbox 的先后，也决定
    # ``unique[:k]`` 谁进 Top-K。此前这里回落成旧三键 (-Score,-SS,-M,symbol)，
    # 于是「谁能进 DMR」实际由动能/分数裁决，天花板 / Z / K / RankKey 一概不参与
    # —— 那正是 v2.0.0 明令禁止的「动能覆盖流通市值判定」（本轮裁决二·5）。
    unique.sort(key=_key)
    inbox = unique[:k]
    for i, m in enumerate(inbox, 1):
        m["market_rank"] = i
        m["tier_rank"] = i
        m["dmr_selected"] = True
    for m in unique[k:]:
        m["dmr_selected"] = False
        m["dmr_truncated"] = True
    meta = {
        "unique_before_k": len(unique),
        "inbox_k": k,
        "inbox_count": len(inbox),
        "truncated": max(0, len(unique) - k),
        "underfilled": len(unique) < 10,
    }
    return inbox, meta


#: `SS3` 展示带的下沿。它不是状态机门槛（StateConfig 里没有对应字段），
#: 只是「楼梯很强」这一条展示用的 reason_code，故留作模块常量。
SS_STRONG = 70.0


def _mcap_audit_fields(r: dict[str, Any], direction: str) -> dict[str, Any]:
    """流通市值主导层的行级审计字段。层没跑过就返回空 dict（不写任何键）。

    判据是 ``mcap_ceiling_zone_{d}`` 是否存在 —— 它由
    :func:`mcap_effective.apply_effective_zone` 在 ``mode != "off"`` 时写入。
    主榜与关闭态的 Y 都不会有它，于是板面逐字段等同改造前（红线第 14 条）。
    """
    d = direction
    if f"mcap_ceiling_zone_{d}" not in r:
        return {}
    codes = list(r.get(f"effective_reason_codes_{d}") or []) + list(
        r.get(f"dominance_reason_codes_{d}") or []
    )
    return {
        "mcap_combo_code": r.get("mcap_combo_code"),
        "mcap_combo_no": r.get("mcap_combo_no"),
        "mcap_combo_status": r.get(f"mcap_combo_status_{d}"),
        "mcap_z10": r.get(f"mcap_z10_{d}"),
        "mcap_priority": r.get(f"mcap_priority_{d}"),
        "mcap_resonance_k": r.get(f"mcap_resonance_k_{d}"),
        "mcap_ceiling_zone": r.get(f"mcap_ceiling_zone_{d}"),
        "base_state": r.get(f"base_state_{d}"),
        "effective_zone": r.get(f"effective_zone_{d}"),
        "final_zone": r.get(f"final_zone_{d}"),
        "mcap_predicate": r.get(f"mcap_predicate_{d}"),
        "mcap_crosscheck": r.get(f"mcap_crosscheck_{d}"),
        "w_base": r.get(f"w_base_{d}"),
        "w_prio": r.get(f"w_prio_{d}"),
        "w_k": r.get(f"w_k_{d}"),
        "w_combo": r.get(f"w_combo_{d}"),
        "w_final": r.get(f"w_final_{d}"),
        "rank_key": r.get(f"rank_key_{d}"),
        "mcap_reason_codes": codes or None,
    }


def board_from_rows(
    rows: list[dict[str, Any]],
    direction: str,
    *,
    now: Optional[datetime] = None,
    cfg: Optional[Any] = None,
    settings: Optional[Any] = None,
) -> list[dict[str, Any]]:
    """把 rows 摊平成一个方向的候选池。

    ``cfg`` = 本板面变体自己的 :class:`StateConfig`（缺陷 N1，文档B §2.3）。
    不传就退回 v1.4.0 生产默认值 —— 但那样一来，一旦变体 overrides 非空，
    ``not_confirmed_reasons`` 与 ``reason_codes`` 就会用**别的板面**的阈值来解释
    本板面的行，即「解释列说谎」。``build_screener_snapshot`` 负责透传。
    """
    key_state = "state_up" if direction == "up" else "state_down"
    key_score = "score_up" if direction == "up" else "score_down"
    key_mom = "momentum_score_up" if direction == "up" else "momentum_score_down"
    key_cons = "consistency_up" if direction == "up" else "consistency_down"
    key_ss = "ss_up" if direction == "up" else "ss_down"
    key_mc = "mcap_momentum_score_up" if direction == "up" else "mcap_momentum_score_down"
    cand = [
        r
        for r in rows
        if r.get(key_state)
        in (
            "WATCH",
            "QUALIFIED",
            "CONFIRMED",
            "ELIMINATED",
            "DATA_INSUFFICIENT",
            "LOW_CONFIDENCE",
        )
    ]
    # —— 排序（文档A §5.6 / 本轮裁决二·5）——
    #
    # 主导层生效时（行上有 ``mcap_ceiling_zone_{d}``），排序由
    # ``mcap_dominance.rank_tuple`` 接管：前四键全是三周期流通市值维度，
    # Score / SS / M 降为次级裁决，symbol 只做稳定兜底。
    # 主榜 v1.4.0 的行不带这些字段 → 原样使用旧三键，行为逐字节不变。
    # 选币榜X v1.3.0：shadow 四列只观察，排序与复盘沿用 main v1.4.0；
    # 后续只由 X overrides 演进。main=off、Y=on 行为不变；旧直接调用无 settings
    # 时保留字段存在的兼容回退，不能把显式 shadow 当成 on。
    from .board_variants import mcap_zone_mode_of
    mode = mcap_zone_mode_of(settings) if settings is not None else None
    if mode == "on" or (mode is None and any(
        r.get(f"mcap_ceiling_zone_{direction}") is not None for r in cand
    )):
        from .mcap_dominance import rank_tuple as _rank_tuple

        cand.sort(key=lambda r: _rank_tuple(r, direction))
    else:
        cand.sort(
            key=lambda r: (
                -float(r.get(key_score) or 0),
                -float(r.get(key_ss) or 0),
                -float(r.get(key_mom) or 0),
                str(r.get("symbol") or ""),
            )
        )
    cfg = cfg or default_state_config()
    pool = []
    for i, r in enumerate(cand, 1):
        grade = r.get("liquidity_grade") or "UNCLASSIFIED"
        if grade not in ("A", "B", "C", "D", "UNCLASSIFIED"):
            grade = "UNCLASSIFIED"
        state = r.get(key_state) or "WATCH"
        dwell = float(
            r.get("state_up_dwell_min" if direction == "up" else "state_down_dwell_min")
            or 0
        )
        stamp = now or utc_now()
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        # 入选时间必须落在 15 分钟节点上（:00/:15/:30/:45）。dwell 已是
        # 栅格倍数，但 --force / 重启会把 wall clock 的秒带进 now−dwell。
        raw_enter = stamp - timedelta(minutes=dwell)
        enter = datetime.fromtimestamp(
            floor_to_node(raw_enter.timestamp()), tz=timezone.utc
        )
        reasons = []
        if r.get("liquidity_hard_pass"):
            reasons.append("G1_PASS")
            reasons.append(f"G1{grade}" if grade != "UNCLASSIFIED" else "G1U")
        if float(r.get(key_mom) or 0) >= float(cfg.mom_confirmed):
            reasons.append("G2OK")
        if not r.get("supply_missing"):
            reasons.append("G3OK")
        if float(r.get(key_ss) or 0) >= float(cfg.ss_confirmed):
            reasons.append("SS2" if float(r.get(key_ss) or 0) < SS_STRONG else "SS3")
        q_path = r.get("qualified_path_up" if direction == "up" else "qualified_path_down")
        c_path = r.get("confirmed_path_up" if direction == "up" else "confirmed_path_down")
        ready = bool(r.get("ready_confirm_up" if direction == "up" else "ready_confirm_down"))
        streak = int(
            r.get("state_up_streak" if direction == "up" else "state_down_streak") or 0
        )
        ncr = not_confirmed_reasons_for(
            state=state,
            score=float(r.get(key_score) or 0),
            ss=float(r.get(key_ss) or 0),
            mom=float(r.get(key_mom) or 0),
            cons=float(r.get(key_cons) or 0),
            dq=float(r.get("data_quality_score") or 80),
            supply_missing=bool(r.get("supply_missing")),
            dwell=dwell,
            streak=streak,
            data_mode=str(r.get("data_mode") or "LIVE"),
            cfg=cfg,
            hard_pass=(r.get("liquidity_hard_pass") is True),
        )
        if q_path:
            reasons.append(f"PATH_{q_path}_QUAL")
        if state == "CONFIRMED" and (c_path or q_path):
            reasons.append(f"PATH_{c_path or q_path}_CONF")
        if ready:
            reasons.append("READY_CONFIRM")
        pool.append(
            {
                "rank": i,
                "symbol": r["symbol"],
                "underlying_asset": r.get("base_asset")
                or r["symbol"].replace("USDT", ""),
                "canonical_asset_id": (r.get("base_asset") or r["symbol"]).lower(),
                "contract_multiplier": 1,
                "direction": direction,
                "state": state,
                "state_enter_time_utc": enter.isoformat().replace("+00:00", "Z"),
                "state_duration_minutes": dwell,
                "score_up": float(r.get("score_up") or 0),
                "score_down": float(r.get("score_down") or 0),
                "direction_confidence": min(
                    1.0,
                    abs(float(r.get("score_up") or 0) - float(r.get("score_down") or 0))
                    / 20.0,
                ),
                "liquidity_score": float(r.get("liquidity_score_abs") or 0),
                "liquidity_grade": grade,
                # 选币榜「等级」与「1h」之间的三列：30m/2h/6h 周期流通市值等级 A–F。
                # 上涨池与下跌池共用同一套等级定义，故按行原样透传，不按方向改写。
                "mcap_grade_30m": r.get("mcap_grade_30m"),
                "mcap_grade_2h": r.get("mcap_grade_2h"),
                "mcap_grade_6h": r.get("mcap_grade_6h"),
                "mcap_tf": r.get("mcap_tf") or {},
                # —— v2.0.0 流通市值主导层的逐币审计链 ——
                # 三周期组合 → 天花板 → 谓词 → 互印证 → 最终分区 → RankKey，
                # 每一步的命中规则都随行下发，前端与复盘据此解释排位变化。
                #
                # **仅在主导层真正跑过时才写这些键**（`_mcap_audit_fields` 返回空 dict
                # 表示没跑）。无条件写会让 `mode="off"` 的板面凭空多出一批键，
                # 破坏红线第 14 条「off = 字节级退化为现网」，也会让
                # shadow_project_y.py 的 B2/C 取证失去意义。
                **_mcap_audit_fields(r, direction),
                "momentum_score": float(r.get(key_mom) or 50),
                "mcap_momentum_score": float(r.get(key_mc) or 50),
                "staircase_score": float(r.get(key_ss) or 0),
                "consistency_score": float(r.get(key_cons) or 0),
                "rank_velocity_score": 50,
                "risk_score": 100,
                "data_confidence": float(r.get("data_quality_score") or 80),
                "ret_15m": r.get("ret_15m"),
                "ret_1h": float(r.get("ret_1h") or 0),
                "ret_4h": float(r.get("ret_4h") or 0),
                "ret_24h": float(r.get("ret_24h") or 0),
                # 选币榜「24h」与「锚点以来」之间的两列：1Week / 1Month 涨跌幅。
                # 上涨池与下跌池共用同一份涨跌幅，故按行原样透传，不按方向改写；
                # 算不出时保持 None（前端显示 —），绝不折叠成 0。
                "ret_1w": r.get("ret_1w"),
                "ret_1mo": r.get("ret_1mo"),
                "ret_since_anchor": r.get("ret_since_anchor"),
                "aqv_6d_m": r.get("aqv_6d_m") or 0,
                "aqv_12d_m": r.get("aqv_12d_m") or 0,
                "aqv_26d_m": r.get("aqv_26d_m") or 0,
                "circulating_supply": r.get("circulating_supply"),
                "market_cap_coingecko": r.get("market_cap_coingecko"),
                "market_cap_calculated": r.get("market_cap_calculated"),
                "supply_source": "CG" if r.get("circulating_supply") else "NONE",
                "data_mode": r.get("data_mode") or "MISSING",
                "supply_as_of_utc": None,
                "mapping_confidence": 0.0 if r.get("supply_missing") else 1.0,
                "risk_flags": [],
                "reason_codes": reasons,
                "not_confirmed_reasons": ncr,
                "qualified_path": q_path,
                "confirmed_path": c_path if state == "CONFIRMED" else None,
                "ready_confirm": ready,
                "ref_price": r.get("last_price") or r.get("mark_price"),
                "last_price": r.get("last_price"),
                "state_enter_price": (
                    r.get(
                        "state_up_enter_price"
                        if direction == "up"
                        else "state_down_enter_price"
                    )
                    if state in ("WATCH", "QUALIFIED", "CONFIRMED")
                    else None
                ),
                "coingecko_coin_id": r.get("coingecko_id"),
            }
        )
        # 216 天花板合取字段：只在源行已经打过标时写出，主板 / ENABLE=0 一个键都不加。
        if "mcap_combo" in r or "product_zone_up" in r or "product_zone_down" in r:
            pool[-1]["mcap_combo"] = r.get("mcap_combo")
            pool[-1]["mcap_z10"] = r.get("mcap_z10")
            pool[-1]["mcap_k"] = r.get("mcap_k")
            pool[-1]["mcap_ceiling"] = r.get(f"mcap_ceiling_{direction}")
            pool[-1]["product_zone"] = r.get(f"product_zone_{direction}")
            pool[-1]["combo_reason"] = r.get(f"combo_reason_{direction}")
        # —— 文档A §4 唯一裁决版天花板层（mcap_zone）的行级字段 ——
        #
        # 只在 mcap_zone_mode ∈ {shadow, on} 时由 mcap_zone.apply_mcap_ceiling 打标；
        # off 时源行上一个键都没有，这里也就一个键都不写 —— 快照与现网逐字节相同
        # （红线第 14 条 / 测试 T11）。前端只渲染这些字段，绝不重算（红线第 19 条）。
        if f"zone_ceiling_{direction}" in r:
            pool[-1]["combo_code"] = r.get("combo_code")
            pool[-1]["z_score"] = r.get(f"z_score_{direction}")
            pool[-1]["combo_zone"] = r.get(f"combo_zone_{direction}")
            pool[-1]["zone_ceiling"] = r.get(f"zone_ceiling_{direction}")
            pool[-1]["product_zone"] = r.get(f"product_zone_y_{direction}")
            for code in r.get(f"mcap_zone_codes_{direction}") or []:
                if code not in reasons:
                    reasons.append(code)
        # —— ChatGpt_SOL5.6 文档A §13.2 的每行血缘字段 ——
        #
        # 同样只在源行已被 mcap_effective.apply_effective_zone 打过标时才写；
        # mode="off" 时源行一个键都没有，快照与冻结基线逐字段一致。
        if f"effective_zone_{direction}" in r:
            pool[-1]["base_state"] = r.get(f"base_state_{direction}") or state
            pool[-1]["mcap_combo_no"] = r.get(f"mcap_combo_no_{direction}")
            pool[-1]["mcap_combo_status"] = r.get(f"mcap_combo_status_{direction}")
            pool[-1]["mcap_z10"] = r.get(f"mcap_z10_{direction}")
            pool[-1]["mcap_priority"] = r.get(f"mcap_priority_{direction}")
            pool[-1]["mcap_resonance_k"] = r.get(f"mcap_resonance_k_{direction}")
            pool[-1]["mcap_ceiling_zone"] = r.get(f"mcap_ceiling_zone_{direction}")
            pool[-1]["effective_zone"] = r.get(f"effective_zone_{direction}")
            pool[-1]["effective_reason_codes"] = list(
                r.get(f"effective_reason_codes_{direction}") or []
            )
    # X shadow 的假设有效区不能被复盘 in_zone() 当实际区：保留四列观察值，
    # effective_zone 只报真实 state，v1.3.0 复盘与 main v1.4.0 零漂移。
    # main/off、Y/on 不进分支；未来 X 仍只经 overrides 演进。
    if mode == "shadow":
        for item in pool:
            if "effective_zone" in item:
                item["effective_zone"] = item["state"]
            # —— final_zone 也必须归一，不能只归一 effective_zone ——
            #
            # review_replay 判 in_zone 时**先读 final_zone**（scan_id >= 20260905-000
            # 起），只有它为空才回退 effective_zone。所以只归一后者的话，一旦有人
            # 在 shadow 板面上跑了主导层（离线回放器就这么干过），X 的复盘四区
            # 会被天花板压低后的分区静默改写，而这里的归一化完全兜不住。
            #
            # 今天 X 之所以安全，只是因为 dom_cfg 恰好为 None ⇒ 没人写 final_zone
            # （实测生产快照两池 1052 行 final_zone 全为 None）。把它变成结构性
            # 保证，而不是「恰好没被调用」的隐性不变量 —— 这正是「216 只约束 DMR、
            # 绝不改变四区归属」这条承诺的兜底。
            #
            # main 走 off、Y 走 on，都不进本分支，因此零回归。
            if item.get("final_zone") is not None:
                item["final_zone"] = None
    return pool


def build_dmr_messages(
    rows: list[dict[str, Any]],
    *,
    settings: SelectionSettings,
    anchor: datetime,
    scan_id: str,
    seq: int,
    now: datetime,
    cfg: Optional[Any] = None,
) -> list[dict[str, Any]]:
    """CONFIRMED -> DMR-zone messages.

    ``cfg`` lets a board variant apply its own DMR-zone thresholds
    (「选币榜Y」/ param-v2.0.0). ``None`` = production v1.4.0 defaults, i.e. the
    original behaviour for 「选币榜」.
    """
    msgs = []
    cfg = cfg or default_state_config()
    dmr_mode = getattr(settings, 'dmr_selection_mode', 'strict-v1.4')
    # 合法值表只有 board_variants.DMR_SELECTION_MODES 一处事实源。
    # 原先这里是一份硬编码副本，新增模式时忘了同步 —— 于是配置期校验放行、
    # 这里却抛 unknown，两处对同一个值给出相反判断。不留第二份。
    from .board_variants import DMR_SELECTION_MODES as _MODES
    if dmr_mode not in _MODES:
        raise ValueError('unknown dmr_selection_mode')
    basic216 = dmr_mode == 'confirmed-basic-216-v1.3'
    mom_floor = float(getattr(settings, 'dmr_momentum_floor', 0.0) or 0.0)
    # rank216：216 退出准入、改做排序权重（依据见 board_variants.DMR_SELECTION_MODES 注释）。
    rank216 = dmr_mode == 'confirmed-basic-rank216-v1.3'
    mapping = None
    if basic216 or rank216:
        if (settings.parameter_version != 'param-v1.3.0-screener-x'
                or settings.mcap_zone_mode != 'shadow'
                or cfg.selection_semantics != 'dual-path-v1.3'):
            raise ValueError('X v1.3 DMR requires X identity, dual-path and shadow')
        # —— 身份不得说谎：ceiling 门槛必须真的是文档A §10.2 的 'DMR' ——
        #
        # 下面的过滤是常量比较 `ceiling != 'DMR'`，而 mcap_dmr_ceiling_min 会经
        # rule_manifest._mcap_dominance_config 进入 config_hash。若两者脱钩，把该值
        # 改成 CONFIRMED 会让 config_hash 变号、触发身份漂移阻断，而 DMR 成员一个
        # 都不变 —— 身份在描述一件代码没做的事。此处 fail-closed，而不是让它读该值：
        # D3 明确禁止把 Y 放宽到 CONFIRMED 的 ceiling 门槛照搬到 X。
        ceiling_min = str(getattr(settings, 'mcap_dmr_ceiling_min', 'DMR') or 'DMR')
        if ceiling_min != 'DMR':
            raise ValueError(
                f'X v1.3 DMR 只支持 mcap_dmr_ceiling_min=DMR，实际 {ceiling_min!r}；'
                '放宽 ceiling 门槛需另行裁决并改本分支的谓词')
        # —— 天花板层必须**真的跑过**，不能只看 settings 声明 ——
        #
        # board_projection 对非受约束板面（X 就是）遇到任何异常都只 log.warning 并把
        # **局部变量** zone_mode 降级为 off，settings.mcap_zone_mode 仍是 'shadow'。
        # 只信声明的话，天花板层挂掉后 mcap_combo_code 会整片缺失，下面的
        # `not combo → continue` 会把 X 的 DMR 静默清空，而快照照常发布、账本照常
        # 写入、没有任何告警 —— 复盘会把「层挂了」记成「策略没选中」。
        # 因此改用运行时事实：apply_effective_zone 在 shadow 下会给每一行写
        # mcap_ceiling_zone_{up,down}（mcap_effective.py:293），缺了就说明没跑成。
        missing_layer = [
            r.get('symbol') for r in rows
            if 'mcap_ceiling_zone_up' not in r or 'mcap_ceiling_zone_down' not in r
        ]
        if missing_layer:
            raise ValueError(
                f'216 天花板层未生效（{len(missing_layer)} 行缺 mcap_ceiling_zone_*，'
                f'首个 {missing_layer[0]!r}）；拒绝以半张表裁决 X 的 DMR')
        from .mcap_mapping import load
        mapping = load(strict=True)
    exp = (now + timedelta(minutes=15)).replace(microsecond=0)
    for r in rows:
        for direction, state_key, score_key, ss_key, mom_key, cons_key, mc_key in (
            (
                "LONG",
                "state_up",
                "score_up",
                "ss_up",
                "momentum_score_up",
                "consistency_up",
                "mcap_momentum_score_up",
            ),
            (
                "SHORT",
                "state_down",
                "score_down",
                "ss_down",
                "momentum_score_down",
                "consistency_down",
                "mcap_momentum_score_down",
            ),
        ):
            if r.get(state_key) != "CONFIRMED":
                continue
            # 216 天花板：天花板≠DMR 或产品分区差于确定 → 不得进 Y inbox。
            dmr_dir = "up" if direction == "LONG" else "down"
            if r.get(f"combo_block_dmr_{dmr_dir}"):
                continue
            if r.get("supply_missing"):
                continue
            if r.get("liquidity_hard_pass") is not True:  # N1 hard requirement
                continue
            sym = r["symbol"]
            raw = f"{anchor.strftime('%Y-%m-%d')}|{scan_id}|{sym}|{direction}"
            mid = hashlib.sha1(raw.encode()).hexdigest()
            if r.get("data_mode") == "MISSING":
                continue
            if float(r.get("data_quality_score") or 0) < 60:
                continue
            combo = None
            if basic216 or rank216:
                combo = mapping.by_combo.get(r.get('mcap_combo_code'))
            if basic216:
                # 准入式：方向 ceiling 必须是 DMR（每侧仅 12/216 组合）
                if not combo or combo['long_ceiling' if direction == 'LONG' else 'short_ceiling'] != 'DMR':
                    continue
            # 动能下限：仅对两种 X v1.3 模式生效，0 时完全不参与（历史口径）。
            if (basic216 or rank216) and mom_floor > 0:
                if float(r.get(mom_key) or 0) < mom_floor:
                    continue
            if rank216:
                # —— 排序式：216 不再挡人，但缺映射仍然 fail-closed ——
                #
                # 为什么不挡：staging 全量重建实测天花板做准入是**反向指标**
                # （确认区 ceil=DMR 合计 -15.1%，ceil=WATCH 合计 +188.8%）。
                # 为什么仍要求映射存在：排序键要用它，缺了就无法定序，
                # 而「静默按缺省值排」正是最容易掩盖降级的做法。
                if not combo:
                    continue
            # 两种 X v1.3 模式都**不**走 v1.4 的强子集（70/55/65/.67/70/LIVE）——
            # 历史 v1.3 的 DMR 基线是「确认 + 基础消息条件」，没有这套阈值。
            if not (basic216 or rank216) and not dmr_zone_ok(
                float(r.get(score_key) or 0),
                float(r.get(ss_key) or 0),
                float(r.get(mom_key) or 0),
                float(r.get(cons_key) or 0),
                float(r.get("data_quality_score") or 0),
                bool(r.get("supply_missing")),
                str(r.get("data_mode") or "LIVE"),
                cfg,
            ):
                continue
            path_key = "confirmed_path_up" if direction == "LONG" else "confirmed_path_down"
            confirm_path = r.get(path_key)
            codes = ["G1_PASS", "G2OK", "G3OK", "SM_CONFIRMED",
                     "DMR_BASIC_216" if basic216 else
                     ("DMR_RANK_216" if rank216 else "DMR_STRICT")]
            if confirm_path:
                codes.append(f"PATH_{confirm_path}")
            msgs.append(
                {
                    "message_id": mid,
                    "system_version": settings.system_version,
                    "anchor_date": anchor.strftime("%Y-%m-%d"),
                    "scan_id": scan_id,
                    "scan_sequence": seq,
                    "scan_timestamp_utc": now.replace(microsecond=0)
                    .isoformat()
                    .replace("+00:00", "Z"),
                    "generated_at_utc": utc_now()
                    .replace(microsecond=0)
                    .isoformat()
                    .replace("+00:00", "Z"),
                    "expires_at_utc": exp.isoformat().replace("+00:00", "Z"),
                    "symbol": sym,
                    "underlying_asset": r.get("base_asset") or sym.replace("USDT", ""),
                    "canonical_asset_id": (r.get("base_asset") or sym).lower(),
                    "contract_multiplier": 1,
                    "direction": direction,
                    "state": "CONFIRMED",
                    "state_duration_minutes": float(
                        r.get(
                            "state_up_dwell_min"
                            if direction == "LONG"
                            else "state_down_dwell_min"
                        )
                        or 0
                    ),
                    "direction_confidence": min(
                        1.0,
                        abs(float(r.get("score_up") or 0) - float(r.get("score_down") or 0))
                        / 20.0,
                    ),
                    "total_score": float(r.get(score_key) or 0),
                    "liquidity_score": float(r.get("liquidity_score_abs") or 0),
                    "momentum_score": float(r.get(mom_key) or 0),
                    "market_cap_momentum_score": float(r.get(mc_key) or 0),
                    "staircase_score": float(r.get(ss_key) or 0),
                    "trend_consistency_score": float(r.get(cons_key) or 0) * 100.0,
                    "rank_velocity_score": 50,
                    "risk_score": 100,
                    "data_confidence": float(r.get("data_quality_score") or 80),
                    # —— v2.0.0 流通市值主导层：排序第一裁决键随消息下发 ——
                    # 缺席（主榜 v1.4.0）时全为 None，rank_dmr_inbox 自动退回旧三键。
                    "mcap_combo_code": r.get("mcap_combo_code"),
                    "mcap_combo_status": r.get(f"mcap_combo_status_{dmr_dir}"),
                    "mcap_z10": r.get(f"mcap_z10_{dmr_dir}"),
                    "mcap_priority": r.get(f"mcap_priority_{dmr_dir}"),
                    "mcap_resonance_k": r.get(f"mcap_resonance_k_{dmr_dir}"),
                    "mcap_ceiling_zone": r.get(f"mcap_ceiling_zone_{dmr_dir}"),
                    # X v1.3.0 只在 shadow 标观察态，阻止 DMR 排序误用天花板；
                    # 复盘强子集复刻 main v1.4.0，未来 on 仍由 X overrides 控制。
                    **({"mcap_zone_mode": "shadow"} if settings.mcap_zone_mode == "shadow" else {}),
                    "effective_zone": r.get(f"effective_zone_{dmr_dir}"),
                    "final_zone": r.get(f"final_zone_{dmr_dir}"),
                    "mcap_predicate": r.get(f"mcap_predicate_{dmr_dir}"),
                    "mcap_crosscheck": r.get(f"mcap_crosscheck_{dmr_dir}"),
                    "w_base": r.get(f"w_base_{dmr_dir}"),
                    "w_prio": r.get(f"w_prio_{dmr_dir}"),
                    "w_k": r.get(f"w_k_{dmr_dir}"),
                    "w_combo": r.get(f"w_combo_{dmr_dir}"),
                    "w_final": r.get(f"w_final_{dmr_dir}"),
                    "rank_key": r.get(f"rank_key_{dmr_dir}"),
                    "market_rank": 0,
                    "tier_rank": 0,
                    "market_cap_tier": "T3",
                    "coingecko_coin_id": r.get("coingecko_id"),
                    "circulating_supply": r.get("circulating_supply"),
                    "market_cap_calculated": r.get("market_cap_calculated"),
                    "market_cap_coingecko": r.get("market_cap_coingecko"),
                    "risk_flags": [],
                    "data_mode": r.get("data_mode") or "LIVE",
                    "reason_codes": codes,
                    "indicator_version": "g1g2g3g4-sm-dual-path",
                    "parameter_version": settings.parameter_version,
                    "mapping_version": "cg-map",
                    "data_version": f"data-{scan_id}",
                    "attribution": "Powered by CoinGecko",
                    "confirm_path": confirm_path,
                }
            )
    msgs.sort(
        key=lambda m: (
            -float(m.get("total_score") or 0),
            -float(m.get("staircase_score") or 0),
            -float(m.get("momentum_score") or 0),
            str(m.get("symbol") or ""),
        )
    )
    for i, m in enumerate(msgs, 1):
        m["market_rank"] = i
        m["tier_rank"] = i
    return msgs


def decorate_board(
    board: dict[str, Any],
    *,
    rows: list[dict[str, Any]],
    dmr_msgs: list[dict[str, Any]],
    dmr_rank: dict[str, Any],
    daily_unique: Optional[dict[str, Any]],
    settings: SelectionSettings,
    cfg: Optional[Any] = None,
    sm: Optional[Any] = None,
    now_ts: Optional[float] = None,
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """Stamp DMR membership + occupancy/hierarchy/control/alerts onto a board.

    Extracted verbatim from ``run_scan_cycle`` so every board variant
    (「选币榜」param-v1.4.0 and 「选币榜Y」param-v2.0.0) decorates its snapshot
    through **one** implementation. A second hand-rolled copy would be exactly
    how the two boards silently drift apart while still claiming to be a clone.

    ``cfg`` only reaches ``scan_control_flags``; ``None`` keeps the production
    v1.4.0 defaults, i.e. behaviour identical to the pre-extraction code.

    Returns ``(occupancy, control, alerts)`` — the three values the caller needs
    for ``latest.internal.json`` and the loop status line.
    """
    dmr_keys = {
        (m["symbol"], "up" if m.get("direction") == "LONG" else "down")
        for m in dmr_msgs
    }
    # The DMR/精选 zone shown in the UI is the exact executable inbox subset,
    # never a second independently-computed list.
    for pool in (board.get("long_pool") or [], board.get("short_pool") or []):
        for row in pool:
            row["dmr_selected"] = (row.get("symbol"), row.get("direction")) in dmr_keys
    # —— 展示区三列（入选时间 / 停留时间 / 停留价格）——
    #
    # 必须排在 dmr_selected 之后：DMR 是展示区之一，它的每一次进出都要重新打戳。
    # 在此之前这三列跟随 base_state（层 A），而板面显示的是 final_zone（层 C）与
    # DMR 旗标 —— 用户看着一个币离开确认区又回来，时间却从未重置、停留一路累加。
    if sm is not None:
        from .state_machine import stamp_display_zone

        stamp_display_zone(
            sm,
            (board.get("long_pool") or [], board.get("short_pool") or []),
            now_ts=now_ts,
        )
        sm.save()
    occ = occupancy_from_rows(rows)
    ctrl = scan_control_flags(rows, cfg=cfg, baseline=settings.baseline_universe)
    board.setdefault("meta", {})
    board["meta"]["occupancy"] = occ
    board["meta"]["hierarchy"] = {
        "dmr_unique": len(dmr_msgs),
        "confirmed_unique": int(occ.get("confirmed_unique") or 0),
        "qualified_including_confirmed_unique": int(occ.get("qualified_unique") or 0)
        + int(occ.get("confirmed_unique") or 0),
        "invariant": "DMR subset CONFIRMED subset QUALIFIED",
    }
    board["meta"]["control"] = ctrl
    board["meta"]["dmr"] = dmr_rank
    board["meta"]["daily_unique"] = daily_unique
    # §10.2 dynamic control. Alerts never relax N1-N4 / F2 / the $3M floor -
    # they only annotate why 占用 is off-target this node.
    alerts: list[str] = []
    occ_lo = int(getattr(cfg, "occupancy_lo", 10) if cfg is not None else 10)
    occ_hi = int(getattr(cfg, "occupancy_hi", 40) if cfg is not None else 40)
    confirmed_n = int(occ.get("confirmed_unique") or 0)
    if ctrl.get("low_breadth"):
        alerts.append("LOW_BREADTH")
    elif confirmed_n < occ_lo:
        alerts.append("CONFIRM_UNDERFILLED")
    if confirmed_n > occ_hi:
        alerts.append("CONFIRM_OVERFLOW")
    if ctrl.get("data_stress"):
        alerts.append("DATA_STRESS")
    if ctrl.get("universe_stress"):
        alerts.append("UNIVERSE_STRESS")
    board["meta"]["alerts"] = alerts
    if alerts:
        board["meta"]["alert"] = alerts[0]
        dmr_rank["alert"] = alerts[0]
    dmr_rank["alerts"] = alerts
    return occ, ctrl, alerts


def build_screener_snapshot(
    settings: SelectionSettings,
    *,
    anchor: datetime,
    scan_id: str,
    seq: int,
    now: datetime,
    rows: list[dict[str, Any]],
    universe_count: int,
    uni_ver: str,
    stats: dict[str, Any],
    transitions: list[dict[str, Any]],
    daily_unique: Optional[dict[str, Any]] = None,
    generated_at: Optional[datetime] = None,
    cfg: Optional[Any] = None,
    param_hash: Optional[str] = None,
    mcap_zone: Optional[dict[str, Any]] = None,
    mcap_effective: Optional[dict[str, Any]] = None,
    rule_identity: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """一块板面快照。

    ``cfg``（缺陷 N1 修复，文档B 阶段 0.1）：本板面变体自己的 StateConfig。透传给
    ``board_from_rows``（行级解释列）与 ``scan_control_flags``（控制面），这样
    「为什么没进确认」用的就是**本板面**的阈值，而不是 v1.4.0 的。

    ``param_hash``（文档B §3.1）：参数指纹，写进 ``meta.param_hash``。它是
    《选币榜Y》与《复盘选币》同源同参的机器保证 —— 回放时比对不一致即拒绝出数。

    ``mcap_zone``（文档B §3.1 表）：216 天花板层的声明块，写进 ``meta.mcap_zone``。

    ``mcap_effective``（ChatGpt_SOL5.6 文档B §8.1）：SOL5.6 口径有效区层的声明块 +
    统计，写进 ``meta.mcap_effective``。

    ``rule_identity``（ChatGpt_SOL5.6 文档A §13.1 / 文档B §3.2）：不可变规则身份
    （rule_revision / config_hash / asset_mapping_version / mcap_mapping_version /
    mapping_hash / code_commit / data_contract_version / universe_policy_version /
    effective_from_*），写进 ``meta.rule_identity``。一致性等式的左半边。
    """
    # X v1.3.0 透传模式以区分观察与裁决；复盘和 main 克隆同源，演进只改 overrides。
    long_pool = board_from_rows(rows, "up", now=now, cfg=cfg, settings=settings)
    short_pool = board_from_rows(rows, "down", now=now, cfg=cfg, settings=settings)
    counts = {
        "WATCH": 0,
        "QUALIFIED": 0,
        "CONFIRMED": 0,
        "ELIMINATED": 0,
        "DATA_INSUFFICIENT": 0,
        "LOW_CONFIDENCE": 0,
        "NONE": 0,
    }
    for r in rows:
        for st in (r.get("state_up"), r.get("state_down")):
            if st in counts:
                counts[st] += 1
            elif st:
                counts["NONE"] += 1
    expires = (now + timedelta(minutes=15)).replace(microsecond=0)
    tr = [
        {
            "id": f"t{i}",
            "at_utc": now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),  # node
            "symbol": t["symbol"],
            # Real predecessor, not a placeholder: 禁止跳级 is only auditable if the
            # board says where the row actually came from.
            "from_state": t.get("from_state") or "NONE",
            "to_state": t["to_state"],
            "direction": t["direction"],
            "reason_codes": [t.get("reason") or "SM"],
        }
        for i, t in enumerate(transitions[:50])
    ]
    return {
        "meta": {
            "system_version": settings.system_version,
            "anchor_date": anchor.strftime("%Y-%m-%d"),
            "scan_id": scan_id,
            "scan_sequence": seq,
            "scan_timestamp_utc": now.replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z"),
            "effective_universe": universe_count,
            "baseline_universe": settings.baseline_universe,
            "regime": 0.5,  # hardcoded; not a live thermometer — do not enable v1.2 §33.3 caps
            "regime_label": "g1_g2g3g4_dual_path_sticky",
            "data_mode": "LIVE",
            "state_counts": counts,
            "coingecko_credits": {
                "used_today": stats.get("gate3", {}).get("credits_est", 0),
                "month_est": 0,
                "month_cap": 10000,
                "utilization": 0,
                "alert_level": "ok",
            },
            "parameter_version": settings.parameter_version,
            "mapping_version": "cg-map",
            "data_version": f"data-{scan_id}",
            "indicator_version": "g1g2g3g4-sm-dual-path",
            "universe_version": uni_ver,
            "occupancy": occupancy_from_rows(rows),
            "daily_unique": daily_unique or {},
            "control": scan_control_flags(
                rows, cfg=cfg, baseline=settings.baseline_universe
            ),
            # 参数指纹 / 天花板层声明：缺省不写键，旧消费方与旧快照都不受影响。
            **({"param_hash": param_hash} if param_hash else {}),
            **({"mcap_zone": mcap_zone} if mcap_zone else {}),
            **({"mcap_effective": mcap_effective} if mcap_effective else {}),
            **({"rule_identity": rule_identity} if rule_identity else {}),
            **{k: v for k, v in stats.items()},
        },
        "long_pool": long_pool,
        "short_pool": short_pool,
        "transitions": tr,
        # Wall clock on purpose: the node time is already in meta.scan_timestamp_utc.
        "generated_at_utc": (generated_at or utc_now())
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "expires_at_utc": expires.isoformat().replace("+00:00", "Z"),
        "attribution": "Powered by CoinGecko",
    }


def run_scan_cycle(
    settings: Optional[SelectionSettings] = None,
    *,
    now: Optional[datetime] = None,
    force: bool = False,
    allow_rerun: bool = bool(os.environ.get("COIN_SELECTION_ALLOW_RERUN")),
    skip_gate1: bool = False,
    skip_gate2: bool = False,
    skip_gate3: bool = False,
    skip_gate4: bool = False,
    skip_mcap_tf: bool = False,
    skip_long_ret: bool = False,
    skip_sm: bool = False,
) -> dict[str, Any]:
    settings = settings or SelectionSettings()
    data_dir = Path(settings.data_dir or ".")
    data_dir.mkdir(parents=True, exist_ok=True)
    locks = FileScanLock(data_dir / "locks")
    snaps = data_dir / "snapshots"
    snaps.mkdir(parents=True, exist_ok=True)

    wall = now or utc_now()
    anchor = resolve_anchor_00utc(wall)
    seq = scan_sequence(wall, anchor)
    scan_id = make_scan_id(anchor, seq)
    # Every logical timestamp (snapshot, dwell, expiry, DMR) uses the node, not
    # the wall clock. `generated_at_utc` keeps the wall clock so "when was this
    # actually produced" stays honest and auditable.
    now = node_time(anchor, seq)

    if not force and not locks.acquire(scan_id):
        log.info("scan %s locked", scan_id)
        return {"scan_id": scan_id, "status": "locked", "latest": str(data_dir / "latest.json")}

    # 已完成的节点绝不重跑 —— 这一条 ``--force`` **也不能**绕过。
    if locks.is_complete(scan_id) and not allow_rerun:
        log.warning(
            "scan %s already completed; refusing to re-run it "
            "(re-running would double-count the state machine streak). "
            "Set COIN_SELECTION_ALLOW_RERUN=1 to override.",
            scan_id,
        )
        return {
            "scan_id": scan_id,
            "status": "already_complete",
            "latest": str(data_dir / "latest.json"),
        }

    symbols, uni_ver, uni_src = load_universe(settings)
    prices = load_prices(settings)
    sym_names = [s["symbol"] for s in symbols]

    # --- Gate1 ---
    g1_map: dict[str, Any] = {}
    g1_stats = {
        "enabled": not skip_gate1,
        "hard_floor_usd": settings.hard_floor_usd,
        "evaluated": 0,
        "passed": 0,
        "failed": 0,
        "insufficient": 0,
        "errors": 0,
    }
    if not skip_gate1 and sym_names:
        results = run_gate1(
            sym_names,
            fapi_rest=settings.fapi_rest,
            cache_dir=data_dir / "turnover_cache",
            sleep_sec=settings.gate1_sleep_sec,
            max_symbols=settings.gate1_max_symbols,
            hard_floor=settings.hard_floor_usd,
            workers=settings.gate1_workers,
            progress_path=data_dir / "gate1_progress.json",
        )
        for sym, res in results.items():
            g1_map[sym] = g1_dict(res)
            g1_stats["evaluated"] += 1
            if res.liquidity_hard_pass:
                g1_stats["passed"] += 1
            elif str(res.reason).startswith("fetch_error"):
                g1_stats["errors"] += 1
                g1_stats["failed"] += 1
            elif res.liquidity_grade == "INSUFFICIENT" or res.history_days < 26:
                g1_stats["insufficient"] += 1
                g1_stats["failed"] += 1
            else:
                g1_stats["failed"] += 1
        log.info("gate1 %s", g1_stats)

    passed = [s for s, g in g1_map.items() if g.get("liquidity_hard_pass")]

    # --- Gate2 ---
    g2_map: dict[str, Any] = {}
    g2_stats = {"enabled": settings.enable_gate2 and not skip_gate2, "evaluated": 0, "errors": 0}
    if settings.enable_gate2 and not skip_gate2 and passed:
        for sym, res in run_gate2(
            passed,
            fapi_rest=settings.fapi_rest,
            cache_dir=data_dir / "kline1h_cache",
            workers=settings.gate2_workers,
            sleep_sec=0.03,
            # 文档 A §7.2【上线阻断】：只读 close_time <= decision_time 的已收盘 K 线。
            # decision_time 就是本节点的逻辑时间 now = node_time(anchor, seq)。
            decision_time=now,
        ).items():
            g2_map[sym] = g2_dict(res)
            g2_stats["evaluated"] += 1
            if str(res.reason).startswith("fetch_error"):
                g2_stats["errors"] += 1
        hours_from_anchor = max(1, int((now - anchor).total_seconds() // 3600))
        kcache = KlineCache(data_dir / "kline1h_cache")
        for sym, rec in g2_map.items():
            closes = kcache.get(sym, ignore_ttl=True)
            if closes and len(closes) > hours_from_anchor:
                rec["ret_since_anchor"] = roc(closes, hours_from_anchor)
        log.info("gate2 %s", g2_stats)

    # --- Gate3 ---
    g3_map: dict[str, Any] = {}
    g3_stats = {
        "enabled": settings.enable_gate3 and not skip_gate3,
        "evaluated": 0,
        "supply_ok": 0,
        "supply_missing": 0,
        "credits_est": 0,
    }
    if settings.enable_gate3 and not skip_gate3 and passed:
        ret24 = {s: float((g2_map.get(s) or {}).get("ret_24h") or 0) for s in passed}
        for sym, res in run_gate3(
            passed,
            prices=prices,
            ret_24h=ret24,
            data_dir=data_dir / "coingecko",
            hermes_root=settings.hermes_root,
            anchor_date=anchor.strftime("%Y-%m-%d"),
            scan_id=scan_id,
            force_mapping=bool(os.environ.get("FORCE_CG_MAPPING")),
        ).items():
            g3_map[sym] = g3_dict(res)
            g3_stats["evaluated"] += 1
            if res.supply_missing:
                g3_stats["supply_missing"] += 1
            else:
                g3_stats["supply_ok"] += 1
        g3_stats["credits_est"] = 1 + max(1, (g3_stats["evaluated"] + 199) // 200)
        g3_stats["coingecko_key"] = has_coingecko_key()
        g3_stats["coingecko_host"] = "pro" if use_pro_host() else "demo"
        g3_stats["path_points_median"] = None
        pts = [int((g3_map.get(s) or {}).get("path_points") or 0) for s in passed]
        if pts:
            pts_sorted = sorted(pts)
            g3_stats["path_points_median"] = pts_sorted[len(pts_sorted) // 2]
            g3_stats["path_points_max"] = pts_sorted[-1]
        log.info("gate3 %s", g3_stats)

    # --- Gate4 ---
    g4_map: dict[str, Any] = {}
    g4_stats = {"enabled": settings.enable_gate4 and not skip_gate4, "evaluated": 0}
    if settings.enable_gate4 and not skip_gate4 and passed:
        for sym, res in run_gate4(
            passed, kline_cache_dir=data_dir / "kline1h_cache"
        ).items():
            g4_map[sym] = g4_dict(res)
            g4_stats["evaluated"] += 1
        log.info("gate4 %s", g4_stats)

    # --- 30m / 2h / 6h 周期流通市值等级（选币榜新增三列） ---
    # 全宇宙计算：DMR / 确认 / 符合 / 观察 / 淘汰 / 数据不足 / 低置信度 七个分区共用
    # 同一张状态栏表格，任何一个分区缺列都算回归，所以这里不按 G1 是否通过裁剪。
    mcap_tf_map: dict[str, Any] = {}
    mcap_tf_stats: dict[str, Any] = {
        "enabled": settings.enable_mcap_tf and not skip_mcap_tf,
        "symbols": 0,
        "timeframes": list(MCAP_TIMEFRAMES),
    }
    if settings.enable_mcap_tf and not skip_mcap_tf and sym_names:
        mcap_syms = sym_names[: settings.mcap_tf_max_symbols or len(sym_names)]
        supplies = {
            s: (g3_map.get(s) or {}).get("circulating_supply") for s in mcap_syms
        }
        multipliers = {
            s["symbol"]: parse_multiplier(s.get("base_asset") or s["symbol"])
            for s in symbols
        }
        try:
            mcap_tf_map, mcap_tf_stats = run_mcap_timeframes(
                mcap_syms,
                supplies=supplies,
                multipliers=multipliers,
                fapi_rest=settings.fapi_rest,
                cache_dir=data_dir / "kline_tf_cache",
                workers=settings.mcap_tf_workers,
                # 与 Gate2 共用同一个已收盘过滤器，杜绝两条路径读到不同时点的序列。
                decision_time=now,
            )
        except Exception:
            # 展示列绝不允许拖垮选币主链路
            log.exception("mcap_tf failed — board keeps the three columns empty")
            mcap_tf_stats = {
                "enabled": True,
                "symbols": len(mcap_syms),
                "timeframes": list(MCAP_TIMEFRAMES),
                "errors": len(mcap_syms),
                "reason": "exception",
            }

    # --- 1Week / 1Month 涨跌幅（选币榜新增两列） ---
    # 与三列流通市值等级同理，按全宇宙计算：七个分区共用同一张表格，
    # 缺列即回归，所以这里同样不按 G1 是否通过裁剪。
    long_ret_map: dict[str, Any] = {}
    long_ret_stats: dict[str, Any] = {
        "enabled": settings.enable_long_ret and not skip_long_ret,
        "symbols": 0,
        "periods": list(LONG_RET_PERIODS),
    }
    if settings.enable_long_ret and not skip_long_ret and sym_names:
        lr_syms = sym_names[: settings.long_ret_max_symbols or len(sym_names)]
        try:
            long_ret_map, long_ret_stats = run_long_returns(
                lr_syms,
                fapi_rest=settings.fapi_rest,
                cache_dir=data_dir / "kline1d_cache",
                workers=settings.long_ret_workers,
            )
        except Exception:
            # 展示列绝不允许拖垮选币主链路
            log.exception("long_ret failed — board keeps the two columns empty")
            long_ret_stats = {
                "enabled": True,
                "symbols": len(lr_syms),
                "periods": list(LONG_RET_PERIODS),
                "errors": len(lr_syms),
                "reason": "exception",
            }

    rows = build_rows(
        symbols,
        prices,
        g1_map,
        g2_map,
        g3_map,
        g4_map,
        settings,
        mcap_tf=mcap_tf_map,
        long_ret=long_ret_map,
    )

    transitions: list[dict[str, Any]] = []
    # 展示区打戳需要它；状态机关闭时保持 None，decorate_board 会整段跳过。
    sm = None
    if settings.enable_state_machine and not skip_sm:
        sm = StateMachineStore(data_dir / "state_machine.json")
        n_q = quantize_enter_times(sm)
        if n_q:
            sm.save()
            log.info("state_machine quantized %s off-grid enter stamps to 15m nodes", n_q)
        n_bf = backfill_enter_prices_from_snapshots(sm, data_dir / "snapshots")
        if n_bf:
            sm.save()
            log.info("state_machine backfilled enter prices=%s", n_bf)
        transitions = apply_state_machine(
            rows, sm, None, scan_id=scan_id, now_ts=now.timestamp()
        )
        log.info("state_machine transitions=%s", len(transitions))

    dmr_all = build_dmr_messages(
        rows, settings=settings, anchor=anchor, scan_id=scan_id, seq=seq, now=now
    )
    dmr_msgs, dmr_rank = rank_dmr_inbox(dmr_all, top_k=settings.dmr_top_k)
    # DMR 日去重必须在同币方向去重与 Top-K 完成之后记录；确认区日去重不能替代它。
    daily_unique = update_daily_unique(
        data_dir,
        anchor.strftime("%Y-%m-%d"),
        rows,
        dmr_messages=dmr_msgs,
        dmr_inbox=Path(settings.dmr_inbox or data_dir / "dmr_inbox"),
    )

    board = build_screener_snapshot(
        settings,
        anchor=anchor,
        scan_id=scan_id,
        seq=seq,
        now=now,
        rows=rows,
        universe_count=len(symbols),
        uni_ver=uni_ver,
        stats={
            "gate1": g1_stats,
            "gate2": g2_stats,
            "gate3": g3_stats,
            "gate4": g4_stats,
            "mcap_tf": mcap_tf_stats,
            "long_ret": long_ret_stats,
        },
        transitions=transitions,
        daily_unique=daily_unique,
        generated_at=wall,
    )

    occ, ctrl, alerts = decorate_board(
        board,
        rows=rows,
        dmr_msgs=dmr_msgs,
        dmr_rank=dmr_rank,
        daily_unique=daily_unique,
        settings=settings,
        # 主榜**不打**展示区戳：它是冻结板，且没有主导层（final_zone 恒缺），
        # 展示区永远等于状态机分区，state_enter_* 本来就是对的。
        # 传 sm 会让它在升级首个节点被整表重置（实测 20260904-044 波及 1048 行）。
    )

    full = {
        "status": "ok",
        "scan_id": scan_id,
        "universe_source": uni_src,
        "universe_count": len(symbols),
        "gate1": g1_stats,
        "gate2": g2_stats,
        "gate3": g3_stats,
        "gate4": g4_stats,
        "mcap_tf": mcap_tf_stats,
        "long_ret": long_ret_stats,
        "transitions": transitions,
        "dmr_confirmed": len(dmr_all),
        "dmr_inbox": len(dmr_msgs),
        "dmr_rank": dmr_rank,
        "occupancy": occ,
        "control": ctrl,
        "daily_unique": daily_unique,
        "board_long": len(board["long_pool"]),
        "board_short": len(board["short_pool"]),
        "rows": rows,
    }
    _atomic_write(snaps / f"{scan_id}.full.json", full)
    _atomic_write(snaps / f"{scan_id}.json", board)
    _atomic_write(data_dir / "latest.json", board)
    _atomic_write(data_dir / "latest.internal.json", full)

    inbox = Path(settings.dmr_inbox or data_dir / "dmr_inbox")
    inbox.mkdir(parents=True, exist_ok=True)
    # —— 执行身份下发（ChatGpt_SOL5.6 文档A §14 / 文档B §18.6）——
    #
    # 主榜是**唯一**可执行板面。正向 allow-only 谓词要求 batch 与 candidate 两侧都带
    # board_key / dmr_executable / consumable_by_dmr + 六个规则身份字段；缺任何一个
    # 都拒绝执行。主榜的规则身份（rule_revision/config_hash/…）不改变任何打分逻辑，
    # 只是把「这批候选属于哪套已部署规则」写清楚。
    main_identity = main_exec_identity(settings)
    for _m in dmr_msgs:
        _m.update(main_identity)
    for _m in dmr_all:
        _m.setdefault("board_key", main_identity["board_key"])
    _atomic_write(
        inbox / f"{scan_id}.candidates.json",
        {
            **main_identity,
            "scan_id": scan_id,
            "generated_at_utc": board["generated_at_utc"],
            "candidates": dmr_msgs,
            "all_confirmed": dmr_all,
            "rank": dmr_rank,
            "note": "CONFIRMED only; Top-K unique coins; QUALIFIED never consumed",
        },
    )

    try:
        ingest_review_scan(
            data_dir,
            scan_id,
            inbox_dir=Path(settings.dmr_inbox) if settings.dmr_inbox else None,
        )
    except Exception as e:
        log.warning("review ledger ingest skipped: %s", e)

    # —— 次级板面投影（当前：「选币榜Y」/ param-v2.0.0-screener-y）——
    #
    # 主板面（选币榜 / param-v1.4.0）到这里已经完全落盘，上面一行都不受下面影响。
    # 投影复用本轮已取到的 G1–G4 指标，按变体自己的参数重算 Score / 状态机 / DMR 区，
    # 写进各自独立的 data_dir、DMR inbox 与复盘账本 —— 不额外打任何交易所接口。
    #
    # 与复盘账本 ingest 同一条约定（P9）：这里失败只记 WARN，绝不允许拖垮 15 分钟循环。
    variant_boards: list[dict[str, Any]] = []
    try:
        from .board_projection import project_secondary_boards

        variant_boards = project_secondary_boards(
            settings,
            symbols=symbols,
            prices=prices,
            g1=g1_map,
            g2=g2_map,
            g3=g3_map,
            g4=g4_map,
            mcap_tf=mcap_tf_map,
            long_ret=long_ret_map,
            anchor=anchor,
            scan_id=scan_id,
            seq=seq,
            now=now,
            universe_count=len(symbols),
            uni_ver=uni_ver,
            stats={
                "gate1": g1_stats,
                "gate2": g2_stats,
                "gate3": g3_stats,
                "gate4": g4_stats,
                "mcap_tf": mcap_tf_stats,
                "long_ret": long_ret_stats,
            },
            generated_at=wall,
            # 首跑对齐：选币榜Y 冷启动时继承一次主板面当刻的状态机，
            # 否则「100% 复刻」会在最需要验证的第一天不成立（见 board_projection）。
            primary_state_path=data_dir / "state_machine.json",
            skip_sm=skip_sm,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("secondary board projection skipped: %s", e)

    log.info(
        "scan %s g1_pass=%s long=%s conf=%s dmr=%s",
        scan_id,
        g1_stats.get("passed"),
        len(board["long_pool"]),
        sum(1 for r in board["long_pool"] if r["state"] == "CONFIRMED"),
        len(dmr_msgs),
    )
    out_doc = {
        "status": "ok",
        "scan_id": scan_id,
        "gate1": g1_stats,
        "gate2": g2_stats,
        "gate3": g3_stats,
        "gate4": g4_stats,
        "mcap_tf": mcap_tf_stats,
        "long_ret": long_ret_stats,
        "board_long": len(board["long_pool"]),
        "confirmed_long": sum(1 for r in board["long_pool"] if r["state"] == "CONFIRMED"),
        "confirmed_unique": occ.get("confirmed_unique"),
        "daily_unique_confirmed": daily_unique.get("confirmed"),
        "alerts": alerts,
        "dmr_messages": len(dmr_msgs),
        "dmr_all_confirmed": len(dmr_all),
        "transitions": len(transitions),
        "latest": str(data_dir / "latest.json"),
        # 纯附加字段：主板面消费方（loop_status / 冒烟）看不懂就忽略，不影响任何既有断言。
        "variant_boards": variant_boards,
    }
    # 记完成：下一次（重启后）再遇到同一个 scan_id 就会被上面的守卫挡住。
    locks.mark_complete(scan_id)
    return out_doc


def _atomic_write(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def main() -> None:
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    dotenv_path = load_dotenv()
    if dotenv_path:
        log.info("loaded env from %s", dotenv_path)

    p = argparse.ArgumentParser(description="coin-selection full gates scan")
    p.add_argument("--force", action="store_true")
    p.add_argument("--max-symbols", type=int, default=None)
    p.add_argument("--workers", type=int, default=None)
    p.add_argument("--skip-gate1", action="store_true")
    p.add_argument("--skip-gate2", action="store_true")
    p.add_argument("--skip-gate3", action="store_true")
    p.add_argument("--skip-gate4", action="store_true")
    p.add_argument(
        "--skip-mcap-tf",
        action="store_true",
        help="Skip the 30m/2h/6h circulating-mcap grade columns (board display only)",
    )
    p.add_argument(
        "--skip-long-ret",
        action="store_true",
        help="Skip the 1Week/1Month return columns (board display only)",
    )
    p.add_argument("--skip-sm", action="store_true")
    p.add_argument("--loop", action="store_true")
    p.add_argument(
        "--fast-confirm",
        action="store_true",
        help="LAB ONLY: set SM_FAST=1 (cascade/no dwell). Production must omit this.",
    )
    p.add_argument(
        "--simulate-scans",
        type=int,
        default=1,
        help="Run N sequential production SM ticks (natural dwell; no SM_FAST). "
        "Use e.g. 9 to approach earliest CONFIRMED path under full thresholds.",
    )
    p.add_argument(
        "--force-mapping",
        action="store_true",
        help="Force refresh CoinGecko futures mapping (applies pseudo overrides)",
    )
    args = p.parse_args()
    settings = SelectionSettings()
    if args.max_symbols is not None:
        settings.gate1_max_symbols = args.max_symbols
    if args.workers is not None:
        settings.gate1_workers = args.workers
        settings.gate2_workers = args.workers
    if args.force_mapping:
        os.environ["FORCE_CG_MAPPING"] = "1"

    # Production default: never enable SM_FAST unless explicitly requested
    if args.fast_confirm:
        os.environ["SM_FAST"] = "1"
        log.warning("SM_FAST enabled — NOT for production")
    else:
        # Ensure production path is clean even if env leaked from prior shell
        if os.environ.get("SM_FAST") in ("1", "true", "yes") and not args.fast_confirm:
            # only clear if user did not ask for fast; keep if they export intentionally
            # Prefer explicit: clear unless COIN_SELECTION_ALLOW_SM_FAST=1
            if os.environ.get("COIN_SELECTION_ALLOW_SM_FAST") not in ("1", "true", "yes"):
                os.environ.pop("SM_FAST", None)
                log.info("SM_FAST cleared for production scan path")

    n = max(1, int(args.simulate_scans or 1))
    if args.loop and n > 1:
        log.warning("--simulate-scans ignored with --loop")

    if args.loop:
        status_path = Path(settings.data_dir or ".") / "loop_status.json"
        catchups = 0
        while True:
            out = None
            try:
                out = run_scan_cycle(
                    settings,
                    force=args.force,
                    skip_gate1=args.skip_gate1,
                    skip_gate2=args.skip_gate2,
                    skip_gate3=args.skip_gate3,
                    skip_gate4=args.skip_gate4,
                    skip_mcap_tf=args.skip_mcap_tf,
                    skip_long_ret=args.skip_long_ret,
                    skip_sm=args.skip_sm,
                )
                try:
                    # —— 副板面出数状态必须冒到循环状态里 ——
                    #
                    # 受约束板面（选币榜Y）遇到未授权 / 身份漂移会 **拒绝出数**。
                    # 此前 loop_status 只描述主扫描，于是 Y 停更时这里照旧写
                    # "ok"，运维与 /health 都看不出板面已经黑了（本轮裁决四）。
                    vb = out.get("variant_boards") or []
                    board_status = [
                        {
                            "board": b.get("board"),
                            "status": b.get("status") or "ok",
                            **(
                                {"reason": b.get("reason")}
                                if b.get("reason")
                                else {}
                            ),
                            **({"error": b.get("error")} if b.get("error") else {}),
                            "scan_id": b.get("scan_id") or out.get("scan_id"),
                        }
                        for b in vb
                    ]
                    degraded = [
                        b for b in board_status if b["status"] not in ("ok", "skipped")
                    ]
                    status_path.write_text(
                        json.dumps(
                            {
                                # 主扫描成功但有副板面拒绝出数 ⇒ degraded，不是 ok。
                                "status": "degraded" if degraded else "ok",
                                "updated_at_utc": utc_now()
                                .replace(microsecond=0)
                                .isoformat()
                                .replace("+00:00", "Z"),
                                "scan_id": out.get("scan_id"),
                                "confirmed_long": out.get("confirmed_long"),
                                "confirmed_unique": out.get("confirmed_unique"),
                                "daily_unique_confirmed": out.get(
                                    "daily_unique_confirmed"
                                ),
                                "alerts": (out.get("alerts") or [])
                                + (["BOARD_REFUSED"] if degraded else []),
                                "dmr_messages": out.get("dmr_messages"),
                                "board_long": out.get("board_long"),
                                "parameter_version": settings.parameter_version,
                                "boards": board_status,
                                "degraded_boards": [b["board"] for b in degraded],
                                "sm_fast": os.environ.get("SM_FAST"),
                                "pid": os.getpid(),
                            },
                            indent=2,
                        ),
                        encoding="utf-8",
                    )
                except Exception:
                    log.exception("status write failed")
            except Exception:
                log.exception("scan failed")
                try:
                    status_path.write_text(
                        json.dumps(
                            {
                                "status": "error",
                                "updated_at_utc": utc_now()
                                .replace(microsecond=0)
                                .isoformat()
                                .replace("+00:00", "Z"),
                                "pid": os.getpid(),
                            },
                            indent=2,
                        ),
                        encoding="utf-8",
                    )
                except Exception:
                    pass
            now = utc_now()

            # A scan stamps the node it STARTED in. If it ran long and finished
            # inside a later node, that node has no snapshot yet — sleeping to
            # the next boundary would skip it permanently. Take it now instead.
            # (Three nodes were lost this way: 20260815-020, 20260817-005,
            # 20260824-046. `--force` is on in production so the re-run is not
            # blocked by the per-node lock, and run_scan_cycle always scans the
            # node it is currently in, so this converges rather than replaying.)
            pending = node_overrun((out or {}).get("scan_id"), now)
            if pending is not None:
                catchups += 1
                log.warning(
                    "scan %s overran its node; %s has no snapshot — scanning it now "
                    "instead of sleeping past it (consecutive=%d)",
                    (out or {}).get("scan_id"),
                    pending,
                    catchups,
                )
                if catchups >= LOOP_CATCHUP_ALERT:
                    # Scans are taking longer than a node lasts. Keep going —
                    # stopping would resume dropping nodes — but make it loud.
                    log.error(
                        "selection scan has overrun its 15m node %d times in a row; "
                        "the loop is permanently behind and every node costs a full scan",
                        catchups,
                    )
                time.sleep(LOOP_CATCHUP_FLOOR_SEC)
                continue

            catchups = 0
            sec = now.minute * 60 + now.second
            wait = 900 - (sec % 900) + 5
            log.info("loop sleep %ss until next 15m node", wait)
            time.sleep(wait)
    else:
        outs = []
        base_now = utc_now()
        for i in range(n):
            # Natural dwell: each simulated scan advances 15 minutes of SM clock
            tick_now = base_now + timedelta(minutes=15 * i) if n > 1 else base_now
            out = run_scan_cycle(
                settings,
                now=tick_now,
                force=True if n > 1 else args.force,
                skip_gate1=args.skip_gate1,
                skip_gate2=args.skip_gate2,
                skip_gate3=args.skip_gate3,
                skip_gate4=args.skip_gate4,
                skip_mcap_tf=args.skip_mcap_tf,
                skip_long_ret=args.skip_long_ret,
                skip_sm=args.skip_sm,
            )
            out["simulate_tick"] = i + 1
            out["tick_now_utc"] = tick_now.replace(microsecond=0).isoformat().replace("+00:00", "Z")
            outs.append(out)
            log.info(
                "simulate tick %s/%s conf_long=%s dmr=%s sm_fast=%s scan=%s",
                i + 1,
                n,
                out.get("confirmed_long"),
                out.get("dmr_messages"),
                os.environ.get("SM_FAST"),
                out.get("scan_id"),
            )
        print(json.dumps(outs[-1] if n == 1 else {"ticks": outs, "final": outs[-1]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
