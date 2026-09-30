const { test, expect } = require('@playwright/test');

const profile = (id, name = id) => ({ id, name, description: `${name} description`, workspace_mode: 'scratch', repository_access: 'none', required_worker_capabilities: ['command_execution'], control_plane_operations: [], network_enabled: false, host_mutation: false });
const base = { record_id: 'catalog-global', definition_id: 'execution.profiles.default', kind: 'execution-profile-catalog', definition_schema_version: '1.0', revision: 1, scope_type: 'global', scope_id: null, lifecycle: 'published', checksum: 'a'.repeat(64), payload: { profiles: [profile('repository-write'), profile('orchestration-only'), profile('custom', 'Custom profile')], default_profile_id: 'repository-write' } };
async function mount(page, intercept = null, readonly = false) {
  const writes = []; let candidate; let effective = structuredClone(base);
  await page.route('**/projects/*/definitions*', route => route.fulfill({ contentType: 'text/html', body: '<body data-project-id="a"><section id="execution-profile-management"></section><script type="module" src="/static/execution_profile_management.js"></script></body>' }));
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url());
    if (intercept && await intercept(route, url)) return;
    if (route.request().method() === 'POST') writes.push({ path: url.pathname, project: url.searchParams.get('project_id'), body: route.request().postDataJSON() });
    if (url.pathname === '/api/identity/me') return route.fulfill({ json: { identity_id: 'operator', principal_kind: 'human', assurance: 'mfa', roles: readonly ? ['reader'] : ['admin'] } });
    if (url.pathname === '/api/execution-profiles') return route.fulfill({ json: { definition: { record_id: effective.record_id } } });
    if (url.pathname.endsWith('/usage')) return route.fulfill({ json: { available: true, count: 2, restricted_count: 1, items: [{ object_type: 'execution', object_id: 'execution-a', label: 'Pinned execution', revision: 1, binding: 'pinned' }] } });
    if (url.pathname === '/api/definitions/records') return route.fulfill({ json: { items: [effective] } });
    if (url.pathname === '/api/definitions/drafts') {
      candidate = { ...base, ...route.request().postDataJSON(), record_id: 'draft-a', lifecycle: 'draft', revision: 1 };
      return route.fulfill({ json: { record: candidate } });
    }
    if (url.pathname.endsWith('/validate')) return route.fulfill({ json: { record: candidate } });
    if (url.pathname.endsWith('/publication-assessment')) return route.fulfill({ json: { requires_independent_approval: false, approvals: [] } });
    if (url.pathname.endsWith('/publish')) { effective = { ...candidate, lifecycle: 'published' }; return route.fulfill({ json: { record: effective } }); }
    return route.fulfill({ status: 404, json: { detail: 'Unexpected endpoint' } });
  });
  await page.goto('http://127.0.0.1:18766/projects/a/definitions?execution_profile=custom');
  await expect(page.getByLabel('Name', { exact: true })).toHaveValue('Custom profile');
  return writes;
}
function dialogs(page) { page.on('dialog', dialog => dialog.accept(dialog.type() === 'prompt' ? (dialog.message().includes('JSON') ? '{}' : 'Reviewed publication') : undefined)); }

test('edits inherited catalog as a Project draft, validates and publishes with empty-slot CAS', async ({ page }) => {
  const writes = await mount(page); dialogs(page);
  await expect(page.locator('[data-profile-impact]')).toContainText('Pinned execution');
  await expect(page.getByLabel('Profile ID', { exact: true })).toHaveJSProperty('readOnly', true);
  await page.getByLabel('Name', { exact: true }).fill('Edited profile');
  await page.getByLabel('Change reason').fill('Improve profile');
  await page.getByRole('button', { name: 'Save new Project draft' }).click();
  await expect(page.locator('[data-profile-draft]')).toContainText('inactive');
  expect(writes[0].body.scope_type).toBe('project'); expect(writes[0].body.scope_id).toBe('a');
  expect(writes[0].body.derived_from_record_id).toBeNull();
  expect(writes[0].body.payload.profiles).toHaveLength(3);
  expect(writes[0].body.payload.profiles.find(p => p.id === 'custom').name).toBe('Edited profile');
  await page.getByRole('button', { name: 'Validate and publish draft' }).click();
  await expect(page.getByRole('button', { name: 'Edited profile', exact: true })).toBeVisible();
  expect(writes.find(w => w.path.endsWith('/publish')).body.expected_active_revision).toBe(0);
  expect(writes.every(w => w.project === 'a')).toBe(true);
});

test('new profile enforces unique IDs, supports search and dirty selection cancellation', async ({ page }) => {
  const writes = await mount(page);
  await page.getByLabel('Name', { exact: true }).fill('Unsaved');
  page.once('dialog', dialog => dialog.dismiss());
  await page.getByRole('button', { name: 'New Execution Profile', exact: true }).click();
  await expect(page.getByLabel('Name', { exact: true })).toHaveValue('Unsaved');
  page.once('dialog', dialog => dialog.accept());
  await page.getByRole('button', { name: 'New Execution Profile', exact: true }).click();
  await page.getByLabel('Profile ID', { exact: true }).fill('custom');
  await page.getByLabel('Name', { exact: true }).fill('New custom');
  await page.getByLabel('Description', { exact: true }).fill('Description');
  await page.getByLabel('Change reason').fill('Create profile');
  await page.getByRole('button', { name: 'Save new Project draft' }).click();
  await expect(page.getByRole('alert')).toContainText('unique profile ID'); expect(writes).toHaveLength(0);
  await page.locator('[name=profile-id]').fill('new-custom');
  await page.getByRole('button', { name: 'Save new Project draft' }).click();
  await expect(page.locator('[data-profile-draft]')).toBeVisible();
  expect(writes[0].body.payload.profiles).toHaveLength(4);
  await page.getByLabel('Find Execution Profiles').fill('Custom');
  await expect(page.locator('[data-profile-list] button')).toHaveCount(1);
});

test('read-only actors can inspect consumers without editing', async ({ page }) => {
  await mount(page, null, true);
  await expect(page.locator('[data-profile-status]')).toContainText('Read only');
  await expect(page.getByRole('button', { name: 'New Execution Profile', exact: true })).toBeDisabled();
  await expect(page.getByRole('button', { name: 'Save new Project draft' })).toBeDisabled();
  await expect(page.locator('[data-profile-impact]')).toContainText('restricted');
});

test('usage failure blocks removal and preserves editor values', async ({ page }) => {
  let writes = 0;
  await mount(page, async (route, url) => {
    if (route.request().method() === 'POST') writes++;
    if (url.pathname.endsWith('/usage')) { await route.fulfill({ status: 503, json: { detail: 'Impact unavailable' } }); return true; }
  });
  await page.getByLabel('Change reason').fill('Remove profile');
  await page.getByRole('button', { name: 'Remove from next catalog' }).click();
  await expect(page.locator('[data-profile-status]')).toContainText('Draft was not saved');
  await expect(page.getByLabel('Change reason')).toHaveValue('Remove profile'); expect(writes).toBe(0);
});

test('server denial preserves edits and offers actionable feedback', async ({ page }) => {
  await mount(page, async (route, url) => {
    if (url.pathname === '/api/definitions/drafts') { await route.fulfill({ status: 403, json: { detail: 'Elevated assurance required' } }); return true; }
  });
  await page.getByLabel('Name', { exact: true }).fill('Retained name');
  await page.getByLabel('Change reason').fill('Update');
  await page.getByRole('button', { name: 'Save new Project draft' }).click();
  await expect(page.getByRole('alert')).toContainText('Elevated assurance required');
  await expect(page.getByLabel('Name', { exact: true })).toHaveValue('Retained name');
});

test('late draft response cannot appear in another Project', async ({ page }) => {
  let release, requested = false;
  const wait = new Promise(resolve => { release = resolve; });
  await mount(page, async (route, url) => {
    if (url.pathname === '/api/definitions/drafts') {
      requested = true; await wait;
      await route.fulfill({ json: { record: { ...base, record_id: 'old-project-draft' } } }); return true;
    }
  });
  await page.getByLabel('Change reason').fill('Update');
  await page.getByRole('button', { name: 'Save new Project draft' }).click();
  await expect.poll(() => requested).toBe(true);
  await page.evaluate(() => {
    history.replaceState({}, '', '/projects/b/definitions');
    window.dispatchEvent(new CustomEvent('codex:project-changed', { detail: { projectId: 'b' } }));
  });
  await expect(page.locator('[data-profile-source]')).toContainText('Project draft for b');
  release();
  await expect(page.locator('[data-profile-draft]')).toBeHidden();
  await expect(page.locator('[data-profile-status]')).not.toContainText('old-project-draft');
});

test('concurrent Project override blocks publication of a draft based on inherited data', async ({ page }) => {
  let moved = false, publishes = 0;
  await mount(page, async (route, url) => {
    if (url.pathname.endsWith('/publish')) publishes++;
    if (moved && url.pathname === '/api/definitions/records') {
      await route.fulfill({ json: { items: [base, { ...base, record_id: 'concurrent', scope_type: 'project', scope_id: 'a', revision: 2 }] } }); return true;
    }
  });
  await page.getByLabel('Change reason').fill('Update');
  await page.getByRole('button', { name: 'Save new Project draft' }).click();
  await expect(page.locator('[data-profile-draft]')).toBeVisible(); moved = true;
  await page.getByRole('button', { name: 'Validate and publish draft' }).click();
  await expect(page.locator('[data-profile-status]')).toContainText('catalog changed');
  expect(publishes).toBe(0);
});

test('custom entry removal creates a reviewed inactive catalog without removing required entries', async ({ page }) => {
  const writes = await mount(page); dialogs(page);
  await page.getByLabel('Change reason').fill('Retire unused profile');
  await page.getByRole('button', { name: 'Remove from next catalog' }).click();
  await expect(page.locator('[data-profile-draft]')).toBeVisible();
  expect(writes[0].body.payload.profiles.map(p => p.id)).toEqual(['repository-write', 'orchestration-only']);
  await page.getByRole('button', { name: 'repository-write', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Remove from next catalog' })).toBeDisabled();
  await expect(page.locator('[data-profile-history]')).toHaveAttribute('href', /definition_record=catalog-global/);
});

test('deep-link history restores the selected profile', async ({ page }) => {
  await mount(page);
  await page.getByRole('button', { name: 'orchestration-only', exact: true }).click();
  await expect(page).toHaveURL(/execution_profile=orchestration-only/);
  await page.goBack();
  await expect(page.getByLabel('Profile ID', { exact: true })).toHaveValue('custom');
});
