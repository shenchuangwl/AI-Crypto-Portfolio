import { formatDwell, formatNum, formatPct } from '../../shared/lib/format';
import { formatEnterClock } from '../../shared/lib/format';
import type { ReviewSummary } from '../../shared/types/review';
import { fmtRate } from './ReviewSummaryCards';

export interface ComparePeriodResult {
  id: string;
  label: string;
  from?: string;
  to?: string;
  summary?: ReviewSummary;
  error?: string;
}

const ROWS: Array<{
  key: string;
  label: string;
  get: (s: ReviewSummary) => string;
  tone?: (s: ReviewSummary) => number | null | undefined;
}> = [
  { key: 'trades', label: '真实交易次数', get: (s) => String(s.trades) },
  { key: 'unique', label: '唯一币种', get: (s) => String(s.unique_coins) },
  { key: 'open', label: '未平仓', get: (s) => String(s.open_trades) },
  { key: 'wl', label: '盈 / 平 / 亏', get: (s) => `${s.win} / ${s.flat} / ${s.loss}` },
  { key: 'wr', label: '胜率', get: (s) => fmtRate(s.win_rate), tone: (s) => (s.win_rate == null ? null : s.win_rate - 0.5) },
  { key: 'sum', label: '合计盈亏%', get: (s) => formatPct(s.sum_pct), tone: (s) => s.sum_pct },
  { key: 'avg', label: '平均盈亏%', get: (s) => formatPct(s.avg_pct), tone: (s) => s.avg_pct },
  { key: 'med', label: '中位盈亏%', get: (s) => formatPct(s.median_pct), tone: (s) => s.median_pct },
  {
    key: 'pf',
    label: '盈亏比',
    get: (s) => (s.profit_factor == null ? '—' : formatNum(s.profit_factor, 2)),
    tone: (s) => (s.profit_factor == null ? null : s.profit_factor - 1),
  },
  { key: 'dd', label: '最大回撤', get: (s) => formatPct(s.max_drawdown_pct), tone: () => -1 },
  { key: 'dwell', label: '平均停留', get: (s) => formatDwell(s.avg_dwell_minutes) },
  { key: 'dir', label: '涨 / 跌 笔数', get: (s) => `${s.up} / ${s.down}` },
  {
    key: 'pv',
    label: '参数版本',
    get: (s) => {
      const vs = Object.keys(s.by_parameter_version || {});
      if (!vs.length) return '—';
      const short = vs.map((v) => v.replace('param-', '').split('-')[0]);
      return short.length > 1 ? `${short.join(' → ')} ⚠` : short[0];
    },
  },
];

/**
 * 多周期并列对比（§11.2，最多 4 列 = 主周期 + 3）。
 * 参数版本行是**必须的**：跨版本的周期对比结论不可直接采信。
 */
export function PeriodCompareTable({ periods }: { periods: ComparePeriodResult[] }) {
  const cols = periods.filter((p) => p.summary || p.error);
  if (cols.length < 2) return null;
  return (
    <div className="review-compare">
      <div className="review-compare-title">周期对比（{cols.length} 列）</div>
      <div className="grid-wrap static">
        <table className="review-compare-table">
          <thead>
            <tr>
              <th>指标</th>
              {cols.map((p) => (
                <th key={p.id} title={p.from && p.to ? `${p.from} → ${p.to}` : undefined}>
                  {p.label}
                  {p.from && p.to ? (
                    <div className="muted">
                      {formatEnterClock(p.from)} → {formatEnterClock(p.to)}
                    </div>
                  ) : null}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {ROWS.map((r) => (
              <tr key={r.key}>
                <td className="review-compare-label">{r.label}</td>
                {cols.map((p) => {
                  if (!p.summary) {
                    return (
                      <td key={p.id} className="muted">
                        {p.error ? '取数失败' : '—'}
                      </td>
                    );
                  }
                  const t = r.tone?.(p.summary);
                  const cls = t == null ? '' : t > 0 ? 'ret up' : t < 0 ? 'ret down' : '';
                  return (
                    <td key={p.id} className={cls}>
                      {r.get(p.summary)}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
