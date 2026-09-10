import type {
  CandidateRow,
  McapTfGrade,
  McapTimeframe,
} from '../types/screener';

/**
 * 周期流通市值等级的排序秩。
 *
 * A/B/C/D/E/F 本身就是一条从"流通市值增长最强"到"流通市值缩减最强"的单调阶梯，
 * 排序秩直接沿用这条阶梯，不重新定义、不改写等级含义：
 *
 *   A(6) 多头排列 → B(5) 多头轻度回调 → C(4) 多头重度回调
 *   → D(3) 空头重度回调 → E(2) 空头轻度回调 → F(1) 空头排列
 *
 * 判不出级（K 线不足 26 根 / 三条均线并列）秩为 0 —— 与既有「等级」列把
 * `UNCLASSIFIED` 记 0 的口径一致；它是"无数据"，不是第七个等级。
 */
export const MCAP_GRADE_RANK: Record<McapTfGrade, number> = {
  A: 6,
  B: 5,
  C: 4,
  D: 3,
  E: 2,
  F: 1,
};

/** 降序（默认）从上到下的排列：A → F → —。 */
export const MCAP_GRADE_DESC_ORDER: (McapTfGrade | null)[] = [
  'A',
  'B',
  'C',
  'D',
  'E',
  'F',
  null,
];

export function mcapGradeRank(g: McapTfGrade | null | undefined): number {
  if (!g) return 0;
  return MCAP_GRADE_RANK[g] ?? 0;
}

/** 取某一周期的等级。三列共用同一套定义，只是取数周期不同。 */
export function mcapGradeOf(
  row: CandidateRow,
  tf: McapTimeframe,
): McapTfGrade | null {
  if (tf === '30m') return row.mcap_grade_30m ?? null;
  if (tf === '2h') return row.mcap_grade_2h ?? null;
  return row.mcap_grade_6h ?? null;
}

/** 表头 sortKey ↔ 周期。两边只此一份映射，避免列与排序对不上。 */
export const MCAP_SORT_ID = {
  '30m': 'mcap_30m',
  '2h': 'mcap_2h',
  '6h': 'mcap_6h',
} as const satisfies Record<McapTimeframe, string>;

export type McapSortId = (typeof MCAP_SORT_ID)[McapTimeframe];

const SORT_ID_TO_TF = {
  mcap_30m: '30m',
  mcap_2h: '2h',
  mcap_6h: '6h',
} as const satisfies Record<McapSortId, McapTimeframe>;

/** sortId 是三列之一时返回其周期，否则 null。 */
export function timeframeOfMcapSortId(sortId: string): McapTimeframe | null {
  return (SORT_ID_TO_TF as Record<string, McapTimeframe>)[sortId] ?? null;
}
