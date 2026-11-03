const {test, expect} = require('@playwright/test');
const {resolveAction} = require('./action_confirmation_helpers');
const base = process.env.CODEX_WEB_BROWSER_BASE_URL || 'http://127.0.0.1:18766';
const fixture = '/tests/browser/work_item_recipient_replacement_fixture.html';

test('accepted Work replacement uses exact selectors, confirmation and one binding-null-safe POST', async ({page}) => {
  await page.goto(base + fixture);
  const button = page.getByRole('button', {name: 'Replace accepted recipient context', exact: true});
  await button.evaluate(node => {node.click(); node.click();});
  await expect.poll(() => page.evaluate(() => window.requests.length)).toBe(1);
  const confirmation = await resolveAction(page);
  expect(confirmation).toContain('team/connector!33');
  expect(confirmation).toContain('accepted-thread');
  await expect.poll(() => page.evaluate(() => window.reloads)).toEqual(['team/connector!33']);
  expect(await page.evaluate(() => window.requests)).toEqual([
    {path: '/api/threads/accepted-thread/replacement-eligibility?work_item_ref=team%2Fconnector%2133&project_id=project-a', method: 'GET', body: null},
    {path: '/api/threads/accepted-thread/replace?work_item_ref=team%2Fconnector%2133&project_id=project-a', method: 'POST', body: null},
  ]);
  await expect(button).toBeDisabled();
});

test('canonical denial stays visible without a replacement POST', async ({page}) => {
  await page.goto(base + fixture);
  await page.evaluate(() => {window.mode = 'denied';});
  await page.getByRole('button', {name: 'Replace accepted recipient context'}).click();
  await expect(page.getByRole('status')).toContainText('owner_recipient_custody_changed');
  expect(await page.evaluate(() => window.requests.length)).toBe(1);
  await expect(page.locator('[data-action-confirmation]')).toHaveCount(0);
});

test('changed Work view after eligibility starts cannot confirm or replace', async ({page}) => {
  await page.goto(base + fixture);
  await page.evaluate(() => {window.mode = 'deferred';});
  await page.getByRole('button', {name: 'Replace accepted recipient context'}).click();
  await expect.poll(() => page.evaluate(() => typeof window.releaseEligibility)).toBe('function');
  await page.evaluate(() => {window.changeProject(); window.releaseEligibility();});
  await expect(page.locator('[data-action-confirmation]')).toHaveCount(0);
  expect(await page.evaluate(() => window.requests.length)).toBe(1);
  expect(await page.evaluate(() => window.reloads)).toEqual([]);
});

test('unknown replacement response is retained visibly and never automatically retried', async ({page}) => {
  await page.goto(base + fixture);
  await page.evaluate(() => {window.mode = 'unknown';});
  const button = page.getByRole('button', {name: 'Replace accepted recipient context', exact: true});
  await button.click();
  await resolveAction(page);
  await expect(page.getByRole('status')).toContainText('No automatic retry');
  await expect(button).toBeDisabled();
  expect(await page.evaluate(() => window.requests.filter(row => row.method === 'POST').length)).toBe(1);
  expect(await page.evaluate(() => window.reloads)).toEqual([]);
});

test('pending handoff does not offer accepted-recipient replacement', async ({page}) => {
  await page.goto(base + fixture);
  await page.evaluate(() => window.install('pending'));
  await expect(page.getByRole('button', {name: 'Replace accepted recipient context'})).toHaveCount(0);
});
