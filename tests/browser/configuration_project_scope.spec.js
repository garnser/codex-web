const { test, expect } = require('@playwright/test');
const fs = require('node:fs');
const path = require('node:path');
const index = fs.readFileSync(path.join(__dirname, '../../static/index.html'), 'utf8');
const marker = index.indexOf('id="refresh-configuration"');
const start = index.lastIndexOf('<div class="developer-card">', marker);
const card = index.slice(start, index.indexOf('<div class="developer-card">', marker));
const specs = [
  { key: 'test.limit', value_kind: 'integer', editable: true, allowed_scopes: ['workspace', 'project', 'resource'], minimum: 1, maximum: 10 },
  { key: 'test.definition', value_kind: 'definition_ref', editable: true, allowed_scopes: ['project'] },
];
function record(project) {
  return { id: `record-${project}`, key: 'test.limit', revision: 1, state: 'draft',
    scope_type: 'project', scope_id: project, value: 5 };
}
async function mount(page, onRequest) {
  await page.route('**/projects/project-a/configuration', route => route.fulfill({
    contentType: 'text/html', body: `<meta charset="utf-8"><body data-project-id="project-a">
      <details id="developer-panel"></details><section data-product-workspace-panel="settings">${card}</section>
      <script src="/static/configuration_admin.js"></script>
      <script src="/static/configuration_management.js"></script></body>`,
  }));
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url());
    if (onRequest && await onRequest(route, url)) return;
    const project = url.searchParams.get('project_id');
    if (url.pathname === '/api/identity/me') return route.fulfill({ json: {
      identity_id: 'operator', principal_kind: 'human', assurance: 'mfa', roles: ['admin'],
      organization_id: 'org-a', workspace_id: 'ws-a',
    } });
    if (url.pathname === '/api/configuration/specs') return route.fulfill({ json: { items: specs } });
    if (url.pathname === '/api/configuration/records') return route.fulfill({ json: { items: project === 'empty' ? [] : [record(project)] } });
    if (url.pathname === '/api/definitions/records') return route.fulfill({ json: { items: [{
      record_id: `definition-${project}`, definition_id: `catalog-${project}`, revision: 1,
      lifecycle: 'published', kind: 'execution-profile-catalog',
    }] } });
    if (url.pathname.startsWith('/api/projects/')) {
      const id = url.pathname.split('/')[3];
      return route.fulfill({ json: url.pathname.endsWith('/resources')
        ? { items: id === 'empty' ? [] : [{ id: `resource-${id}`, name: id, resource_type: 'repository' }] }
        : { id, name: id } });
    }
    return route.fulfill({ json: { items: [] } });
  });
  await page.goto('http://127.0.0.1:18766/projects/project-a/configuration');
  await expect(page.locator('#configuration-record-list')).toContainText('project-a');
  await expect(page.locator('#configuration-draft-project')).toHaveValue('project-a');
}
async function changeProject(page, projectId) {
  await page.evaluate(id => {
    history.replaceState({}, '', `/projects/${id}/configuration`);
    document.body.dataset.projectId = id;
    window.dispatchEvent(new CustomEvent('codex:project-changed', { detail: { projectId: id } }));
  }, projectId);
}
test('Configuration scopes records, resource and Definition choices, resolution and empty/error states', async ({ page }) => {
  const resolutions = [];
  await mount(page, async (route, url) => {
    if (url.searchParams.get('project_id') === 'foreign' || url.pathname.includes('/projects/foreign')) {
      await route.fulfill({ status: 404, json: { detail: 'Project not found' } }); return true;
    }
    if (url.pathname === '/api/configuration/resolve') {
      resolutions.push(route.request().postDataJSON());
      await route.fulfill({ json: { effective: { key: 'test.limit', value: 5, source: 'default', resolution_chain: [] } } }); return true;
    }
  });
  await changeProject(page, 'project-c');
  await expect(page.locator('#configuration-record-list')).toContainText('project-c');
  await expect(page.locator('#configuration-record-list')).not.toContainText('project-a');
  await expect(page.locator('#configuration-resolve-project')).toHaveValue('project-c');
  await expect(page.locator('#configuration-resolve-project')).toBeDisabled();
  await expect(page.locator('#configuration-resolve-resource option')).toHaveText(['No resource context', 'project-c · repository · resource-project-c']);
  await page.locator('#configuration-management-panel').evaluate(node => { node.open = true; });
  await page.locator('#configuration-draft-key').selectOption('test.definition');
  await expect(page.locator('#configuration-draft-value-host')).toContainText('catalog-project-c');
  await expect(page.locator('#configuration-draft-value-host')).not.toContainText('catalog-project-a');
  await page.locator('#configuration-resolve-resource').evaluate(node => { node.closest('details').open = true; });
  await page.locator('#configuration-resolve-resource').selectOption('resource-project-c');
  await page.locator('#resolve-configuration').click();
  await expect.poll(() => resolutions.length).toBe(1);
  expect(resolutions[0].context).toMatchObject({ project_id: 'project-c', resource_id: 'resource-project-c' });
  await changeProject(page, 'empty');
  await expect(page.locator('#configuration-record-list')).toContainText('No records');
  await expect(page.locator('#configuration-resolve-result')).toBeEmpty();
  await expect(page.locator('#configuration-resolve-resource option')).toHaveCount(1);
  await changeProject(page, 'foreign');
  await expect(page.locator('#configuration-status')).toContainText('Project not found');
  await expect(page.locator('#configuration-record-list')).toBeEmpty();
  await expect(page.locator('#create-configuration-draft')).toBeDisabled();
});
for (const stage of ['records', 'definitions']) {
  test(`late ${stage} responses cannot repopulate a previous Project`, async ({ page }) => {
    await page.addInitScript(() => {
      const original = fetch;
      window.fetch = (url, options = {}) => original(url, { ...options, signal: undefined });
    });
    let hold = false, requested = false, release;
    const pending = new Promise(resolve => { release = resolve; });
    const endpoint = stage === 'records' ? '/api/configuration/records' : '/api/definitions/records';
    await mount(page, async (route, url) => {
      if (hold && url.pathname === endpoint && url.searchParams.get('project_id') === 'project-a') {
        requested = true; await pending;
        await route.fulfill({ json: { items: stage === 'records' ? [record('project-a')] : [{
          record_id: 'stale-reference', definition_id: 'stale-reference', revision: 1, lifecycle: 'published',
        }] } }); return true;
      }
    });
    hold = true;
    await page.locator('#refresh-configuration').click();
    await expect.poll(() => requested).toBe(true);
    await changeProject(page, 'project-c');
    await expect(page.locator('#configuration-draft-project')).toHaveValue('project-c');
    const response = page.waitForResponse(url => url.url().includes(endpoint + '?project_id=project-a'));
    release(); await response;
    await expect(page.locator('#configuration-record-list')).toContainText('project-c');
    await expect(page.locator('#configuration-record-list')).not.toContainText('project-a');
    await page.locator('#configuration-management-panel').evaluate(node => { node.open = true; });
    await page.locator('#configuration-draft-key').selectOption('test.definition');
    await expect(page.locator('#configuration-draft-value-host')).toContainText('catalog-project-c');
    await expect(page.locator('#configuration-draft-value-host')).not.toContainText('stale-reference');
  });
}
test('switching Project during impact preflight prevents later publication and prompts', async ({ page }) => {
  let requested = false, release;
  const writes = [], dialogs = [];
  const pending = new Promise(resolve => { release = resolve; });
  await mount(page, async (route, url) => {
    if (route.request().method() === 'POST') writes.push(url.pathname);
    if (url.pathname.endsWith('/impact')) {
      requested = true; await pending;
      await route.fulfill({ json: { more_specific_overrides: [] } }); return true;
    }
  });
  page.on('dialog', async dialog => { dialogs.push(dialog.message()); await dialog.dismiss(); });
  await page.locator('[data-configuration-record="record-project-a"]').evaluate(node => { node.open = true; });
  await page.locator('[data-configuration-action="publish"]').click();
  await expect.poll(() => requested).toBe(true);
  await changeProject(page, 'project-c');
  await expect(page.locator('#configuration-record-list')).toContainText('project-c');
  const response = page.waitForResponse(url => url.url().endsWith('/impact'));
  release(); await response;
  await page.waitForTimeout(100);
  expect(writes).toEqual([]); expect(dialogs).toEqual([]);
});
test('unsaved Configuration draft survives same-Project refresh and cancelled navigation', async ({ page }) => {
  await mount(page);
  await page.locator('#configuration-management-panel').evaluate(node => { node.open = true; });
  await page.locator('#configuration-draft-value').fill('7');
  await page.locator('#configuration-draft-reason').fill('Keep this draft');
  await page.locator('#refresh-configuration').click();
  await expect(page.locator('[data-dirty-editor-status]')).toHaveText('Unsaved changes');
  await expect(page.locator('#configuration-draft-value')).toHaveValue('7');
  page.once('dialog', dialog => dialog.dismiss());
  expect(await page.evaluate(async () => (await import('/static/dirty_editor.js')).confirmDiscard())).toBe(false);
  await expect(page.locator('#configuration-draft-reason')).toHaveValue('Keep this draft');
});
