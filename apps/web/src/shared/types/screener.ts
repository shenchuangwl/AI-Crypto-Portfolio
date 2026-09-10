export type Direction = 'up' | 'down';

export type ScreenerState =
  | 'NONE'
  | 'WATCH'
  | 'QUALIFIED'
  | 'CONFIRMED'
  | 'ELIMINATED'
  | 'DATA_INSUFFICIENT'
  | 'LOW_CONFIDENCE';

export type DataMode =
  | 'LIVE'
  | 'CACHE_FRESH'
  | 'CACHE_STALE'
  | 'SUPPLY_CACHE_PRICE_LIVE'
  | 'MISSING'
  | 'BACKFILL';

export type LiquidityGrade = 'A' | 'B' | 'C' | 'D' | 'UNCLASSIFIED';

/**
 * 216 组合天花板层的业务五区枚举（rule_revision y-v2.0.0-r3）。
 * 顺序即 rank：DMR=0 最好，ELIMINATED=4 最差；合并恒为 max(rank)，只降不升。
 */
export type McapZone = 'DMR' | 'CONFIRMED' | 'QUALIFIED' | 'WATCH' | 'ELIMINATED';

/** 五区中文标签（与后端 ZONE_CN 一致）。DMR 不译，保持与执行层同名。 */
export const MCAP_ZONE_LABEL: Record<McapZone, string> = {
  DMR: 'DMR',
  CONFIRMED: '确定',
  QUALIFIED: '符合',
  WATCH: '观察',
  ELIMINATED: '淘汰',
};

/** 选币榜三列 `30m流通市值 / 2h流通市值 / 6h流通市值` 共用的周期流通市值等级。 */
export type McapTfGrade = 'A' | 'B' | 'C' | 'D' | 'E' | 'F';

/** 新增三列对应的周期，顺序即列顺序。 */
export const MCAP_TIMEFRAMES = ['30m', '2h', '6h'] as const;
export type McapTimeframe = (typeof MCAP_TIMEFRAMES)[number];

/**
 * 周期流通市值等级定义（三个周期使用完全相同的一套规则，仅计算数据所属周期不同；
 * 上涨候选与下跌候选也共用同一套定义，绝不拆成两套）：
 *
 *   A：6 日平均流通市值 > 12 日平均流通市值 > 26 日平均流通市值 | 多头排列
 *   B：12 日平均流通市值 > 6 日平均流通市值 > 26 日平均流通市值 | 多头轻度回调
 *   C：12 日平均流通市值 > 26 日平均流通市值 > 6 日平均流通市值 | 多头重度回调
 *   D：12 日平均流通市值 < 26 日平均流通市值 < 6 日平均流通市值 | 空头重度回调
 *   E：12 日平均流通市值 < 6 日平均流通市值 < 26 日平均流通市值 | 空头轻度回调
 *   F：6 日平均流通市值 < 12 日平均流通市值 < 26 日平均流通市值 | 空头排列
 *
 * 其中「日」沿用既有命名，实际含义是**当前周期下的 K 线根数**。
 */
export const MCAP_GRADE_MEANING: Record<McapTfGrade, string> = {
  A: '多头排列（6 日 > 12 日 > 26 日平均流通市值）',
  B: '多头轻度回调（12 日 > 6 日 > 26 日平均流通市值）',
  C: '多头重度回调（12 日 > 26 日 > 6 日平均流通市值）',
  D: '空头重度回调（12 日 < 26 日 < 6 日平均流通市值）',
  E: '空头轻度回调（12 日 < 6 日 < 26 日平均流通市值）',
  F: '空头排列（6 日 < 12 日 < 26 日平均流通市值）',
};

export const MCAP_TIMEFRAME_LABEL: Record<McapTimeframe, string> = {
  '30m': '30m流通市值',
  '2h': '2h流通市值',
  '6h': '6h流通市值',
};

/** 单周期均线明细，仅用于 tooltip / 审计；等级本身走扁平的 mcap_grade_* 字段。 */
export interface McapTfDetail {
  grade: McapTfGrade | null;
  /** 6 日(根)平均流通市值 USD；流通供应量未知时为 null（等级依然有效） */
  ma6: number | null;
  ma12: number | null;
  ma26: number | null;
  bars: number;
  supply_known: boolean;
  reason: string;
}

export interface ScanMeta {
  system_version: string;
  anchor_date: string;
  scan_id: string;
  scan_sequence: number;
  scan_timestamp_utc: string;
  effective_universe: number;
  baseline_universe: number;
  regime: number;
  regime_label: string;
  data_mode: DataMode;
  state_counts: Partial<Record<ScreenerState, number>>;
  coingecko_credits: {
    used_today: number;
    month_est: number;
    month_cap: number;
    utilization: number;
    alert_level: 'ok' | 'warn' | 'critical';
  };
  parameter_version: string;
  mapping_version: string;
  data_version: string;
  indicator_version?: string;
  occupancy?: {
    confirmed_unique?: number;
    qualified_unique?: number;
    sides?: Partial<Record<ScreenerState, number>>;
    ready_confirm?: Array<{ symbol: string; direction: Direction; score?: number }>;
  };
  /** Per-anchor-day unique-symbol roll-up. Never render it as occupancy (计划 §13.4). */
  daily_unique?: {
    anchor_date?: string;
    confirmed?: number;
    qualified?: number;
    watch?: number;
    eliminated?: number;
    /** Actual post-dedupe, Top-K DMR inbox selections seen in this UTC cycle. */
    dmr?: number;
    confirmed_symbols?: string[];
    dmr_symbols?: string[];
  };
  control?: {
    n_impulse?: number;
    live_ratio?: number;
    freeze_new_confirm?: boolean;
    low_breadth?: boolean;
    data_stress?: boolean;
    universe_stress?: boolean;
    universe_dev?: number;
    g1_unique?: number;
  };
  dmr?: {
    unique_before_k?: number;
    inbox_k?: number;
    inbox_count?: number;
    truncated?: number;
    underfilled?: boolean;
    alert?: string;
    alerts?: string[];
  };
  hierarchy?: {
    dmr_unique?: number;
    confirmed_unique?: number;
    qualified_including_confirmed_unique?: number;
    invariant?: string;
  };
  alert?: string;
  alerts?: string[];
  /** 板面变体标识（选币榜Y 才有；选币榜的快照不带这几个字段） */
  board_key?: string;
  board_label?: string;
  board_projected?: boolean;
  dmr_executable?: boolean;
  /**
   * 时间区（周期）状态。
   *
   * 只有启用了周期管理的板面才有：「选币榜Y」是 24 小时循环（起点 00:00 UTC，
   * 与 `anchor_date` / `scan_id` 的 000 号节点同一刻度）；「选币榜」没有周期，
   * 快照里连这个键都不存在。
   */
  cycle?: ScanCycle;
  // —— ChatGpt_SOL5.6 第三轮新增的审计元数据（可选）——
  /** SOL5.6 口径有效区层声明块 + 统计（文档B §8.1）。mode=off 时不下发。 */
  mcap_effective?: Record<string, unknown> | null;
  /**
   * 不可变规则身份（文档A §13.1 / 文档B §3.2）：一致性等式的左半边。
   * rule_revision / config_hash / asset_mapping_version / mcap_mapping_version /
   * mapping_hash / code_commit / data_contract_version / universe_policy_version /
   * effective_from_utc / effective_from_scan_id / feature_flags。
   * 身份不同即为不同规则段 —— 复盘默认不得跨段聚合。
   */
  rule_identity?: Record<string, unknown> | null;
}

/** `meta.cycle` —— 板面的时间区状态。事实源：coin_selection/cycle_reset.py */
export interface ScanCycle {
  enabled: boolean;
  period_hours?: number;
  /** 周期起点的 UTC 时刻，"HH:MM" */
  anchor_utc?: string;
  /** 周期身份，例 `20260825T0000Z`；也是重置的幂等键 */
  cycle_key?: string;
  cycle_start_utc?: string;
  cycle_end_utc?: string;
  /** 本周期内的第几个 15m 节点（0 = 周期起点） */
  node_in_cycle?: number;
  nodes_per_cycle?: number;
  /** 这一节点是否刚刚执行了清空重建 */
  reset_at_this_node?: boolean;
  last_reset_cycle_key?: string | null;
  last_reset_scan_id?: string | null;
  last_reset_at_utc?: string | null;
  last_reset_node_in_cycle?: number | null;
  /** true = 本周期的分区不是从 0 号节点开始重建的（首次启用 / 循环停机跨过了周期节点） */
  partial_cycle?: boolean | null;
  partial_reason?: 'cold_start' | 'missed_cycle_node' | null;
  /** 重置前一刻各分区的成员数（只在重置节点出现） */
  closed_zones?: Record<string, number>;
  cleared?: Record<string, number | boolean>;
  warmup?: {
    enabled: boolean;
    nodes: number;
    active: boolean;
    state_config?: Record<string, number>;
  };
}

export interface CandidateRow {
  rank: number;
  symbol: string;
  underlying_asset: string;
  canonical_asset_id: string;
  contract_multiplier: number;
  direction: Direction;
  state: ScreenerState;
  state_enter_time_utc: string;
  state_duration_minutes: number;
  /** Last/mark when this side first entered current QUALIFIED/CONFIRMED. Other states: null. */
  state_enter_price?: number | null;
  score_up: number;
  score_down: number;
  direction_confidence: number;
  liquidity_score: number;
  liquidity_grade: LiquidityGrade;
  /** 「等级」与「1h」之间的三列：周期流通市值等级。判不出级（K 线不足 / 均线并列）时为 null。 */
  mcap_grade_30m?: McapTfGrade | null;
  mcap_grade_2h?: McapTfGrade | null;
  mcap_grade_6h?: McapTfGrade | null;
  mcap_tf?: Partial<Record<McapTimeframe, McapTfDetail>>;
  /**
   * 216 组合天花板 / 流通市值主导层的逐币审计链（rule_revision y-v2.0.0-r3 起）。
   *
   * 三周期 A–F 等级 → 组合号 → 方向 Z10 → 共振 K → 天花板 → 谓词/互印证 → 最终分区 → RankKey。
   * 全部 optional + nullable：主导层关闭时（以及主榜 v1.4.0）后端一个键都不写，
   * 旧快照照常合法（文档B §3.2 向后兼容三条铁律：只加不改不删、新字段全 nullable、
   * 前端只渲染不重算）。
   */
  mcap_combo_code?: string | null;
  mcap_combo_no?: number | null;
  mcap_combo_status?: 'COMPLETE' | 'INCOMPLETE' | 'NOT_APPLICABLE' | null;
  /** 方向 Z10（整数，值域 −30…+30）。Z = Z10/10。 */
  mcap_z10?: number | null;
  /** |Z10| 分档得到的优先级 1–5。 */
  mcap_priority?: number | null;
  /** 三周期结构共振度 K ∈ {100, 70, 40, 10}。 */
  mcap_resonance_k?: number | null;
  /** 该方向的 216 天花板（业务五区枚举）。 */
  mcap_ceiling_zone?: McapZone | null;
  /** 状态机自己的分区（未经天花板压制）。 */
  base_state?: ScreenerState | null;
  /** max(base, ceiling) 之后的分区。DMR 是派生集合，绝不作为取值（文档A §5）。 */
  effective_zone?: ScreenerState | null;
  /**
   * 再经 P1/P2/P3 与 SS×市值互印证之后的最终分区 —— 这就是 state 的来源。
   * NONE / DATA_INSUFFICIENT / LOW_CONFIDENCE 是特殊态**原样旁路**（文档A §5），
   * 不进五区也不参与排序权重，但会原样出现在这里，所以值域比 McapZone 宽。
   */
  final_zone?: McapZone | ScreenerState | null;
  mcap_predicate?: 'P1_CONFIRM' | 'P2_SOFTEN' | 'P3_VETO' | null;
  mcap_crosscheck?: 'ALIGNED' | 'DIVERGENT' | null;
  w_base?: number | null;
  w_prio?: number | null;
  w_k?: number | null;
  w_combo?: number | null;
  w_final?: number | null;
  /** (Score/100) × (W_final / 1.210)。主导层生效时的排序第一裁决之一。 */
  rank_key?: number | null;
  mcap_reason_codes?: string[] | null;
  momentum_score: number;
  mcap_momentum_score: number;
  staircase_score: number;
  consistency_score: number;
  rank_velocity_score: number;
  risk_score: number;
  data_confidence: number;
  ret_15m: number | null;
  ret_1h: number;
  ret_4h: number;
  ret_24h: number;
  /**
   * 「24h」与「锚点以来」之间的两列：近 7 天 / 近 30 天涨跌幅。
   * 口径与 1h/4h/24h 完全一致（最新价 / 回看 N 根之前的收盘价 − 1），只是取数周期不同；
   * 日 K 根数不足以回看时为 null（显示 `—`），绝不折叠成 0。
   * 命名用 `1mo` 而非 `1m`：`ret_15m` 已经占用了"分钟"语义。
   */
  ret_1w: number | null;
  ret_1mo: number | null;
  ret_since_anchor: number | null;
  aqv_6d_m: number;
  aqv_12d_m: number;
  aqv_26d_m: number;
  circulating_supply: number | null;
  market_cap_coingecko: number | null;
  market_cap_calculated: number | null;
  supply_source: 'CG' | 'BINANCE_CS' | 'CACHE' | 'NONE';
  data_mode: DataMode;
  supply_as_of_utc: string | null;
  mapping_confidence: number;
  risk_flags: string[];
  reason_codes: string[];
  not_confirmed_reasons: string[];
  /**
   * 展示区（用户看到的那个区：DMR / final_zone）的进入时刻、停留与进入价。
   *
   * 与 `state_enter_*` 的区别：后者跟随状态机 base_state（层 A），主导层生效后
   * 与板面显示的分区可能不同 —— 那正是「离开确认区又回来、时间却没重置」的成因。
   * 主榜 v1.4.0 与主导层关闭时后端不写这三个键，前端回退 `state_enter_*`。
   * **服务端算好下发，前端只渲染。**
   */
  zone_enter_time_utc?: string | null;
  zone_duration_minutes?: number | null;
  zone_enter_price?: number | null;
  /** Dual-path audit: S | M. Display only. */
  qualified_path?: 'S' | 'M' | null;
  confirmed_path?: 'S' | 'M' | null;
  ready_confirm?: boolean;
  /** Exact executable DMR/精选 subset selected from CONFIRMED. */
  dmr_selected?: boolean;
  /** 选币榜Y 216 合取（ENABLE=1）。主板快照没有这些键。 */
  mcap_combo?: string | null;
  mcap_k?: number | null;
  mcap_ceiling?: string | null;
  /** worse(SM, ceiling)。Y 芯片优先读它；缺省回退 state。 */
  product_zone?: string | null;
  combo_reason?: string | null;
  /** 文档A §4 天花板层：三字母组合，判不出级为 null。mcap_zone_mode=off 时不下发。 */
  combo_code?: string | null;
  /** 方向分 Z_direction ∈ [-3, +3]（文档A §4.2）。上涨侧 = +Z，下跌侧 = -Z。 */
  z_score?: number | null;
  /** 216 表查出的名义天花板（不含谓词调整）。 */
  combo_zone?: ScreenerState | 'DMR' | null;
  /**
   * 实际生效的天花板（含弃权 / P1–P3 / SS 背离）。**服务端下发，前端只渲染。**
   * 前端不得用 mcap_grade_* 自己查表重算（文档A 红线第 19 条）。
   */
  zone_ceiling?: ScreenerState | 'DMR' | null;


  // —— ChatGpt_SOL5.6 文档A §13.2 每行血缘 ——
  //
  // 其余血缘字段（base_state / mcap_combo_no / mcap_combo_status / mcap_priority /
  // mcap_resonance_k / mcap_ceiling_zone / effective_zone）已并入上面 y-v2.0.0-r3 的
  // 主导层审计链，不在此重复声明 —— 重复声明会让两处类型分叉（TS2717）。
  /** Y5_* 有效区原因码（文档A §13.2）。 */
  effective_reason_codes?: string[] | null;
  ref_price?: number;
  last_price?: number;
  price_change_since_scan?: number;
  market_rank?: number;
  tier_rank?: number;
  market_cap_tier?: 'T1' | 'T2' | 'T3' | 'T4' | 'T5';
  coingecko_coin_id?: string;
}

export interface TransitionEvent {
  id: string;
  at_utc: string;
  symbol: string;
  from_state: ScreenerState;
  to_state: ScreenerState;
  direction: Direction;
  reason_codes: string[];
}

export interface ScreenerSnapshot {
  meta: ScanMeta;
  long_pool: CandidateRow[];
  short_pool: CandidateRow[];
  transitions: TransitionEvent[];
  generated_at_utc: string;
  expires_at_utc: string;
  attribution?: string;
}

export type { KlineInterval } from '../lib/intervals';

export interface OhlcvBar {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export type ChartOverlay =
  | { type: 'anchor_line'; payload: { price: number; label?: string } }
  | { type: 'confirm_marker'; payload: { time: number; price: number; score?: number } }
  | {
      type: 'entry_arrow';
      payload: {
        time: number;
        price: number;
        direction: Direction;
        state: 'QUALIFIED' | 'CONFIRMED';
      };
    }
  | { type: 'scan_marker'; payload: { time: number; scan_sequence?: number } }
  | { type: 'state_band'; payload: unknown }
  | { type: 'pivot'; payload: unknown };

export interface SymbolPathNode {
  scan_sequence: number;
  scan_timestamp_utc: string;
  state: ScreenerState;
  score_up: number;
  score_down: number;
  ret_since_anchor: number;
}

export interface SymbolScreenerDetail {
  current: CandidateRow | null;
  path: SymbolPathNode[];
  meta: ScanMeta;
}

export const STATE_LABEL: Record<ScreenerState, string> = {
  NONE: '—',
  WATCH: '观察',
  QUALIFIED: '符合',
  CONFIRMED: '确认',
  ELIMINATED: '淘汰',
  DATA_INSUFFICIENT: '数据不足',
  LOW_CONFIDENCE: '低置信度',
};

/** Default visible pool: live boards + WATCH (CONFIRMED still needs dwell). */
export const DEFAULT_POOL_STATES: ScreenerState[] = ['CONFIRMED', 'QUALIFIED', 'WATCH'];
export const ISOLATED_STATES: ScreenerState[] = [
  'DATA_INSUFFICIENT',
  'LOW_CONFIDENCE',
  'ELIMINATED',
  'WATCH',
];

/** Normalize legacy API/mock payloads that used OBSERVE */
export function normalizeScreenerState(raw: string): ScreenerState {
  if (raw === 'OBSERVE') return 'WATCH';
  return raw as ScreenerState;
}
