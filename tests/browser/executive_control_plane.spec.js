const { test, expect } = require('@playwright/test');


test('Executive canonical work item locks execution role and delegates through its project', async ({ page }) => {
  await page.goto('http://127.0.0.1:18766/tests/browser/executive_control_plane_fixture.html');

  await page.locator('#executive-launch').click();
  await expect(page.locator('.executive-drawer')).toHaveClass(/open/);
  await expect(page.locator('#executive-agent option')).toHaveCount(2);
  await expect(page.locator('#executive-execution-role option')).toHaveCount(4);
  await expect(page.locator('#executive-work-item option')).toHaveCount(2);
  await expect(page.locator('#executive-work-item')).not.toContainText('Closed feature');

  await page.locator('#executive-work-item').selectOption('acme/product#42');
  await expect(page.locator('#executive-project')).toHaveValue('product');
  await expect(page.locator('#executive-execution-role')).toBeDisabled();
  await expect(page.locator('.executive-status')).toContainText('canonical work-item owner/stage');

  await page.locator('#executive-prompt').fill('Implement the canonical feature safely.');
  await page.locator('#executive-send').click();
  await expect(page.locator('.executive-message.assistant .executive-message-body')).toHaveText(
    'Use the canonical work-item lane.'
  );

  await page.locator('.executive-message.assistant button', { hasText: 'Delegate to Codex' }).click();
  await expect(page.locator('.executive-status')).toContainText('Delegated to James');

  const delegation = await page.evaluate(() =>
    window.__executiveRequests.find((request) => request.path === '/api/executive/delegate')
  );
  expect(delegation.method).toBe('POST');
  expect(delegation.body.project_id).toBe('product');
  expect(delegation.body.work_item_ref).toBe('acme/product#42');
  expect(delegation.body.execution_role_id).toBeNull();
  expect(delegation.body.task).toBe('Implement the canonical feature safely.');
  expect(delegation.body.executive_reply).toBe('Use the canonical work-item lane.');
});


test('Executive non-canonical delegation leaves explicit role selectable', async ({ page }) => {
  await page.goto('http://127.0.0.1:18766/tests/browser/executive_control_plane_fixture.html');
  await page.locator('#executive-launch').click();

  await page.locator('#executive-work-item').selectOption('');
  await expect(page.locator('#executive-execution-role')).toBeEnabled();
  await page.locator('#executive-execution-role').selectOption('quinn');
  await expect(page.locator('#executive-execution-role')).toHaveValue('quinn');
});
