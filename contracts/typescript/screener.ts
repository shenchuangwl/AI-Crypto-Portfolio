/**
 * Shared screener + DMR contract types (design v1.2 §37 / §39).
 * Source of truth alongside JSON Schema under contracts/json-schema/.
 *
 * NOTE: API state uses WATCH (design doc). Frontend F1 historically used OBSERVE
 * as a display alias — map at the UI boundary only.
 */

export type Direction = 'up' | 'down';

/** Board direction ↔ DMR side */
export type DmrDirection = 'LONG' | 'SHORT' | 'NEUTRAL';

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
 * 周期流通市值等级（30m / 2h / 6h 共用同一套定义，上涨/下跌候选不拆分）。
 *   A: MA6  > MA12 > MA26  多头排列
 *   B: MA12 > MA6  > MA26  多头轻度回调
 *   C: MA12 > MA26 > MA6   多头重度回调
 *   D: MA12 < MA26 < MA6   空头重度回调
 *   E: MA12 < MA6  < MA26  空头轻度回调
 *   F: MA6  < MA12 < MA26  空头排列
 * MA_k = 最近 k 根当前周期 K 线流通市值的算术平均（k = 6 / 12 / 26）。
 * K 线不足 26 根或均线并列时为 null —— 禁止新造等级名称。
 */
export type McapTfGrade = 'A' | 'B' | 'C' | 'D' | 'E' | 'F' | null;

export interface McapTfDetail {
  grade: McapTfGrade;
  /** 6 日(根)平均流通市值 USD；流通供应量未知时为 null（等级仍然有效） */
  ma6: number | null;
  ma12: number | null;
  ma26: number | null;
  bars: number;
  supply_known: boolean;
  reason: string;
}

export type McapTfBlock = Partial<Record<'30m' | '2h' | '6h', McapTfDetail>>;

export type MarketCapTier = 'T1' | 'T2' | 'T3' | 'T4' | 'T5';

export interface CoinGeckoCredits {
  used_today: number;
  month_est: number;
  month_cap: number;
  utilization: number;
  alert_level: 'ok' | 'warn' | 'critical';
}

export interface ScanMeta {
  system_version: string;
  /** UTC calendar date of 00:00 anchor, e.g. "2026-08-10" */
  anchor_date: string;
  /** e.g. "20260810-025" */
  scan_id: string;
  scan_sequence: number;
  scan_timestamp_utc: string;
  effective_universe: number;
  baseline_universe: number;
  regime: number;
  regime_label: string;
  data_mode: DataMode;
  state_counts: Partial<Record<ScreenerState, number>>;
  coingecko_credits: CoinGeckoCredits;
  parameter_version: string;
  mapping_version: string;
  data_version: string;
  indicator_version?: string;
  /** Instantaneous per-scan occupancy (what DMR can consume at this node). */
  occupancy?: {
    sides?: Partial<Record<ScreenerState, number>>;
    confirmed_unique?: number;
    qualified_unique?: number;
    ready_confirm?: Array<{ symbol: string; direction: Direction; score?: number }>;
  };
  /** Per-anchor-day unique-symbol roll-up. Never render it as occupancy (计划 §13.4). */
  daily_unique?: {
    anchor_date?: string;
    confirmed?: number;
    qualified?: number;
    watch?: number;
    eliminated?: number;
    confirmed_symbols?: string[];
  };
  /** 计划 §10.2 dynamic control. Alerts annotate; they never relax a gate. */
  control?: {
    n_impulse?: number;
    live_ratio?: number;
    universe_dev?: number;
    universe_count?: number;
    g1_unique?: number;
    freeze_new_confirm?: boolean;
    low_breadth?: boolean;
    data_stress?: boolean;
    universe_stress?: boolean;
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
  /** Last/mark when this side first entered current QUALIFIED/CONFIRMED. */
  state_enter_price?: number | null;

  score_up: number;
  score_down: number;
  direction_confidence: number;
  liquidity_score: number;
  liquidity_grade: LiquidityGrade;
  /** 选币榜「等级」与「1h」之间的三列：30m/2h/6h 周期流通市值等级 */
  mcap_grade_30m?: McapTfGrade;
  mcap_grade_2h?: McapTfGrade;
  mcap_grade_6h?: McapTfGrade;
  mcap_tf?: McapTfBlock;
  momentum_score: number;
  mcap_momentum_score: number;
  staircase_score: number;
  consistency_score: number;
  rank_velocity_score: number;
  risk_score: number;
  data_confidence: number;

  ret_15m: number;
  ret_1h: number;
  ret_4h: number;
  ret_24h: number;
  /** 「24h」与「锚点以来」之间的两列：近 7 天 / 近 30 天涨跌幅。算不出时为 null。 */
  ret_1w: number | null;
  ret_1mo: number | null;
  ret_since_anchor: number;

  /** Avg daily quote turnover, million USD */
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
  qualified_path?: 'S' | 'M' | null;
  confirmed_path?: 'S' | 'M' | null;
  ready_confirm?: boolean;
  dmr_selected?: boolean;
  mcap_combo?: string | null;
  mcap_z10?: number | null;
  mcap_k?: number | null;
  mcap_ceiling?: string | null;
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

  // —— ChatGpt_SOL5.6 文档A §13.2 每行血缘（第三轮新增，全部可选 + 可空）——
  //
  // 只有 mcap_zone_mode ∈ {shadow, on} 且 mcap_ruleset="sol5.6" 时服务端才下发。
  // 前端**只渲染**，绝不用 mcap_grade_* 自己查 216 表重算（红线：单一事实来源）。
  /** 状态机原始 base_state —— effective_zone 不反写它（文档A §10.2）。 */
  base_state?: ScreenerState | null;
  /** 216 组合号 1..216；三周期任一 grade=null 时为 null（文档A §9.5）。 */
  mcap_combo_no?: number | null;
  /** COMPLETE / INCOMPLETE（缺周期）/ NOT_APPLICABLE（特殊态旁路）。 */
  mcap_combo_status?: 'COMPLETE' | 'INCOMPLETE' | 'NOT_APPLICABLE' | null;
  /** |Z10| 分档优先级 1..5，全空间分布 28/54/74/46/14（文档A §9.1）。 */
  mcap_priority?: number | null;
  /** 结构共振度 K ∈ {100, 70, 40, 10}，各 54 组（文档A §9.2）。 */
  mcap_resonance_k?: number | null;
  /** 本方向 ceiling —— 432 方向侧准入上限之一（文档A §9.4）。 */
  mcap_ceiling_zone?: ScreenerState | 'DMR' | null;
  /**
   * 有效区 = max(base_rank, ceiling_rank)，只能保持或降级。
   * DMR 是 CONFIRMED 的派生集合，**绝不会**出现在这里（文档A §5）。
   */
  effective_zone?: ScreenerState | null;
  /** Y5_* 有效区原因码（文档A §13.2）。 */
  effective_reason_codes?: string[] | null;

  /** Scan reference price for display only — never rewrite scores from live ticker */
  ref_price?: number;
  last_price?: number;
  price_change_since_scan?: number;

  market_rank?: number;
  tier_rank?: number;
  market_cap_tier?: MarketCapTier;
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

/** GET /screener/latest */
export interface ScreenerSnapshot {
  meta: ScanMeta;
  long_pool: CandidateRow[];
  short_pool: CandidateRow[];
  transitions: TransitionEvent[];
  generated_at_utc: string;
  expires_at_utc: string;
  attribution?: string;
}

export interface OhlcvBar {
  /** Unix seconds UTC (candle open) */
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface SymbolPathNode {
  scan_sequence: number;
  scan_timestamp_utc: string;
  state: ScreenerState;
  score_up: number;
  score_down: number;
  ret_since_anchor: number;
}

/** GET /screener/symbols/{symbol} */
export interface SymbolScreenerDetail {
  current: CandidateRow | null;
  path: SymbolPathNode[];
  meta: ScanMeta;
}

/**
 * Design v1.2 §39 message published to DMR.
 * message_id = sha1(anchor_date|scan_id|symbol|direction)
 */
export interface DmrCandidateMessage {
  /** X v1.3.0 shadow 不改变 main v1.4.0 复刻的 DMR/复盘排序；独立演进只经 X overrides。 */
  mcap_zone_mode?: 'off' | 'shadow' | 'on';
  message_id: string;
  system_version: string;
  anchor_date: string;
  scan_id: string;
  scan_sequence: number;
  scan_timestamp_utc: string;
  generated_at_utc: string;
  expires_at_utc: string;
  symbol: string;
  underlying_asset: string;
  canonical_asset_id: string;
  contract_multiplier: number;
  direction: DmrDirection;
  state: ScreenerState;
  state_duration_minutes: number;
  direction_confidence: number;
  total_score: number;
  liquidity_score: number;
  momentum_score: number;
  market_cap_momentum_score: number;
  staircase_score: number;
  trend_consistency_score: number;
  rank_velocity_score: number;
  risk_score: number;
  data_confidence: number;
  market_rank: number;
  tier_rank: number;
  market_cap_tier: MarketCapTier;
  coingecko_coin_id: string | null;
  circulating_supply: number | null;
  market_cap_calculated: number | null;
  market_cap_coingecko: number | null;
  risk_flags: string[];
  data_mode: DataMode;
  reason_codes: string[];
  indicator_version: string;
  parameter_version: string;
  mapping_version: string;
  data_version: string;
  attribution?: string;
}

/** §39.3 reject predicates (executor must implement) */
export function dmrShouldReject(
  msg: DmrCandidateMessage,
  now: Date = new Date(),
  opts?: {
    requireConfirmed?: boolean;
    minScore?: number;
    parameterWhitelist?: string[];
  },
): string | null {
  const requireConfirmed = opts?.requireConfirmed ?? true;
  if (requireConfirmed && msg.state !== 'CONFIRMED') return 'state_not_confirmed';
  if (msg.direction === 'NEUTRAL') return 'neutral_direction';
  if (now.getTime() > Date.parse(msg.expires_at_utc)) return 'expired';
  if (msg.data_mode === 'MISSING' || msg.data_confidence < 60) return 'data_quality';
  if (msg.risk_flags.some((f) => f.startsWith('H'))) return 'hard_risk';
  if (msg.circulating_supply == null || msg.circulating_supply <= 0) return 'supply_missing';
  if (
    opts?.parameterWhitelist &&
    !opts.parameterWhitelist.includes(msg.parameter_version)
  ) {
    return 'parameter_version';
  }
  if (opts?.minScore != null && msg.total_score < opts.minScore) return 'score_floor';
  return null;
}

export function boardDirectionToDmr(d: Direction): Exclude<DmrDirection, 'NEUTRAL'> {
  return d === 'up' ? 'LONG' : 'SHORT';
}

export const STATE_LABEL_ZH: Record<ScreenerState, string> = {
  NONE: '—',
  WATCH: '观察',
  QUALIFIED: '符合',
  CONFIRMED: '确认',
  ELIMINATED: '淘汰',
  DATA_INSUFFICIENT: '数据不足',
  LOW_CONFIDENCE: '低置信度',
};

export const DEFAULT_POOL_STATES: ScreenerState[] = ['CONFIRMED', 'QUALIFIED'];

export const ISOLATED_STATES: ScreenerState[] = [
  'DATA_INSUFFICIENT',
  'LOW_CONFIDENCE',
  'ELIMINATED',
  'WATCH',
];

/** WS channel names (ws-gateway) */
export const WS_CHANNELS = {
  screenerUpdated: 'screener.updated',
  ticker: (symbol: string) => `ticker:${symbol}`,
  kline: (symbol: string, interval: string) => `kline:${symbol}:${interval}`,
  book: (symbol: string) => `book:${symbol}`,
  trades: (symbol: string) => `trades:${symbol}`,
} as const;

export interface ScreenerUpdatedEvent {
  type: 'screener.updated';
  scan_id: string;
  anchor_date: string;
  scan_timestamp_utc: string;
  expires_at_utc: string;
}
