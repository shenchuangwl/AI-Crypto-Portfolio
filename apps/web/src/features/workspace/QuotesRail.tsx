import { useEffect, useMemo, useRef, useState } from 'react';
import { useVirtualizer } from '@tanstack/react-virtual';
import { fetchUniverse, type UniverseSymbol } from '../../shared/api/markets';
import type { CandidateRow, ScreenerSnapshot } from '../../shared/types/screener';
import { STATE_LABEL } from '../../shared/types/screener';
import { useTickerStore } from '../../shared/stores/tickerStore';
import { formatCompact, formatPct } from '../../shared/lib/format';
import { formatPrice } from '../../shared/lib/price';
import { allRows, uniqueBySymbol } from './funnel';

type SortKey = 'symbol' | 'last' | 'chg24' | 'mcap';

interface Props {
  snap: ScreenerSnapshot | null;
  selected: string | null;
  onPick: (row: CandidateRow) => void;
}

function stubRow(u: UniverseSymbol, snapRow?: CandidateRow): CandidateRow {
  if (snapRow) return snapRow;
  const base = (u.base_asset || u.symbol.replace(/USDT$/, '')).toUpperCase();
  return {
    rank: 0,
    symbol: u.symbol,
    underlying_asset: base,
    canonical_asset_id: base.toLowerCase(),
    contract_multiplier: 1,
    direction: 'up',
    state: 'NONE',
    state_enter_time_utc: '',
    state_duration_minutes: 0,
    state_enter_price: null,
    score_up: 0,
    score_down: 0,
    direction_confidence: 0,
    liquidity_score: 0,
    liquidity_grade: 'UNCLASSIFIED',
    momentum_score: 0,
    mcap_momentum_score: 0,
    staircase_score: 0,
    consistency_score: 0,
    rank_velocity_score: 0,
    risk_score: 0,
    data_confidence: 0,
    ret_15m: 0,
    ret_1h: 0,
    ret_4h: 0,
    ret_24h: 0,
    ret_1w: null,
    ret_1mo: null,
    ret_since_anchor: 0,
    aqv_6d_m: 0,
    aqv_12d_m: 0,
    aqv_26d_m: 0,
    circulating_supply: null,
    market_cap_coingecko: null,
    market_cap_calculated: null,
    supply_source: 'NONE',
    data_mode: 'MISSING',
    supply_as_of_utc: null,
    mapping_confidence: 0,
    risk_flags: [],
    reason_codes: [],
    not_confirmed_reasons: [],
  };
}

export function QuotesRail({ snap, selected, onPick }: Props) {
  const [uni, setUni] = useState<UniverseSymbol[]>([]);
  const [q, setQ] = useState('');
  const [sort, setSort] = useState<SortKey>('mcap');
  const [err, setErr] = useState<string | null>(null);
  const parentRef = useRef<HTMLDivElement>(null);
  const quotes = useTickerStore((s) => s.quotes);

  useEffect(() => {
    let alive = true;
    fetchUniverse()
      .then((body) => {
        if (!alive) return;
        setUni(body.symbols || []);
      })
      .catch((e: Error) => alive && setErr(e.message));
    return () => {
      alive = false;
    };
  }, []);

  const bySym = useMemo(() => {
    const m = new Map<string, CandidateRow>();
    if (snap) {
      for (const r of uniqueBySymbol(allRows(snap))) m.set(r.symbol, r);
    }
    return m;
  }, [snap]);

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
      const snapRow = bySym.get(u.symbol);
      const px = quotes[u.symbol];
      const last = px?.last ?? px?.mark ?? snapRow?.last_price ?? snapRow?.ref_price ?? null;
      const chg = snapRow?.ret_24h ?? null;
      const mcap = snapRow?.market_cap_calculated ?? snapRow?.market_cap_coingecko ?? null;
      return { u, snapRow, last, chg, mcap };
    });
    decorated.sort((a, b) => {
      if (sort === 'symbol') return a.u.symbol.localeCompare(b.u.symbol);
      if (sort === 'last') return (b.last ?? -1) - (a.last ?? -1);
      if (sort === 'chg24') return (b.chg ?? -999) - (a.chg ?? -999);
      return (b.mcap ?? -1) - (a.mcap ?? -1);
    });
    return decorated;
  }, [uni, q, sort, bySym, quotes]);

  const virt = useVirtualizer({
    count: rows.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => 36,
    overscan: 16,
  });

  return (
    <aside className="quotes-rail">
      <header className="sel-col-head">
        <b>行情</b>
        <span>{rows.length}/{uni.length || '—'}</span>
      </header>
      <div className="quotes-toolbar">
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="搜索 BTC / 1000SHIB"
          aria-label="搜索行情"
        />
        <div className="quotes-sort">
          {(
            [
              ['mcap', '市值'],
              ['chg24', '24h'],
              ['last', '价格'],
              ['symbol', '代码'],
            ] as const
          ).map(([k, lab]) => (
            <button
              key={k}
              type="button"
              className={sort === k ? 'on' : ''}
              onClick={() => setSort(k)}
            >
              {lab}
            </button>
          ))}
        </div>
      </div>
      <div className="quotes-cols">
        <span>商品</span>
        <span>价格</span>
        <span>24h</span>
      </div>
      {err && <div className="desk-err">{err}</div>}
      <div className="quotes-scroll" ref={parentRef}>
        <div style={{ height: virt.getTotalSize(), position: 'relative' }}>
          {virt.getVirtualItems().map((vi) => {
            const r = rows[vi.index];
            const st = r.snapRow?.state;
            return (
              <button
                type="button"
                key={r.u.symbol}
                className={`quotes-row${selected === r.u.symbol ? ' sel' : ''}`}
                style={{
                  position: 'absolute',
                  top: 0,
                  left: 0,
                  width: '100%',
                  height: vi.size,
                  transform: `translateY(${vi.start}px)`,
                }}
                onClick={() => onPick(stubRow(r.u, r.snapRow))}
              >
                <span className="q-sym">
                  <b>{r.u.base_asset || r.u.symbol.replace(/USDT$/, '')}</b>
                  <em>USDT</em>
                  {st && st !== 'NONE' ? (
                    <i className={`st st-${st.toLowerCase()}`}>{STATE_LABEL[st]}</i>
                  ) : null}
                </span>
                <span className="q-px">
                  {formatPrice(r.last, r.u.price_precision)}
                </span>
                <span className={`q-chg ${r.chg == null ? '' : r.chg > 0 ? 'up' : r.chg < 0 ? 'down' : ''}`}>
                  {r.chg == null ? '—' : formatPct(r.chg)}
                </span>
                <span className="q-mcap">{formatCompact(r.mcap)}</span>
              </button>
            );
          })}
        </div>
      </div>
    </aside>
  );
}
