const { test, expect } = require('@playwright/test');

const fixture = 'http://127.0.0.1:18766/tests/browser/operations_observability_fixture.html';

test('concurrent Operations refresh triggers coalesce to one bounded request batch', async ({ page }) => {
  const counts = {
    observability: 0,
    operations: 0,
    assignments: 0,
    identity: 0,
    projects: 0,
  };
  const delayed = async (route, key, json) => {
    counts[key] += 1;
    await new Promise((resolve) => setTimeout(resolve, 120));
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify(json) });
  };

  await page.route('**/api/observability', (route) => delayed(route, 'observability', {
    health: {
      status: 'healthy',
      liveness: true,
      readiness: true,
      degraded: false,
      autonomousExecutionEligible: true,
      uptimeSeconds: 10,
      dependencies: [],
    },
    metrics: { uptimeSeconds: 10, counters: {}, timers: {} },
    recentTraces: [],
    traceCount: 0,
  }));
  await page.route('**/api/operations?**', (route) => delayed(route, 'operations', {
    windowSeconds: 900,
    runtime: {
      healthy: true,
      activeTurns: 0,
      queuedTurns: 0,
      queueThreads: 0,
      pendingApprovals: 0,
      oldestQueueAgeSeconds: 0,
      averageQueueAgeSeconds: 0,
      healthProblems: [],
      supervisorTasks: {},
    },
    workItems: {
      open: 0,
      blocked: 0,
      pendingHandoffs: 0,
      releaseGates: 0,
      splitBrain: 0,
      stages: {},
      owners: {},
      oldestPendingHandoffAgeSeconds: 0,
    },
    providers: {
      runtimeConnections: 0,
      recentDeliveryFailures: 0,
      slackBackfillCooldownRemainingSeconds: 0,
      gitlabSyncConsecutiveFailures: 0,
    },
    activity: { events: 0, eventTypes: {}, recoveryEvents: 0, executiveEvents: 0 },
  }));
  await page.route('**/api/execution-workers/assignments', (route) => delayed(route, 'assignments', { items: [] }));
  await page.route('**/api/identity/me', (route) => delayed(route, 'identity', {
    principal_kind: 'human',
    assurance: 'mfa',
    roles: ['admin'],
  }));
  await page.route('**/api/projects', (route) => delayed(route, 'projects', []));

  await page.goto(fixture);
  await page.waitForFunction(() => window.__codexOperationsReady === true);
  await page.evaluate(() => {
    document.getElementById('refresh-developer').click();
    document.getElementById('refresh-operations').click();
  });

  await expect(page.locator('#operations-status')).toContainText('Operator telemetry loaded');
  expect(counts).toEqual({
    observability: 1,
    operations: 1,
    assignments: 1,
    identity: 1,
    projects: 1,
  });

  const windowStatus = await page.evaluate(() => (
    window.__codexFrontendPerf.requestWindowStatus({ sinceMs: 5000 })
  ));
  expect(windowStatus.total).toBe(5);
  expect(windowStatus.repeated).toEqual([]);
  expect(windowStatus.ok).toBeTruthy();
});


test('large trace sets render through a bounded DOM window', async ({ page }) => {
  const traces = Array.from({ length: 500 }, (_, index) => ({
    spanId: `span-${index}`,
    name: `trace-${index}`,
    correlationId: `corr-${index}`,
    causationId: null,
    parentSpanId: null,
    status: 'ok',
    durationSeconds: 0.01,
    startedAt: 1900000000 + index,
    endedAt: 1900000000 + index + 0.01,
    attributes: { index },
  }));
  await page.route('**/api/observability', (route) => route.fulfill({
    json: {
      health: {
        status: 'healthy', liveness: true, readiness: true, degraded: false,
        autonomousExecutionEligible: true, uptimeSeconds: 10, dependencies: [],
      },
      metrics: { uptimeSeconds: 10, counters: {}, timers: {} },
      recentTraces: traces,
      traceCount: traces.length,
    },
  }));
  await page.route('**/api/operations?**', (route) => route.fulfill({
    json: {
      windowSeconds: 900,
      runtime: {
        healthy: true, activeTurns: 0, queuedTurns: 0, queueThreads: 0,
        pendingApprovals: 0, oldestQueueAgeSeconds: 0, averageQueueAgeSeconds: 0,
        healthProblems: [], supervisorTasks: {},
      },
      workItems: {
        open: 0, blocked: 0, pendingHandoffs: 0, releaseGates: 0, splitBrain: 0,
        stages: {}, owners: {}, oldestPendingHandoffAgeSeconds: 0,
      },
      providers: {
        runtimeConnections: 0, recentDeliveryFailures: 0,
        slackBackfillCooldownRemainingSeconds: 0, gitlabSyncConsecutiveFailures: 0,
      },
      activity: { events: 0, eventTypes: {}, recoveryEvents: 0, executiveEvents: 0 },
    },
  }));
  await page.route('**/api/execution-workers/assignments', (route) => route.fulfill({
    json: { items: Array.from({ length: 500 }, (_, index) => ({ id: `a-${index}`, status: 'pending' })) },
  }));
  await page.route('**/api/identity/me', (route) => route.fulfill({
    json: { principal_kind: 'human', assurance: 'mfa', roles: ['admin'] },
  }));
  await page.route('**/api/projects', (route) => route.fulfill({ json: [] }));

  await page.goto(fixture);
  await page.waitForFunction(() => window.__codexOperationsReady === true);
  await page.evaluate(() => document.getElementById('refresh-operations').click());
  await expect(page.locator('#operations-status')).toContainText('500 recent trace');
  await expect(page.locator('#operations-traces')).toContainText('Showing latest 100 of 500 matching traces');
  await expect(page.locator('#operations-traces details.comm-entry')).toHaveCount(100);
});


test('Operations overview unifies canonical health, worker lifecycle and failure remediation without extra polling', async ({ page }) => {
  await page.route('**/api/observability', (route) => route.fulfill({
    json: {
      health: {
        status: 'degraded',
        liveness: true,
        readiness: false,
        degraded: true,
        autonomousExecutionEligible: false,
        uptimeSeconds: 120,
        dependencies: [],
      },
      metrics: { uptimeSeconds: 120, counters: {}, timers: {} },
      recentTraces: [],
      traceCount: 0,
    },
  }));
  await page.route('**/api/operations?**', (route) => route.fulfill({
    json: {
      windowSeconds: 900,
      runtime: {
        healthy: false,
        activeTurns: 2,
        queuedTurns: 3,
        queueThreads: 1,
        pendingApprovals: 1,
        oldestQueueAgeSeconds: 30,
        averageQueueAgeSeconds: 10,
        healthProblems: ['worker capacity'],
        supervisorTasks: {},
      },
      workItems: {
        open: 4, blocked: 1, pendingHandoffs: 0, releaseGates: 0, splitBrain: 0,
        stages: {}, owners: {}, oldestPendingHandoffAgeSeconds: 0,
      },
      providers: {
        runtimeConnections: 2,
        recentDeliveryFailures: 1,
        slackBackfillCooldownRemainingSeconds: 0,
        gitlabSyncConsecutiveFailures: 2,
        gitlabSyncLastSuccessAt: 1900000000,
      },
      activity: { events: 3, eventTypes: {}, recoveryEvents: 0, executiveEvents: 0 },
    },
  }));
  await page.route('**/api/execution-workers/assignments', (route) => route.fulfill({
    json: {
      items: [{
        id: 'assignment-failed',
        execution_id: 'exec-failed',
        status: 'failed',
        failure: {
          reason_code: 'worker_capability_missing',
          summary: 'Required execution capability is unavailable.',
          retryability: 'after_remediation',
          remediation_key: 'restore_worker_capability',
        },
      }],
    },
  }));
  await page.route('**/api/identity/me', (route) => route.fulfill({
    json: { principal_kind: 'human', assurance: 'mfa', roles: ['admin'] },
  }));
  await page.route('**/api/projects', (route) => route.fulfill({ json: [] }));

  await page.goto(fixture);
  await page.waitForFunction(() => window.__codexOperationsReady === true);
  await page.evaluate(() => {
    window.dispatchEvent(new CustomEvent('codex:execution-worker-state-rendered', {
      detail: {
        workers: [
          { id: 'worker-active', lifecycle: 'active' },
          { id: 'worker-offline', lifecycle: 'offline' },
          { id: 'worker-draining', lifecycle: 'draining' },
        ],
      },
    }));
    document.getElementById('refresh-operations').click();
  });

  const overview = page.locator('#operations-overview');
  await expect(overview).toContainText('Runtime');
  await expect(overview).toContainText('degraded');
  await expect(overview).toContainText('Execution workers');
  await expect(overview).toContainText('Offline / revoked');
  await expect(overview).toContainText('Provider activity');
  await expect(overview).toContainText('Failures & remediation');
  await expect(overview).toContainText('worker_capability_missing');
  await expect(overview).toContainText('restore_worker_capability');
  await expect(overview.getByRole('button', { name: 'Attention' })).toBeAttached();
  await expect(overview.getByRole('button', { name: 'Evidence' })).toBeAttached();

  const requestWindow = await page.evaluate(() => (
    window.__codexFrontendPerf.requestWindowStatus({ sinceMs: 5000 })
  ));
  expect(requestWindow.total).toBe(5);
  expect(requestWindow.repeated).toEqual([]);
});

test('execution worker frontend never renders lease bearer-token material', async ({ request }) => {
  const response = await request.get('http://127.0.0.1:18766/static/execution_worker_admin.js');
  expect(response.ok()).toBeTruthy();
  const source = await response.text();
  expect(source).toContain('lease.lease_token');
  expect(source).not.toContain('escapeHtml(item.lease.lease_token)');
  expect(source).not.toContain('bearer token:');
  expect(source).toContain('lease credential: hidden');
});
