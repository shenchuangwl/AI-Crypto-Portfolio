import type { ReviewSummary } from './review';

/** 《OnlyCoin · 来源候选回放》DMR 区历史统计（服务端 `onlycoin-stats-v1`）。 */

/** 首次入选时所属候选池；`direction` 是板面/盈亏内核用的 up/down 映射。 */
export type OnlyCoinPool = 'LONG' | 'SHORT';

export interface OnlyCoinStatRow {
  /** 唯一币种标识（canonical_asset_id → underlying_asset → symbol，大写）。 */
  coin_id: string;
  symbol: string;
  canonical_asset_id: string | null;
  underlying_asset: string | null;
  contract_multiplier: number | null;
  pool: OnlyCoinPool;
  direction: 'up' | 'down';
  zone: 'DMR';
  /** OPEN = 首次入选那一天还没走完，永不进任何盈亏汇总。 */
  status: 'CLOSED' | 'OPEN';
  business_date: string;
  day_complete: boolean;
  enter_scan_id: string;
  enter_time_utc: string;
  enter_price: number | null;
  enter_price_source: string | null;
  exit_scan_id: string | null;
  exit_time_utc: string | null;
  exit_price: number | null;
  exit_price_source: string | null;
  dwell_minutes: number | null;
  dwell_nodes: number | null;
  pnl_pct: number | null;
  /** 项目内「盈亏数值」列的既有口径：方向感知符号 1 / 0 / −1。 */
  pnl_sign: 1 | 0 | -1 | null;
  first_available_at: string | null;
  availability_quality: string | null;
  directions: string[];
  /** 该唯一币种在本区间内被跳过的重复入选次数（不计入统计）。 */
  repeat_entries: number;
  parameter_version: string | null;
  flags: string[];
  reason_codes: string[];
  mcap_combo: string | null;
  liquidity_grade: string | null;
}

export interface OnlyCoinStatRange {
  id: string;
  label: string;
  from: string;
  to: string;
  /** 因果可用性截止：只计入 `available_at <= cutoff` 的提交。 */
  cutoff: string;
  rows?: OnlyCoinStatRow[];
  summary?: ReviewSummary;
  unique_symbols?: number;
  incomplete_days?: string[];
  days_with_commits?: string[];
  span_days?: number;
  error?: string;
}

export interface OnlyCoinStats {
  schema: 'onlycoin-stats-v1';
  board_key: 'y';
  parameter_version: string;
  business_date: string;
  as_of: string;
  server_time: string;
  dataset_id: 'live' | 'archive';
  node_grid: {
    nodes_per_day: number;
    interval_minutes: number;
    last_node_seq: number;
    day_boundary_utc: string;
    exit_rule: string;
  };
  pnl_basis: { pct: string; value: string; excluded: string[] };
  main: OnlyCoinStatRange;
  compare: OnlyCoinStatRange[];
  snapshot_reads: number;
}

/** 前端侧的对比周期输入（与原复盘 `ReviewPeriod` 同形）。 */
export interface OnlyCoinComparePeriod {
  id: string;
  label: string;
  from: string;
  to: string;
}
