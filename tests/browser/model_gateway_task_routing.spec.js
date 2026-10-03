const { test, expect } = require('@playwright/test');
const fs = require('node:fs');
const path = require('node:path');

// Use production controls/module without running the application or a model.
const html = fs.readFileSync(path.join(__dirname, '../../static/index.html'), 'utf8');
const panel = html.match(/<details id="model-route-preview-panel">([\s\S]*?)<\/details>/)[1];
const moduleSource = fs.readFileSync(path.join(__dirname, '../../static/model_gateway_route_controls.js'), 'utf8');
const models = [
  { id: 'review-a', provider_id: 'provider-a', workload_classes: ['code_review'] },
  { id: 'review-b', provider_id: 'provider-b', workload_classes: ['code_review', 'summary'] },
];

test.beforeEach(async ({ page }) => {
  await page.route('http://127.0.0.1:18766/task-routing-fixture', route => route.fulfill({
    contentType: 'text/html',
    body: `<html><body>${panel}<script type="module">import * as controls from '/task-routing-module.js'; window.controls = controls;</script></body></html>`,
  }));
  await page.route('http://127.0.0.1:18766/task-routing-module.js', route => route.fulfill({
    contentType: 'text/javascript', body: moduleSource,
  }));
  await page.goto('http://127.0.0.1:18766/task-routing-fixture');
  await page.waitForFunction(() => Boolean(window.controls));
  await page.evaluate(items => window.controls.populateTaskRouteControls(items), models);
});

test('provider-labelled pins and task preferences use canonical request fields', async ({ page }) => {
  await expect(page.locator('#model-route-pinned-model option')).toHaveText([
    'Automatic selection', 'provider-a / review-a', 'provider-b / review-b',
  ]);
  await page.getByLabel('Workload class', { exact: true }).fill(' code_review ');
  // Wrapped select labels contain their option text; target stable production
  // IDs rather than exact label text that changes with the provider catalog.
  await page.locator('#model-route-pinned-model').selectOption('review-b');
  await page.locator('#model-route-preferred-latency').selectOption(['low', 'standard']);
  await page.getByLabel('Prefer lower estimated cost').check();
  expect(await page.evaluate(() => window.controls.readTaskRoutePreferences())).toEqual({
    workload_class: 'code_review', pinned_model_id: 'review-b',
    preferred_latency_classes: ['low', 'standard'], prefer_lower_cost: true,
  });
});

test('catalog refresh preserves strict pins including unavailable models', async ({ page }) => {
  const pin = page.locator('#model-route-pinned-model');
  await pin.selectOption('review-b');
  await page.evaluate(items => window.controls.populateTaskRouteControls(items), models);
  await expect(pin).toHaveValue('review-b');
  await page.evaluate(() => window.controls.populateTaskRouteControls([]));
  await expect(pin).toHaveValue('review-b');
  await expect(pin.locator('option:checked')).toHaveText('Unavailable / review-b');
  expect(await page.evaluate(() => window.controls.readTaskRoutePreferences().pinned_model_id)).toBe('review-b');
  await pin.selectOption('');
  expect(await page.evaluate(() => window.controls.readTaskRoutePreferences().pinned_model_id)).toBeNull();
});

test('provider selection scopes available pins without silently clearing an existing pin', async ({ page }) => {
  const pin = page.locator('#model-route-pinned-model');
  await pin.selectOption('review-b');
  await page.evaluate(items => window.controls.populateTaskRouteControls(items, ['provider-a']), models);
  await expect(pin).toHaveValue('review-b');
  await expect(pin.locator('option')).toHaveText([
    'Automatic selection', 'provider-a / review-a', 'Unavailable / review-b',
  ]);
  await pin.selectOption('review-a');
  expect(await page.evaluate(() => window.controls.readTaskRoutePreferences().pinned_model_id)).toBe('review-a');
});
