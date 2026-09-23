const { test, expect } = require('@playwright/test');

test('mature canonical cards are adopted out of Developer into first-class workspaces', async ({ page }) => {
  await page.goto('http://127.0.0.1:18766/tests/browser/product_workspaces_fixture.html');

  await expect(page.locator('#product-workspace-switcher')).toHaveCount(1);
  await expect(page.locator('.product-workspaces-launch')).toBeVisible();

  const developerCards = page.locator('#developer-panel .developer-grid > .developer-card');
  await expect(developerCards).toHaveCount(1);
  await expect(developerCards.first()).toContainText('Daemon');

  await expect(page.locator('[data-product-workspace-host="resources"] > #resource-card')).toHaveCount(1);
  await expect(page.locator('[data-product-workspace-host="work"] > #work-card')).toHaveCount(1);
  await expect(page.locator('[data-product-workspace-host="definitions"] > #definition-card')).toHaveCount(1);
  await expect(page.locator('[data-product-workspace-host="organization"] > #identity-card')).toHaveCount(1);
  await expect(page.locator('[data-product-workspace-host="integrations"] > #extension-card')).toHaveCount(1);
  await expect(page.locator('[data-product-workspace-host="agents"] > #model-card')).toHaveCount(1);
  await expect(page.locator('[data-product-workspace-host="workers"] > #worker-card')).toHaveCount(1);
  await expect(page.locator('[data-product-workspace-host="operations"] > #ops-card')).toHaveCount(1);
  await expect(page.locator('[data-product-workspace-host="settings"] > #settings-card')).toHaveCount(1);
  await expect(page.locator('[data-product-workspace-host="autonomy"] > #autonomy-control-center-card')).toHaveCount(1);
});

test('workspace navigation delegates to existing domain launchers instead of duplicating state', async ({ page }) => {
  await page.goto('http://127.0.0.1:18766/tests/browser/product_workspaces_fixture.html');
  await page.locator('.product-workspaces-launch').click();
  const switcher = page.locator('#product-workspace-switcher');
  await expect(switcher).toBeVisible();

  await switcher.locator('[data-product-workspace-nav="goals"]').click();
  await expect(page.locator('#goals-button')).toHaveAttribute('data-clicked', '1');

  await page.locator('.product-workspaces-launch').click();
  await switcher.locator('[data-product-workspace-nav="inbox"]').click();
  await expect(page.locator('[data-attention-launch]')).toHaveAttribute('data-clicked', '1');

  await page.evaluate(() => window.CodexProductUI.openWorkspace('resources'));
  const dialog = page.locator('#product-workspace-dialog');
  await expect(dialog).toBeVisible();
  await expect(dialog.locator('[data-product-workspace-title]')).toHaveText('Resources');
  await expect(dialog.locator('#resource-card')).toBeVisible();
  await expect(page.locator('#refresh-resources')).toHaveAttribute('data-clicked', '1');
});

test('dynamic cards are adopted and shared UI primitives expose distinct canonical concepts', async ({ page }) => {
  await page.goto('http://127.0.0.1:18766/tests/browser/product_workspaces_fixture.html');

  await page.evaluate(() => {
    const card = document.createElement('div');
    card.className = 'developer-card';
    card.id = 'agent-provider-card';
    card.innerHTML = '<h2>Agent Providers & Sessions</h2><button id="refresh-agent-providers">Refresh</button>';
    document.querySelector('#developer-panel .developer-grid').appendChild(card);
  });
  await expect(page.locator('[data-product-workspace-host="operations"] > #agent-provider-card')).toHaveCount(1);

  const api = await page.evaluate(() => ({
    concepts: window.CodexProductUI.concepts.map((item) => item.key),
    stages: window.CodexProductUI.explainStages,
    badge: window.CodexProductUI.statusBadge('quarantined').outerHTML,
    concept: window.CodexProductUI.conceptBadge('compatibility').outerHTML,
  }));
  expect(api.concepts).toEqual([
    'definition', 'policy', 'configuration', 'entitlement',
    'quota', 'compatibility', 'health', 'evidence'
  ]);
  expect(api.stages).toContain('Approval / quorum');
  expect(api.stages).toContain('ActionIntent / provider receipt');
  expect(api.badge).toContain('status-negative');
  expect(api.concept).toContain('concept-compatibility');
});

test('overview Explain Action routes to canonical Autonomy explain UI without model work', async ({ page }) => {
  await page.goto('http://127.0.0.1:18766/tests/browser/product_workspaces_fixture.html');
  await page.evaluate(() => window.CodexProductUI.openWorkspace('overview'));
  const dialog = page.locator('#product-workspace-dialog');
  await dialog.locator('.product-overview-reference > summary').click();
  await dialog.locator('[data-product-explain-id]').fill('action-intent-123');
  await dialog.locator('[data-product-explain]').click();

  await expect(dialog.locator('[data-product-workspace-title]')).toHaveText('Automation / Autonomy');
  await expect(page.locator('[data-acc-intent]')).toHaveValue('action-intent-123');
  await expect(page.locator('[data-acc-explain]')).toHaveAttribute('data-clicked', '1');
});

test('keyboard and hash routing work and phone layout does not exceed viewport', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('http://127.0.0.1:18766/tests/browser/product_workspaces_fixture.html#workspace/resources');

  const dialog = page.locator('#product-workspace-dialog');
  await expect(dialog).toBeVisible();
  await expect(dialog.locator('[data-product-workspace-title]')).toHaveText('Resources');

  await dialog.locator('[data-product-workspace-close]').click();
  await page.keyboard.press('Control+K');
  const switcher = page.locator('#product-workspace-switcher');
  await expect(switcher).toBeVisible();
  await expect(switcher.locator('#product-workspace-search')).toBeFocused();

  const box = await switcher.boundingBox();
  expect(box.width).toBeLessThanOrEqual(390);
  expect(box.x).toBeGreaterThanOrEqual(0);
});


test('shell keeps canonical Project context visible and switches without reloading', async ({ page }) => {
  await page.goto('http://127.0.0.1:18766/tests/browser/product_workspaces_fixture.html');

  const switcher = page.locator('#product-project-switcher');
  await expect(switcher).toBeVisible();
  await expect(switcher.locator('option')).toHaveCount(2);
  await expect(switcher).toHaveValue('home');
  await expect(page.locator('[data-project-indicator]')).toHaveText('Project: Home');

  await switcher.selectOption('alpha');
  await expect.poll(() => page.evaluate(() => window.__selectedProject)).toBe('alpha');
  await expect(page.locator('[data-project-indicator]')).toHaveText('Project: Alpha');
  await expect(page.locator('body')).toHaveAttribute('data-active-project', 'alpha');
});

test('command search filters the workflow navigation and keeps Attention discoverable', async ({ page }) => {
  await page.goto('http://127.0.0.1:18766/tests/browser/product_workspaces_fixture.html');
  await page.keyboard.press('Control+K');

  const switcher = page.locator('#product-workspace-switcher');
  const search = switcher.locator('#product-workspace-search');
  await search.fill('automation');

  await expect(switcher.locator('[data-product-workspace-nav="autonomy"]')).toBeVisible();
  await expect(switcher.locator('[data-product-workspace-nav="work"]')).toBeHidden();

  await search.fill('attention');
  await expect(switcher.locator('[data-product-workspace-nav="inbox"]')).toBeVisible();
});

test('workspace deep links participate in browser back navigation', async ({ page }) => {
  await page.goto('http://127.0.0.1:18766/tests/browser/product_workspaces_fixture.html');
  await page.evaluate(() => window.CodexProductUI.openWorkspace('resources'));
  await expect(page).toHaveURL(/#workspace\/resources$/);

  await page.evaluate(() => window.CodexProductUI.openWorkspace('operations'));
  await expect(page).toHaveURL(/#workspace\/operations$/);
  await expect(page.locator('[data-product-workspace-title]')).toHaveText('Operations / Observability');

  await page.goBack();
  await expect(page).toHaveURL(/#workspace\/resources$/);
  await expect(page.locator('[data-product-workspace-title]')).toHaveText('Resources');
});
