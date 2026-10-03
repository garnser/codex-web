const { resolveAction } = require('./action_confirmation_helpers');
const { test, expect } = require('@playwright/test');
const fs = require('node:fs'); const path = require('node:path');
const schemas = require('./provider_binding_schemas.json');
const index = fs.readFileSync(path.join(__dirname, '../../static/index.html'), 'utf8');
const at = index.indexOf('<h2>Provider Binding Management</h2>');
const card = index.slice(index.lastIndexOf('<div class="developer-card">', at), index.indexOf('<div class="developer-card">', at));
const fixture = fs.readFileSync(path.join(__dirname, 'product_workspaces_fixture.html'), 'utf8').replace('<button id="sidebar-toggle" type="button">', '<button id="sidebar-toggle" class="sidebar-toggle" type="button">').replace('<link rel="stylesheet" href="/static/styles.css">', '<link rel="stylesheet" href="/static/design_tokens.css"><link rel="stylesheet" href="/static/workspace_components.css"><link rel="stylesheet" href="/static/styles.css">').replace('</body>', `${card}<script type="module" src="/static/provider_binding_management.js"></script></body>`);
const agent = { id: 'runner', display_name: 'Runner', declared_capabilities: ['agent_execution'], granted_capabilities: ['agent_execution'], model_provider_ids: ['model-one'], extension_installation_id: null, configuration_refs: [], credential_refs: ['credential-one'], residency_tags: [], compliance_tags: [], lifecycle: 'active', health: 'healthy', compatibility: 'compatible', revision: 1 };
const model = { id: 'model-one', display_name: 'Model provider', adapter_type: 'mock', base_url: null, credential_ref: 'credential-one', credential_required: true, residency_tags: [], compliance_tags: [], status: 'active' };
async function mount(page, { readonly = false, unavailable = false, intercept, query = 'provider_id=runner' } = {}) {
  const state = { writes: [], revision: 1, fail: false, impacts: 0 };
  await page.route('**/projects/**', route => route.request().resourceType() === 'document' ? route.fulfill({ contentType: 'text/html', body: fixture }) : route.continue());
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url()); const method = route.request().method();
    if (intercept && await intercept(route, url, state)) return;
    if (url.pathname === '/api/agent-providers/administration') return route.fulfill({ json: { schema: schemas.agent, organization_id: 'org', workspace_id: 'ws', runtime_ownership: 'Runtime registration is deployment-owned.', can_manage: !readonly, items: [agent, { ...agent, id: 'model-one', display_name: 'Synthesized model', synthesized_from_model_gateway: true }] } });
    if (url.pathname === '/api/model-gateway/provider-administration') return route.fulfill({ json: { schema: schemas.model, can_manage: !readonly, items: [model] } });
    if (url.pathname.endsWith('/impact')) {
      state.impacts++; const isModel = url.pathname.includes('/model-gateway/'); const synthesized = !isModel && url.pathname.includes('/model-one/');
      return route.fulfill({ json: { provider: isModel ? model : synthesized ? { ...agent, id: 'model-one', synthesized_from_model_gateway: true } : agent, expected_revision: isModel ? 'model-fingerprint' : state.revision, owner: isModel ? 'model_gateway_binding' : synthesized ? 'model_gateway' : 'agent_provider', can_manage: !readonly && !unavailable && !synthesized, available: !unavailable, organization_id: 'org', workspace_id: 'ws', total: 2, consumers: [{ kind: 'session', id: 'session-one', status: 'running' }, { kind: 'runtime', id: 'cli', revision: 1 }], effect: 'Future routing changes; running sessions and remote accounts are not changed.', limitations: 'Explicit retained references only; no archive/delete support.', blockers: unavailable ? ['Inventory unavailable'] : [] } });
    }
    if (method === 'PUT') {
      state.writes.push({ path: url.pathname, project: url.searchParams.get('project_id'), expected: url.searchParams.get('expected_revision'), payload: route.request().postDataJSON() });
      return route.fulfill(state.fail ? { status: 409, json: { detail: 'Binding changed; reload' } } : { json: { item: {} } });
    }
    return route.fulfill({ json: { items: [] } });
  });
  await page.goto(`http://127.0.0.1:18766/projects/home/operations?${query}`);
  await page.locator('#refresh-provider-bindings').click();
  await expect(page.locator('#provider-binding-content')).toContainText('Shared workspace org/ws');
  await expect(page.locator('[data-binding-detail]')).toContainText('Owner:');
  return state;
}
async function review(page) { await page.locator('[data-binding-preview]').click(); await expect(page.locator('[data-binding-save]')).toBeEnabled(); }

test('provider disable previews consumers and uses the scoped revision-checked binding API', async ({ page }) => {
  const state = await mount(page);
  await page.locator('[name=lifecycle]').selectOption('disabled'); await review(page);
  await expect(page.locator('[data-binding-impact]')).toContainText('session-one');
  await expect(page.locator('[data-binding-impact]')).toContainText('active → disabled');
  await page.locator('[data-binding-save]').click(); await resolveAction(page);
  await expect.poll(() => state.writes.length).toBe(1);
  expect(state.writes[0]).toMatchObject({ path: '/api/agent-providers/runner', project: 'home', expected: '1', payload: { lifecycle: 'disabled', credential_refs: ['credential-one'] } });
  expect(state.writes[0].payload).not.toHaveProperty('revision');
});

test('ModelGateway lifecycle uses its own owner endpoint and preserves credential references', async ({ page }) => {
  const state = await mount(page, { query: 'model_provider_id=model-one' });
  await page.locator('[name=status]').selectOption('disabled'); await review(page);
  await page.locator('[data-binding-save]').click(); await resolveAction(page);
  await expect.poll(() => state.writes.length).toBe(1);
  expect(state.writes[0]).toMatchObject({ path: '/api/model-gateway/providers/model-one', expected: 'model-fingerprint', payload: { status: 'disabled', credential_required: true, credential_ref: 'credential-one', base_url: null } });
});

test('synthesized provider links to its owning ModelGateway binding and cannot be edited as an AgentProvider', async ({ page }) => {
  await mount(page, { query: 'provider_id=model-one' });
  await expect(page.locator('[data-binding-detail]')).toContainText('synthesized view is read-only');
  await expect(page.getByRole('link', { name: 'Manage owning ModelGateway binding' })).toHaveAttribute('href', '/projects/home/operations?model_provider_id=model-one');
  await expect(page.locator('[data-binding-form]')).toHaveCount(0);
});

test('unavailable consumer inventory and missing authority disable binding controls', async ({ page }) => {
  await mount(page, { unavailable: true });
  await expect(page.locator('[data-binding-detail]')).toContainText('Inventory unavailable');
  await expect(page.locator('[data-binding-preview]')).toBeDisabled();
  await expect(page.locator('[name=lifecycle]')).toBeDisabled();
});

test('capability grants cannot exceed declarations and stale preview cannot replace newer state', async ({ page }) => {
  const state = await mount(page);
  await page.locator('[name=granted_capabilities]').selectOption(['agent_execution', 'shell_tools']);
  await page.locator('[data-binding-preview]').click();
  await expect(page.locator('[data-validation-summary]')).toContainText('must also be declared');
  await page.locator('[name=granted_capabilities]').selectOption(['agent_execution']);
  state.revision = 2; await page.locator('[data-binding-preview]').click();
  await expect(page.locator('[data-validation-summary]')).toContainText('reload and review');
  await expect(page.locator('[data-binding-save]')).toBeDisabled(); expect(state.writes).toEqual([]);
});

test('conflicted updates preserve edits and prevent a blind retry', async ({ page }) => {
  const state = await mount(page); state.fail = true;
  await page.locator('[name=display_name]').fill('Changed Runner'); await review(page);
  await page.locator('[data-binding-save]').click(); await resolveAction(page);
  await expect(page.locator('[data-validation-summary]')).toContainText('Binding changed');
  await expect(page.locator('[name=display_name]')).toHaveValue('Changed Runner');
  await expect(page.locator('[data-binding-save]')).toBeDisabled();
});

test('dirty refresh can be cancelled and editor fits desktop and phone', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 }); await mount(page);
  expect((await page.locator('[data-binding-form]').boundingBox()).width).toBeGreaterThan(850);
  await page.locator('[name=display_name]').fill('Unsaved Runner');
  page.once('dialog', dialog => dialog.dismiss()); await page.locator('#refresh-provider-bindings').click();
  await expect(page.locator('[name=display_name]')).toHaveValue('Unsaved Runner');
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.locator('[data-binding-form]').evaluate(form => { const box = form.getBoundingClientRect(); return [...form.querySelectorAll('input,select')].every(node => node.getBoundingClientRect().right <= box.right); })).toBe(true);
});

test('late impact after A-B-A cannot populate a departed editor', async ({ page }) => {
  let release; let pending = false;
  await mount(page, { intercept: async (route, url, state) => {
    if (url.pathname.endsWith('/impact') && state.impacts >= 1 && !pending) {
      pending = true; await new Promise(resolve => { release = resolve; });
      await route.fulfill({ json: { available: true, can_manage: true, expected_revision: 1, effect: 'STALE IMPACT' } }); return true;
    }
  } });
  await page.locator('[data-binding-preview]').click(); await expect.poll(() => pending).toBe(true);
  await page.evaluate(() => { for (const projectId of ['other', 'home']) window.dispatchEvent(new CustomEvent('codex:project-changed', { detail: { projectId } })); });
  release();
  await expect(page.locator('[data-binding-form]')).toBeVisible();
  await expect(page.locator('[data-binding-save]')).toBeDisabled();
  await expect(page.locator('#provider-binding-content')).not.toContainText('STALE IMPACT');
});
