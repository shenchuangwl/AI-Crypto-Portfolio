import assert from 'node:assert/strict';

import { useReviewUi } from '../src/shared/stores/reviewUi';
import { defaultReviewRange, localInputToUtcIso, utcIsoToLocalInput } from '../src/shared/lib/reviewTime';
import { readReviewTime, REVIEW_TIME_STORAGE_KEY } from '../src/shared/lib/reviewTimePreferences';
import { searchToState } from '../src/features/review/reviewQuery';

useReviewUi.getState().reset();
const state = useReviewUi.getState();
assert.match(utcIsoToLocalInput(state.customFrom), /T07:00$/, '首次开始时间应预填 07:00');
assert.match(utcIsoToLocalInput(state.customTo), /T07:00$/, '首次结束时间应预填 07:00');
assert.equal(state.useCustom, false, '预填不应静默替换原有快捷周期查询');

console.log('PASS: first-load inputs show 07:00 without changing the active query');

useReviewUi.getState().toggleCompare();
for (const p of useReviewUi.getState().comparePeriods) {
  assert.match(utcIsoToLocalInput(p.from), /T07:00$/, '首次对比起点预填 07:00');
  assert.match(utcIsoToLocalInput(p.to), /T07:00$/, '首次对比终点预填 07:00');
}
useReviewUi.getState().addComparePeriod();
assert.equal(useReviewUi.getState().comparePeriods.length, 2);
assert.match(utcIsoToLocalInput(useReviewUi.getState().comparePeriods[1].from), /T07:00$/);
assert.match(utcIsoToLocalInput(useReviewUi.getState().comparePeriods[1].to), /T07:00$/);
console.log('PASS: new comparison periods start at 07:00');

// Only browser storage is substituted; exercise the real store actions.
const memory = new Map<string, string>();
Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: {
  getItem: (key: string) => memory.get(key) ?? null,
  setItem: (key: string, value: string) => memory.set(key, value),
} });
const from = '2026-09-02T02:35:00Z';
const to = '2026-09-03T11:45:00Z';
useReviewUi.getState().setCustom(from, to);
assert.ok(memory.size > 0, '手动修改必须持久化到浏览器');
useReviewUi.getState().setBoard('x');
assert.match(utcIsoToLocalInput(useReviewUi.getState().customFrom), /T07:00$/, '未用过的 v1.3.0 默认 07:00');
useReviewUi.getState().setBoard('main');
assert.equal(useReviewUi.getState().customFrom, from, '返回 v1.4.0 保留手动起点');
assert.equal(useReviewUi.getState().customTo, to, '返回 v1.4.0 保留手动终点');
assert.equal(useReviewUi.getState().useCustom, true);
console.log('PASS: time preferences persist and remain separate by board');

for (const board of ['main', 'x', 'y'] as const) {
  useReviewUi.getState().setBoard(board);
  useReviewUi.getState().setCustom(from, to);
  const saved = readReviewTime(board);
  assert.equal(saved.customFrom, from);
  assert.equal(saved.customTo, to);
  assert.equal(saved.useCustom, true);
  useReviewUi.getState().toggleCompare();
  if (!useReviewUi.getState().comparePeriods.length) useReviewUi.getState().addComparePeriod();
  const id = useReviewUi.getState().comparePeriods[0].id;
  useReviewUi.getState().updateComparePeriod(id, { from, to });
  assert.equal(readReviewTime(board).comparePeriods[0].from, from);
  useReviewUi.getState().toggleCompare();
  assert.equal(readReviewTime(board).comparePeriods[0].to, to, '关闭比较不清除输入');
  useReviewUi.getState().setRange(14);
  assert.equal(readReviewTime(board).useCustom, false);
  assert.equal(readReviewTime(board).customFrom, from, '快捷周期保留编辑值');
}
console.log('PASS: all three versions retain edited main and comparison times');

const override = { ...readReviewTime('y'), ...searchToState('?from=2026-08-01T00:00:00Z&to=2026-08-02T00:00:00Z') };
assert.equal(override.customFrom, '2026-08-01T00:00:00Z');
assert.equal(override.useCustom, true);
assert.equal(searchToState('?range=7').useCustom, false);
useReviewUi.getState().clearCustom();
assert.equal(readReviewTime('y').customFrom, '', '显式清空不强制重填');
assert.equal(readReviewTime('y').useCustom, false);
useReviewUi.getState().setCustom(from, '');
assert.equal(readReviewTime('y').customFrom, from);
assert.equal(readReviewTime('y').useCustom, false, '半填不激活查询');
const manualSeven = localInputToUtcIso('2026-09-02T07:00');
useReviewUi.getState().setCustom(manualSeven, to);
assert.equal(utcIsoToLocalInput(readReviewTime('y').customFrom), '2026-09-02T07:00');
console.log('PASS: URL override, quick ranges, clearing, partial input and manual return to 07:00');

// Auto-aligned parameter windows must not overwrite the manually remembered editor.
useReviewUi.getState().lockParamVersion('param-v2.0.0-screener-y', '2026-08-01T03:15:00Z', '2026-08-02T05:30:00Z');
assert.equal(useReviewUi.getState().customFrom, '2026-08-01T03:15:00Z');
assert.equal(readReviewTime('y').customFrom, manualSeven);
useReviewUi.getState().lockParamHash('test-hash', '2026-08-03T03:15:00Z', '2026-08-04T05:30:00Z');
assert.equal(readReviewTime('y').customFrom, manualSeven);
console.log('PASS: version/hash data-span alignment remains exact and does not overwrite manual memory');

assert.deepEqual(defaultReviewRange(7, Date.parse('2026-12-31T18:30:00Z')), {
  from: '2026-12-25T00:00:00Z', to: '2027-01-01T00:00:00Z',
}, '按越南当地日期处理跨年，而不是机器日期');
assert.equal(localInputToUtcIso('2026-09-02T07:00'), '2026-09-02T00:00:00Z');
for (const bad of ['{', 'null', '{}', JSON.stringify({ ...readReviewTime('main'), customFrom: 'invalid' })]) {
  memory.set(`${REVIEW_TIME_STORAGE_KEY}:x`, bad);
  assert.match(utcIsoToLocalInput(readReviewTime('x').customFrom), /T07:00$/);
}
Object.defineProperty(globalThis, 'localStorage', { configurable: true, get: () => { throw new Error('blocked'); } });
assert.match(utcIsoToLocalInput(readReviewTime('x').customTo), /T07:00$/);
assert.doesNotThrow(() => useReviewUi.getState().setCustom(from, to));
console.log('PASS: timezone/year boundary, invalid storage and blocked storage');
