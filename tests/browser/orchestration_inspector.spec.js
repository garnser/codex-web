const { test, expect } = require('@playwright/test');

test('orchestration inspector explains canonical cycle and dependency-backed state without triggering work', async ({ page }) => {
  await page.goto('http://127.0.0.1:18766/tests/browser/orchestration_inspector_fixture.html');

  const card = page.locator('#orchestration-inspector-card');
  await expect(card).toBeVisible();
  await expect(card).toContainText('pipeline-42');
  await expect(card).toContainText('reasoning gate');
  await expect(card).toContainText('anthropic/claude-code');
  await expect(card).toContainText('openai/gpt-test');
  await expect(card.getByRole('link', { name: 'ActionIntent intent-a' })).toBeVisible();
  await expect(card.locator('a[href="/api/approval-requests/approval-a"]')).toBeVisible();
  await expect(card.locator('a[href="/api/attention/attention-a"]')).toBeVisible();
  await expect(card.locator('a[href="/api/agent-sessions/session-a"]')).toBeVisible();
  await expect(card.locator('a[href="/api/evidence/evidence-a"]')).toBeVisible();

  await card.locator('summary', { hasText: 'Durable schedules' }).click();
  await expect(card).toContainText('Europe/Stockholm');
  await expect(card).toContainText('fire_once');

  await card.locator('summary', { hasText: 'Evaluation & replay' }).click();
  await expect(card).toContainText('candidate-v2');
  await expect(card).toContainText('generic.system@4');
  await expect(card).toContainText('token usage regression exceeds configured threshold');
  await expect(card).toContainText('token budget exceeded');

  await card.locator('summary', { hasText: 'ApprovalRequests' }).click();
  await expect(card).toContainText('2/2');
  await expect(card).toContainText('role.release-manager');

  await card.locator('summary', { hasText: 'Human Attention' }).click();
  await expect(card).toContainText('Provider result requires reconciliation');

  const requests = await page.evaluate(() => window.__orchRequests);
  expect(requests.filter((item) => item.path === '/api/orchestration/inspector')).toHaveLength(1);
  expect(requests.some((item) => item.path === '/api/evaluations/runs' && item.method === 'POST')).toBeFalsy();
  expect(requests.some((item) => item.path.includes('model') && item.method !== 'GET')).toBeFalsy();
});

test('schedule controls use canonical scheduler endpoints then refresh inspector', async ({ page }) => {
  await page.goto('http://127.0.0.1:18766/tests/browser/orchestration_inspector_fixture.html');

  const card = page.locator('#orchestration-inspector-card');
  await card.locator('summary', { hasText: 'Durable schedules' }).click();
  await card.locator('button[data-schedule-action="pause"]').click();

  await expect.poll(async () => page.evaluate(() =>
    window.__orchRequests.some((item) =>
      item.path === '/api/schedules/schedule-a/pause' && item.method === 'POST'
    )
  )).toBeTruthy();
  await expect(card).toContainText('paused');

  const requests = await page.evaluate(() => window.__orchRequests);
  expect(requests.filter((item) => item.path === '/api/orchestration/inspector').length).toBeGreaterThanOrEqual(2);
});

test('orchestration inspector stays usable at phone width', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('http://127.0.0.1:18766/tests/browser/orchestration_inspector_fixture.html');

  const card = page.locator('#orchestration-inspector-card');
  await expect(card).toBeVisible();
  const box = await card.boundingBox();
  expect(box.width).toBeLessThanOrEqual(390);
  await expect(card.locator('.orch-pipeline .orch-stage')).toHaveCount(8);

  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);
  expect(overflow).toBeFalsy();
});
