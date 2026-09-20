const { test, expect } = require('@playwright/test');

test('retained execution preflight renders blocker and authorized retry', async ({ page }) => {
  await page.goto('http://127.0.0.1:18766/tests/browser/execution_preflight_fixture.html');
  await expect.poll(() => page.evaluate(() => Boolean(window.__ready))).toBe(true);

  await expect(page.locator('.message.user')).toContainText('make the requested change');
  await expect(page.locator('.message.tool')).toContainText('worker_capability_missing');
  await expect(page.locator('.message.tool')).toContainText('Restore a qualified worker.');
  await expect(page.locator('.message.tool')).toContainText('Correlation: thread-turn-1');

  const retry = page.getByRole('button', { name: 'Retry' });
  await expect(retry).toBeVisible();
  await retry.click();

  await expect.poll(() => page.evaluate(() => window.__apiCalls.filter(
    (item) => item.method === 'POST'
  ).length)).toBe(1);
  expect(await page.evaluate(() => window.__reloadCalls)).toEqual(['thread-1']);
  expect(await page.evaluate(() => window.__refreshCalls)).toEqual([0]);
});

test('preflight handler reloads retained state instead of showing a dead turn', async ({ page }) => {
  await page.goto('http://127.0.0.1:18766/tests/browser/execution_preflight_fixture.html');
  await expect.poll(() => page.evaluate(() => Boolean(window.__ready))).toBe(true);

  const handled = await page.evaluate(() => window.__ui.handleError({
    detail: { code: 'execution_preflight_blocked' },
  }, 'thread-1'));

  expect(handled).toBe(true);
  expect(await page.evaluate(() => window.__reloadCalls)).toEqual(['thread-1']);
  expect(await page.evaluate(() => window.__refreshCalls)).toEqual([0]);
});
