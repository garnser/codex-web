const { test, expect } = require('@playwright/test');
const { resolveAction } = require('./action_confirmation_helpers');
const fs = require('node:fs');
const path = require('node:path');
const html = fs.readFileSync(path.join(__dirname, '../../static/index.html'), 'utf8');

test('model qualification admin shows evidence and publishes governed revisions', async ({ page }) => {
  const mutations = [];
  await page.route('**/model-qualification-fixture', route => route.fulfill({ contentType: 'text/html', body: '<body data-project-id="a"></body>' }));
  await page.route('**/api/model-gateway/**', route => {
    const request = route.request();
    if (request.method() === 'POST') {
      mutations.push({ path: new URL(request.url()).pathname, body: request.postDataJSON() });
      return route.fulfill({ json: { item: { id: 'created' } } });
    }
    const pathName = new URL(request.url()).pathname;
    if (pathName.endsWith('/qualification-profiles')) return route.fulfill({ json: { items: [{ workload_class: 'architecture', revision: 1, minimum_quality_score: 0.8, maximum_cost_per_successful_outcome_usd: 1, required_suite_ids: ['architecture-replay'], metrics: ['quality'], created_by: 'admin', created_at: 1790380800 }] } });
    if (pathName.endsWith('/qualifications')) return route.fulfill({ json: { items: [{ id: 'qualification-1', model_id: 'architect', provider_id: 'provider-a', model_version: 'v1', workload_class: 'architecture', status: 'qualified', revision: 1, evaluation_profile_revision: 1, evaluation_run_ids: ['run-1'], reason: 'passed replay', created_at: 1790380800 }] } });
    return route.fulfill({ json: { items: [{ mapping_id: 'architecture', revision: 2, active: true, function_id: 'architecture.adr', workload_class: 'architecture', evaluated_at: 1790380800, qualification_revision: 'eval-2026-09-26', primary_model_ids: ['architect'], escalation_model_ids: [], critic_model_ids: ['critic'], required_capabilities: ['reasoning'], allow_fallback: true, source: 'operator' }] } });
  });
  await page.goto('http://127.0.0.1:18766/model-qualification-fixture');
  await page.evaluate(async markup => {
    const doc = new DOMParser().parseFromString(markup, 'text/html');
    document.body.innerHTML = `<button id="refresh-model-gateway"></button><div id="model-gateway-management-status"></div>${doc.querySelector('#model-qualification-management').outerHTML}<div id="model-qualification-list"></div>`;
    document.querySelector('#model-qualification-management').open = true;
    await import('/static/model_qualification_admin.js');
    window.dispatchEvent(new CustomEvent('codex:model-gateway-rendered', { detail: { models: [{ id: 'architect', provider_id: 'provider-a' }] } }));
  }, html);

  await expect(page.locator('#model-qualification-list .comm-entry')).toHaveCount(3);
  await expect(page.locator('#model-qualification-list')).toContainText('eval-2026-09-26');
  await page.locator('#qualification-profile-workload').selectOption('architecture');
  await page.locator('#publish-qualification-profile').click();
  await resolveAction(page);
  await expect.poll(() => mutations.length).toBe(1);
  expect(mutations[0].path).toBe('/api/model-gateway/qualification-profiles');
  expect(mutations[0].body.workload_class).toBe('architecture');
});
