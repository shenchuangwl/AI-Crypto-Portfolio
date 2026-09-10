import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import type { CandidateRow, OhlcvBar, SymbolScreenerDetail } from '../../shared/types/screener';
import { STATE_LABEL } from '../../shared/types/screener';
import { fetchKlines, fetchSymbolDetail } from '../../shared/api/screener';
import { formatCompact, formatPct } from '../../shared/lib/format';
import { formatPrice, resolvePricePrecision } from '../../shared/lib/price';
import { useTickerStore } from '../../shared/stores/tickerStore';
import { ChartWorkspace } from '../chart/ChartWorkspace';
import { selectionSteps } from './funnel';

interface Props {
  row: CandidateRow | null;
}

export function SelectionInspector({ row }: Props) {
  const last = useTickerStore((s) => (row ? s.quotes[row.symbol] : undefined));
  const [detail, setDetail] = useState<SymbolScreenerDetail | null>(null);
  const [bars, setBars] = useState<OhlcvBar[]>([]);
  const [src, setSrc] = useState('');

  useEffect(() => {
    if (!row) {
      setDetail(null);
      setBars([]);
      return;
    }
    let alive = true;
    Promise.all([
      fetchSymbolDetail(row.symbol, row.direction),
      fetchKlines(row.symbol, '15m', 80),
    ]).then(([d, k]) => {
      if (!alive) return;
      setDetail(d);
      setBars(k.bars);
      setSrc(k.source);
    });
    return () => {
      alive = false;
    };
  }, [row?.symbol, row?.direction]);

  if (!row) {
    return (
      <aside className="sel-inspect empty">
        <header className="sel-col-head">
          <b>甄选过程</b>
        </header>
        <p className="inspect-placeholder">点九榜里的一只币，看 G1→G4→状态机 怎么筛下来。</p>
      </aside>
    );
  }

  const steps = selectionSteps(row);
  const px = last?.last ?? last?.mark ?? row.last_price ?? row.ref_price;
  const score = row.direction === 'down' ? row.score_down : row.score_up;
  const path = detail?.path || [];

  return (
    <aside className="sel-inspect">
      <header className="sel-col-head">
        <b>甄选过程</b>
        <Link to={`/market/${row.symbol}?direction=${row.direction}&from=terminal`}>K 线页</Link>
      </header>
      <div className="inspect-sym">
        <div>
          <h2>{row.symbol}</h2>
          <span className={`st st-${row.state.toLowerCase()}`}>{STATE_LABEL[row.state]}</span>
          <span className="dir">{row.direction === 'up' ? '多' : '空'}</span>
        </div>
        <div className="inspect-px">
          <span className="px">{formatPrice(px)}</span>
          <span className="muted">Last* 隔离报价 · 不改 Score</span>
        </div>
      </div>
      <dl className="inspect-kpis">
        <div>
          <dt>Score</dt>
          <dd>{score.toFixed(1)}</dd>
        </div>
        <div>
          <dt>DirConf</dt>
          <dd>{row.direction_confidence.toFixed(2)}</dd>
        </div>
        <div>
          <dt>1h</dt>
          <dd className={row.ret_1h > 0 ? 'up' : row.ret_1h < 0 ? 'down' : ''}>{formatPct(row.ret_1h)}</dd>
        </div>
        <div>
          <dt>通道</dt>
          <dd>{row.confirmed_path || row.qualified_path || '—'}</dd>
        </div>
        <div>
          <dt>市值</dt>
          <dd>{formatCompact(row.market_cap_calculated ?? row.market_cap_coingecko)}</dd>
        </div>
      </dl>
      <ol className="gate-steps">
        {steps.map((s, i) => (
          <li key={s.id} className={`gate-step tone-${s.tone}`}>
            <span className="gate-n">{i + 1}</span>
            <div>
              <b>{s.title}</b>
              <p>{s.detail}</p>
            </div>
          </li>
        ))}
      </ol>
      {row.reason_codes?.length ? (
        <div className="reason-chips">
          {row.reason_codes.map((c) => (
            <span key={c}>{c}</span>
          ))}
        </div>
      ) : null}
      {row.not_confirmed_reasons?.length ? (
        <p className="not-conf">未确认：{row.not_confirmed_reasons.join(' · ')}</p>
      ) : null}
      {path.length > 0 && (
        <div className="path-strip">
          {path.slice(-8).map((n) => (
            <span key={n.scan_sequence} className={`st st-${n.state.toLowerCase()}`}>
              #{n.scan_sequence} {STATE_LABEL[n.state]}
            </span>
          ))}
        </div>
      )}
      <div className="inspect-chart">
        <ChartWorkspace
          candles={bars}
          height={168}
          source={src}
          pricePrecision={resolvePricePrecision({
            samples: bars.flatMap((b) => [b.open, b.high, b.low, b.close]),
          })}
        />
      </div>
    </aside>
  );
}
