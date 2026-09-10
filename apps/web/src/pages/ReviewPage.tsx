import { OnlyCoinReview } from '../features/onlycoin/OnlyCoinReview';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import {
  fetchReviewCoverage,
  fetchReviewSummary,
  fetchReviewTrades,
  windowFromDays,
  type ReviewQuery,
} from '../shared/api/review';
import { subscribeScreenerEvents } from '../shared/api/screener';
import { usePricePoll } from '../shared/hooks/usePricePoll';
import { formatEnterClock } from '../shared/lib/format';
import { downloadCsv, tradesToCsv } from '../shared/lib/reviewCsv';
import { dualClock, localInputToUtcIso, utcIsoToLocalInput } from '../shared/lib/reviewTime';
import { readReviewTime } from '../shared/lib/reviewTimePreferences';
import { MAX_COMPARE_PERIODS, useReviewUi } from '../shared/stores/reviewUi';
import type {
  ReviewCoverage,
  ReviewCycleWindow,
  ReviewResponse,
  ReviewTrade,
} from '../shared/types/review';
import { CONTROL_ZONES, REVIEW_ZONE_LABEL, REVIEW_ZONES } from '../shared/types/review';
import { ReviewDataGrid } from '../features/review/ReviewDataGrid';
import { ControlBenchmarkCard, SummaryCard } from '../features/review/ReviewSummaryCards';
import { CoverageBanner } from '../features/review/CoverageBanner';
import { ParamHashBar } from '../features/review/ParamHashBar';
import { ParamVersionBar } from '../features/review/ParamVersionBar';
import { ReviewBoardBar } from '../features/review/ReviewBoardBar';
import { CycleWindowBar } from '../features/review/CycleWindowBar';
import { BOARDS } from '../shared/config/boards';
import { PeriodCompareTable, type ComparePeriodResult } from '../features/review/PeriodCompareTable';
import { OpenTradesPanel } from '../features/review/OpenTradesPanel';
import { searchToState, stateToSearch } from '../features/review/reviewQuery';
import { snapWindow, windowAligned } from '../features/review/cycleAlign';

function parseSymbols(raw: string): string[] | undefined {
  const parts = raw
    .split(/[\s,;]+/)
    .map((s) => s.trim())
    .filter(Boolean);
  return parts.length ? parts : undefined;
}

/** UTC 起止输入。展示 +07 墙钟，提交转 UTC；清空/半填不抛错、不发请求。 */
function RangeInput({
  value,
  onChange,
  ariaLabel,
}: {
  value: string;
  onChange: (utcIso: string) => void;
  ariaLabel: string;
}) {
  return (
    <input
      type="datetime-local"
      aria-label={ariaLabel}
      value={utcIsoToLocalInput(value)}
      onChange={(e) => onChange(localInputToUtcIso(e.target.value))}
    />
  );
}

export function ReviewPage({ embedded = false }: { embedded?: boolean }) {
  const board = useReviewUi(s => s.board);
  const [mode, setMode] = useState<'normal' | 'onlycoin'>('normal');
  useEffect(() => { if (board !== 'y') setMode('normal'); }, [board]);
  const onlycoin = board === 'y' && mode === 'onlycoin';
  return <>
    {board === 'y' && <div className="onlycoin-review-tabs">
      <button type="button" aria-pressed={!onlycoin} onClick={() => setMode('normal')}>原复盘 · 进出账本</button>
      <button type="button" aria-pressed={onlycoin} onClick={() => setMode('onlycoin')}>OnlyCoin · 来源候选回放</button>
    </div>}
    {onlycoin && <OnlyCoinReview />}
    <div hidden={onlycoin}><NormalReviewPage embedded={embedded} /></div>
  </>;
}

function NormalReviewPage({ embedded = false }: { embedded?: boolean }) {
  const ui = useReviewUi();
  const navigate = useNavigate();
  const location = useLocation();
  const [pack, setPack] = useState<ReviewResponse | null>(null);
  const [compare, setCompare] = useState<ComparePeriodResult[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [cov, setCov] = useState<ReviewCoverage | undefined>(undefined);
  const [booted, setBooted] = useState(false);
  const bootedOnce = useRef(false);
  usePricePoll(8000);

  // ---- Boot: hydrate filters from the URL, then learn the ledger watermark,
  //      and only then issue the first query.
  //
  //      Filters live in the URL (shareable, debuggable). Only time controls are
  //      remembered locally per board; explicit URL times take precedence. The window
  //      is anchored on the ledger watermark, not the wall clock, so querying
  //      before coverage arrives would paint one window and then silently
  //      replace it with a different one on the next interaction.
  useEffect(() => {
    if (bootedOnce.current) return;
    bootedOnce.current = true;
    let alive = true;
    const patch = searchToState(location.search);
    const board = patch.board ?? useReviewUi.getState().board;
    useReviewUi.setState({ ...readReviewTime(board), ...patch } as never);
    // 覆盖度也必须按板面取：窗口锚在账本水位上，拿错账本会画出一个不存在的周期。
    fetchReviewCoverage({ board: useReviewUi.getState().board })
      .then((c) => {
        if (alive) setCov((c as unknown as ReviewCoverage) ?? undefined);
      })
      .catch(() => undefined)
      .finally(() => {
        if (alive) setBooted(true);
      });
    return () => {
      alive = false;
    };
  }, [location.search]);

  // 切规则版本 = 换账本 → 水位也换了。先把覆盖度重新取一遍，否则第一次查询会用
  // 上一本账本的水位算窗口，画出一个该版本里并不存在的周期。
  useEffect(() => {
    if (!booted) return;
    let alive = true;
    fetchReviewCoverage({ board: ui.board })
      .then((c) => {
        if (alive) setCov((c as unknown as ReviewCoverage) ?? undefined);
      })
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, [ui.board, booted]);

  const anchor = cov?.watermark_ts || cov?.last_ts || undefined;

  /**
   * 该规则版本是否有时间区。
   *
   * 有周期时（v2.0.0 / 选币榜Y）窗口必须落在周期栅格上：24 小时重置会在 00:00 UTC
   * 清空全部分区，「水位往前推 N×24h」的滚动窗口会从半夜切开两个周期 ——
   * 既漏掉当前周期的头几个小时，又把上一个周期的尾巴和它的重置强平算进来。
   * 实测同一时刻两种口径的均值连正负号都不一样。
   */
  const cyc = cov?.cycle?.enabled ? cov.cycle : undefined;
  /** 走周期栅格：非自定义窗口 + 该版本有周期。窗口由服务端算，前端只负责显示。 */
  const cycleMode = Boolean(cyc) && !ui.useCustom;

  const windowOf = useCallback((): { from?: string; to?: string } => {
    if (ui.useCustom && ui.customFrom && ui.customTo) {
      return { from: ui.customFrom, to: ui.customTo };
    }
    return windowFromDays(anchor, ui.rangeDays);
  }, [ui.useCustom, ui.customFrom, ui.customTo, ui.rangeDays, anchor]);

  const buildQuery = useCallback((): ReviewQuery => {
    const w = windowOf();
    return {
      // 规则版本 → 账本。v1.4.0 = 选币榜，v2.0.0 = 选币榜Y。
      board: ui.board,
      zones: ui.zones,
      direction: ui.direction,
      symbols: parseSymbols(ui.search),
      // 有时间区就发 cycles=N，让服务端按周期栅格算窗口（栅格只有那一处实现）；
      // 没有时间区（v1.4.0）仍走原来的 from/to，行为逐字节不变。
      ...(cycleMode
        ? { cycles: ui.rangeDays, whole_cycles: ui.wholeCycles }
        : { from: w.from, to: w.to }),
      attribution: ui.attribution,
      include_open: ui.includeOpen,
      only_flagged: ui.onlyFlagged,
      // 周期重置强平不是策略的退出决策 —— 勾上就把它们从胜率/均值/明细里全部拿掉。
      exclude_flags: ui.excludeCycleReset ? ['CYCLE_RESET'] : undefined,
      pv: ui.paramVersion,
      // 调参段（param_hash）。同一 parameter_version 内部，216 主导层上线前后
      // 是两段不可比的数据 —— 不带这个参数，页面上的胜率就是两段混算的。
      ph: ui.paramHash,
      min_dwell_nodes: ui.minDwellNodes,
      sort: ui.sortId,
      desc: ui.sortDesc,
      limit: 1000,
    };
  }, [
    ui.board,
    ui.zones,
    ui.direction,
    ui.search,
    cycleMode,
    ui.rangeDays,
    ui.wholeCycles,
    ui.attribution,
    ui.includeOpen,
    ui.onlyFlagged,
    ui.paramHash,
    ui.excludeCycleReset,
    ui.paramVersion,
    ui.minDwellNodes,
    ui.sortId,
    ui.sortDesc,
    windowOf,
  ]);

  const reload = useCallback(async () => {
    const q = buildQuery();
    try {
      const body = await fetchReviewTrades(q);
      setPack(body);
      setCov(body.coverage);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }

    const wanted = ui.compareOn ? ui.comparePeriods.filter((p) => p.from && p.to) : [];
    if (!wanted.length) {
      setCompare([]);
      return;
    }
    const results = await Promise.all(
      wanted.map(async (p, i): Promise<ComparePeriodResult> => {
        try {
          // 对比周期是显式起止，必须把 cycles 关掉，否则服务端会拿周期栅格覆盖它。
          const c = await fetchReviewSummary({
            ...q,
            cycles: undefined,
            whole_cycles: undefined,
            from: p.from,
            to: p.to,
          });
          return { id: p.id, label: p.label || `周期 ${i + 2}`, from: p.from, to: p.to, summary: c.summary };
        } catch (e) {
          return { id: p.id, label: p.label || `周期 ${i + 2}`, from: p.from, to: p.to, error: (e as Error).message };
        }
      }),
    );
    setCompare(results);
  }, [buildQuery, ui.compareOn, ui.comparePeriods]);

  useEffect(() => {
    if (!booted) return;
    void reload();
  }, [reload, booted]);

  useEffect(() => {
    if (!booted) return;
    // 订阅**当前规则版本对应的板面**通道：看 v2.0.0 的账本时，应该由选币榜Y 的
    // 新节点触发刷新，而不是选币榜的。
    const unsub = subscribeScreenerEvents(
      (ev) => {
        if (ev.type === 'screener.updated' || ev.type === 'confirmed.changed') void reload();
      },
      { board: ui.board },
    );
    return unsub;
  }, [reload, booted, ui.board]);

  // ---- store → URL, replace so the back button is not spammed by filter clicks
  useEffect(() => {
    const search = stateToSearch({
      board: ui.board,
      zones: ui.zones,
      direction: ui.direction,
      attribution: ui.attribution,
      zoneMode: ui.zoneMode,
      rangeDays: ui.rangeDays,
      useCustom: ui.useCustom,
      customFrom: ui.customFrom,
      customTo: ui.customTo,
      comparePeriods: ui.comparePeriods,
      compareOn: ui.compareOn,
      search: ui.search,
      includeOpen: ui.includeOpen,
      onlyFlagged: ui.onlyFlagged,
      excludeCycleReset: ui.excludeCycleReset,
      wholeCycles: ui.wholeCycles,
      minDwellNodes: ui.minDwellNodes,
      columnPreset: ui.columnPreset,
      paramVersion: ui.paramVersion,
      paramHash: ui.paramHash,
      sortId: ui.sortId,
      sortDesc: ui.sortDesc,
    });
    if (!booted) return;
    if (`?${search}` !== location.search) navigate({ search }, { replace: true });
  }, [
    ui.board,
    ui.zones, ui.direction, ui.attribution, ui.zoneMode, ui.rangeDays, ui.useCustom,
    ui.customFrom, ui.customTo, ui.comparePeriods, ui.compareOn, ui.search, ui.includeOpen,
    ui.onlyFlagged, ui.excludeCycleReset, ui.wholeCycles, ui.minDwellNodes, ui.columnPreset,
    ui.paramVersion,
    ui.sortId, ui.sortDesc,
    navigate, location.search, booted,
  ]);

  // Rows arrive already ordered by the ledger (whole window, not one page).
  const allRows = useMemo<ReviewTrade[]>(() => pack?.trades || [], [pack]);
  const closedRows = useMemo(() => allRows.filter((t) => t.status === 'CLOSED'), [allRows]);
  const openRows = useMemo(() => allRows.filter((t) => t.status === 'OPEN'), [allRows]);

  const summary = pack?.summary;
  // 本窗口内有多少笔是周期重置强平（v1.4.0 的账本恒为 0 —— 它没有周期）
  const cycleReset = ui.excludeCycleReset
    ? -1 // 已排除时保持开关可见，否则一勾上开关自己就消失了
    : (summary?.flag_counts?.CYCLE_RESET ?? 0);
  const total = pack?.total ?? allRows.length;
  const truncated = Boolean(pack?.truncated);
  // 周期模式下窗口是服务端算的，回读它 —— 前端另算一个只会和服务端对不上。
  const cw = (pack?.filters?.cycle_window as ReviewCycleWindow | null | undefined) ?? undefined;
  const win = cw ? { from: cw.from, to: cw.to } : windowOf();

  const rangeLabel = ui.useCustom
    ? '自定义'
    : cycleMode
      ? ui.rangeDays === 1
        ? ui.wholeCycles
          ? '上一完整周期'
          : '本周期'
        : `近 ${ui.rangeDays} 个周期${ui.wholeCycles ? '（完整）' : ''}`
      : `近 ${ui.rangeDays} 天`;

  const mainPeriod: ComparePeriodResult = {
    id: 'main',
    label: rangeLabel,
    from: win.from,
    to: win.to,
    summary,
  };

  const exportCsv = () => {
    const stamp = (win.to || anchor || '').replace(/[-:]/g, '').slice(0, 13);
    const span = cw ? `${cw.first_cycle_key}-${cw.last_cycle_key}` : stamp || 'window';
    downloadCsv(
      `review_${BOARDS[ui.board].rulesetLabel}_${ui.zones.join('-')}_${span}.csv`,
      tradesToCsv(allRows),
    );
  };

  if (loading || !booted) return <div className="page-status">加载复盘账本…</div>;

  return (
    <div className={`screener-page review-page${embedded ? ' embedded' : ''}`}>
      <header className="header-strip">
        <div className="header-row">
          <div className="brand">
            <span className="brand-mark">复盘</span>
            <div>
              <div className="brand-title">
                复盘选币 · 历史进出账本
                <span className="muted">
                  {' '}
                  · {BOARDS[ui.board].rulesetLabel} {BOARDS[ui.board].label}
                </span>
              </div>
              <div className="brand-sub">
                已实现盈亏（退出价 vs 停留价）· 笔数不去重 · {cov?.parameter_version || '—'}
              </div>
            </div>
          </div>
          <div className="header-metrics">
            <div className="metric">
              <div className="metric-label">账本节点</div>
              <div className="metric-value" title={dualClock(cov?.watermark_ts)}>
                {cov?.watermark_scan_id || '—'} · {cov?.watermark_ts ? formatEnterClock(cov.watermark_ts) : '—'}
              </div>
            </div>
            <div className="metric">
              <div className="metric-label">物理覆盖</div>
              <div className="metric-value" title={cov?.first_ts ? `自 ${dualClock(cov.first_ts)}` : undefined}>
                {cov?.available_days != null ? `${cov.available_days.toFixed(2)} 天` : '—'}
                {cov?.nodes_ingested ? <span className="muted"> · {cov.nodes_ingested} 节点</span> : null}
              </div>
            </div>
            <div className="metric">
              <div className="metric-label">账本行数</div>
              <div className="metric-value">
                {cov?.closed_rows ?? '—'} 平 / {cov?.open_rows ?? '—'} 未平
              </div>
            </div>
            <div className="metric">
              <div className="metric-label">口径</div>
              <div className="metric-value">
                {ui.attribution === 'exit' ? '按退出时间' : ui.attribution === 'enter' ? '按入选时间' : '完全包含'}
              </div>
            </div>
          </div>
        </div>
      </header>

      {error && (
        <div className="review-banner error">
          ⚠ 取数失败：{error}
          <button type="button" className="linkish" onClick={() => void reload()}>
            重试
          </button>
          <span className="muted">
            {' '}
            账本未建时先跑{' '}
            {ui.board === 'main'
              ? 'python3 scripts/build_review_ledger.py --reset'
              : `python3 scripts/replay_screener_y.py --board ${ui.board}`}
          </span>
        </div>
      )}

      {/* 规则版本切换：v1.4.0（选币榜）↔ v2.0.0（选币榜Y）。换的是整本账本。 */}
      <ReviewBoardBar value={ui.board} onChange={ui.setBoard} cov={cov} error={error} />
      {/* 周期栅格条：只有启用了 24h 周期的规则版本（v2.0.0）才出现。
          v1.4.0 的 coverage.cycle 是 {enabled:false}，这里返回 null。 */}
      {cycleMode && (
        <CycleWindowBar
          cycle={cyc}
          window={cw}
          wholeCycles={ui.wholeCycles}
          onToggleWhole={ui.setWholeCycles}
        />
      )}
      <CoverageBanner cov={cov} zones={ui.zones} />
      <ParamVersionBar cov={cov} summary={summary} value={ui.paramVersion} onChange={ui.lockParamVersion} />
      {/* 调参段在参数版本之下：先选哪套标准，再选那套标准的哪一次调参。 */}
      <ParamHashBar cov={cov} summary={summary} value={ui.paramHash} onChange={ui.lockParamHash} />

      <div className="review-cards">
        <SummaryCard title={`${rangeLabel} · 合并`} s={summary} />
        <SummaryCard title="上涨候选池" s={summary?.by_direction?.up} compact />
        <SummaryCard title="下跌候选池" s={summary?.by_direction?.down} compact />
        <ControlBenchmarkCard s={summary} />
      </div>

      {/* 分区统计卡：只要选中了分区就出，单选一个区也要出（并列 / 合并模式都出）。
          以前挂了 zoneMode==='parallel' && zones.length>1 两个条件，导致
          「只点 DMR」或切到「合并」时这块整个消失，看不到该区自己的统计。 */}
      {summary?.by_zone && ui.zones.length >= 1 && (
        <div className="review-cards compact">
          {ui.zones.map((z) => (
            <SummaryCard
              key={z}
              title={REVIEW_ZONE_LABEL[z]}
              s={summary.by_zone?.[z]}
              compact
              badge={CONTROL_ZONES.includes(z) ? '对照' : undefined}
            />
          ))}
        </div>
      )}

      <div className="chips">
        {REVIEW_ZONES.map((z) => {
          const n = summary?.chip_counts?.[z] ?? summary?.by_zone?.[z]?.trades;
          const on = ui.zones.includes(z);
          const control = CONTROL_ZONES.includes(z);
          return (
            <button
              key={z}
              type="button"
              className={`chip state-${z === 'DMR' ? 'dmr' : z.toLowerCase()} ${on ? 'on' : ''}`}
              onClick={() => ui.toggleZone(z)}
              title={control ? '对照组：反向基准，不代表可执行策略' : '当前周期内的完整进出笔数（不是当刻占用）'}
            >
              <span className="chip-dot" />
              {REVIEW_ZONE_LABEL[z]}
              {control ? <i className="path-tag">对照</i> : null}
              <b>{n ?? '—'}</b>
            </button>
          );
        })}
      </div>

      <div className="screener-controls review-controls">
        <div className="dir-tabs">
          {(
            [
              ['up', '上涨候选池', summary?.by_direction?.up?.trades ?? summary?.up],
              ['down', '下跌候选池', summary?.by_direction?.down?.trades ?? summary?.down],
              ['both', '合并', summary?.trades],
            ] as const
          ).map(([id, label, n]) => (
            <button
              key={id}
              type="button"
              className={`dir-tab ${id === 'up' ? 'up' : id === 'down' ? 'down' : ''} ${ui.direction === id ? 'on' : ''}`}
              title="复盘数字是完整进出笔数，不是唯一币种——与选币榜相反"
              onClick={() => ui.setDirection(id)}
            >
              {label} <span>{n ?? 0}</span>
            </button>
          ))}
        </div>
        <div className="toolbar">
          <div className="review-ranges">
            {([1, 7, 14, 28] as const).map((d) => (
              <button
                key={d}
                type="button"
                className={`grade-btn ${!ui.useCustom && ui.rangeDays === d ? 'on' : ''}`}
                onClick={() => ui.setRange(d)}
                title={
                  cycleMode
                    ? '窗口对齐到 24h 周期边界（00:00 UTC），与选币榜Y 的重置刻度同一张栅格'
                    : '自水位往前推 N×24h 的滚动窗口'
                }
              >
                {/* 有时间区的规则版本按「周期」计数：滚动 N 天会从半夜切开两个周期。 */}
                {cycleMode ? (d === 1 ? (ui.wholeCycles ? '上一周期' : '本周期') : `近 ${d} 周期`) : `近 ${d} 天`}
              </button>
            ))}
            {cycleMode && (
              <label
                className="toolbar-label"
                title="进行中的周期只跑了几个小时，和完整的 24 小时周期一起平均会混淆分母口径。做周期间对比时建议打开。"
              >
                <input
                  type="checkbox"
                  checked={ui.wholeCycles}
                  onChange={(e) => ui.setWholeCycles(e.target.checked)}
                />{' '}
                只算完整周期
              </label>
            )}
          </div>
          <label className="toolbar-label">
            口径
            <select
              value={ui.attribution}
              onChange={(e) => ui.setAttribution(e.target.value as typeof ui.attribution)}
              title="切换口径笔数会变，这是正常的：同一笔交易只归属一个周期，不拆仓"
            >
              <option value="exit">按退出时间</option>
              <option value="enter">按入选时间</option>
              <option value="contained">完全包含</option>
            </select>
          </label>
          <label className="toolbar-label">
            分区
            <select value={ui.zoneMode} onChange={(e) => ui.setZoneMode(e.target.value as typeof ui.zoneMode)}>
              <option value="parallel">并列</option>
              <option value="merge">合并</option>
            </select>
          </label>
          <label className="toolbar-label">
            预设
            <select value={ui.preset} onChange={(e) => ui.setPreset(e.target.value as typeof ui.preset)}>
              <option value="executable">可执行区</option>
              <option value="funnel">全晋级链</option>
              <option value="control">对照组</option>
              <option value="custom" disabled>
                自定义
              </option>
            </select>
          </label>
          <label className="toolbar-label">
            列
            <select
              value={ui.columnPreset}
              onChange={(e) => ui.setColumnPreset(e.target.value as typeof ui.columnPreset)}
            >
              <option value="compact">精简</option>
              <option value="trader">交易员</option>
              <option value="full">完整</option>
            </select>
          </label>
          <label className="toolbar-label">
            最短停留
            <select
              value={String(ui.minDwellNodes)}
              onChange={(e) => ui.setMinDwell(Number(e.target.value))}
              title="剥离 DMR Top-K 边界抖动用；默认不过滤，因为那些进出真实发生过"
            >
              <option value="0">不限</option>
              <option value="2">≥2 节点</option>
              <option value="4">≥4 节点</option>
              <option value="8">≥8 节点</option>
            </select>
          </label>
          <label className="toolbar-label">
            <input type="checkbox" checked={ui.includeOpen} onChange={(e) => ui.setIncludeOpen(e.target.checked)} />{' '}
            显示未平仓
          </label>
          <label className="toolbar-label" title="只看带旗标的记录：缺价 / 板面消失 / 节点空洞 / 跨版本">
            <input type="checkbox" checked={ui.onlyFlagged} onChange={(e) => ui.setOnlyFlagged(e.target.checked)} />{' '}
            仅显示异常
          </label>
          {/* 只在有 24h 周期的规则版本下出现：v1.4.0 的账本里一条 CYCLE_RESET 都没有，
              摆一个永远筛不到东西的开关只会让人以为数据不对。 */}
          {/* X v1.3.0 无周期，与 main v1.4.0 复刻同源复盘；未来只经 X overrides 演进。 */}
          {(cycleReset > 0 || BOARDS[ui.board].cycleEnabled) && (
            <label
              className="toolbar-label"
              title="周期重置强平不是策略的退出决策。勾上后胜率 / 均值 / 明细都只算策略自己走的那些笔。"
            >
              <input
                type="checkbox"
                checked={ui.excludeCycleReset}
                onChange={(e) => ui.setExcludeCycleReset(e.target.checked)}
              />{' '}
              排除周期重置强平
              {cycleReset > 0 && <span className="muted"> ({cycleReset})</span>}
            </label>
          )}
          <input
            className="search"
            placeholder="币种：HYPE, ETH 或粘贴名单"
            value={ui.search}
            onChange={(e) => ui.setSearch(e.target.value)}
          />
          <button type="button" className="grade-btn" onClick={exportCsv} title="导出当前筛选结果的全部字段（含 flags）">
            导出 CSV
          </button>
        </div>
      </div>

      <div className="review-custom-range">
        <span className="muted">自定义起止（本地 UTC+7，提交按 UTC）</span>
        {/* 手填的起止是自由的，可能从半夜切开两个周期 —— 那正是这次要修的毛病。
            所以有时间区时必须当面提示，并给一键吸附。 */}
        {cyc?.enabled && ui.useCustom && !windowAligned(ui.customFrom, ui.customTo, cyc) && (
          <span className="review-misaligned">
            ⚠ 该窗口没有落在 {cyc.period_hours ?? 24}h 周期边界上，会从中间切开周期
            <button
              type="button"
              className="linkish"
              onClick={() => {
                const w = snapWindow(ui.customFrom, ui.customTo, cyc);
                ui.setCustom(w.from, w.to);
              }}
            >
              对齐到周期
            </button>
          </span>
        )}
        <RangeInput
          ariaLabel="自定义起点"
          value={ui.customFrom}
          onChange={(iso) => ui.setCustom(iso, ui.customTo)}
        />
        <span>—</span>
        <RangeInput ariaLabel="自定义终点" value={ui.customTo} onChange={(iso) => ui.setCustom(ui.customFrom, iso)} />
        {(ui.customFrom || ui.customTo) && (
          <button type="button" className="linkish" onClick={ui.clearCustom}>
            清除自定义
          </button>
        )}
        {(ui.customFrom || ui.customTo) && !ui.useCustom && (
          <span className="muted">
            {ui.customFrom && ui.customTo ? (
              <>（预填时间，当前仍按快捷周期统计）<button type="button" className="linkish" onClick={() => ui.setCustom(ui.customFrom, ui.customTo)}>应用自定义</button></>
            ) : '（起止都填齐后生效）'}
          </span>
        )}
        <span className="review-range-sep" />
        <label className="toolbar-label">
          <input type="checkbox" checked={ui.compareOn} onChange={ui.toggleCompare} /> 周期对比
        </label>
        {ui.compareOn && (
          <>
            {ui.comparePeriods.map((p, i) => (
              <span key={p.id} className="review-compare-period">
                <b>周期 {i + 2}</b>
                {cyc?.enabled && p.from && p.to && !windowAligned(p.from, p.to, cyc) && (
                  <button
                    type="button"
                    className="linkish review-misaligned"
                    title={`该对比周期没有落在 ${cyc.period_hours ?? 24}h 周期边界上，与主周期口径不一致`}
                    onClick={() => ui.updateComparePeriod(p.id, snapWindow(p.from, p.to, cyc))}
                  >
                    ⚠ 对齐
                  </button>
                )}
                <RangeInput
                  ariaLabel={`对比周期 ${i + 2} 起点`}
                  value={p.from}
                  onChange={(iso) => ui.updateComparePeriod(p.id, { from: iso })}
                />
                <RangeInput
                  ariaLabel={`对比周期 ${i + 2} 终点`}
                  value={p.to}
                  onChange={(iso) => ui.updateComparePeriod(p.id, { to: iso })}
                />
                <button type="button" className="linkish" onClick={() => ui.removeComparePeriod(p.id)}>
                  ×
                </button>
              </span>
            ))}
            {ui.comparePeriods.length < MAX_COMPARE_PERIODS && (
              <button type="button" className="linkish" onClick={() => ui.addComparePeriod()}>
                + 添加周期
              </button>
            )}
          </>
        )}
      </div>

      <PeriodCompareTable periods={[mainPeriod, ...compare]} />

      <div className="toolbar-meta review-meta">
        显示 <b>{closedRows.length}</b> 笔
        {truncated ? (
          <span className="review-truncated"> / 共 {total} 笔（已截断，请缩小周期或收窄分区）</span>
        ) : (
          <span className="muted"> / 共 {total} 笔</span>
        )}
        {' · '}真实交易 {summary?.trades ?? 0} · 有效 {summary?.usable ?? 0} · 唯一币 {summary?.unique_coins ?? 0} · 未平仓{' '}
        {summary?.open_trades ?? 0}
        {summary?.missing_px ? ` · 排除缺价 ${summary.missing_px}` : ' · 排除 0'}
        {ui.minDwellNodes > 0 ? ` · 已隐藏 <${ui.minDwellNodes} 节点的短停留` : ''}
        {ui.excludeCycleReset
          ? ' · 已排除周期重置强平（只看策略自己的退出）'
          : cycleReset > 0
            ? ` · 含周期重置强平 ${cycleReset} 笔（24h 循环清空所致，非策略退出）`
            : ''}
        <span className="muted"> · 盈亏不含手续费/资金费/滑点，只衡量选币方向命中</span>
      </div>

      <ReviewDataGrid rows={closedRows} columnPreset={ui.columnPreset} onPickSymbol={ui.setSearch} />

      {ui.includeOpen && <OpenTradesPanel rows={openRows} total={summary?.open_trades ?? openRows.length} />}

      <footer className="attr-footer">Powered by CoinGecko · 复盘账本只读消费选币快照</footer>
    </div>
  );
}
