/**
 * 选币榜 `30m流通市值 / 2h流通市值 / 6h流通市值` 三列排序功能的验证闸。
 *
 *   cd apps/web && npm run verify:mcap-sort
 *
 * 覆盖：等级阶梯与排序秩、三个周期的升/降序全序、同级稳定性、三列互相独立、
 * 对既有列排序的非回归、表头 sortable/高亮/箭头（含三种列预设）、
 * 点击表头的 store 行为，以及生产快照 1050 行的实测单调性。
 */
import { renderToStaticMarkup } from 'react-dom/server';
import { MemoryRouter } from 'react-router-dom';
import { readFileSync } from 'node:fs';
import { CandidateDataGrid } from '../src/features/screener/components/CandidateDataGrid';
import { sortRows } from '../src/shared/lib/sortRows';
import { useScreenerUi } from '../src/shared/stores/screenerUi';
import {
  MCAP_GRADE_DESC_ORDER,
  MCAP_SORT_ID,
  mcapGradeOf,
  mcapGradeRank,
  timeframeOfMcapSortId,
} from '../src/shared/lib/mcapGrade';
import type {
  CandidateRow,
  McapTfGrade,
  McapTimeframe,
} from '../src/shared/types/screener';

let failures = 0;
function check(name: string, ok: boolean, extra = '') {
  console.log((ok ? '  ok   ' : '  FAIL ') + name + (extra ? '  ' + extra : ''));
  if (!ok) failures += 1;
}

function row(
  symbol: string,
  g30: McapTfGrade | null,
  g2h: McapTfGrade | null,
  g6h: McapTfGrade | null,
  score = 50,
): CandidateRow {
  return {
    rank: 1, symbol, underlying_asset: symbol.replace('USDT', ''),
    canonical_asset_id: symbol.toLowerCase(), contract_multiplier: 1,
    direction: 'up', state: 'WATCH',
    state_enter_time_utc: '2026-08-22T12:00:00Z', state_duration_minutes: 15,
    state_enter_price: 1, score_up: score, score_down: 100 - score,
    direction_confidence: 1, liquidity_score: 80, liquidity_grade: 'A',
    mcap_grade_30m: g30, mcap_grade_2h: g2h, mcap_grade_6h: g6h, mcap_tf: {},
    momentum_score: 70, mcap_momentum_score: 60, staircase_score: 55,
    consistency_score: 0.7, rank_velocity_score: 50, risk_score: 100,
    data_confidence: 90, ret_15m: null, ret_1h: 0, ret_4h: 0, ret_24h: 0,
    ret_1w: 0, ret_1mo: 0,
    ret_since_anchor: 0, aqv_6d_m: 10, aqv_12d_m: 9, aqv_26d_m: 8,
    circulating_supply: 1e9, market_cap_coingecko: 1e9, market_cap_calculated: 1e9,
    supply_source: 'CG', data_mode: 'LIVE', supply_as_of_utc: null,
    mapping_confidence: 1, risk_flags: [], reason_codes: [], not_confirmed_reasons: [],
  };
}

const TFS: McapTimeframe[] = ['30m', '2h', '6h'];
const LABEL: Record<McapTimeframe, string> = {
  '30m': '30m流通市值', '2h': '2h流通市值', '6h': '6h流通市值',
};
const g = (r: CandidateRow, tf: McapTimeframe) => mcapGradeOf(r, tf) ?? '—';

console.log('\n=== 1. 等级阶梯与秩 ===');
check('A>B>C>D>E>F>判不出级',
  ['A', 'B', 'C', 'D', 'E', 'F'].map((x) => mcapGradeRank(x as McapTfGrade)).join(',') === '6,5,4,3,2,1'
  && mcapGradeRank(null) === 0 && mcapGradeRank(undefined) === 0,
  '[' + ['A', 'B', 'C', 'D', 'E', 'F', null].map((x) => mcapGradeRank(x as McapTfGrade)).join(',') + ']');
check('sortId ↔ 周期映射唯一',
  TFS.every((tf) => timeframeOfMcapSortId(MCAP_SORT_ID[tf]) === tf)
  && timeframeOfMcapSortId('score') === null
  && timeframeOfMcapSortId('grade') === null);

console.log('\n=== 2. 降序 / 升序全序（每个周期各测一次）===');
const SHUFFLED: (McapTfGrade | null)[] = ['D', null, 'A', 'F', 'C', 'E', 'B'];
for (const tf of TFS) {
  const rows = SHUFFLED.map((gr, i) =>
    row('S' + i + 'USDT', tf === '30m' ? gr : 'C', tf === '2h' ? gr : 'C', tf === '6h' ? gr : 'C'));
  const desc = sortRows(rows, 'up', MCAP_SORT_ID[tf], true, {}).map((r) => g(r, tf));
  const asc = sortRows(rows, 'up', MCAP_SORT_ID[tf], false, {}).map((r) => g(r, tf));
  const wantDesc = MCAP_GRADE_DESC_ORDER.map((x) => x ?? '—');
  check(tf + ' 降序 A→F→—', JSON.stringify(desc) === JSON.stringify(wantDesc), desc.join(''));
  check(tf + ' 升序 为降序逆序',
    JSON.stringify(asc) === JSON.stringify([...wantDesc].reverse()), asc.join(''));
}

console.log('\n=== 3. 同级稳定性：回落到后端 rank/Score 次序 ===');
const tie = [
  row('T1USDT', 'A', 'A', 'A', 90),
  row('T2USDT', 'A', 'A', 'A', 80),
  row('T3USDT', 'F', 'F', 'F', 70),
  row('T4USDT', 'A', 'A', 'A', 60),
];
const tieSorted = sortRows(tie, 'up', 'mcap_2h', true, {}).map((r) => r.symbol);
check('同为 A 的三行保持输入次序，F 沉底',
  JSON.stringify(tieSorted) === JSON.stringify(['T1USDT', 'T2USDT', 'T4USDT', 'T3USDT']),
  tieSorted.join(','));

console.log('\n=== 4. 三列互相独立 ===');
const mixed = [
  row('M1USDT', 'F', 'A', 'C'),
  row('M2USDT', 'A', 'C', 'F'),
  row('M3USDT', 'C', 'F', 'A'),
];
for (const tf of TFS) {
  const got = sortRows(mixed, 'up', MCAP_SORT_ID[tf], true, {}).map((r) => g(r, tf));
  check('按 ' + tf + ' 排序只看 ' + tf + ' 列',
    JSON.stringify(got) === JSON.stringify(['A', 'C', 'F']), got.join(''));
}

console.log('\n=== 5. 不影响其它列的排序 ===');
const base = SHUFFLED.map((gr, i) => row('B' + i + 'USDT', gr, gr, gr, 40 + i));
const byScore = sortRows(base, 'up', 'score', true, {}).map((r) => r.symbol);
check('score 排序不受新分支影响',
  JSON.stringify(byScore) === JSON.stringify([...base].sort((a, b) => b.score_up - a.score_up).map((r) => r.symbol)));
check('等级(流动性 A–D) 列仍可排序', sortRows(base, 'up', 'grade', true, {}).length === base.length);
check('未知 sortId 回落 Score（原行为）',
  JSON.stringify(sortRows(base, 'up', 'nope', true, {}).map((r) => r.symbol)) === JSON.stringify(byScore));

console.log('\n=== 6. 表头可点排序 + 箭头 ===');
// React 19 SSR 会在相邻文本节点之间插入 <!-- -->，取整段 innerHTML 再去掉注释
const HEAD = /<div class="(th[^"]*)"[^>]*>(.*?)<\/div>/g;
const clean = (x: string) => x.replace(/<!--.*?-->/g, '').trim();
function headers(sortId: string, desc: boolean) {
  // 排序状态现在由页面注入（选币榜 / 选币榜Y 各持一份仓库），组件不再自己读单例。
  // 仍然先写进 store 再读回来，保证测的是「页面会传什么」而不是硬编码的值。
  useScreenerUi.setState({ sortId: sortId as never, sortDesc: desc });
  const ui = useScreenerUi.getState();
  const html = renderToStaticMarkup(
    <MemoryRouter>
      <CandidateDataGrid
        rows={[row('AAAUSDT', 'A', 'D', null)]}
        preset="compact"
        // X v1.3.0 来源必填；本夹具保留 main v1.4.0，复盘与调参边界不变。
        originTag="screener"
        direction="up"
        sortId={ui.sortId}
        sortDesc={ui.sortDesc}
        onSort={ui.setSort}
      />
    </MemoryRouter>,
  );
  return [...html.matchAll(HEAD)].map((m) => ({ cls: m[1], label: clean(m[2]) }));
}
for (const tf of TFS) {
  const hs = headers(MCAP_SORT_ID[tf], true);
  const mine = hs.find((h) => h.label.startsWith(LABEL[tf]))!;
  const others = TFS.filter((x) => x !== tf).map((x) => hs.find((h) => h.label.startsWith(LABEL[x]))!);
  check(LABEL[tf] + ' 表头 sortable', mine.cls.includes('sortable'), JSON.stringify(mine.cls));
  check(LABEL[tf] + ' 选中时表头高亮 (class sorted)', mine.cls.includes('sorted'), JSON.stringify(mine.cls));
  check(LABEL[tf] + ' 选中时降序箭头 ↓', mine.label.endsWith('↓'), JSON.stringify(mine.label));
  check(LABEL[tf] + ' 选中时其余两列不高亮', others.every((o) => !o.cls.includes('sorted')));
}
const asc6h = headers(MCAP_SORT_ID['6h'], false).find((h) => h.label.startsWith(LABEL['6h']))!;
check('升序表头显示 ↑', asc6h.label.endsWith('↑'), JSON.stringify(asc6h.label));
const unsorted = headers('score', true);
check('未选中时三列均 sortable 且无 sorted',
  TFS.every((tf) => {
    const h = unsorted.find((x) => x.label.startsWith(LABEL[tf]))!;
    return h.cls.includes('sortable') && !h.cls.includes('sorted');
  }));
check('三列在 trader/full_s37 预设下同样可排序',
  (['trader', 'full_s37'] as const).every((preset) => {
    useScreenerUi.setState({ sortId: 'mcap_2h' as never, sortDesc: true });
    const ui = useScreenerUi.getState();
    const html = renderToStaticMarkup(
      <MemoryRouter>
        <CandidateDataGrid
          rows={[row('AAAUSDT', 'A', 'D', null)]}
          preset={preset}
          originTag="screener"
          direction="up"
          sortId={ui.sortId}
          sortDesc={ui.sortDesc}
          onSort={ui.setSort}
        />
      </MemoryRouter>,
    );
    const hs = [...html.matchAll(HEAD)].map((m) => ({ cls: m[1], label: clean(m[2]) }));
    return TFS.every((tf) => hs.find((h) => h.label.startsWith(LABEL[tf]))!.cls.includes('sortable'))
      && hs.find((h) => h.label.startsWith(LABEL['2h']))!.label.endsWith('↓');
  }));

console.log('\n=== 7. 点击表头的 store 行为 ===');
useScreenerUi.setState({ sortId: 'score' as never, sortDesc: true });
useScreenerUi.getState().setSort('mcap_6h');
check('首击 → sortId=mcap_6h, 降序',
  useScreenerUi.getState().sortId === 'mcap_6h' && useScreenerUi.getState().sortDesc === true);
useScreenerUi.getState().setSort('mcap_6h');
check('再击 → 同列切升序', useScreenerUi.getState().sortDesc === false);
useScreenerUi.getState().setSort('mcap_30m');
check('换列 → 重置为降序',
  useScreenerUi.getState().sortId === 'mcap_30m' && useScreenerUi.getState().sortDesc === true);
useScreenerUi.getState().resetFilters();
check('恢复默认 → 回到 score 降序',
  useScreenerUi.getState().sortId === 'score' && useScreenerUi.getState().sortDesc === true);

console.log('\n=== 8. 生产快照实测（data/coin-selection/latest.json）===');
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
  for (const tf of TFS) {
    const rows = pool as CandidateRow[];
    const sorted = sortRows(rows, 'up', MCAP_SORT_ID[tf], true, {});
    const ranks = sorted.map((r) => mcapGradeRank(mcapGradeOf(r, tf)));
    const monotone = ranks.every((v, i) => i === 0 || ranks[i - 1] >= v);
    const asc = sortRows(rows, 'up', MCAP_SORT_ID[tf], false, {});
    const ascOk = asc.map((r) => mcapGradeRank(mcapGradeOf(r, tf)))
      .every((v, i, a) => i === 0 || a[i - 1] <= v);
    const counts: Record<string, number> = {};
    for (const r of sorted) counts[g(r, tf)] = (counts[g(r, tf)] ?? 0) + 1;
    check(poolName + ' ' + tf + ' 降序单调 (' + sorted.length + ' 行)', monotone,
      JSON.stringify(counts) + ' 首' + g(sorted[0], tf) + ' 末' + g(sorted[sorted.length - 1], tf));
    check(poolName + ' ' + tf + ' 升序单调', ascOk);
    check(poolName + ' ' + tf + ' 排序不增删不重复行',
      sorted.length === rows.length
      && new Set(sorted.map((r) => r.symbol)).size === new Set(rows.map((r) => r.symbol)).size);
    // 同级内回落到后端 rank（Score 降序）
    let tieOk = true;
    for (let i = 1; i < sorted.length; i += 1) {
      if (ranks[i] === ranks[i - 1] && sorted[i].rank < sorted[i - 1].rank) tieOk = false;
    }
    check(poolName + ' ' + tf + ' 同级内保持后端 rank 次序', tieOk);
  }
}

console.log(failures === 0 ? '\nALL PASS (' + '0 failures)' : '\n' + failures + ' FAILURES');
process.exit(failures === 0 ? 0 : 1);
