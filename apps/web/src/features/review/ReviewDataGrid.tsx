import type { ReactNode } from 'react';
import { useMemo, useRef } from 'react';
import { useVirtualizer } from '@tanstack/react-virtual';
import { useNavigate } from 'react-router-dom';
import type { ReviewSortId, ReviewTrade, ReviewZone } from '../../shared/types/review';
import { REVIEW_FLAG_LABEL, REVIEW_ZONE_LABEL } from '../../shared/types/review';
import { formatDwell, formatEnterClock, formatNum, formatPct } from '../../shared/lib/format';
import { formatPrice } from '../../shared/lib/price';
import { stayPnl } from '../../shared/lib/stayPnl';
import { useTickerStore } from '../../shared/stores/tickerStore';
import { useReviewUi } from '../../shared/stores/reviewUi';
import type { ReviewColumnPreset } from '../../shared/stores/reviewUi';

type Col = {
  id: string;
  label: string;
  width: number;
  /** Only ids the ledger can ORDER BY are clickable; the rest must not pretend. */
  sortId?: ReviewSortId;
  render: (t: ReviewTrade) => ReactNode;
};

function Ret({ v }: { v: number | null | undefined }) {
  if (v == null || Number.isNaN(v)) return <span className="muted">—</span>;
  const cls = v > 0 ? 'up' : v < 0 ? 'down' : '';
  return <span className={`ret ${cls}`}>{formatPct(v)}</span>;
}

function Px({ v }: { v: number | null | undefined }) {
  if (v == null) return <span className="muted">—</span>;
  return <span>{formatPrice(v)}</span>;
}

function Live({ symbol }: { symbol: string }) {
  const q = useTickerStore((s) => s.quotes[symbol]);
  const v = q?.last ?? q?.mark;
  if (v == null) return <span className="muted">—</span>;
  return <span className="live-last">{formatPrice(v)}</span>;
}

/**
 * 未平仓行的**浮动**参考盈亏（实时价 vs 停留价）。
 * 与选币榜的 `stayPnl` 同一函数，语义也一样是「未实现」——
 * 已平仓行一律不显示，避免与账本里的已实现盈亏混淆。
 */
function FloatingPnl({ t }: { t: ReviewTrade }) {
  const q = useTickerStore((s) => s.quotes[t.symbol]);
  if (t.status !== 'OPEN') return <span className="muted">—</span>;
  const v = stayPnl(q?.last ?? q?.mark ?? null, t.enter_price, t.direction);
  if (!v) return <span className="muted">—</span>;
  const cls = v.pct > 0 ? 'up' : v.pct < 0 ? 'down' : '';
  return (
    <span className={`ret dim ${cls}`} title="未实现浮动盈亏（实时价 vs 停留价），不进任何汇总">
      {formatPct(v.pct)}
    </span>
  );
}

function Flags({ t }: { t: ReviewTrade }) {
  const flags = t.flags || [];
  if (!flags.length) return <span className="muted">—</span>;
  return (
    <span className="risk-flags" title={flags.map((f) => REVIEW_FLAG_LABEL[f] || f).join(' · ')}>
      {flags.map((f) => (REVIEW_FLAG_LABEL[f] || f).slice(0, 2)).join(' ')}
    </span>
  );
}

/** 列预设（§12.2）：精简 / 交易员 / 完整。完整 = 需求 25 列 + 旗标。 */
export const COLUMN_PRESETS: Record<ReviewColumnPreset, string[] | null> = {
  compact: [
    'n', 'symbol', 'zone', 'enter_time_utc', 'exit_time_utc', 'dwell_minutes',
    'enter_price', 'exit_price', 'live', 'pnl_pct', 'pnl_sign', 'flags', 'reasons',
  ],
  trader: [
    'n', 'symbol', 'zone', 'enter_time_utc', 'exit_time_utc', 'dwell_minutes',
    'enter_price', 'exit_price', 'live', 'pnl_pct', 'pnl_sign',
    'score', 'dir_conf', 'grade', 'ret_1h', 'ret_4h', 'ret_24h', 'flags', 'reasons',
  ],
  full: null,
};

export function reviewColumns(onPickSymbol?: (symbol: string) => void): Col[] {
  return [
    { id: 'n', label: '#', width: 44, render: () => '' },
    {
      id: 'symbol',
      label: '合约',
      width: 120,
      sortId: 'symbol',
      render: (t) => (
        <span
          className="sym linkish"
          title="点击合约名 = 只看这个币（不跳走）"
          onClick={(e) => {
            if (!onPickSymbol) return;
            e.stopPropagation();
            onPickSymbol(t.symbol);
          }}
        >
          <b>{t.symbol}</b>
          {t.contract_multiplier > 1 && <i>×{t.contract_multiplier}</i>}
        </span>
      ),
    },
    {
      id: 'zone',
      label: '状态',
      width: 96,
      sortId: 'zone',
      render: (t) => (
        <span className={`state-badge state-${t.zone === 'DMR' ? 'dmr' : t.zone.toLowerCase()}`}>
          {REVIEW_ZONE_LABEL[t.zone as ReviewZone] ?? t.zone}
          {t.confirmed_path || t.qualified_path ? (
            <i className="path-tag"> {t.confirmed_path || t.qualified_path}</i>
          ) : null}
          {t.status === 'OPEN' ? <i className="path-tag"> 未平</i> : null}
        </span>
      ),
    },
    {
      id: 'enter_time_utc',
      label: '入选时间',
      width: 88,
      sortId: 'enter_time_utc',
      render: (t) => (
        <span title={`${t.enter_scan_id} · ${t.enter_time_utc}`}>
          {t.flags?.includes('TRUNCATED_ENTER') ? '‹' : ''}
          {formatEnterClock(t.enter_time_utc)}
        </span>
      ),
    },
    {
      id: 'dwell_minutes',
      label: '停留时间',
      width: 88,
      sortId: 'dwell_minutes',
      render: (t) => (
        <span title={`${t.dwell_nodes} 节点 / ${Number(t.dwell_minutes || 0).toFixed(0)} 分钟`}>
          {formatDwell(t.dwell_minutes)}
        </span>
      ),
    },
    {
      id: 'enter_price',
      label: '停留价格',
      width: 88,
      sortId: 'enter_price',
      render: (t) => (
        <span title={t.enter_price_source ? `来源 ${t.enter_price_source}` : undefined}>
          <Px v={t.enter_price} />
        </span>
      ),
    },
    { id: 'live', label: '实时价格', width: 88, render: (t) => <Live symbol={t.symbol} /> },
    {
      id: 'exit_time_utc',
      label: '退出时间',
      width: 88,
      sortId: 'exit_time_utc',
      render: (t) =>
        t.exit_time_utc ? (
          <span title={`${t.exit_scan_id} · ${t.exit_time_utc}`}>{formatEnterClock(t.exit_time_utc)}</span>
        ) : (
          <span className="muted">—</span>
        ),
    },
    {
      id: 'exit_price',
      label: '退出价格',
      width: 88,
      sortId: 'exit_price',
      render: (t) => (
        <span title={t.exit_price_source ? `来源 ${t.exit_price_source}` : undefined}>
          <Px v={t.exit_price} />
        </span>
      ),
    },
    {
      id: 'score',
      label: 'Score',
      width: 72,
      sortId: 'score',
      render: (t) => (
        <span className="score-cell" title="入选时冻结值">
          <b>{formatNum(t.score, 1)}</b>
          <small>{formatNum(t.score_opposite, 1)}</small>
        </span>
      ),
    },
    {
      id: 'dir_conf',
      label: 'DirConf',
      width: 72,
      sortId: 'direction_confidence',
      render: (t) => formatNum(t.direction_confidence, 2),
    },
    { id: 'grade', label: '等级', width: 64, render: (t) => t.liquidity_grade || '—' },
    { id: 'mcap_30m', label: '30m流通市值', width: 110, render: (t) => t.mcap_grade_30m || <span className="muted">—</span> },
    { id: 'mcap_2h', label: '2h流通市值', width: 110, render: (t) => t.mcap_grade_2h || <span className="muted">—</span> },
    { id: 'mcap_6h', label: '6h流通市值', width: 110, render: (t) => t.mcap_grade_6h || <span className="muted">—</span> },
    { id: 'ret_1h', label: '1h', width: 76, render: (t) => <Ret v={t.ret_1h} /> },
    { id: 'ret_4h', label: '4h', width: 76, render: (t) => <Ret v={t.ret_4h} /> },
    { id: 'ret_24h', label: '24h', width: 76, render: (t) => <Ret v={t.ret_24h} /> },
    { id: 'ret_1w', label: '1Week', width: 76, render: (t) => <Ret v={t.ret_1w} /> },
    { id: 'ret_1mo', label: '1Month', width: 76, render: (t) => <Ret v={t.ret_1mo} /> },
    { id: 'ret_anchor', label: '锚点以来', width: 88, render: (t) => <Ret v={t.ret_since_anchor} /> },
    {
      id: 'pnl_pct',
      label: '盈亏百分比',
      width: 88,
      sortId: 'pnl_pct',
      render: (t) => <Ret v={t.pnl_pct} />,
    },
    {
      id: 'pnl_sign',
      label: '盈亏数值',
      width: 72,
      sortId: 'pnl_sign',
      render: (t) => {
        if (t.pnl_sign == null) return <span className="muted">—</span>;
        const cls = t.pnl_sign > 0 ? 'up' : t.pnl_sign < 0 ? 'down' : '';
        return <span className={`ret ${cls}`}>{t.pnl_sign}</span>;
      },
    },
    { id: 'float_pnl', label: '浮动盈亏', width: 88, render: (t) => <FloatingPnl t={t} /> },
    {
      id: 'risk',
      label: '风险',
      width: 80,
      render: (t) =>
        t.risk_flags?.length ? (
          <span className="risk-flags">{t.risk_flags.join(' ')}</span>
        ) : (
          <span className="muted">—</span>
        ),
    },
    { id: 'flags', label: '旗标', width: 72, render: (t) => <Flags t={t} /> },
    {
      id: 'reasons',
      label: '入选原因',
      width: 160,
      render: (t) => (
        <span className="tags" title={(t.reason_codes || []).join(' · ')}>
          {(t.reason_codes || []).slice(0, 3).join(' · ') || '—'}
        </span>
      ),
    },
  ];
}

export function ReviewDataGrid({
  rows,
  columnPreset = 'full',
  onPickSymbol,
  height,
}: {
  rows: ReviewTrade[];
  columnPreset?: ReviewColumnPreset;
  onPickSymbol?: (symbol: string) => void;
  height?: number;
}) {
  const parentRef = useRef<HTMLDivElement>(null);
  const navigate = useNavigate();
  const { sortId, sortDesc, setSort } = useReviewUi();
  const cols = useMemo(() => {
    const all = reviewColumns(onPickSymbol);
    const keep = COLUMN_PRESETS[columnPreset];
    return keep ? all.filter((c) => keep.includes(c.id)) : all;
  }, [columnPreset, onPickSymbol]);
  const rowVirtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => 40,
    overscan: 12,
  });
  const totalWidth = cols.reduce((a, c) => a + c.width, 0);

  return (
    <div className="grid-wrap" ref={parentRef} style={height ? { maxHeight: height } : undefined}>
      <div className="grid-header" style={{ width: totalWidth }}>
        {cols.map((c) => {
          const active = c.sortId && sortId === c.sortId;
          return (
            <div
              key={c.id}
              className={`th ${c.sortId ? 'sortable' : ''} ${active ? 'sorted' : ''}`}
              style={{ width: c.width }}
              title={c.sortId ? '点击按该列服务端排序（整窗口，不只是当前页）' : '该列不参与排序'}
              onClick={() => c.sortId && setSort(c.sortId)}
            >
              {c.label}
              {active ? (sortDesc ? ' ↓' : ' ↑') : ''}
            </div>
          );
        })}
      </div>
      <div className="grid-body" style={{ height: rowVirtualizer.getTotalSize(), width: totalWidth }}>
        {rowVirtualizer.getVirtualItems().map((vr) => {
          const t = rows[vr.index];
          return (
            <div
              key={t.trade_id}
              className={`grid-row state-row-${t.zone === 'DMR' ? 'confirmed' : t.zone.toLowerCase()}${
                t.status === 'OPEN' ? ' review-open' : ''
              }${t.flags?.length ? ' review-flagged' : ''}`}
              style={{
                height: vr.size,
                transform: `translateY(${vr.start}px)`,
                width: totalWidth,
              }}
              onClick={() => navigate(`/market/${t.symbol}?direction=${t.direction}&from=review`)}
              title={t.trade_id}
            >
              {cols.map((c) => (
                <div key={c.id} className="td" style={{ width: c.width }}>
                  {c.id === 'n' ? vr.index + 1 : c.render(t)}
                </div>
              ))}
            </div>
          );
        })}
      </div>
      {rows.length === 0 && <div className="empty">该交叉条件下 0 笔完整交易</div>}
    </div>
  );
}
