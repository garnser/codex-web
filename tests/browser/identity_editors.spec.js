const { test, expect } = require('@playwright/test');
const { resolveAction } = require('./action_confirmation_helpers');
const fs = require('node:fs'), path = require('node:path');
const html = fs.readFileSync(path.join(__dirname, '../../static/index.html'), 'utf8');
const detail = {
  actor: { identity_id: 'human-a', organization_id: 'org-a', workspace_id: 'workspace-a', assurance: 'mfa' },
  state: {
    organizations: ['a', 'b'].map(id => ({ id: `org-${id}`, name: `Organization ${id}` })),
    workspaces: ['a', 'b'].map(id => ({ id: `workspace-${id}`, organization_id: `org-${id}`, name: `Workspace ${id}` })),
    humans: [{ id: 'human-a', display_name: 'Operator' }],
    services: [{ id: 'service-a', name: 'Delivery service' }],
    teams: [{ id: 'team-a', name: 'Team A', organization_id: 'org-a', workspace_id: 'workspace-a' }],
    memberships: [{ identity_id: 'service-a', principal_kind: 'service', organization_id: 'org-a', workspace_id: 'workspace-a' }],
  },
};
async function mount(page, mutation = null) {
  await page.route('**/identity-editor-fixture', route => route.fulfill({ contentType: 'text/html', body: '<body data-project-id="one"></body>' }));
  await page.route('**/api/**', route => mutation ? mutation(route) : route.fulfill({ json: {} }));
  await page.goto('http://127.0.0.1:18766/identity-editor-fixture');
  await page.evaluate(async markup => {
    const parsed = new DOMParser().parseFromString(markup, 'text/html');
    document.body.innerHTML = parsed.querySelector('#identity-authority-panel').outerHTML;
    document.querySelectorAll('details').forEach(node => { node.open = true; });
    await import('/static/identity_authority_admin.js');
  }, html);
  await expect(page.locator('[data-identity-discard]')).toHaveCount(6);
  await page.evaluate(detail => window.dispatchEvent(new CustomEvent('codex:identity-state-rendered', { detail })), detail);
  await expect(page.locator('#identity-membership-org')).toHaveValue('org-a');
}

test('identity refresh and dependent scope selection preserve other drafts; discard restores option catalogs', async ({ page }) => {
  await mount(page);
  await page.locator('#identity-token-scopes').fill('work_item.read');
  await page.locator('#identity-membership-workspace').selectOption('workspace-a');
  await page.locator('#identity-membership-teams').selectOption('team-a');
  await page.locator('#identity-token-org').selectOption('org-b');
  await expect(page.locator('#identity-membership-teams')).toHaveValues(['team-a']);
  await page.evaluate(detail => window.dispatchEvent(new CustomEvent('codex:identity-state-rendered', { detail })), detail);
  await expect(page.locator('#identity-authority-status')).toContainText('Refresh deferred');
  await expect(page.locator('#identity-token-scopes')).toHaveValue('work_item.read');
  page.once('dialog', dialog => dialog.dismiss());
  await page.locator('[data-identity-discard=token]').click();
  await expect(page.locator('#identity-token-org')).toHaveValue('org-b');
  page.once('dialog', dialog => dialog.accept());
  await page.locator('[data-identity-discard=token]').click();
  await expect(page.locator('#identity-token-org')).toHaveValue('org-a');
  await expect(page.locator('#identity-token-workspace')).toHaveValue('workspace-a');
  await expect(page.locator('#identity-token-service')).toHaveValue('service-a');
  await expect(page.locator('#identity-token-scopes')).toHaveValue('');
  await expect(page.locator('[data-identity-discard=token]').locator('..').locator('[data-dirty-editor-status]')).toHaveText('No unsaved changes');
});

test('identity failures preserve edits and accepted saves only acknowledge submitted values', async ({ page }) => {
  let fail = true, held;
  await mount(page, route => {
    if (fail) return route.fulfill({ status: 409, json: { detail: 'Organization already exists' } });
    held = route;
  });
  await page.locator('#identity-create-org-name').fill('Submitted Organization');
  await page.locator('#identity-create-org').click(); await resolveAction(page);
  await expect(page.locator('#identity-authority-status')).toContainText('Organization already exists');
  const status = page.locator('[data-identity-discard=org]').locator('..').locator('[data-dirty-editor-status]');
  await expect(status).toHaveText('Unsaved changes');
  fail = false;
  await page.locator('#identity-create-org').click(); await resolveAction(page);
  await expect.poll(() => Boolean(held)).toBe(true);
  await page.locator('#identity-create-org-name').fill('Newer Organization');
  await held.fulfill({ json: { item: { id: 'new-org' } } });
  await expect(page.locator('#identity-authority-status')).toContainText('Created organization');
  await expect(status).toHaveText('Unsaved changes');
  page.once('dialog', dialog => dialog.accept());
  await page.locator('[data-identity-discard=org]').click();
  await expect(page.locator('#identity-create-org-name')).toHaveValue('Submitted Organization');
  await expect(status).toHaveText('No unsaved changes');
});

test('late token creation does not reveal its one-time result after a Project change', async ({ page }) => {
  let held;
  await mount(page, route => { held = route; });
  await page.locator('#identity-token-scopes').fill('work_item.read');
  await page.locator('#identity-create-token').click(); await resolveAction(page);
  await expect.poll(() => Boolean(held)).toBe(true);
  await page.evaluate(() => window.dispatchEvent(new CustomEvent('codex:project-changed', { detail: { projectId: 'two' } })));
  await held.fulfill({ json: { token_id: 'fixture-token-id', token: 'synthetic-fixture-value' } });
  await expect(page.locator('#identity-create-token')).toBeEnabled();
  await expect(page.locator('#identity-created-token')).toBeEmpty();
  await expect(page.locator('#identity-created-token-result')).toBeHidden();
  expect(await page.evaluate(() => `${JSON.stringify(localStorage)} ${JSON.stringify(sessionStorage)}`)).not.toContain('synthetic-fixture-value');
});
