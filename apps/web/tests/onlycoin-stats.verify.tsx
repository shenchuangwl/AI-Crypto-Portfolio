/**
 * 《OnlyCoin · 来源候选回放》DMR 区历史统计 —— 前端验证闸。
 *
 * 覆盖需求 §6 的前端可判定部分：未选日期时不渲染、明细列与缺失值展示、
 * 多空方向与「盈亏数值 = 方向感知符号」的既有口径、自定义起止/周期对比的
 * 请求序列化，以及功能开关。交互时序（切换/取消日期）另由浏览器闸覆盖。
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { renderToStaticMarkup } from 'react-dom/server';
import { MemoryRouter } from 'react-router-dom';
import { DetailTable, MAX_ONLYCOIN_COMPARE, onlyCoinStatsEnabled } from '../src/features/onlycoin/OnlyCoinStatsPanel';
import { OnlyCoinReview } from '../src/features/onlycoin/OnlyCoinReview';
import { fetchOnlyCoinStats, ONLYCOIN_STATS_TIMEOUT_MS } from '../src/shared/api/onlycoin';
import type { OnlyCoinStatRow } from '../src/shared/types/onlycoinStats';

const base: OnlyCoinStatRow = {
  coin_id: 'GRIFFAIN', symbol: 'GRIFFAINUSDT', canonical_asset_id: 'griffain', underlying_asset: 'GRIFFAIN',
  contract_multiplier: 1, pool: 'LONG', direction: 'up', zone: 'DMR', status: 'CLOSED',
  business_date: '2026-09-17', day_complete: true,
  enter_scan_id: '20260917-010', enter_time_utc: '2026-09-17T02:30:00Z', enter_price: 0.014075,
  enter_price_source: 'last_price', exit_scan_id: '20260917-095', exit_time_utc: '2026-09-17T23:45:00Z',
  exit_price: 0.013955, exit_price_source: 'last_price', dwell_minutes: 1275, dwell_nodes: 85,
  pnl_pct: -0.008525754884547099, pnl_sign: -1, first_available_at: '2026-09-17T02:32:26Z',
  availability_quality: 'COMMITTED', directions: ['LONG'], repeat_entries: 1,
  parameter_version: 'param-v2.0.0-screener-y', flags: [], reason_codes: [], mcap_combo: 'AAA', liquidity_grade: 'A',
};
const short: OnlyCoinStatRow = {
  ...base, coin_id: 'FF', symbol: 'FFUSDT', canonical_asset_id: 'ff', underlying_asset: 'FF',
  pool: 'SHORT', direction: 'down', enter_scan_id: '20260917-063', enter_time_utc: '2026-09-17T15:45:00Z',
  enter_price: 0.12617, exit_price: 0.12672, pnl_pct: -0.0043591979075849885, pnl_sign: -1, repeat_entries: 0,
};
const openRow: OnlyCoinStatRow = {
  ...base, coin_id: 'TUT', symbol: 'TUTUSDT', canonical_asset_id: 'tut', underlying_asset: 'TUT',
  business_date: '2026-09-18', day_complete: false, status: 'OPEN',
  exit_scan_id: null, exit_time_utc: null, exit_price: null, exit_price_source: null,
  dwell_minutes: null, dwell_nodes: null, pnl_pct: null, pnl_sign: null, flags: ['INCOMPLETE_DAY'], repeat_entries: 0,
};
const noPrice: OnlyCoinStatRow = {
  ...base, coin_id: 'VOID', symbol: 'VOIDUSDT', canonical_asset_id: 'void', underlying_asset: 'VOID',
  exit_price: null, exit_price_source: null, pnl_pct: null, pnl_sign: null,
  flags: ['MISSING_EXIT_PRICE'], repeat_entries: 0,
};

const render = (node: React.ReactElement) => renderToStaticMarkup(<MemoryRouter>{node}</MemoryRouter>);

// —— 明细：必须的列都在，且顺序与需求 §4.5 一致 ——
const html = render(<DetailTable rows={[base, short, openRow, noPrice]} />);
const headers = [...html.matchAll(/<th[^>]*>([^<]*)<\/th>/g)].map((m) => m[1]);
assert.deepEqual(headers, [
  '#', '唯一币种', '合约', '候选池 / 方向', '入选时间 (+07)', '停留价格',
  '退出时间 (+07)', '退出价格', '停留时间', '盈亏百分比', '盈亏数值', '数据完整性',
]);
assert.equal((html.match(/<tr /g) || []).length + (html.match(/<tr>/g) || []).length, 5, '表头 1 行 + 4 个唯一币种');
console.log('PASS 明细列齐全且每个唯一币种只有一行');

// 首次入选时间按 +07 展示（02:30 UTC = 09:30 +07），与上方名单同一时钟。
assert.match(html, /09:30/);
assert.match(html, /09-17 23:45|23:45/, '退出时间是该业务日收线节点');
// 停留价格 / 退出价格用项目既有的价格格式化，不四舍五入成 0。
assert.match(html, /0\.014075/);
assert.match(html, /0\.013955/);
console.log('PASS 入选/退出时间与价格按既有口径展示');

// —— 多空方向：上涨池=做多、下跌池=做空，class 分别为 up / down ——
assert.match(html, /class="ret up">上涨候选池 · 做多/);
assert.match(html, /class="ret down">下跌候选池 · 做空/);
console.log('PASS 候选池与多空方向一一对应');

// —— 「盈亏数值」沿用项目既有口径 = 方向感知符号，不是凭空造的金额 ——
const signCells = [...html.matchAll(/不含仓位、本金、杠杆与手续费"><span class="ret (up|down)">(-?\d)<\/span>/g)].map((m) => m[2]);
assert.deepEqual(signCells, ['-1', '-1'], '只有可算盈亏的行有符号；缺价与未完成日必须是 —');
console.log('PASS 盈亏数值 = 1/0/−1 方向符号，与选币榜/原复盘同列口径');

// —— 缺失值：绝不用 0 顶替 ——
assert.doesNotMatch(html, />0<\/span>/, '缺价不得展示为 0');
assert.match(html, /交易日未结束/);
assert.match(html, /缺收线行情/);
assert.equal((html.match(/—/g) || []).length >= 6, true, '未完成日与缺价行的时间/价格/盈亏都是 —');
assert.match(html, /class="[^"]*review-open/, '未完成交易日的行降权展示');
console.log('PASS 未完成交易日与缺行情标注为数据不足，不伪装成完整统计');

// —— 空数据 ——
assert.match(render(<DetailTable rows={[]} />), /该统计区间内 0 个唯一币种/);
console.log('PASS 空区间给出明确空态');

// —— 重复入选只计一次，但次数可见 ——
assert.match(html, /×2/, '同一唯一币种的重复入选次数可见，但只有一行');

// —— 未选择回放日期时，统计区整块不存在 ——
const review = render(<OnlyCoinReview />);
assert.doesNotMatch(review, /onlycoin-stats-panel/, '未触发日期回放时不得渲染统计区');
assert.doesNotMatch(review, /DMR区 · 历史数据统计/);
const source = readFileSync('src/features/onlycoin/OnlyCoinReview.tsx', 'utf8');
assert.match(source, /replayed && <OnlyCoinStatsPanel/, '统计区只挂在“已完成回放”的状态上');
assert.match(source, /key=\{`\$\{replayed\.date\}\|\$\{replayed\.asOf\}`\}/, '换回放身份必须重挂载，杜绝旧数据残留');
assert.match(source, /setReplayed\(null\)/, '清空/重查时先撤销已回放身份');
assert.equal((source.match(/setReplayed\(null\)/g) || []).length, 2, 'clear() 与 load() 起点都要撤销');
assert.match(source, /setReplayed\(\{ date, asOf: utc \}\)/, '只有查询成功后才算“已选择日期”');
console.log('PASS 未选择/未触发回放时统计区隐藏；换日期强制重挂载');

// —— 请求序列化：自定义起止成对、周期对比可重复、超时更宽 ——
const calls: string[] = [];
globalThis.fetch = (async (url: string | URL | Request) => {
  calls.push(String(url));
  return new Response(JSON.stringify({ schema: 'onlycoin-stats-v1', board_key: 'y' }), { status: 200 });
}) as typeof fetch;
await fetchOnlyCoinStats('2026-09-17', '2026-09-17T23:59:59Z');
await fetchOnlyCoinStats('2026-09-17', '2026-09-17T23:59:59Z', { from: '2026-09-15T00:00:00Z' });
await fetchOnlyCoinStats('2026-09-17', '2026-09-17T23:59:59Z', {
  from: '2026-09-15T00:00:00Z', to: '2026-09-18T00:00:00Z',
  compare: [
    { from: '2026-09-15T00:00:00Z', to: '2026-09-16T00:00:00Z' },
    { from: '2026-09-16T00:00:00Z', to: '2026-09-17T00:00:00Z' },
    { from: '', to: '2026-09-17T00:00:00Z' },
  ],
});
const q0 = new URL(calls[0], 'https://test').searchParams;
assert.equal(q0.get('board'), 'y');
assert.equal(q0.get('business_date'), '2026-09-17');
assert.equal(q0.get('as_of'), '2026-09-17T23:59:59Z');
assert.equal(q0.get('from'), null, '默认按回放业务日，不带自定义区间');
const q1 = new URL(calls[1], 'https://test').searchParams;
assert.equal(q1.get('from'), null, '半填的自定义起止一律不提交');
assert.equal(q1.get('to'), null);
const q2 = new URL(calls[2], 'https://test').searchParams;
assert.equal(q2.get('from'), '2026-09-15T00:00:00Z');
assert.equal(q2.get('to'), '2026-09-18T00:00:00Z');
assert.deepEqual(q2.getAll('compare'), [
  '2026-09-15T00:00:00Z~2026-09-16T00:00:00Z',
  '2026-09-16T00:00:00Z~2026-09-17T00:00:00Z',
], '半填的对比周期被丢弃，不发半个区间');
assert.ok(ONLYCOIN_STATS_TIMEOUT_MS > 12000, '统计要读板面快照，超时必须比名单查询宽');
assert.equal(MAX_ONLYCOIN_COMPARE, 3, '与原复盘 MAX_COMPARE_PERIODS 对齐');
console.log('PASS 自定义起止与周期对比的请求序列化');

// —— 失败必须抛，不得静默把上一段数字留在屏幕上 ——
globalThis.fetch = (async () => new Response(JSON.stringify({ error: 'ONLYCOIN_STATS_DISABLED' }), { status: 404 })) as typeof fetch;
await assert.rejects(() => fetchOnlyCoinStats('2026-09-17', '2026-09-17T23:59:59Z'), /HTTP 404/);
console.log('PASS 统计取数失败会抛出（前端据此隐藏或报错）');

// —— 功能开关：前端侧 ——
assert.equal(onlyCoinStatsEnabled(), true, '默认启用');
for (const off of ['off', 'OFF', '0', 'false', 'no']) {
  (import.meta.env as Record<string, unknown>).VITE_ONLYCOIN_STATS = off;
  assert.equal(onlyCoinStatsEnabled(), false, `VITE_ONLYCOIN_STATS=${off} 必须整块停用`);
}
(import.meta.env as Record<string, unknown>).VITE_ONLYCOIN_STATS = undefined;
assert.equal(onlyCoinStatsEnabled(), true);
console.log('PASS 前端停用开关');

// —— 复用而非复制：统计卡与周期对比表用的就是原复盘那两个组件 ——
const panel = readFileSync('src/features/onlycoin/OnlyCoinStatsPanel.tsx', 'utf8');
assert.match(panel, /from '\.\.\/review\/ReviewSummaryCards'/);
assert.match(panel, /from '\.\.\/review\/PeriodCompareTable'/);
assert.match(panel, /from '\.\.\/\.\.\/shared\/lib\/reviewTime'/, '起止时间换算沿用原复盘同一对函数');
// 原复盘账本页面不得被改动口径。
const reviewPage = readFileSync('src/pages/ReviewPage.tsx', 'utf8');
assert.doesNotMatch(reviewPage, /OnlyCoinStatsPanel/, '原复盘·进出账本不引入新统计，保持原样');
console.log('PASS 复用原 DMR 统计组件，原复盘账本未被改动');

console.log('PASS OnlyCoin 回放历史统计（全部）');
