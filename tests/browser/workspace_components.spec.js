const { test, expect } = require('@playwright/test');

test('shared workspace components expose consistent semantic state and identity treatments', async ({ page }) => {
  await page.goto('http://127.0.0.1:18766/tests/browser/workspace_components_fixture.html');

  await expect(page.locator('.cw-status-badge')).toHaveCount(8);
  await expect(page.locator('[data-status="healthy"]').first()).toHaveClass(/cw-status-positive/);
  await expect(page.locator('[data-status="degraded"]')).toHaveClass(/cw-status-warning/);
  await expect(page.locator('[data-status="failed"]')).toHaveClass(/cw-status-negative/);
  await expect(page.locator('[data-status="custom"]')).toHaveClass(/cw-status-neutral/);

  await expect(page.locator('[data-identity-kind="agent"]').first()).toContainText('Build agent');
  await expect(page.locator('[data-identity-kind="person"]').first()).toContainText('Alice');
  await expect(page.locator('[data-identity-kind="team"]')).toContainText('Platform');
});

test('loading empty degraded error and offline states retain explicit semantics', async ({ page }) => {
  await page.goto('http://127.0.0.1:18766/tests/browser/workspace_components_fixture.html');

  await expect(page.locator('[data-state="loading"]')).toHaveAttribute('aria-busy', 'true');
  await expect(page.locator('[data-state="empty"]')).toContainText('No runs');
  await expect(page.locator('[data-state="degraded"]')).toContainText('Provider degraded');
  await expect(page.locator('[data-state="error"]')).toHaveAttribute('role', 'alert');
  await expect(page.locator('[data-state="offline"]')).toHaveAttribute('role', 'alert');
  await expect(page.locator('.cw-skeleton')).toHaveAttribute('aria-busy', 'true');
});

test('action feedback exposes accessible lifecycle states without inventing progress', async ({ page }) => {
  await page.goto('http://127.0.0.1:18766/tests/browser/workspace_components_fixture.html');

  await expect(page.locator('[data-action-state="acknowledged"]')).toHaveAttribute('role', 'status');
  await expect(page.locator('[data-action-state="in_progress"]')).toContainText('Applying reviewed plan');
  await expect(page.locator('[data-action-state="succeeded"]')).toContainText('Plan applied');
  await expect(page.locator('[data-action-state="failed"]')).toHaveAttribute('role', 'alert');
  await expect(page.locator('[data-action-state="needs_attention"]')).toContainText('Approval required');
  await expect(page.locator('[role="progressbar"]')).toHaveCount(0);
});


test('interactive shared primitives remain keyboard focusable', async ({ page }) => {
  await page.goto('http://127.0.0.1:18766/tests/browser/workspace_components_fixture.html');

  await page.keyboard.press('Tab');
  await expect(page.getByRole('button', { name: 'Open' })).toBeFocused();
  await page.keyboard.press('Tab');
  await expect(page.getByRole('button', { name: 'Retry' })).toBeFocused();
  await page.keyboard.press('Tab');
  await expect(page.getByText(/Provenance · 2 stages/)).toBeFocused();
});

test('shared components collapse intentionally at phone width', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('http://127.0.0.1:18766/tests/browser/workspace_components_fixture.html');

  const bodyWidth = await page.evaluate(() => document.documentElement.scrollWidth);
  expect(bodyWidth).toBeLessThanOrEqual(390);

  const grid = page.locator('.cw-metadata-grid').first();
  const columns = await grid.evaluate((node) => getComputedStyle(node).gridTemplateColumns.split(' ').length);
  expect(columns).toBe(1);
});
