const { test, expect } = require('@playwright/test');

const fixture = `<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/static/styles.css"></head>
<body data-project-id="project-a"><details id="developer-panel" open><button id="refresh-developer" type="button">Refresh all</button>
<section class="developer-card"><button id="refresh-action-providers" type="button" class="ghost-button">Refresh</button>
<div id="action-provider-status" class="form-result" aria-live="polite"></div><div id="action-provider-list" class="comm-log"></div></section></details>
<script type="module" src="/static/action_provider_admin.js"></script></body></html>`;

async function mount(page, { patchStatus = 200, holdPatch = false, patchAbort = false } = {}) {
  let enabled = false;
  const patches = [];
  let releasePatch;
  const patchGate = holdPatch ? new Promise(resolve => { releasePatch = resolve; }) : Promise.resolve();
  await page.route('**/action-provider-admin-fixture', route => route.fulfill({ contentType: 'text/html', body: fixture }));
  await page.route('**/api/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === '/api/action-providers' && request.method() === 'GET') {
      return route.fulfill({ json: { items: [{
        status: enabled ? 'available' : 'disabled',
        binding: {
          id: 'binding-a', provider_type: 'github', provider_instance: 'github.com', enabled,
          organization_id: 'org-a', workspace_id: 'workspace-a', project_id: 'project-a',
          resource_ids: ['repository-a'], credential_ref: 'secret-github', security_policy: {},
        },
        actions: [{
          action_id: 'repository.write', title: 'Repository write', risk_class: 'high', description: '',
          capabilities: {}, required_authority: ['repository.write'], required_resource_types: ['repository'],
          credential_required: true, credential_purpose: 'github-api-token', expected_evidence: [],
          reversible: false, timeout_seconds: 30, retry_max_attempts: 1, filesystem_access: 'read',
          network_access: true, process_access: false,
        }],
      }] } });
    }
    if (path === '/api/resources') return route.fulfill({ json: { items: [{ id: 'repository-a', name: 'Primary repository' }] } });
    if (path === '/api/projects') return route.fulfill({ json: [{ id: 'project-a', name: 'Delivery project' }] });
    if (path === '/api/action-providers/bindings/binding-a' && request.method() === 'PATCH') {
      const body = request.postDataJSON();
      patches.push(body);
      await patchGate;
      if (patchAbort) return route.abort('failed');
      if (patchStatus !== 200) return route.fulfill({ status: patchStatus, json: { detail: `provider update ${patchStatus}` } });
      enabled = body.enabled;
      return route.fulfill({ json: { item: { id: 'binding-a', enabled } } });
    }
    return route.fulfill({ status: 404, json: { detail: 'fixture route missing' } });
  });
  await page.goto('http://127.0.0.1:18766/action-provider-admin-fixture');
  await expect(page.getByRole('button', { name: 'Enable ActionProvider binding binding-a' })).toBeVisible();
  return { patches, releasePatch: () => releasePatch?.() };
}

async function acceptToggle(page, label = 'Enable ActionProvider binding') {
  const dialog = page.getByRole('dialog', { name: label });
  await dialog.getByRole('checkbox').check();
  await dialog.getByRole('button', { name: label }).click();
}

test('binding toggle reviews exact canonical target, supports keyboard cancellation, and refreshes success', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const { patches } = await mount(page);
  const button = page.getByRole('button', { name: 'Enable ActionProvider binding binding-a' });
  await button.focus();
  await page.keyboard.press('Enter');
  let dialog = page.getByRole('dialog', { name: 'Enable ActionProvider binding' });
  await expect(dialog).toContainText('github/github.com');
  await expect(dialog).toContainText('binding binding-a');
  await expect(dialog).toContainText('tenant org-a/workspace-a');
  await expect(dialog).toContainText('Delivery project (project-a)');
  await expect(dialog).toContainText('Primary repository (repository-a)');
  await expect(dialog).toContainText('does not prepare or execute an action');
  await page.keyboard.press('Escape');
  // Native focus restoration can precede the asynchronous close handler.
  // Wait for cancellation cleanup before asking for a fresh confirmation.
  await expect(page.locator('dialog.action-confirmation')).toHaveCount(0);
  await expect(button).toBeFocused();
  expect(patches).toEqual([]);

  await page.keyboard.press('Enter');
  await acceptToggle(page);
  await expect.poll(() => patches).toEqual([{ enabled: true }]);
  await expect(page.locator('#action-provider-status')).toContainText('Enable succeeded');
  await expect(page.locator('#action-provider-list')).toContainText('Enabled: yes');
  await expect(page.getByRole('button', { name: 'Disable ActionProvider binding binding-a' })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

test('binding toggle exposes a disabled pending state until canonical refresh completes', async ({ page }) => {
  const { releasePatch } = await mount(page, { holdPatch: true });
  try {
    await page.getByRole('button', { name: 'Enable ActionProvider binding binding-a' }).click();
    await acceptToggle(page);
    await expect(page.locator('#action-provider-status')).toContainText('Enable pending');
    const card = page.locator('[data-reference-kind="action_provider"][data-reference-id="binding-a"]');
    await expect(card).toHaveAttribute('aria-busy', 'true');
    await expect(card.getByRole('button', { name: 'Enable ActionProvider binding binding-a' })).toBeDisabled();
  } finally {
    releasePatch();
  }
  await expect(page.locator('#action-provider-status')).toContainText('Enable succeeded');
});

for (const [status, message] of [[403, 'Denied:'], [404, 'Binding missing:'], [409, 'Conflict:']]) {
  test(`binding toggle reports canonical ${status} failure without changing displayed state`, async ({ page }) => {
    await mount(page, { patchStatus: status });
    await page.getByRole('button', { name: 'Enable ActionProvider binding binding-a' }).click();
    await acceptToggle(page);
    await expect(page.locator('#action-provider-status')).toContainText(message);
    await expect(page.locator('#action-provider-list')).toContainText('Enabled: no');
    await expect(page.getByRole('button', { name: 'Enable ActionProvider binding binding-a' })).toBeEnabled();
  });
}

test('unknown transport outcome requires a canonical refresh before another toggle', async ({ page }) => {
  await mount(page, { patchAbort: true });
  await page.getByRole('button', { name: 'Enable ActionProvider binding binding-a' }).click();
  await acceptToggle(page);
  await expect(page.locator('#action-provider-status')).toContainText('Update outcome unknown');
  await expect(page.getByRole('button', { name: 'Refresh required for ActionProvider binding binding-a' })).toBeDisabled();
  await page.locator('#refresh-action-providers').click();
  await expect(page.getByRole('button', { name: 'Enable ActionProvider binding binding-a' })).toBeEnabled();
});
