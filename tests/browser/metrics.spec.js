const { test, expect } = require('@playwright/test');

test('metric explorer shows freshness, provenance and immutable snapshots', async ({ page }) => {
  const metric = {
    id: 'metric-a',
    key: 'availability',
    name: 'Availability',
    description: 'Observed service availability.',
    owner_identity_id: 'owner-a',
    unit: 'percent',
    value_type: 'number',
    aggregation: 'last',
    window_seconds: null,
    freshness_seconds: 120,
    direction: 'higher_is_better',
    source_requirements: ['monitor'],
    thresholds: [{ label: 'target', operator: 'gte', value: 99.9 }],
    project_id: 'project-a',
    resource_id: 'resource-api',
    revision: 3,
  };
  const current = {
    metric_id: 'metric-a',
    metric_revision: 3,
    observation_ids: ['observation-1'],
    aggregation: 'last',
    value: 99.95,
    unit: 'percent',
    freshness: 'fresh',
    freshness_reason: 'observations satisfy the metric freshness policy',
    newest_observation_at: 1890000000,
    evaluated_at: 1890000010,
  };

  await page.route(/\/api\/metrics(?:[/?].*)?$/, async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    let body;
    if (path === '/api/metrics') {
      body = { items: [{ definition: metric, current }], count: 1 };
    } else if (path === '/api/metrics/metric-a') {
      body = { definition: metric, current };
    } else if (path === '/api/metrics/metric-a/observations') {
      body = {
        items: [{
          id: 'observation-1',
          metric_id: 'metric-a',
          metric_revision: 3,
          value: 99.95,
          unit: 'percent',
          observed_at: 1890000000,
          source: 'availability-check',
          provider: 'monitoring',
          external_record_ref: 'monitoring://availability/1',
          evidence_ids: ['evidence-1'],
          partial: false,
        }],
        count: 1,
      };
    } else if (path === '/api/metrics/metric-a/snapshots') {
      body = {
        items: [{
          id: 'metric-snapshot-1',
          metric_id: 'metric-a',
          metric_revision: 3,
          observation_ids: ['observation-1'],
          aggregation: 'last',
          value: 99.95,
          unit: 'percent',
          freshness: 'fresh',
          freshness_reason: 'observations satisfy the metric freshness policy',
          captured_by: 'decision-service',
          captured_at: 1890000010,
        }],
        count: 1,
      };
    } else {
      return route.fulfill({ status: 404, contentType: 'application/json', body: '{}' });
    }
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(body),
    });
  });

  await page.goto('http://127.0.0.1:18766/tests/browser/metrics_fixture.html');
  await expect(page.locator('#metrics-button')).toBeVisible();
  await page.locator('#metrics-button').click();

  const dialog = page.locator('#metrics-dialog');
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText('Availability');
  await expect(dialog).toContainText('99.95 percent');
  await expect(dialog.locator('.metric-freshness.fresh').first()).toHaveText('fresh');
  await expect(dialog).toContainText('monitoring / availability-check');
  await expect(dialog).toContainText('monitoring://availability/1');
  await expect(dialog).toContainText('evidence-1');
  await expect(dialog).toContainText('metric-snapshot-1');
  await expect(dialog).toContainText('metric r3');
});

test('metric explorer renders missing data explicitly and is responsive', async ({ page }) => {
  await page.route(/\/api\/metrics(?:[/?].*)?$/, async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    const definition = {
      id: 'metric-empty',
      key: 'latency',
      name: 'Latency',
      description: 'Request latency.',
      owner_identity_id: 'owner-a',
      unit: 'ms',
      value_type: 'number',
      aggregation: 'average',
      window_seconds: 300,
      freshness_seconds: 60,
      direction: 'lower_is_better',
      source_requirements: [],
      thresholds: [],
      revision: 1,
    };
    const current = {
      metric_id: 'metric-empty',
      metric_revision: 1,
      observation_ids: [],
      aggregation: 'average',
      value: null,
      unit: 'ms',
      freshness: 'missing',
      freshness_reason: 'no observation is available in the requested window',
    };
    let body;
    if (path === '/api/metrics') body = { items: [{ definition, current }], count: 1 };
    else if (path === '/api/metrics/metric-empty') body = { definition, current };
    else if (path.endsWith('/observations') || path.endsWith('/snapshots')) body = { items: [], count: 0 };
    else return route.fulfill({ status: 404, contentType: 'application/json', body: '{}' });
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
  });

  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('http://127.0.0.1:18766/tests/browser/metrics_fixture.html');
  await page.locator('#metrics-button').click();

  const dialog = page.locator('#metrics-dialog');
  await expect(dialog.locator('.metric-freshness.missing').first()).toHaveText('missing');
  await expect(dialog).toContainText('no observation is available in the requested window');
  await expect(dialog).toContainText('No observations.');
  await expect(dialog).toContainText('No persisted snapshots yet.');
});
