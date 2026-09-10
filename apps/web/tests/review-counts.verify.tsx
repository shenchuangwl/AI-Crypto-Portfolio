/**
 * 复盘选币验证闸。
 *
 *   cd apps/web && npm run verify:review-counts
 *
 * 覆盖：已实现盈亏公式、顶栏插入位置、25 列含退出时间/退出价、
 * DMR 入价不得误用确认区停留价（纯函数层）、OPEN 无 pnl。
 */
import { readFileSync } from 'node:fs';
import { renderToStaticMarkup } from 'react-dom/server';
import { MemoryRouter } from 'react-router-dom';
import { AppNav } from '../src/components/AppNav';
import { ReviewPage } from '../src/pages/ReviewPage';
import { CoverageBanner } from '../src/features/review/CoverageBanner';
import { PeriodCompareTable } from '../src/features/review/PeriodCompareTable';
import { SummaryCard, ControlBenchmarkCard } from '../src/features/review/ReviewSummaryCards';
import * as SummaryCardsMod from '../src/features/review/ReviewSummaryCards';
import { ParamVersionBar, shortVersion, versionSpan } from '../src/features/review/ParamVersionBar';
import { reviewQueryString } from '../src/shared/api/review';
import { ReviewDataGrid } from '../src/features/review/ReviewDataGrid';
import type { ReviewCoverage, ReviewSummary } from '../src/shared/types/review';
import { COLUMN_PRESETS, reviewColumns } from '../src/features/review/ReviewDataGrid';
import { realizedPnl } from '../src/shared/lib/reviewPnl';
import { stayPnl, stayPriceOf } from '../src/shared/lib/stayPnl';
import type { CandidateRow } from '../src/shared/types/screener';
import { REVIEW_SORT_KEYS, REVIEW_FLAG_LABEL } from '../src/shared/types/review';
import { presetOf, useReviewUi } from '../src/shared/stores/reviewUi';
import { searchToState, stateToSearch } from '../src/features/review/reviewQuery';
import { dualClock, localInputToUtcIso, utcIsoToLocalInput } from '../src/shared/lib/reviewTime';
import { tradesToCsv } from '../src/shared/lib/reviewCsv';
import type { ReviewTrade } from '../src/shared/types/review';

let failures = 0;
function check(name: string, ok: boolean, extra = '') {
  console.log((ok ? '  ok   ' : '  FAIL ') + name + (extra ? '  ' + extra : ''));
  if (!ok) failures += 1;
}

console.log('\n=== 1. realizedPnl ===');
{
  const a = realizedPnl('up', 100, 110);
  check('上涨 100→110 = +10% / 1', !!a && Math.abs(a.pct - 0.1) < 1e-12 && a.sign === 1);
  const b = realizedPnl('down', 100, 90);
  check('下跌 100→90 = +10% / 1', !!b && Math.abs(b.pct - 0.1) < 1e-12 && b.sign === 1);
  const c = realizedPnl('down', 100, 110);
  check('下跌 100→110 = −10% / -1', !!c && Math.abs(c.pct + 0.1) < 1e-12 && c.sign === -1);
  const d = realizedPnl('up', 50, 50);
  check('持平 → 0 / 0', !!d && d.pct === 0 && d.sign === 0);
  check('停留价 0 → null', realizedPnl('up', 0, 1) === null);
  check('缺退出价 → null', realizedPnl('up', 1, null) === null);
}

console.log('\n=== 1b. stayPriceOf 与展示停留价同口径 ===');
{
  const y = {
    state: 'CONFIRMED',
    state_enter_price: 1.361,
    zone_enter_price: 1.3525,
    last_price: 1.3433,
  } as CandidateRow;
  check('Y 用 zone_enter 作停留价', stayPriceOf(y) === 1.3525);
  const yp = stayPnl(1.3433, stayPriceOf(y), 'up');
  check(
    'Y 上涨盈亏 = last/zone_enter−1',
    !!yp && Math.abs(yp.pct - (1.3433 / 1.3525 - 1)) < 1e-12 && yp.sign === -1,
  );
  const main = {
    state: 'CONFIRMED',
    state_enter_price: 0.46181,
    last_price: 0.45622,
  } as CandidateRow;
  check('v1.4.0 无 zone_enter 回退 state_enter', stayPriceOf(main) === 0.46181);
  const short = stayPnl(0.22961, 0.24165, 'down');
  check(
    '下跌 停留 0.24165 → last 0.22961 = +4.98% / 1',
    !!short && Math.abs(short.pct - (1 - 0.22961 / 0.24165)) < 1e-12 && short.sign === 1,
  );
  check('淘汰无停留价', stayPriceOf({ state: 'ELIMINATED', state_enter_price: 1 } as CandidateRow) === null);
}

console.log('\n=== 2. 顶栏位置 ===');
{
  const html = renderToStaticMarkup(
    <MemoryRouter>
      <AppNav />
    </MemoryRouter>,
  );
  const iQuotes = html.indexOf('行情');
  const iTerm = html.indexOf('工作台');
  const iScr = html.indexOf('选币榜');
  const iRev = html.indexOf('复盘选币');
  const iMkt = html.indexOf('合约清单');
  check('五栏都在', iQuotes >= 0 && iTerm >= 0 && iScr >= 0 && iRev >= 0 && iMkt >= 0);
  check('复盘在选币榜之后、合约清单之前', iScr < iRev && iRev < iMkt);
  check('链到 /review', html.includes('to="/review"') || html.includes('href="/review"'));
}

console.log('\n=== 3. 状态栏列 ===');
{
  const cols = reviewColumns();
  const labels = cols.map((c) => c.label);
  const need = [
    '合约',
    '状态',
    '入选时间',
    '停留时间',
    '停留价格',
    '实时价格',
    '退出时间',
    '退出价格',
    'Score',
    'DirConf',
    '等级',
    '30m流通市值',
    '2h流通市值',
    '6h流通市值',
    '1h',
    '4h',
    '24h',
    '1Week',
    '1Month',
    '锚点以来',
    '盈亏百分比',
    '盈亏数值',
    '风险',
    '入选原因',
  ];
  for (const lab of need) {
    check(`列 ${lab}`, labels.includes(lab));
  }
  const iStay = labels.indexOf('停留价格');
  const iLive = labels.indexOf('实时价格');
  const iExitT = labels.indexOf('退出时间');
  const iExitP = labels.indexOf('退出价格');
  const iPnl = labels.indexOf('盈亏百分比');
  check('停留价 → 实时价 → 退出时间 → 退出价', iStay < iLive && iLive < iExitT && iExitT < iExitP);
  check('盈亏百分比在退出价之后', iExitP < iPnl);
}

console.log('\n=== 4. 时间栅格：datetime-local ⇄ UTC ===');
{
  const utc = '2026-08-17T00:00:00Z';
  const shown = utcIsoToLocalInput(utc);
  check('UTC 00:00Z → 本地 07:00', shown === '2026-08-17T07:00', shown);
  check('本地 07:00 → UTC 00:00Z', localInputToUtcIso(shown) === utc, localInputToUtcIso(shown));
  let round = utc;
  for (let i = 0; i < 5; i += 1) round = localInputToUtcIso(utcIsoToLocalInput(round));
  check('反复往返不漂移', round === utc, round);
  check('清空输入返回空串而不抛错', localInputToUtcIso('') === '');
  check('非法输入返回空串而不抛错', localInputToUtcIso('garbage') === '');
  check('空 ISO 显示为空', utcIsoToLocalInput(null) === '');
  check('双时钟带 UTC+7 与 UTC', dualClock(utc) === '2026-08-17 07:00 (UTC+7) · 00:00 UTC', dualClock(utc));
}

console.log('\n=== 5. 排序：可点列必须服务端可排 ===');
{
  const cols = reviewColumns();
  const sortable = cols.filter((c) => c.sortId);
  const bad = sortable.filter((c) => !(REVIEW_SORT_KEYS as readonly string[]).includes(c.sortId as string));
  check('每个可点列都在服务端白名单内', bad.length === 0, bad.map((c) => c.id).join(','));
  check('盈亏百分比可排序', sortable.some((c) => c.sortId === 'pnl_pct'));
  check('退出时间可排序', sortable.some((c) => c.sortId === 'exit_time_utc'));
  const fake = cols.filter((c) => !c.sortId).map((c) => c.id);
  check('不可排序列不声明 sortId（不假装）', fake.includes('live') && fake.includes('grade'), fake.join(','));
}

console.log('\n=== 6. 列预设 ===');
{
  const ids = reviewColumns().map((c) => c.id);
  for (const [name, keep] of Object.entries(COLUMN_PRESETS)) {
    if (keep === null) {
      check(`预设 ${name} = 全列`, true, `${ids.length} 列`);
      continue;
    }
    const unknown = keep.filter((k) => !ids.includes(k));
    check(`预设 ${name} 无未知列`, unknown.length === 0, unknown.join(','));
    for (const must of ['symbol', 'enter_time_utc', 'exit_time_utc', 'pnl_pct', 'pnl_sign']) {
      check(`预设 ${name} 含 ${must}`, keep.includes(must));
    }
  }
}

console.log('\n=== 7. 分区选择与预设回落 ===');
{
  useReviewUi.getState().reset();
  check('默认 = DMR + 确认', presetOf(useReviewUi.getState().zones) === 'executable');
  useReviewUi.getState().toggleZone('QUALIFIED');
  check('手改分区后预设 = 自定义', useReviewUi.getState().preset === 'custom', useReviewUi.getState().preset);
  useReviewUi.getState().toggleZone('QUALIFIED');
  check('改回来预设回落 = 可执行区', useReviewUi.getState().preset === 'executable');
  useReviewUi.getState().setPreset('funnel');
  check('全晋级链 4 区', useReviewUi.getState().zones.length === 4);
  useReviewUi.getState().toggleZone('DMR');
  useReviewUi.getState().toggleZone('CONFIRMED');
  useReviewUi.getState().toggleZone('QUALIFIED');
  useReviewUi.getState().toggleZone('WATCH');
  check('最后一枚不许关', useReviewUi.getState().zones.length === 1, useReviewUi.getState().zones.join(','));
  useReviewUi.getState().reset();
  useReviewUi.getState().setCustom('2026-08-20T00:00:00Z', '');
  check('半填自定义不生效', useReviewUi.getState().useCustom === false);
  useReviewUi.getState().setCustom('2026-08-20T00:00:00Z', '2026-08-21T00:00:00Z');
  check('起止填齐才生效', useReviewUi.getState().useCustom === true);
  useReviewUi.getState().reset();
}

console.log('\n=== 8. URL 往返 ===');
{
  const state = {
    zones: ['DMR', 'CONFIRMED', 'QUALIFIED'],
    direction: 'down',
    attribution: 'enter',
    zoneMode: 'merge',
    rangeDays: 14,
    useCustom: false,
    customFrom: '',
    customTo: '',
    comparePeriods: [{ id: 'p1', from: '2026-08-20T00:00:00Z', to: '2026-08-21T00:00:00Z' }],
    compareOn: true,
    search: 'HYPE,ETH',
    includeOpen: true,
    onlyFlagged: true,
    minDwellNodes: 2,
    columnPreset: 'trader',
    sortId: 'pnl_pct',
    sortDesc: true,
  } as Parameters<typeof stateToSearch>[0];
  const round = searchToState(stateToSearch(state));
  check('zones 往返', (round.zones || []).join(',') === 'DMR,CONFIRMED,QUALIFIED', String(round.zones));
  check('候选池往返', round.direction === 'down');
  check('口径往返', round.attribution === 'enter');
  check('分区模式往返', round.zoneMode === 'merge');
  check('周期往返', round.rangeDays === 14);
  check('对比周期往返', (round.comparePeriods || []).length === 1);
  check('币种往返', round.search === 'HYPE,ETH');
  check('未平仓/异常/停留往返', round.includeOpen === true && round.onlyFlagged === true && round.minDwellNodes === 2);
  check('列预设往返', round.columnPreset === 'trader');
  check('排序往返', round.sortId === 'pnl_pct' && round.sortDesc === true);
  const junk = searchToState('?zones=NOPE&range=999&sort=;DROP&cols=hack');
  check('非法 URL 参数被丢弃而不是套用', !junk.zones && !junk.rangeDays && !junk.sortId && !junk.columnPreset);
}

console.log('\n=== 9. CSV 导出 ===');
{
  const t = {
    trade_id: 'HYPEUSDT|up|DMR|20260822-014',
    symbol: 'HYPEUSDT',
    canonical_asset_id: 'HYPE',
    direction: 'up',
    zone: 'DMR',
    status: 'CLOSED',
    enter_time_utc: '2026-08-22T03:30:00Z',
    exit_time_utc: '2026-08-22T05:15:00Z',
    pnl_pct: 0.0231,
    pnl_sign: 1,
    reason_codes: ['G1_PASS', 'STAIRCASE, OK'],
    flags: ['TRUNCATED_ENTER'],
  } as unknown as ReviewTrade;
  const csv = tradesToCsv([t]);
  const lines = csv.replace(/^\uFEFF/, '').trim().split('\n');
  check('表头含 flags', lines[0].includes('flags'));
  check('盈亏导出原始小数（不是展示值）', lines[1].includes('0.0231'), lines[1].slice(0, 120));
  check('含逗号的字段被正确转义', lines[1].includes('"G1_PASS|STAIRCASE, OK"'), lines[1]);
  check(
    '未平仓行导出空退出列',
    tradesToCsv([{ ...t, status: 'OPEN', exit_time_utc: null, pnl_pct: null } as ReviewTrade]).includes(',,'),
  );
}

// 第 11/12 节复用这两个夹具，提到块外声明。
let cov: ReviewCoverage;
let s: ReviewSummary;

console.log('\n=== 10. 首屏渲染冒烟（不得在 render 阶段抛错）===');
{
  // fetch 在 node 里不可用；页面在 booted=false 时必须先出加载态而不是崩。
  (globalThis as { fetch?: unknown }).fetch = () => new Promise(() => {});
  let html = '';
  let crashed = '';
  try {
    html = renderToStaticMarkup(
      <MemoryRouter initialEntries={['/review?zones=DMR,CONFIRMED&range=7']}>
        <ReviewPage />
      </MemoryRouter>,
    );
  } catch (e) {
    crashed = (e as Error).message;
  }
  check('ReviewPage 首帧不抛错', crashed === '', crashed);
  check('首帧是加载态（等覆盖度到达再查）', html.includes('加载复盘账本'), html.slice(0, 120));

  cov = {
    watermark_scan_id: '20260824-044',
    watermark_ts: '2026-08-24T11:00:00Z',
    first_ts: '2026-08-15T00:24:16Z',
    last_ts: '2026-08-24T11:00:00Z',
    requested_days: 28,
    available_days: 9.44,
    short_zones: ['DMR'],
    zone_available: { DMR: { from: '2026-08-21T17:28:25Z', days: 2.72 } },
    freshness: { stale: true, latest_scan_id: '20260824-045', lag_nodes: 1, lag_minutes: 15 },
    parameter_segments: [
      { version: 'param-v1.2.0-g1g2g3g4-path', from_utc: '2026-08-15T00:24:16Z', to_utc: '2026-08-21T17:00:05Z', trades: 7800 },
      { version: 'param-v1.3.0-dual-path-sticky', from_utc: '2026-08-21T17:28:25Z', to_utc: '2026-08-22T03:15:00Z', trades: 771 },
      { version: 'param-v1.4.0-staircase-confirm-dmr', from_utc: '2026-08-22T03:30:00Z', to_utc: '2026-08-24T11:00:00Z', trades: 5048 },
    ],
  };
  s = {
    trades: 1049, usable: 1049, missing_px: 0, unique_coins: 157, open_trades: 46,
    up: 633, down: 416, win: 426, flat: 5, loss: 618, win_rate: 0.4076,
    sum_pct: -0.58, avg_pct: -0.00055, median_pct: -0.0027, max_pct: 0.42, min_pct: -0.31,
    avg_win_pct: 0.0245, avg_loss_pct: -0.018, profit_factor: 0.947,
    avg_dwell_minutes: 150, median_dwell_minutes: 60, max_drawdown_pct: -3.83,
    turnover_per_coin_per_day: 0.95, data_quality: 1,
    dwell_histogram: [{ from_nodes: 1, to_nodes: 1, count: 198 }, { from_nodes: 32, to_nodes: null, count: 69 }],
    equity_curve: [{ at_utc: 'a', cum_pnl_pct: 0.1 }, { at_utc: 'b', cum_pnl_pct: -0.2 }],
    flag_counts: { PARAM_VERSION_CHANGED: 62 },
    by_parameter_version: {
      'param-v1.3.0-dual-path-sticky': { trades: 211, trade_share: 0.2, win_rate: 0.4 } as unknown as ReviewSummary,
      'param-v1.4.0-staircase-confirm-dmr': { trades: 829, trade_share: 0.79, win_rate: 0.41 } as unknown as ReviewSummary,
    },
    control_benchmark: { zones: ['ELIMINATED'], trades: 3311, avg_pct: -0.00097, win_rate: 0.64, edge_avg_pct: 0.00042, edge_win_rate: -0.23 },
  };
  const banner = renderToStaticMarkup(<CoverageBanner cov={cov} zones={['DMR', 'CONFIRMED']} />);
  check('覆盖不足必现横幅', banner.includes('9.44') && banner.includes('28'), '');
  check('DMR 短历史必现横幅', banner.includes('DMR'), '');
  check('账本落后必现横幅', banner.includes('账本落后'), '');
  // 参数版本告警已移交 ParamVersionBar 独占（见第 12 节），覆盖横幅不再重复渲染，
  // 否则同一件事会在页面上出现两遍。
  check('覆盖横幅不再重复渲染参数版本告警', !banner.includes('参数版本'), '');

  const cards = renderToStaticMarkup(
    <>
      <SummaryCard title="合并" s={s} />
      <ControlBenchmarkCard s={s} />
    </>,
  );
  check('汇总卡含盈亏比/回撤/换手/数据质量', ['盈亏比', '最大回撤', '换手', '数据质量'].every((k) => cards.includes(k)));
  check('汇总卡显式写出有效笔数分母', cards.includes('分母 = 有效 1049 笔'), '');
  check('对照组卡标注非可执行', cards.includes('不代表可执行策略'));

  const cmp = renderToStaticMarkup(
    <PeriodCompareTable
      periods={[
        { id: 'main', label: '近 7 天', summary: s },
        { id: 'p1', label: '周期 2', summary: s },
      ]}
    />,
  );
  check('对比表出现参数版本行', cmp.includes('参数版本'));
  check('跨版本列打 ⚠', cmp.includes('⚠'), '');

  const empty = renderToStaticMarkup(
    <MemoryRouter>
      <ReviewDataGrid rows={[]} />
    </MemoryRouter>,
  );
  check('空态是提示而不是报错', empty.includes('0 笔完整交易'));
}

console.log('\n=== 11. 红色需求：图表区已删除 ===');
{
  const exported = Object.keys(SummaryCardsMod);
  check('EquitySparkline 已移除', !exported.includes('EquitySparkline'), exported.join(','));
  check('DwellHistogram 已移除', !exported.includes('DwellHistogram'), exported.join(','));
  const page = renderToStaticMarkup(
    <>
      <SummaryCard title="合并" s={s} />
      <ControlBenchmarkCard s={s} />
    </>,
  );
  check('页面不再出现累计曲线/直方图容器', !page.includes('review-spark') && !page.includes('review-hist'));
  // 最大回撤仍在汇总卡里（后端照常算），只是不再画图
  check('最大回撤仍然保留在汇总卡', page.includes('最大回撤'));
}

console.log('\n=== 12. 黄色需求：参数版本切换 ===');
{
  check('版本短名', shortVersion('param-v1.4.0-staircase-confirm-dmr') === 'v1.4.0', shortVersion('param-v1.4.0-staircase-confirm-dmr'));
  const picked: string[] = [];
  const bar = renderToStaticMarkup(
    <ParamVersionBar cov={cov} summary={s} value="all" onChange={(v) => picked.push(v)} />,
  );
  check('三个版本按钮都在', ['v1.2.0', 'v1.3.0', 'v1.4.0'].every((v) => bar.includes(v)), '');
  check('有「全部」按钮', bar.includes('全部'));
  check('跨版本时给出告警', bar.includes('跨'), '');

  const locked = renderToStaticMarkup(
    <ParamVersionBar cov={cov} summary={s} value="param-v1.4.0-staircase-confirm-dmr" onChange={() => {}} />,
  );
  check('锁定某版本时明确说明口径', locked.includes('已锁定参数版本') && locked.includes('v1.4.0'), '');
  // 锁定后仍必须能切回去（开关不能挂在「跨版本」条件上）
  check('锁定后切换条依然渲染', locked.includes('全部'), '');

  // 单一版本的账本不显示切换条
  const single = renderToStaticMarkup(
    <ParamVersionBar
      cov={{ ...cov, parameter_segments: [{ version: 'param-v1.4.0-x', trades: 10 }] }}
      summary={s}
      value="all"
      onChange={() => {}}
    />,
  );
  check('只有一个版本时不占地方', single === '', single.slice(0, 60));

  check('pv 进查询串', reviewQueryString({ pv: 'param-v1.4.0-x' }).includes('pv=param-v1.4.0-x'));
  check("pv=all 不进查询串", !reviewQueryString({ pv: 'all' }).includes('pv='));
  const pvState = {
    board: 'main',
    zones: ['DMR'], direction: 'both', attribution: 'exit', zoneMode: 'parallel', rangeDays: 7,
    useCustom: false, customFrom: '', customTo: '', comparePeriods: [], compareOn: false,
    search: '', includeOpen: false, onlyFlagged: false, excludeCycleReset: false,
    wholeCycles: false, minDwellNodes: 0, columnPreset: 'full',
    paramVersion: 'param-v1.3.0-dual-path-sticky', paramHash: 'all', sortId: 'enter_time_utc', sortDesc: false,
  } as Parameters<typeof stateToSearch>[0];
  const round = searchToState(stateToSearch(pvState));
  check('参数版本 URL 往返', round.paramVersion === 'param-v1.3.0-dual-path-sticky', String(round.paramVersion));

  // 调参段（param_hash）：与 pv 同构的第二层过滤。同一 parameter_version 内部，
  // 216 主导层上线前后是两段不可比的数据，没有它页面会把两段混算成一个胜率。
  check('ph 进查询串', reviewQueryString({ ph: 'pf1_fcea251fa94122fe' }).includes('ph=pf1_fcea251fa94122fe'));
  check('ph=all 不进查询串', !reviewQueryString({ ph: 'all' }).includes('ph='));
  const phState = { ...pvState, paramHash: 'pf1_fcea251fa94122fe' } as Parameters<typeof stateToSearch>[0];
  const phRound = searchToState(stateToSearch(phState));
  check('调参段 URL 往返', phRound.paramHash === 'pf1_fcea251fa94122fe', String(phRound.paramHash));
  const nullState = { ...pvState, paramHash: 'null' } as Parameters<typeof stateToSearch>[0];
  check('无指纹段 ph=null 往返', searchToState(stateToSearch(nullState)).paramHash === 'null');

  // 外板入点锁不得落到另一本账本上：Y 没有 v1.4.0 入点，AND 后是 0 笔。
  const yForeign = searchToState('?board=y&pv=param-v1.4.0-staircase-confirm-dmr');
  check('Y + 主板 pv 被丢掉', yForeign.paramVersion === 'all', String(yForeign.paramVersion));
  const yOwn = searchToState('?board=y&pv=param-v2.0.0-screener-y');
  check('Y 自己的 pv 保留', yOwn.paramVersion === 'param-v2.0.0-screener-y', String(yOwn.paramVersion));
  const mainHist = searchToState('?pv=param-v1.3.0-dual-path-sticky');
  check('主板历史 dual-path 入点保留', mainHist.paramVersion === 'param-v1.3.0-dual-path-sticky', String(mainHist.paramVersion));
  const mainXid = searchToState('?board=main&pv=param-v1.3.0-screener-x');
  check('主板上的 X 当前身份被丢掉', mainXid.paramVersion === 'all', String(mainXid.paramVersion));
}

console.log('\n=== 13. 版本切换后不得变成空白页（回归）===');
{
  // 事故：锁 v1.2.0 / v1.3.0 后页面全 0。根因不是过滤坏了，而是版本的生效区间
  // 与「近 1 天」窗口零重叠 —— 两个独立条件被 AND 到一起，交集为空。
  const seg = {
    version: 'param-v1.2.0-g1g2g3g4-path',
    from_utc: '2026-08-15T00:24:16Z',
    to_utc: '2026-08-21T17:00:05Z',
    first_exit_utc: '2026-08-15T01:00:05Z',
    last_exit_utc: '2026-08-24T10:45:00Z',
    trades: 7800,
  };
  const span = versionSpan(seg);
  check('区间取「最早入选 → 最晚退出」（超集，不裁边界）',
    span?.from === '2026-08-15T00:24:16Z' && span?.to === '2026-08-24T10:45:00Z', JSON.stringify(span));

  // 点版本按钮必须同时带出区间，否则窗口不动 = 依然空白
  const calls: Array<[string, string | null | undefined, string | null | undefined]> = [];
  const bar = renderToStaticMarkup(
    <ParamVersionBar cov={cov} summary={s} value="all" onChange={(v, f, t) => calls.push([v, f, t])} />,
  );
  check('按钮不再复用 28×28 的等级方块', bar.includes('review-param-btn') && !bar.includes('grade-btn'), '');
  check('按钮上直接标出该版本的数据区间', bar.includes('→'), '');

  useReviewUi.getState().reset();
  const st = () => useReviewUi.getState();
  st().lockParamVersion('param-v1.3.0-dual-path-sticky', '2026-08-21T17:28:25Z', '2026-08-22T11:30:00Z');
  check('锁版本时窗口自动对齐', st().useCustom === true && st().customFrom === '2026-08-21T17:28:25Z', `${st().useCustom} ${st().customFrom}`);
  check('版本已锁定', st().paramVersion === 'param-v1.3.0-dual-path-sticky');
  st().lockParamVersion('all');
  check('切回「全部」解锁并恢复周期', st().paramVersion === 'all' && st().useCustom === false, `${st().paramVersion} ${st().useCustom}`);
  st().reset();

  // 空结果必须解释原因 + 给一键修复，不能留白
  const emptySummary = { ...s, trades: 0, by_parameter_version: {} } as ReviewSummary;
  const emptyBar = renderToStaticMarkup(
    <ParamVersionBar cov={cov} summary={emptySummary} value="param-v1.2.0-g1g2g3g4-path" onChange={() => {}} />,
  );
  check('空结果时说明「没有任何完整交易」', emptyBar.includes('没有任何完整交易'), '');
  check('空结果时给出该版本的数据区间', emptyBar.includes('与当前窗口不重叠'), '');
  check('空结果时提供一键切换', emptyBar.includes('切到该版本的数据区间'), '');
  check('空结果时提示分区组合也可能是原因', emptyBar.includes('DMR 区自'), '');

  // 有数据时不得误报空态
  const okBar = renderToStaticMarkup(
    <ParamVersionBar cov={cov} summary={s} value="param-v1.4.0-staircase-confirm-dmr" onChange={() => {}} />,
  );
  check('有数据时不出空态提示', !okBar.includes('没有任何完整交易'), '');

  const ySingle = {
    watermark_scan_id: '20260910-048',
    parameter_segments: [{ version: 'param-v2.0.0-screener-y', from_utc: '2026-08-16T00:00:00Z', to_utc: '2026-09-10T12:00:00Z', trades: 55931 }],
  } as ReviewCoverage;
  const yEmpty = { ...s, trades: 0, by_parameter_version: {} } as ReviewSummary;
  const yForeignBar = renderToStaticMarkup(
    <ParamVersionBar cov={ySingle} summary={yEmpty} value="param-v1.4.0-staircase-confirm-dmr" onChange={() => {}} />,
  );
  check('Y 单版本 + 外板锁必须露出解锁条', yForeignBar.includes('不属于当前账本') && yForeignBar.includes('解除入点锁'), yForeignBar.slice(0, 120));
  const yIdle = renderToStaticMarkup(
    <ParamVersionBar cov={ySingle} summary={s} value="all" onChange={() => {}} />,
  );
  check('Y 单版本未加锁时仍不占地方', yIdle === '', yIdle.slice(0, 60));
}

console.log('\n=== 参数指纹：混参时不得混着展示（T19 / 文档B §3.1 规则 3） ===');
{
  // 契约层：ReviewTrade 必须能带 param_hash 与 4 个组合列，且全部可选 ——
  // 31 天窗口内必然同时存在「有指纹」与「无指纹」的行。
  const tsSrc = readFileSync('src/shared/types/review.ts', 'utf8');
  check('ReviewTrade 带可选 param_hash', /param_hash\?:\s*string \| null/.test(tsSrc));
  check('ReviewTrade 带可选 combo_code', /combo_code\?:\s*string \| null/.test(tsSrc));
  check('ReviewTrade 带可选 zone_ceiling', /zone_ceiling\?:\s*string \| null/.test(tsSrc));
  check('ReviewTrade 带可选 z_score', /z_score\?:\s*number \| null/.test(tsSrc));
  check('ReviewFlag 含 PARAM_HASH_CHANGED', tsSrc.includes("'PARAM_HASH_CHANGED'"));
  check('PARAM_HASH_CHANGED 有中文标签', REVIEW_FLAG_LABEL['PARAM_HASH_CHANGED'] !== undefined,
    REVIEW_FLAG_LABEL['PARAM_HASH_CHANGED'] ?? '(缺)');

  // 混参判据本身是纯函数级的：两组不同指纹 → 不可比
  const hashes = (rows: Array<{ param_hash?: string | null }>) =>
    new Set(rows.map((r) => r.param_hash).filter(Boolean));
  check('单一指纹 → 可比', hashes([{ param_hash: 'pf1_a' }, { param_hash: 'pf1_a' }]).size <= 1);
  check('两套指纹 → 不可比', hashes([{ param_hash: 'pf1_a' }, { param_hash: 'pf1_b' }]).size > 1);
  check('全为 null（阶段0之前）→ 不算混参', hashes([{ param_hash: null }, {}]).size === 0);
}

if (failures) {
  console.log(`\nFAILED ${failures}`);
  process.exit(1);
}
console.log('\nall ok');
