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

function operatorPayload() {
  return {
    item: {
      ref: 'team/project-a#42',
      title: 'Ship operator view',
      current_stage: 'implementation_active',
      current_owner: 'james',
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
    diagnostics: [{
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
}

async function mockOperatorApis(page, posts) {
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
    body: JSON.stringify(operatorPayload()),
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
  await page.locator('#work-items-button').click();

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
  await page.locator('#work-items-button').click();

  const dialog = page.locator('#work-items-dialog');
  await dialog.locator('.work-source-scope').fill('team/new-scope');
  await dialog.locator('.work-source-save').click();

  await expect.poll(() => saved).not.toBeNull();
  expect(saved).toEqual({
    source_type: 'gitlab',
    source_instance: 'https://gitlab.example/api/v4',
    scope: 'team/new-scope',
  });
});
