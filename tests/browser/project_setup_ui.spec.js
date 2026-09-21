const { test, expect } = require('@playwright/test');

test('blocked Project exposes setup, exact readiness blocker, and gates execution controls', async ({ page }) => {
  await page.goto('http://127.0.0.1:18766/tests/browser/project_setup_ui_fixture.html');

  await expect(page.locator('#project-setup-launch')).toHaveText('Project setup');
  await expect(page.locator('#new-thread')).toBeDisabled();
  await expect(page.locator('#send')).toBeDisabled();
  await expect(page.locator('#prompt')).toBeDisabled();

  await page.locator('#project-setup-launch').click();
  const dialog = page.locator('#project-setup-dialog');
  await expect(dialog).toBeVisible();
  await expect(dialog.locator('[data-check-id="execution:worker"]')).toContainText('worker_capability_missing');
  await expect(dialog.locator('[data-check-id="execution:worker"]')).toContainText('Start a qualified worker');

  await dialog.locator('[data-check-id="execution:worker"] [data-setup-route]').click();
  await expect.poll(async () => page.evaluate(() => window.fixture.openedWorkspace)).toBe('workers');
});

test('fresh setup retry moves blocked Project to ready without a hand-authored manifest', async ({ page }) => {
  await page.goto('http://127.0.0.1:18766/tests/browser/project_setup_ui_fixture.html');
  await page.locator('#project-setup-launch').click();
  const dialog = page.locator('#project-setup-dialog');

  await dialog.locator('[data-setup-fresh]').click();
  await expect(page.locator('#project-setup-launch')).toHaveText('Project ready');
  await expect(page.locator('#new-thread')).toBeEnabled();
  await expect(page.locator('#send')).toBeEnabled();

  const calls = await page.evaluate(() => window.fixture.calls);
  expect(calls.some((item) => item.path.endsWith('/fresh-bootstrap') && item.method === 'POST')).toBeTruthy();
});

test('plan/apply uses canonical bootstrap API and normalized resource topology', async ({ page }) => {
  await page.goto('http://127.0.0.1:18766/tests/browser/project_setup_ui_fixture.html');
  await page.locator('#project-setup-launch').click();
  const dialog = page.locator('#project-setup-dialog');

  await dialog.locator('[data-setup-tab="plan"]').click();
  await dialog.locator('[data-setup-plan]').click();
  await expect(dialog.locator('.project-setup-plan-summary')).toContainText('bootstrap-plan-1');
  await expect(dialog.locator('[data-project-setup-manifest]')).toHaveValue(/\\/workspace\\/project-a/);

  await dialog.locator('[data-setup-apply]').click();
  await expect(page.locator('#project-setup-launch')).toHaveText('Project ready');

  const calls = await page.evaluate(() => window.fixture.calls);
  const plan = calls.find((item) => item.path.endsWith('/bootstrap/plan'));
  const apply = calls.find((item) => item.path.endsWith('/bootstrap/apply'));
  expect(JSON.parse(plan.body).manifest.repositories[0].path).toBe('/workspace/project-a');
  expect(JSON.parse(apply.body).expected_plan_id).toBe('bootstrap-plan-1');
});

test('setup UI never renders raw credential-shaped fixture data', async ({ page }) => {
  await page.goto('http://127.0.0.1:18766/tests/browser/project_setup_ui_fixture.html');
  await page.locator('#project-setup-launch').click();

  const text = await page.locator('#project-setup-dialog').innerText();
  expect(text).not.toContain('ULTRA_PRIVATE_VALUE');
  expect(text).not.toContain('xoxb-');
});
