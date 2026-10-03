const { test, expect } = require('@playwright/test');
const { resolveAction } = require('./action_confirmation_helpers');

async function mount(page, mutate = null) {
  const installation = { id: 'extension-a', lifecycle: 'disabled', configuration_record_ids: ['config-a'], secret_bindings: { api: 'secret-a' }, manifest: { id: 'example.extension', version: '1.0', capabilities: { requested: ['read.records'] }, configuration: { schema: 'config.json', secret_refs: ['api'] } } };
  const writes = [];
  await page.route('**/api/**', route => {
    const request = route.request(), path = new URL(request.url()).pathname;
    if (request.method() !== 'GET') {
      writes.push({ path, body: request.postDataJSON() });
      return mutate ? mutate(route) : route.fulfill({ json: {} });
    }
    if (path === '/api/identity/me') return route.fulfill({ json: { identity_id: 'admin', principal_kind: 'human', assurance: 'mfa', roles: ['admin'] } });
    const items = path === '/api/extensions' ? [installation]
      : path === '/api/secrets' ? ['a', 'b'].map(id => ({ id: `secret-${id}`, name: `Credential ${id}`, status: 'active' }))
      : path === '/api/configuration/records' ? ['a', 'b'].map(id => ({ id: `config-${id}`, key: `setting.${id}`, state: 'published', revision: 1 }))
      : path === '/api/resources' ? [{ id: 'resource-a', name: 'Repository A', resource_type: 'repository' }] : [];
    return route.fulfill({ json: { items } });
  });
  await page.route('**/extension-editor-fixture', route => route.fulfill({ contentType: 'text/html', body: '<body data-project-id="one"><details id="developer-panel" open><button id="refresh-extensions">Refresh</button><div id="extension-admin-status"></div><div id="extension-admin-list"></div></details></body>' }));
  await page.goto('http://127.0.0.1:18766/extension-editor-fixture');
  await page.evaluate(async () => {
    await import('/static/extension_configuration_admin.js');
    await import('/static/extension_admin.js');
  });
  await expect(page.locator('[data-extension-secret-slot]')).toHaveValue('secret-a');
  await expect(page.locator('[data-extension-discard]')).toHaveCount(2);
  return writes;
}

test('extension reference and resource selections survive refresh and require deliberate discard', async ({ page }) => {
  await mount(page);
  const config = page.locator('[data-extension-configuration]');
  await config.locator('[data-extension-secret-slot]').selectOption('secret-b');
  await config.locator('[data-extension-config-records]').selectOption('config-b');
  await page.locator('[data-extension-resource-scope]').selectOption('resource-a');
  await page.locator('#refresh-extensions').click();
  await expect(page.locator('#extension-admin-status')).toContainText('Refresh deferred');
  await expect(config.locator('[data-extension-secret-slot]')).toHaveValue('secret-b');
  await expect(page.locator('[data-extension-resource-scope]')).toHaveValues(['resource-a']);
  page.once('dialog', dialog => dialog.dismiss());
  await config.locator('[data-extension-discard]').click();
  await expect(config.locator('[data-dirty-editor-status]')).toHaveText('Unsaved changes');
  page.once('dialog', dialog => dialog.accept());
  await config.locator('[data-extension-discard]').click();
  await expect(config.locator('[data-extension-secret-slot]')).toHaveValue('secret-a');
  await expect(config.locator('[data-extension-config-records]')).toHaveValues(['config-a']);
  page.once('dialog', dialog => dialog.accept());
  expect(await page.evaluate(async () => (await import('/static/dirty_editor.js')).confirmDiscard())).toBe(true);
  await expect(page.locator('[data-extension-resource-scope]')).toHaveValues([]);
});

test('extension failed save retains values and late success preserves newer selections', async ({ page }) => {
  let fail = true, held;
  const writes = await mount(page, route => {
    if (fail) return route.fulfill({ status: 409, json: { detail: 'Canonical extension changed' } });
    held = route;
  });
  const config = page.locator('[data-extension-configuration]');
  await config.locator('[data-extension-secret-slot]').selectOption('secret-b');
  await config.locator('[data-extension-config-save]').click(); await resolveAction(page);
  await expect(page.locator('#extension-admin-status')).toContainText('Canonical extension changed');
  await expect(config.locator('[data-extension-secret-slot]')).toHaveValue('secret-b');
  await expect(config.locator('[data-dirty-editor-status]')).toHaveText('Unsaved changes');
  fail = false;
  await config.locator('[data-extension-config-save]').click(); await resolveAction(page);
  await expect.poll(() => Boolean(held)).toBe(true);
  await config.locator('[data-extension-secret-slot]').selectOption('secret-a');
  await held.fulfill({ json: {} });
  await expect(page.locator('#extension-admin-status')).toContainText('Refresh deferred');
  await expect(config.locator('[data-extension-secret-slot]')).toHaveValue('secret-a');
  await expect(config.locator('[data-dirty-editor-status]')).toHaveText('Unsaved changes');
  page.once('dialog', dialog => dialog.accept());
  await config.locator('[data-extension-discard]').click();
  await expect(config.locator('[data-extension-secret-slot]')).toHaveValue('secret-b');
  await expect(config.locator('[data-dirty-editor-status]')).toHaveText('No unsaved changes');
  expect(writes[1].body).toEqual({ configuration_record_ids: ['config-a'], secret_bindings: { api: 'secret-b' } });
  expect(await page.evaluate(() => `${JSON.stringify(localStorage)} ${JSON.stringify(sessionStorage)}`)).not.toContain('secret-b');
});
