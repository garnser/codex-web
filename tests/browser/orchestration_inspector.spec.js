const { resolveAction } = require('./action_confirmation_helpers');
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
  await expect(card.getByRole('link', { name: 'Approval approval-a' })).toBeVisible();
  await expect(card.getByRole('link', { name: 'Attention attention-a' })).toBeVisible();
  await expect(card.getByRole('link', { name: 'AgentSession session-a' })).toBeVisible();
  await expect(card.getByRole('link', { name: 'Evidence evidence-a' })).toBeVisible();

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


test('schedule resume and cancellation require deliberate review; pause remains immediate', async ({ page }) => {
  await page.goto('http://127.0.0.1:18766/tests/browser/orchestration_inspector_fixture.html');
  const card = page.locator('#orchestration-inspector-card');
  await card.locator('summary', { hasText: 'Durable schedules' }).click();
  await card.locator('[data-schedule-action="cancel"]').click();
  const message = await resolveAction(page, false);
  expect(message).toContain('schedule-a');
  expect(message).toContain('review.due');
  expect(await page.evaluate(() => window.__orchRequests.some(item => item.path.endsWith('/cancel')))).toBeFalsy();
  await card.locator('[data-schedule-action="pause"]').click();
  await expect(card.locator('[data-schedule-action="resume"]')).toBeVisible();
  await card.locator('[data-schedule-action="resume"]').click();
  await resolveAction(page);
  await expect.poll(() => page.evaluate(() => window.__orchRequests.some(item => item.path === '/api/schedules/schedule-a/resume' && item.method === 'POST'))).toBeTruthy();
  await card.locator('[data-schedule-action="cancel"]').click();
  await resolveAction(page);
  await expect.poll(() => page.evaluate(() => window.__orchRequests.some(item => item.path === '/api/schedules/schedule-a/cancel' && item.method === 'POST'))).toBeTruthy();
});

test('inspector reviews live autonomy changes and leaves emergency controls immediate', async ({ page }) => {
  await page.goto('http://127.0.0.1:18766/tests/browser/orchestration_inspector_fixture.html');
  await page.locator('[data-orch-resume]').click();
  await resolveAction(page, false);
  expect(await page.evaluate(() => window.__orchRequests.some(item => item.path === '/api/autonomy/resume'))).toBeFalsy();
  await page.locator('[data-orch-resume]').click();
  await resolveAction(page);
  await expect.poll(() => page.evaluate(() => window.__orchRequests.some(item => item.path === '/api/autonomy/resume'))).toBeTruthy();
  await page.locator('[data-orch-dry-run]').check();
  await expect.poll(() => page.evaluate(() => window.__orchRequests.some(item => item.path === '/api/autonomy/control' && JSON.parse(item.body).dry_run === true))).toBeTruthy();
  await page.locator('[data-orch-dry-run]').uncheck();
  await resolveAction(page, false);
  await expect(page.locator('[data-orch-dry-run]')).toBeChecked();
  await page.locator('[data-orch-dry-run]').uncheck();
  await resolveAction(page);
  await expect.poll(() => page.evaluate(() => window.__orchRequests.some(item => item.path === '/api/autonomy/control' && JSON.parse(item.body).dry_run === false))).toBeTruthy();
  await page.locator('[data-orch-kill]').click();
  await expect.poll(() => page.evaluate(() => window.__orchRequests.some(item => item.path === '/api/autonomy/kill'))).toBeTruthy();
  await expect(page.locator('[data-action-confirmation]')).toHaveCount(0);
});
