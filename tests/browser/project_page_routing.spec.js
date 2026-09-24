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
  await expect(page.locator('[data-product-workspace-title]')).toHaveText('Agents');

  await page.reload();

  await expect(page).toHaveURL(/\/projects\/alpha\/agents$/);
  await expect(page.locator('#product-project-switcher')).toHaveValue('alpha');
  await expect(page.locator('[data-product-workspace-title]')).toHaveText('Agents');
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
  await expect(page.locator('[data-product-workspace-title]')).toHaveText('Automations');
});

test('Project switch preserves routed page instead of falling back to Chat', async ({ page }) => {
  await serveProjectShell(page);
  await page.goto('http://127.0.0.1:18766/projects/home/operations');

  await page.locator('#product-project-switcher').selectOption('alpha');

  await expect(page).toHaveURL(/\/projects\/alpha\/operations$/);
  await expect(page.locator('body')).toHaveAttribute('data-active-project', 'alpha');
});


test('stale Project deep link is rejected before page-specific state is fetched', async ({ page }) => {
  await page.goto('http://127.0.0.1:18766/tests/browser/product_workspaces_fixture.html');

  const result = await page.evaluate(async () => {
    const { loadProjectUiState } = await import('/static/project_ui_state.js');
    const calls = [];
    const api = async (requestPath) => {
      calls.push(requestPath);
      if (requestPath === '/api/projects') {
        return [{ id: 'home', name: 'Home', path: '/workspace/home' }];
      }
      throw new Error(`unexpected page-data request: ${requestPath}`);
    };
    try {
      await loadProjectUiState({
        api,
        projectId: 'deleted-project',
        projects: [],
        reloadProjects: true,
      });
      return { name: 'no-error', calls };
    } catch (error) {
      return { name: error.name, projectId: error.projectId, calls };
    }
  });

  expect(result.name).toBe('ProjectContextUnavailableError');
  expect(result.projectId).toBe('deleted-project');
  expect(result.calls).toEqual(['/api/projects']);
});

test('stale Project state disables scoped actions and recovers through explicit Project selection', async ({ page }) => {
  await page.goto('http://127.0.0.1:18766/tests/browser/product_workspaces_fixture.html');

  await page.evaluate(() => {
    window.dispatchEvent(new CustomEvent('codex:project-context-unavailable', {
      detail: {
        projectId: 'deleted-project',
        projects: [{ id: 'home', name: 'Home', path: '/workspace/home' }],
      },
    }));
  });

  const status = page.locator('[data-project-context-state]');
  await expect(status).toBeVisible();
  await expect(status).toContainText('unavailable');
  await expect(page.locator('body')).toHaveClass(/project-context-unavailable/);
  await expect(page.locator('[data-project-nav-node="overview"]')).toBeDisabled();
  await expect(page.locator('#product-project-switcher')).toHaveValue('');

  await page.locator('#product-project-switcher').selectOption('home');

  await expect(status).toBeHidden();
  await expect(page.locator('body')).not.toHaveClass(/project-context-unavailable/);
  await expect(page.locator('body')).toHaveAttribute('data-active-project', 'home');
  await expect(page.locator('[data-project-nav-node="overview"]')).toBeEnabled();
});


async function serveLegacyRootShell(page) {
  await page.route('http://127.0.0.1:18766/', async (route) => {
    if (route.request().resourceType() !== 'document') return route.continue();
    await route.fulfill({ status: 200, contentType: 'text/html', body: fixtureHtml });
  });
}

test('legacy Project entry migrates to Overview and does not mount Chat as the page', async ({ page }) => {
  await serveLegacyRootShell(page);

  await page.goto('http://127.0.0.1:18766/');

  await expect(page).toHaveURL(/\/projects\/home\/overview$/);
  await expect(page.locator('body')).toHaveClass(/product-routed-project-page/);
  await expect(page.locator('body')).not.toHaveClass(/product-chat-page/);
  await expect(page.locator('.main')).toBeHidden();
  await expect(page.locator('#product-workspace-dialog')).toBeVisible();
  await expect(page.locator('[data-product-workspace-title]')).toHaveText('Overview');
});

test('legacy Chat hash migrates to routed Chat and retires the Developer control surface', async ({ page }) => {
  await serveLegacyRootShell(page);

  await page.goto('http://127.0.0.1:18766/#workspace/threads');

  await expect(page).toHaveURL(/\/projects\/home\/chat$/);
  await expect(page.locator('body')).toHaveClass(/product-chat-page/);
  await expect(page.locator('.main')).toBeVisible();
  await expect(page.locator('#developer-panel')).toBeHidden();
  await expect(page.locator('#developer-panel')).toHaveAttribute('data-product-compatibility-source', 'true');
  await expect(page.locator('#developer-panel').locator('xpath=..')).toHaveJSProperty('tagName', 'BODY');
  await expect(page.locator('#repository-target')).toBeVisible();
  await expect(page.locator('#sandbox')).toBeVisible();
  await expect(page.locator('#approval-policy')).toBeVisible();
  await expect(page.locator('#rename-thread')).toBeVisible();
  await expect(page.locator('#archive-thread')).toBeVisible();
});

test('primary Project navigation tree exposes every migration destination', async ({ page }) => {
  await page.goto('http://127.0.0.1:18766/tests/browser/product_workspaces_fixture.html');

  const expected = [
    'overview',
    'work-items',
    'runs',
    'chat',
    'goals',
    'decisions',
    'agent-profiles',
    'teams',
    'skills',
    'automations',
    'integrations',
    'attention',
    'runtime',
    'providers',
    'incidents',
    'project-settings',
  ];
  for (const id of expected) {
    await expect(page.locator(`[data-project-nav-node="${id}"]`)).toHaveCount(1);
  }
  await expect(page.locator('[data-project-nav-node="administration"]')).toHaveCount(1);
});
