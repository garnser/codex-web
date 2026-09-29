const { test, expect } = require('@playwright/test');
const fixture = 'http://127.0.0.1:18766/tests/browser/product_workspaces_fixture.html';

async function automationEditor(page, { rejectSave = false } = {}) {
  const item = { id: 'daily', definition: { name: 'Daily review', instructions: 'Review changes',
    lifecycle: 'paused', trigger: { type: 'manual' }, target: { kind: 'agent_profile', id: 'reviewer' } },
  definitionRef: { record_id: 'original', revision: 1 } };
  const writes = [];
  await page.route('**/api/**', async route => {
    const req = route.request();
    const url = new URL(req.url());
    if (req.method() === 'POST' && url.pathname.startsWith('/api/automations/drafts')) {
      writes.push(JSON.parse(req.postData()));
      await route.fulfill(rejectSave ? { status: 422, json: { detail: 'Target is unavailable' } }
        : { json: { record: { record_id: 'new-draft' } } });
    } else await route.fulfill({ json: { items: url.pathname === '/api/automations' ? [item] : [] } });
  });
  await page.goto(fixture);
  await page.evaluate(() => window.CodexProductUI.openWorkspace('autonomy'));
  await page.locator('[data-automation-edit]').click();
  return { form: page.locator('[data-automation-editor]'), writes };
}

test('failed Automation save preserves edits, announces error and stays dirty', async ({ page }) => {
  const { form, writes } = await automationEditor(page, { rejectSave: true });
  await form.locator('[name=name]').fill('Unsaved revised name');
  await form.locator('button[type=submit]').click();
  await expect(page.locator('[data-automation-editor-error]')).toHaveText('Target is unavailable');
  await expect(form.locator('[name=name]')).toHaveValue('Unsaved revised name');
  await expect(form.locator('[data-dirty-editor-status]')).toHaveText('Unsaved changes');
  expect(writes).toHaveLength(1);
  page.once('dialog', dialog => dialog.dismiss());
  await form.locator('[data-automation-edit-cancel]').click();
  await expect(form).toBeVisible();
  page.once('dialog', dialog => dialog.accept());
  await form.locator('[data-automation-edit-cancel]').click();
  await expect(form).toHaveCount(0);
});

test('workspace and Project navigation require discard and cancelled history restores the editor URL', async ({ page }) => {
  const { form } = await automationEditor(page);
  await form.locator('[name=name]').fill('Keep this work');
  const location = page.url();
  page.once('dialog', dialog => dialog.dismiss());
  await page.evaluate(() => window.CodexProductUI.openWorkspace('resources'));
  await expect(form).toBeVisible();
  page.once('dialog', dialog => dialog.dismiss());
  await page.locator('#product-project-switcher').selectOption('alpha');
  await expect(page.locator('#product-project-switcher')).toHaveValue('home');
  await expect(form.locator('[name=name]')).toHaveValue('Keep this work');
  page.once('dialog', dialog => dialog.dismiss());
  await page.evaluate(() => { location.hash = '#workspace/resources'; });
  await expect(page).toHaveURL(location);
  await expect(form).toBeVisible();
  page.once('dialog', dialog => dialog.accept());
  await page.locator('#product-project-switcher').selectOption('alpha');
  await expect(form).toHaveCount(0);
  await expect(page.locator('#product-project-switcher')).toHaveValue('alpha');
});

test('successful save clears protection and unchanged editors close without warning', async ({ page }) => {
  const { form, writes } = await automationEditor(page);
  await expect(form.locator('[data-dirty-editor-status]')).toHaveText('No unsaved changes');
  await form.locator('[data-automation-edit-cancel]').click();
  await expect(form).toHaveCount(0);
  await page.locator('[data-automation-edit]').click();
  await form.locator('[name=name]').fill('Saved revision');
  await form.locator('button[type=submit]').click();
  await expect(form).toHaveCount(0);
  expect(writes).toHaveLength(2);
  await page.evaluate(() => window.CodexProductUI.openWorkspace('resources'));
  await expect(page.locator('[data-product-workspace-title]')).toHaveText('Resources');
});

test('discarding an Automation during draft creation fences the later publish', async ({ page }) => {
  const { form } = await automationEditor(page);
  let release;
  let pending = false;
  const held = new Promise(resolve => { release = resolve; });
  const publications = [];
  await page.route('**/api/automations/drafts**', async route => {
    if (route.request().url().endsWith('/publish')) {
      publications.push(route.request().url());
      return route.fulfill({ json: {} });
    }
    pending = true;
    await held;
    await route.fulfill({ json: { record: { record_id: 'late-draft' } } });
  });
  await form.locator('[name=name]').fill('Discard pending edit');
  await form.locator('button[type=submit]').click();
  await expect.poll(() => pending).toBe(true);
  await expect(form.locator('[name=name]')).toBeDisabled();
  page.once('dialog', dialog => dialog.accept());
  await form.locator('[data-automation-edit-cancel]').click();
  await expect(form).toHaveCount(0);
  const response = page.waitForResponse('**/api/automations/drafts');
  release();
  await response;
  await page.locator('[data-automation-edit]').click();
  await expect(form.locator('[name=name]')).toHaveValue('Daily review');
  expect(publications).toEqual([]);
});

test('Back navigation keeps the editor and Project navigator declines an unsaved switch', async ({ page }) => {
  const { form } = await automationEditor(page);
  await page.evaluate(async () => {
    const { createProjectNavigator } = await import('/static/project_context.js');
    window.projectState = { projectId: 'home' };
    window.selectTestProject = createProjectNavigator(window.projectState);
  });
  const url = page.url();
  await form.locator('[name=name]').fill('Keep on Back');
  page.once('dialog', dialog => dialog.dismiss());
  await page.evaluate(() => history.back());
  await expect(page).toHaveURL(url);
  await expect(form.locator('[name=name]')).toHaveValue('Keep on Back');
  page.once('dialog', dialog => dialog.dismiss());
  await page.evaluate(() => window.selectTestProject('alpha'));
  expect(await page.evaluate(() => window.projectState.projectId)).toBe('home');
});

test('Agent and Team editors protect Escape, retain invalid payload, and keep drafts out of storage', async ({ page }) => {
  await page.goto(fixture);
  for (const kind of ['profile', 'team']) {
    await page.evaluate(async kind => {
      const { openEditor } = await import('/static/collaboration_management.js');
      openEditor(kind);
    }, kind);
    const dialog = page.locator('dialog:modal');
    await dialog.locator('[data-payload]').fill('draft-private-marker');
    await dialog.locator('[data-save]').click();
    await expect(dialog.locator('[data-status]')).toContainText('JSON is invalid');
    await expect(dialog.locator('[data-payload]')).toHaveValue('draft-private-marker');
    expect(await page.evaluate(() => JSON.stringify([Object.entries(localStorage), Object.entries(sessionStorage)]))).not.toContain('draft-private-marker');
    expect(await page.evaluate(() => {
      const event = new Event('beforeunload', { cancelable: true });
      window.dispatchEvent(event);
      return event.defaultPrevented;
    })).toBe(true);
    page.once('dialog', dialog => dialog.dismiss());
    await page.keyboard.press('Escape');
    await expect(dialog).toBeVisible();
    page.once('dialog', dialog => dialog.accept());
    await dialog.locator('[data-close]').click();
    await expect(dialog).toHaveCount(0);
  }
});
