const { test, expect } = require('@playwright/test');
const fs = require('node:fs'); const path = require('node:path');
const index = fs.readFileSync(path.join(__dirname, '../../static/index.html'), 'utf8');
const marker = index.indexOf('id="refresh-secrets"');
const card = index.slice(index.lastIndexOf('<div class="developer-card">', marker), index.indexOf('<div class="developer-card">', marker));
const fixture = fs.readFileSync(path.join(__dirname, 'product_workspaces_fixture.html'), 'utf8').replace('</body>', `${card}<script type="module" src="/static/secret_admin.js"></script></body>`);
const reference = { id: 'secret-shared', name: 'Shared credential', organization_id: 'org', workspace_id: 'ws', backend: 'local', status: 'active', provider: 'github', rotation: 0, owner_identity_id: 'operator', allowed_identity_ids: ['worker'], reveal_identity_ids: [], use_allowed: true, project_reference_count: 1, shared_reference_count: 2 };
async function mount(page, { intercept, readonly = false, empty = false } = {}) {
  const writes = []; let items = empty ? [] : [structuredClone(reference)];
  await page.route('**/projects/**', async route => {
    if (route.request().resourceType() === 'document') await route.fulfill({ contentType: 'text/html', body: fixture });
    else await route.continue();
  });
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url());
    if (intercept && await intercept(route, url)) return;
    const method = route.request().method();
    if (['POST', 'DELETE'].includes(method) && url.pathname.startsWith('/api/secrets')) writes.push({ path: url.pathname, project: url.searchParams.get('project_id'), body: method === 'POST' ? route.request().postDataJSON() : null });
    if (url.pathname === '/api/identity/me') return route.fulfill({ json: { identity_id: 'operator', principal_kind: 'human', roles: readonly ? ['member'] : ['admin'], assurance: 'mfa', organization_id: 'org', workspace_id: 'ws' } });
    if (url.pathname === '/api/identity') return route.fulfill({ json: { memberships: [], humans: [], services: [] } });
    if (url.pathname === '/api/secrets/audit') return route.fulfill({ json: { items: [] } });
    if (url.pathname.endsWith('/usage')) return route.fulfill({ json: { available: true, count: 3, outside_view_count: 1, coverage: ['task_sources', 'configuration_revisions'], limitations: ['Historical references are labeled.'], items: [{ label: 'Authoritative TaskSource credential', object_id: 'source-home', scope: 'project', page: 'work-items', state: 'github' }] } });
    if (url.pathname === '/api/secrets' && method === 'POST') { items.push({ ...reference, id: 'secret-new', name: route.request().postDataJSON().name }); return route.fulfill({ json: { item: items.at(-1) } }); }
    if (url.pathname.endsWith('/rotate')) { items[0].rotation++; return route.fulfill({ json: { item: items[0] } }); }
    if (method === 'DELETE') { items[0].status = 'revoked'; items[0].revoked_at = 1; return route.fulfill({ json: { item: items[0] } }); }
    if (url.pathname === '/api/secrets') return route.fulfill({ json: { items, impact_available: true, broken_references: [] } });
    return route.fulfill({ json: { items: [], enabled: false } });
  });
  await page.goto('http://127.0.0.1:18766/projects/home/secrets');
  await expect(page.locator('[data-product-workspace-title]')).toHaveText('Secrets');
  await expect(page.locator('#secret-admin-status')).toContainText('workspace secret reference(s)');
  return writes;
}
function acceptDialogs(page) { page.on('dialog', dialog => dialog.accept()); }

test('Secrets has a main-page Project route, explicit shared scope and reloadable reference links', async ({ page }) => {
  await mount(page);
  await expect(page.locator('[data-project-nav-node="secrets"]')).toHaveAttribute('aria-current', 'page');
  await expect(page.locator('#product-workspace-page #secret-admin-list')).toBeVisible();
  await expect(page.locator('[data-project-page-scope]')).toContainText('Workspace secrets / Project consumer context');
  await expect(page.locator('#secret-admin-list')).toContainText('not a Project-owned copy');
  await page.getByRole('button', { name: 'Inspect consumers' }).click();
  await expect(page.locator('[data-secret-usage-result]')).toContainText('1 outside this Project view');
  await page.getByRole('link', { name: 'Link to this reference' }).click();
  await expect(page).toHaveURL(/\/projects\/home\/secrets\?secret_id=secret-shared/);
  await expect(page.locator('[data-secret-usage-result]')).toContainText('TaskSource');
  await page.reload(); await expect(page.locator('[data-secret-usage-result]')).toContainText('TaskSource');
});

test('creation sends one write-only value, clears it and offers canonical binding paths', async ({ page }) => {
  const writes = await mount(page, { empty: true }); acceptDialogs(page);
  await expect(page.locator('#secret-admin-list')).toContainText('No secret references');
  await page.locator('#secret-create-panel summary').click();
  await page.getByLabel('Secret reference name', { exact: true }).fill('New credential');
  await page.getByLabel('Secret value (never rendered back)', { exact: true }).fill('test-only-create-material');
  await page.getByRole('button', { name: 'Create secret', exact: true }).click();
  await expect(page.locator('#secret-admin-list')).toContainText('New credential');
  await expect(page.locator('#secret-create-value')).toHaveValue('');
  expect(writes).toHaveLength(1); expect(writes[0].project).toBe('home'); expect(writes[0].body.value).toBe('test-only-create-material');
  await expect(page.locator('body')).not.toContainText('test-only-create-material');
  expect(await page.evaluate(() => JSON.stringify([Object.values(localStorage), Object.values(sessionStorage)]))).not.toContain('test-only-create-material');
  await expect(page.getByRole('link', { name: 'Bind / unbind TaskSource credential' })).toHaveAttribute('href', /\/projects\/home\/work-items/);
});

test('rotation and revocation require consumer review and leave only metadata', async ({ page }) => {
  const writes = await mount(page); const messages = [];
  page.on('dialog', dialog => { messages.push(dialog.message()); return dialog.accept(); });
  await page.getByLabel('Replacement value for Shared credential').fill('test-only-replacement');
  await page.getByRole('button', { name: 'Rotate', exact: true }).click();
  await expect(page.locator('#secret-admin-list')).toContainText('Rotation: 1');
  await expect(page.getByLabel('Replacement value for Shared credential')).toHaveValue('');
  await page.getByRole('button', { name: 'Revoke', exact: true }).click();
  await expect(page.locator('#secret-admin-list')).toContainText('revoked');
  await expect(page.getByRole('button', { name: 'Rotate', exact: true })).toHaveCount(0);
  expect(writes.map(item => item.project)).toEqual(['home', 'home']);
  expect(messages.every(message => message.includes('3 canonical reference(s)') && message.includes('1 outside this Project view'))).toBe(true);
  await expect(page.locator('body')).not.toContainText('test-only-replacement');
});

test('unavailable impact blocks revocation and clears replacement values', async ({ page }) => {
  let mutations = 0;
  await mount(page, { intercept: async (route, url) => {
    if (['POST', 'DELETE'].includes(route.request().method()) && url.pathname.startsWith('/api/secrets')) mutations++;
    if (url.pathname.endsWith('/usage')) { await route.fulfill({ status: 503, json: { detail: 'Scan unavailable' } }); return true; }
  } });
  await page.getByLabel('Replacement value for Shared credential').fill('test-only-pending');
  await page.getByRole('button', { name: 'Revoke', exact: true }).click();
  await expect(page.locator('#secret-admin-status')).toContainText('impact is unavailable');
  await expect(page.getByLabel('Replacement value for Shared credential')).toHaveValue(''); expect(mutations).toBe(0);
});

test('read-only actors inspect metadata without lifecycle buttons', async ({ page }) => {
  await mount(page, { readonly: true });
  await expect(page.locator('#secret-create-assurance')).toContainText('Read only');
  await expect(page.locator('#create-secret')).toBeDisabled();
  await expect(page.getByRole('button', { name: 'Revoke', exact: true })).toHaveCount(0);
  await expect(page.locator('#secret-admin-list')).toContainText('Use permission: allowed');
  await expect(page.locator('#secret-admin-list')).toContainText('never reveal stored values');
});

test('Project change while impact loads prevents mutation and clears all write-only fields', async ({ page }) => {
  let release, requested = false, mutations = 0;
  const pending = new Promise(resolve => { release = resolve; });
  await mount(page, { intercept: async (route, url) => {
    if (url.pathname.endsWith('/usage') && url.searchParams.get('project_id') === 'home') {
      requested = true; await pending; await route.fulfill({ json: { available: true, count: 1, outside_view_count: 0, items: [] } }); return true;
    }
    if (['POST', 'DELETE'].includes(route.request().method()) && url.pathname.startsWith('/api/secrets')) mutations++;
  } });
  await page.getByLabel('Replacement value for Shared credential').fill('test-only-old-project');
  await page.getByRole('button', { name: 'Rotate', exact: true }).click();
  await expect.poll(() => requested).toBe(true);
  await page.locator('#product-project-switcher').selectOption('alpha');
  await expect(page.locator('#secret-admin-status')).toContainText('Project alpha'); release();
  await expect(page.getByLabel('Replacement value for Shared credential')).toHaveValue(''); expect(mutations).toBe(0);
});

test('rejected creation clears material and preserves non-secret metadata', async ({ page }) => {
  await mount(page, { intercept: async (route, url) => {
    if (url.pathname === '/api/secrets' && route.request().method() === 'POST') { await route.fulfill({ status: 403, json: { detail: 'Denied' } }); return true; }
  } }); acceptDialogs(page);
  await page.locator('#secret-create-panel summary').click();
  await page.getByLabel('Secret reference name', { exact: true }).fill('Retained metadata');
  await page.getByLabel('Secret value (never rendered back)', { exact: true }).fill('test-only-rejected-material');
  await page.getByRole('button', { name: 'Create secret', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('secret value was cleared');
  await expect(page.locator('#secret-create-value')).toHaveValue('');
  await expect(page.getByLabel('Secret reference name', { exact: true })).toHaveValue('Retained metadata');
});

test('Secrets remains usable at phone width and shows broken references with repair links', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await mount(page, { intercept: async (route, url) => {
    if (url.pathname === '/api/secrets' && route.request().method() === 'GET') {
      await route.fulfill({ json: { items: [reference], impact_available: true, broken_references: [{ secret_id: 'secret-missing', status: 'missing_or_unavailable' }] } }); return true;
    }
  } });
  await expect(page.locator('#secret-broken-references')).toContainText('missing_or_unavailable');
  await expect(page.locator('#secret-broken-references a')).toHaveCount(2);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
  await page.getByRole('button', { name: 'Inspect consumers' }).focus();
  await page.keyboard.press('Enter');
  await expect(page.locator('[data-secret-usage-result]')).toContainText('TaskSource');
});
