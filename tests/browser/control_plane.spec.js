const { test, expect } = require('@playwright/test');

test('operations telemetry is rendered in the Developer control plane', async ({ page }) => {
  await page.route('**/api/operations*', async (route) => {
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        generatedAt: 1789582000,
        runtime: { healthy: true, activeTurns: 2, queuedTurns: 3, oldestQueueAgeSeconds: 125 },
        workItems: { open: 7, pendingHandoffs: 1, blocked: 2, splitBrain: 0, releaseGates: 1, oldestPendingHandoffAgeSeconds: 90 },
        providers: { runtimeConnections: 2, recentDeliveryFailures: 0, gitlabSyncConsecutiveFailures: 0 },
        activity: { recoveryEvents: 4, executiveEvents: 5 },
      }),
    });
  });

  await page.goto('http://127.0.0.1:18766/tests/browser/control_plane_fixture.html');

  await expect(page.locator('#operations-card')).toBeVisible();
  await expect(page.locator('#operations-card')).toContainText('Healthy');
  await expect(page.locator('#operations-card')).toContainText('Queued');
  await expect(page.locator('#operations-card')).toContainText('3');
  await expect(page.locator('#operations-card')).toContainText('Open items');
  await expect(page.locator('#operations-card')).toContainText('7');
});

test('Executive knowledge manager respects scope and saves project knowledge', async ({ page }) => {
  const posts = [];
  await page.route('**/api/executive/knowledge*', async (route) => {
    const request = route.request();
    if (request.method() === 'POST') {
      posts.push(request.postDataJSON());
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({ ok: true, item: { id: 'new-entry' } }),
      });
      return;
    }
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        items: [{
          id: 'company-1',
          scope: 'company',
          project_id: null,
          title: 'Database standard',
          content: 'Use PostgreSQL.',
          tags: ['database'],
          source: 'ADR-12',
          priority: 80,
        }],
      }),
    });
  });
  await page.route('**/api/operations*', async (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({ runtime: {}, workItems: {}, providers: {}, activity: {} }),
  }));

  await page.goto('http://127.0.0.1:18766/tests/browser/control_plane_fixture.html');
  const knowledge = page.locator('.executive-knowledge');
  await knowledge.locator('summary').click();
  await expect(knowledge).toContainText('Database standard');

  await knowledge.locator('.knowledge-scope').selectOption('project');
  await knowledge.locator('.knowledge-title').fill('Project deployment rule');
  await knowledge.locator('.knowledge-tags').fill('deploy, release');
  await knowledge.locator('.knowledge-source').fill('runbook');
  await knowledge.locator('.knowledge-priority').fill('70');
  await knowledge.locator('.knowledge-content').fill('Deploy through the release manager.');
  await knowledge.locator('.knowledge-save').click();

  await expect.poll(() => posts.length).toBe(1);
  expect(posts[0]).toMatchObject({
    scope: 'project',
    project_id: 'project-a',
    title: 'Project deployment rule',
    source: 'runbook',
    priority: 70,
    tags: ['deploy', 'release'],
  });
});
