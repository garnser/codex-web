const { test, expect } = require('@playwright/test');

test('guided runtime enrollment shows a one-time scoped command and history without token digests', async ({ page }) => {
  let created = 0;
  await page.route('**/api/identity/me', (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({
      identity_id: 'admin',
      principal_kind: 'human',
      assurance: 'mfa',
      roles: ['admin'],
      service_scopes: [],
    }),
  }));
  await page.route('**/api/execution-workers/enrollments', async (route) => {
    if (route.request().method() === 'POST') {
      created += 1;
      return route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          token: 'one-time-enrollment-token-123456789',
          item: {
            id: 'worker-enrollment-1',
            service_identity_id: 'worker-service',
            pool: 'remote',
            expires_at: 1790000300,
            used_at: null,
            worker_id: null,
            allowed_capabilities: ['git', 'command_execution'],
            max_concurrency_ceiling: 1,
          },
        }),
      });
    }
    return route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        items: [{
          id: 'worker-enrollment-1',
          service_identity_id: 'worker-service',
          pool: 'remote',
          expires_at: 1890000300,
          used_at: null,
          worker_id: null,
          allowed_capabilities: ['git', 'command_execution'],
          max_concurrency_ceiling: 1,
        }],
      }),
    });
  });

  await page.goto('http://127.0.0.1:18766/tests/browser/runtime_enrollment_fixture.html');
  await expect(page.locator('#execution-worker-management-assurance')).toContainText('mutation: allowed');
  await page.locator('#create-execution-worker-enrollment').click();
  await expect.poll(() => created).toBe(1);
  await expect(page.locator('#execution-worker-enrollment-result')).toContainText('Token is shown only in this response');
  await expect(page.locator('[data-runtime-enrollment-command]')).toContainText('one-time-enrollment-token-123456789');
  await expect(page.locator('#execution-worker-enrollment-list')).toContainText('worker-service');
  await expect(page.locator('#execution-worker-enrollment-list')).not.toContainText('token_digest');
});
