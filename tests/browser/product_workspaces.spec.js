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
  await page.evaluate(() => {
    window.__workItemsOpenEvents = 0;
    window.addEventListener('codex:open-work-items', () => { window.__workItemsOpenEvents += 1; });
  });
  await page.locator('.product-workspaces-launch').click();
  const switcher = page.locator('#product-workspace-switcher');
  await expect(switcher).toBeVisible();

  await switcher.locator('[data-product-workspace-nav="goals"]').click();
  await expect(page.locator('#goals-button')).toHaveAttribute('data-clicked', '1');

  await page.locator('.product-workspaces-launch').click();
  await switcher.locator('[data-product-workspace-nav="inbox"]').click();
  await expect(page.locator('[data-attention-launch]')).toHaveAttribute('data-clicked', '1');

  await page.evaluate(() => window.CodexProductUI.openWorkspace('work'));
  const workDialog = page.locator('#product-workspace-dialog');
  await expect(workDialog.locator('[data-product-workspace-actions] button', { hasText: 'Work Items' })).toHaveCount(1);
  await workDialog.locator('[data-product-workspace-actions] button', { hasText: 'Work Items' }).click();
  await expect.poll(() => page.evaluate(() => window.__workItemsOpenEvents)).toBe(1);

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


test('project navigation is hierarchical, keeps active state, and separates Administration', async ({ page }) => {
  await page.goto('http://127.0.0.1:18766/tests/browser/product_workspaces_fixture.html');

  const tree = page.locator('.product-project-nav-tree');
  await expect(tree).toBeVisible();
  await expect(tree).toHaveAttribute('aria-label', 'Project navigation');

  await expect(tree.locator('[data-project-nav-group="work-group"] > summary')).toHaveText('Work');
  await expect(tree.locator('[data-project-nav-group="agents-group"] > summary')).toHaveText('Agents');
  await expect(tree.locator('[data-project-nav-group="automation-group"] > summary')).toHaveText('Automation');
  await expect(tree.locator('[data-project-nav-group="operations-group"] > summary')).toHaveText('Operations');

  const chat = tree.locator('[data-project-nav-node="chat"]');
  await chat.focus();
  await expect(chat).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(chat).toHaveAttribute('aria-current', 'page');
  await expect(page).toHaveURL(/#workspace\/threads$/);

  const automationGroup = tree.locator('[data-project-nav-group="automation-group"]');
  await automationGroup.locator('summary').focus();
  await page.keyboard.press('Enter');
  await expect(automationGroup).toHaveAttribute('open', '');

  const global = page.locator('.product-global-nav');
  await expect(global).toHaveAttribute('aria-label', 'Global navigation');
  await expect(global.locator('[data-project-nav-node="administration"]')).toHaveText('Administration');
  await expect(tree.locator('[data-project-nav-node="administration"]')).toHaveCount(0);
});

test('hierarchical navigation remains usable at phone width', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('http://127.0.0.1:18766/tests/browser/product_workspaces_fixture.html');

  await page.locator('body').evaluate((body) => body.classList.add('mobile-nav-open'));
  await expect(page.locator('.sidebar')).toHaveCSS('transform', 'matrix(1, 0, 0, 1, 0, 0)');
  const navigation = page.locator('.product-project-navigation');
  await expect(navigation).toBeVisible();
  const box = await navigation.boundingBox();
  expect(box.x).toBeGreaterThanOrEqual(0);
  expect(box.x + box.width).toBeLessThanOrEqual(390);

  const attention = navigation.locator('[data-project-nav-node="attention"]');
  await attention.focus();
  await expect(attention).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(page.locator('[data-attention-launch]')).toHaveAttribute('data-clicked', '1');
});


test('major Project destinations explain purpose and scope without hover', async ({ page }) => {
  await page.goto('http://127.0.0.1:18766/tests/browser/product_workspaces_fixture.html');

  await expect(page.locator('.product-field-help')).toContainText('Project scope');
  const runs = page.locator('[data-project-nav-node="runs"]');
  await expect(runs.locator('.product-project-nav-help')).toContainText('What executed');
  const runtime = page.locator('[data-project-nav-node="runtime"]');
  await expect(runtime.locator('.product-project-nav-help')).toContainText('executions run');

  await page.evaluate(() => window.CodexProductUI.openWorkspace('overview'));
  const dialog = page.locator('#product-workspace-dialog');
  await expect(dialog.locator('[data-product-workspace-title]')).toHaveText('Overview');
  await expect(dialog.locator('[data-product-workspace-description]')).toContainText('current work');
  await expect(dialog.locator('[data-project-page-scope]')).toHaveText('Scope: Project');
});

test('routed pages use page-specific purpose text for shared workspace surfaces', async ({ page }) => {
  const fs = require('fs');
  const path = require('path');
  const html = fs.readFileSync(path.join(__dirname, 'product_workspaces_fixture.html'), 'utf8');
  await page.route('**/projects/**', async (route) => {
    if (route.request().resourceType() !== 'document') return route.continue();
    await route.fulfill({ status: 200, contentType: 'text/html', body: html });
  });

  await page.goto('http://127.0.0.1:18766/projects/home/runs');

  const dialog = page.locator('#product-workspace-dialog');
  await expect(dialog.locator('[data-product-workspace-title]')).toHaveText('Runs / Execution');
  await expect(dialog.locator('[data-product-workspace-description]')).toContainText('what agents and workers actually executed');
  await expect(dialog.locator('[data-project-page-scope]')).toHaveText('Scope: Project execution');
  await expect(page.locator('[data-project-nav-node="runs"]')).toHaveAttribute('aria-current', 'page');
  await expect(page.locator('[data-project-nav-node="work-items"]')).toHaveAttribute('aria-current', 'false');
});
