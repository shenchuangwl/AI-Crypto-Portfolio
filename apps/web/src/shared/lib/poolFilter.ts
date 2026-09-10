import type { CandidateRow, LiquidityGrade, ScreenerState } from '../types/screener';

/**
 * 选币榜一次筛选的全部判据。
 *
 * 分区 = 六个状态 chip + DMR。DMR 不是第七个状态，而是叠在 CONFIRMED 之上的
 * **可执行子集标记**（`dmr_selected`）—— 所以「DMR + 确认」这种多选里，同一行会被
 * 两个分区同时命中，任何"按分区分别计数再相加"的写法都会把它数成 2。
 */
export interface PoolFilter {
  activeStates: ScreenerState[];
  dmrActive: boolean;
  grades: LiquidityGrade[];
  search: string;
}

/**
 * 唯一币种 ID —— 计数的口径是"币种"，不是表格条目。
 *
 * `canonical_asset_id` 是后端下发的规范化币种标识（`base_asset` 小写）；缺失时依次退回
 * `underlying_asset` / `symbol`。统一大写只是为了大小写不敏感，不做别的归一化 ——
 * 宁可少去重，也绝不把两个不同的币并成一个。
 */
export function coinIdOf(r: CandidateRow): string {
  return (r.canonical_asset_id || r.underlying_asset || r.symbol || '').trim().toUpperCase();
}

/**
 * 分区判据：命中任意一个被选中的分区即可（状态多选 ∪ DMR）。
 *
 * 这是**并集**语义：一行同时属于 DMR 区和确认区时，`filter` 只会留下它一次，
 * 天然不会重复累加 —— 去重不是事后补救，而是由数据结构保证的。
 */
const ZONE_RANK: Record<string, number> = {
  DMR: 0,
  CONFIRMED: 1,
  QUALIFIED: 2,
  WATCH: 3,
  ELIMINATED: 4,
};

function isState(z: unknown): z is ScreenerState {
  return (
    z === 'WATCH' ||
    z === 'QUALIFIED' ||
    z === 'CONFIRMED' ||
    z === 'ELIMINATED' ||
    z === 'DATA_INSUFFICIENT' ||
    z === 'LOW_CONFIDENCE'
  );
}

/**
 * 渲染用的分区 —— **全部来自服务端字段，前端一个判据都不重算。**
 *
 * 优先级：
 *   1. `product_zone`  服务端已经算好的 `min(状态机分区, 天花板)`（唯一权威）
 *   2. `zone_ceiling`  天花板本身。只有在它**比 `state` 更严**时才取它 ——
 *      这是文档B §3.3 明确要求的渲染行为，取的是两个服务端字段里更严的那个，
 *      不是「用 mcap_grade_* 查 216 表」。后者违反文档A 红线第 19 条，
 *      board-split.verify.tsx 里有 grep 断言钉住前端不含 A–F 查表常量。
 *   3. `state`         天花板层关闭时的现网行为，逐字节不变。
 */
export function displayZone(r: CandidateRow): ScreenerState {
  const pz = r.product_zone;
  if (isState(pz)) return pz;
  const cap = r.zone_ceiling;
  if (isState(cap) && (ZONE_RANK[cap] ?? -1) > (ZONE_RANK[r.state] ?? -1)) {
    return cap;
  }
  return r.state;
}

export function inSelectedZones(
  r: CandidateRow,
  activeStates: ScreenerState[],
  dmrActive: boolean,
): boolean {
  return activeStates.includes(displayZone(r)) || (dmrActive && !!r.dmr_selected);
}

/** 单行是否通过整套筛选（分区 → 流动性等级 → 搜索）。 */
export function matchesFilter(r: CandidateRow, f: PoolFilter): boolean {
  if (!inSelectedZones(r, f.activeStates, f.dmrActive)) return false;
  if (f.grades.length && !f.grades.includes(r.liquidity_grade)) return false;
  const q = f.search.trim().toUpperCase();
  if (!q) return true;
  return (
    r.symbol.includes(q) ||
    r.underlying_asset.toUpperCase().includes(q) ||
    r.canonical_asset_id.toUpperCase().includes(q)
  );
}

/**
 * 过滤一个方向的候选池。表格渲染与两个方向标签页的计数**共用这一个判据**，
 * 所以「上涨候选池 N」与实际渲染的行数不可能对不上。
 */
export function filterRows(rows: CandidateRow[], f: PoolFilter): CandidateRow[] {
  return rows.filter((r) => matchesFilter(r, f));
}

/** 按唯一币种 ID 去重后的币种数。同一币种出现多次（多合约 / 跨分区）只计 1。 */
export function uniqueCoinCount(rows: CandidateRow[]): number {
  const seen = new Set<string>();
  for (const r of rows) {
    const id = coinIdOf(r);
    if (id) seen.add(id);
  }
  return seen.size;
}

/** 两个方向各自的筛选结果 + 去重后的唯一币种数。 */
export interface FilteredPools {
  long: CandidateRow[];
  short: CandidateRow[];
  /** 「上涨候选池」标签页上的数字：当前分区组合并集内的唯一币种数。 */
  longCoins: number;
  /** 「下跌候选池」标签页上的数字。 */
  shortCoins: number;
}

/**
 * 一次算完两个池 —— 表格取 `long`/`short`，方向标签页取 `longCoins`/`shortCoins`，
 * 两者出自同一次过滤，数字与行永远对得上。
 *
 * 纯函数，不碰实时报价：报价每 8 秒刷新一次，标签页上的币种数不该跟着抖，
 * 1050 行也不该跟着重算。
 */
export function filterPools(
  longPool: CandidateRow[],
  shortPool: CandidateRow[],
  f: PoolFilter,
): FilteredPools {
  const long = filterRows(longPool, f);
  const short = filterRows(shortPool, f);
  return {
    long,
    short,
    longCoins: uniqueCoinCount(long),
    shortCoins: uniqueCoinCount(short),
  };
}
