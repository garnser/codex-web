const { test, expect } = require('@playwright/test');

test('cancelled bootstrap inspection distinguishes freed quota from retained files and shows authority evidence', async ({ page }) => {
  const workspace = {
    id: 'workspace-cancelled', execution_id: 'bootstrap-execution', status: 'released',
    subject: { kind: 'thread_bootstrap', ref: 'bootstrap-failed' },
    kind: 'git_worktree', path: '/private/recovery/worktree', branch_name: 'recovery-branch',
    resource_ids: [], owner_identity_id: 'operator', cleaned_at: null,
  };
  const responses = {
    '/api/execution-workspaces/inspection': { items: [{ workspace, lease: {
      id: 'reservation', released_at: 1790000000, release_reason: 'bootstrap startup failed',
    }, lease_active: false }] },
    '/api/execution-workspaces/workspace-cancelled/events': { items: [{
      event_type: 'workspace_released', actor_identity_id: 'operator',
      details: { preserve_files: true, discard: false },
    }] },
    '/api/execution-workers': { items: [] },
    '/api/execution-workers/assignments': { items: [{
      id: 'assignment-cancelled', subject: workspace.subject, status: 'cancelled',
      execution_id: workspace.execution_id, execution_workspace_id: workspace.id,
      failure_code: 'thread_bootstrap_cancelled', failure_message: 'bootstrap startup failed', fence: 2,
    }] },
    '/api/execution-workers/events': { items: [{
      event_type: 'assignment_cancelled', actor_id: 'operator', assignment_id: 'assignment-cancelled',
      details: { previous_fence: 1, fence: 2, preserve_files: true },
    }] },
    '/api/identity/me': { identity_id: 'operator', principal_kind: 'human', roles: ['admin'], assurance: 'mfa' },
    '/api/projects': [], '/api/resources': { items: [] },
  };
  await page.route('**/api/**', route => route.fulfill({
    contentType: 'application/json', body: JSON.stringify(responses[new URL(route.request().url()).pathname] || {}),
  }));
  await page.goto('http://127.0.0.1:18766/tests/browser/bootstrap_recovery_fixture.html');
  // The isolated fixture initializes the asynchronously imported inspectors
  // after their shared modules have loaded, as the application shell does.
  await page.waitForLoadState('networkidle');
  await page.evaluate(() => window.dispatchEvent(new Event('DOMContentLoaded')));
  const row = page.locator('[data-workspace-row="workspace-cancelled"]');
  await row.locator('summary').click();
  await expect(row).toContainText('Reservation released; files retained for recovery');
  await expect(row).toContainText('/private/recovery/worktree');
  await expect(row).toContainText('recovery-branch');
  await row.getByRole('button', { name: 'Load event history' }).click();
  await expect(row).toContainText('"preserve_files":true');
  await expect(page.locator('#execution-worker-events')).toContainText('assignment_cancelled');
  await expect(page.locator('#execution-worker-events')).toContainText('operator');
  await expect(page.locator('#execution-worker-events')).toContainText('"previous_fence":1');
  await expect(page.locator('#execution-assignment-list')).toContainText('cancelled');
});
