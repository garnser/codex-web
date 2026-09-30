const fs = require('fs');
const path = require('path');
const { test, expect } = require('@playwright/test');
const root = path.join(__dirname, '../..');
const index = fs.readFileSync(path.join(root, 'static/index.html'), 'utf8');
const start = index.lastIndexOf('<div class="developer-card">', index.indexOf('<h2>Resource Catalog</h2>'));
const end = index.indexOf('<div class="developer-card">', start + 1);
const card = index.slice(start, end);
const fixture = fs.readFileSync(path.join(__dirname, 'product_workspaces_fixture.html'), 'utf8')
  .replace(/<div class="developer-card" id="resource-card">.*?<\/div>/, card)
  .replace('</body>', '<script type="module" src="/static/resource_catalog_admin.js"></script><script type="module" src="/static/resource_catalog_management.js"></script></body>');
const url = 'http://127.0.0.1:18766/tests/browser/resource_editor_fixture.html';

async function setup(page) {
  const state = { failRead: false, failWrite: false, writes: [], holdWrite: null };
  state.resources = ['one', 'two'].map(id => ({ id, name: `Resource ${id}`, resource_type: 'service', lifecycle: 'active', risk: 'low', sensitivity: 'internal', owner_identity_id: 'owner', aliases: [] }));
  await page.route(url, route => route.fulfill({ contentType: 'text/html', body: fixture }));
  await page.route('**/api/**', async route => {
    const pathname = new URL(route.request().url()).pathname;
    if (pathname === '/api/identity/me') return route.fulfill({ json: { organization_id: 'org', workspace_id: 'ws' } });
    if (pathname === '/api/identity') return route.fulfill({ json: { humans: [{ id: 'owner', display_name: 'Owner' }], memberships: [{ identity_id: 'owner', organization_id: 'org', workspace_id: 'ws' }] } });
    if (pathname.startsWith('/api/resources')) {
      if (route.request().method() !== 'GET') {
        state.writes.push(route.request().postDataJSON());
        if (state.holdWrite) await state.holdWrite;
        if (state.failWrite) return route.fulfill({ status: 409, json: { detail: 'Canonical update conflict' } });
        const id = pathname.split('/').pop();
        state.resources = state.resources.map(item => item.id === id ? { ...item, ...state.writes.at(-1) } : item);
        return route.fulfill({ json: {} });
      }
      return route.fulfill(state.failRead ? { status: 503, json: { detail: 'Catalog offline' } } : { json: { items: state.resources } });
    }
    return route.fulfill({ json: {} });
  });
  await page.goto(`${url}#workspace/resources`);
  await expect(page.locator('[data-resource-editor]')).toHaveCount(2);
  const editor = page.locator('[data-resource-editor][data-resource-id="one"]');
  await editor.locator('summary').click();
  return { state, editor };
}

test('resource edits survive refresh, filtering and read failures; explicit discard applies latest catalog', async ({ page }) => {
  const { state, editor } = await setup(page);
  await editor.locator('[data-resource-edit-name]').fill('Unsaved resource');
  state.resources[0].name = 'Latest canonical name';
  await page.locator('#refresh-resources').click();
  await expect(page.locator('#resource-catalog-status')).toContainText('deferred');
  await page.locator('#resource-search').fill('no match');
  await expect(editor.locator('[data-resource-edit-name]')).toHaveValue('Unsaved resource');
  state.failRead = true;
  await page.locator('#refresh-resources').click();
  await expect(page.locator('#resource-catalog-status')).toContainText('Unsaved resource edits are preserved');
  await expect(editor.locator('[data-dirty-editor-status]')).toHaveText('Unsaved changes');
  state.failRead = false;
  await page.locator('#resource-search').fill('');
  page.once('dialog', dialog => dialog.dismiss());
  await editor.locator('[data-resource-discard]').click();
  await expect(editor.locator('[data-resource-edit-name]')).toHaveValue('Unsaved resource');
  page.once('dialog', dialog => dialog.accept());
  await editor.locator('[data-resource-discard]').click();
  await expect(editor.locator('[data-resource-edit-name]')).toHaveValue('Latest canonical name');
  expect(state.writes).toEqual([]);
});

test('resource edits protect workspace, Project and history navigation without browser draft storage', async ({ page }) => {
  const { editor } = await setup(page);
  await editor.locator('[data-resource-edit-description]').fill('Private draft metadata');
  page.once('dialog', dialog => dialog.dismiss());
  await page.evaluate(() => window.CodexProductUI.openWorkspace('definitions'));
  await expect(page.locator('[data-product-workspace-title]')).toHaveText('Resources');
  page.once('dialog', dialog => dialog.dismiss());
  await page.locator('#product-project-switcher').selectOption('alpha');
  await expect(page.locator('#product-project-switcher')).toHaveValue('home');
  const location = page.url();
  page.once('dialog', dialog => dialog.dismiss());
  await page.evaluate(() => { location.hash = '#workspace/definitions'; });
  await expect(page).toHaveURL(location);
  await expect(editor.locator('[data-resource-edit-description]')).toHaveValue('Private draft metadata');
  expect(await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage }))).not.toContain('Private draft metadata');
});

test('failed save retains draft and a late successful save does not erase newer typing', async ({ page }) => {
  const { state, editor } = await setup(page);
  const name = editor.locator('[data-resource-edit-name]');
  await name.fill('Submitted value');
  state.failWrite = true;
  page.once('dialog', dialog => dialog.accept());
  await editor.locator('[data-resource-save]').click();
  await expect(page.locator('#resource-management-status')).toContainText('conflict');
  await expect(name).toHaveValue('Submitted value');
  await expect(editor.locator('[data-dirty-editor-status]')).toHaveText('Unsaved changes');
  state.failWrite = false;
  let release;
  state.holdWrite = new Promise(resolve => { release = resolve; });
  page.once('dialog', dialog => dialog.accept());
  await editor.locator('[data-resource-save]').click();
  await expect.poll(() => state.writes.length).toBe(2);
  await name.fill('Newer draft');
  release();
  await expect(page.locator('#resource-management-status')).toContainText('Updated one');
  await expect(page.locator('#resource-catalog-status')).toContainText('deferred');
  await expect(name).toHaveValue('Newer draft');
  await expect(editor.locator('[data-dirty-editor-status]')).toHaveText('Unsaved changes');
  expect(state.writes[1].name).toBe('Submitted value');
});

test('create and relationship drafts retain selected values on catalog hydration', async ({ page }) => {
  const { state } = await setup(page);
  const create = page.locator('#resource-create-panel');
  await create.locator('summary').click();
  await page.locator('#resource-create-name').fill('New service');
  await page.locator('#resource-create-owner').selectOption('owner');
  const relationship = page.locator('#resource-relationship-panel');
  await relationship.locator('summary').click();
  await page.locator('#resource-relationship-from').selectOption('one');
  await page.locator('#resource-relationship-to').selectOption('two');
  await page.locator('#refresh-resources').click();
  await expect(page.locator('#resource-create-owner')).toHaveValue('owner');
  await expect(page.locator('#resource-relationship-from')).toHaveValue('one');
  await expect(page.locator('#resource-relationship-to')).toHaveValue('two');
  state.failWrite = true;
  page.once('dialog', dialog => dialog.accept());
  await page.locator('#create-resource-relationship').click();
  await expect(page.locator('#resource-management-status')).toContainText('conflict');
  await expect(relationship.locator('[data-dirty-editor-status]')).toHaveText('Unsaved changes');
  page.once('dialog', dialog => dialog.accept());
  await page.locator('#create-resource').click();
  await expect(page.locator('#resource-management-status')).toContainText('Resource creation failed');
  await expect(create.locator('[data-dirty-editor-status]')).toHaveText('Unsaved changes');
});

test('successful resource save releases navigation and pristine filters do not prompt', async ({ page }) => {
  const { state, editor } = await setup(page);
  await editor.locator('[data-resource-edit-name]').fill('Saved resource name');
  page.once('dialog', dialog => dialog.accept());
  await editor.locator('[data-resource-save]').click();
  await expect(page.locator('#resource-management-status')).toContainText('Updated one');
  await expect(editor.locator('[data-dirty-editor-status]')).toHaveText('No unsaved changes');
  await page.locator('#resource-search').fill('two');
  await expect(page.locator('[data-resource-editor]')).toHaveCount(1);
  const dialogs = [];
  page.on('dialog', async dialog => { dialogs.push(dialog.message()); await dialog.dismiss(); });
  await page.evaluate(() => window.CodexProductUI.openWorkspace('definitions'));
  await expect(page.locator('[data-product-workspace-title]')).toHaveText('Definitions / Contracts');
  expect(dialogs).toEqual([]);
  expect(state.writes).toHaveLength(1);
});
