const { resolveAction } = require('./action_confirmation_helpers');
const { test, expect } = require('@playwright/test');
const fs = require('node:fs'); const path = require('node:path');
const schema = require('./recovery_policy_schema.json');
const index = fs.readFileSync(path.join(__dirname, '../../static/index.html'), 'utf8');
const at = index.indexOf('<h2>Recovery Policy & Evidence</h2>');
const card = index.slice(index.lastIndexOf('<div class="developer-card">', at), index.indexOf('<div class="developer-card">', at));
const fixture = fs.readFileSync(path.join(__dirname, 'product_workspaces_fixture.html'), 'utf8').replace('<button id="sidebar-toggle" type="button">', '<button id="sidebar-toggle" class="sidebar-toggle" type="button">').replace('<link rel="stylesheet" href="/static/styles.css">', '<link rel="stylesheet" href="/static/design_tokens.css"><link rel="stylesheet" href="/static/workspace_components.css"><link rel="stylesheet" href="/static/styles.css">').replace('</body>', `${card}<script type="module" src="/static/recovery_policy_ui.js"></script></body>`);
const policy = { id: 'recovery-policy-default', version: '1', deployment_mode: 'local', backup_key_id: 'key-backup', destination_id: 'local', backup_interval_seconds: 3600, restore_verification_interval_seconds: 86400, retention_count: 30, require_audit_integrity: true, require_key_manifest: true, allow_point_in_time_recovery: false, objectives: [{ data_class: 'canonical_state', rpo_seconds: 3600, rto_seconds: 14400 }] };
async function mount(page, { readonly = false, intercept } = {}) {
  const state = { writes: [], preview: [], current: structuredClone(policy), fail: false, failPreview: false };
  await page.route('**/projects/**', route => route.request().resourceType() === 'document' ? route.fulfill({ contentType: 'text/html', body: fixture }) : route.continue());
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url()); const method = route.request().method();
    if (intercept && await intercept(route, url)) return;
    if (url.pathname === '/api/recovery/status') return route.fulfill({ json: { policy: state.current, policy_control: { schema, can_configure: !readonly, expected_fingerprint: 'current-fingerprint', organization_id: 'org', workspace_id: 'ws', destinations: [{ id: 'local' }], scheduler_available: true, history: [{ id: 'change-one', policy, actor_id: 'operator', occurred_at: 1 }] }, health: { recovery_qualified: false, blockers: ['Restore verification required'] }, backups: [{ id: 'backup-one', key_id: 'key-backup', key_version: 1, key_manifest: [{ key_id: 'key-backup', versions: [1] }], destination_ref: 'hidden-destination-path' }], verifications: [] } });
    if (url.pathname === '/api/crypto/keys') return route.fulfill({ json: { items: [{ id: 'key-backup', purpose: 'backup', status: 'active', scope: {} }] } });
    if (url.pathname === '/api/recovery/policy/preview') {
      state.preview.push(route.request().postDataJSON());
      return route.fulfill(state.failPreview ? { status: 409, json: { detail: 'Referenced key is unavailable' } } : { json: { available: true, expected_fingerprint: 'current-fingerprint', changed_fields: ['retention_count'], retained_backup_count: 4, backups_over_new_retention: 2, organization_id: 'org', workspace_id: 'ws', schedule_effect: 'Replace canonical timers; paused timers remain paused.', application: 'Applies to subsequent operations.', rollback_limit: 'Cannot recover expired backup bytes.' } });
    }
    if (url.pathname === '/api/recovery/policy' && method === 'PUT' || url.pathname.includes('/policy/rollback/')) {
      state.writes.push({ path: url.pathname, project: url.searchParams.get('project_id'), expected: url.searchParams.get('expected_fingerprint'), payload: route.request().postDataJSON() });
      if (state.fail) return route.fulfill({ status: 409, json: { detail: 'Recovery policy changed; reload' } });
      if (method === 'PUT') state.current = route.request().postDataJSON();
      return route.fulfill({ json: { policy: state.current } });
    }
    return route.fulfill({ json: { items: [] } });
  });
  await page.goto('http://127.0.0.1:18766/projects/home/operations?consumer_type=backup&consumer_id=backup-one');
  await page.locator('#refresh-recovery-policy').click();
  await expect(page.locator('[data-recovery-form]')).toBeVisible();
  return state;
}

test('typed policy edits require preview and publish through scoped canonical CAS', async ({ page }) => {
  const state = await mount(page);
  await expect(page.locator('[data-recovery-save]')).toBeDisabled();
  await page.locator('[name=retention_count]').fill('2');
  await page.locator('[data-recovery-preview]').click();
  await expect(page.locator('[data-recovery-impact]')).toContainText('4 retained backups');
  await page.locator('[data-recovery-save]').click(); await resolveAction(page);
  await expect.poll(() => state.writes.length).toBe(1);
  expect(state.writes[0].project).toBe('home'); expect(state.writes[0].expected).toBe('current-fingerprint');
  expect(state.writes[0].payload.retention_count).toBe(2);
  expect(state.writes[0].payload.require_key_manifest).toBe(true);
});

test('invalid input and stale publication preserve form values and block blind retries', async ({ page }) => {
  const state = await mount(page);
  await page.locator('[name=retention_count]').fill('0');
  await page.locator('[data-recovery-preview]').click();
  await expect(page.locator('[data-validation-summary]')).toBeVisible(); expect(state.preview).toEqual([]);
  await page.locator('[name=retention_count]').fill('2');
  await page.locator('[data-recovery-preview]').click();
  await expect(page.locator('[data-recovery-save]')).toBeEnabled();
  state.fail = true;
  await page.locator('[data-recovery-save]').click(); await resolveAction(page);
  await expect(page.locator('#recovery-policy-status')).toContainText('conflicted');
  await expect(page.locator('[name=retention_count]')).toHaveValue('2');
  await expect(page.locator('[data-recovery-save]')).toBeDisabled();
});

test('read-only evidence and recovery consumers link to key metadata without destination material', async ({ page }) => {
  await mount(page, { readonly: true });
  await expect(page.locator('[name=retention_count]')).toBeDisabled();
  await expect(page.locator('[data-recovery-preview]')).toBeDisabled();
  await expect(page.locator('#recovery-policy-content')).toContainText('Runtime evidence — read-only');
  await expect(page.locator('[data-reference-id="backup-one"]')).toBeFocused();
  await expect(page.locator('[data-reference-id="backup-one"] a').first()).toHaveAttribute('href', '/projects/home/configuration?key_id=key-backup&key_version=1');
  await expect(page.locator('#recovery-policy-content')).not.toContainText('hidden-destination-path');
});

test('policy rollback reviews impact, uses expected fingerprint and leaves evidence read-only', async ({ page }) => {
  const state = await mount(page);
  await page.getByRole('button', { name: 'Review rollback', exact: true }).click();
  await expect(page.locator('[data-recovery-impact]')).toContainText('Cannot recover expired backup bytes');
  await page.getByRole('button', { name: 'Confirm policy rollback', exact: true }).click(); await resolveAction(page);
  await expect.poll(() => state.writes.length).toBe(1);
  expect(state.writes[0].path).toBe('/api/recovery/policy/rollback/change-one');
  expect(state.writes[0].payload.expected_fingerprint).toBe('current-fingerprint');
});

test('unavailable rollback dependencies prevent confirmation and mutation', async ({ page }) => {
  const state = await mount(page); state.failPreview = true;
  await page.getByRole('button', { name: 'Review rollback', exact: true }).click();
  await expect(page.locator('#recovery-policy-status')).toContainText('no policy changed');
  await expect(page.getByRole('button', { name: 'Confirm policy rollback', exact: true })).toHaveCount(0);
  expect(state.writes).toEqual([]);
});

test('late impact cannot enable policy publication after an A-B-A Project visit', async ({ page }) => {
  let release; let entered; const started = new Promise(resolve => { entered = resolve; });
  const state = await mount(page, { intercept: async (route, url) => {
    if (url.pathname === '/api/recovery/policy/preview') { entered(); await new Promise(resolve => { release = resolve; }); await route.fulfill({ json: { changed_fields: [], expected_fingerprint: 'old' } }); return true; }
  } });
  await page.locator('[data-recovery-preview]').click(); await started;
  await page.evaluate(() => { for (const projectId of ['other', 'home']) window.dispatchEvent(new CustomEvent('codex:project-changed', { detail: { projectId } })); });
  release();
  await expect(page.locator('[data-recovery-form]')).toBeVisible();
  await expect(page.locator('[data-recovery-save]')).toBeDisabled();
  expect(state.writes).toEqual([]);
});

test('policy editor preserves cancelled navigation and fits phone text scaling', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await mount(page);
  await page.addStyleTag({ content: 'html { font-size: 200% !important; }' });
  await page.locator('[name=retention_count]').fill('2');
  const previous = page.url(); page.once('dialog', dialog => dialog.dismiss());
  await page.evaluate(() => window.CodexProductUI.openWorkspace('overview'));
  await expect(page).toHaveURL(previous);
  await expect(page.locator('[name=retention_count]')).toHaveValue('2');
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(391);
  await expect(page.locator('[data-recovery-preview]')).toBeVisible();
});

test('new edits after rollback review can cancel rollback without losing the draft', async ({ page }) => {
  const state = await mount(page);
  await page.getByRole('button', { name: 'Review rollback', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Confirm policy rollback', exact: true })).toBeVisible();
  await page.locator('[name=retention_count]').fill('7');
  page.once('dialog', dialog => dialog.dismiss());
  await page.getByRole('button', { name: 'Confirm policy rollback', exact: true }).click();
  await expect(page.locator('[name=retention_count]')).toHaveValue('7');
  expect(state.writes).toEqual([]);
});


test('recovery editor uses the main content width on desktop', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await mount(page);
  expect((await page.locator('[data-recovery-form]').boundingBox()).width).toBeGreaterThan(850);
});
