/**
 * 「选币榜」/「选币榜Y」双板面隔离的验证闸。
 *
 *   cd apps/web && npm run verify:board-split
 *
 * 要守住的东西只有一句话：**新增选币榜Y 不得改动选币榜的任何一个字节**，
 * 同时两块板面必须真的走两条独立的取数、状态与账本通道。
 *
 * 覆盖：
 *   1. 注册表：两块板面的 key / 路由 / API 前缀 / 参数版本 / 账本互不相同；
 *      选币榜的四项必须与改动前逐字一致（写死在这里当护栏，改了就红）。
 *   2. 取数：`/screener/*` 与 `/screener-y/*`，默认参数落在选币榜（既有调用点不变）。
 *   3. UI 仓库：两份实例互不串台 —— 在 Y 上改排序/筛选不得影响选币榜。
 *   4. 复盘：board=y 才进查询串，board=main 一个字符都不加；URL 往返可还原。
 *   5. 顶栏：选币榜Y 出现在「选币榜」与「复盘选币」之间。
 *   8. K 线「返回榜单」：`from=screener-y` 回到 `/screener-y`，`from=screener` /
 *      缺省仍回 `/screener`（选币榜原路径一个字节都不能改）。
 */
import { readFileSync, writeFileSync } from 'node:fs';
import { renderToStaticMarkup } from 'react-dom/server';
import { MemoryRouter } from 'react-router-dom';
import { AppNav } from '../src/components/AppNav';
import { ReviewBoardBar } from '../src/features/review/ReviewBoardBar';
import { ParamVersionBar } from '../src/features/review/ParamVersionBar';
import { ConfirmedBanner } from '../src/features/screener/components/ConfirmedBanner';
import { CycleStrip } from '../src/features/screener/components/CycleStrip';
import { CycleWindowBar } from '../src/features/review/CycleWindowBar';
import { cycleEndOf, cycleStartOf, isAligned, snapWindow, windowAligned }
  from '../src/features/review/cycleAlign';
import type { ReviewCycleCoverage, ReviewCycleWindow } from '../src/shared/types/review';
import { REVIEW_FLAG_LABEL } from '../src/shared/types/review';
import type { CandidateRow, ScanCycle, ScreenerState } from '../src/shared/types/screener';
import { buildCols } from '../src/features/screener/components/candidateColumns';
import { formatEnterClock } from '../src/shared/lib/format';
import {
  BOARDS,
  BOARD_KEYS,
  DEFAULT_BOARD,
  boardConfig,
  boardFromOrigin,
  backHrefFromOrigin,
  originTagFor,
  shortParamVersion,
} from '../src/shared/config/boards';
import { mcapCeilingLabel, screenerBrandNote } from '../src/shared/lib/mcapCeilingStatus';
import { reviewQueryString } from '../src/shared/api/review';
import { searchToState, stateToSearch } from '../src/features/review/reviewQuery';
import {
  createScreenerUiStore,
  screenerUiStoreFor,
  useScreenerUi,
  useScreenerXUi,
  useScreenerYUi,
} from '../src/shared/stores/screenerUi';

let failures = 0;
function check(name: string, ok: boolean, extra = '') {
  console.log((ok ? '  ok   ' : '  FAIL ') + name + (extra ? '  ' + extra : ''));
  if (!ok) failures += 1;
}

console.log('=== 1. 板面注册表 ===');
// 选币榜X v1.3.0 复刻 main v1.4.0，独立复盘闭环；今后调参只在 X overrides。
check('三块板面顺序 main,x,y', BOARD_KEYS.join(',') === 'main,x,y', BOARD_KEYS.join(','));
check('默认板面仍是选币榜', DEFAULT_BOARD === 'main', DEFAULT_BOARD);

// 冻结项：这四行就是「原选币榜不得被改动」的机器可读版本
check('选币榜 路由不变', BOARDS.main.route === '/screener', BOARDS.main.route);
check('选币榜 API 前缀不变', BOARDS.main.apiPrefix === '/screener', BOARDS.main.apiPrefix);
check(
  '选币榜 参数版本不变 v1.4.0',
  BOARDS.main.parameterVersion === 'param-v1.4.0-staircase-confirm-dmr',
  BOARDS.main.parameterVersion,
);
check('选币榜 DMR 仍可执行', BOARDS.main.dmrExecutable === true);

check('选币榜Y 参数版本 v2.0.0', BOARDS.y.parameterVersion === 'param-v2.0.0-screener-y');
check('选币榜Y 路由独立', BOARDS.y.route === '/screener-y' && BOARDS.y.route !== BOARDS.main.route);
check('选币榜Y API 前缀独立', BOARDS.y.apiPrefix !== BOARDS.main.apiPrefix, BOARDS.y.apiPrefix);
check('选币榜Y 账本独立', BOARDS.y.reviewBoard !== BOARDS.main.reviewBoard);
check('选币榜Y 的 DMR 不可执行（不进纸面执行层）', BOARDS.y.dmrExecutable === false);
check('规则短名 v1.4.0 / v1.3.0 / v2.0.0', BOARDS.main.rulesetLabel === 'v1.4.0'
  && BOARDS.x.rulesetLabel === 'v1.3.0' && BOARDS.y.rulesetLabel === 'v2.0.0');
check('X 不复用旧 v1.3.0', BOARDS.x.parameterVersion === 'param-v1.3.0-screener-x');
check('三块板面的路径/前缀/目录/复盘两两独立',
  ['route','apiPrefix','dataDir','reviewBoard'].every((f) =>
    new Set(BOARD_KEYS.map((k) => BOARDS[k][f as keyof typeof BOARDS.main])).size === 3));
check('短版本号提取', shortParamVersion('param-v2.0.0-screener-y') === 'v2.0.0');
check('未知 key 回落选币榜（不炸页）', boardConfig('nope').key === 'main');

console.log('\n=== 2. UI 仓库互不串台 ===');
{
  // X v1.3.0 复刻 main v1.4.0 但 UI/复盘独立；未来 overrides 演进不得共用状态。
  const before = useScreenerUi.getState();
  const yBefore = useScreenerYUi.getState();
  const x = useScreenerXUi.getState();
  x.setDirection('down'); x.setSearch('BTC'); x.setSort('ret_1h');
  x.setColumnPreset('full_s37'); x.setActiveStates(['ELIMINATED']); x.setDmrActive(true);
  check('X 拿到独立工厂实例', screenerUiStoreFor('x') === useScreenerXUi
    && useScreenerXUi !== useScreenerUi && useScreenerXUi !== useScreenerYUi);
  check('X 改方向/搜索/排序/列/分区不影响 main/y',
    useScreenerUi.getState() === before && useScreenerYUi.getState() === yBefore);
  useScreenerXUi.getState().resetFilters();
}
check('两块板面各持一份仓库', useScreenerUi !== useScreenerYUi);
check('按 key 取到的就是对应那份', screenerUiStoreFor('y') === useScreenerYUi && screenerUiStoreFor('main') === useScreenerUi);
{
  const before = useScreenerUi.getState();
  useScreenerYUi.getState().setDirection('down');
  useScreenerYUi.getState().setSearch('HYPE');
  useScreenerYUi.getState().setSort('ret_1w');
  const main = useScreenerUi.getState();
  check('在 Y 上改方向不影响选币榜', main.direction === before.direction, main.direction);
  check('在 Y 上改搜索不影响选币榜', main.search === '', JSON.stringify(main.search));
  check('在 Y 上改排序不影响选币榜', main.sortId === before.sortId, main.sortId);
  check('Y 自己确实改到了', useScreenerYUi.getState().direction === 'down' && useScreenerYUi.getState().sortId === 'ret_1w');
  useScreenerYUi.getState().resetFilters();
}
{
  const a = createScreenerUiStore();
  const b = createScreenerUiStore();
  a.getState().setSearch('X');
  check('工厂每次产出独立实例', b.getState().search === '');
}

console.log('\n=== 3. 复盘账本按板面切换 ===');
check('board=main 不进查询串（既有 URL 不变）', !reviewQueryString({ board: 'main', zones: ['DMR'] }).includes('board='));
check('省略 board 不进查询串', !reviewQueryString({ zones: ['DMR'] }).includes('board='));
check('board=y 进查询串', reviewQueryString({ board: 'y', zones: ['DMR'] }).includes('board=y'));
{
  const base = {
    zones: ['DMR'] as never, direction: 'both', attribution: 'exit', zoneMode: 'parallel',
    rangeDays: 7, useCustom: false, customFrom: '', customTo: '', comparePeriods: [],
    compareOn: false, search: '', includeOpen: false, onlyFlagged: false, minDwellNodes: 0,
    excludeCycleReset: false, wholeCycles: false,
    columnPreset: 'full', paramVersion: 'all', paramHash: 'all', sortId: 'enter_time_utc', sortDesc: false,
  };
  const mainSearch = stateToSearch({ ...base, board: 'main' } as Parameters<typeof stateToSearch>[0]);
  const ySearch = stateToSearch({ ...base, board: 'y' } as Parameters<typeof stateToSearch>[0]);
  check('main 不写进 URL', !mainSearch.includes('board='), mainSearch);
  check('y 写进 URL', ySearch.includes('board=y'), ySearch);
  check('Y 显式关掉 nocycle 时 URL 写 0', ySearch.includes('nocycle=0'), ySearch);
  check('Y 且 URL 无 nocycle → 默认剔除周期强平', searchToState('?board=y&zones=DMR').excludeCycleReset === true);
  check('主板 URL 无 nocycle → 不默认剔除', searchToState('?zones=DMR').excludeCycleReset === undefined);
  check('Y nocycle=0 往返保持关闭', searchToState(ySearch).excludeCycleReset === false);
  check('URL 往返还原板面', searchToState(ySearch).board === 'y', String(searchToState(ySearch).board));
  check('未知 board 被忽略（回落默认）', searchToState('?board=zzz').board === undefined);
}

console.log('\n=== 4. 顶栏位置 ===');
{
  const html = renderToStaticMarkup(
    <MemoryRouter>
      <AppNav />
    </MemoryRouter>,
  );
  const iScreener = html.indexOf('/screener"');
  const iY = html.indexOf('/screener-y"');
  const iX = html.indexOf('/screener-x"');
  check('顶栏 main < x < y < review', iScreener >= 0 && iScreener < iX && iX < iY
    && iY < html.indexOf('/review"'), `${iScreener} < ${iX} < ${iY}`);
  writeFileSync('tests/.out/x-nav.html', html);
  const iReview = html.indexOf('/review"');
  check('顶栏有「选币榜Y」入口', iY > -1 && html.includes('选币榜Y'));
  check('位置在「选币榜」与「复盘选币」之间', iScreener > -1 && iScreener < iY && iY < iReview,
    `${iScreener} < ${iY} < ${iReview}`);
  check('原「选币榜」入口仍在', html.includes('href="/screener"'));
  check('原「复盘选币」入口仍在', html.includes('href="/review"') && html.includes('复盘选币'));
}

console.log('\n=== 5. 复盘规则版本切换条 ===');
{
  const cov = { watermark_scan_id: '20260825-019', watermark_ts: '2026-08-25T04:45:00Z', ledger_ready: true };
  const mainHtml = renderToStaticMarkup(
    <ReviewBoardBar value="main" onChange={() => {}} cov={cov as never} />,
  );
  const yHtml = renderToStaticMarkup(
    <ReviewBoardBar value="y" onChange={() => {}} cov={cov as never} />,
  );
  check('三个规则版本按钮都在', ['v1.4.0', 'v1.3.0', 'v2.0.0'].every((v) => mainHtml.includes(v)));
  check('标出各自的参数版本', mainHtml.includes('param-v1.4.0-staircase-confirm-dmr')
    && mainHtml.includes('param-v2.0.0-screener-y'));
  check('选中的规则版本被高亮', yHtml.includes('review-param-btn on'));
  check('说明「两套规则各有各的账本，从不合并计算」', mainHtml.includes('从不合并计算'));
  check('账本水位可见', mainHtml.includes('20260825-019'));
  // v1.3对齐不能用改名替代：选board是选库，不是过滤入点版本；X仍为v1.4克隆。
  const xScope = renderToStaticMarkup(<ReviewBoardBar value="x" onChange={() => {}} />);
  check('选库说明不得把main历史冒称当前v1.4全量',
    !mainHtml.includes('这套选币机制产出的交易') && mainHtml.includes('入点版本'));
  check('X复盘常驻明确非旧dual-path', xScope.includes('当前复刻 main v1.4.0')
    && xScope.includes('param-v1.3.0-dual-path-sticky'));
  const oldScope = renderToStaticMarkup(<ParamVersionBar
    cov={{ parameter_segments: [
      { version: 'param-v1.3.0-dual-path-sticky', trades: 1 },
      { version: 'param-v1.4.0-staircase-confirm-dmr', trades: 1 },
    ] } as never}
    value="param-v1.3.0-dual-path-sticky" onChange={() => {}} />);
  check('旧版本过滤明确只锁入点、不证明整笔同规则',
    oldScope.includes('入点版本') && oldScope.includes('PARAM_VERSION_CHANGED'));
  writeFileSync('tests/.out/x-v13-scope.html', mainHtml + xScope + oldScope);
  // Y 的账本没铺底时必须明说，并给出正确的补数据命令（不是主板面那条）
  const empty = renderToStaticMarkup(
    <ReviewBoardBar value="y" onChange={() => {}} error="HTTP 503 ledger_not_ready" />,
  );
  check('Y 账本为空时给出提示', empty.includes('还没有数据'));
  check('提示的是 replay_screener_y.py 而不是 build_review_ledger.py',
    empty.includes('replay_screener_y.py') && !empty.includes('build_review_ledger.py'));
  const mainEmpty = renderToStaticMarkup(
    <ReviewBoardBar value="main" onChange={() => {}} error="boom" />,
  );
  check('主板面不显示这条 Y 专属提示', !mainEmpty.includes('还没有数据'));
}

console.log('\n=== 6. 时间区（24h 周期）状态条 ===');
{
  const base: ScanCycle = {
    enabled: true,
    period_hours: 24,
    anchor_utc: '00:00',
    cycle_key: '20260825T0000Z',
    cycle_start_utc: '2026-08-25T00:00:00Z',
    cycle_end_utc: '2026-08-26T00:00:00Z',
    node_in_cycle: 40,
    nodes_per_cycle: 96,
    reset_at_this_node: false,
    last_reset_node_in_cycle: 0,
  };

  // 选币榜没有 cycle —— 这条必须完全不渲染，页面与改动前逐像素相同
  check('未启用周期时不渲染', renderToStaticMarkup(<CycleStrip cycle={{ enabled: false }} />) === '');

  const mid = renderToStaticMarkup(<CycleStrip cycle={base} />);
  check('标出周期身份与起点', mid.includes('20260825T0000Z'));
  check('标出本周期第几节点', mid.includes('40') && mid.includes('96'));
  check('有下次重置倒计时', mid.includes('距下次清空重建'));
  check('周期中段不显示「重建中」', !mid.includes('分区重建中'));
  check('周期中段不显示重置横幅', !mid.includes('已执行周期重置'));

  const atReset = renderToStaticMarkup(
    <CycleStrip cycle={{ ...base, node_in_cycle: 0, reset_at_this_node: true,
      closed_zones: { CONFIRMED: 62, QUALIFIED: 95, WATCH: 250, NONE: 0 } }} />,
  );
  check('重置节点显示清空横幅', atReset.includes('全部分区的历史币种数据已清空'));
  check('重置横幅列出被清出的分区', atReset.includes('CONFIRMED 62') && atReset.includes('WATCH 250'));
  check('重置横幅说明正在按新起点重新采集', atReset.includes('重新采集并重建分区'));
  check('NONE 不算分区，不进清空清单', !atReset.includes('NONE 0'));

  const warming = renderToStaticMarkup(<CycleStrip cycle={{ ...base, node_in_cycle: 2 }} />);
  check('重建窗口显示「分区重建中」', warming.includes('分区重建中'));
  check('重建窗口说明确认区为空是预期行为', warming.includes('预期行为'));
  check('重建窗口给出各区出现的节点号', warming.includes('确认区 / DMR 区第 5 节点'));
  check('重建窗口说清「不在表内」而不是「没数据」',
    warming.includes('暂时不在表内') && warming.includes('当刻已全部算好'));
  check('重置横幅说明本节点表内只有淘汰区/数据不足区',
    atReset.includes('只会看到当刻就能直接判定的淘汰区 / 数据不足区'));
  const settled = renderToStaticMarkup(<CycleStrip cycle={{ ...base, node_in_cycle: 5 }} />);
  check('第 5 个节点起不再提示重建', !settled.includes('分区重建中'));

  const partial = renderToStaticMarkup(
    <CycleStrip cycle={{ ...base, node_in_cycle: 30, partial_cycle: true,
      partial_reason: 'cold_start', last_reset_node_in_cycle: 26,
      last_reset_at_utc: '2026-08-25T06:30:00Z' }} />,
  );
  check('不完整周期给出告警', partial.includes('不是从 0 号节点开始重建'));
  check('说明是首次启用而不是停机', partial.includes('首次启用周期管理'));

  check('复盘有「周期重置强平」旗标标签', REVIEW_FLAG_LABEL.CYCLE_RESET === '周期重置强平');
}

console.log('\n=== 7. 复盘窗口对齐到周期栅格 ===');
{
  // 有时间区的规则版本：发 cycles=N，且**不再**发 from/to（服务端按栅格算，
  // 两套一起发只会让人搞不清最终用的是哪个窗口）
  const qs = reviewQueryString({ board: 'y', zones: ['DMR'], cycles: 7, from: 'X', to: 'Y' });
  check('cycles 进查询串', qs.includes('cycles=7'), qs);
  check('cycles 与 from/to 互斥', !qs.includes('from=') && !qs.includes('to='), qs);
  check('whole_cycles 默认不发', !qs.includes('whole_cycles'));
  check('whole_cycles=1 可发',
    reviewQueryString({ board: 'y', cycles: 7, whole_cycles: true }).includes('whole_cycles=1'));
  // 没有 cycles 时仍走原来的 from/to —— v1.4.0 的请求 URL 一个字符都不变
  const legacy = reviewQueryString({ zones: ['DMR'], from: 'A', to: 'B' });
  check('无 cycles 时仍发 from/to（v1.4.0 行为不变）',
    legacy.includes('from=A') && legacy.includes('to=B') && !legacy.includes('cycles='), legacy);

  // URL 往返
  const base = {
    zones: ['DMR'] as never, direction: 'both', attribution: 'exit', zoneMode: 'parallel',
    rangeDays: 7, useCustom: false, customFrom: '', customTo: '', comparePeriods: [],
    compareOn: false, search: '', includeOpen: false, onlyFlagged: false,
    excludeCycleReset: false, wholeCycles: false,
    minDwellNodes: 0, columnPreset: 'full', paramVersion: 'all', paramHash: 'all',
    sortId: 'enter_time_utc', sortDesc: false, board: 'y',
  };
  const wSearch = stateToSearch({ ...base, wholeCycles: true } as Parameters<typeof stateToSearch>[0]);
  check('只算完整周期写进 URL', wSearch.includes('whole=1'), wSearch);
  check('只算完整周期 URL 往返', searchToState(wSearch).wholeCycles === true);
  check('默认不写进 URL',
    !stateToSearch(base as Parameters<typeof stateToSearch>[0]).includes('whole='));

  // 栅格条
  const cov: ReviewCycleCoverage = {
    enabled: true, period_hours: 24, anchor_utc: '00:00', nodes_per_cycle: 96,
    current_key: '20260825T0000Z', current_start_utc: '2026-08-25T00:00:00Z',
    current_end_utc: '2026-08-26T00:00:00Z', current_elapsed_hours: 7,
    current_complete: false, current_progress: 0.2917,
    first_cycle_key: '20260815T0000Z', first_cycle_partial: true, complete_cycles: 9,
  };
  const w: ReviewCycleWindow = {
    from: '2026-08-19T00:00:01Z', to: '2026-08-25T07:00:00Z', cycles: 7, whole_cycles: false,
    attribution: 'exit', first_cycle_key: '20260819T0000Z', last_cycle_key: '20260825T0000Z',
    window_start_utc: '2026-08-19T00:00:00Z', window_end_utc: '2026-08-25T07:00:00Z',
    includes_partial_current: true,
  };
  check('无时间区时栅格条不渲染',
    renderToStaticMarkup(
      <CycleWindowBar cycle={{ enabled: false }} wholeCycles={false} onToggleWhole={() => {}} />,
    ) === '');
  const bar = renderToStaticMarkup(
    <CycleWindowBar cycle={cov} window={w} wholeCycles={false} onToggleWhole={() => {}} />,
  );
  check('说明窗口已对齐到周期边界', bar.includes('窗口已对齐到周期边界'));
  check('列出首末周期', bar.includes('08-19') && bar.includes('08-25'));
  check('标出与选币榜Y 同一张栅格', bar.includes('同一张栅格'));
  check('给出当前周期进度', bar.includes('7.0h / 24h'));
  check('给出账本内完整周期数', bar.includes('账本内完整周期'));
  check('残缺的首周期被说明未计入', bar.includes('未计入'));
  check('含进行中周期时给出告警', bar.includes('进行中的当前周期') && bar.includes('只算完整周期'));
  const whole = renderToStaticMarkup(
    <CycleWindowBar
      cycle={cov}
      window={{ ...w, whole_cycles: true, includes_partial_current: false }}
      wholeCycles
      onToggleWhole={() => {}}
    />,
  );
  check('只算完整周期时说明已排除进行中周期', whole.includes('已排除进行中的当前周期'));
  check('只算完整周期时不再显示混算告警', !whole.includes('别直接放在一起平均'));
  const empty = renderToStaticMarkup(
    <CycleWindowBar
      cycle={cov}
      window={{ ...w, includes_partial_current: false, empty_current_cycle: true }}
      wholeCycles={false}
      onToggleWhole={() => {}}
    />,
  );
  check('周期刚开始时说明本周期还没有已平仓交易', empty.includes('还没有属于它的'));
  check('并说清强平算在上一个周期名下', empty.includes('算在上一个周期名下'));

  // 自定义 / 对比周期的吸附：锚在服务端给的 current_start_utc 上，不是前端另造的栅格
  check('周期边界判定', isAligned('2026-08-20T00:00:00Z', cov) && !isAligned('2026-08-20T07:00:00Z', cov));
  check('向下取整到所属周期起点', cycleStartOf('2026-08-20T07:30:00Z', cov) === '2026-08-20T00:00:00Z');
  check('向上取整到所属周期终点', cycleEndOf('2026-08-20T07:30:00Z', cov) === '2026-08-21T00:00:00Z');
  const snapped = snapWindow('2026-08-18T07:00:00Z', '2026-08-24T13:20:00Z', cov);
  check('吸附后窗口由若干完整周期组成',
    snapped.from === '2026-08-18T00:00:00Z' && snapped.to === '2026-08-25T00:00:00Z',
    JSON.stringify(snapped));
  check('吸附后判定为已对齐', windowAligned(snapped.from, snapped.to, cov));
  check('未对齐的窗口被识别', !windowAligned('2026-08-18T07:00:00Z', '2026-08-24T13:20:00Z', cov));
  // 没有时间区的规则版本：一律视为已对齐，不打扰
  check('无时间区时不判定对齐', windowAligned('2026-08-18T07:00:00Z', '2026-08-24T13:20:00Z',
    { enabled: false }));
  check('无时间区时吸附是恒等',
    snapWindow('2026-08-18T07:00:00Z', '2026-08-24T13:20:00Z', { enabled: false }).from
      === '2026-08-18T07:00:00Z');
}

console.log('\n=== 8. K 线「返回榜单」按来源板面回跳 ===');
{
  check('X 出站 from=screener-x', originTagFor('x') === 'screener-x');
  check('X K 线详情读 X', boardFromOrigin('screener-x') === 'x' && boardFromOrigin('x') === 'x');
  check('X 返回 /screener-x', backHrefFromOrigin('screener-x') === '/screener-x'
    && backHrefFromOrigin('x') === '/screener-x');
  check('选币榜 originTag 仍是 screener', originTagFor('main') === 'screener', originTagFor('main'));
  check('选币榜Y originTag 是 screener-y', originTagFor('y') === 'screener-y', originTagFor('y'));
  check('from=screener-y 解析为板面 y', boardFromOrigin('screener-y') === 'y');
  check('from=screener 解析为板面 main', boardFromOrigin('screener') === 'main');
  check('缺省 from 回落选币榜', boardFromOrigin(null) === 'main' && boardFromOrigin(undefined) === 'main');
  check('未知 from 回落选币榜（不炸页）', boardFromOrigin('nope') === 'main');

  check('从选币榜Y 进 K 线后「返回榜单」落 /screener-y',
    backHrefFromOrigin('screener-y') === '/screener-y',
    backHrefFromOrigin('screener-y'));
  check('从选币榜 进 K 线后「返回榜单」仍落 /screener',
    backHrefFromOrigin('screener') === '/screener',
    backHrefFromOrigin('screener'));
  check('缺省 / 空 from 仍回选币榜（原路径不变）',
    backHrefFromOrigin(null) === '/screener' && backHrefFromOrigin('') === '/screener');
  check('from=review 回到复盘选币', backHrefFromOrigin('review') === '/review');
  check('from=quotes 回到行情', backHrefFromOrigin('quotes') === '/quotes');
  check('from=terminal 回到工作台', backHrefFromOrigin('terminal') === '/terminal');
  check('from=markets 回到合约清单', backHrefFromOrigin('markets') === '/markets');
  check('未知 from 仍回选币榜', backHrefFromOrigin('zzz') === '/screener');

  // 护栏：K 线页必须真的读 from= 来决定返回，不能再写死 to="/screener"。
  // 只断言源码接线，避免把 ChartWorkspace / 网关 fetch 拉进这条闸。
  // 路径相对 apps/web（npm run verify:board-split 的 cwd）；bundled 后
  // import.meta.url 落在 tests/.out/，不能再用相对它的 ../src。
  const marketSrc = readFileSync('src/pages/MarketPage.tsx', 'utf8');
  check('MarketPage 用 backHrefFromOrigin 决定返回',
    marketSrc.includes('backHrefFromOrigin') && marketSrc.includes("params.get('from')"));
  check('MarketPage 不再写死 to="/screener"',
    !/to=["']\/screener["']/.test(marketSrc));
  check('从 Y 进 K 线时详情/SSE 也走 Y 板面',
    marketSrc.includes('boardFromOrigin') && marketSrc.includes('fetchSymbolDetail(symbol, direction, board)'));

  const screenerSrc = readFileSync('src/pages/ScreenerPage.tsx', 'utf8');
  check('选币榜页用 originTagFor 打 from=', screenerSrc.includes('originTagFor'));
  // 整页刷新会把 3MB latest 连拉两遍，公网一直打转；三块板面一律禁止。
  check('X/main/y 首访都不 location.reload', !screenerSrc.includes('location.reload'));
}

console.log('\n=== X. v1.3.0 复盘消歧 / 无周期 / 路由卸载 ===');
{
  // X v1.3.0 复刻不等于可执行：复盘观察 DMR 须明示，未来只经 X overrides 解锁评审。
  const banner = renderToStaticMarkup(<MemoryRouter><ConfirmedBanner
    {...{board:'x' as const}} long={[{symbol:'BTCUSDT',score_up:80,staircase_score:70} as CandidateRow]}
    short={[]} /></MemoryRouter>);
  check('X DMR 横幅不得冒称 paper 可执行', !banner.includes('可 paper 执行'));
  check('X DMR 横幅币种 K 线来源正确', banner.includes('from=screener-x'));
  check('board=x API 与 URL 往返', reviewQueryString({board:'x'}).includes('board=x')
    && searchToState('?board=x').board === 'x');
  check('X 无周期，不默认添加 nocycle', !BOARDS.x.cycleEnabled
    && searchToState('?board=x').excludeCycleReset === undefined);
  const app = readFileSync('src/App.tsx','utf8');
  check('三条路由都有组件 key，切板不残留旧行', BOARD_KEYS.every(k =>
    app.includes(`path="${BOARDS[k].route}" element={<ScreenerPage key="${k}" board="${k}"`)));
  const empty = renderToStaticMarkup(<ReviewBoardBar value="x" onChange={() => {}} error="ledger_not_ready" />);
  check('X 提示自身路径与 --board x', empty.includes('data/coin-selection-x/review/ledger.sqlite')
    && empty.includes('replay_screener_y.py --board x'));
  const board = renderToStaticMarkup(<ReviewBoardBar value="main" onChange={() => {}} />);
  const old = renderToStaticMarkup(<ParamVersionBar value="all" onChange={() => {}}
    cov={{parameter_segments:[{version:'param-v1.3.0-dual-path-sticky',trades:771},
      {version:'param-v1.4.0-staircase-confirm-dmr',trades:1}]} as never} />);
  // 仅 HTML 消歧夹具，不是绩效；常驻身份必须在文本节点而非仅 title。
  check('同屏两个 v1.3.0 可见层身份不同', old.includes('>param-v1.3.0-dual-path-sticky</span>')
    && board.includes('>param-v1.3.0-screener-x</span>') && board.includes('>选币榜X</span>'));
  const names = [...board.matchAll(/class="review-param-btn-name">([^<]+)</g)].map(m=>m[1]);
  check('复盘按钮顺序 v1.4.0 → v1.3.0 → v2.0.0', names.join(',') === 'v1.4.0,v1.3.0,v2.0.0');
  writeFileSync('tests/.out/x-review-disambiguation.html', board+old);
}

console.log('\n=== 9. 分区计数取服务端字段（T18 / 文档B §3.3） ===');
{
  // 分区计数必须来自服务端下发的字段，前端不得重算。zone_ceiling 存在且严于
  // state 时以它为准 —— 这是文档B §3.3 对 board-split 闸的扩展要求。
  const poolSrc = readFileSync('src/shared/lib/poolFilter.ts', 'utf8');
  check('displayZone 优先读服务端 product_zone', poolSrc.includes('r.product_zone'));
  check('displayZone 认 zone_ceiling', poolSrc.includes('r.zone_ceiling'));
  check('前端不含 A–F 到分区的查表常量', !/['"][A-F]{3}['"]\s*:/.test(poolSrc));

  const stripped = poolSrc
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/(^|[^:])\/\/.*$/gm, '$1');
  check('displayZone 的代码不读 mcap_grade_*', !/mcap_grade_/.test(stripped));
  check('displayZone 的代码不含天花板切点常量',
    !/2\.1[^0-9]/.test(stripped) && !/-0\.5[^0-9]/.test(stripped));
}

console.log('\n=== 10. 选币榜Y 入选时间：与选币榜同一套「日期+时间」 ===');
{
  // 选币榜Y 的入选时间必须与选币榜同一格式：M.D HH:mm（含日期），
  // 时间半边落在 15 分钟节点（:00/:15/:30/:45）。缺日期就是回归失败。
  const ZONES: ScreenerState[] = [
    'CONFIRMED', 'QUALIFIED', 'WATCH', 'ELIMINATED', 'DATA_INSUFFICIENT', 'LOW_CONFIDENCE',
  ];
  function enterRow(iso: string, state: ScreenerState, direction: 'up' | 'down'): CandidateRow {
    return {
      rank: 1, symbol: 'GRIDUSDT', underlying_asset: 'GRID',
      canonical_asset_id: 'grid', contract_multiplier: 1,
      direction, state, state_enter_time_utc: iso, state_duration_minutes: 15,
      state_enter_price: 1, score_up: 50, score_down: 50,
      direction_confidence: 1, liquidity_score: 80, liquidity_grade: 'A',
      mcap_grade_30m: 'A', mcap_grade_2h: 'C', mcap_grade_6h: null, mcap_tf: {},
      momentum_score: 70, mcap_momentum_score: 60, staircase_score: 55,
      consistency_score: 0.7, rank_velocity_score: 50, risk_score: 100,
      data_confidence: 90, ret_15m: null, ret_1h: 0, ret_4h: 0, ret_24h: 0,
      ret_1w: 0, ret_1mo: 0, ret_since_anchor: 0,
      aqv_6d_m: 10, aqv_12d_m: 9, aqv_26d_m: 8,
      circulating_supply: 1e9, market_cap_coingecko: 1e9, market_cap_calculated: 1e9,
      supply_source: 'CG', data_mode: 'LIVE', supply_as_of_utc: null,
      mapping_confidence: 1, risk_flags: [], reason_codes: [], not_confirmed_reasons: [],
    };
  }
  function cell(iso: string, state: ScreenerState, direction: 'up' | 'down') {
    const cols = buildCols('compact', direction);
    const col = cols.find((c) => c.id === 'enter_at')!;
    return renderToStaticMarkup(<>{col.render(enterRow(iso, state, direction))}</>)
      .replace(/<[^>]*>/g, '')
      .replace(/<!--.*?-->/g, '')
      .trim();
  }

  check('09:17 吸附到 9.1 09:15（含日期）',
    formatEnterClock('2026-09-01T02:17:44Z') === '9.1 09:15');
  check('09:00 / 09:30 / 09:45 带日期',
    formatEnterClock('2026-09-01T02:00:00Z') === '9.1 09:00'
    && formatEnterClock('2026-09-01T02:30:00Z') === '9.1 09:30'
    && formatEnterClock('2026-09-01T02:45:05Z') === '9.1 09:45');
  check('跨月日 8.30 10:45（与选币榜截图一致）',
    formatEnterClock('2026-08-30T03:45:00Z') === '8.30 10:45');
  check('主板样例 8.17 14:15 不变',
    formatEnterClock('2026-08-17T07:15:00Z') === '8.17 14:15');

  const GRID = [
    ['2026-09-01T02:00:00Z', '9.1 09:00'],
    ['2026-09-01T02:15:00Z', '9.1 09:15'],
    ['2026-09-01T02:30:00Z', '9.1 09:30'],
    ['2026-09-01T02:45:00Z', '9.1 09:45'],
    ['2026-09-01T02:17:44Z', '9.1 09:15'],
    ['2026-08-30T03:45:00Z', '8.30 10:45'],
  ] as const;
  for (const zone of ZONES) {
    for (const direction of ['up', 'down'] as const) {
      const pool = direction === 'up' ? '上涨候选池' : '下跌候选池';
      for (const [iso, want] of GRID) {
        const got = cell(iso, zone, direction);
        check(`Y ${zone} ${pool} ${iso} → ${want}`, got === want, got);
      }
    }
  }

  const colSrc = readFileSync('src/features/screener/components/candidateColumns.tsx', 'utf8');
  check('列工厂不再给 Y 单独走 clock 样式',
    !colSrc.includes("board === 'y' ? 'clock'") && !colSrc.includes('enterStyle'));
  check('formatEnterClock 调用不含 style=clock',
    !/formatEnterClock\([^)]*'clock'/.test(colSrc));
}

console.log('\n=== 9. 选币榜首屏不得整页刷新 / 不得挡在 confirmed 后面 ===');
{
  const pageSrc = readFileSync('src/pages/ScreenerPage.tsx', 'utf8');
  check('不再 location.reload（Y 快照会连拉两遍，公网一直打转）',
    !pageSrc.includes('location.reload'));
  check('不再 ui-build-loaded sessionStorage 自检刷新',
    !pageSrc.includes('ui-build-loaded'));
  check('快照到位才轮询报价', pageSrc.includes('usePricePoll(8000, !!snap)'));
  check('同一 scan 的 SSE 推送不再重拉 latest',
    pageSrc.includes('snapRef.current?.meta.scan_id === sid'));
  const apiSrc = readFileSync('src/shared/api/screener.ts', 'utf8');
  check('latest 超时会重试，不把一次公网抖动写成加载失败',
    apiSrc.includes('retries: 2') && apiSrc.includes('timeoutMs: 12_000'));
  check('SSE 断线轮询走 /meta 而不是再拉 2MB latest',
    apiSrc.includes('fetchScreenerMeta(board)') && apiSrc.includes('`${prefixOf(board)}/meta`'));
  check('隐藏标签页会关掉 EventSource，给 /latest 让出 HTTP/1.1 连接槽',
    apiSrc.includes('visibilitychange') && apiSrc.includes('document.hidden'));
  check('工作台状态栏不再每 15s 拉 /screener/latest',
    !readFileSync('src/features/workspace/TerminalStatusBar.tsx', 'utf8').includes('/screener/latest'));
}

console.log('\n=== 216 天花板页头跟快照开关 ===');
{
  const yOn = screenerBrandNote(BOARDS.y, { mcap_effective: { mode: 'on' } } as never);
  check('Y mode=on 显示开', !!yOn && yOn.includes('216 天花板 开') && !yOn.includes('默认关'), yOn);
  const yOff = screenerBrandNote(BOARDS.y, { mcap_effective: { mode: 'off' } } as never);
  check('Y mode=off 显示关', !!yOff && yOff.includes('216 天花板 关') && !yOff.includes('默认关'), yOff);
  const xSh = screenerBrandNote(BOARDS.x, { mcap_effective: { mode: 'shadow' } } as never);
  check('X shadow 显示影子', !!xSh && xSh.includes('影子'), xSh);
  check('静态 note 不再写默认关', !BOARDS.y.note?.includes('默认关'));
  check('无快照时不捏造开关', mcapCeilingLabel(undefined) === null);
}

console.log(failures === 0 ? '\nALL PASS (0 failures)' : '\n' + failures + ' FAILURES');
process.exit(failures === 0 ? 0 : 1);
