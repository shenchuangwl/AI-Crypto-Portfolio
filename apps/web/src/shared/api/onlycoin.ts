import type { OnlyCoinDaily, OnlyCoinLive, OnlyCoinSource, OnlyCoinControl } from '../types/onlycoin';
import type { OnlyCoinStats } from '../types/onlycoinStats';
const BASE = (import.meta.env.VITE_API_BASE as string | undefined)?.replace(/\/$/, '') || '/api/v1';
async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, { ...init, cache: 'no-store', headers: { Accept: 'application/json', ...init?.headers }, signal: init?.signal ?? AbortSignal.timeout(12000) });
  const body = await response.json().catch(() => null);
  if (!response.ok) throw new Error(`HTTP ${response.status}: ${body?.hint || body?.detail || body?.error || response.statusText}`);
  if (!body) throw new Error('Empty OnlyCoin response');
  return body as T;
}
export const fetchOnlyCoinDaily = () => request<OnlyCoinDaily>('/screener-y/dmr-daily');
export const fetchOnlyCoinLive = () => request<OnlyCoinLive>('/onlycoin/live');
export function fetchOnlyCoinReview(businessDate: string, asOf: string): Promise<OnlyCoinDaily> {
  const query = new URLSearchParams({ board: 'y', business_date: businessDate, as_of: asOf });
  return request(`/review/onlycoin?${query}`);
}
/** 回放统计要读板面快照，比名单投影重得多；给它单独的超时，别用 12s 名单预算。 */
export const ONLYCOIN_STATS_TIMEOUT_MS = 45000;

/** 《OnlyCoin · 来源候选回放》DMR 区历史统计。`compare` 为 `from~to` 的独立区间。 */
export function fetchOnlyCoinStats(
  businessDate: string,
  asOf: string,
  options: { from?: string; to?: string; compare?: { from: string; to: string }[]; signal?: AbortSignal } = {},
): Promise<OnlyCoinStats> {
  const query = new URLSearchParams({ board: 'y', business_date: businessDate, as_of: asOf });
  // 自定义起止必须成对提交；半填一律不发，交给调用方守门。
  if (options.from && options.to) { query.set('from', options.from); query.set('to', options.to); }
  for (const p of options.compare || []) if (p.from && p.to) query.append('compare', `${p.from}~${p.to}`);
  return request(`/review/onlycoin/stats?${query}`, {
    signal: options.signal ?? AbortSignal.timeout(ONLYCOIN_STATS_TIMEOUT_MS),
  });
}

export const fetchOnlyCoinSource = () => request<OnlyCoinSource>('/onlycoin/sources/screener-y/status');
/** Admin token exists only in caller component memory. Never persisted or put in URLs. */
export const controlOnlyCoinSource = (body: OnlyCoinControl, token: string) => request<OnlyCoinSource>('/onlycoin/sources/screener-y/control', {
  method: 'PUT', headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) }, body: JSON.stringify(body),
});
