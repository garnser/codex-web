const { test, expect } = require('@playwright/test');
const { resolveAction } = require('./action_confirmation_helpers');
const fs = require('node:fs');
const path = require('node:path');
const html = fs.readFileSync(path.join(__dirname, '../../static/index.html'), 'utf8');
const data = {
 providers: [{ id: 'provider-a', display_name: 'Provider A', adapter_type: 'local', status: 'active', credential_required: false, residency_tags: [], compliance_tags: [] }],
 models: [{ id: 'model-a', provider_id: 'provider-a', concrete_model: 'concrete-a', model_classes: ['coding'], capabilities: ['text'], modalities: ['text'], context_window_tokens: 8000, max_output_tokens: 1000, lifecycle: 'active', route_priority: 10 }],
 prompts: [{ template_id: 'summary', version: '1', content: 'Published source', active: true }],
 policy: { max_attempts: 2, allowed_provider_ids: ['provider-a'], allowed_model_ids: ['model-a'] },
};
async function render(page, detail = data) {
 await page.evaluate(detail => window.dispatchEvent(new CustomEvent('codex:model-gateway-rendered', { detail })), detail);
}
async function mount(page, mutate = null) {
 await page.route('**/model-editor-fixture', route => route.fulfill({ contentType: 'text/html', body: '<body data-project-id="a"></body>' }));
 await page.route('**/api/**', route => {
  const request = route.request(), url = new URL(request.url());
  if (request.method() === 'PUT' && mutate) return mutate(route);
  return route.fulfill({ json: url.pathname === '/api/identity/me' ? { principal_kind: 'human', assurance: 'mfa', roles: ['admin'] } : { items: [] } });
 });
 await page.goto('http://127.0.0.1:18766/model-editor-fixture');
 await page.evaluate(async markup => {
  const doc = new DOMParser().parseFromString(markup, 'text/html');
  document.body.innerHTML = doc.querySelector('#model-gateway-management-panel').outerHTML;
  document.querySelectorAll('details').forEach(node => { node.open = true; });
  await import('/static/model_gateway_management.js');
 }, html);
 await expect(page.locator('[data-model-discard]')).toHaveCount(4);
 await render(page);
 await expect(page.locator('#model-provider-existing option')).toHaveCount(2);
 await expect(page.locator('#model-policy-max-attempts')).toHaveValue('2');
}

test('pristine Model source selection is quiet, dirty selection and refresh preserve edits', async ({ page }) => {
 await mount(page);
 const provider = page.locator('#model-provider-id').locator('..');
 await page.locator('#model-provider-existing').selectOption('provider-a');
 await expect(page.locator('#model-provider-name')).toHaveValue('Provider A');
 await expect(provider.locator('[data-dirty-editor-status]')).toHaveText('No unsaved changes');
 await page.locator('#model-provider-name').fill('Unsaved provider name');
 page.once('dialog', dialog => dialog.dismiss());
 await page.locator('#model-provider-existing').selectOption('');
 await expect(page.locator('#model-provider-existing')).toHaveValue('provider-a');
 await expect(page.locator('#model-provider-name')).toHaveValue('Unsaved provider name');
 await render(page, { ...data, policy: { ...data.policy, max_attempts: 4 } });
 await expect(page.locator('#model-gateway-management-status')).toContainText('deferred');
 await expect(page.locator('#model-policy-max-attempts')).toHaveValue('2');
 page.once('dialog', dialog => dialog.accept());
 await page.locator('[data-model-discard=provider]').click();
 await expect(page.locator('#model-provider-name')).toHaveValue('Provider A');
 await render(page, { ...data, policy: { ...data.policy, max_attempts: 4 } });
 await expect(page.locator('#model-policy-max-attempts')).toHaveValue('4');
});

test('rejected Model provider update retains draft, and successful save clears only submitted values', async ({ page }) => {
 let fail = true, pending;
 await mount(page, route => {
  if (fail) return route.fulfill({ status: 409, json: { detail: 'Canonical provider changed' } });
  pending = route;
 });
 await page.locator('#model-provider-existing').selectOption('provider-a');
 await page.locator('#model-provider-name').fill('Submitted provider name');
 await page.locator('#save-model-provider').click(); await resolveAction(page);
 await expect(page.locator('#model-gateway-management-status')).toContainText('Canonical provider changed');
 const dirty = page.locator('#model-provider-id').locator('..').locator('[data-dirty-editor-status]');
 await expect(dirty).toHaveText('Unsaved changes');
 fail = false;
 await page.locator('#save-model-provider').click(); await resolveAction(page);
 await expect.poll(() => Boolean(pending)).toBe(true);
 await page.locator('#model-provider-name').fill('Newer unsaved name');
 await pending.fulfill({ json: {} });
 await expect(page.locator('#model-gateway-management-status')).toContainText('Saved provider');
 await expect(page.locator('#model-provider-name')).toHaveValue('Newer unsaved name');
 await expect(dirty).toHaveText('Unsaved changes');
 page.once('dialog', dialog => dialog.accept());
 await page.locator('[data-model-discard=provider]').click();
 await expect(page.locator('#model-provider-name')).toHaveValue('Submitted provider name');
 await expect(dirty).toHaveText('No unsaved changes');
});

test('prompt and routing policy changes participate in the shared navigation guard without browser persistence', async ({ page }) => {
 await mount(page);
 await page.locator('#model-prompt-source').selectOption('0');
 await page.locator('#model-prompt-content').fill('Unsaved private prompt');
 await page.locator('#model-policy-max-attempts').fill('3');
 page.once('dialog', dialog => dialog.dismiss());
 expect(await page.evaluate(async () => (await import('/static/dirty_editor.js')).confirmDiscard())).toBe(false);
 await expect(page.locator('#model-prompt-content')).toHaveValue('Unsaved private prompt');
 await expect(page.locator('#model-policy-max-attempts')).toHaveValue('3');
 expect(await page.evaluate(() => JSON.stringify([Object.values(localStorage), Object.values(sessionStorage)]))).not.toContain('Unsaved private prompt');
 page.once('dialog', dialog => dialog.accept());
 expect(await page.evaluate(async () => (await import('/static/dirty_editor.js')).confirmDiscard())).toBe(true);
 await expect(page.locator('#model-prompt-content')).toHaveValue('Published source');
 await expect(page.locator('#model-policy-max-attempts')).toHaveValue('2');
});
