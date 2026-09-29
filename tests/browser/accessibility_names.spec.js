const { test, expect } = require('@playwright/test');

test('all static form controls and icon actions retain names without placeholders or titles', async ({ page }) => {
  test.setTimeout(90_000);
  // Inspect production markup without booting services or depending on remote state.
  await page.route('**/*.js*', route => route.abort());
  await page.goto('http://127.0.0.1:18766/static/index.html');
  await page.evaluate(() => {
    document.querySelectorAll('[hidden]').forEach(node => node.removeAttribute('hidden'));
    document.querySelectorAll('dialog').forEach(node => node.setAttribute('open', ''));
    document.querySelectorAll('details').forEach(node => { node.open = true; });
    document.querySelectorAll('[placeholder], [title]').forEach(node => {
      node.removeAttribute('placeholder');
      node.removeAttribute('title');
    });
  });
  const controls = page.locator('input:not([type=hidden]), textarea, select, button');
  expect(await controls.count()).toBeGreaterThan(150);
  for (const control of await controls.all()) {
    await expect(control, `Accessible name for #${await control.getAttribute('id')}`).toHaveAccessibleName(/\S/);
  }
});


test('workspace transitions place keyboard focus on the destination heading', async ({ page }) => {
  await page.goto('http://127.0.0.1:18766/tests/browser/product_workspaces_fixture.html');
  for (const destination of ['Goals', 'Resources']) {
    await page.keyboard.press('Control+K');
    await page.locator('#product-workspace-search').fill(destination);
    await page.keyboard.press('Enter');
    const heading = page.locator('#product-workspace-page-title');
    await expect(heading).toHaveText(destination);
    await expect(heading).toBeFocused();
    await page.keyboard.press('Tab');
    await expect(page.locator('[data-open-workspace-switcher]')).toBeFocused();
  }
});
