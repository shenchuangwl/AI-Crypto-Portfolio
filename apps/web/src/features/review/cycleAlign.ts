/**
 * 自定义 / 对比周期的「对齐到周期栅格」小工具。
 *
 * 预设窗口（近 N 周期）由**服务端**按 `CycleConfig` 算，前端不掺和。
 * 但用户手填的起止是自由的，可能从半夜切开两个周期 —— 那正是这次要修的毛病。
 * 所以这里只做两件事：判断一个时刻在不在栅格上，以及把它吸附到最近的边界。
 *
 * 栅格不是前端自己定义的：它锚在服务端给的 `coverage.cycle.current_start_utc`
 * 与 `period_hours` 上，所以永远和 `CycleConfig` 是同一张栅格，不会各算各的。
 */
import type { ReviewCycleCoverage } from '../../shared/types/review';

function gridMs(cyc: ReviewCycleCoverage): { anchorMs: number; periodMs: number } | null {
  const anchorMs = cyc.current_start_utc ? Date.parse(cyc.current_start_utc) : NaN;
  const periodMs = (cyc.period_hours ?? 24) * 3600_000;
  if (!Number.isFinite(anchorMs) || !(periodMs > 0)) return null;
  return { anchorMs, periodMs };
}

function toIso(ms: number): string {
  return new Date(ms).toISOString().replace(/\.\d{3}Z$/, 'Z');
}

/** `iso` 所属周期的起点。取不到栅格就原样返回。 */
export function cycleStartOf(iso: string, cyc?: ReviewCycleCoverage): string {
  const g = cyc && gridMs(cyc);
  const t = Date.parse(iso);
  if (!g || !Number.isFinite(t)) return iso;
  const n = Math.floor((t - g.anchorMs) / g.periodMs);
  return toIso(g.anchorMs + n * g.periodMs);
}

/** `iso` 所属周期的终点（= 下一个周期的起点）。 */
export function cycleEndOf(iso: string, cyc?: ReviewCycleCoverage): string {
  const g = cyc && gridMs(cyc);
  const t = Date.parse(iso);
  if (!g || !Number.isFinite(t)) return iso;
  const n = Math.floor((t - g.anchorMs) / g.periodMs);
  return toIso(g.anchorMs + (n + 1) * g.periodMs);
}

/** 这个时刻正好压在周期边界上吗。 */
export function isAligned(iso: string, cyc?: ReviewCycleCoverage): boolean {
  const g = cyc && gridMs(cyc);
  const t = Date.parse(iso);
  if (!g || !Number.isFinite(t)) return true;
  return (((t - g.anchorMs) % g.periodMs) + g.periodMs) % g.periodMs === 0;
}

/**
 * 把一段自定义窗口吸附到周期栅格：起点向下取整到所属周期的开头，
 * 终点向上取整到所属周期的结尾。这样窗口一定由若干个**完整周期**组成。
 */
export function snapWindow(
  from: string,
  to: string,
  cyc?: ReviewCycleCoverage,
): { from: string; to: string } {
  if (!from || !to || !cyc?.enabled) return { from, to };
  return { from: cycleStartOf(from, cyc), to: cycleEndOf(to, cyc) };
}

/** 这段窗口是否已经落在栅格上（两端都对齐）。 */
export function windowAligned(
  from: string,
  to: string,
  cyc?: ReviewCycleCoverage,
): boolean {
  if (!from || !to || !cyc?.enabled) return true;
  return isAligned(from, cyc) && isAligned(to, cyc);
}
