// Read-only UI round trips. Uses real gateway data; no trading/admin writes.
// BASE_URL=http://127.0.0.1:18080 node tests/kline-return.browser.mjs
import assert from 'node:assert/strict';
import { readFileSync, writeFileSync } from 'node:fs';
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const base = process.env.BASE_URL || 'http://127.0.0.1:18080';
const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
page.setDefaultTimeout(20000);
const results = [];
const zones = ['DMR', 'CONFIRMED', 'QUALIFIED', 'WATCH', 'ELIMINATED', 'DATA_INSUFFICIENT', 'LOW_CONFIDENCE'];
// Fail if production adds a partition without extending this interaction matrix.
const reviewTypes = readFileSync('src/shared/types/review.ts', 'utf8');
const declaredZones = [...reviewTypes.split('export type ReviewZone =')[1].split(';')[0].matchAll(/'([A-Z_]+)'/g)].map(m => m[1]);
assert.deepEqual(zones, declaredZones);
const boards = { main: '/screener', x: '/screener-x', y: '/screener-y' };
const versions = { main: 'v1.4.0', x: 'v1.3.0', y: 'v2.0.0' };
const onlycoinTab = () => page.getByRole('button', { name: 'OnlyCoin · 来源候选回放', exact: true });
const normalTab = () => page.getByRole('button', { name: '原复盘 · 进出账本', exact: true });
const selectedZones = () => page.locator('.chips .chip.on').evaluateAll(es => es.map(e => [...e.classList].find(c => c.startsWith('state-')).slice(6).toUpperCase()).sort());
const save = () => {
  if (process.env.RESULT_FILE) writeFileSync(process.env.RESULT_FILE, JSON.stringify(results, null, 2));
};
async function selectZone(zone) {
  const target = page.locator(`.chips .state-${zone.toLowerCase()}`);
  if (!(await target.getAttribute('class')).split(' ').includes('on')) await target.click();
  for (const other of zones.filter(z => z !== zone)) {
    const chip = page.locator(`.chips .state-${other.toLowerCase()}.on`);
    if (await chip.count()) await chip.click();
  }
  await page.waitForFunction(zone => {
    const chips = [...document.querySelectorAll('.chips .chip.on')];
    return chips.length === 1 && chips[0].classList.contains(`state-${zone.toLowerCase()}`);
  }, zone);
  assert.deepEqual(await selectedZones(), [zone]);
}
async function visitReview(url) {
  const response = page.waitForResponse(r => new URL(r.url()).pathname === '/api/v1/review/trades');
  await page.goto(url);
  const trades = await response;
  assert.ok(trades.ok(), `review request failed: HTTP ${trades.status()}`);
  assert.ok(Array.isArray((await trades.json()).trades), 'review response must contain ledger rows');
  await page.locator('.review-page .chips').waitFor();
  await page.locator('.review-page .grid-row, .review-page .grid-wrap .empty').first().waitFor();
  assert.equal(await page.locator('.review-banner.error').count(), 0, 'API errors must not be classified as empty partitions');
}
async function openReview(board, zone) {
  await visitReview(`${base}/review?board=${board}&zones=${zone}&range=7`);
  assert.deepEqual(await selectedZones(), [zone]);
  assert.ok((await page.locator('.review-page .brand-title').innerText()).includes(versions[board]));
}
async function roundTrip(locator, { id, board, zone, origin, module = 'normal' }) {
  const source = page.url();
  const beforeZones = await selectedZones();
  await locator.click();
  await page.locator('.market-page a.back').waitFor();
  const market = page.url();
  assert.equal(new URL(market).searchParams.get('from'), origin);
  // Interval changes must retain origin, rather than regenerate it from the version.
  await page.getByRole('tab', { name: '30m', exact: true }).click();
  assert.equal(new URL(page.url()).searchParams.get('from'), origin);
  const back = await page.locator('a.back').getAttribute('href');
  await page.locator('a.back').click();
  const path = origin.startsWith('screener') ? boards[board] : '/review';
  assert.equal(new URL(page.url()).pathname, path, `${id}: correct column`);
  if (path === '/review') {
    await page.waitForURL(url => url.pathname === path && url.searchParams.get('zones') === zone);
    assert.equal(new URL(page.url()).searchParams.get('board') || 'main', board);
    if (board === 'y') {
      assert.equal(await onlycoinTab().getAttribute('aria-pressed'), String(module === 'onlycoin'), `${id}: correct submodule`);
      assert.equal(await normalTab().getAttribute('aria-pressed'), String(module !== 'onlycoin'));
    }
    if (module === 'normal') await page.locator('.review-page .chips').waitFor();
    else {
      await page.locator('[data-testid="onlycoin-live"]').waitFor();
      // The original ledger remains hidden and fetches independently on remount.
      await page.locator('.review-page .chips').waitFor({ state: 'attached' });
    }
  } else {
    await page.locator(`.board-${board} .chips`).waitFor();
  }
  assert.deepEqual(await selectedZones(), beforeZones, `${id}: partition restored`);
  const item = { id, board, zone, source, market, back, returned: page.url(), status: 'passed' };
  results.push(item); save(); console.log(`PASS ${id}`);
}
async function loadHistory() {
  const api = await (await page.request.get(`${base}/api/v1/onlycoin/live`)).json();
  assert.equal(api.valid, true, 'live source required for a real historical cutoff');
  const local = new Date(Date.parse(api.server_time) + 7 * 60 * 60 * 1000).toISOString().slice(0, 19);
  await page.getByLabel('UTC 业务日', { exact: true }).fill(api.business_date);
  // Chromium normalizes :00 seconds away; Playwright fill can reject that otherwise-valid value.
  await page.getByLabel('回放截至时间（本地 UTC+7）', { exact: true }).evaluate((input, value) => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(input, value);
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
  }, local);
  await page.getByRole('button', { name: '查询 / 刷新回放', exact: true }).click();
  await page.locator('.onlycoin-review-panel .onlycoin-lists').waitFor();
}
try {
  for (const zone of process.env.SUITE === 'baseline' ? [] : zones) {
    await openReview('y', zone);
    await onlycoinTab().click();
    for (const surface of ['live', 'history']) {
      for (const [column, name] of ['LONG', 'SHORT', 'OnlyCoin'].entries()) {
        if (surface === 'history') await loadHistory();
        const panel = surface === 'live' ? '[data-testid="onlycoin-live"]' : '.onlycoin-review-panel';
        const link = page.locator(`${panel} .onlycoin-lists > div`).nth(column).locator('a').first();
        await link.waitFor();
        await roundTrip(link, { id: `C/${zone}/${surface}/${name}`, board: 'y', zone, origin: 'review-onlycoin', module: 'onlycoin' });
      }
    }
    // A must still work immediately after C in the same SPA session.
    await page.locator('a[href="/screener-y"]').click();
    await page.locator('.board-y .chips').waitFor();
    await selectZone(zone);
    for (const [column, name] of ['LONG', 'SHORT', 'OnlyCoin'].entries()) {
      const link = page.locator('.onlycoin-panel .onlycoin-lists > div').nth(column).locator('a').first();
      await link.waitFor();
      await roundTrip(link, { id: `A/${zone}/${name}`, board: 'y', zone, origin: 'screener-y' });
    }
  }
  for (const [board, path] of Object.entries(boards)) {
    for (const zone of zones) {
      await openReview(board, zone);
      if (!(await page.locator('.review-page .grid-row').count())) {
        const response = await page.request.get(`${base}/api/v1/review/coverage?board=${board}`);
        assert.ok(response.ok(), 'coverage lookup must succeed before broadening the window');
        const coverage = await response.json();
        assert.ok(coverage.first_ts && coverage.watermark_ts, 'physical coverage required before declaring an empty ledger');
        const search = new URLSearchParams({ board, zones: zone, from: coverage.first_ts, to: coverage.watermark_ts });
        await visitReview(`${base}/review?${search}`);
      }
      const row = page.locator('.review-page .grid-row').first();
      if (await row.count()) {
        await roundTrip(row.locator('.td').first(), { id: `B/${board}/${zone}`, board, zone, origin: 'review' });
      } else {
        assert.equal(await page.locator('.review-page .grid-wrap .empty').innerText(), '该交叉条件下 0 笔完整交易');
        results.push({ id: `B/${board}/${zone}`, status: 'skipped', reason: 'real ledger has no closed rows even across physical coverage' }); save();
      }
      await page.locator(`a[href="${path}"]`).click();
      await page.locator(`.board-${board} .chips`).waitFor();
      await selectZone(zone);
      await page.locator(`.board-${board} .grid-row, .board-${board} .grid-wrap .empty`).first().waitFor();
      let gridRow = page.locator('.grid-row').first();
      if (!(await gridRow.count())) {
        await page.locator('.dir-tab.down').click();
        await page.locator('.dir-tab.down.on').waitFor();
        await page.locator(`.board-${board} .grid-row, .board-${board} .grid-wrap .empty`).first().waitFor();
        gridRow = page.locator('.grid-row').first();
      }
      if (await gridRow.count()) {
        await roundTrip(gridRow.locator('.td').first(), { id: `grid/${board}/${zone}`, board, zone, origin: path.slice(1) });
      } else {
        assert.equal(await page.locator(`.board-${board} .grid-wrap .empty`).innerText(), '当前过滤条件下无候选');
        results.push({ id: `grid/${board}/${zone}`, status: 'skipped', reason: 'real snapshot partition empty in both directions' }); save();
      }
    }
  }
  assert.equal(results.length, zones.length * ((process.env.SUITE === 'baseline' ? 0 : 9) + Object.keys(boards).length * 2));
  assert.equal(new Set(results.map(r => r.id)).size, results.length);
  console.log(JSON.stringify({ passed: results.filter(r => r.status === 'passed').length, skipped: results.filter(r => r.status === 'skipped') }));
} finally {
  save();
  await browser.close();
}
