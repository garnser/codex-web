const { test, expect } = require('@playwright/test');
const fs = require('node:fs'); const path = require('node:path');
const index = fs.readFileSync(path.join(__dirname, '../../static/index.html'), 'utf8');
const base = fs.readFileSync(path.join(__dirname, 'product_workspaces_fixture.html'), 'utf8');
for (const [kind, marker, module, refresh, pageName] of [
  ['model_provider', 'id="refresh-model-gateway"', 'model_gateway_admin', 'refresh-model-gateway', 'agents'],
  ['action_provider', 'id="refresh-action-providers"', 'action_provider_admin', 'refresh-action-providers', 'integrations'],
  ['extension', 'id="refresh-extensions"', 'extension_admin', 'refresh-extensions', 'integrations'],
]) test(`${kind} consumer links focus exact metadata and link back to the Project Secret`, async ({ page }) => {
  const at = index.indexOf(marker); expect(at).toBeGreaterThan(0);
  const card = index.slice(index.lastIndexOf('<div class="developer-card">', at), index.indexOf('<div class="developer-card">', at));
  const trimmed = base.replace(/<div class="developer-card" id="(?:model|extension)-card">.*?<\/div>/g, '');
  const fixture = trimmed.replace('</body>', `${card}<script type="module" src="/static/${module}.js"></script></body>`);
  await page.route('**/projects/**', route => route.request().resourceType() === 'document' ? route.fulfill({ contentType: 'text/html', body: fixture }) : route.continue());
  await page.route('**/api/**', route => {
    const url = new URL(route.request().url());
    if (url.pathname === '/api/model-gateway/policy') return route.fulfill({ json: { item: {} } });
    if (url.pathname === '/api/model-gateway/providers') return route.fulfill({ json: { items: [{ id: 'consumer-one', display_name: 'Model Provider', credential_ref: 'secret-shared' }] } });
    if (url.pathname === '/api/action-providers') return route.fulfill({ json: { items: [{ binding: { id: 'consumer-one', credential_ref: 'secret-shared' }, actions: [] }] } });
    if (url.pathname === '/api/extensions') return route.fulfill({ json: { items: [{ id: 'consumer-one', lifecycle: 'enabled', secret_bindings: { auth: 'secret-shared' }, manifest: { id: 'package-one', version: '1.0.0' } }] } });
    if (url.pathname === '/api/identity/me') return route.fulfill({ json: { principal_kind: 'human', roles: ['member'], assurance: 'primary' } });
    return route.fulfill({ json: { items: [], waits: [] } });
  });
  await page.goto(`http://127.0.0.1:18766/projects/home/${pageName}?consumer_type=${kind}&consumer_id=consumer-one`);
  await page.locator('#' + refresh).click();
  const row = page.locator(`[data-reference-kind="${kind}"][data-reference-id="consumer-one"]`);
  await expect(row).toBeVisible(); await expect(row).toBeFocused();
  await expect(row.getByRole('link', { name: 'secret-shared', exact: true })).toHaveAttribute('href', '/projects/home/secrets?secret_id=secret-shared');
});
