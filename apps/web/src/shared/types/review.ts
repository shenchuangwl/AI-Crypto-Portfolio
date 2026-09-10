/**
 * Review / 复盘选币 types (aligned with contracts/typescript/review.ts).
 */

export type Direction = 'up' | 'down';

export type ReviewZone =
  | 'DMR'
  | 'CONFIRMED'
  | 'QUALIFIED'
  | 'WATCH'
  | 'ELIMINATED'
  | 'DATA_INSUFFICIENT'
  | 'LOW_CONFIDENCE';

export type ReviewTradeStatus = 'CLOSED' | 'OPEN';

export type ReviewFlag =
  | 'MISSING_ENTRY_PRICE'
  | 'MISSING_EXIT_PRICE'
  | 'VANISHED'
  | 'GAP_BEFORE_EXIT'
  | 'TRUNCATED_ENTER'
  | 'PARAM_VERSION_CHANGED'
  | 'ZERO_ENTRY_PRICE'
  | 'DMR_MEMBER_FROM_INBOX'
  /** 24h 周期重置强平（选币榜Y / v2.0.0）——不是策略退出，也不是数据质量问题 */
  | 'CYCLE_RESET'
  /** 跨 param_hash（同一 parameter_version 内的一次调参）的停留（文档B §3.1） */
  | 'PARAM_HASH_CHANGED';

export interface ReviewTrade {
  trade_id: string;
  symbol: string;
  canonical_asset_id: string;
  underlying_asset: string;
  contract_multiplier: number;
  direction: Direction;
  zone: ReviewZone;
  status: ReviewTradeStatus;
  enter_scan_id: string;
  enter_time_utc: string;
  exit_scan_id: string | null;
  exit_time_utc: string | null;
  dwell_minutes: number;
  dwell_nodes: number;
  enter_price: number | null;
  enter_price_source: string | null;
  exit_price: number | null;
  exit_price_source: string | null;
  pnl_pct: number | null;
  pnl_sign: 1 | 0 | -1 | null;
  score: number;
  score_opposite: number;
  direction_confidence: number;
  liquidity_grade: string;
  mcap_grade_30m: string | null;
  mcap_grade_2h: string | null;
  mcap_grade_6h: string | null;
  ret_1h: number | null;
  ret_4h: number | null;
  ret_24h: number | null;
  ret_1w: number | null;
  ret_1mo: number | null;
  ret_since_anchor: number | null;
  risk_flags: string[];
  reason_codes: string[];
  qualified_path: 'S' | 'M' | null;
  confirmed_path: 'S' | 'M' | null;
  parameter_version: string;
  flags: ReviewFlag[];
  mcap_combo?: string | null;
  product_zone?: string | null;
  mcap_k?: number | null;
  /** 入场时生效的参数指纹（文档B §3.1）。阶段 0 之前的行为 null，不回填，UI 显示「—」。 */
  param_hash?: string | null;
  combo_code?: string | null;
  combo_zone?: string | null;
  zone_ceiling?: string | null;
  z_score?: number | null;
}

export interface ReviewDwellBucket {
  from_nodes: number;
  to_nodes: number | null;
  count: number;
}

export interface ReviewEquityPoint {
  at_utc: string;
  cum_pnl_pct: number;
}

export interface ReviewSummary {
  /** 完整「选入+退出」笔数 —— 主数字，不按币去重 */
  trades: number;
  /** 可算盈亏的笔数 —— 胜率/均值的分母 */
  usable: number;
  missing_px: number;
  unique_coins: number;
  /** 未平仓，永不进任何盈亏汇总（R1） */
  open_trades: number;
  up: number;
  down: number;
  win: number;
  flat: number;
  loss: number;
  win_rate: number | null;
  sum_pct: number;
  avg_pct: number | null;
  median_pct: number | null;
  max_pct: number | null;
  min_pct: number | null;
  avg_win_pct?: number | null;
  avg_loss_pct?: number | null;
  profit_factor: number | null;
  avg_dwell_minutes: number | null;
  median_dwell_minutes?: number | null;
  dwell_histogram?: ReviewDwellBucket[];
  equity_curve?: ReviewEquityPoint[];
  max_drawdown_pct?: number | null;
  turnover_per_coin_per_day?: number | null;
  data_quality?: number | null;
  flag_counts?: Record<string, number>;
  by_zone?: Partial<Record<ReviewZone, ReviewSummary>>;
  by_direction?: { up: ReviewSummary; down: ReviewSummary };
  by_parameter_version?: Record<string, ReviewSummary & { trade_share?: number; from_utc?: string; to_utc?: string }>;
  parameter_versions?: string[];
  chip_counts?: Partial<Record<ReviewZone, number>>;
  control_benchmark?: {
    zones: ReviewZone[];
    trades: number;
    avg_pct: number | null;
    win_rate: number | null;
    edge_avg_pct: number | null;
    edge_win_rate: number | null;
    note?: string;
  };
}

export interface ReviewFreshness {
  stale?: boolean;
  latest_scan_id?: string | null;
  latest_ts?: string | null;
  lag_minutes?: number | null;
  lag_nodes?: number | null;
  hint?: string | null;
}

export interface ReviewZoneCoverage {
  from?: string | null;
  to?: string | null;
  rows?: number;
  days?: number | null;
  window_days?: number | null;
}

export interface ReviewParameterSegment {
  version: string;
  from_utc?: string | null;
  to_utc?: string | null;
  first_exit_utc?: string | null;
  last_exit_utc?: string | null;
  trades: number;
}

/**
 * 一个**调参段**（param_hash）。
 *
 * 与 `ReviewParameterSegment` 是两层，别混：
 *   parameter_version  哪套选入标准（v1.4.0 / v2.0.0），一整个版本周期内不变
 *   param_hash         这套标准的**哪一次调参**（overrides 每改一次就换一个）
 *
 * 选币榜Y 的 216 主导层上线前后 parameter_version 都是 param-v2.0.0-screener-y，
 * 只有 param_hash 变（pf1_1b44… → pf1_fcea…）。不按它分段就会把「天花板关」
 * 与「天花板主导」两段混算成一个胜率。
 *
 * `param_hash` 为 null = 阶段 0 之前没有指纹的历史行，不可与任何段合并统计。
 */
export interface ReviewParamHashSegment {
  param_hash: string | null;
  parameter_version?: string | null;
  from_utc?: string | null;
  to_utc?: string | null;
  first_exit_utc?: string | null;
  last_exit_utc?: string | null;
  trades: number;
}

export interface ReviewCoverage {
  watermark_scan_id?: string;
  watermark_ts?: string;
  nodes_ingested?: number | null;
  first_ts?: string | null;
  last_ts?: string | null;
  trade_rows?: number;
  closed_rows?: number;
  open_rows?: number;
  requested_from?: string | null;
  requested_to?: string | null;
  requested_days?: number | null;
  available_days?: number | null;
  coverage_ratio?: number | null;
  zone_available?: Partial<Record<ReviewZone, ReviewZoneCoverage>>;
  short_zones?: ReviewZone[];
  field_availability?: Record<string, { from_utc?: string | null; from_scan_id?: string | null }>;
  parameter_segments?: ReviewParameterSegment[];
  param_hash_segments?: ReviewParamHashSegment[];
  parameter_version?: string;
  ledger_ready?: boolean;
  freshness?: ReviewFreshness;
  /** 这本账本属于哪块板面：`main` = 选币榜(v1.4.0)，`y` = 选币榜Y(v2.0.0) */
  board?: string;
  board_parameter_version?: string | null;
  /** 时间区栅格。启用了 24h 周期的规则版本（v2.0.0）才有 enabled:true */
  cycle?: ReviewCycleCoverage;
}

/** `coverage.cycle` —— 复盘窗口要落在哪张周期栅格上。事实源：api-gateway/review_api.py */
export interface ReviewCycleCoverage {
  enabled: boolean;
  period_hours?: number;
  anchor_utc?: string;
  nodes_per_cycle?: number;
  current_key?: string;
  current_start_utc?: string;
  current_end_utc?: string;
  /** 当前周期已经跑了多少小时（进行中的周期不该和完整周期放在一起平均） */
  current_elapsed_hours?: number;
  current_complete?: boolean;
  current_progress?: number;
  first_cycle_key?: string;
  /** 账本起点落在某个周期中间 → 那个周期是残缺的，不计入 complete_cycles */
  first_cycle_partial?: boolean;
  complete_cycles?: number;
}

/** `filters.cycle_window` —— 服务端最终采用的周期对齐窗口 */
export interface ReviewCycleWindow {
  from: string;
  to: string;
  cycles: number;
  whole_cycles: boolean;
  attribution: string;
  first_cycle_key: string;
  last_cycle_key: string;
  window_start_utc: string;
  window_end_utc: string;
  includes_partial_current: boolean;
  /** 水位正好压在周期起点：当前周期一秒没跑，是空的（不是「跑了一半」） */
  empty_current_cycle?: boolean;
}

export interface ReviewResponse {
  coverage: ReviewCoverage;
  /** 回显本次查询用的板面/规则版本；两套账本从不合表，混算胜率没有意义 */
  board?: string;
  board_parameter_version?: string | null;
  parameter_versions?: string[];
  attribution: 'exit' | 'enter' | 'contained';
  filters?: Record<string, unknown>;
  summary: ReviewSummary;
  trades?: ReviewTrade[];
  count?: number;
  /** 窗口内命中的总行数（忽略 limit/offset），用于「显示 500 / 共 5396」 */
  total?: number;
  truncated?: boolean;
  offset?: number;
  limit?: number;
  error?: string;
  hint?: string;
}

export const REVIEW_ZONES: ReviewZone[] = [
  'DMR',
  'CONFIRMED',
  'QUALIFIED',
  'WATCH',
  'ELIMINATED',
  'DATA_INSUFFICIENT',
  'LOW_CONFIDENCE',
];

export const REVIEW_ZONE_LABEL: Record<ReviewZone, string> = {
  DMR: 'DMR',
  CONFIRMED: '确认',
  QUALIFIED: '符合',
  WATCH: '观察',
  ELIMINATED: '淘汰',
  DATA_INSUFFICIENT: '数据不足',
  LOW_CONFIDENCE: '低置信度',
};

export const CONTROL_ZONES: ReviewZone[] = [
  'ELIMINATED',
  'DATA_INSUFFICIENT',
  'LOW_CONFIDENCE',
];

/** 服务端可排序键（与 review_ledger.SORT_COLS 白名单一一对应） */
export const REVIEW_SORT_KEYS = [
  'enter_time_utc',
  'exit_time_utc',
  'dwell_minutes',
  'dwell_nodes',
  'pnl_pct',
  'pnl_sign',
  'score',
  'direction_confidence',
  'symbol',
  'zone',
  'enter_price',
  'exit_price',
] as const;

export type ReviewSortId = (typeof REVIEW_SORT_KEYS)[number];

export const REVIEW_FLAG_LABEL: Record<string, string> = {
  MISSING_ENTRY_PRICE: '缺停留价',
  MISSING_EXIT_PRICE: '缺退出价',
  ZERO_ENTRY_PRICE: '零停留价',
  VANISHED: '板面消失关账',
  GAP_BEFORE_EXIT: '关账前有节点空洞',
  TRUNCATED_ENTER: '入选早于账本起点',
  PARAM_VERSION_CHANGED: '停留期跨参数版本',
  PARAM_HASH_CHANGED: '停留期跨参数指纹（同版本内调参）',
  DMR_MEMBER_FROM_INBOX: 'DMR 成员取自 inbox',
  CYCLE_RESET: '周期重置强平',
};
