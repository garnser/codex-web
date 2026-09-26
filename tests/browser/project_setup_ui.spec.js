const { test, expect } = require('@playwright/test');

test('clean first-run surface exposes setup immediately without an indefinite loading state', async ({ page }) => {
  await page.goto('http://127.0.0.1:18766/tests/browser/project_setup_ui_fixture.html');

  const launch = page.locator('#project-setup-launch');
  await expect(launch).toBeVisible();
  await expect(launch).toHaveText('Project setup');

  await launch.click();
  const dialog = page.locator('#project-setup-dialog');
  await expect(dialog).toBeVisible();
  await expect(dialog.locator('.project-setup-summary')).toBeVisible();
  await expect(dialog.locator('[role="progressbar"]')).toHaveCount(0);
  await expect(page.locator('#new-thread')).toBeDisabled();
  await expect(page.locator('#send')).toBeDisabled();
});

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
  expect(await dialog.locator('[data-project-setup-manifest]').inputValue()).toContain('/workspace/project-a');

  await dialog.locator('[data-setup-apply]').click();
  await expect(page.locator('#project-setup-launch')).toHaveText('Project ready');

  const calls = await page.evaluate(() => window.fixture.calls);
  const plan = calls.find((item) => item.path.endsWith('/bootstrap/plan'));
  const apply = calls.find((item) => item.path.endsWith('/bootstrap/apply'));
  expect(JSON.parse(plan.body).manifest.repositories[0].path).toBe('/workspace/project-a');
  expect(JSON.parse(apply.body).expected_plan_id).toBe('bootstrap-plan-1');
});

test('plan apply acknowledges once, prevents duplicate submission, and confirms canonical readiness', async ({ page }) => {
  await page.goto('http://127.0.0.1:18766/tests/browser/project_setup_ui_fixture.html?explicit=1');
  await page.locator('#project-setup-launch').click();
  const dialog = page.locator('#project-setup-dialog');
  await dialog.locator('[data-setup-tab="plan"]').click();
  await dialog.locator('[data-setup-plan]').click();
  await dialog.locator('[data-setup-apply]').click();
  await expect(dialog.locator('[data-setup-action-feedback] [data-action-state="succeeded"]')).toContainText('Canonical readiness now reports execution ready');
  expect(await page.evaluate(() => window.fixture.applyCalls)).toBe(1);
});

test('plan apply exposes recovery context when canonical apply fails', async ({ page }) => {
  await page.goto('http://127.0.0.1:18766/tests/browser/project_setup_ui_fixture.html?failApply=1');
  await page.locator('#project-setup-launch').click();
  const dialog = page.locator('#project-setup-dialog');
  await dialog.locator('[data-setup-tab="plan"]').click();
  await dialog.locator('[data-setup-plan]').click();
  await dialog.locator('[data-setup-apply]').click();
  await expect(dialog.locator('[data-setup-action-feedback] [data-action-state="failed"]')).toContainText('worker stopped during apply');
  await expect(dialog.locator('[data-setup-tab="plan"]')).toBeVisible();
});


test('setup UI never renders raw credential-shaped fixture data', async ({ page }) => {
  await page.goto('http://127.0.0.1:18766/tests/browser/project_setup_ui_fixture.html');
  await page.locator('#project-setup-launch').click();

  const text = await page.locator('#project-setup-dialog').innerText();
  expect(text).not.toContain('ULTRA_PRIVATE_VALUE');
  expect(text).not.toContain('xoxb-');
});


test('explicit repository policy is ready while requiring a target per turn', async ({ page }) => {
  await page.goto('http://127.0.0.1:18766/tests/browser/project_setup_ui_fixture.html?explicit=1');

  await expect(page.locator('#project-setup-launch')).toHaveText('Project ready');
  await expect(page.locator('#new-thread')).toBeEnabled();
  await expect(page.locator('#send')).toBeEnabled();

  await page.locator('#project-setup-launch').click();
  const dialog = page.locator('#project-setup-dialog');
  await expect(dialog.locator('.project-setup-hero')).toContainText('Repository target required per turn');
  await expect(dialog.locator('.project-setup-metrics')).toContainText('Explicit per turn');
  await expect(dialog.locator('[data-check-id="repository:execution-target"]')).toContainText('repository_target_required_per_turn');
});
