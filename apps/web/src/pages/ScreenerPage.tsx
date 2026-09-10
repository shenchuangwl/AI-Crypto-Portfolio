import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  fetchConfirmedFeed,
  fetchScreenerLatest,
  subscribeScreenerEvents,
  type ConfirmedFeed,
} from '../shared/api/screener';
import type { CandidateRow, ScreenerSnapshot, ScreenerState } from '../shared/types/screener';
import { DEFAULT_POOL_STATES, STATE_LABEL } from '../shared/types/screener';
import { boardConfig, DEFAULT_BOARD, originTagFor, type BoardKey } from '../shared/config/boards';
import { screenerUiStoreFor } from '../shared/stores/screenerUi';
import { usePricePoll } from '../shared/hooks/usePricePoll';
import { useTickerStore } from '../shared/stores/tickerStore';
import { sortRows } from '../shared/lib/sortRows';
import { filterPools, type PoolFilter } from '../shared/lib/poolFilter';
import { ScreenerHeaderStrip } from '../features/screener/components/ScreenerHeaderStrip';
import { StateSummaryChips } from '../features/screener/components/StateSummaryChips';
import { DirectionTabs } from '../features/screener/components/DirectionTabs';
import { PoolToolbar } from '../features/screener/components/PoolToolbar';
import { CandidateDataGrid } from '../features/screener/components/CandidateDataGrid';
import { TransitionTicker } from '../features/screener/components/TransitionTicker';
import { ConfirmedBanner } from '../features/screener/components/ConfirmedBanner';
import { OnlyCoinPanel } from '../features/onlycoin/OnlyCoinPanel';
import { matchingConfirmed } from '../shared/lib/onlycoin';
import { CycleStrip } from '../features/screener/components/CycleStrip';

/** 稳定的空数组：快照还没到时不要每次 render 都造新引用，白白让下游 memo 失效。 */
const NO_ROWS: CandidateRow[] = [];

/**
 * 选币榜页面 —— 「选币榜」(board=main) 与「选币榜Y」(board=y) 共用的**同一个**组件。
 *
 * 两块板面的差别被完全收敛进 `board` 这一个 prop：
 *   - 取数前缀    `/screener` ↔ `/screener-y`（见 shared/config/boards.ts）
 *   - UI 状态仓库  各自一份，筛选/排序互不串台
 *   - 参数版本     v1.4.0 ↔ v2.0.0（只用于页头展示）
 *
 * 其余一切 —— 列结构、分区、候选池去重口径、排序、SSE + 轮询、复盘闭环 —— 完全一致。
 * 这就是「Y 是选币榜 100% 复刻」在前端的落点：没有第二份实现可以偷偷跑偏。
 */
export function ScreenerPage({
  embedded = false,
  board = DEFAULT_BOARD,
}: {
  embedded?: boolean;
  board?: BoardKey;
}) {
  const cfg = boardConfig(board);
  const useUi = screenerUiStoreFor(cfg.key);
  const [snap, setSnap] = useState<ScreenerSnapshot | null>(null);
  const [confirmed, setConfirmed] = useState<ConfirmedFeed | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [pulse, setPulse] = useState(false);
  const [lastEvent, setLastEvent] = useState('');
  const [sseLive, setSseLive] = useState(false);
  const ui = useUi();
  const quotes = useTickerStore((s) => s.quotes);
  const snapRef = useRef<ScreenerSnapshot | null>(null);
  const inflightRef = useRef<Promise<void> | null>(null);
  snapRef.current = snap;
  // 快照没到之前不要跟 latest 抢网关；Last* 列可以先显示快照价。
  usePricePoll(8000, !!snap);

  const reload = useCallback(
    async (reason?: string) => {
      if (inflightRef.current) return inflightRef.current;
      const run = (async () => {
        try {
          const s = await fetchScreenerLatest(cfg.key);
          setSnap(s);
          setError(null);
          if (reason) setLastEvent(reason);
          setLoading(false);
          // confirmed 是横幅/脉冲的增量，不要挡住整张榜。
          void fetchConfirmedFeed(cfg.key)
            .then(setConfirmed)
            .catch(() => {
              /* 横幅可从 snap.dmr_selected 回退 */
            });
        } catch (e) {
          setError((e as Error).message);
          setLoading(false);
        } finally {
          inflightRef.current = null;
        }
      })();
      inflightRef.current = run;
      return run;
    },
    [cfg.key],
  );

  useEffect(() => {
    void reload('initial');
  }, [reload]);

  // 先把 /latest 拉完再占一条 SSE。HTTP/1.1 每主机 6 条，多标签 /events
  // 会把 /screener/latest 排队到 12s AbortError。
  useEffect(() => {
    if (!snap) return;
    const unsub = subscribeScreenerEvents((ev) => {
      setSseLive(true);
      if (ev.type === 'hello') {
        setLastEvent('SSE connected');
        return;
      }
      if (ev.type === 'screener.updated' || ev.type === 'confirmed.changed') {
        const sid = String(ev.data.scan_id || '');
        const cc = ev.data.confirmed_count;
        setLastEvent(`${ev.type}${sid ? ` ${sid}` : ''}${cc != null ? ` conf=${cc}` : ''}`);
        if (ev.type === 'confirmed.changed') {
          setPulse(true);
          window.setTimeout(() => setPulse(false), 2500);
        }
        if (sid && snapRef.current?.meta.scan_id === sid) return;
        void reload(ev.type);
      }
    }, { board: cfg.key });
    return unsub;
  }, [!!snap, reload, cfg.key]);

  const poolFilter = useMemo<PoolFilter>(
    () => ({
      activeStates: ui.activeStates,
      dmrActive: ui.dmrActive,
      grades: ui.grades,
      search: ui.search,
    }),
    [ui.activeStates, ui.dmrActive, ui.grades, ui.search],
  );

  // 两个池按同一套判据各过滤一次，顺带给出去重后的唯一币种数（分区多选是并集：
  // 同一行同时命中 DMR 与确认时只留一份，不会重复累加）。刻意不依赖 quotes ——
  // 8 秒一次的报价轮询不该让 1050 行重新过滤，标签页的数字也不该跟着报价抖动。
  const pools = useMemo(
    () => filterPools(snap?.long_pool ?? NO_ROWS, snap?.short_pool ?? NO_ROWS, poolFilter),
    [snap, poolFilter],
  );

  const pool = useMemo(() => {
    if (!snap) return NO_ROWS;
    return ui.direction === 'up' ? snap.long_pool : snap.short_pool;
  }, [snap, ui.direction]);

  const filtered = useMemo(
    () =>
      sortRows(
        ui.direction === 'up' ? pools.long : pools.short,
        ui.direction,
        ui.sortId,
        ui.sortDesc,
        quotes,
      ),
    [pools, ui.direction, ui.sortId, ui.sortDesc, quotes],
  );

  /** 只讲分区（DMR + 状态 chip），方向/等级/搜索不进 —— 给方向标签页的 tooltip 用。 */
  const zoneSummary = useMemo(() => {
    const labels = ui.activeStates.map((s) => STATE_LABEL[s]);
    if (ui.dmrActive) labels.unshift('DMR');
    return labels.join('+') || '无分区';
  }, [ui.dmrActive, ui.activeStates]);

  const filterSummary = useMemo(() => {
    const stateLabels = ui.activeStates.map((s) => STATE_LABEL[s]);
    if (ui.dmrActive) stateLabels.unshift('DMR');
    const parts = [ui.direction === 'up' ? '上涨池' : '下跌池', stateLabels.join('+') || '无状态'];
    if (ui.grades.length) parts.push(`等级${ui.grades.join('/')}`);
    if (ui.search.trim()) parts.push(`搜索:${ui.search.trim()}`);
    return parts.join(' · ');
  }, [ui.direction, ui.dmrActive, ui.activeStates, ui.grades, ui.search]);

  const isDefaultFilter =
    ui.direction === 'up' &&
    !ui.dmrActive &&
    ui.search === '' &&
    ui.grades.length === 0 &&
    ui.activeStates.length === DEFAULT_POOL_STATES.length &&
    DEFAULT_POOL_STATES.every((s) => ui.activeStates.includes(s));

  const counts = useMemo(() => {
    const src = [...(snap?.long_pool ?? []), ...(snap?.short_pool ?? [])];
    const out: Partial<Record<ScreenerState, number>> = {};
    for (const r of src) {
      const z = (r.product_zone as ScreenerState | null | undefined) || r.state;
      out[z] = (out[z] ?? 0) + 1;
    }
    const anyProduct = src.some((r) => r.product_zone);
    return anyProduct ? out : snap?.meta.state_counts;
  }, [snap]);

  const currentFeed = matchingConfirmed(confirmed, snap?.meta.scan_id || '');

  if (loading) return <div className="page-status">加载选币快照…</div>;
  if (!snap || (error && cfg.key !== 'y')) return <div className="page-status error">加载失败：{error}</div>;

  return (
    <div className={`screener-page board-${cfg.key}${embedded ? ' embedded' : ''}`}>
      <ScreenerHeaderStrip
        meta={snap.meta}
        expiresAt={snap.expires_at_utc}
        brandMark={cfg.mark}
        brandTitle={`Binance USDT 永续 · ${cfg.label}实时榜`}
        brandNote={cfg.note}
      />
      {/* 时间区状态条：只有启用了 24h 周期的板面（选币榜Y）才渲染；
          选币榜的快照没有 meta.cycle，这里返回 null，页面与改动前逐像素相同。 */}
      {snap.meta.cycle?.enabled && <CycleStrip cycle={snap.meta.cycle} />}
      <div className="live-row">
        <span className={`live-dot${sseLive ? ' on' : ''}`} />
        <span className="muted">
          {sseLive ? '实时通道已连接（SSE/轮询）' : '连接实时通道…'} · {lastEvent || '—'}
        </span>
      </div>
      {error && <p role="alert">快照刷新失败：{error} · 显示缓存</p>}
      {cfg.key === 'y' ? <OnlyCoinPanel onRefresh={() => void reload('manual')} current={
        <ConfirmedBanner board={cfg.key} long={(currentFeed?.long || snap.long_pool).filter(r => r.dmr_selected)} short={(currentFeed?.short || snap.short_pool).filter(r => r.dmr_selected)} scanId={snap.meta.scan_id} pulse={pulse} lastEvent={lastEvent} onRefresh={() => void reload('manual')} />
      } /> : <ConfirmedBanner
        board={cfg.key}
        long={(currentFeed?.long || snap.long_pool).filter((r) => r.dmr_selected)}
        short={(currentFeed?.short || snap.short_pool).filter((r) => r.dmr_selected)}
        scanId={snap.meta.scan_id}
        pulse={pulse}
        lastEvent={lastEvent}
        onRefresh={() => void reload('manual')}
      />}
      <StateSummaryChips
        counts={counts ?? snap.meta.state_counts}
        dmrCount={snap.meta.hierarchy?.dmr_unique ?? snap.meta.dmr?.inbox_count ?? 0}
        dmrActive={ui.dmrActive}
        onToggleDmr={ui.toggleDmr}
        active={ui.activeStates}
        onToggle={ui.toggleState}
      />
      <div className="screener-controls">
        <DirectionTabs
          value={ui.direction}
          longCount={pools.longCoins}
          shortCount={pools.shortCoins}
          zoneSummary={zoneSummary}
          onChange={ui.setDirection}
        />
        <PoolToolbar
          search={ui.search}
          onSearchChange={ui.setSearch}
          preset={ui.columnPreset}
          onPresetChange={ui.setColumnPreset}
          grades={ui.grades}
          onGradesChange={ui.setGrades}
          visibleCount={filtered.length}
          totalCount={pool.length}
          filterSummary={filterSummary}
          isDefaultFilter={isDefaultFilter}
          onResetFilters={ui.resetFilters}
        />
      </div>
      <CandidateDataGrid
        rows={filtered}
        preset={ui.columnPreset}
        direction={ui.direction}
        sortId={ui.sortId}
        sortDesc={ui.sortDesc}
        onSort={ui.setSort}
        originTag={originTagFor(cfg.key)}
      />
      <TransitionTicker events={snap.transitions} />
      <footer className="attr-footer">
        {snap.attribution ?? 'Powered by CoinGecko'}
        <span className="muted">
          · mapping {snap.meta.mapping_version} · data {snap.meta.data_version}
        </span>
        <span className="muted">
          {' '}
          · Charts by{' '}
          <a href="https://www.tradingview.com" target="_blank" rel="noreferrer">
            TradingView
          </a>
        </span>
      </footer>
    </div>
  );
}
