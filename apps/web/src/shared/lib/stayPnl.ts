import type { CandidateRow, Direction } from '../types/screener';

export type StayPnl = { pct: number; sign: 1 | 0 | -1 };

/** Stay-price PnL vs Last*. Up: last/stay−1; down: 1−last/stay. */
export function stayPnl(
  last: number | null | undefined,
  stay: number | null | undefined,
  direction: Direction,
): StayPnl | null {
  if (last == null || stay == null) return null;
  const L = Number(last);
  const S = Number(stay);
  if (!Number.isFinite(L) || !Number.isFinite(S) || S === 0) return null;
  const pct = direction === 'up' ? L / S - 1 : 1 - L / S;
  if (!Number.isFinite(pct)) return null;
  const sign: 1 | 0 | -1 = pct > 0 ? 1 : pct < 0 ? -1 : 0;
  return { pct, sign };
}

export function stayPriceOf(row: CandidateRow): number | null {
  if (row.state !== 'WATCH' && row.state !== 'QUALIFIED' && row.state !== 'CONFIRMED') {
    return null;
  }
  // Same denominator as the 停留价格 column: display-zone enter (zone_enter_*)
  // when the board stamped it (选币榜Y / 216 主导层), else state_enter_*.
  // v1.4.0 / 选币榜X never write zone_enter_price, so this is a no-op there.
  const v = row.zone_enter_price ?? row.state_enter_price;
  if (v == null || Number.isNaN(Number(v))) return null;
  return Number(v);
}

export function lastPrintOf(
  row: CandidateRow,
  quote?: { last?: number | null; mark?: number | null },
): number | null {
  const v = quote?.last ?? quote?.mark ?? row.last_price ?? row.ref_price;
  if (v == null || Number.isNaN(Number(v))) return null;
  return Number(v);
}

const GRADE_RANK: Record<string, number> = {
  A: 4,
  B: 3,
  C: 2,
  D: 1,
  UNCLASSIFIED: 0,
  INSUFFICIENT: 0,
};

export function gradeRank(grade: string | null | undefined): number {
  if (!grade) return 0;
  return GRADE_RANK[grade] ?? 0;
}
