const { test, expect } = require('@playwright/test');

function operatingItem(overrides = {}) {
  return {
    kpi_id: 'business-kpi-arr',
    kpi_revision: 2,
    metric_id: 'metric-arr',
    metric_revision: 3,
    key: 'arr',
    name: 'Annual recurring revenue',
    domain: 'revenue',
    value: 1250000,
    unit: 'usd',
    currency: 'USD',
    freshness: 'fresh',
    readiness: 'current',
    readiness_reasons: [],
    observation_ids: ['observation-12'],
    newest_observation_at: 1890000000,
    trend_delta: 50000,
    trend_percent: 4.17,
    thresholds: [{
      label: 'plan',
      operator: 'gte',
      target_value: 1200000,
      observed_value: 1250000,
      state: 'met',
      variance: 50000,
    }],
    fact_keys: ['arr'],
    external_record_ref_ids: ['external-record-crm-1'],
    goal_ids: ['goal-growth'],
    decision_ids: ['decision-budget'],
    ...overrides,
  };
}

async function installRoutes(page, item) {
  await page.route(/\/api\/business-kpis(?:[/?].*)?$/, async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === '/api/business-kpis/operating-view') {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          item: {
            organization_id: 'org-a',
            workspace_id: 'ws-a',
            current: item.readiness === 'current',
            items: [item],
            blockers: item.readiness === 'current' ? [] : ['arr: partial'],
            evaluated_at: 1890000010,
          },
        }),
      });
    }
    if (url.pathname === '/api/business-kpis/refresh' && route.request().method() === 'POST') {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ items: [], count: 0 }),
      });
    }
    return route.fulfill({ status: 404, body: '{}' });
  });

  const definition = {
    id: 'metric-arr',
    key: 'business.arr',
    name: 'Annual recurring revenue',
    description: 'Canonical business KPI.',
    owner_identity_id: 'finance-owner',
    unit: 'usd',
    value_type: 'number',
    aggregation: 'last',
    freshness_seconds: 3600,
    direction: 'higher_is_better',
    source_requirements: ['business-kpi:business-kpi-arr'],
    thresholds: [],
    revision: 3,
  };
  const current = {
    metric_id: 'metric-arr',
    metric_revision: 3,
    observation_ids: ['observation-12'],
    aggregation: 'last',
    value: 1250000,
    unit: 'usd',
    freshness: 'fresh',
    freshness_reason: 'observations satisfy the metric freshness policy',
  };
  await page.route(/\/api\/metrics(?:[/?].*)?$/, async (route) => {
    const url = new URL(route.request().url());
    let body;
    if (url.pathname === '/api/metrics') body = { items: [{ definition, current }], count: 1 };
    else if (url.pathname === '/api/metrics/metric-arr') body = { definition, current };
    else if (url.pathname.endsWith('/observations') || url.pathname.endsWith('/snapshots')) body = { items: [], count: 0 };
    else return route.fulfill({ status: 404, body: '{}' });
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(body),
    });
  });
}

test('operating view renders current KPI provenance thresholds and bindings', async ({ page }) => {
  await installRoutes(page, operatingItem());
  await page.goto('http://127.0.0.1:18766/tests/browser/business_kpi_fixture.html');
  await expect(page.locator('#business-kpi-button')).toBeVisible();
  await page.locator('#business-kpi-button').click();

  const dialog = page.locator('#business-kpi-dialog');
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText('CURRENT');
  await expect(dialog).toContainText('Annual recurring revenue');
  await expect(dialog).toContainText('1,250,000 USD');
  await expect(dialog.locator('.business-kpi-readiness.current')).toHaveText('current');
  await expect(dialog).toContainText('KPI r2 · Metric r3');
  await expect(dialog).toContainText('observation-12');
  await expect(dialog).toContainText('external-record-crm-1');
  await expect(dialog).toContainText('goal-growth');
  await expect(dialog).toContainText('decision-budget');
  await expect(dialog.locator('.business-kpi-threshold.met')).toContainText('variance 50000');

  await dialog.locator('.business-kpi-open-metric').click();
  await expect(page.locator('#metrics-dialog')).toBeVisible();
  await expect(page.locator('#metrics-dialog')).toContainText('Annual recurring revenue');
  await expect(page.locator('#metrics-dialog')).toContainText('Revision');
  await expect(page.locator('#metrics-dialog')).toContainText('3');
});

test('partial KPI blocks apparent current state and remains usable on phone width', async ({ page }) => {
  await installRoutes(page, operatingItem({
    value: 900000,
    freshness: 'partial',
    readiness: 'partial',
    readiness_reasons: ['customer-17: arr is stale', 'customer-22: arr has conflicting provider values'],
    trend_delta: null,
    trend_percent: null,
    thresholds: [{
      label: 'plan',
      operator: 'gte',
      target_value: 1200000,
      observed_value: 900000,
      state: 'unknown',
      variance: null,
    }],
  }));
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('http://127.0.0.1:18766/tests/browser/business_kpi_fixture.html');
  await page.locator('#business-kpi-button').click();

  const dialog = page.locator('#business-kpi-dialog');
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText('NOT CURRENT');
  await expect(dialog.locator('.business-kpi-readiness.partial')).toHaveText('partial');
  await expect(dialog).toContainText('customer-17: arr is stale');
  await expect(dialog).toContainText('conflicting provider values');
  await expect(dialog.locator('.business-kpi-threshold.unknown')).toContainText('unknown');

  const box = await dialog.locator('.business-kpi-card').boundingBox();
  expect(box.width).toBeLessThanOrEqual(390);
});
