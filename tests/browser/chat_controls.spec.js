const { test, expect } = require('@playwright/test');

test('chat execution controls are compact and persist through thread settings', async ({ page }) => {
  await page.goto('http://127.0.0.1:18766/tests/browser/chat_controls_fixture.html');

  await expect(page.locator('#sandbox')).toBeHidden();
  await expect(page.locator('#rename-thread')).toBeHidden();
  expect((await page.locator('.topbar').boundingBox()).height).toBeLessThan(110);

  await page.locator('#thread-settings-menu > summary').click();
  await expect(page.locator('#sandbox')).toBeVisible();
  await page.locator('#sandbox').selectOption('danger-full-access');
  await expect(page.locator('#sandbox-danger-warning')).toBeVisible();
  await page.locator('#repository-write-targets').selectOption(['repo-app', 'repo-api']);

  await expect.poll(async () => page.evaluate(() => window.updates)).toEqual(expect.arrayContaining([
    { threadId: 'thread-1', updates: { sandbox: 'danger-full-access' } },
    { threadId: 'thread-1', updates: { writable_repository_resource_ids: ['repo-app', 'repo-api'] } },
  ]));

  await page.locator('#thread-actions-menu > summary').click();
  await expect(page.locator('#thread-settings-menu')).not.toHaveAttribute('open', '');
  await expect(page.locator('#rename-thread')).toBeVisible();
  await expect(page.locator('#archive-thread')).toBeVisible();
});

test('chat thread menus fit a 320px phone viewport', async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 700 });
  await page.goto('http://127.0.0.1:18766/tests/browser/chat_controls_fixture.html');

  const dimensions = await page.evaluate(() => ({
    viewport: window.innerWidth,
    root: document.documentElement.scrollWidth,
    body: document.body.scrollWidth,
  }));
  expect(dimensions.root).toBeLessThanOrEqual(dimensions.viewport);
  expect(dimensions.body).toBeLessThanOrEqual(dimensions.viewport);

  for (const selector of ['#thread-settings-menu > summary', '#thread-actions-menu > summary']) {
    const box = await page.locator(selector).boundingBox();
    expect(box.height).toBeGreaterThanOrEqual(44);
  }

  await page.locator('#thread-settings-menu > summary').click();
  const popover = await page.locator('.thread-settings-popover').boundingBox();
  expect(popover.x).toBeGreaterThanOrEqual(0);
  expect(popover.x + popover.width).toBeLessThanOrEqual(320);
  expect(popover.y + popover.height).toBeLessThanOrEqual(700);
});
