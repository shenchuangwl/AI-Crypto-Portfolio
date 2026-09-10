import type { CandidateRow, ScreenerSnapshot, ScreenerState } from '../../shared/types/screener';
import { formatDwell, formatGrade, formatPct } from '../../shared/lib/format';

export interface FunnelCounts {
  universe: number;
  scanned: number;
  g1: number;
  g3: number;
  watch: number;
  qualified: number;
  confirmed: number;
  dmr: number;
  eliminated: number;
  insufficient: number;
}

export function uniqueBySymbol(rows: CandidateRow[]): CandidateRow[] {
  const seen = new Set<string>();
  const out: CandidateRow[] = [];
  for (const r of rows) {
    if (seen.has(r.symbol)) continue;
    seen.add(r.symbol);
    out.push(r);
  }
  return out;
}

export function allRows(snap: ScreenerSnapshot): CandidateRow[] {
  return [...snap.long_pool, ...snap.short_pool];
}

export function funnelCounts(snap: ScreenerSnapshot): FunnelCounts {
  const rows = uniqueBySymbol(allRows(snap));
  const byState = (s: ScreenerState) => rows.filter((r) => r.state === s).length;
  const g1 = rows.filter((r) => {
    const codes = r.reason_codes || [];
    return (
      codes.some((c) => /G1/i.test(c) && !/FAIL|MISS/i.test(c)) ||
      r.liquidity_score >= 45 ||
      r.state !== 'DATA_INSUFFICIENT'
    );
  }).length;
  const g3 = rows.filter((r) => r.circulating_supply != null && r.circulating_supply > 0).length;
  return {
    universe: snap.meta.effective_universe || rows.length,
    scanned: rows.length,
    g1,
    g3,
    watch: snap.meta.state_counts.WATCH ?? byState('WATCH'),
    // Cumulative levels: DMR ⊂ CONFIRMED ⊂ QUALIFIED.
    qualified:
      (snap.meta.state_counts.QUALIFIED ?? byState('QUALIFIED')) +
      (snap.meta.state_counts.CONFIRMED ?? byState('CONFIRMED')),
    confirmed: snap.meta.state_counts.CONFIRMED ?? byState('CONFIRMED'),
    dmr: rows.filter((r) => r.state === 'CONFIRMED' && r.dmr_selected).length,
    eliminated: snap.meta.state_counts.ELIMINATED ?? byState('ELIMINATED'),
    insufficient: snap.meta.state_counts.DATA_INSUFFICIENT ?? byState('DATA_INSUFFICIENT'),
  };
}

export type GateTone = 'pass' | 'hold' | 'fail' | 'wait';

export interface GateStep {
  id: string;
  title: string;
  tone: GateTone;
  detail: string;
}

function hasCode(row: CandidateRow, re: RegExp) {
  return (row.reason_codes || []).some((c) => re.test(c));
}

/** Display-only reconstruction of the selection path. Does not recompute gates. */
export function selectionSteps(row: CandidateRow): GateStep[] {
  const g1Fail = hasCode(row, /G1.*(FAIL|MISS)|LIQ_FAIL/i);
  const g1Pass = hasCode(row, /G1_PASS|G1D|G1OK/i) || (!g1Fail && row.liquidity_score >= 45);
  const g3Pass =
    hasCode(row, /G3OK|G3_PASS/i) ||
    (row.circulating_supply != null && row.circulating_supply > 0);
  const g2Pass = row.momentum_score >= 45;
  const g4Pass = row.staircase_score >= 28;

  let sm: GateStep;
  if (row.state === 'CONFIRMED') {
    sm = { id: 'sm', title: '状态机', tone: 'pass', detail: `CONFIRMED · 停留 ${formatDwell(row.state_duration_minutes)}` };
  } else if (row.state === 'QUALIFIED') {
    sm = {
      id: 'sm',
      title: '状态机',
      tone: 'hold',
      detail: `QUALIFIED${row.qualified_path ? ` · PATH_${row.qualified_path}` : ''}${row.ready_confirm ? ' · READY_CONFIRM' : ''} · ${(row.not_confirmed_reasons || ['dwell']).join(' · ')}`,
    };
  } else if (row.state === 'WATCH') {
    sm = {
      id: 'sm',
      title: '状态机',
      tone: 'hold',
      detail: `WATCH · 连续扫描中 · ${formatDwell(row.state_duration_minutes)}`,
    };
  } else if (row.state === 'ELIMINATED') {
    sm = { id: 'sm', title: '状态机', tone: 'fail', detail: 'ELIMINATED · 未过门槛或跌出池' };
  } else if (row.state === 'DATA_INSUFFICIENT') {
    sm = { id: 'sm', title: '状态机', tone: 'fail', detail: 'DATA_INSUFFICIENT · 缺流通量/K线' };
  } else {
    sm = { id: 'sm', title: '状态机', tone: 'wait', detail: row.state };
  }

  return [
    { id: 'uni', title: '宇宙入场', tone: 'pass', detail: `${row.symbol} · USDT-M PERP · ${row.underlying_asset}` },
    {
      id: 'g1',
      title: 'G1 成交额 / 流动性',
      tone: g1Pass ? 'pass' : g1Fail ? 'fail' : 'hold',
      detail: `S_L ${row.liquidity_score.toFixed(0)} · 等级 ${formatGrade(row.liquidity_grade)} · 6d ${row.aqv_6d_m.toFixed(1)}M`,
    },
    {
      id: 'g2',
      title: 'G2 价格动量',
      tone: g2Pass ? 'pass' : 'hold',
      detail: `M ${row.momentum_score.toFixed(0)} · 15m ${formatPct(row.ret_15m)} · 1h ${formatPct(row.ret_1h)}`,
    },
    {
      id: 'g3',
      title: 'G3 流通市值',
      tone: g3Pass ? 'pass' : 'fail',
      detail: g3Pass
        ? `S_MC ${row.mcap_momentum_score.toFixed(0)} · 流通 ${row.circulating_supply ?? '—'} · 源 ${row.supply_source}`
        : '流通量缺失 · 不得进确认区',
    },
    {
      id: 'g4',
      title: 'G4 旋转楼梯',
      tone: g4Pass ? 'pass' : 'hold',
      detail: `SS ${row.staircase_score.toFixed(0)} · 一致性 ${row.consistency_score.toFixed(0)} · 排名速 ${row.rank_velocity_score.toFixed(0)}`,
    },
    sm,
  ];
}
