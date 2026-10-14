const { test, expect } = require('@playwright/test');

for (const phase of ['prepared','prestart_refused','native_outcome_unknown','native_input_started','released']) {
  test(`batch ${phase} exposes originals and uncertainty without individual replay`, async ({ page }) => {
    await page.goto(`http://127.0.0.1:18766/tests/browser/execution_preflight_fixture.html?disposition=reconciliation_required&batch=${phase}`);
    await expect.poll(() => page.evaluate(() => Boolean(window.__ready))).toBe(true);
    await expect(page.getByText('Original count: 2; oldest age: 600 seconds', {exact:false})).toBeVisible();
    await expect(page.getByText('Every original outcome remains unresolved.', {exact:false})).toBeVisible();
    await expect(page.getByRole('button',{name:/^Retry$|Reconcile unstarted refusal|Retire terminal scheduling/})).toHaveCount(0);
    const shared = page.locator('.message.tool').filter({hasText:'Original count: 2'});
    if (phase==='native_input_started') await expect(shared).toContainText('Shared native turn: shared-native-turn');
    if (phase==='released') await expect(shared).toContainText('Admission released · originals returned to queue');
    await shared.getByRole('button',{name:'Inspect batch originals'}).click();
    await expect(shared.locator('pre')).toContainText('Full original <img src=x onerror="window.__unsafe=true"> body');
    await expect(shared.locator('pre')).toContainText('Requested execution: requested-one');
    await expect(shared.locator('pre')).toContainText('Original thread: original-thread; admitted thread: thread-1');
    await expect(page.locator('img')).toHaveCount(0);
    expect(await page.evaluate(() => window.__unsafe)).toBeUndefined();
    expect(await page.evaluate(() => window.__apiCalls.every(call=>call.method==='GET'))).toBe(true);
  });
}

test('denied batch detail remains visible and cannot enable replay', async ({ page }) => {
  await page.goto('http://127.0.0.1:18766/tests/browser/execution_preflight_fixture.html?batch=prepared&deniedBatchDetail');
  await expect.poll(() => page.evaluate(() => Boolean(window.__ready))).toBe(true);
  await page.getByRole('button',{name:'Inspect batch originals'}).first().click();
  await expect(page.locator('pre').first()).toContainText('Batch unavailable in this scope');
  await expect(page.getByRole('button',{name:'Retry',exact:true})).toHaveCount(0);
});

test('late batch detail never replaces another Project view', async ({ page }) => {
  await page.goto('http://127.0.0.1:18766/tests/browser/execution_preflight_fixture.html?batch=prepared&holdBatchDetail');
  await expect.poll(() => page.evaluate(() => Boolean(window.__ready))).toBe(true);
  await page.getByRole('button',{name:'Inspect batch originals'}).first().click();
  await expect.poll(() => page.evaluate(() => Boolean(window.__finishBatchDetail))).toBe(true);
  await page.evaluate(() => {
    window.dispatchEvent(new CustomEvent('codex:project-changed',{detail:{projectId:'foreign'}}));
    window.__finishBatchDetail.resolve({originals:[{original:{message:'Late foreign content',created_at:1}}]});
  });
  await page.waitForTimeout(100);
  await expect(page.locator('#messages')).not.toContainText('Late foreign content');
});

test('older admission history loads through read-only pagination', async ({ page }) => {
  await page.goto('http://127.0.0.1:18766/tests/browser/execution_preflight_fixture.html?batch=native_input_started&pagedBatch');
  await expect.poll(() => page.evaluate(() => Boolean(window.__ready))).toBe(true);
  await page.getByRole('button',{name:'Load older admissions'}).click();
  await expect(page.getByText('Batch: fixture-older-batch',{exact:false})).toBeVisible();
  await expect(page.getByText('Shared native input delivered',{exact:true})).toBeVisible();
  await expect(page.getByRole('button',{name:'Load older admissions'})).toHaveCount(0);
  expect(await page.evaluate(() => window.__apiCalls.every(call=>call.method==='GET'))).toBe(true);
});

test('failed older-history load preserves the current observed admission', async ({ page }) => {
  await page.goto('http://127.0.0.1:18766/tests/browser/execution_preflight_fixture.html?batch=native_input_started&pagedBatch&deniedBatchHistory');
  await expect.poll(() => page.evaluate(() => Boolean(window.__ready))).toBe(true);
  await page.getByRole('button',{name:'Load older admissions'}).click();
  await expect(page.getByText('Older history unavailable',{exact:true})).toBeVisible();
  await expect(page.getByText('Shared native input delivered',{exact:true})).toBeVisible();
  await expect(page.getByRole('button',{name:'Load older admissions'})).toBeEnabled();
});
