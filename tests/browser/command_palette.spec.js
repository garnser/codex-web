const { test, expect } = require('@playwright/test');
const fixture = 'http://127.0.0.1:18766/tests/browser/product_workspaces_fixture.html';
const palette = '#product-workspace-switcher';
const search = '#product-workspace-search';

test('keyboard search, arrow selection, execution, recent destinations and focus restoration', async ({ page }) => {
  await page.goto(fixture);
  const entry = page.locator('.product-all-workspaces');
  await entry.focus();
  await page.keyboard.press('Control+K');
  await expect(page.locator(search)).toBeFocused();
  await page.locator(search).fill('Goals');
  await page.keyboard.press('ArrowDown');
  await expect(page.locator(search)).toHaveAttribute('aria-activedescendant', 'product-command-0');
  await page.keyboard.press('Enter');
  await expect(page.locator(palette)).not.toBeVisible();
  await expect(page.locator('[data-product-workspace-title]')).toHaveText('Goals');
  await entry.click();
  await expect(page.locator(search)).toBeFocused();
  await expect(page.locator('#product-command-results button').first()).toContainText('Goals');
  await expect(page.locator('#product-command-results button').first()).toContainText('Recent');
  await page.keyboard.press('ArrowDown');
  await expect(page.locator(search)).toHaveAttribute('aria-activedescendant', 'product-command-1');
  await page.keyboard.press('ArrowUp');
  await expect(page.locator(search)).toHaveAttribute('aria-activedescendant', 'product-command-0');
  await page.keyboard.press('Escape');
  await expect(entry).toBeFocused();
});

test('Project switching is explicit and loaded objects stay in their Project across rapid switches', async ({ page }) => {
  await page.goto(fixture);
  await page.evaluate(async () => {
    const { installThreadCommands, installWorkItemCommands } = await import('/static/object_commands.js');
    window.commandState = { projectId: 'home', threadIndexProject: 'home', threads: [
      { id: 'a', name: 'Alpha conversation', projectId: 'home' },
      { id: 'b', name: 'Foreign conversation', projectId: 'alpha' },
    ], items: [
      { ref: 'repo#1', title: 'Loaded Work Item', project_id: 'home' },
      { ref: 'repo#2', title: 'Foreign Work Item', project_id: 'alpha' },
    ] };
    window.openedThreads = [];
    installThreadCommands(window.commandState, (id) => window.openedThreads.push(id));
    installWorkItemCommands(window.commandState, () => {});
  });
  await page.keyboard.press('Control+K');
  await page.locator(search).fill('Foreign');
  await expect(page.locator('[data-command-status]')).toContainText('No matching');
  await page.locator(search).fill('Loaded Work Item');
  await expect(page.locator('#product-command-results')).toContainText('Project: Home');
  await page.locator(search).fill('Alpha conversation');
  await page.keyboard.press('Enter');
  await expect.poll(() => page.evaluate(() => window.openedThreads)).toEqual(['a']);
  await page.keyboard.press('Control+K');
  await page.locator(search).fill('Switch to Project: Alpha');
  await expect(page.locator('#product-command-results')).toContainText('Changes active Project');
  await page.keyboard.press('Enter');
  await expect(page.locator('#product-project-switcher')).toHaveValue('alpha');
  await page.keyboard.press('Control+K');
  await page.locator(search).fill('Alpha conversation');
  await expect(page.locator('[data-command-status]')).toContainText('No matching');
  await page.locator(search).fill('Loaded Work Item');
  await expect(page.locator('[data-command-status]')).toContainText('No matching');
  await page.locator(search).fill('Switch to Project: Home');
  await page.keyboard.press('Enter');
  await page.keyboard.press('Control+K');
  await page.locator(search).fill('Alpha conversation');
  await expect(page.locator('[data-command-status]')).toContainText('No matching');
});

test('unavailable commands are excluded and execution rechecks changed availability', async ({ page }) => {
  await page.goto(fixture);
  await page.evaluate(async () => {
    document.querySelector('[data-project-nav-node="administration"]').hidden = true;
    document.querySelector('#goals-button').disabled = true;
    const { registerCommandSource } = await import('/static/command_palette.js');
    window.commandAllowed = true;
    window.commandExecutions = 0;
    registerCommandSource('test', () => [{ id: 'safe', label: 'Available action',
      available: () => window.commandAllowed, run: () => window.commandExecutions++ }]);
  });
  await page.keyboard.press('Control+K');
  await page.locator(search).fill('Administration');
  await expect(page.locator('[data-command-status]')).toContainText('No matching');
  await page.locator(search).fill('Goals');
  await expect(page.locator('[data-command-status]')).toContainText('No matching');
  await page.locator(search).fill('Available action');
  await expect(page.locator('#product-command-results button')).toHaveCount(1);
  await page.evaluate(() => { window.commandAllowed = false; });
  await page.keyboard.press('Enter');
  await expect(page.locator('[data-command-status]')).toContainText('No matching');
  expect(await page.evaluate(() => window.commandExecutions)).toBe(0);
});

test('empty Project scope, no results, modal boundaries and phone width remain usable', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(fixture);
  await page.evaluate(() => window.dispatchEvent(new CustomEvent('codex:project-context-unavailable', {
    detail: { projectId: 'removed', projects: [] },
  })));
  await page.keyboard.press('Control+K');
  await page.locator(search).fill('Work Item');
  await expect(page.locator('[data-command-status]')).toContainText('No matching');
  await page.keyboard.press('Enter');
  await expect(page.locator(palette)).toBeVisible();
  const box = await page.locator(palette).boundingBox();
  expect(box.x).toBeGreaterThanOrEqual(0);
  expect(box.width).toBeLessThanOrEqual(390);
  await page.keyboard.press('Escape');
  await page.evaluate(() => {
    const dialog = document.createElement('dialog');
    dialog.id = 'other-dialog';
    dialog.innerHTML = '<button>Confirm</button>';
    document.body.append(dialog);
    dialog.showModal();
  });
  await page.keyboard.press('Control+K');
  await expect(page.locator(palette)).not.toBeVisible();
  await expect(page.locator('#other-dialog button')).toBeFocused();
});
