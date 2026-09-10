import { boardConfig, DEFAULT_BOARD, type BoardKey } from '../config/boards';
import type { ReviewResponse, ReviewSortId, ReviewZone } from '../types/review';

const API_BASE =
  (import.meta.env.VITE_API_BASE as string | undefined)?.replace(/\/$/, '') || '/api/v1';

export type ReviewQuery = {
  /**
   * 规则版本 / 板面账本：`main` = 选币榜 v1.4.0，`y` = 选币榜Y v2.0.0。
   *
   * 省略 = `main`，即本次改动之前的行为逐字节不变。两套账本是两个 sqlite 文件，
   * **从不合表**——选入标准变了，跨版本汇总的胜率没有意义（§14.4）。
   */
  board?: BoardKey;
  zones?: ReviewZone[];
  direction?: 'up' | 'down' | 'both';
  symbols?: string[];
  from?: string;
  to?: string;
  /**
   * 周期对齐窗口：最近 N 个周期。**只对启用了 24h 周期的规则版本（v2.0.0）有效**，
   * 传了它就由服务端按周期栅格算 from/to 并覆盖上面两个字段。
   *
   * 为什么不在前端算：栅格定义在服务端的 CycleConfig 里，前端再实现一遍必然漂开，
   * 而漂开的代价是窗口从半夜切开两个周期 —— 实测均值连正负号都会反。
   */
  cycles?: number;
  /** 只算已经跑完的周期，排除进行中的当前周期 */
  whole_cycles?: boolean;
  attribution?: 'exit' | 'enter' | 'contained';
  include_open?: boolean;
  only_flagged?: boolean;
  /**
   * 剔除带这些旗标的交易。主用途 `['CYCLE_RESET']`：把「选币榜Y」24h 周期重置
   * 造成的强制平仓拿掉，只看策略自己的退出决策 —— 强平不是选币标准的成绩。
   */
  exclude_flags?: string[];
  /** 参数版本；省略或 'all' = 不过滤 */
  pv?: string;
  /** 调参段（param_hash）。'null' 特指没有指纹的历史行。 */
  ph?: string;
  min_dwell_nodes?: number;
  preset?: 'executable' | 'funnel' | 'control';
  /** Sorting is server-side on purpose: a window can hold more rows than `limit`. */
  sort?: ReviewSortId;
  desc?: boolean;
  limit?: number;
  offset?: number;
};

export function reviewQueryString(q: ReviewQuery): string {
  const p = new URLSearchParams();
  // 只有非默认板面才带 board=：/review 的既有请求 URL 一个字符都不变。
  if (q.board && q.board !== DEFAULT_BOARD) p.set('board', boardConfig(q.board).reviewBoard);
  if (q.zones?.length) p.set('zones', q.zones.join(','));
  if (q.direction) p.set('direction', q.direction);
  if (q.symbols?.length) p.set('symbols', q.symbols.join(','));
  // cycles 与 from/to 互斥：服务端会用周期栅格覆盖窗口，同时发两套只会让人误解。
  if (q.cycles) {
    p.set('cycles', String(q.cycles));
    if (q.whole_cycles) p.set('whole_cycles', '1');
  } else {
    if (q.from) p.set('from', q.from);
    if (q.to) p.set('to', q.to);
  }
  if (q.attribution) p.set('attribution', q.attribution);
  if (q.include_open) p.set('include_open', '1');
  if (q.only_flagged) p.set('only_flagged', '1');
  if (q.exclude_flags?.length) p.set('exclude_flags', q.exclude_flags.join(','));
  if (q.pv && q.pv !== 'all') p.set('pv', q.pv);
  if (q.ph && q.ph !== 'all') p.set('ph', q.ph);
  if (q.min_dwell_nodes) p.set('min_dwell_nodes', String(q.min_dwell_nodes));
  if (q.preset) p.set('preset', q.preset);
  if (q.sort) p.set('sort', q.sort);
  if (q.desc) p.set('desc', '1');
  if (q.limit) p.set('limit', String(q.limit));
  if (q.offset) p.set('offset', String(q.offset));
  const s = p.toString();
  return s ? `?${s}` : '';
}

async function httpGet<T>(path: string): Promise<T> {
  const url = `${API_BASE}${path.startsWith('/') ? path : `/${path}`}`;
  const res = await fetch(url, { headers: { Accept: 'application/json' } });
  const body = (await res.json()) as T;
  if (!res.ok) {
    const err = body as { error?: string; hint?: string };
    throw new Error(err.hint || err.error || `HTTP ${res.status} ${url}`);
  }
  return body;
}

export function fetchReviewCoverage(q: ReviewQuery = {}): Promise<ReviewResponse> {
  return httpGet(`/review/coverage${reviewQueryString(q)}`);
}

export function fetchReviewSummary(q: ReviewQuery): Promise<ReviewResponse> {
  return httpGet(`/review/summary${reviewQueryString(q)}`);
}

export function fetchReviewTrades(q: ReviewQuery): Promise<ReviewResponse> {
  return httpGet(`/review/trades${reviewQueryString({ ...q, limit: q.limit ?? 800 })}`);
}

export function fetchReviewSymbol(symbol: string, q: ReviewQuery): Promise<ReviewResponse> {
  return httpGet(`/review/symbols/${encodeURIComponent(symbol)}${reviewQueryString(q)}`);
}

export function windowFromDays(endIso: string | undefined, days: number): { from: string; to?: string } {
  const end = endIso ? Date.parse(endIso) : Date.now();
  const from = new Date(end - days * 86400_000).toISOString().replace(/\.\d{3}Z$/, 'Z');
  return { from, to: endIso };
}
