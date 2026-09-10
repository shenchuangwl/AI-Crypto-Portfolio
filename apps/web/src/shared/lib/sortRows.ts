import type { CandidateRow, McapZone } from '../types/screener';

/** 五区 rank：DMR=0 最好。与后端 mcap_effective.ZONE_RANK 一致。 */
const MCAP_ZONE_RANK: Record<McapZone, number> = {
  DMR: 0,
  CONFIRMED: 1,
  QUALIFIED: 2,
  WATCH: 3,
  ELIMINATED: 4,
};
import { gradeRank, lastPrintOf, stayPnl, stayPriceOf } from './stayPnl';
import { mcapGradeOf, mcapGradeRank, timeframeOfMcapSortId } from './mcapGrade';

/**
 * 表格排序。`Array.prototype.sort` 在 ES2019 起保证稳定，入参 `rows` 保持后端
 * 下发的 rank 次序（Score 降序），因此同值行的次序天然回落到 Score —— 这对
 * 只有 7 个取值的周期流通市值三列尤其重要，不需要再手写次级键。
 */
export function sortRows(
  rows: CandidateRow[],
  direction: 'up' | 'down',
  sortId: string,
  sortDesc: boolean,
  quotes: Record<string, { last?: number | null; mark?: number | null }>,
) {
  const score = (r: CandidateRow) => (direction === 'up' ? r.score_up : r.score_down);
  const pnlOf = (r: CandidateRow) =>
    stayPnl(lastPrintOf(r, quotes[r.symbol]), stayPriceOf(r), direction);
  // 30m流通市值 / 2h流通市值 / 6h流通市值：按 A→F 的等级阶梯排序，判不出级记 0。
  const mcapTf = timeframeOfMcapSortId(sortId);
  const val = (r: CandidateRow) => {
    if (mcapTf) return mcapGradeRank(mcapGradeOf(r, mcapTf));
    switch (sortId) {
      case 'rank':
        return r.rank;
      case 'ret_1h':
        return r.ret_1h;
      case 'ret_4h':
        return r.ret_4h ?? Number.NEGATIVE_INFINITY;
      case 'ret_24h':
        return r.ret_24h;
      // 1Week / 1Month：算不出的行沉底（降序），与 4h / 锚点以来 同一口径
      case 'ret_1w':
        return r.ret_1w ?? Number.NEGATIVE_INFINITY;
      case 'ret_1mo':
        return r.ret_1mo ?? Number.NEGATIVE_INFINITY;
      case 'ret_anchor':
        return r.ret_since_anchor ?? Number.NEGATIVE_INFINITY;
      case 'dir_conf':
        return r.direction_confidence;
      case 'grade':
        return gradeRank(r.liquidity_grade);
      case 'pnl_pct':
        return pnlOf(r)?.pct ?? Number.NEGATIVE_INFINITY;
      case 'pnl_sign':
        return pnlOf(r)?.sign ?? Number.NEGATIVE_INFINITY;
      case 'liquidity':
        return r.liquidity_score;
      case 'ss':
        return r.staircase_score;
      // —— 216 天花板四列（y-v2.0.0-r3）——
      // 主导层关闭时这些字段不下发，一律沉底（降序），与 1Week/4h 同一口径。
      case 'mcap_combo':
        return r.mcap_combo_no ?? Number.NEGATIVE_INFINITY;
      case 'mcap_z':
        return r.mcap_z10 ?? Number.NEGATIVE_INFINITY;
      case 'mcap_k':
        return r.mcap_resonance_k ?? Number.NEGATIVE_INFINITY;
      case 'mcap_ceiling':
        // 天花板越好（rank 越小）排越前：降序时取负 rank。判不出的沉底。
        return r.mcap_ceiling_zone ? -MCAP_ZONE_RANK[r.mcap_ceiling_zone] : Number.NEGATIVE_INFINITY;
      default:
        return score(r);
    }
  };
  return [...rows].sort((a, b) => (sortDesc ? val(b) - val(a) : val(a) - val(b)));
}
