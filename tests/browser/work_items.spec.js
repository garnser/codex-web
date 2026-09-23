const { test, expect } = require('@playwright/test');

function projectPayload() {
  return [{
    id: 'project-a',
    name: 'Project A',
    path: '/workspace/project-a',
    sandbox: 'workspace-write',
    approval_policy: 'on-request',
    authoritative_task_source: {
      source_type: 'gitlab',
      source_instance: 'https://gitlab.example/api/v4',
      scope: 'team/project-a',
    },
  }];
}

function operatorPayload(itemOverrides = {}, diagnosticOverrides = null) {
  const payload = {
    item: {
      ref: 'team/project-a#42',
      title: 'Ship operator view',
      current_stage: 'implementation_active',
      current_owner: 'james',
      implementation_owner: 'james',
      validation_owner: 'quinn',
      release_owner: 'riley',
      priority: 'high',
      next_action: 'Run focused validation.',
      goal_id: 'goal-42',
      decision_id: 'decision-7',
      resource_ids: ['repo-app'],
      mr_refs: ['!123'],
      next_owner: null,
      artifact_state: 'branch',
      blocker: null,
      release_gate: false,
      handoff: null,
      execution: {
        retry: { attempt: 1, policy: { max_attempts: 3, backoff_seconds: 5 } },
        timeout_seconds: 300,
        deadline_at: 1789680000,
        failure_reason: null,
        latest_checkpoint: {
          id: 'checkpoint-2',
          summary: 'Implementation ready for validation.',
          next_actions: ['Run focused validation.'],
        },
        usage: {
          calls: 2,
          input_tokens: 1200,
          output_tokens: 400,
          reasoning_tokens: 300,
          estimated_cost_usd: 0.22,
          goal_id: null,
          decision_id: null,
        },
      },
    },
    external: {
      configuration: {
        source_type: 'gitlab',
        source_instance: 'https://gitlab.example/api/v4',
        scope: 'team/project-a',
      },
      identity: {
        source_type: 'gitlab',
        source_instance: 'https://gitlab.example/api/v4',
        external_id: 'team/project-a#42',
        revision: '2026-09-17T20:00:00Z',
        event_cursor: 'evt-77',
      },
      available: true,
      capabilities: ['discovery', 'read', 'events', 'owner_write', 'state_write', 'comments'],
      projected_status_label: 'status::implementing',
      projected_labels: ['status::implementing'],
      last_projected_event_at: 1789675200,
      sync: { last_success_at: 1789675300, consecutive_failures: 0 },
    },
    execution_contract: {
      schema_version: '1.1',
      role_id: 'james',
      agent_id: 'james',
      permissions: { sandbox: 'inherit', approval_policy: 'inherit' },
      success_criteria: ['Produce required artifacts.'],
      failure_conditions: ['Do not claim validation without evidence.'],
    },
    execution_policy: {
      sandbox: 'workspace-write',
      approval_policy: 'on-request',
      source: 'project-default',
    },
    diagnostics: diagnosticOverrides || [{
      kind: 'task_source_snapshot_stale_ignored',
      severity: 'warning',
      message: 'stale revision ignored',
      created_at: 1789675100,
    }],
    history: {
      items: [{
        ref: 'team/project-a#42',
        event_type: 'execution_checkpoint_recorded',
        actor: 'james',
        source: 'worker',
        reason: 'context boundary',
        created_at: 1789675000,
        payload: {},
      }],
    },
    actions: {
      retry: { allowed: true },
      reconcile: { allowed: true },
    },
  };
  payload.item = { ...payload.item, ...itemOverrides };
  return payload;
}

async function mockOperatorApis(page, posts, detailPayload = operatorPayload()) {
  await page.route('**/api/projects', async (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify(projectPayload()),
  }));
  await page.route('**/api/task-sources', async (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({
      items: [{
        project_id: 'project-a',
        source_type: 'gitlab',
        source_instance: 'https://gitlab.example/api/v4',
        scope: 'team/project-a',
        available: true,
        capabilities: ['discovery', 'read', 'events'],
      }],
      sync: { last_success_at: 1789675300, consecutive_failures: 0 },
    }),
  }));
  await page.route('**/api/secrets', async (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({
      items: [
        { id: 'secret-jira', name: 'Jira API token', status: 'active' },
        { id: 'secret-snow', name: 'ServiceNow credential', status: 'active' },
      ],
    }),
  }));
  await page.route('**/api/work-items?project_id=*', async (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({
      count: 1,
      items: [{
        ref: 'team/project-a#42',
        title: 'Ship operator view',
        current_stage: 'implementation_active',
        current_owner: 'james',
        routingError: null,
      }],
    }),
  }));
  await page.route('**/api/work-items/**/operator', async (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify(detailPayload),
  }));
  await page.route('**/api/work-items/**/retry', async (route) => {
    posts.push({ action: 'retry', body: route.request().postDataJSON() });
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify(operatorPayload()) });
  });
  await page.route('**/api/work-items/**/reconcile', async (route) => {
    posts.push({ action: 'reconcile', body: route.request().postDataJSON() });
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify(operatorPayload()) });
  });
}

test('work-item operator distinguishes canonical and external state with execution evidence', async ({ page }) => {
  const posts = [];
  await mockOperatorApis(page, posts);
  await page.goto('http://127.0.0.1:18766/tests/browser/work_items_fixture.html');
  await expect(page.locator('#work-items-button')).toHaveCount(0);
  await page.evaluate(() => window.dispatchEvent(new CustomEvent('codex:open-work-items')));

  const dialog = page.locator('#work-items-dialog');
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText('Canonical codex-web state');
  await expect(dialog).toContainText('Authoritative external source');
  await expect(dialog).toContainText('2026-09-17T20:00:00Z');
  await expect(dialog).toContainText('evt-77');
  await expect(dialog).toContainText('checkpoint-2');
  await expect(dialog).toContainText('james');
  await expect(dialog).toContainText('workspace-write');
  await expect(dialog).toContainText('stale revision ignored');
  await expect(dialog).toContainText('context boundary');
  await expect(dialog).toContainText('1200');

  await dialog.locator('.work-item-retry').click();
  await expect.poll(() => posts.length).toBe(1);
  expect(posts[0]).toMatchObject({ action: 'retry' });
  expect(posts[0].body.actor).toBe('operator');
});

test('source configuration writes the canonical project task-source endpoint', async ({ page }) => {
  const posts = [];
  await mockOperatorApis(page, posts);
  let saved = null;
  await page.route('**/api/projects/project-a/task-source', async (route) => {
    if (route.request().method() === 'PUT') {
      saved = route.request().postDataJSON();
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({ ...projectPayload()[0], authoritative_task_source: saved }),
      });
      return;
    }
    await route.fallback();
  });
  await page.goto('http://127.0.0.1:18766/tests/browser/work_items_fixture.html');
  await page.evaluate(() => window.dispatchEvent(new CustomEvent('codex:open-work-items')));

  const dialog = page.locator('#work-items-dialog');
  const scope = dialog.locator('.work-source-scope');
  await expect(scope).toHaveValue('team/project-a');
  await scope.fill('team/new-scope');
  await dialog.locator('.work-source-save').click();

  await expect.poll(() => saved).not.toBeNull();
  expect(saved).toEqual({
    source_type: 'gitlab',
    source_instance: 'https://gitlab.example/api/v4',
    scope: 'team/new-scope',
  });
});


test('source configuration sends typed Jira and ServiceNow settings without secret material', async ({ page }) => {
  const posts = [];
  await mockOperatorApis(page, posts);
  const saved = [];
  await page.route('**/api/projects/project-a/task-source', async (route) => {
    if (route.request().method() === 'PUT') {
      saved.push(route.request().postDataJSON());
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify(projectPayload()[0]),
      });
      return;
    }
    await route.fallback();
  });
  await page.goto('http://127.0.0.1:18766/tests/browser/work_items_fixture.html');
  await page.evaluate(() => window.dispatchEvent(new CustomEvent('codex:open-work-items')));

  const dialog = page.locator('#work-items-dialog');
  const type = dialog.locator('.work-source-type');

  await type.selectOption('jira');
  await dialog.locator('.work-source-instance').fill('https://jira.example');
  await dialog.locator('.work-source-scope').fill('ENG');
  await dialog.locator('.work-source-secret').selectOption('secret-jira');
  await dialog.locator('.work-source-jira-username').fill('agent@example.com');
  await dialog.locator('.work-source-save').click();
  await expect.poll(() => saved.length).toBe(1);
  await expect(dialog.locator('.work-items-status')).toHaveText('Authoritative source saved');
  expect(saved[0]).toEqual({
    source_type: 'jira',
    source_instance: 'https://jira.example',
    scope: 'ENG',
    credential_secret_id: 'secret-jira',
    provider_settings: { kind: 'jira', username: 'agent@example.com' },
  });

  await type.selectOption('servicenow');
  await dialog.locator('.work-source-instance').fill('https://example.service-now.com');
  await dialog.locator('.work-source-scope').fill('assignment_group=platform');
  await dialog.locator('.work-source-secret').selectOption('secret-snow');
  await dialog.locator('.work-source-servicenow-table').fill('task');
  await dialog.locator('.work-source-servicenow-active').fill('2');
  await dialog.locator('.work-source-servicenow-closed').fill('7');
  await dialog.locator('.work-source-save').click();
  await expect.poll(() => saved.length).toBe(2);
  expect(saved[1]).toEqual({
    source_type: 'servicenow',
    source_instance: 'https://example.service-now.com',
    scope: 'assignment_group=platform',
    credential_secret_id: 'secret-snow',
    provider_settings: {
      kind: 'servicenow',
      table: 'task',
      canonical_state_values: {
        implementation_active: '2',
        closed: '7',
      },
    },
  });

  expect(JSON.stringify(saved)).not.toContain('token-value');
  expect(JSON.stringify(saved)).not.toContain('password');
});


test('canonical summary keeps state, routing and related objects understandable from one surface', async ({ page }) => {
  const posts = [];
  await mockOperatorApis(page, posts);
  await page.goto('http://127.0.0.1:18766/tests/browser/work_items_fixture.html');
  await page.evaluate(() => window.dispatchEvent(new CustomEvent('codex:open-work-items')));

  const dialog = page.locator('#work-items-dialog');
  await expect(dialog.locator('.work-overview')).toContainText('implementation active');
  await expect(dialog.locator('.work-overview')).toContainText('james');
  await expect(dialog.locator('.work-overview')).toContainText('high');
  await expect(dialog.locator('.work-overview')).toContainText('Run focused validation.');
  await expect(dialog.locator('.work-routing-lanes')).toContainText('quinn');
  await expect(dialog.locator('.work-routing-lanes')).toContainText('riley');
  await expect(dialog.locator('.work-related-objects')).toContainText('goal-42');
  await expect(dialog.locator('.work-related-objects')).toContainText('decision-7');
  await expect(dialog.locator('.work-related-objects')).toContainText('repo-app');
  await expect(dialog.locator('.work-related-objects')).toContainText('!123');
});

for (const scenario of [
  {
    name: 'blocked',
    item: { blocker: 'Waiting for security review', blocking_findings: ['Policy gate unresolved'] },
    diagnostics: [],
    expected: ['Blocked', 'Waiting for security review', 'Policy gate unresolved'],
  },
  {
    name: 'awaiting approval',
    item: { release_gate: true },
    diagnostics: [{ kind: 'approval_required', severity: 'warning', message: 'Release approval required', created_at: 1789675100 }],
    expected: ['Approval required', 'Release approval required'],
  },
  {
    name: 'failed and retryable',
    item: {
      execution: {
        retry: { attempt: 2, policy: { max_attempts: 3, backoff_seconds: 5 } },
        failure_reason: { code: 'worker_lost', category: 'runtime', message: 'Worker lease was lost' },
        usage: {},
      },
    },
    diagnostics: [],
    expected: ['Execution failed', 'worker_lost', 'Worker lease was lost', 'retry2/3'],
  },
  {
    name: 'completed',
    item: { current_stage: 'closed', terminal_outcome: 'completed', next_action: null },
    diagnostics: [],
    expected: ['Completed', 'closed'],
  },
]) {
  test(`Work Item summary represents ${scenario.name} canonical state`, async ({ page }) => {
    const posts = [];
    const payload = operatorPayload(scenario.item, scenario.diagnostics);
    await mockOperatorApis(page, posts, payload);
    await page.goto('http://127.0.0.1:18766/tests/browser/work_items_fixture.html');
    await page.evaluate(() => window.dispatchEvent(new CustomEvent('codex:open-work-items')));
    const dialog = page.locator('#work-items-dialog');
    for (const text of scenario.expected) {
      await expect(dialog).toContainText(text);
    }
  });
}
