import { DISPLAY_TZ_OFFSET_MIN } from './reviewTime';
type Cycle = { board_key: string; business_date: string; cycle_start_utc: string; cycle_end_utc: string; server_time: string };
export function onlyCoinLocalTime(input: string): string {
  const localDay = input.slice(0, 10);
  const validated = onlyCoinReplayTime(localDay, input);
  return new Date(Date.parse(validated) - DISPLAY_TZ_OFFSET_MIN * 60000).toISOString().replace('.000Z', 'Z');
}
export function onlyCoinCycleEndInput(day: string): string {
  const start = Date.parse(onlyCoinReplayTime(day, `${day}T00:00`));
  return new Date(start + (24 * 60 + DISPLAY_TZ_OFFSET_MIN) * 60000 - 1000).toISOString().slice(0, 19);
}
/** datetime-local may omit zero seconds; interpret the explicitly UTC control without local conversion. */
export function onlyCoinReplayTime(day: string, input: string): string {
  const match = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})(?::(\d{2}))?$/.exec(input);
  if (!match) throw new Error('无效的截至时间，请选择完整日期与时间。');
  const result = `${match[1]}T${match[2]}:${match[3] || '00'}Z`;
  const ms = Date.parse(result);
  if (!Number.isFinite(ms) || new Date(ms).toISOString().slice(0, 19) !== result.slice(0, 19)) throw new Error('无效的日期或时间。');
  if (match[1] !== day) throw new Error('截至时间须与左侧业务日属于同一 UTC 日期；这里不是起止区间。');
  return result;
}
/** Anchor to monotonic elapsed time; browser timezone and clock edits cannot extend a day. */
export function dailyExpiry(c: Cycle, receivedAt: number): number {
  const remaining = Date.parse(c.cycle_end_utc) - Date.parse(c.server_time);
  return receivedAt + (Number.isFinite(remaining) ? Math.max(0, remaining) : 0);
}
export function isDailyCurrent(c: Cycle, expiresAt: number, now: number): boolean {
  return c.board_key === 'y' && c.business_date === c.cycle_start_utc.slice(0, 10)
    && Date.parse(c.server_time) >= Date.parse(c.cycle_start_utc) && now < expiresAt;
}
export function matchingConfirmed<T extends { scan_id?: string }>(feed: T | null, scanId: string): T | null {
  return feed?.scan_id === scanId ? feed : null;
}
