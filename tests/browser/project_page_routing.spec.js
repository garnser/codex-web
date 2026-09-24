const fs = require('fs');
const path = require('path');
const { test, expect } = require('@playwright/test');

const fixtureHtml = fs.readFileSync(
  path.join(__dirname, 'product_workspaces_fixture.html'),
  'utf8',
);

async function serveProjectShell(page) {
  await page.route('**/projects/**', async (route) => {
    if (route.request().resourceType() !== 'document') {
      await route.continue();
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: 'text/html',
      body: fixtureHtml,
    });
  });
}

test('direct Project page URL restores Project and workspace context on reload', async ({ page }) => {
  await serveProjectShell(page);

  await page.goto('http://127.0.0.1:18766/projects/alpha/agents');

  await expect(page).toHaveURL(/\/projects\/alpha\/agents$/);
  await expect(page.locator('body')).toHaveAttribute('data-project-page', 'agents');
  await expect(page.locator('#product-project-switcher')).toHaveValue('alpha');
  await expect(page.locator('#product-workspace-dialog')).toBeVisible();
  await expect(page.locator('[data-product-workspace-title]')).toHaveText('Team / Agents');

  await page.reload();

  await expect(page).toHaveURL(/\/projects\/alpha\/agents$/);
  await expect(page.locator('#product-project-switcher')).toHaveValue('alpha');
  await expect(page.locator('[data-product-workspace-title]')).toHaveText('Team / Agents');
});

test('navigation uses stable Project paths and browser Back restores prior page', async ({ page }) => {
  await serveProjectShell(page);
  await page.goto('http://127.0.0.1:18766/projects/home/overview');

  const navigation = page.locator('.product-project-navigation');
  const automationGroup = navigation.locator('[data-project-nav-group="automation-group"]');
  if (!(await automationGroup.evaluate((element) => element.open))) {
    const summary = automationGroup.locator('summary');
    await summary.focus();
    await expect(summary).toBeFocused();
    await page.keyboard.press('Enter');
    await expect(automationGroup).toHaveAttribute('open', '');
  }
  const automations = navigation.locator('[data-project-nav-node="automations"]');
  await automations.focus();
  await expect(automations).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(page).toHaveURL(/\/projects\/home\/automations$/);
  await expect(page.locator('body')).toHaveAttribute('data-project-page', 'automations');

  const chat = navigation.locator('[data-project-nav-node="chat"]');
  await chat.focus();
  await expect(chat).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(page).toHaveURL(/\/projects\/home\/chat$/);
  await expect(page.locator('#product-workspace-dialog')).toBeHidden();
  await expect(navigation.locator('[data-project-nav-node="chat"]')).toHaveAttribute('aria-current', 'page');

  await page.goBack();
  await expect(page).toHaveURL(/\/projects\/home\/automations$/);
  await expect(page.locator('#product-workspace-dialog')).toBeVisible();
  await expect(page.locator('[data-product-workspace-title]')).toHaveText('Automation / Autonomy');
});

test('Project switch preserves routed page instead of falling back to Chat', async ({ page }) => {
  await serveProjectShell(page);
  await page.goto('http://127.0.0.1:18766/projects/home/operations');

  await page.locator('#product-project-switcher').selectOption('alpha');

  await expect(page).toHaveURL(/\/projects\/alpha\/operations$/);
  await expect(page.locator('body')).toHaveAttribute('data-active-project', 'alpha');
});
