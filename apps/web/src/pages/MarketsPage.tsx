import { useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useVirtualizer } from '@tanstack/react-virtual';
import { fetchUniverse, type UniverseSymbol } from '../shared/api/markets';

export function MarketsPage({ embedded = false }: { embedded?: boolean }) {
  const [rows, setRows] = useState<UniverseSymbol[]>([]);
  const [stats, setStats] = useState<Record<string, unknown>>({});
  const [source, setSource] = useState('');
  const [q, setQ] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const navigate = useNavigate();
  const parentRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    fetchUniverse()
      .then((body) => {
        if (!alive) return;
        setRows(body.symbols || []);
        setStats((body.stats || {}) as Record<string, unknown>);
        setSource(body.source || '');
      })
      .catch((e: Error) => alive && setError(e.message))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, []);

  const filtered = useMemo(() => {
    const needle = q.trim().toUpperCase();
    if (!needle) return rows;
    return rows.filter(
      (r) =>
        r.symbol.toUpperCase().includes(needle) ||
        (r.base_asset || '').toUpperCase().includes(needle),
    );
  }, [rows, q]);

  const virtualizer = useVirtualizer({
    count: filtered.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => 36,
    overscan: 18,
  });

  if (loading) return <div className="page-status">加载合约清单…</div>;
  if (error) return <div className="page-status error">加载失败：{error}</div>;

  return (
    <div className={`markets-page${embedded ? ' embedded' : ''}`}>
      <header className="markets-head">
        <div>
          <h1>Binance USDT-M · COIN 永续清单</h1>
          <p className="muted">
            选币宇宙 = USDT + PERPETUAL + TRADING + underlyingType=COIN。
            TRADIFI_PERPETUAL 是 <b>contractType</b>，不进本表。 源 {source || '—'} ·{' '}
            {filtered.length}/{rows.length}
          </p>
        </div>
        <div className="markets-stats">
          <Stat label="选中" value={String(stats.selected_count ?? rows.length)} />
          <Stat label="COIN USDT-M" value={String(stats.coin_usdt_perp_trading ?? '—')} />
          <Stat label="TradFi CT" value={String(stats.tradifi_perpetual_contract_type ?? '—')} />
          <Stat label="exchangeInfo" value={String(stats.exchange_symbol_count ?? '—')} />
        </div>
      </header>
      <div className="markets-toolbar">
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="搜索 BTC / 1000SHIB …"
        />
      </div>
      <div className="markets-table-head">
        <span>合约</span>
        <span>标的</span>
        <span>underlying</span>
        <span>kind</span>
        <span>精度</span>
      </div>
      <div className="markets-scroll" ref={parentRef}>
        <div style={{ height: virtualizer.getTotalSize(), position: 'relative' }}>
          {virtualizer.getVirtualItems().map((vi) => {
            const r = filtered[vi.index];
            return (
              <button
                type="button"
                key={r.symbol}
                className="markets-row"
                style={{
                  position: 'absolute',
                  top: 0,
                  left: 0,
                  width: '100%',
                  height: vi.size,
                  transform: `translateY(${vi.start}px)`,
                }}
                onClick={() => navigate(`/market/${r.symbol}`)}
              >
                <b>{r.symbol}</b>
                <span>{r.base_asset}</span>
                <span className="muted">{r.underlying_type || '—'}</span>
                <span>{r.market_kind || 'coin_perp'}</span>
                <span className="muted">
                  p{r.price_precision}/q{r.quantity_precision}
                </span>
              </button>
            );
          })}
        </div>
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

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="metric">
      <div className="metric-label">{label}</div>
      <div className="metric-value">{value}</div>
    </div>
  );
}
