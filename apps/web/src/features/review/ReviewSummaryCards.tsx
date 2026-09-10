import { formatDwell, formatNum, formatPct } from '../../shared/lib/format';
import type { ReviewSummary } from '../../shared/types/review';

export function fmtRate(v: number | null | undefined, digits = 1) {
  if (v == null || Number.isNaN(v)) return '—';
  return `${(v * 100).toFixed(digits)}%`;
}

function Cell({ label, value, tone, title }: { label: string; value: string; tone?: string; title?: string }) {
  return (
    <div className="review-stat" title={title}>
      <span className="review-stat-label">{label}</span>
      <span className={`review-stat-value${tone ? ` ${tone}` : ''}`}>{value}</span>
    </div>
  );
}

function toneOf(v: number | null | undefined) {
  if (v == null || Number.isNaN(v)) return undefined;
  return v > 0 ? 'up' : v < 0 ? 'down' : undefined;
}

export function SummaryCard({
  title,
  s,
  compact = false,
  badge,
}: {
  title: string;
  s?: ReviewSummary;
  compact?: boolean;
  badge?: string;
}) {
  if (!s) return null;
  return (
    <div className={`review-card${compact ? ' compact' : ''}`}>
      <div className="review-card-title">
        {title}
        {badge ? <i className="path-tag">{badge}</i> : null}
      </div>
      <div className="review-card-row">
        真实交易 <b>{s.trades}</b>
        <span className="muted">
          {' '}
          · 唯一币 {s.unique_coins} · 未平仓 {s.open_trades}
          {s.missing_px ? ` · 缺价 ${s.missing_px}` : ''}
        </span>
      </div>
      <div className="review-card-row">
        盈 {s.win} / 平 {s.flat} / 亏 {s.loss} · 胜率 <b>{fmtRate(s.win_rate)}</b>
        <span className="muted"> （分母 = 有效 {s.usable} 笔）</span>
      </div>
      <div className="review-card-row">
        合计 <b className={`ret ${toneOf(s.sum_pct) || ''}`}>{formatPct(s.sum_pct)}</b> · 平均{' '}
        <b className={`ret ${toneOf(s.avg_pct) || ''}`}>{formatPct(s.avg_pct)}</b> · 中位{' '}
        <span className={`ret ${toneOf(s.median_pct) || ''}`}>{formatPct(s.median_pct)}</span>
      </div>
      {!compact && (
        <div className="review-stat-grid">
          <Cell
            label="盈亏比"
            value={s.profit_factor == null ? '—' : formatNum(s.profit_factor, 2)}
            tone={s.profit_factor != null && s.profit_factor >= 1 ? 'up' : 'down'}
            title="Σ 正盈亏 / |Σ 负盈亏|；无亏损笔时为 —"
          />
          <Cell label="平均盈利" value={formatPct(s.avg_win_pct)} tone="up" />
          <Cell label="平均亏损" value={formatPct(s.avg_loss_pct)} tone="down" />
          <Cell label="最大单笔" value={formatPct(s.max_pct)} tone="up" />
          <Cell label="最大单亏" value={formatPct(s.min_pct)} tone="down" />
          <Cell
            label="最大回撤"
            value={formatPct(s.max_drawdown_pct)}
            tone="down"
            title="累计盈亏曲线的最大峰谷差"
          />
          <Cell label="平均停留" value={formatDwell(s.avg_dwell_minutes)} />
          <Cell label="中位停留" value={formatDwell(s.median_dwell_minutes)} />
          <Cell
            label="换手"
            value={s.turnover_per_coin_per_day == null ? '—' : `${formatNum(s.turnover_per_coin_per_day, 2)} 笔/币/日`}
            title="交易次数 / 唯一币 / 窗口天数——分区稳定性"
          />
          <Cell
            label="数据质量"
            value={fmtRate(s.data_quality)}
            tone={s.data_quality != null && s.data_quality < 0.95 ? 'down' : undefined}
            title="有效盈亏笔数 / 完整交易笔数"
          />
        </div>
      )}
    </div>
  );
}

/**
 * 对照组相对表现：确认/DMR 的均值减去淘汰等对照区的均值。
 * 若为负，说明这套选币标准还跑不赢「不选」。
 */
export function ControlBenchmarkCard({ s }: { s?: ReviewSummary }) {
  const cb = s?.control_benchmark;
  if (!cb || !cb.trades) return null;
  const edge = cb.edge_avg_pct;
  return (
    <div className="review-card control">
      <div className="review-card-title">
        对照组相对表现<i className="path-tag">对照</i>
      </div>
      <div className="review-card-row">
        当前选区平均 <b className={`ret ${toneOf(s?.avg_pct) || ''}`}>{formatPct(s?.avg_pct)}</b>
        <span className="muted"> · 对照区平均 {formatPct(cb.avg_pct)}（{cb.trades} 笔）</span>
      </div>
      <div className="review-card-row">
        超额 <b className={`ret ${toneOf(edge) || ''}`}>{formatPct(edge)}</b> · 胜率差{' '}
        <b className={`ret ${toneOf(cb.edge_win_rate) || ''}`}>
          {cb.edge_win_rate == null ? '—' : `${cb.edge_win_rate > 0 ? '+' : ''}${(cb.edge_win_rate * 100).toFixed(1)}pt`}
        </b>
      </div>
      <div className="review-card-row muted">{cb.note || '对照组盈亏是反向基准，不代表可执行策略'}</div>
    </div>
  );
}
