const { resolveAction } = require('./action_confirmation_helpers');
const { test, expect } = require('@playwright/test');
const fs = require('node:fs');
const path = require('node:path');
const index = fs.readFileSync(path.join(__dirname, '../../static/index.html'), 'utf8');
const marker = index.indexOf('id="refresh-definitions"');
const start = index.lastIndexOf('<div class="developer-card">', marker);
const card = index.slice(start, index.indexOf('<div class="developer-card">', marker));

function record(project) {
  return { record_id: `record-${project}`, kind: 'execution-profile-catalog',
    definition_id: 'execution.profiles.default', revision: 1, lifecycle: 'draft',
    definition_schema_version: '1.0', scope_type: 'project', scope_id: project,
    payload: { profiles: [{ id: project, name: `Profile ${project}` }] }, checksum: 'a'.repeat(64) };
}

async function mount(page, onRequest = null) {
  await page.route('**/projects/project-a/definitions', route => route.fulfill({
    contentType: 'text/html', body: `<body data-project-id="project-a">
      <details id="developer-panel"></details>
      <section data-product-workspace-panel="definitions">${card}</section>
      <script src="/static/definition_registry_admin.js"></script>
      <script src="/static/definition_registry_management.js"></script></body>`,
  }));
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url());
    if (onRequest && await onRequest(route, url)) return;
    if (url.pathname === '/api/identity/me') return route.fulfill({ json: {
      identity_id: 'operator', principal_kind: 'human', assurance: 'mfa',
      roles: ['admin'], organization_id: 'org-a', workspace_id: 'ws-a',
    } });
    if (url.pathname === '/api/definitions/schemas') return route.fulfill({ json: {
      items: [{ kind: 'execution-profile-catalog', schema_version: '1.0' }],
    } });
    if (url.pathname === '/api/definitions/records') {
      const project = url.searchParams.get('project_id');
      return route.fulfill({ json: { items: project === 'empty' ? [] : [record(project)] } });
    }
    if (url.pathname.startsWith('/api/projects/')) return route.fulfill({ json: {
      id: url.pathname.split('/').pop(), name: 'Selected Project',
    } });
    return route.fulfill({ json: { engine_version: '1.0', items: [], records: 20, active: 10 } });
  });
  await page.goto('http://127.0.0.1:18766/projects/project-a/definitions');
  await expect(page.locator('#definition-registry-list')).toContainText('Profile project-a');
}

async function changeProject(page, projectId) {
  await page.evaluate(id => {
    history.replaceState({}, '', `/projects/${id}/definitions`);
    document.body.dataset.projectId = id;
    window.dispatchEvent(new CustomEvent('codex:project-changed', { detail: { projectId: id } }));
  }, projectId);
}

test('Definitions follows Project context, scoped resolution and empty/error states', async ({ page }) => {
  await mount(page, async (route, url) => {
    if (url.searchParams.get('project_id') === 'foreign' || url.pathname.endsWith('/projects/foreign')) {
      await route.fulfill({ status: 404, json: { detail: 'Project not found' } });
      return true;
    }
  });
  await expect(page.locator('#definition-resolve-project')).toHaveValue('project-a');
  await expect(page.locator('#definition-resolve-project')).toBeDisabled();
  await changeProject(page, 'project-c');
  await expect(page.locator('#definition-registry-list')).toContainText('Profile project-c');
  await expect(page.locator('#definition-registry-list')).not.toContainText('Profile project-a');
  await expect(page.locator('#definition-draft-project')).toHaveValue('project-c');
  await changeProject(page, 'empty');
  await expect(page.locator('#definition-registry-list')).toContainText('No definition revisions');
  await expect(page.locator('#definition-bootstrap')).toContainText('Visible records: 0');
  await changeProject(page, 'foreign');
  await expect(page.locator('#definition-registry-status')).toContainText('Project not found');
  await expect(page.locator('#definition-registry-list')).toBeEmpty();
  await expect(page.locator('#create-definition-draft')).toBeDisabled();
});

test('late catalog responses cannot repopulate the previous Project', async ({ page }) => {
  await page.addInitScript(() => {
    const original = fetch;
    window.fetch = (url, options = {}) => original(url, { ...options, signal: undefined });
  });
  let hold = false;
  let release;
  let requested = false;
  const pending = new Promise(resolve => { release = resolve; });
  await mount(page, async (route, url) => {
    if (hold && url.pathname === '/api/definitions/records' && url.searchParams.get('project_id') === 'project-a') {
      requested = true;
      await pending;
      await route.fulfill({ json: { items: [record('project-a')] } });
      return true;
    }
  });
  hold = true;
  await page.locator('#refresh-definitions').click();
  await expect.poll(() => requested).toBe(true);
  await changeProject(page, 'project-c');
  await expect(page.locator('#definition-registry-list')).toContainText('Profile project-c');
  const response = page.waitForResponse(url => url.url().includes('records?project_id=project-a'));
  release();
  await response;
  await expect(page.locator('#definition-registry-list')).not.toContainText('Profile project-a');
  await expect(page.locator('#definition-resolve-project')).toHaveValue('project-c');
});

for (const stage of ['publication-assessment', 'usage']) {
test(`changing Project during ${stage} prevents a later publish`, async ({ page }) => {
  let release;
  let requested = false;
  const writes = [];
  const pending = new Promise(resolve => { release = resolve; });
  await mount(page, async (route, url) => {
    if (route.request().method() === 'POST') writes.push(url.pathname);
    if (url.pathname === '/api/definitions/records') {
      const project = url.searchParams.get('project_id');
      const draft = { ...record(project), revision: 2 };
      await route.fulfill({ json: { items: [draft, { ...draft,
        record_id: `published-${project}`, lifecycle: 'published', revision: 1 }] } });
      return true;
    }
    if (url.pathname.endsWith('/' + stage)) {
      requested = true;
      await pending;
      await route.fulfill({ json: { requires_independent_approval: false, approvals: [], reasons: [] } });
      return true;
    }
  });
  const dialogs = [];
  page.on('dialog', async dialog => { dialogs.push(dialog.message()); await dialog.dismiss(); });
  await page.locator('#definition-search').fill('project-a');
  await page.locator('[data-definition-record="record-project-a"]').evaluate(node => { node.open = true; });
  await page.locator('[data-definition-action="publish"]').click();
  await expect.poll(() => requested).toBe(true);
  await changeProject(page, 'project-c');
  await expect(page.locator('#definition-registry-list')).toContainText('Profile project-c');
  const response = page.waitForResponse(response => new URL(response.url()).pathname.endsWith('/' + stage));
  release();
  await response;
  await page.waitForTimeout(100);
  expect(writes).toEqual([]);
  expect(dialogs).toEqual([]);
  await expect(page.locator('#definition-lifecycle-status')).not.toContainText('project-a');
});
}

test('a dirty Definition draft can cancel navigation without losing its input', async ({ page }) => {
  await mount(page);
  await expect(page.locator('#definition-draft-project')).toHaveValue('project-a');
  await page.locator('#definition-lifecycle-panel').evaluate(node => { node.open = true; });
  await page.locator('#definition-draft-id').fill('unsaved.profile.catalog');
  await page.locator('#definition-draft-payload').fill('{"profiles":[]}');
  await expect(page.locator('[data-dirty-editor-status]')).toHaveText('Unsaved changes');
  page.once('dialog', dialog => dialog.dismiss());
  const allowed = await page.evaluate(async () => {
    const { confirmDiscard } = await import('/static/dirty_editor.js');
    return confirmDiscard();
  });
  expect(allowed).toBe(false);
  await expect(page.locator('#definition-draft-id')).toHaveValue('unsaved.profile.catalog');
  await expect(page.locator('#definition-draft-payload')).toHaveValue('{"profiles":[]}');
});

test('Definition mutations and exports carry the selected Project to canonical APIs', async ({ page }) => {
  const calls = [];
  await mount(page, async (route, url) => {
    if (url.pathname.endsWith('/validate') || url.pathname.endsWith('/export')) {
      calls.push({ path: url.pathname, project: url.searchParams.get('project_id') });
      await route.fulfill({ json: { records: [] } });
      return true;
    }
  });
  await page.locator('[data-definition-record="record-project-a"]').evaluate(node => { node.open = true; });
  page.once('dialog', dialog => dialog.accept());
  await page.locator('[data-definition-action="validate"]').click();
  await expect.poll(() => calls.length).toBe(1);
  await page.evaluate(async () => {
    const { exportDefinitions } = await import('/static/definition_registry_transfer.js');
    await exportDefinitions(() => {});
  });
  await expect.poll(() => calls.length).toBe(2);
  expect(calls.every(call => call.project === 'project-a')).toBe(true);
  await changeProject(page, 'project-c');
  await expect(page.locator('#definition-registry-list')).toContainText('Profile project-c');
  await page.evaluate(async () => {
    const { exportDefinitions } = await import('/static/definition_registry_transfer.js');
    await exportDefinitions(() => {});
  });
  expect(calls.at(-1).project).toBe('project-c');
});

test('catalog quarantine requires available Project consumer impact and shows its scope', async ({ page }) => {
  let unavailable = true, mutations = 0; const confirmations = [];
  await mount(page, async (route, url) => {
    if (url.pathname === '/api/execution-profiles') {
      expect(url.searchParams.get('project_id')).toBe('project-a');
      await route.fulfill({ json: { items: [{ id: 'repository-write' }] } }); return true;
    }
    if (url.pathname.startsWith('/api/execution-profiles/') && url.pathname.endsWith('/usage')) {
      await route.fulfill(unavailable ? { status: 503, json: { detail: 'Unavailable impact' } }
        : { json: { available: true, count: 4, restricted_count: 1 } }); return true;
    }
    if (url.pathname.endsWith('/quarantine')) { mutations++; await route.fulfill({ json: { record: record('project-a') } }); return true; }
  });
  await page.locator('[data-definition-record]').evaluate(node => { node.open = true; });
  await page.locator('[data-definition-action="quarantine"]').click();
  await expect(page.locator('#definition-lifecycle-status')).toContainText('impact unavailable'); expect(mutations).toBe(0);
  unavailable = false;
  page.on('dialog', async dialog => { confirmations.push(dialog.message()); await dialog.accept(dialog.type() === 'prompt' ? 'Reviewed consumers' : undefined); });
  await page.locator('[data-definition-action="quarantine"]').click();
  confirmations.push(await resolveAction(page));
  await expect.poll(() => mutations).toBe(1);
  expect(confirmations.join(' ')).toContain('4 profile consumer reference(s)');
  expect(confirmations.join(' ')).toContain('sibling Project consumers are outside this preview');
});
