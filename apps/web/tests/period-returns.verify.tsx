/**
 * 选币榜 `1Week / 1Month` 两列的验证闸。
 *
 *   cd apps/web && npm run verify:period-returns
 *
 * 覆盖：列顺序 24h → 1Week → 1Month → 锚点以来（三种列预设）、七个分区 × 上涨/下跌
 * 两个候选池的单元格渲染、升/降序全序与算不出的行沉底、两列互相独立、对既有列排序的
 * 非回归、表头 sortable/高亮/箭头与 1h/4h/24h 逐字节一致、点击表头的 store 行为，
 * 以及生产快照 1050 行的实测单调性。
 */
import { renderToStaticMarkup } from 'react-dom/server';
import { MemoryRouter } from 'react-router-dom';
import { readFileSync } from 'node:fs';
import { CandidateDataGrid } from '../src/features/screener/components/CandidateDataGrid';
import { buildCols } from '../src/features/screener/components/candidateColumns';
import { sortRows } from '../src/shared/lib/sortRows';
import { useScreenerUi, type ColumnPreset } from '../src/shared/stores/screenerUi';
import type { CandidateRow, ScreenerState } from '../src/shared/types/screener';

let failures = 0;
function check(name: string, ok: boolean, extra = '') {
  console.log((ok ? '  ok   ' : '  FAIL ') + name + (extra ? '  ' + extra : ''));
  if (!ok) failures += 1;
}

/** 两列的列 id / 表头文案 / sortId —— 三者在本文件里只写这一次。 */
const COLS = [
  { id: 'ret_1w', label: '1Week', field: 'ret_1w' },
  { id: 'ret_1mo', label: '1Month', field: 'ret_1mo' },
] as const;
type NewCol = (typeof COLS)[number];

/** 参照组：新两列的交互必须与这三列完全一致。 */
const REF_COLS = [
  { id: 'ret_1h', label: '1h' },
  { id: 'ret_4h', label: '4h' },
  { id: 'ret_24h', label: '24h' },
] as const;

const PRESETS: ColumnPreset[] = ['compact', 'trader', 'full_s37'];

/** 「选币榜」的七个分区。DMR 不是 state，而是 CONFIRMED 之上的可执行子集标记。 */
const ZONES: { name: string; state: ScreenerState; dmr?: boolean }[] = [
  { name: 'DMR区', state: 'CONFIRMED', dmr: true },
  { name: '确认区', state: 'CONFIRMED' },
  { name: '符合区', state: 'QUALIFIED' },
  { name: '观察区', state: 'WATCH' },
  { name: '淘汰区', state: 'ELIMINATED' },
  { name: '数据不足区', state: 'DATA_INSUFFICIENT' },
  { name: '低置信度区', state: 'LOW_CONFIDENCE' },
];

function row(
  symbol: string,
  ret_1w: number | null,
  ret_1mo: number | null,
  opts: { score?: number; state?: ScreenerState; dmr?: boolean; direction?: 'up' | 'down' } = {},
): CandidateRow {
  const score = opts.score ?? 50;
  return {
    rank: 1, symbol, underlying_asset: symbol.replace('USDT', ''),
    canonical_asset_id: symbol.toLowerCase(), contract_multiplier: 1,
    direction: opts.direction ?? 'up', state: opts.state ?? 'WATCH',
    state_enter_time_utc: '2026-08-22T12:00:00Z', state_duration_minutes: 15,
    state_enter_price: 1, score_up: score, score_down: 100 - score,
    direction_confidence: 1, liquidity_score: 80, liquidity_grade: 'A',
    mcap_grade_30m: 'A', mcap_grade_2h: 'C', mcap_grade_6h: null, mcap_tf: {},
    momentum_score: 70, mcap_momentum_score: 60, staircase_score: 55,
    consistency_score: 0.7, rank_velocity_score: 50, risk_score: 100,
    data_confidence: 90, ret_15m: null, ret_1h: 0.01, ret_4h: 0.02, ret_24h: 0.03,
    ret_1w, ret_1mo,
    ret_since_anchor: 0.05, aqv_6d_m: 10, aqv_12d_m: 9, aqv_26d_m: 8,
    circulating_supply: 1e9, market_cap_coingecko: 1e9, market_cap_calculated: 1e9,
    supply_source: 'CG', data_mode: 'LIVE', supply_as_of_utc: null,
    mapping_confidence: 1, risk_flags: [], reason_codes: [], not_confirmed_reasons: [],
    dmr_selected: opts.dmr,
  };
}

const val = (r: CandidateRow, c: NewCol) => r[c.field];
/** 单元格的可见文本。表格是虚拟滚动的，SSR 渲染不出行，所以逐列取 render。 */
function cellText(cols: ReturnType<typeof buildCols>, id: string, r: CandidateRow) {
  const col = cols.find((c) => c.id === id)!;
  return renderToStaticMarkup(<>{col.render(r)}</>)
    .replace(/<[^>]*>/g, '')
    .replace(/<!--.*?-->/g, '')
    .trim();
}

console.log('\n=== 1. 列结构与顺序：24h → 1Week → 1Month → 锚点以来 ===');
for (const preset of PRESETS) {
  for (const direction of ['up', 'down'] as const) {
    const ids = buildCols(preset, direction).map((c) => c.id);
    const at = (id: string) => ids.indexOf(id);
    check(
      preset + '/' + direction + ' 两列夹在 24h 与 锚点以来 之间且顺序为 1Week→1Month',
      at('ret_24h') >= 0
        && at('ret_1w') === at('ret_24h') + 1
        && at('ret_1mo') === at('ret_24h') + 2
        && at('ret_anchor') === at('ret_24h') + 3,
      ids.slice(Math.max(0, at('ret_24h') - 1), at('ret_24h') + 5).join(' → '),
    );
    const cols = buildCols(preset, direction);
    check(
      preset + '/' + direction + ' 表头文案与 sortKey 正确',
      COLS.every((c) => {
        const col = cols.find((x) => x.id === c.id)!;
        return col && col.label === c.label && col.sortKey === c.id;
      }),
      COLS.map((c) => cols.find((x) => x.id === c.id)?.label).join(','),
    );
    check(
      preset + '/' + direction + ' 两列宽度与既有 1h/4h/24h 一致',
      COLS.every((c) => cols.find((x) => x.id === c.id)!.width
        === cols.find((x) => x.id === 'ret_24h')!.width),
    );
  }
}

console.log('\n=== 2. 七个分区 × 上涨/下跌候选池的单元格渲染 ===');
for (const zone of ZONES) {
  for (const direction of ['up', 'down'] as const) {
    const cols = buildCols('compact', direction);
    const r = row('ZONEUSDT', 0.1234, -0.0567, { state: zone.state, dmr: zone.dmr, direction });
    const got = COLS.map((c) => cellText(cols, c.id, r));
    check(zone.name + ' ' + (direction === 'up' ? '上涨候选池' : '下跌候选池') + ' 渲染涨跌幅',
      JSON.stringify(got) === JSON.stringify(['+12.34%', '-5.67%']), got.join(' / '));
    const none = row('ZONEUSDT', null, null, { state: zone.state, dmr: zone.dmr, direction });
    check(zone.name + ' ' + (direction === 'up' ? '上涨候选池' : '下跌候选池') + ' 算不出显示 —',
      COLS.every((c) => cellText(cols, c.id, none) === '—'));
  }
}
console.log('  (七个分区共用同一张表 —— 列由 buildCols 单点产出，与 state / dmr_selected 无关)');
const upCols = buildCols('compact', 'up').map((c) => c.id).join(',');
const downCols = buildCols('compact', 'down').map((c) => c.id).join(',');
check('上涨池与下跌池列结构完全一致', upCols === downCols);

console.log('\n=== 3. 涨/跌配色沿用既有 ret 单元格 ===');
{
  const cols = buildCols('compact', 'up');
  const html = (v: number | null) =>
    renderToStaticMarkup(<>{cols.find((c) => c.id === 'ret_1w')!.render(row('XUSDT', v, v))}</>);
  check('正值 class="ret up"', html(0.05).includes('class="ret up"'), html(0.05));
  check('负值 class="ret down"', html(-0.05).includes('class="ret down"'), html(-0.05));
  check('null 走 muted 占位', html(null).includes('muted'), html(null));
  const ref = renderToStaticMarkup(<>{cols.find((c) => c.id === 'ret_24h')!.render(row('XUSDT', 0.05, 0.05))}</>)
    .replace(/>[^<]*</, '><');
  check('与 24h 列同一套 DOM 结构', html(0.05).replace(/>[^<]*</, '><') === ref);
}

console.log('\n=== 4. 升序 / 降序全序（每列各测一次）===');
const SHUFFLED: (number | null)[] = [0.02, null, 0.5, -0.3, 0.11, -0.08, 0.0];
const WANT_DESC = ['0.5', '0.11', '0.02', '0', '-0.08', '-0.3', 'null'];
for (const c of COLS) {
  const rows = SHUFFLED.map((v, i) =>
    row('S' + i + 'USDT', c.id === 'ret_1w' ? v : 0.03, c.id === 'ret_1mo' ? v : 0.03));
  const desc = sortRows(rows, 'up', c.id, true, {}).map((r) => String(val(r, c)));
  const asc = sortRows(rows, 'up', c.id, false, {}).map((r) => String(val(r, c)));
  check(c.label + ' 降序：大→小，算不出的沉底',
    JSON.stringify(desc) === JSON.stringify(WANT_DESC), desc.join(','));
  check(c.label + ' 升序：为降序逆序（算不出的置顶）',
    JSON.stringify(asc) === JSON.stringify([...WANT_DESC].reverse()), asc.join(','));
}

console.log('\n=== 5. 同值稳定性：回落到后端 rank/Score 次序 ===');
const tie = [
  row('T1USDT', 0.2, 0.2, { score: 90 }),
  row('T2USDT', 0.2, 0.2, { score: 80 }),
  row('T3USDT', -0.9, -0.9, { score: 70 }),
  row('T4USDT', 0.2, 0.2, { score: 60 }),
];
for (const c of COLS) {
  const got = sortRows(tie, 'up', c.id, true, {}).map((r) => r.symbol);
  check(c.label + ' 同值三行保持输入次序，最小值沉底',
    JSON.stringify(got) === JSON.stringify(['T1USDT', 'T2USDT', 'T4USDT', 'T3USDT']), got.join(','));
}

console.log('\n=== 6. 两列互相独立 ===');
const mixed = [
  row('M1USDT', -0.5, 0.9),
  row('M2USDT', 0.9, 0.1),
  row('M3USDT', 0.1, -0.5),
];
for (const c of COLS) {
  const got = sortRows(mixed, 'up', c.id, true, {}).map((r) => val(r, c));
  check('按 ' + c.label + ' 排序只看 ' + c.label + ' 列',
    JSON.stringify(got) === JSON.stringify([0.9, 0.1, -0.5]), got.join(','));
}

console.log('\n=== 7. 不影响其它列的排序 ===');
const base = SHUFFLED.map((v, i) => row('B' + i + 'USDT', v, v, { score: 40 + i }));
const byScore = sortRows(base, 'up', 'score', true, {}).map((r) => r.symbol);
check('score 排序不受新分支影响',
  JSON.stringify(byScore)
  === JSON.stringify([...base].sort((a, b) => b.score_up - a.score_up).map((r) => r.symbol)));
for (const ref of REF_COLS) {
  const got = sortRows(base, 'up', ref.id, true, {}).map((r) => r.symbol);
  check(ref.label + ' 列排序仍与输入等长且不重不漏',
    got.length === base.length && new Set(got).size === base.length);
}
check('锚点以来 列排序不受影响', sortRows(base, 'up', 'ret_anchor', true, {}).length === base.length);
check('未知 sortId 仍回落 Score（原行为）',
  JSON.stringify(sortRows(base, 'up', 'nope', true, {}).map((r) => r.symbol)) === JSON.stringify(byScore));

console.log('\n=== 8. 表头可点排序 + 高亮 + 箭头（与 1h/4h/24h 逐字节一致）===');
// React 19 SSR 会在相邻文本节点之间插入 <!-- -->，取整段 innerHTML 再去掉注释
const HEAD = /<div class="(th[^"]*)"[^>]*>(.*?)<\/div>/g;
const clean = (x: string) => x.replace(/<!--.*?-->/g, '').trim();
function headers(sortId: string, desc: boolean, preset: ColumnPreset = 'compact') {
  // 排序状态由页面注入（见 mcap-sort.verify.tsx 同处注释）。
  useScreenerUi.setState({ sortId: sortId as never, sortDesc: desc });
  const ui = useScreenerUi.getState();
  const html = renderToStaticMarkup(
    <MemoryRouter>
      <CandidateDataGrid
        // X v1.3.0 来源必填；main v1.4.0 复刻与复盘回归保持原标记，演进只改 X overrides。
        originTag="screener"
        rows={[row('AAAUSDT', 0.1, null)]}
        preset={preset}
        direction="up"
        sortId={ui.sortId}
        sortDesc={ui.sortDesc}
        onSort={ui.setSort}
      />
    </MemoryRouter>,
  );
  return [...html.matchAll(HEAD)].map((m) => ({ cls: m[1], label: clean(m[2]) }));
}
const find = (hs: { cls: string; label: string }[], label: string) =>
  hs.find((h) => h.label.startsWith(label))!;

for (const c of COLS) {
  const hs = headers(c.id, true);
  const mine = find(hs, c.label);
  check(c.label + ' 表头 sortable', mine.cls.includes('sortable'), JSON.stringify(mine.cls));
  check(c.label + ' 选中时表头高亮 (class sorted)', mine.cls.includes('sorted'), JSON.stringify(mine.cls));
  check(c.label + ' 选中时降序箭头 ↓', mine.label === c.label + ' ↓', JSON.stringify(mine.label));
  check(c.label + ' 选中时其它 ret 列不高亮',
    [...COLS, ...REF_COLS].filter((x) => x.label !== c.label)
      .every((x) => !find(hs, x.label).cls.includes('sorted')));
  const ascHs = headers(c.id, false);
  check(c.label + ' 升序箭头 ↑', find(ascHs, c.label).label === c.label + ' ↑',
    JSON.stringify(find(ascHs, c.label).label));
  // 与参照列逐字节比对：class 串（视觉反馈/过渡动画都挂在 class 上）必须一模一样
  const refSelected = find(headers('ret_24h', true), '24h');
  check(c.label + ' 选中态 class 与 24h 完全相同',
    mine.cls === refSelected.cls, mine.cls + ' vs ' + refSelected.cls);
  const unsel = find(headers('score', true), c.label);
  const refUnsel = find(headers('score', true), '24h');
  check(c.label + ' 未选中态 class 与 24h 完全相同',
    unsel.cls === refUnsel.cls && !unsel.cls.includes('sorted'), unsel.cls);
}
for (const preset of PRESETS) {
  const hs = headers('ret_1mo', true, preset);
  check(preset + ' 预设下两列同样可点排序且箭头正确',
    COLS.every((c) => find(hs, c.label).cls.includes('sortable'))
    && find(hs, '1Month').label === '1Month ↓'
    && !find(hs, '1Week').cls.includes('sorted'));
}
// 1Week 的表头不能被 1Month 的前缀匹配吃掉，反之亦然
check('1Week / 1Month 表头互不混淆', (() => {
  const hs = headers('score', true);
  return hs.filter((h) => h.label.startsWith('1Week')).length === 1
    && hs.filter((h) => h.label.startsWith('1Month')).length === 1;
})());

console.log('\n=== 9. 点击表头的 store 行为（与既有列一致）===');
for (const c of COLS) {
  useScreenerUi.setState({ sortId: 'score' as never, sortDesc: true });
  useScreenerUi.getState().setSort(c.id);
  check(c.label + ' 首击 → sortId=' + c.id + ', 降序',
    useScreenerUi.getState().sortId === c.id && useScreenerUi.getState().sortDesc === true);
  useScreenerUi.getState().setSort(c.id);
  check(c.label + ' 再击 → 同列切升序', useScreenerUi.getState().sortDesc === false);
  useScreenerUi.getState().setSort('ret_24h');
  check(c.label + ' → 24h 换列 → 重置为降序',
    useScreenerUi.getState().sortId === 'ret_24h' && useScreenerUi.getState().sortDesc === true);
}
useScreenerUi.getState().setSort('ret_1w');
useScreenerUi.getState().resetFilters();
check('恢复默认 → 回到 score 降序',
  useScreenerUi.getState().sortId === 'score' && useScreenerUi.getState().sortDesc === true);

console.log('\n=== 10. 生产快照实测（data/coin-selection/latest.json）===');
const SNAPSHOT = process.env.HERMES_SNAPSHOT
  ?? '../../data/coin-selection/latest.json';   // npm script 从 apps/web 运行
let snap: { long_pool: CandidateRow[]; short_pool: CandidateRow[] } | null = null;
try {
  snap = JSON.parse(readFileSync(SNAPSHOT, 'utf8'));
} catch {
  console.log('  skip  没有生产快照 ' + SNAPSHOT + '（设 HERMES_SNAPSHOT 指定路径）');
}
if (snap)
for (const [poolName, pool] of [['上涨池', snap.long_pool], ['下跌池', snap.short_pool]] as const) {
  const rows = pool as CandidateRow[];
  for (const c of COLS) {
    const present = rows.filter((r) => val(r, c) != null).length;
    check(poolName + ' ' + c.label + ' 字段下发到位 (' + present + '/' + rows.length + ' 行有值)',
      rows.every((r) => c.field in r) && present > 0);
    const key = (v: number | null | undefined) => (v == null ? Number.NEGATIVE_INFINITY : v);
    const desc = sortRows(rows, 'up', c.id, true, {}).map((r) => key(val(r, c)));
    check(poolName + ' ' + c.label + ' 降序单调 (' + rows.length + ' 行)',
      desc.every((v, i) => i === 0 || desc[i - 1] >= v),
      '首' + desc[0] + ' 末' + desc[desc.length - 1]);
    const asc = sortRows(rows, 'up', c.id, false, {}).map((r) => key(val(r, c)));
    check(poolName + ' ' + c.label + ' 升序单调', asc.every((v, i) => i === 0 || asc[i - 1] <= v));
    const sorted = sortRows(rows, 'up', c.id, true, {});
    check(poolName + ' ' + c.label + ' 排序不增删不重复行',
      sorted.length === rows.length
      && new Set(sorted.map((r) => r.symbol)).size === new Set(rows.map((r) => r.symbol)).size);
    let tieOk = true;
    for (let i = 1; i < sorted.length; i += 1) {
      if (desc[i] === desc[i - 1] && sorted[i].rank < sorted[i - 1].rank) tieOk = false;
    }
    check(poolName + ' ' + c.label + ' 同值内保持后端 rank 次序', tieOk);
  }
  // 每个分区都要有数据，不能只有确认区才填得上
  for (const zone of ZONES) {
    const zoneRows = zone.dmr
      ? rows.filter((r) => r.dmr_selected)
      : rows.filter((r) => r.state === zone.state);
    if (!zoneRows.length) {
      console.log('  skip  ' + poolName + ' ' + zone.name + ' 本轮快照无行');
      continue;
    }
    check(poolName + ' ' + zone.name + ' 两列字段齐全 (' + zoneRows.length + ' 行)',
      zoneRows.every((r) => COLS.every((c) => c.field in r)));
  }
}

console.log(failures === 0 ? '\nALL PASS (' + '0 failures)' : '\n' + failures + ' FAILURES');
process.exit(failures === 0 ? 0 : 1);
