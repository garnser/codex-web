const { test, expect } = require('@playwright/test');
const fs = require('node:fs'); const path = require('node:path');
const quota_schema = require('./entitlement_quota_schema.json');
const index = fs.readFileSync(path.join(__dirname, '../../static/index.html'), 'utf8');
const at = index.indexOf('<h2>Entitlements & Usage</h2>');
const card = index.slice(index.lastIndexOf('<div class="developer-card">', at), index.indexOf('<div class="developer-card">', at));
const fixture = fs.readFileSync(path.join(__dirname, 'product_workspaces_fixture.html'), 'utf8').replace('<button id="sidebar-toggle" type="button">', '<button id="sidebar-toggle" class="sidebar-toggle" type="button">').replace('<link rel="stylesheet" href="/static/styles.css">', '<link rel="stylesheet" href="/static/design_tokens.css"><link rel="stylesheet" href="/static/workspace_components.css"><link rel="stylesheet" href="/static/styles.css">').replace('</body>', `${card}<script type="module" src="/static/entitlement_admin.js"></script></body>`);
async function mount(page, { external = false, intercept } = {}) {
  const state = { writes: [], previews: [], fail: false, revision: 'current-revision' };
  state.snapshot = { schema_version: '1.0', revision: state.revision, organization_id: 'org', workspace_id: 'ws', inheritance: 'All Projects inherit this workspace configuration; no Project override exists.', control: { kind: external ? 'external' : 'local', controller_identity_id: external ? 'billing-service' : null, guidance: external ? 'Contact plan support' : 'Workspace administrators' }, can_manage: !external, mode: 'enforced', mode_source: 'explicit', capabilities: [{ id: 'grant-one', capability: 'external_actions', enabled: true, source: 'hosted-label-is-only-provenance', decision: { allowed: true, reason: 'capability_entitled', mode: 'enforced' } }], quotas: [{ id: 'quota-one', metric: 'attempts', limit: 10, window: 'month', behavior: 'hard_stop', warning_fraction: 0.8, source: 'manual', current_usage: 4 }], quota_schema };
  await page.route('**/projects/**', route => route.request().resourceType() === 'document' ? route.fulfill({ contentType: 'text/html', body: fixture }) : route.continue());
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url()); const method = route.request().method();
    if (intercept && await intercept(route, url, state)) return;
    if (url.pathname === '/api/entitlements/administration') return route.fulfill({ json: state.snapshot });
    if (url.pathname === '/api/entitlements/usage') return route.fulfill({ json: { items: [{ metric: 'attempts', amount: 4, source: 'runtime', id: 'usage-one', idempotency_key: 'event-one' }] } });
    if (url.pathname === '/api/entitlements/administration/preview') {
      const data = route.request().postDataJSON(); state.previews.push(data);
      return route.fulfill({ json: { available: true, expected_revision: state.revision, kind: data.kind, key: data.key, organization_id: 'org', workspace_id: 'ws', current_usage: 4, effect: data.kind === 'retire_quota' ? 'Removes quota enforcement. Usage history remains.' : 'Reduction affects subsequent entitled operations.', usage_caveat: 'Usage may change after preview.' } });
    }
    if (url.pathname.startsWith('/api/entitlements/') && ['PUT', 'DELETE'].includes(method)) {
      state.writes.push({ path: url.pathname, project: url.searchParams.get('project_id'), expected: url.searchParams.get('expected_revision'), method, payload: route.request().postData() ? route.request().postDataJSON() : null });
      return route.fulfill(state.fail ? { status: 409, json: { detail: 'Entitlement configuration changed; reload' } } : { json: { item: {} } });
    }
    return route.fulfill({ json: { items: [] } });
  });
  await page.goto('http://127.0.0.1:18766/projects/home/configuration');
  await page.locator('#refresh-entitlements').click();
  await expect(page.locator('#entitlement-management')).toContainText('Shared workspace org/ws');
  return state;
}
async function review(page) {
  await page.locator('[data-entitlement-preview]').click();
  await expect(page.locator('[data-entitlement-save]')).toBeEnabled();
}

test('quota reduction previews workspace impact and publishes scoped CAS without changing usage', async ({ page }) => {
  const state = await mount(page);
  await page.getByRole('button', { name: 'Edit quota attempts', exact: true }).click();
  await page.locator('[name=limit]').fill('2');
  await expect(page.locator('[data-entitlement-save]')).toBeDisabled();
  await review(page);
  await expect(page.locator('[data-entitlement-impact]')).toContainText('Current usage in proposed window: 4');
  page.once('dialog', dialog => dialog.accept()); await page.locator('[data-entitlement-save]').click();
  await expect.poll(() => state.writes.length).toBe(1);
  expect(state.writes[0]).toMatchObject({ project: 'home', expected: 'current-revision', payload: { limit: 2 } });
  await expect(page.locator('#entitlement-usage')).toContainText('event-one');
});

test('external control is read-only with inherited scope and controller guidance', async ({ page }) => {
  const state = await mount(page, { external: true });
  await expect(page.locator('#entitlement-management')).toContainText('Read-only');
  await expect(page.locator('#entitlement-management')).toContainText('billing-service');
  await expect(page.locator('#entitlement-management')).toContainText('Contact plan support');
  await expect(page.locator('#entitlement-management')).toContainText('no Project override');
  await expect(page.getByRole('button', { name: 'Edit tenant mode' })).toHaveCount(0);
  expect(state.writes).toEqual([]);
});

test('local provenance labels remain editable and typed capability values are preserved', async ({ page }) => {
  const state = await mount(page);
  await page.getByRole('button', { name: 'Edit capability external_actions', exact: true }).click();
  await expect(page.locator('[name=source]')).toHaveValue('hosted-label-is-only-provenance');
  await page.locator('[name=enabled]').selectOption('disabled');
  await page.locator('[name=expires_at]').fill('2000000000');
  await review(page); page.once('dialog', dialog => dialog.accept()); await page.locator('[data-entitlement-save]').click();
  await expect.poll(() => state.writes.length).toBe(1);
  expect(state.writes[0].payload).toMatchObject({ enabled: false, expires_at: 2000000000 });
});

test('retiring a quota reviews removal and calls the guarded retirement API', async ({ page }) => {
  const state = await mount(page);
  await page.getByRole('button', { name: 'Retire quota attempts', exact: true }).click();
  await review(page); await expect(page.locator('[data-entitlement-impact]')).toContainText('Removes quota enforcement');
  page.once('dialog', dialog => dialog.accept()); await page.locator('[data-entitlement-save]').click();
  await expect.poll(() => state.writes.length).toBe(1);
  expect(state.writes[0]).toMatchObject({ method: 'DELETE', path: '/api/entitlements/quotas/attempts', expected: 'current-revision' });
});

test('invalid and conflicted edits are retained; preview cannot overwrite a changed base', async ({ page }) => {
  const state = await mount(page);
  await page.getByRole('button', { name: 'Edit quota attempts', exact: true }).click();
  await page.locator('[name=limit]').fill('-1'); await page.locator('[data-entitlement-preview]').click();
  await expect(page.locator('[data-validation-summary]')).toBeVisible(); expect(state.previews).toEqual([]);
  await page.locator('[name=limit]').fill('2'); await review(page);
  state.fail = true; page.once('dialog', dialog => dialog.accept()); await page.locator('[data-entitlement-save]').click();
  await expect(page.locator('[data-validation-summary]')).toContainText('configuration changed');
  await expect(page.locator('[name=limit]')).toHaveValue('2'); await expect(page.locator('[data-entitlement-save]')).toBeDisabled();
  state.revision = 'new-revision'; await page.locator('[data-entitlement-preview]').click();
  await expect(page.locator('[data-validation-summary]')).toContainText('reload before reviewing');
  await expect(page.locator('[data-entitlement-save]')).toBeDisabled();
});

test('dirty edit refresh cancellation and small-screen reflow preserve values', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 }); await mount(page);
  await page.getByRole('button', { name: 'Edit quota attempts', exact: true }).click();
  await page.locator('[name=limit]').fill('3');
  page.once('dialog', dialog => dialog.dismiss()); await page.locator('#refresh-entitlements').click();
  await expect(page.locator('[name=limit]')).toHaveValue('3');
  await page.addStyleTag({ content: 'html { font-size: 200%; }' });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test('late preview from a departed Project cannot enable publication after A-B-A', async ({ page }) => {
  let release; let pending;
  await mount(page, { intercept: async (route, url) => {
    if (url.pathname.endsWith('/administration/preview')) {
      pending = true; await new Promise(resolve => { release = resolve; });
      await route.fulfill({ json: { available: true, expected_revision: 'current-revision', effect: 'stale evidence' } }); return true;
    }
  } });
  await page.getByRole('button', { name: 'Edit tenant mode', exact: true }).click();
  await page.locator('[data-entitlement-preview]').click(); await expect.poll(() => pending).toBe(true);
  await page.evaluate(() => { for (const projectId of ['other', 'home']) window.dispatchEvent(new CustomEvent('codex:project-changed', { detail: { projectId } })); });
  release();
  await expect(page.locator('#entitlement-management')).toContainText('Shared workspace');
  await expect(page.locator('[data-entitlement-save]')).toHaveCount(0);
  await expect(page.locator('#entitlement-management')).not.toContainText('stale evidence');
});


test('entitlement editor uses desktop content width and keeps fields inside its container', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 }); await mount(page);
  await page.getByRole('button', { name: 'Edit quota attempts', exact: true }).click();
  expect((await page.locator('[data-entitlement-form]').boundingBox()).width).toBeGreaterThan(850);
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 1000 });
    expect(await page.locator('[data-entitlement-form]').evaluate(form => {
      const box = form.getBoundingClientRect();
      return [...form.querySelectorAll('input,select')].every(node => { const rect = node.getBoundingClientRect(); return rect.left >= box.left && rect.right <= box.right; });
    })).toBe(true);
  }
});
