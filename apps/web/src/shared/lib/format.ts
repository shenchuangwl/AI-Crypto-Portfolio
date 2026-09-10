import dayjs from 'dayjs';
import utc from 'dayjs/plugin/utc';
import timezone from 'dayjs/plugin/timezone';

dayjs.extend(utc);
dayjs.extend(timezone);

export function formatUtc(iso: string, pattern = 'YYYY-MM-DD HH:mm:ss [UTC]') {
  return dayjs.utc(iso).format(pattern);
}

export function formatLocal(iso: string, tz: string, pattern = 'MM-DD HH:mm') {
  return dayjs.utc(iso).tz(tz).format(pattern);
}

/** Scan cadence in minutes. Enter stamps are published on this UTC grid. */
export const SCAN_GRID_MINUTES = 15;
const SCAN_GRID_MS = SCAN_GRID_MINUTES * 60_000;

/** Floor a UTC instant onto the 15-minute scan grid (`:00/:15/:30/:45`). */
export function snapUtcToScanGrid(iso: string): Date | null {
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) return null;
  return new Date(Math.floor(ms / SCAN_GRID_MS) * SCAN_GRID_MS);
}

/**
 * First-print clock for 入选时间 (same TZ as header 本地 / Asia/Bangkok).
 *
 * Both 选币榜 and 选币榜Y use `M.D HH:mm` (e.g. `9.1 09:15`, `8.30 10:45`).
 * The clock half is always on the 15-minute scan grid (`:00/:15/:30/:45`).
 * Off-cadence stamps (`09:17:44`) floor to the node they belong to (`09:15`).
 */
export function formatEnterClock(
  iso: string | null | undefined,
  tz = 'Asia/Bangkok',
): string {
  if (!iso) return '—';
  const snapped = snapUtcToScanGrid(iso);
  if (!snapped) return '—';
  const d = dayjs.utc(snapped.getTime()).tz(tz);
  if (!d.isValid()) return '—';
  return `${d.month() + 1}.${d.date()} ${d.format('HH:mm')}`;
}

export function formatPct(v: number | null | undefined, digits = 2) {
  if (v == null || Number.isNaN(v)) return '—';
  const sign = v > 0 ? '+' : '';
  return `${sign}${(v * 100).toFixed(digits)}%`;
}

/** SM dwell in the *current* state. <1 scan (~15m) = just entered. */
export function formatDwell(minutes: number | null | undefined): string {
  if (minutes == null || Number.isNaN(minutes) || minutes < 0) return '—';
  const m = Math.round(minutes);
  if (m < 8) return '刚进';
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  const rem = m % 60;
  return rem ? `${h}h${rem}m` : `${h}h`;
}

export function formatGrade(grade: string | null | undefined): string {
  if (!grade || grade === 'UNCLASSIFIED' || grade === 'INSUFFICIENT') return '未分级';
  return grade;
}

export function formatNum(v: number | null | undefined, digits = 1) {
  if (v == null || Number.isNaN(v)) return '—';
  return v.toFixed(digits);
}

export function formatCompact(v: number | null | undefined) {
  if (v == null || Number.isNaN(v)) return '—';
  return new Intl.NumberFormat('en', { notation: 'compact', maximumFractionDigits: 2 }).format(v);
}

export function hoursSinceAnchor(anchorDate: string, scanIso: string) {
  const a = Date.parse(`${anchorDate}T00:00:00Z`);
  const s = Date.parse(scanIso);
  return Math.max(0, (s - a) / 3_600_000);
}

export function nextScanEtaSec(fromMs = Date.now()) {
  // next 15-min UTC boundary
  const d = new Date(fromMs);
  const mins = d.getUTCMinutes();
  const nextMin = Math.ceil((mins + 0.001) / 15) * 15;
  const target = new Date(d);
  if (nextMin >= 60) {
    target.setUTCHours(d.getUTCHours() + 1, 0, 0, 0);
  } else {
    target.setUTCMinutes(nextMin, 0, 0);
  }
  return Math.max(0, Math.floor((target.getTime() - fromMs) / 1000));
}

export function formatEta(sec: number) {
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}
