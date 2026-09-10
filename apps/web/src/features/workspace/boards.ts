import type { CandidateRow, Direction, ScreenerSnapshot } from '../../shared/types/screener';
import { formatGrade } from '../../shared/lib/format';

export type BoardId =
  | 'dmr'
  | 'gainers'
  | 'losers'
  | 'hot'
  | 'confirmed'
  | 'qualified'
  | 'watch'
  | 'mcap'
  | 'stairs'
  | 'liq';

export interface BoardDef {
  id: BoardId;
  title: string;
  hint: string;
  direction?: Direction;
}

/** Visible rows per workspace mini-board (涨幅/跌幅/热度/确认/符合/观察/市值/楼梯/流动性). */
export const BOARD_LIMIT = 20;

export const BOARDS: BoardDef[] = [
  { id: 'gainers', title: '涨幅甄选', hint: '15m / 1h 涨幅 · 上涨池', direction: 'up' },
  { id: 'losers', title: '跌幅甄选', hint: '15m / 1h 跌幅 · 下跌池', direction: 'down' },
  { id: 'hot', title: '热度成交', hint: '6/12/26 日均成交额', direction: 'up' },
  { id: 'dmr', title: 'DMR区', hint: '精选 · 真实执行 inbox', direction: 'up' },
  { id: 'confirmed', title: '确认区', hint: '旋转楼梯硬标准 · 含DMR', direction: 'up' },
  { id: 'qualified', title: '符合区', hint: 'QUALIFIED · 含确认/DMR', direction: 'up' },
  { id: 'watch', title: '观察区', hint: 'WATCH 连续扫描', direction: 'up' },
  { id: 'mcap', title: '市值动量', hint: '门槛三 S_MC', direction: 'up' },
  { id: 'stairs', title: '旋转楼梯', hint: '门槛四 SS', direction: 'up' },
  { id: 'liq', title: '流动性 A/B', hint: 'S_L + 等级', direction: 'up' },
];

function scoreOf(r: CandidateRow, dir: Direction) {
  return dir === 'up' ? r.score_up : r.score_down;
}

function pickPool(snap: ScreenerSnapshot, dir: Direction) {
  return dir === 'up' ? snap.long_pool : snap.short_pool;
}

/** Rank a board from the scan snapshot. Display-only — never rewrite Score. */
export function rankBoard(snap: ScreenerSnapshot, id: BoardId, limit = BOARD_LIMIT): CandidateRow[] {
  const dir: Direction = BOARDS.find((b) => b.id === id)?.direction ?? 'up';
  let rows = [...pickPool(snap, dir)];

  switch (id) {
    case 'dmr':
      rows = [...snap.long_pool, ...snap.short_pool].filter(
        (r) => r.state === 'CONFIRMED' && r.dmr_selected,
      );
      rows.sort((a, b) => scoreOf(b, b.direction) - scoreOf(a, a.direction));
      break;
    case 'gainers':
      rows = rows.filter((r) => ['WATCH', 'QUALIFIED', 'CONFIRMED'].includes(r.state));
      rows.sort((a, b) => b.ret_1h - a.ret_1h || (b.ret_15m ?? 0) - (a.ret_15m ?? 0));
      break;
    case 'losers':
      rows = [...snap.short_pool].filter((r) =>
        ['WATCH', 'QUALIFIED', 'CONFIRMED'].includes(r.state),
      );
      rows.sort((a, b) => a.ret_1h - b.ret_1h || (a.ret_15m ?? 0) - (b.ret_15m ?? 0));
      break;
    case 'hot':
      rows.sort((a, b) => (b.aqv_6d_m || 0) - (a.aqv_6d_m || 0));
      break;
    case 'confirmed':
    case 'qualified': {
      // Cumulative display sets enforce DMR ⊂ CONFIRMED ⊂ QUALIFIED.
      const allowed = id === 'confirmed' ? ['CONFIRMED'] : ['QUALIFIED', 'CONFIRMED'];
      rows = [...snap.long_pool, ...snap.short_pool].filter((r) => allowed.includes(r.state));
      rows.sort((a, b) => scoreOf(b, b.direction) - scoreOf(a, a.direction));
      break;
    }
    case 'watch':
      rows = rows.filter((r) => r.state === 'WATCH');
      rows.sort((a, b) => scoreOf(b, dir) - scoreOf(a, dir));
      break;
    case 'mcap':
      rows = rows.filter((r) => ['WATCH', 'QUALIFIED', 'CONFIRMED'].includes(r.state));
      rows.sort((a, b) => b.mcap_momentum_score - a.mcap_momentum_score);
      break;
    case 'stairs':
      rows = rows.filter((r) => ['WATCH', 'QUALIFIED', 'CONFIRMED'].includes(r.state));
      rows.sort((a, b) => b.staircase_score - a.staircase_score);
      break;
    case 'liq':
      rows = rows.filter((r) => ['WATCH', 'QUALIFIED', 'CONFIRMED'].includes(r.state));
      rows.sort((a, b) => b.liquidity_score - a.liquidity_score);
      break;
    default:
      break;
  }
  return rows.slice(0, limit);
}

export function boardMetric(row: CandidateRow, id: BoardId): { label: string; value: string; cls?: string } {
  const pct = (v: number) => {
    const s = `${v >= 0 ? '+' : ''}${(v * 100).toFixed(2)}%`;
    return { label: id === 'losers' ? '1h' : '1h', value: s, cls: v > 0 ? 'up' : v < 0 ? 'down' : '' };
  };
  switch (id) {
    case 'gainers':
    case 'losers':
      return pct(row.ret_1h);
    case 'hot':
      return { label: '6d额', value: `${(row.aqv_6d_m || 0).toFixed(1)}M` };
    case 'dmr':
    case 'confirmed':
    case 'qualified':
    case 'watch':
      return {
        label: 'Score',
        value: (row.direction === 'down' ? row.score_down : row.score_up).toFixed(1),
      };
    case 'mcap':
      return { label: 'S_MC', value: row.mcap_momentum_score.toFixed(0) };
    case 'stairs':
      return { label: 'SS', value: row.staircase_score.toFixed(0) };
    case 'liq':
      return { label: formatGrade(row.liquidity_grade), value: row.liquidity_score.toFixed(0) };
    default:
      return { label: '', value: '—' };
  }
}
