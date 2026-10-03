const { resolveAction } = require('./action_confirmation_helpers');
const { test, expect } = require('@playwright/test');
const fs = require('node:fs'); const path = require('node:path');
const index = fs.readFileSync(path.join(__dirname, '../../static/index.html'), 'utf8');
const marker = index.indexOf('<h2>Encryption Keys</h2>');
const card = index.slice(index.lastIndexOf('<div class="developer-card">', marker), index.indexOf('<div class="developer-card">', marker));
const fixture = fs.readFileSync(path.join(__dirname, 'product_workspaces_fixture.html'), 'utf8').replace('</body>', `${card}<script type="module" src="/static/crypto_key_admin.js"></script></body>`);
const key = { id: 'key-backup', purpose: 'backup', status: 'active', current_version: 2, backend_type: 'local', scope: { organization_id: 'org', workspace_id: 'ws' }, versions: [{ version: 1, status: 'decrypt_only', backend_ref: 'backend-reference-v1' }, { version: 2, status: 'active', backend_ref: 'backend-reference-v2' }] };
async function mount(page, intercept) {
  const state = { count: 1, unavailable: false, writes: [], reads: [], events: [], broken: false };
  await page.route('**/projects/**', route => route.request().resourceType() === 'document' ? route.fulfill({ contentType: 'text/html', body: fixture }) : route.continue());
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url());
    if (intercept && await intercept(route, url)) return;
    const method = route.request().method();
    if (url.pathname.startsWith('/api/crypto')) {
      state.reads.push({ path: url.pathname, project: url.searchParams.get('project_id'), version: url.searchParams.get('version') });
      if (method === 'POST') { state.writes.push({ path: url.pathname, project: url.searchParams.get('project_id') }); return route.fulfill({ json: { previous_version: 2, current_version: 3 } }); }
    }
    if (url.pathname.endsWith('/usage')) state.events.push('impact');
    if (url.pathname.endsWith('/usage')) return route.fulfill(state.unavailable ? { status: 503, json: { detail: 'Impact unavailable' } } : { json: { available: true, count: state.count, blocking_count: state.count, items: state.count ? [{ object_id: 'backup-1', label: 'Encrypted recovery backup', scope: 'shared_workspace', relationship: 'encrypted_content', versions: [1], broken: state.broken }] : [], coverage: ['backup_envelopes'], limitations: ['External copies are not enumerated.'] } });
    if (url.pathname === '/api/crypto/keys') return route.fulfill({ json: { items: [key] } });
    if (url.pathname === '/api/crypto/backend-health') return route.fulfill({ json: { local: true } });
    if (url.pathname === '/api/crypto/manifest') return route.fulfill({ json: { items: [{ key_id: key.id, versions: [1, 2] }] } });
    return route.fulfill({ json: { items: [] } });
  });
  await page.goto('http://127.0.0.1:18766/projects/home/configuration?key_id=key-backup');
  await page.locator('#refresh-crypto-keys').click();
  await expect(page.locator('[data-crypto-key-row]')).toBeVisible();
  return state;
}

test('key consumers and manifest deep links expose metadata and explain broken dependencies', async ({ page }) => {
  const state = await mount(page); state.broken = true;
  await page.locator('[data-crypto-usage]').click();
  await expect(page.locator('[data-key-usage-result]')).toContainText('backup-1');
  await expect(page.locator('[data-key-usage-result]')).toContainText('Broken:');
  await expect(page.locator('[data-key-usage-result] [data-key-link]')).toHaveAttribute('href', '/projects/home/configuration?key_id=key-backup&key_version=1');
  await expect(page.locator('#crypto-key-manifest a')).toHaveAttribute('href', '/projects/home/configuration?key_id=key-backup');
  expect(state.reads.every(row => row.project === 'home')).toBe(true);
});

test('positive dependencies and unavailable impact block destructive confirmations and writes', async ({ page }) => {
  const state = await mount(page); let dialogs = 0;
  page.on('dialog', async dialog => { dialogs++; await dialog.dismiss(); });
  await page.locator('[data-crypto-revoke]').click();
  await expect(page.locator('#crypto-key-status')).toContainText('Revocation blocked');
  await page.locator('[data-crypto-revoke-version]').click();
  expect(state.reads.some(row => row.version === '1')).toBe(true);
  state.unavailable = true;
  await page.locator('[data-crypto-revoke]').click();
  await expect(page.locator('#crypto-key-status')).toContainText('blocked until dependency impact');
  expect(state.writes).toEqual([]); expect(dialogs).toBe(0);
});

test('rotation displays dependencies before confirmation and preserves scoped mutation', async ({ page }) => {
  const state = await mount(page);
  await page.locator('[data-crypto-rotate]').click();
  await expect(page.locator('[data-action-impact]')).toContainText('backup-1');
  state.events.push('confirm'); await resolveAction(page);
  await expect.poll(() => state.writes.length).toBe(1);
  expect(state.writes[0]).toEqual({ path: '/api/crypto/keys/key-backup/rotate', project: 'home' });
  expect(state.events).toEqual(['impact', 'confirm']);
});

test('unreferenced version retirement uses a fresh impact read before confirmation', async ({ page }) => {
  const state = await mount(page); state.count = 0;
  page.on('dialog', async dialog => dialog.accept(dialog.type() === 'prompt' ? 'retained copies migrated' : undefined));
  await page.locator('[data-crypto-revoke-version]').click(); await resolveAction(page);
  await expect.poll(() => state.writes.length).toBe(1);
  expect(state.writes[0].path).toContain('/versions/1/revoke');
});

for (const returnsToOrigin of [false, true]) test(`late impact cannot authorize a key action after Project navigation${returnsToOrigin ? ' A-B-A' : ''}`, async ({ page }) => {
  let held; let entered;
  const started = new Promise(resolve => { entered = resolve; });
  const state = await mount(page, async (route, url) => {
    if (url.pathname.endsWith('/usage')) { entered(); await new Promise(resolve => { held = resolve; }); await route.fulfill({ json: { available: true, count: 0, blocking_count: 0 } }); return true; }
  });
  let dialogs = 0; page.on('dialog', async dialog => { dialogs++; await dialog.dismiss(); });
  await page.locator('[data-crypto-revoke]').click(); await started;
  await page.evaluate(back => {
    window.dispatchEvent(new CustomEvent('codex:project-changed', { detail: { projectId: 'other' } }));
    if (back) window.dispatchEvent(new CustomEvent('codex:project-changed', { detail: { projectId: 'home' } }));
  }, returnsToOrigin);
  held();
  await expect(page.locator('#crypto-key-status')).toContainText('managed key(s)');
  await expect(page.locator('[data-key-usage-result]')).not.toContainText('0 canonical dependency');
  expect(state.writes).toEqual([]); expect(dialogs).toBe(0);
});

test('key metadata refresh defers edits and failed creation keeps the form', async ({ page }) => {
  await mount(page, async (route, url) => {
    if (url.pathname === '/api/crypto/keys' && route.request().method() === 'POST') {
      await route.fulfill({ status: 409, json: { detail: 'Key scope changed' } }); return true;
    }
  });
  await page.locator('#crypto-key-create-panel summary').click();
  await page.locator('#crypto-key-project-id').fill('project-draft');
  await page.locator('#refresh-crypto-keys').click();
  await expect(page.locator('#crypto-key-status')).toContainText('Refresh deferred');
  await page.locator('#create-crypto-key').click(); await resolveAction(page);
  await expect(page.locator('#crypto-key-status')).toContainText('Key scope changed');
  await expect(page.locator('#crypto-key-project-id')).toHaveValue('project-draft');
  page.once('dialog', dialog => dialog.accept());
  await page.locator('[data-key-discard]').click();
  await expect(page.locator('#crypto-key-project-id')).toHaveValue('');
});

test('late key creation retains newer scope metadata', async ({ page }) => {
  let held;
  await mount(page, async (route, url) => {
    if (url.pathname === '/api/crypto/keys' && route.request().method() === 'POST') { held = route; return true; }
  });
  await page.locator('#crypto-key-create-panel summary').click();
  await page.locator('#crypto-key-project-id').fill('submitted-project');
  await page.locator('#create-crypto-key').click(); await resolveAction(page);
  await expect.poll(() => Boolean(held)).toBe(true);
  await page.locator('#crypto-key-project-id').fill('newer-project');
  await held.fulfill({ json: { item: key } });
  await expect(page.locator('#crypto-key-status')).toContainText('Newer metadata remains unsaved');
  await expect(page.locator('#crypto-key-project-id')).toHaveValue('newer-project');
  await expect(page.locator('#crypto-key-create-panel [data-dirty-editor-status]')).toHaveText('Unsaved changes');
});

test('typing during initial key catalog load retains scope while enabling backend selection', async ({ page }) => {
  const held = []; let released = false;
  const mounting = mount(page, async (route, url) => {
    if (!released && url.pathname === '/api/crypto/backend-health') { held.push(route); return true; }
  });
  await expect.poll(() => held.length).toBeGreaterThan(0);
  await page.locator('#crypto-key-create-panel summary').click();
  await page.locator('#crypto-key-project-id').fill('Typed while loading');
  released = true;
  await Promise.all(held.map(route => route.fulfill({ json: { local: true } })));
  await mounting;
  await expect(page.locator('#crypto-key-backend')).toHaveValue('local');
  await expect(page.locator('#crypto-key-project-id')).toHaveValue('Typed while loading');
  await expect(page.locator('#crypto-key-create-panel [data-dirty-editor-status]')).toHaveText('Unsaved changes');
});
