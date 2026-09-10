/**
 * 选币榜表格的单元格组件。只导出组件 —— 列定义在 `candidateColumns.tsx`。
 */
import type { CandidateRow, McapTimeframe } from '../../../shared/types/screener';
import { MCAP_GRADE_MEANING, MCAP_TIMEFRAME_LABEL } from '../../../shared/types/screener';
import { formatCompact, formatPct } from '../../../shared/lib/format';
import { formatPrice } from '../../../shared/lib/price';
import { mcapGradeOf } from '../../../shared/lib/mcapGrade';
import { lastPrintOf, stayPnl, stayPriceOf } from '../../../shared/lib/stayPnl';
import { useTickerStore } from '../../../shared/stores/tickerStore';

export function McapGradeCell({ row, tf }: { row: CandidateRow; tf: McapTimeframe }) {
  const grade = mcapGradeOf(row, tf);
  const d = row.mcap_tf?.[tf];
  if (!grade) {
    const why =
      d && d.bars < 26
        ? `${MCAP_TIMEFRAME_LABEL[tf]}：已收盘 K 线仅 ${d.bars} 根，不足 26 根，不判级`
        : `${MCAP_TIMEFRAME_LABEL[tf]}：6/12/26 根均线并列，无法落入 A–F 任一严格排列`;
    return (
      <span className="grade grade-UNCLASSIFIED" title={why}>
        —
      </span>
    );
  }
  const ma =
    d && d.ma6 != null && d.ma12 != null && d.ma26 != null
      ? ` · 6/12/26 日平均流通市值 ${formatCompact(d.ma6)}/${formatCompact(d.ma12)}/${formatCompact(
          d.ma26,
        )}`
      : ' · 流通供应量未知，仅发布等级（等级与已知供应量时完全一致）';
  return (
    <span
      className={`grade mcap-grade mcap-grade-${grade}`}
      title={`${MCAP_TIMEFRAME_LABEL[tf]} ${grade} — ${MCAP_GRADE_MEANING[grade]}${ma}`}
    >
      {grade}
    </span>
  );
}

export function LiveLast({ symbol, fallback }: { symbol: string; fallback?: number }) {
  const q = useTickerStore((s) => s.quotes[symbol]);
  const v = q?.last ?? q?.mark ?? fallback;
  if (v == null) return <span className="muted">—</span>;
  return <span className="live-last">{formatPrice(v)}</span>;
}

export function EnterPrice({ row }: { row: CandidateRow }) {
  if (row.state !== 'WATCH' && row.state !== 'QUALIFIED' && row.state !== 'CONFIRMED') {
    return <span className="muted">—</span>;
  }
  // 展示区进入价优先：用户看到的是哪个区，价就是进那个区时的首次印价。
  // 缺失回退 state_enter_price（主榜与主导层关闭时走这条路）。
  const v = row.zone_enter_price ?? row.state_enter_price;
  if (v == null || Number.isNaN(Number(v))) return <span className="muted">—</span>;
  return (
    <span className="enter-px" title="进入当前分区时的首次选入价（固定，不跟 Last*）">
      {formatPrice(v)}
    </span>
  );
}

export function Ret({ v }: { v: number | null | undefined }) {
  if (v == null || Number.isNaN(v)) return <span className="muted">—</span>;
  const cls = v > 0 ? 'up' : v < 0 ? 'down' : '';
  return <span className={`ret ${cls}`}>{formatPct(v)}</span>;
}

/** Stay-price PnL vs live Last*. Up: last/stay−1; down: 1−last/stay. */
function useStayPnl(row: CandidateRow, direction: 'up' | 'down') {
  const q = useTickerStore((s) => s.quotes[row.symbol]);
  return stayPnl(lastPrintOf(row, q), stayPriceOf(row), direction);
}

export function StayPnlPct({ row, direction }: { row: CandidateRow; direction: 'up' | 'down' }) {
  const pnl = useStayPnl(row, direction);
  if (!pnl) return <span className="muted">—</span>;
  const cls = pnl.sign > 0 ? 'up' : pnl.sign < 0 ? 'down' : '';
  return <span className={`ret ${cls}`}>{formatPct(pnl.pct)}</span>;
}

export function StayPnlSign({ row, direction }: { row: CandidateRow; direction: 'up' | 'down' }) {
  const pnl = useStayPnl(row, direction);
  if (!pnl) return <span className="muted">—</span>;
  const cls = pnl.sign > 0 ? 'up' : pnl.sign < 0 ? 'down' : '';
  return <span className={`ret ${cls}`}>{pnl.sign}</span>;
}
