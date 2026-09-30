const { test, expect } = require('@playwright/test');
const { resolveAction } = require('./action_confirmation_helpers');
async function mount(page, options = {}) {
  await page.route('**/confirmation-fixture', route => route.fulfill({ contentType: 'text/html', body: '<body data-project-id="one"><button id="start">Change object</button><button id="after">After</button></body>' }));
  await page.goto('http://127.0.0.1:18766/confirmation-fixture');
  await page.evaluate(async options => {
    const { confirmAction } = await import('/static/action_confirmation.js');
    window.results = [];
    document.querySelector('#start').onclick = async () => window.results.push(await confirmAction({ action: 'Revoke object', target: '<object-one>', consequence: 'Future access stops.', risk: 'high', impact: '2 canonical consumers; one outside this Project.', recovery: 'No Undo is supported.', ...options }));
  }, options);
  await page.locator('#start').click();
}

test('high-impact confirmation exposes target and consequences, traps focus, and requires deliberate acknowledgement', async ({ page }) => {
  await mount(page);
  const dialog = page.getByRole('dialog', { name: 'Revoke object' });
  await expect(dialog).toContainText('Target: <object-one>');
  await expect(dialog.locator('object-one')).toHaveCount(0);
  await expect(dialog).toContainText('2 canonical consumers');
  await expect(dialog).toContainText('No Undo');
  await expect(dialog.getByRole('button', { name: 'Cancel' })).toBeFocused();
  await expect(dialog.getByRole('button', { name: 'Revoke object' })).toBeDisabled();
  await page.keyboard.press('Tab');
  expect(await page.evaluate(() => Boolean(document.activeElement.closest('[data-action-confirmation]')))).toBe(true);
  await dialog.getByRole('checkbox').check();
  await dialog.getByRole('button', { name: 'Revoke object' }).click();
  await expect.poll(() => page.evaluate(() => window.results)).toEqual([true]);
  await expect(page.locator('#start')).toBeFocused();
});

test('Escape and Cancel make no decision and restore the originating control', async ({ page }) => {
  await mount(page, { risk: 'bounded' });
  await expect(page.locator('[data-action-ack-label]')).toBeHidden();
  await page.keyboard.press('Escape');
  await expect.poll(() => page.evaluate(() => window.results)).toEqual([false]);
  await expect(page.locator('#start')).toBeFocused();
  await page.locator('#start').click(); await page.getByRole('button', { name: 'Cancel', exact: true }).click();
  await expect.poll(() => page.evaluate(() => window.results)).toEqual([false, false]);
});

test('Project navigation invalidates a pending confirmation, including A-B-A visits', async ({ page }) => {
  await mount(page);
  await page.evaluate(() => {
    window.dispatchEvent(new CustomEvent('codex:project-changed', { detail: { projectId: 'two' } }));
    window.dispatchEvent(new CustomEvent('codex:project-changed', { detail: { projectId: 'one' } }));
  });
  await expect(page.locator('[data-action-confirmation]')).toHaveCount(0);
  await expect.poll(() => page.evaluate(() => window.results)).toEqual([false]);
});

test('phone dialog remains within the viewport at enlarged text size', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 }); await mount(page);
  await page.addStyleTag({ content: 'html { font-size: 200%; }' });
  const bounds = await page.locator('[data-action-confirmation]').boundingBox();
  expect(bounds.x).toBeGreaterThanOrEqual(0); expect(bounds.x + bounds.width).toBeLessThanOrEqual(390);
  await expect(page.getByRole('button', { name: 'Cancel', exact: true })).toBeVisible();
});

test('configuration publication fails closed on unavailable impact and preserves canonical revision checks', async ({ page }) => {
  await mount(page); await page.keyboard.press('Escape');
  let unavailable = true; const writes = [];
  await page.route('**/api/configuration/**', route => {
    const request = route.request();
    if (request.method() === 'POST') {
      writes.push({ url: request.url(), body: request.postDataJSON() });
      return route.fulfill({ json: {} });
    }
    return route.fulfill(unavailable ? { status: 503, json: { detail: 'Impact unavailable' } }
      : { json: { more_specific_overrides: [{ id: 'override' }], startup_only: true } });
  });
  page.on('dialog', dialog => dialog.accept('Reviewed rollout'));
  await page.evaluate(async () => {
    const { publishRecord } = await import('/static/configuration_lifecycle_ui.js');
    window.messages = [];
    document.querySelector('#start').onclick = () => publishRecord({
      id: 'draft-a', key: 'runtime.setting', revision: 4, scope_type: 'project', scope_id: 'one',
    }, { active: { revision: 3 }, setStatus: message => window.messages.push(message) });
  });
  await page.locator('#start').click();
  await expect.poll(() => page.evaluate(() => window.messages.join(' '))).toContain('impact unavailable');
  await expect(page.locator('[data-action-confirmation]')).toHaveCount(0);
  expect(writes).toHaveLength(0);
  unavailable = false;
  await page.locator('#start').click();
  const review = await resolveAction(page, false);
  expect(review).toContain('project:one'); expect(review).toContain('1 more-specific');
  expect(review).toContain('startup-only'); expect(writes).toHaveLength(0);
  await page.locator('#start').click(); await resolveAction(page);
  await expect.poll(() => writes.length).toBe(1);
  expect(writes[0].body.expected_active_revision).toBe(3);
  expect(new URL(writes[0].url).searchParams.get('project_id')).toBe('one');
});

test('extension removal cancels before mutation and submits the canonical tombstone contract after review', async ({ page }) => {
  const writes = [];
  await page.route('**/api/**', route => {
    const request = route.request(); const path = new URL(request.url()).pathname;
    if (request.method() === 'POST') { writes.push(request.postDataJSON()); return route.fulfill({ json: {} }); }
    if (path === '/api/identity/me') return route.fulfill({ json: { principal_kind: 'human', assurance: 'mfa', roles: ['admin'] } });
    return route.fulfill({ json: { items: path === '/api/extensions' ? [{ id: 'extension-a', lifecycle: 'disabled', manifest: { id: 'example.extension', version: '1.0', capabilities: {} } }] : [] } });
  });
  await page.route('**/extension-review-fixture', route => route.fulfill({ contentType: 'text/html', body: '<body data-project-id="one"><details id="developer-panel" open><div id="extension-admin-status"></div><div id="extension-admin-list"></div></details><script src="/static/extension_admin.js"></script></body>' }));
  await page.goto('http://127.0.0.1:18766/extension-review-fixture');
  page.on('dialog', dialog => dialog.accept('Retire installation'));
  await page.getByRole('button', { name: 'Remove', exact: true }).click();
  const review = await resolveAction(page, false);
  expect(review).toContain('extension-a'); expect(review).toContain('hard deletion is not supported');
  expect(writes).toHaveLength(0);
  await page.getByRole('button', { name: 'Remove', exact: true }).click(); await resolveAction(page);
  await expect.poll(() => writes.length).toBe(1);
  expect(writes[0]).toEqual({ reason: 'Retire installation', preserve_tombstone: true });
});
