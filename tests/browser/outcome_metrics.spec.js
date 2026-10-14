const { test, expect } = require('@playwright/test');
const base = process.env.CODEX_WEB_BROWSER_BASE_URL || 'http://127.0.0.1:18766';
const report = (project = 'project-a') => ({
  formula_version: 'accepted-outcome/1.0', project_id: project, status: 'partial',
  eligible_count: 2, accepted_count: 1, accepted_outcome_rate: 0.5,
  window: { start_at: 1791800000, end_at: 1791900000, as_of: 1791950000 },
  limitations: ['Explicit observations; not independent verification.', 'Missing review time remains null.'],
  cohorts: [{ workload: 'documentation', model_ids: ['baseline'], template_artifact_id: null,
    template_revision: null, eligible_count: 2, accepted_count: 1, correction_observation_count: 1,
    outcome_counts: { accepted: 1, rejected: 1, pending: 0, reopened: 0, unknown: 0 },
    status: 'partial', values: { accepted_outcome_rate: 0.5, review_seconds: null,
      tokens_per_accepted_outcome: 300, cost_usd_per_accepted_outcome: 0.02 },
    tasks: [{ task_key: 'work:fixture#one', outcome: 'accepted', runtime_usage_ids: ['usage-one'],
      execution_ids: ['execution-one'], feedback_evidence_id: 'evidence-one',
      feedback_history_ids: ['evidence-one'], feedback_head_ids: ['evidence-one'], source_evidence_ids: ['runtime-one'] }] }],
});
async function setup(page, { deny = false } = {}) {
  const writes = [];
  await page.route('**/api/metrics?**', (route) => route.fulfill({ json: { items: [], count: 0 } }));
  await page.route('**/api/metrics/outcomes/projects/**', async (route) => {
    const url = new URL(route.request().url());
    const project = url.pathname.split('/')[5];
    if (route.request().method() === 'POST') {
      writes.push({ path: url.pathname, body: route.request().postDataJSON() });
      return route.fulfill(deny ? { status: 403, json: { detail: 'MFA required' } } : {
        json: url.pathname.endsWith('/feedback') ? { item: { id: 'evidence-saved' } }
          : { report: report(project), observations: [{ metric_id: 'metric-one' }] },
      });
    }
    return route.fulfill({ json: report(project) });
  });
  await page.goto(`${base}/tests/browser/metrics_fixture.html`);
  await page.locator('#metrics-button').click();
  await page.locator('.outcome-metrics summary').click();
  return writes;
}
test('outcome inspection exposes denominators, missing effort and canonical provenance', async ({ page }) => {
  await setup(page);
  await page.getByRole('button', { name: 'Inspect outcomes' }).click();
  await expect(page.locator('.outcome-results')).toContainText('1 accepted / 2');
  await expect(page.locator('.outcome-results')).toContainText('Missing');
  await expect(page.locator('.outcome-results')).toContainText('evidence-one');
  await expect(page.locator('.outcome-results')).toContainText('accepted-outcome/1.0');
  await expect(page.locator('.outcome-results')).toContainText('not independent verification');
});
test('optional feedback uses selected attempt and bounded observations, and explicitly supersedes current review', async ({ page }) => {
  const writes = await setup(page);
  await page.getByRole('button', { name: 'Inspect outcomes' }).click();
  await page.locator('.outcome-feedback [name=workload]').fill('documentation');
  await page.locator('.outcome-feedback [name=outcome]').selectOption('reopened');
  await page.locator('.outcome-feedback [name=seconds]').fill('45');
  await page.locator('.outcome-feedback [name=replace]').check();
  await page.getByRole('button', { name: 'Save review Evidence' }).click();
  await expect(page.locator('.outcome-status')).toContainText('evidence-saved');
  expect(writes[0].body.runtime_usage_id).toBe('usage-one');
  expect(writes[0].body.supersedes_evidence_ids).toEqual(['evidence-one']);
  expect(writes[0].body.review_seconds).toBe(45);
  expect(writes[0].body.review_count).toBeNull();
  expect(writes[0].body).not.toHaveProperty('comment');
});
test('denied feedback retains draft and does not claim saved review', async ({ page }) => {
  await setup(page, { deny: true });
  await page.getByRole('button', { name: 'Inspect outcomes' }).click();
  await page.locator('.outcome-feedback [name=workload]').fill('documentation');
  await page.locator('.outcome-feedback [name=seconds]').fill('20');
  await page.getByRole('button', { name: 'Save review Evidence' }).click();
  await expect(page.locator('.outcome-status')).toContainText('MFA required');
  await expect(page.locator('.outcome-feedback [name=seconds]')).toHaveValue('20');
  await expect(page.locator('.outcome-status')).not.toContainText('Saved');
});
test('publication sends the window and displays server-calculated Metrics receipt', async ({ page }) => {
  const writes = await setup(page);
  await page.getByRole('button', { name: 'Inspect outcomes' }).click();
  await page.getByRole('button', { name: 'Record canonical Metrics' }).click();
  await expect(page.locator('.outcome-status')).toContainText('Recorded 1');
  expect(Object.keys(writes[0].body).sort()).toEqual(['end_at', 'start_at']);
});
test('Project switch clears prior review data, draft and measurement selection', async ({ page }) => {
  await setup(page);
  await page.getByRole('button', { name: 'Inspect outcomes' }).click();
  await page.locator('.outcome-feedback [name=workload]').fill('private-project-a');
  await page.evaluate(() => {
    document.body.dataset.activeProject = 'project-b';
    window.dispatchEvent(new CustomEvent('codex:project-changed'));
  });
  await expect(page.locator('.outcome-scope')).toContainText('project-b');
  await expect(page.locator('.outcome-results')).toBeEmpty();
  await expect(page.locator('.outcome-feedback')).toBeHidden();
  await expect(page.locator('.outcome-feedback [name=workload]')).toHaveValue('');
  await expect(page.getByRole('button', { name: 'Record canonical Metrics' })).toBeDisabled();
});
test('no Project disables outcome requests and mobile feedback remains reachable by keyboard', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await setup(page);
  await page.getByRole('button', { name: 'Inspect outcomes' }).focus();
  await page.keyboard.press('Enter');
  await expect(page.locator('.outcome-feedback')).toBeVisible();
  await page.evaluate(() => {
    document.body.dataset.activeProject = '';
    window.dispatchEvent(new CustomEvent('codex:project-changed'));
  });
  await expect(page.getByRole('button', { name: 'Inspect outcomes' })).toBeDisabled();
});
test('initially empty Project exposes its empty state without issuing metric requests', async ({ page }) => {
  let requests = 0;
  await page.route('**/api/metrics**', (route) => { requests += 1; return route.fulfill({ json: { items: [] } }); });
  await page.route('**/tests/browser/metrics_fixture.html', (route) => route.fulfill({
    contentType: 'text/html', body: '<html><body><script type="module" src="/static/metrics_ui.js"></script></body></html>',
  }));
  await page.goto(`${base}/tests/browser/metrics_fixture.html`);
  await page.locator('#metrics-button').click();
  await page.locator('.outcome-metrics summary').click();
  await expect(page.locator('.outcome-scope')).toContainText('Select a Project');
  await expect(page.getByRole('button', { name: 'Inspect outcomes' })).toBeDisabled();
  expect(requests).toBe(0);
});
