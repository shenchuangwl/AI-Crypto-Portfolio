/**
 * 216 组合天花板层的前端验证闸（文档B §3.3 / §9.1 测试 T13）。
 *
 *   cd apps/web && npm run verify:mcap-zone
 *
 * 要守住的只有一句话：**天花板的结果由服务端下发，前端只渲染，绝不重算。**
 * 文档A 红线第 19 条 —— 任何在前端用 `mcap_grade_*` 查 216 表算分区的实现都违规。
 *
 * 覆盖：
 *   1. `state=CONFIRMED, zone_ceiling=WATCH` 的行渲染进**观察区**，不是确认区。
 *   2. `product_zone` 存在时以它为唯一权威（它已经是服务端算好的 min(state, ceiling)）。
 *   3. 天花板比 state **宽**时不得提级（只降不升）。
 *   4. 两个字段都缺（mcap_zone_mode=off）时逐字段回退 `state` —— 现网行为不变。
 *   5. grep 断言：前端源码里不存在 A–F 到分区的查表常量。
 *   6. 计数与过滤共用同一个 `displayZone`，不可能出现「芯片数与表格行数对不上」。
 */
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';
import {
  displayZone,
  filterRows,
  inSelectedZones,
  uniqueCoinCount,
} from '../src/shared/lib/poolFilter';
import type { CandidateRow, ScreenerState } from '../src/shared/types/screener';

let failures = 0;
function check(name: string, ok: boolean, extra = '') {
  console.log((ok ? '  ok   ' : '  FAIL ') + name + (extra ? '  ' + extra : ''));
  if (!ok) failures += 1;
}

/** 只填断言用得到的字段；其余按 CandidateRow 的形状补齐最小值。 */
function row(over: Partial<CandidateRow> & { symbol: string; state: ScreenerState }): CandidateRow {
  return {
    rank: 1,
    underlying_asset: over.symbol.replace('USDT', ''),
    canonical_asset_id: over.symbol.replace('USDT', '').toLowerCase(),
    contract_multiplier: 1,
    direction: 'up',
    state_enter_time_utc: '2026-08-31T00:00:00Z',
    state_duration_minutes: 30,
    score_up: 70,
    score_down: 30,
    direction_confidence: 1,
    liquidity_score: 90,
    liquidity_grade: 'A',
    momentum_score: 70,
    mcap_momentum_score: 60,
    staircase_score: 60,
    consistency_score: 1,
    rank_velocity_score: 50,
    risk_score: 100,
    data_confidence: 90,
    ret_15m: 0,
    ret_1h: 0,
    ret_4h: 0,
    ret_24h: 0,
    ret_since_anchor: 0,
    aqv_6d_m: 10,
    aqv_12d_m: 10,
    aqv_26d_m: 10,
    circulating_supply: 1,
    market_cap_coingecko: 1,
    market_cap_calculated: 1,
    supply_source: 'CG',
    data_mode: 'LIVE',
    supply_as_of_utc: null,
    mapping_confidence: 1,
    risk_flags: [],
    reason_codes: [],
    not_confirmed_reasons: [],
    ...over,
  } as CandidateRow;
}

console.log('=== 1. zone_ceiling 严于 state 时以天花板为准 ===');
const capped = row({
  symbol: 'AAAUSDT',
  state: 'CONFIRMED',
  zone_ceiling: 'WATCH',
  combo_code: 'FFF',
  z_score: -3,
});
check('CONFIRMED + 天花板 WATCH → 渲染进观察区', displayZone(capped) === 'WATCH', displayZone(capped));
check('它不出现在确认区', !inSelectedZones(capped, ['CONFIRMED'], false));
check('它出现在观察区', inSelectedZones(capped, ['WATCH'], false));

const cappedQual = row({ symbol: 'BBBUSDT', state: 'CONFIRMED', zone_ceiling: 'QUALIFIED' });
check('CONFIRMED + 天花板 QUALIFIED → 符合区', displayZone(cappedQual) === 'QUALIFIED', displayZone(cappedQual));

console.log('\n=== 2. product_zone 是唯一权威（服务端已算好 min(state, ceiling)） ===');
const both = row({
  symbol: 'CCCUSDT',
  state: 'CONFIRMED',
  zone_ceiling: 'WATCH',
  product_zone: 'QUALIFIED',
});
check('product_zone 存在时压过 zone_ceiling', displayZone(both) === 'QUALIFIED', displayZone(both));

console.log('\n=== 3. 只降不升：天花板更宽时不得提级 ===');
const wide = row({ symbol: 'DDDUSDT', state: 'WATCH', zone_ceiling: 'DMR', combo_code: 'AAA', z_score: 3 });
check('WATCH + 天花板 DMR 仍是观察区', displayZone(wide) === 'WATCH', displayZone(wide));
const wide2 = row({ symbol: 'EEEUSDT', state: 'QUALIFIED', zone_ceiling: 'CONFIRMED' });
check('QUALIFIED + 天花板 CONFIRMED 仍是符合区', displayZone(wide2) === 'QUALIFIED', displayZone(wide2));
const elim = row({ symbol: 'GGGUSDT', state: 'ELIMINATED', zone_ceiling: 'DMR' });
check('ELIMINATED 不因天花板好而复活', displayZone(elim) === 'ELIMINATED', displayZone(elim));

console.log('\n=== 4. 开关关（mcap_zone_mode=off）：逐字段回退 state ===');
const plain = row({ symbol: 'HHHUSDT', state: 'CONFIRMED' });
check('没有 zone_ceiling / product_zone → state', displayZone(plain) === 'CONFIRMED', displayZone(plain));
check('天花板字段为 null 也回退 state',
  displayZone(row({ symbol: 'IIIUSDT', state: 'QUALIFIED', zone_ceiling: null, product_zone: null })) === 'QUALIFIED');

console.log('\n=== 5. 前端源码不含 A–F 分区查表常量（红线第 19 条） ===');
// 验证闸由 `npm run verify:*` 在 apps/web 下启动，cwd 就是 apps/web
// （board-split.verify.tsx 读源码用的也是同一个相对路径约定）。
const SRC = 'src';
function walk(dir: string): string[] {
  const out: string[] = [];
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) out.push(...walk(p));
    else if (/\.(ts|tsx)$/.test(name)) out.push(p);
  }
  return out;
}
const files = walk(SRC);
// 216 表的指纹：三字母组合字面量（'AAA'..'FFF'）与切点常量（2.1 / 1.5 / 0.8 / -0.5）
// 同时出现在一个前端文件里，就说明有人把天花板搬到了前端。
const comboLiteral = /['"][A-F]{3}['"]\s*:/;
const offenders: string[] = [];
for (const f of files) {
  const txt = readFileSync(f, 'utf8');
  if (comboLiteral.test(txt)) offenders.push(f.slice(SRC.length + 1));
}
check('前端无 216 组合 → 分区的查表常量', offenders.length === 0, offenders.slice(0, 3).join(','));
// 只看**代码**，不看注释 —— poolFilter.ts 的文档串里就写着「不得读 mcap_grade_*」，
// 拿原文 grep 会把这句说明本身当成违规。
const stripComments = (t: string) =>
  t.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:])\/\/.*$/gm, '$1');
const poolSrc = stripComments(readFileSync(join(SRC, 'shared/lib/poolFilter.ts'), 'utf8'));
check('displayZone 的代码不读 mcap_grade_*', !/mcap_grade_/.test(poolSrc));

console.log('\n=== 6. 计数与过滤同源 ===');
const rows = [capped, cappedQual, both, wide, plain];
// 观察区 = 天生的 WATCH（DDDUSDT）+ 被天花板压下来的 CONFIRMED（AAAUSDT）
const watchRows = filterRows(rows, { activeStates: ['WATCH'], dmrActive: false, grades: [], search: '' });
check('观察区 = 天生 WATCH + 被压下来的行',
  watchRows.map((r) => r.symbol).sort().join(',') === 'AAAUSDT,DDDUSDT',
  watchRows.map((r) => r.symbol).join(','));
check('观察区唯一币数 == 表格行数', uniqueCoinCount(watchRows) === watchRows.length,
  `${uniqueCoinCount(watchRows)}/${watchRows.length}`);
const confRows = filterRows(rows, { activeStates: ['CONFIRMED'], dmrActive: false, grades: [], search: '' });
check('确认区只剩没被压的那一行', confRows.length === 1 && confRows[0].symbol === 'HHHUSDT',
  confRows.map((r) => r.symbol).join(','));

console.log(failures === 0 ? '\nALL PASS (0 failures)' : `\n${failures} FAILURES`);
if (failures) process.exit(1);
