import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { Link, useParams, useSearchParams } from 'react-router-dom';
import { fetchKlines, fetchSymbolDetail, subscribeScreenerEvents } from '../shared/api/screener';
import { fetchSymbolMeta } from '../shared/api/markets';
import type {
  ChartOverlay,
  Direction,
  OhlcvBar,
  SymbolScreenerDetail,
} from '../shared/types/screener';
import { STATE_LABEL } from '../shared/types/screener';
import { formatCompact, formatDwell, formatGrade, formatNum, formatPct, formatUtc } from '../shared/lib/format';
import { resolvePricePrecision } from '../shared/lib/price';
import {
  DEFAULT_KLINE_INTERVAL,
  KLINE_INTERVALS,
  intervalBarLimit,
  intervalSeconds,
  parseKlineInterval,
  snapBarTime,
  type KlineInterval,
} from '../shared/lib/intervals';
import { ChartWorkspace } from '../features/chart/ChartWorkspace';
import { backHrefFromOrigin, boardFromOrigin } from '../shared/config/boards';

export function MarketPage({
  embedded = false,
  symbolOverride,
}: {
  embedded?: boolean;
  symbolOverride?: string;
}) {
  const { symbol: routeSymbol = '' } = useParams();
  const symbol = (symbolOverride || routeSymbol).toUpperCase();
  const [params, setParams] = useSearchParams();
  const direction = (params.get('direction') as Direction) || 'up';
  const interval = parseKlineInterval(params.get('interval'));
  const from = params.get('from');
  const board = boardFromOrigin(from);
  const backHref = backHrefFromOrigin(from);
  const [candles, setCandles] = useState<OhlcvBar[]>([]);
  const [klineSource, setKlineSource] = useState('—');
  const [detail, setDetail] = useState<SymbolScreenerDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [chartBusy, setChartBusy] = useState(false);
  const [declaredPrecision, setDeclaredPrecision] = useState<number | undefined>(undefined);
  const hasBarsRef = useRef(false);

  const setInterval = (iv: KlineInterval) => {
    const next = new URLSearchParams(params);
    if (iv === DEFAULT_KLINE_INTERVAL) next.delete('interval');
    else next.set('interval', iv);
    setParams(next, { replace: true });
  };

  useEffect(() => {
    let alive = true;
    if (!hasBarsRef.current) setLoading(true);
    else setChartBusy(true);
    Promise.all([
      fetchKlines(symbol, interval, intervalBarLimit(interval)),
      fetchSymbolDetail(symbol, direction, board),
    ])
      .then(([kl, d]) => {
        if (!alive) return;
        setCandles(kl.bars);
        hasBarsRef.current = (kl.bars?.length || 0) > 0;
        setKlineSource(kl.source);
        setDetail(d);
      })
      .finally(() => {
        if (!alive) return;
        setLoading(false);
        setChartBusy(false);
      });
    return () => {
      alive = false;
    };
  }, [symbol, interval, direction, board]);

  useEffect(() => {
    return subscribeScreenerEvents((ev) => {
      if (ev.type !== 'screener.updated' && ev.type !== 'confirmed.changed') return;
      void fetchSymbolDetail(symbol, direction, board).then(setDetail);
    }, { board });
  }, [symbol, direction, board]);

  useEffect(() => {
    let alive = true;
    setDeclaredPrecision(undefined);
    fetchSymbolMeta(symbol)
      .then((m) => {
        if (!alive) return;
        const p = m?.price_precision;
        setDeclaredPrecision(p != null && Number.isFinite(p) ? p : undefined);
      })
      .catch(() => {
        if (alive) setDeclaredPrecision(undefined);
      });
    return () => {
      alive = false;
    };
  }, [symbol]);

  const pricePrecision = useMemo(
    () =>
      resolvePricePrecision({
        declared: declaredPrecision,
        samples: candles.flatMap((b) => [b.open, b.high, b.low, b.close]),
      }),
    [candles, declaredPrecision],
  );

  const overlays: ChartOverlay[] = useMemo(() => {
    if (!candles.length) return [];
    const last = candles[candles.length - 1];
    const list: ChartOverlay[] = [
      { type: 'anchor_line', payload: { price: candles[0].open, label: '00:00 UTC 锚点近似' } },
    ];
    const cur = detail?.current;
    const listed = cur?.state === 'QUALIFIED' || cur?.state === 'CONFIRMED';
    const enterPx = cur?.state_enter_price;
    const enterIso = cur?.state_enter_time_utc;
    if (listed && enterPx != null && Number.isFinite(Number(enterPx)) && enterIso) {
      const eventUnix = Date.parse(enterIso) / 1000;
      const barTime = snapBarTime(candles, eventUnix, intervalSeconds(interval));
      if (barTime != null) {
        list.push({
          type: 'entry_arrow',
          payload: {
            time: barTime,
            price: Number(enterPx),
            direction,
            state: cur.state === 'CONFIRMED' ? 'CONFIRMED' : 'QUALIFIED',
          },
        });
      }
    }
    list.push({
      type: 'scan_marker',
      payload: { time: last.time, scan_sequence: detail?.meta.scan_sequence },
    });
    return list;
  }, [candles, detail, direction, interval]);

  const row = detail?.current;

  return (
    <div className={`market-page${embedded ? ' embedded' : ''}`}>
      <div className="market-top">
        {!embedded && (
          <Link to={backHref} className="back">
            ← 返回榜单
          </Link>
        )}
        <div className="symbol-header">
          <h1>{symbol}</h1>
          {row ? (
            <div className="symbol-meta">
              <span className={`state-badge state-${row.state.toLowerCase()}`}>{STATE_LABEL[row.state]}</span>
              <span>
                {direction === 'up' ? 'UP' : 'DOWN'} · 停留 {formatDwell(row.state_duration_minutes)}
              </span>
              <span>
                Score <b>{formatNum(direction === 'up' ? row.score_up : row.score_down, 1)}</b>
              </span>
              <span>等级 {formatGrade(row.liquidity_grade)}</span>
            </div>
          ) : (
            !loading && <div className="muted">未入当轮候选池</div>
          )}
        </div>
      </div>

      <div className="market-layout">
        <section className="chart-panel">
          <div className="interval-bar" role="tablist" aria-label="K线周期">
            {KLINE_INTERVALS.map((iv) => (
              <button
                key={iv}
                type="button"
                role="tab"
                aria-selected={interval === iv}
                className={interval === iv ? 'on' : ''}
                onClick={() => setInterval(iv)}
              >
                {iv}
              </button>
            ))}
            {chartBusy ? <span className="interval-busy">切换 {interval}…</span> : null}
          </div>
          {loading && !candles.length ? (
            <div className="page-status">加载 K 线…</div>
          ) : (
            <ChartWorkspace
              candles={candles}
              overlays={overlays}
              height={embedded ? 280 : 520}
              defaultEngine="klinecharts"
              source={klineSource}
              pricePrecision={pricePrecision}
            />
          )}
          <div className="dock-placeholder">
            <div className="dock-card">
              <h3>OrderBook</h3>
              <p className="muted">P5：经网关转发，浏览器不直连交易所</p>
            </div>
            <div className="dock-card">
              <h3>Trades</h3>
              <p className="muted">P5：WS trades 频道</p>
            </div>
          </div>
        </section>

        <aside className="right-rail">
          <RailCard title="选币快照">
            {row ? (
              <dl className="kv">
                <dt>DirConf</dt>
                <dd>{formatNum(row.direction_confidence, 2)}</dd>
                <dt>S_L / M / SS</dt>
                <dd>
                  {formatNum(row.liquidity_score, 0)} / {formatNum(row.momentum_score, 0)} /{' '}
                  {formatNum(row.staircase_score, 0)}
                </dd>
                <dt>1h / 4h / 24h</dt>
                <dd>
                  <span className={row.ret_1h >= 0 ? 'up' : 'down'}>{formatPct(row.ret_1h)}</span>
                  {' · '}
                  <span className={(row.ret_4h ?? 0) >= 0 ? 'up' : 'down'}>{formatPct(row.ret_4h)}</span>
                  {' · '}
                  <span className={row.ret_24h >= 0 ? 'up' : 'down'}>{formatPct(row.ret_24h)}</span>
                </dd>
                <dt>入选</dt>
                <dd>{row.reason_codes.join(' · ') || '—'}</dd>
                <dt>未确认</dt>
                <dd>{row.not_confirmed_reasons.join(' · ') || '—'}</dd>
              </dl>
            ) : (
              <p className="muted">无快照</p>
            )}
          </RailCard>
          <RailCard title="双口径市值">
            {row ? (
              <dl className="kv">
                <dt>流通量</dt>
                <dd>{formatCompact(row.circulating_supply)}</dd>
                <dt>M^Calc</dt>
                <dd>{formatCompact(row.market_cap_calculated)}</dd>
                <dt>M^CG</dt>
                <dd>{formatCompact(row.market_cap_coingecko)}</dd>
                <dt>供应源</dt>
                <dd>
                  {row.supply_source} · {row.data_mode}
                </dd>
                <dt>供应时间</dt>
                <dd>{row.supply_as_of_utc ? formatUtc(row.supply_as_of_utc, 'HH:mm:ss') : '—'}</dd>
              </dl>
            ) : (
              <p className="muted">—</p>
            )}
          </RailCard>
          <RailCard title="门槛雷达">
            {row ? (
              <div className="bars">
                <Bar label="S_L" value={row.liquidity_score} />
                <Bar label="M" value={row.momentum_score} />
                <Bar label="S_MC" value={row.mcap_momentum_score} />
                <Bar label="SS" value={row.staircase_score} />
                <Bar label="S_DQ" value={row.data_confidence} />
              </div>
            ) : (
              <p className="muted">—</p>
            )}
          </RailCard>
          <RailCard title="状态路径">
            <div className="timeline">
              {(detail?.path ?? []).map((n) => (
                <div key={n.scan_sequence} className={`tl-node state-${n.state.toLowerCase()}`}>
                  <span className="tl-seq">#{n.scan_sequence}</span>
                  <span className="tl-state">{STATE_LABEL[n.state]}</span>
                  <span className="tl-score">
                    {formatNum(direction === 'up' ? n.score_up : n.score_down, 0)}
                  </span>
                </div>
              ))}
            </div>
          </RailCard>
        </aside>
      </div>
      <footer className="attr-footer">
        Charts by{' '}
        <a href="https://www.tradingview.com" target="_blank" rel="noreferrer">
          TradingView
        </a>{' '}
        Lightweight Charts · Powered by CoinGecko
      </footer>
    </div>
  );
}

function RailCard({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="rail-card">
      <h3>{title}</h3>
      {children}
    </div>
  );
}

function Bar({ label, value }: { label: string; value: number }) {
  return (
    <div className="bar-row">
      <span>{label}</span>
      <div className="bar-track">
        <div className="bar-fill" style={{ width: `${Math.min(100, Math.max(0, value))}%` }} />
      </div>
      <b>{formatNum(value, 0)}</b>
    </div>
  );
}
