/**
 * 选币榜「上涨候选池 / 下跌候选池」币种计数的验证闸。
 *
 *   cd apps/web && npm run verify:pool-counts
 *
 * 覆盖：唯一币种 ID 口径、七个分区单选计数、多选并集去重（DMR ⊂ 确认 是最容易
 * 重复累加的组合）、同币多合约只计 1、计数与表格实际渲染行同源、计数不随报价抖动、
 * 分区全关时归零，以及生产快照 1050 行的实测。
 */
import { renderToStaticMarkup } from 'react-dom/server';
import { readFileSync } from 'node:fs';
import { DirectionTabs } from '../src/features/screener/components/DirectionTabs';
import {
  coinIdOf,
  filterPools,
  filterRows,
  inSelectedZones,
  uniqueCoinCount,
  type PoolFilter,
} from '../src/shared/lib/poolFilter';
import type { CandidateRow, ScreenerState } from '../src/shared/types/screener';

let failures = 0;
function check(name: string, ok: boolean, extra = '') {
  console.log((ok ? '  ok   ' : '  FAIL ') + name + (extra ? '  ' + extra : ''));
  if (!ok) failures += 1;
}

/** 「选币榜」的七个分区。DMR 不是状态，而是叠在确认之上的可执行子集标记。 */
const ZONES: { name: string; state: ScreenerState | null; dmr: boolean }[] = [
  { name: 'DMR区', state: null, dmr: true },
  { name: '确认区', state: 'CONFIRMED', dmr: false },
  { name: '符合区', state: 'QUALIFIED', dmr: false },
  { name: '观察区', state: 'WATCH', dmr: false },
  { name: '淘汰区', state: 'ELIMINATED', dmr: false },
  { name: '数据不足区', state: 'DATA_INSUFFICIENT', dmr: false },
  { name: '低置信度区', state: 'LOW_CONFIDENCE', dmr: false },
];

/** 由若干分区拼出一次筛选。 */
function pick(...zones: string[]): PoolFilter {
  const chosen = ZONES.filter((z) => zones.includes(z.name));
  return {
    activeStates: chosen.map((z) => z.state).filter(Boolean) as ScreenerState[],
    dmrActive: chosen.some((z) => z.dmr),
    grades: [],
    search: '',
  };
}

function row(
  symbol: string,
  state: ScreenerState,
  opts: { dmr?: boolean; coin?: string; direction?: 'up' | 'down'; grade?: 'A' | 'B' } = {},
): CandidateRow {
  return {
    rank: 1, symbol, underlying_asset: symbol.replace('USDT', ''),
    canonical_asset_id: (opts.coin ?? symbol.replace('USDT', '')).toLowerCase(),
    contract_multiplier: 1, direction: opts.direction ?? 'up', state,
    state_enter_time_utc: '2026-08-23T12:00:00Z', state_duration_minutes: 15,
    state_enter_price: 1, score_up: 50, score_down: 50,
    direction_confidence: 1, liquidity_score: 80, liquidity_grade: opts.grade ?? 'A',
    mcap_grade_30m: 'A', mcap_grade_2h: 'C', mcap_grade_6h: null, mcap_tf: {},
    momentum_score: 70, mcap_momentum_score: 60, staircase_score: 55,
    consistency_score: 0.7, rank_velocity_score: 50, risk_score: 100,
    data_confidence: 90, ret_15m: null, ret_1h: 0, ret_4h: 0, ret_24h: 0,
    ret_1w: 0, ret_1mo: 0, ret_since_anchor: 0,
    aqv_6d_m: 10, aqv_12d_m: 9, aqv_26d_m: 8,
    circulating_supply: 1e9, market_cap_coingecko: 1e9, market_cap_calculated: 1e9,
    supply_source: 'CG', data_mode: 'LIVE', supply_as_of_utc: null,
    mapping_confidence: 1, risk_flags: [], reason_codes: [], not_confirmed_reasons: [],
    dmr_selected: opts.dmr,
  };
}

console.log('\n=== 1. 唯一币种 ID 口径 ===');
check('取 canonical_asset_id', coinIdOf(row('BTCUSDT', 'WATCH', { coin: 'btc' })) === 'BTC');
check('大小写不敏感',
  coinIdOf(row('XUSDT', 'WATCH', { coin: 'btc' })) === coinIdOf(row('YUSDT', 'WATCH', { coin: 'BTC' })));
check('canonical 缺失回退 underlying_asset',
  coinIdOf({ ...row('SOLUSDT', 'WATCH'), canonical_asset_id: '' }) === 'SOL');
check('两者都缺回退 symbol',
  coinIdOf({ ...row('SOLUSDT', 'WATCH'), canonical_asset_id: '', underlying_asset: '' }) === 'SOLUSDT');
check('不同币不会被并成一个',
  coinIdOf(row('BTCUSDT', 'WATCH')) !== coinIdOf(row('ETHUSDT', 'WATCH')));

console.log('\n=== 2. 分区判据：状态 chip ∪ DMR ===');
{
  const confirmed = row('AUSDT', 'CONFIRMED');
  const dmrRow = row('BUSDT', 'CONFIRMED', { dmr: true });
  const watch = row('CUSDT', 'WATCH');
  check('只选 DMR 时，非 DMR 的确认行不入选',
    !inSelectedZones(confirmed, [], true) && inSelectedZones(dmrRow, [], true));
  check('只选确认时，DMR 行也在（DMR ⊂ 确认）',
    inSelectedZones(confirmed, ['CONFIRMED'], false) && inSelectedZones(dmrRow, ['CONFIRMED'], false));
  check('分区全关 → 一行都不入选',
    !inSelectedZones(confirmed, [], false) && !inSelectedZones(dmrRow, [], false)
    && !inSelectedZones(watch, [], false));
}

console.log('\n=== 3. 单选：每个分区各报各的数 ===');
// 每区造不同数量的行，双池数量刻意不同，能抓出"两个池报同一个数"的老 bug
const LONG_BY_ZONE: Record<string, number> = {
  DMR区: 2, 确认区: 5, 符合区: 7, 观察区: 11, 淘汰区: 13, 数据不足区: 3, 低置信度区: 1,
};
const SHORT_BY_ZONE: Record<string, number> = {
  DMR区: 1, 确认区: 4, 符合区: 6, 观察区: 9, 淘汰区: 17, 数据不足区: 2, 低置信度区: 8,
};
function buildPool(by: Record<string, number>, direction: 'up' | 'down'): CandidateRow[] {
  const out: CandidateRow[] = [];
  // DMR 行同时是确认行 —— 与真实数据一致
  for (let i = 0; i < by.DMR区; i += 1)
    out.push(row(`${direction}DMR${i}USDT`, 'CONFIRMED', { dmr: true, direction }));
  for (const z of ZONES.filter((x) => x.state)) {
    for (let i = 0; i < by[z.name]; i += 1)
      out.push(row(`${direction}${z.name}${i}USDT`, z.state as ScreenerState, { direction }));
  }
  return out;
}
const LONG = buildPool(LONG_BY_ZONE, 'up');
const SHORT = buildPool(SHORT_BY_ZONE, 'down');

for (const z of ZONES) {
  const p = filterPools(LONG, SHORT, pick(z.name));
  const wantLong = z.name === '确认区' ? LONG_BY_ZONE.确认区 + LONG_BY_ZONE.DMR区 : LONG_BY_ZONE[z.name];
  const wantShort = z.name === '确认区' ? SHORT_BY_ZONE.确认区 + SHORT_BY_ZONE.DMR区 : SHORT_BY_ZONE[z.name];
  check(`${z.name} 单选 → 上涨 ${wantLong} / 下跌 ${wantShort}`,
    p.longCoins === wantLong && p.shortCoins === wantShort,
    `实得 ${p.longCoins}/${p.shortCoins}`);
}
check('两个池报的不是同一个数（旧 bug：都显示 525）',
  ZONES.some((z) => {
    const p = filterPools(LONG, SHORT, pick(z.name));
    return p.longCoins !== p.shortCoins;
  }));
check('没有任何分区固定报满池',
  ZONES.every((z) => {
    const p = filterPools(LONG, SHORT, pick(z.name));
    return p.longCoins < LONG.length && p.shortCoins < SHORT.length;
  }));

console.log('\n=== 4. 多选去重：DMR ⊂ 确认，严禁重复累加 ===');
{
  const dmrOnly = filterPools(LONG, SHORT, pick('DMR区'));
  const confOnly = filterPools(LONG, SHORT, pick('确认区'));
  const union = filterPools(LONG, SHORT, pick('DMR区', '确认区'));
  check('上涨：DMR+确认 = 确认，不是 DMR+确认 相加',
    union.longCoins === confOnly.longCoins
    && union.longCoins !== dmrOnly.longCoins + confOnly.longCoins,
    `并集 ${union.longCoins} vs 朴素相加 ${dmrOnly.longCoins + confOnly.longCoins}`);
  check('下跌：同上',
    union.shortCoins === confOnly.shortCoins
    && union.shortCoins !== dmrOnly.shortCoins + confOnly.shortCoins,
    `并集 ${union.shortCoins} vs 朴素相加 ${dmrOnly.shortCoins + confOnly.shortCoins}`);
}
{
  // 无交集的两区：并集恰好等于相加，说明去重没有"多减"
  const a = filterPools(LONG, SHORT, pick('观察区'));
  const b = filterPools(LONG, SHORT, pick('淘汰区'));
  const ab = filterPools(LONG, SHORT, pick('观察区', '淘汰区'));
  check('无交集的两区：并集 = 相加（去重不多减）',
    ab.longCoins === a.longCoins + b.longCoins && ab.shortCoins === a.shortCoins + b.shortCoins,
    `${ab.longCoins} = ${a.longCoins}+${b.longCoins}`);
}
check('并集单调且不超过各区之和（任取两区）',
  ZONES.every((x) => ZONES.every((y) => {
    const ux = filterPools(LONG, SHORT, pick(x.name));
    const uy = filterPools(LONG, SHORT, pick(y.name));
    const u = filterPools(LONG, SHORT, pick(x.name, y.name));
    return u.longCoins >= Math.max(ux.longCoins, uy.longCoins)
      && u.longCoins <= ux.longCoins + uy.longCoins
      && u.shortCoins >= Math.max(ux.shortCoins, uy.shortCoins)
      && u.shortCoins <= ux.shortCoins + uy.shortCoins;
  })));
check('全选七个分区 = 整池（每行都属于某个分区）',
  (() => {
    const all = filterPools(LONG, SHORT, pick(...ZONES.map((z) => z.name)));
    return all.longCoins === LONG.length && all.shortCoins === SHORT.length;
  })());
check('分区全关 → 两个池都归零',
  (() => {
    const none = filterPools(LONG, SHORT, { activeStates: [], dmrActive: false, grades: [], search: '' });
    return none.longCoins === 0 && none.shortCoins === 0 && none.long.length === 0;
  })());

console.log('\n=== 5. 计的是币种，不是条目：同币多合约只算 1 ===');
{
  const dual = [
    row('BOMEUSDT', 'CONFIRMED', { coin: 'bome' }),
    row('1000BOMEUSDT', 'CONFIRMED', { coin: 'bome' }),   // 同一个币的第二个合约
    row('BTCUSDT', 'CONFIRMED', { coin: 'btc' }),
  ];
  const p = filterPools(dual, [], pick('确认区'));
  check('3 条目 / 2 币种 → 计 2', p.longCoins === 2 && p.long.length === 3,
    `条目 ${p.long.length} 币种 ${p.longCoins}`);
  check('uniqueCoinCount 与 Set 语义一致',
    uniqueCoinCount(dual) === new Set(dual.map(coinIdOf)).size);
  check('空数组 → 0', uniqueCoinCount([]) === 0);
}

console.log('\n=== 6. 计数与表格渲染同源 ===');
for (const combo of [['DMR区'], ['确认区'], ['DMR区', '确认区'], ['确认区', '符合区', '观察区'],
  ['DMR区', '确认区', '符合区'], ZONES.map((z) => z.name)]) {
  const f = pick(...combo);
  const p = filterPools(LONG, SHORT, f);
  const same = p.long.length === filterRows(LONG, f).length
    && p.short.length === filterRows(SHORT, f).length
    && p.longCoins <= p.long.length && p.shortCoins <= p.short.length;
  check(`${combo.join('+')} 计数与过滤出的行同源`, same,
    `行 ${p.long.length}/${p.short.length} 币种 ${p.longCoins}/${p.shortCoins}`);
}

console.log('\n=== 7. 等级 / 搜索也计入（标签数字必须预告切过去看到什么）===');
{
  const mixed = [
    row('AAAUSDT', 'CONFIRMED', { grade: 'A' }),
    row('BBBUSDT', 'CONFIRMED', { grade: 'B' }),
    row('AABUSDT', 'CONFIRMED', { grade: 'A' }),
  ];
  const byGrade = filterPools(mixed, [], { ...pick('确认区'), grades: ['A'] });
  check('等级筛选生效', byGrade.longCoins === 2, String(byGrade.longCoins));
  const bySearch = filterPools(mixed, [], { ...pick('确认区'), search: 'aa' });
  check('搜索大小写不敏感且生效', bySearch.longCoins === 2, String(bySearch.longCoins));
  const both = filterPools(mixed, [], { ...pick('确认区'), grades: ['B'], search: 'AAA' });
  check('等级与搜索是「与」关系', both.longCoins === 0, String(both.longCoins));
}

console.log('\n=== 8. 计数不随报价抖动（纯函数，签名里没有 quotes）===');
{
  const f = pick('DMR区', '确认区');
  const a = filterPools(LONG, SHORT, f);
  const b = filterPools(LONG, SHORT, f);
  check('同样输入两次调用结果一致',
    a.longCoins === b.longCoins && a.shortCoins === b.shortCoins);
  check('filterPools 只吃 (long, short, filter) 三个参数', filterPools.length === 3);
}

console.log('\n=== 9. 标签页渲染出的就是传进去的数 ===');
{
  const html = renderToStaticMarkup(
    <DirectionTabs value="up" longCount={90} shortCount={46} zoneSummary="DMR+确认+符合" onChange={() => {}} />,
  );
  const nums = [...html.matchAll(/<span>(\d+)<\/span>/g)].map((m) => m[1]);
  check('上涨 90 / 下跌 46 各就各位', JSON.stringify(nums) === JSON.stringify(['90', '46']), nums.join(','));
  check('tooltip 说明去重口径', html.includes('并集去重后') && html.includes('唯一币种数'));
  check('选中态只落在当前方向',
    html.includes('dir-tab up on') && !html.includes('dir-tab down on'));
  const zero = renderToStaticMarkup(
    <DirectionTabs value="down" longCount={0} shortCount={0} onChange={() => {}} />,
  );
  check('0 如实显示 0（不回退成满池）',
    JSON.stringify([...zero.matchAll(/<span>(\d+)<\/span>/g)].map((m) => m[1])) === JSON.stringify(['0', '0']));
}

console.log('\n=== 10. 生产快照实测（data/coin-selection/latest.json）===');
const SNAPSHOT = process.env.HERMES_SNAPSHOT ?? '../../data/coin-selection/latest.json';
let snap: { long_pool: CandidateRow[]; short_pool: CandidateRow[] } | null = null;
try {
  snap = JSON.parse(readFileSync(SNAPSHOT, 'utf8'));
} catch {
  console.log('  skip  没有生产快照 ' + SNAPSHOT + '（设 HERMES_SNAPSHOT 指定路径）');
}
if (snap) {
  const L = snap.long_pool, S = snap.short_pool;
  console.log(`  快照：上涨池 ${L.length} 行 / 下跌池 ${S.length} 行`);
  const single: Record<string, { l: number; s: number }> = {};
  for (const z of ZONES) {
    const p = filterPools(L, S, pick(z.name));
    single[z.name] = { l: p.longCoins, s: p.shortCoins };
    const rowsL = z.dmr ? L.filter((r) => r.dmr_selected) : L.filter((r) => r.state === z.state);
    const rowsS = z.dmr ? S.filter((r) => r.dmr_selected) : S.filter((r) => r.state === z.state);
    check(`${z.name} 单选 上涨 ${p.longCoins} / 下跌 ${p.shortCoins}`,
      p.longCoins === new Set(rowsL.map(coinIdOf)).size
      && p.shortCoins === new Set(rowsS.map(coinIdOf)).size);
  }
  const conf = single['确认区'], dmr = single['DMR区'];
  const u = filterPools(L, S, pick('DMR区', '确认区'));
  check(`DMR+确认 去重：上涨 = 确认 ${conf.l}（DMR ⊂ 确认，朴素相加会是 ${dmr.l + conf.l}）`,
    u.longCoins === conf.l, `实得 ${u.longCoins}`);
  check(`DMR+确认 去重：下跌 = 确认 ${conf.s}（朴素相加会是 ${dmr.s + conf.s}）`,
    u.shortCoins === conf.s, `实得 ${u.shortCoins}`);
  const three = filterPools(L, S, pick('DMR区', '确认区', '符合区'));
  check(`DMR+确认+符合：上涨 ${three.longCoins} / 下跌 ${three.shortCoins}（均 < 525）`,
    three.longCoins < L.length && three.shortCoins < S.length
    && three.longCoins > 0 && three.shortCoins > 0);
  const all = filterPools(L, S, pick(...ZONES.map((z) => z.name)));
  check(`全选七区 = 整池去重后 ${all.longCoins}/${all.shortCoins}`,
    all.longCoins === new Set(L.map(coinIdOf)).size
    && all.shortCoins === new Set(S.map(coinIdOf)).size);
  // 任意两区组合都不得超过各自之和
  let subadditive = true;
  for (const x of ZONES) for (const y of ZONES) {
    if (x === y) continue;
    const uu = filterPools(L, S, pick(x.name, y.name));
    if (uu.longCoins > single[x.name].l + single[y.name].l) subadditive = false;
    if (uu.shortCoins > single[x.name].s + single[y.name].s) subadditive = false;
  }
  check('任意两区组合的并集都 ≤ 两区之和（无重复累加）', subadditive);
}

console.log(failures === 0 ? '\nALL PASS (' + '0 failures)' : '\n' + failures + ' FAILURES');
process.exit(failures === 0 ? 0 : 1);
