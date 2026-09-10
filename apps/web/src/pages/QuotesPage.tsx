import { useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useVirtualizer } from '@tanstack/react-virtual';
import { fetchUniverse, type UniverseSymbol } from '../shared/api/markets';
import { fetchScreenerLatest } from '../shared/api/screener';
import type { CandidateRow, ScreenerSnapshot } from '../shared/types/screener';
import { usePricePoll } from '../shared/hooks/usePricePoll';
import { useTickerStore } from '../shared/stores/tickerStore';
import { formatCategory } from '../shared/lib/category';
import { formatCompact, formatPct } from '../shared/lib/format';
import { formatPrice } from '../shared/lib/price';
import { allRows, uniqueBySymbol } from '../features/workspace/funnel';

type SortKey = 'symbol' | 'last' | 'chg' | 'mcap' | 'vol' | 'supply' | 'circMcap' | 'turnCirc' | 'turn' | 'cat';

const COLS: { id: SortKey; label: string; align: 'left' | 'right' }[] = [
  { id: 'symbol', label: '商品', align: 'left' },
  { id: 'last', label: '价格', align: 'right' },
  { id: 'chg', label: '涨跌幅', align: 'right' },
  { id: 'mcap', label: '总市值', align: 'right' },
  { id: 'vol', label: '美元成交量24H', align: 'right' },
  { id: 'supply', label: '流通供应量', align: 'right' },
  { id: 'circMcap', label: '流通市值', align: 'right' },
  { id: 'turnCirc', label: '成交额/流通市值', align: 'right' },
  { id: 'turn', label: '成交量/市值', align: 'right' },
  { id: 'cat', label: '分类', align: 'left' },
];

function snapMap(snap: ScreenerSnapshot | null) {
  const m = new Map<string, CandidateRow>();
  if (!snap) return m;
  for (const r of uniqueBySymbol(allRows(snap))) m.set(r.symbol, r);
  return m;
}

function catText(raw: string): string {
  return raw.replace(/，/g, ', ').replace(/^—$/, '');
}

export function QuotesPage() {
  usePricePoll(8000);
  const [uni, setUni] = useState<UniverseSymbol[]>([]);
  const [snap, setSnap] = useState<ScreenerSnapshot | null>(null);
  const [q, setQ] = useState('');
  const [sort, setSort] = useState<SortKey>('mcap');
  const [sortDesc, setSortDesc] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const parentRef = useRef<HTMLDivElement>(null);
  const quotes = useTickerStore((s) => s.quotes);
  const navigate = useNavigate();

  useEffect(() => {
    let alive = true;
    Promise.all([fetchUniverse(), fetchScreenerLatest().catch(() => null)])
      .then(([u, s]) => {
        if (!alive) return;
        setUni(u.symbols || []);
        setSnap(s);
      })
      .catch((e: Error) => alive && setErr(e.message));
    return () => {
      alive = false;
    };
  }, []);

  const bySym = useMemo(() => snapMap(snap), [snap]);

  const rows = useMemo(() => {
    const needle = q.trim().toUpperCase();
    let list = uni;
    if (needle) {
      list = uni.filter(
        (u) =>
          u.symbol.toUpperCase().includes(needle) ||
          (u.base_asset || '').toUpperCase().includes(needle),
      );
    }
    const decorated = list.map((u) => {
      const s = bySym.get(u.symbol);
      const px = quotes[u.symbol];
      const last = px?.last ?? px?.mark ?? s?.last_price ?? s?.ref_price ?? null;
      const chg = px?.chg24h ?? s?.ret_24h ?? null;
      const mcap = s?.market_cap_coingecko ?? s?.market_cap_calculated ?? null;
      const circMcap = s?.market_cap_calculated ?? null;
      const vol = px?.quoteVolume24h ?? null;
      const supply = s?.circulating_supply ?? null;
      const turn = vol != null && mcap != null && mcap > 0 ? vol / mcap : null;
      const turnCirc = vol != null && circMcap != null && circMcap > 0 ? vol / circMcap : null;
      const cat = catText(formatCategory(u));
      return { u, last, chg, mcap, vol, supply, circMcap, turnCirc, turn, cat };
    });
    const dir = sortDesc ? 1 : -1;
    decorated.sort((a, b) => {
      const miss = (v: number | null) => (v == null || Number.isNaN(v) ? -1e99 : v);
      switch (sort) {
        case 'symbol':
          return dir * a.u.symbol.localeCompare(b.u.symbol);
        case 'last':
          return dir * (miss(b.last) - miss(a.last));
        case 'chg':
          return dir * (miss(b.chg) - miss(a.chg));
        case 'vol':
          return dir * (miss(b.vol) - miss(a.vol));
        case 'supply':
          return dir * (miss(b.supply) - miss(a.supply));
        case 'circMcap':
          return dir * (miss(b.circMcap) - miss(a.circMcap));
        case 'turnCirc':
          return dir * (miss(b.turnCirc) - miss(a.turnCirc));
        case 'turn':
          return dir * (miss(b.turn) - miss(a.turn));
        case 'cat':
          return dir * a.cat.localeCompare(b.cat);
        default:
          return dir * (miss(b.mcap) - miss(a.mcap));
      }
    });
    return decorated;
  }, [uni, q, sort, sortDesc, bySym, quotes]);

  const virt = useVirtualizer({
    count: rows.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => 36,
    overscan: 20,
  });
  const vItems = virt.getVirtualItems();
  const padTop = vItems[0]?.start ?? 0;
  const padBottom = virt.getTotalSize() - (vItems[vItems.length - 1]?.end ?? 0);

  const clickSort = (k: SortKey) => {
    if (sort === k) setSortDesc((d) => !d);
    else {
      setSort(k);
      setSortDesc(k !== 'symbol' && k !== 'cat');
    }
  };

  return (
    <div className="quotes-page">
      <header className="quotes-page-head">
        <div>
          <h1>行情</h1>
          <p className="muted">
            选币宇宙 {rows.length}/{uni.length || '—'} · USDT-M COIN 永续 · 不含 TradFi
          </p>
        </div>
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="搜索 BTC / 1000SHIB"
          aria-label="搜索行情"
        />
      </header>
      {err && <div className="desk-err">{err}</div>}
      <div className="quotes-table-wrap" ref={parentRef}>
        <table className="quotes-table">
          <colgroup>
            <col className="c-sym" />
            <col className="c-px" />
            <col className="c-chg" />
            <col className="c-mcap" />
            <col className="c-vol" />
            <col className="c-sup" />
            <col className="c-cmcap" />
            <col className="c-tcirc" />
            <col className="c-turn" />
            <col className="c-cat" />
          </colgroup>
          <thead>
            <tr>
              {COLS.map((c) => (
                <th
                  key={c.id}
                  scope="col"
                  className={`qh qh-${c.align}${sort === c.id ? ' on' : ''}${c.id === 'cat' ? ' qh-cat' : ''}`}
                >
                  <button type="button" onClick={() => clickSort(c.id)}>
                    {c.label}
                    {sort === c.id ? (sortDesc ? ' ↓' : ' ↑') : ''}
                  </button>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {padTop > 0 && (
              <tr className="qt-pad" aria-hidden>
                <td colSpan={10} style={{ height: padTop }} />
              </tr>
            )}
            {vItems.map((vi) => {
              const r = rows[vi.index];
              const name = r.u.base_asset || r.u.symbol.replace(/USDT$/, '');
              const chgCls = r.chg == null ? '' : r.chg > 0 ? 'up' : r.chg < 0 ? 'down' : '';
              return (
                <tr
                  key={r.u.symbol}
                  className="quotes-table-row"
                  tabIndex={0}
                  onClick={() => navigate(`/market/${r.u.symbol}?from=quotes`)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' || e.key === ' ') {
                      e.preventDefault();
                      navigate(`/market/${r.u.symbol}?from=quotes`);
                    }
                  }}
                >
                  <td className="qt-sym">
                    <b>{name}</b>
                    <em>USDT</em>
                  </td>
                  <td className="qt-num">
                    {formatPrice(r.last, r.u.price_precision)}
                  </td>
                  <td className={`qt-num ${chgCls}`}>
                    {r.chg == null ? '—' : formatPct(r.chg)}
                  </td>
                  <td className="qt-num">{formatCompact(r.mcap)}</td>
                  <td className="qt-num">{formatCompact(r.vol)}</td>
                  <td className="qt-num">{formatCompact(r.supply)}</td>
                  <td className="qt-num" title={r.circMcap != null ? String(r.circMcap) : undefined}>
                    {formatCompact(r.circMcap)}
                  </td>
                  <td className="qt-num">
                    {r.turnCirc == null ? '—' : `${(r.turnCirc * 100).toFixed(2)}%`}
                  </td>
                  <td className="qt-num">
                    {r.turn == null ? '—' : `${(r.turn * 100).toFixed(2)}%`}
                  </td>
                  <td className="qt-cat" title={r.cat || undefined}>
                    {r.cat || '—'}
                  </td>
                </tr>
              );
            })}
            {padBottom > 0 && (
              <tr className="qt-pad" aria-hidden>
                <td colSpan={10} style={{ height: padBottom }} />
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
