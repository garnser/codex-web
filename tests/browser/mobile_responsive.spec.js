const { test, expect } = require('@playwright/test');

const portraitWidths = [320, 360, 375, 390, 414];

async function assertNoPageOverflow(page) {
  const dimensions = await page.evaluate(() => ({
    inner: window.innerWidth,
    root: document.documentElement.scrollWidth,
    body: document.body.scrollWidth,
  }));
  expect(dimensions.root).toBeLessThanOrEqual(dimensions.inner);
  expect(dimensions.body).toBeLessThanOrEqual(dimensions.inner);
}

for (const width of portraitWidths) {
  test(`shared mobile shell works at ${width}px without page overflow`, async ({ page }) => {
    await page.setViewportSize({ width, height: 780 });
    await page.goto('http://127.0.0.1:18766/tests/browser/mobile_responsive_fixture.html');

    const navToggle = page.locator('#mobile-nav-toggle');
    await expect(navToggle).toBeVisible();
    await expect(navToggle).toHaveAttribute('aria-expanded', 'false');
    await expect(page.locator('.sidebar')).toHaveAttribute('aria-hidden', 'true');
    await assertNoPageOverflow(page);

    const box = await navToggle.boundingBox();
    expect(box.width).toBeGreaterThanOrEqual(44);
    expect(box.height).toBeGreaterThanOrEqual(44);

    await navToggle.click();
    await expect(navToggle).toHaveAttribute('aria-expanded', 'true');
    await expect(page.locator('body')).toHaveClass(/mobile-nav-open/);
    await expect(page.locator('.sidebar')).toHaveAttribute('aria-hidden', 'false');
    await expect(page.locator('#project-one')).toBeFocused();

    await page.keyboard.press('Escape');
    await expect(navToggle).toHaveAttribute('aria-expanded', 'false');
    await expect(navToggle).toBeFocused();

    await page.locator('#open-dialog').click();
    const dialog = page.locator('#fixture-dialog');
    await expect(dialog).toBeVisible();
    const dialogBox = await dialog.boundingBox();
    expect(dialogBox.x).toBeGreaterThanOrEqual(0);
    expect(dialogBox.width).toBeLessThanOrEqual(width);
    expect(dialogBox.height).toBeLessThanOrEqual(780);

    const input = dialog.locator('input');
    const inputBox = await input.boundingBox();
    expect(inputBox.height).toBeGreaterThanOrEqual(44);
    await assertNoPageOverflow(page);
  });
}

test('wide table uses bounded horizontal scrolling rather than stretching the page', async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 700 });
  await page.goto('http://127.0.0.1:18766/tests/browser/mobile_responsive_fixture.html');
  const wrapper = page.locator('.responsive-table-wrap');
  const data = await wrapper.evaluate((node) => ({
    client: node.clientWidth,
    scroll: node.scrollWidth,
    overflowX: getComputedStyle(node).overflowX,
  }));
  expect(data.client).toBeLessThanOrEqual(320);
  expect(['auto', 'scroll']).toContain(data.overflowX);
  await assertNoPageOverflow(page);
});

test('mobile navigation closes after selecting a sidebar destination', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('http://127.0.0.1:18766/tests/browser/mobile_responsive_fixture.html');
  await page.locator('#mobile-nav-toggle').click();
  await page.locator('#thread-one').click();
  await expect(page.locator('body')).not.toHaveClass(/mobile-nav-open/);
  await expect(page.locator('#mobile-nav-toggle')).toHaveAttribute('aria-expanded', 'false');
});

test('constrained landscape keeps navigation, controls and dialog within viewport', async ({ page }) => {
  await page.setViewportSize({ width: 640, height: 360 });
  await page.goto('http://127.0.0.1:18766/tests/browser/mobile_responsive_fixture.html');
  await expect(page.locator('#mobile-nav-toggle')).toBeVisible();
  await assertNoPageOverflow(page);

  await page.locator('#open-dialog').click();
  const box = await page.locator('#fixture-dialog').boundingBox();
  expect(box.width).toBeLessThanOrEqual(640);
  expect(box.height).toBeLessThanOrEqual(360);

  const controls = await page.locator('.controls').boundingBox();
  expect(controls.x + controls.width).toBeLessThanOrEqual(640);
});
