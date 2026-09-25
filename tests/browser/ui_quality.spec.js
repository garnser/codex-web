const { test, expect } = require('@playwright/test');

async function expectNoPageOverflow(page, label) {
  const metrics = await page.evaluate(() => ({
    innerWidth: window.innerWidth,
    documentWidth: document.documentElement.scrollWidth,
    bodyWidth: document.body.scrollWidth,
  }));
  expect(metrics.documentWidth, `${label}: document overflow`).toBeLessThanOrEqual(metrics.innerWidth + 1);
  expect(metrics.bodyWidth, `${label}: body overflow`).toBeLessThanOrEqual(metrics.innerWidth + 1);
}

async function installWorkItemApis(page) {
  await page.route('**/api/projects', (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify([{
      id: 'project-a',
      name: 'Project A',
      path: '/workspace/project-a',
      authoritative_task_source: {
        source_type: 'gitlab',
        source_instance: 'https://gitlab.example/api/v4',
        scope: 'team/project-a',
      },
    }]),
  }));
  await page.route('**/api/task-sources', (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({ items: [], sync: {} }),
  }));
  await page.route('**/api/secrets', (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({ items: [] }),
  }));
  await page.route('**/api/work-items?**', (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({
      items: [{ ref: 'team/project-a#42', title: 'Accessible work item', current_stage: 'implementation_active' }],
      nextCursor: null,
      hasMore: false,
    }),
  }));
  await page.route('**/api/work-items/**/runs?**', (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({ active: [], items: [], nextCursor: null, hasMore: false }),
  }));
  await page.route('**/api/work-items/**/operator', (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({
      item: {
        ref: 'team/project-a#42',
        title: 'Accessible work item',
        current_stage: 'implementation_active',
        execution: { retry: { policy: {} }, usage: {} },
      },
      external: { configuration: {}, identity: {}, capabilities: [] },
      execution_contract: { permissions: {}, execution_profile: {} },
      execution_policy: {},
      diagnostics: [],
      history: { items: [] },
      actions: { retry: { allowed: false }, reconcile: { allowed: false } },
    }),
  }));
}

for (const width of [390, 768, 1280]) {
  test(`workspace shell remains bounded and keyboard-usable at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 900 });
    await page.goto('http://127.0.0.1:18766/tests/browser/product_workspaces_fixture.html');
    await page.keyboard.press('Control+K');

    const switcher = page.locator('#product-workspace-switcher');
    await expect(switcher).toBeVisible();
    await expect(switcher).toHaveAccessibleName('Navigate');
    await expect(switcher.locator('#product-workspace-search')).toBeFocused();

    await page.keyboard.press('Tab');
    await expect(switcher.locator('[data-product-workspace-nav]').first()).toBeFocused();
    await expectNoPageOverflow(page, `workspace shell at ${width}px`);

    if (width === 390) {
      const smallTargets = await switcher.locator('button:visible').evaluateAll((buttons) => (
        buttons
          .map((button) => {
            const box = button.getBoundingClientRect();
            return { label: button.getAttribute('aria-label') || button.textContent.trim(), width: box.width, height: box.height };
          })
          .filter((item) => item.width < 44 || item.height < 44)
      ));
      expect(smallTargets, 'phone controls must meet the 44px touch-target floor').toEqual([]);
    }
  });
}

test('workspace shell remains usable at 200% text scaling and exposes non-color status semantics', async ({ page }) => {
  await page.setViewportSize({ width: 768, height: 900 });
  await page.goto('http://127.0.0.1:18766/tests/browser/product_workspaces_fixture.html');
  await page.addStyleTag({ content: 'html { font-size: 200% !important; }' });

  await page.evaluate(() => window.CodexProductUI.openWorkspace('overview'));
  const pageSurface = page.locator('#product-workspace-page');
  await expect(pageSurface).toBeVisible();
  await expect(pageSurface).toHaveAccessibleName('Overview');
  await expectNoPageOverflow(page, 'workspace at 200% text scaling');

  await pageSurface.locator('.product-overview-reference > summary').click();
  const negative = pageSurface.locator('.product-status-badge.status-negative');
  await expect(negative).toContainText('blocked');
  const marker = await negative.evaluate((node) => getComputedStyle(node, '::before').content);
  expect(marker).not.toBe('none');
  expect(marker).not.toBe('normal');
});

test('reduced-motion preference suppresses shared animation and transition timing', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('http://127.0.0.1:18766/tests/browser/product_workspaces_fixture.html');
  await page.keyboard.press('Control+K');

  const durations = await page.locator('#product-workspace-switcher button').first().evaluate((node) => {
    const style = getComputedStyle(node);
    return { transition: style.transitionDuration, animation: style.animationDuration };
  });
  const maxSeconds = (value) => Math.max(...value.split(',').map((part) => {
    const token = part.trim();
    if (token.endsWith('ms')) return parseFloat(token) / 1000;
    return parseFloat(token);
  }));
  expect(maxSeconds(durations.transition)).toBeLessThanOrEqual(0.00001);
  expect(maxSeconds(durations.animation)).toBeLessThanOrEqual(0.00001);
});

test('Work Item operator has named dialog/status semantics and remains usable on phone + text scaling', async ({ page }) => {
  await installWorkItemApis(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('http://127.0.0.1:18766/tests/browser/work_items_fixture.html');
  await page.evaluate(() => window.dispatchEvent(new CustomEvent('codex:open-work-items')));

  const dialog = page.locator('#work-items-dialog');
  await expect(dialog).toBeVisible();
  await expect(dialog).toHaveAccessibleName('Work-item operator');
  await expect(dialog.locator('.work-items-status')).toHaveAttribute('role', 'status');
  await expect(dialog.locator('.work-items-status')).toHaveAttribute('aria-live', 'polite');
  await expectNoPageOverflow(page, 'Work Item operator on phone');

  await page.addStyleTag({ content: 'html { font-size: 200% !important; }' });
  await expect(dialog.locator('.work-items-close')).toBeVisible();
  await expect(dialog.locator('.work-items-refresh')).toBeVisible();
  await expectNoPageOverflow(page, 'Work Item operator at 200% text scaling');

  await dialog.locator('.work-items-project').focus();
  await page.keyboard.press('Tab');
  await expect(dialog.locator('.work-items-search')).toBeFocused();
  await page.keyboard.press('Tab');
  await expect(dialog.locator('.work-items-refresh')).toBeFocused();
  const focusStyle = await dialog.locator('.work-items-refresh').evaluate((node) => {
    const style = getComputedStyle(node);
    return { outlineStyle: style.outlineStyle, outlineWidth: style.outlineWidth };
  });
  expect(focusStyle.outlineStyle).not.toBe('none');
  expect(parseFloat(focusStyle.outlineWidth)).toBeGreaterThan(0);
});

test('wide workspace uses available content width without wrapping compact controls', async ({ page }) => {
  await page.setViewportSize({ width: 1600, height: 1000 });
  await page.goto('http://127.0.0.1:18766/tests/browser/product_workspaces_fixture.html');
  await page.evaluate(() => window.CodexProductUI.openWorkspace('overview'));

  const panel = page.locator('.product-workspace-panel:visible');
  const panelBox = await panel.boundingBox();
  expect(panelBox.width).toBeGreaterThan(900);

  const headerButtons = page.locator('.product-workspace-header-actions button:visible');
  const wrapped = await headerButtons.evaluateAll((buttons) => buttons.filter((button) => button.scrollHeight > button.clientHeight + 1).map((button) => button.textContent.trim()));
  expect(wrapped).toEqual([]);

  const cards = page.locator('.product-overview-grid > *:visible');
  expect(await cards.count()).toBeGreaterThan(1);
  await expectNoPageOverflow(page, 'wide workspace density');
});
