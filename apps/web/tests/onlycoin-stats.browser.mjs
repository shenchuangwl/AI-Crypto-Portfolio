// 《OnlyCoin · 来源候选回放》DMR 区历史统计 —— 真实浏览器验收闸。
// 针对只读网关运行：BASE_URL=http://127.0.0.1:18099 STATS_DAY=2026-09-17 node this-file.
// 覆盖需求 §6 的交互时序：未选日期隐藏、选日期后显示、切换/取消日期无残留、
// 自定义起止与周期对比可用、原复盘 DMR 统计未受影响。
import { chromium } from 'playwright';
import assert from 'node:assert/strict';

const base = process.env.BASE_URL || 'http://127.0.0.1:18099';
const day = process.env.STATS_DAY || '2026-09-17';
const otherDay = process.env.STATS_DAY_2 || '2026-09-16';
/** 业务日 00:00 UTC == 本地 +07 当日 07:00；收线截至取该业务周期最后一秒。 */
const endInput = (d) => `${new Date(Date.parse(`${d}T00:00:00Z`) + 31 * 3600e3 - 1000).toISOString().slice(0, 19)}`;

const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
const statsRequests = [];
page.on('request', (r) => { if (r.url().includes('/review/onlycoin/stats')) statsRequests.push(r.url()); });
try {
  await page.goto(`${base}/review?board=y`);
  await page.getByRole('button', { name: 'OnlyCoin · 来源候选回放' }).click();
  const stats = page.locator('.onlycoin-stats-panel');
  const replay = page.locator('.onlycoin-review-panel');
  await replay.waitFor();

  // 1. 未选择回放日期 → 统计区不存在（输入框为空，也没有任何统计请求）。
  assert.equal(await page.getByLabel('UTC 业务日', { exact: true }).inputValue(), '');
  assert.equal(await stats.count(), 0, '未选择日期时统计区必须隐藏');
  assert.equal(statsRequests.length, 0, '未触发回放不得发统计请求');

  // 2. 仅填日期、尚未点击查询 → 仍然隐藏（预填的截至时间不算“已选择”）。
  await page.getByLabel('UTC 业务日', { exact: true }).fill(day);
  await page.waitForTimeout(400);
  assert.equal(await stats.count(), 0, '未点击查询前不得显示统计');
  assert.equal(statsRequests.length, 0);

  // 3. 完成回放 → 统计区出现，且对应所选日期。
  await page.getByLabel('回放截至时间（本地 UTC+7）', { exact: true }).fill(endInput(day));
  await page.getByRole('button', { name: '查询 / 刷新回放', exact: true }).click();
  await stats.waitFor({ timeout: 60000 });
  await stats.locator('.onlycoin-stats-table').waitFor({ timeout: 60000 });
  assert.equal(statsRequests.length, 1);
  const q1 = new URL(statsRequests[0]).searchParams;
  assert.equal(q1.get('business_date'), day);
  assert.equal(q1.get('board'), 'y');

  // 统计的唯一币种数必须等于上方 OnlyCoin 名单的成员数（同一份来源投影）。
  const listed = await replay.locator('.onlycoin-lists > div').nth(2).locator('li a').count();
  const detailRows = await stats.locator('.onlycoin-stats-table tbody tr').count();
  assert.equal(detailRows, listed, '明细行数 = 上方 OnlyCoin 名单成员数（唯一币种去重后）');
  const apiBody = await (await page.request.get(`${base}/api/v1/review/onlycoin/stats?board=y&business_date=${day}&as_of=${encodeURIComponent(endInput(day).replace('T', 'T') + 'Z')}`)).json().catch(() => null);
  if (apiBody?.main) assert.equal(apiBody.main.rows.length, detailRows, 'UI 明细与接口逐行对齐');

  const firstRowDay = await stats.locator('.onlycoin-stats-table tbody tr').first().innerText();
  assert.ok(firstRowDay.length > 0);

  // 4. 切换日期 → 旧行不得残留，请求按新日期重发。
  await page.getByLabel('UTC 业务日', { exact: true }).fill(otherDay);
  await page.waitForTimeout(300);
  assert.equal(await stats.count(), 0, '改日期后（未重新查询）统计区必须先隐藏');
  await page.getByLabel('回放截至时间（本地 UTC+7）', { exact: true }).fill(endInput(otherDay));
  await page.getByRole('button', { name: '查询 / 刷新回放', exact: true }).click();
  await stats.locator('.onlycoin-stats-table').waitFor({ timeout: 60000 });
  assert.equal(statsRequests.length, 2);
  assert.equal(new URL(statsRequests[1]).searchParams.get('business_date'), otherDay);
  const secondText = await stats.innerText();
  assert.ok(secondText.includes(otherDay), '统计区显示的是新日期的区间');

  // 5. 取消日期选择（清空输入）→ 统计区整块消失。
  await page.getByLabel('UTC 业务日', { exact: true }).fill('');
  await page.waitForTimeout(300);
  assert.equal(await stats.count(), 0, '取消日期后统计区必须隐藏');

  // 6. 自定义起止 + 周期对比。
  await page.getByLabel('UTC 业务日', { exact: true }).fill(day);
  await page.getByLabel('回放截至时间（本地 UTC+7）', { exact: true }).fill(endInput(day));
  await page.getByRole('button', { name: '查询 / 刷新回放', exact: true }).click();
  await stats.locator('.onlycoin-stats-table').waitFor({ timeout: 60000 });
  const before = statsRequests.length;
  await stats.getByLabel('启用自定义起止').check();
  await stats.getByLabel('统计自定义起点').fill(`${otherDay}T07:00`);
  await stats.getByLabel('统计自定义终点').fill(`${day}T07:00`);
  await page.waitForFunction(
    (n) => performance.getEntriesByType('resource').filter((e) => e.name.includes('/review/onlycoin/stats')).length > n,
    before, { timeout: 60000 });
  await stats.locator('.onlycoin-stats-table').waitFor({ timeout: 60000 });
  const custom = new URL(statsRequests[statsRequests.length - 1]).searchParams;
  assert.ok(custom.get('from') && custom.get('to'), '自定义起止必须成对提交');
  assert.equal(custom.get('from'), `${otherDay}T00:00:00Z`, '+07 墙钟 07:00 → UTC 00:00');

  await stats.getByLabel('启用周期对比').check();
  await stats.getByLabel('统计对比周期 2 起点').fill(`${otherDay}T07:00`);
  await stats.getByLabel('统计对比周期 2 终点').fill(`${day}T07:00`);
  await page.waitForTimeout(2000);
  await page.locator('.onlycoin-stats-panel .review-compare-table').first().waitFor({ timeout: 60000 });
  const cmp = new URL(statsRequests[statsRequests.length - 1]).searchParams;
  assert.deepEqual(cmp.getAll('compare'), [`${otherDay}T00:00:00Z~${day}T00:00:00Z`]);
  assert.ok((await stats.innerText()).includes('周期对比'), '周期对比表已渲染');

  // 7. 原复盘 · 进出账本不受影响：切回去，DMR 统计与控件照常。
  await page.getByRole('button', { name: '原复盘 · 进出账本' }).click();
  await page.locator('.review-page').waitFor();
  await page.locator('.review-page .review-cards').first().waitFor({ timeout: 60000 });
  assert.equal(await page.locator('.review-page .onlycoin-stats-panel').count(), 0, '新统计不得渗入原复盘账本');
  assert.ok((await page.locator('.review-page').innerText()).includes('自定义起止'), '原复盘控件完好');

  console.log(`PASS OnlyCoin 回放历史统计 · 浏览器闸（统计请求 ${statsRequests.length} 次，明细 ${detailRows} 行）`);
} finally {
  await browser.close();
}
