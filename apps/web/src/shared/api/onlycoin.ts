import type { OnlyCoinDaily, OnlyCoinLive, OnlyCoinSource, OnlyCoinControl } from '../types/onlycoin';
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
export const fetchOnlyCoinSource = () => request<OnlyCoinSource>('/onlycoin/sources/screener-y/status');
/** Admin token exists only in caller component memory. Never persisted or put in URLs. */
export const controlOnlyCoinSource = (body: OnlyCoinControl, token: string) => request<OnlyCoinSource>('/onlycoin/sources/screener-y/control', {
  method: 'PUT', headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) }, body: JSON.stringify(body),
});
