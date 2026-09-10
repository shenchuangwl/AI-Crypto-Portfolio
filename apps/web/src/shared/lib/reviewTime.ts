/**
 * 复盘时间栅格助手。
 *
 * 存储 / API 一律 UTC ISO8601（`…Z`），展示与输入一律 **Asia/Bangkok (UTC+7)**，
 * 与选币榜 `formatEnterClock` 的「本地」保持同一时钟。
 *
 * `<input type="datetime-local">` 的 value 不带偏移量，`new Date(value)` 会按
 * **浏览器本地时区**解析——机器不在 +07 时就会静默偏移，而且受控输入回填后
 * 会逐次累积。这里显式按 +07 换算，结果与浏览器时区无关。
 */

/** 展示时区固定 +07:00，与 format.ts 的 `formatEnterClock` 默认值一致。 */
export const DISPLAY_TZ_OFFSET_MIN = 7 * 60;

/** 仅预填编辑器：越南当天 07:00 与此前 N 天 07:00，不替换快捷周期查询。 */
export function defaultReviewRange(days = 7, now = Date.now()): { from: string; to: string } {
  const local = new Date(now + DISPLAY_TZ_OFFSET_MIN * 60_000);
  const end = Date.UTC(local.getUTCFullYear(), local.getUTCMonth(), local.getUTCDate());
  return {
    from: new Date(end - days * 86_400_000).toISOString().replace(/\.\d{3}Z$/, 'Z'),
    to: new Date(end).toISOString().replace(/\.\d{3}Z$/, 'Z'),
  };
}

function pad(n: number, w = 2): string {
  return String(Math.abs(n)).padStart(w, '0');
}

/** UTC ISO → `<input type="datetime-local">` 需要的 `YYYY-MM-DDTHH:mm`（+07 墙钟）。 */
export function utcIsoToLocalInput(iso: string | null | undefined): string {
  if (!iso) return '';
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) return '';
  const d = new Date(ms + DISPLAY_TZ_OFFSET_MIN * 60_000);
  return (
    `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}` +
    `T${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`
  );
}

/** `<input type="datetime-local">` 的 +07 墙钟 → UTC ISO（秒精度，`Z` 结尾）。 */
export function localInputToUtcIso(value: string | null | undefined): string {
  if (!value) return '';
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(value);
  if (!m) return '';
  const asUtc = Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], 0);
  return new Date(asUtc - DISPLAY_TZ_OFFSET_MIN * 60_000).toISOString().replace(/\.\d{3}Z$/, 'Z');
}

/** 15 分钟节点栅格：向下吸附。窗口起点必须落在真实扫描节点上。 */
export function floorToNode(iso: string | null | undefined, minutes = 15): string {
  if (!iso) return '';
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) return '';
  const step = minutes * 60_000;
  return new Date(Math.floor(ms / step) * step).toISOString().replace(/\.\d{3}Z$/, 'Z');
}

/** 双时钟页头：`2026-08-24 12:15 (UTC+7) · 05:15 UTC`。 */
export function dualClock(iso: string | null | undefined): string {
  if (!iso) return '—';
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) return '—';
  const l = new Date(ms + DISPLAY_TZ_OFFSET_MIN * 60_000);
  const u = new Date(ms);
  return (
    `${l.getUTCFullYear()}-${pad(l.getUTCMonth() + 1)}-${pad(l.getUTCDate())} ` +
    `${pad(l.getUTCHours())}:${pad(l.getUTCMinutes())} (UTC+7) · ` +
    `${pad(u.getUTCHours())}:${pad(u.getUTCMinutes())} UTC`
  );
}

export function daysBetween(a?: string | null, b?: string | null): number | null {
  if (!a || !b) return null;
  const da = Date.parse(a);
  const db = Date.parse(b);
  if (Number.isNaN(da) || Number.isNaN(db)) return null;
  return Math.max(0, (db - da) / 86_400_000);
}
