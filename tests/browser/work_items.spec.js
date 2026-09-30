const { resolveAction } = require('./action_confirmation_helpers');
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
  await page.route('**/api/work-items/**/operator?*', async (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify(detailPayload),
  }));
  await page.route('**/api/work-items/**/retry?*', async (route) => {
    posts.push({ action: 'retry', body: route.request().postDataJSON() });
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify(operatorPayload()) });
  });
  await page.route('**/api/work-items/**/reconcile?*', async (route) => {
    posts.push({ action: 'reconcile', body: route.request().postDataJSON() });
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify(operatorPayload()) });
  });
}

test('work-item operator distinguishes canonical and external state with execution evidence', async ({ page }) => {
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  const posts = [];
  await mockOperatorApis(page, posts);
  await page.goto('http://127.0.0.1:18766/tests/browser/work_items_fixture.html');
  await expect(page.locator('#work-items-button')).toHaveCount(0);
  await page.evaluate(() => window.dispatchEvent(new CustomEvent('codex:open-work-items')));
  await expect(page.locator('.work-items-status')).toHaveText('Up to date');
  expect(errors).toEqual([]);

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

for (const delayed of ['operator', 'retry', 'runs', 'return-to-project']) {
  test(`Project switches discard delayed ${delayed} responses and clear stale controls`, async ({ page }) => {
    const projects = ['project-a', 'project-b', 'empty'].map(id => ({ id, name: id, path: `/workspace/${id}` }));
    let hold = false;
    let detailRevision = 0;
    const endpoint = delayed === 'return-to-project' ? 'operator' : delayed;
    let release;
    let held = false;
    const gate = new Promise(resolve => { release = resolve; });
    await page.route('**/api/**', async route => {
      const url = new URL(route.request().url());
      const path = decodeURIComponent(url.pathname);
      if (path === '/api/projects') return route.fulfill({ json: projects });
      if (path === '/api/task-sources' || path === '/api/secrets') return route.fulfill({ json: { items: [] } });
      if (path === '/api/work-items') {
        const project = url.searchParams.get('project_id');
        return route.fulfill({ json: { items: project === 'empty' ? [] : [{ ref: `${project}#42`, title: `${project} item` }] } });
      }
      const project = path.includes('project-a') ? 'project-a' : 'project-b';
      const revision = path.endsWith('/operator') && project === 'project-a' ? ++detailRevision : 0;
      if (hold && project === 'project-a' && path.endsWith(`/${endpoint}`)) {
        held = true;
        await gate;
      }
      if (path.endsWith('/runs')) return route.fulfill({ json: { active: [], items: [{ executionId: `${project}-run`, status: 'completed' }] } });
      return route.fulfill({ json: { item: { ref: `${project}#42`, project_id: project, title: `${project} detail ${revision}` }, actions: { retry: { allowed: true }, reconcile: { allowed: true } } } });
    });
    await page.goto('http://127.0.0.1:18766/tests/browser/work_items_fixture.html?project=project-a');
    await page.evaluate(() => window.dispatchEvent(new CustomEvent('codex:open-work-items')));
    const detail = page.locator('.work-item-detail');
    await expect(detail).toContainText('project-a detail');
    hold = true;
    if (delayed === 'retry') await page.locator('.work-item-retry').click();
    else if (delayed === 'runs') await page.evaluate(() => window.dispatchEvent(new CustomEvent('codex:work-item-run-updated', { detail: { workItemRef: 'project-a#42' } })));
    else await page.locator('.work-items-refresh').click();
    await expect.poll(() => held).toBe(true);
    await page.evaluate(() => {
      document.body.dataset.projectId = 'project-b';
      window.dispatchEvent(new CustomEvent('codex:project-changed', { detail: { projectId: 'project-b' } }));
    });
    await expect(detail).toContainText('project-b detail');
    if (delayed === 'return-to-project') {
      hold = false;
      await page.evaluate(() => {
        document.body.dataset.projectId = 'project-a';
        window.dispatchEvent(new CustomEvent('codex:project-changed', { detail: { projectId: 'project-a' } }));
      });
      await expect(detail).toContainText('project-a detail 3');
    }
    const settled = page.waitForResponse(response => decodeURIComponent(new URL(response.url()).pathname).endsWith(`project-a#42/${endpoint}`));
    release();
    await settled;
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    if (delayed === 'return-to-project') {
      await expect(detail).toContainText('project-a detail 3');
      await expect(detail).not.toContainText('project-a detail 2');
    } else {
      await expect(detail).toContainText('project-b detail');
      await expect(detail).not.toContainText('project-a');
    }
    await page.evaluate(() => {
      document.body.dataset.projectId = 'empty';
      window.dispatchEvent(new CustomEvent('codex:project-changed', { detail: { projectId: 'empty' } }));
      window.__emptyProjectDetail = document.querySelector('.work-item-detail').textContent;
    });
    expect(await page.evaluate(() => window.__emptyProjectDetail)).not.toMatch(/project-[ab]/);
    await expect(page.locator('.work-items-list')).toContainText('No canonical work items');
    await expect(detail.locator('button')).toHaveCount(0);
    await expect(page.locator('.work-items-status')).toContainText('0 Work Items');
  });
}

test('command palette opens the requested loaded Work Item through canonical operator refresh', async ({ page }) => {
  await mockOperatorApis(page, []);
  await page.route('**/api/work-items?project_id=*', route => route.fulfill({ json: { items: [
    { ref: 'team/project-a#42', title: 'First Work Item', project_id: 'project-a' },
    { ref: 'team/project-a#43', title: 'Second Work Item', project_id: 'project-a' },
  ] } }));
  await page.route('**/api/work-items/**/operator?*', route => {
    const ref = decodeURIComponent(new URL(route.request().url()).pathname).includes('#43') ? 'team/project-a#43' : 'team/project-a#42';
    return route.fulfill({ json: operatorPayload({ ref, title: ref.endsWith('43') ? 'Second Work Item' : 'First Work Item', project_id: 'project-a' }) });
  });
  await page.goto('http://127.0.0.1:18766/tests/browser/work_items_fixture.html?project=project-a');
  await page.evaluate(async () => {
    const sidebar = document.createElement('aside');
    sidebar.className = 'sidebar';
    sidebar.innerHTML = '<div class="brand">Codex</div>';
    document.body.prepend(sidebar);
    await import('/static/product_workspaces.js');
    window.dispatchEvent(new CustomEvent('codex:projects-rendered', { detail: {
      projectId: 'project-a', projects: [{ id: 'project-a', name: 'Project A' }],
    } }));
    window.dispatchEvent(new CustomEvent('codex:open-work-items'));
  });
  await expect(page.locator('.work-item-row')).toHaveCount(2);
  await expect(page.locator('dialog:modal')).toHaveCount(0);
  await page.evaluate(() => window.CodexProductUI.openWorkspace('overview'));
  await page.keyboard.press('Control+K');
  await page.locator('#product-workspace-search').fill('Second Work Item');
  await expect(page.locator('#product-command-results')).toContainText('Project A');
  await page.keyboard.press('Enter');
  await expect(page.locator('.work-item-detail')).toContainText('Second Work Item');
  await expect(page.locator('[data-product-workspace-host="work"] > .work-items-shell')).toBeVisible();
  await expect(page.locator('dialog:modal')).toHaveCount(0);
  await expect(page.locator('#product-workspace-switcher')).not.toBeVisible();
});

test('GitLab TaskSource preserves, replaces and unbinds canonical SecretReferences', async ({ page }) => {
  await mockOperatorApis(page, []);
  const projects = projectPayload();
  projects[0].authoritative_task_source.credential_secret_id = 'secret-old';
  await page.route('**/api/projects', route => route.fulfill({ json: projects }));
  await page.route('**/api/secrets', route => route.fulfill({ json: { items: [
    { id: 'secret-old', name: 'Original GitLab credential', status: 'active' },
    { id: 'secret-new', name: 'Replacement GitLab credential', status: 'active' },
  ] } }));
  const saved = [];
  await page.route('**/api/projects/project-a/task-source', route => {
    const source = route.request().postDataJSON(); saved.push(source);
    projects[0].authoritative_task_source = source;
    return route.fulfill({ json: projects[0] });
  });
  await page.goto('http://127.0.0.1:18766/tests/browser/work_items_fixture.html');
  await page.evaluate(() => window.dispatchEvent(new CustomEvent('codex:open-work-items')));
  const dialog = page.locator('#work-items-dialog');
  await expect(dialog.locator('.work-source-secret')).toBeVisible();
  await expect(dialog.locator('.work-source-secret')).toHaveValue('secret-old');
  await expect(dialog.locator('[data-manage-source-secret]')).toHaveAttribute('href', '/projects/project-a/secrets?secret_id=secret-old');
  await dialog.locator('.work-source-save').click();
  await expect.poll(() => saved.length).toBe(1);
  expect(saved[0].credential_secret_id).toBe('secret-old');
  await expect(dialog.locator('.work-items-status')).toHaveText('Authoritative source saved');
  await dialog.locator('.work-source-secret').selectOption('secret-new');
  await dialog.locator('.work-source-save').click();
  await expect.poll(() => saved.length).toBe(2);
  expect(saved[1].credential_secret_id).toBe('secret-new');
  await expect(dialog.locator('.work-items-status')).toHaveText('Authoritative source saved');
  await dialog.locator('.work-source-secret').selectOption('');
  await dialog.locator('.work-source-save').click();
  await expect.poll(() => saved.length).toBe(3);
  expect(saved[2]).not.toHaveProperty('credential_secret_id');
});

test('TaskSource editor rejects sibling adapter fallback and explains explicit Project authority', async ({ page }) => {
  await mockOperatorApis(page, []);
  const projects = projectPayload(); projects[0].authoritative_task_source = null;
  await page.route('**/api/projects', route => route.fulfill({ json: projects }));
  await page.route('**/api/task-sources', route => route.fulfill({ json: { items: [{ project_id: 'other-project', source_type: 'gitlab', source_instance: 'https://foreign-instance.example', available: true }] } }));
  await page.goto('http://127.0.0.1:18766/tests/browser/work_items_fixture.html');
  await page.evaluate(() => window.dispatchEvent(new CustomEvent('codex:open-work-items')));
  await expect(page.locator('.work-source-instance')).toHaveValue('');
  await expect(page.locator('[data-source-provenance]')).toContainText('no explicit authoritative TaskSource');
  await expect(page.locator('.work-source-capabilities')).toContainText('Adapter not registered');
  await expect(page.locator('#work-items-dialog')).not.toContainText('foreign-instance');
});

test('dirty TaskSource supports cancellation, failed-save recovery and confirmed clear', async ({ page }) => {
  await mockOperatorApis(page, []); let deletes = 0;
  await page.route('**/api/projects/project-a/task-source', route => {
    if (route.request().method() === 'DELETE') { deletes++; return route.fulfill({ json: {} }); }
    return route.fulfill({ status: 403, json: { detail: 'Project administration denied' } });
  });
  await page.goto('http://127.0.0.1:18766/tests/browser/work_items_fixture.html');
  await page.evaluate(() => window.dispatchEvent(new CustomEvent('codex:open-work-items')));
  await page.locator('.work-source-scope').fill('retained-draft');
  page.once('dialog', dialog => dialog.dismiss());
  await page.locator('.work-items-close').click();
  await expect(page.locator('#work-items-dialog')).toBeVisible();
  await page.locator('.work-source-save').click();
  await expect(page.locator('.work-source-config [role=alert]')).toContainText('Project administration denied');
  await expect(page.locator('.work-source-scope')).toHaveValue('retained-draft');
  page.once('dialog', dialog => dialog.accept());
  await page.getByRole('button', { name: 'Discard source edits' }).click();
  await expect(page.locator('.work-source-scope')).toHaveValue('team/project-a');
  await page.locator('.work-source-clear').click(); await resolveAction(page, false); expect(deletes).toBe(0);
  await page.locator('.work-source-clear').click(); await resolveAction(page);
  await expect.poll(() => deletes).toBe(1);
});

for (const backToOriginal of [false, true]) {
  test(`late TaskSource save stays with its originating Project${backToOriginal ? ' after an A-B-A visit' : ''}`, async ({ page }) => {
    await mockOperatorApis(page, []);
    const projects = [...projectPayload(), { ...projectPayload()[0], id: 'project-b', name: 'Project B', authoritative_task_source: { source_type: 'gitlab', source_instance: 'https://b.example', scope: 'scope-b' } }];
    await page.route('**/api/projects', route => route.fulfill({ json: projects }));
    let release, requested = false;
    const pending = new Promise(resolve => { release = resolve; });
    await page.route('**/api/projects/project-a/task-source', async route => { requested = true; await pending; await route.fulfill({ json: projects[0] }); });
    await page.goto('http://127.0.0.1:18766/tests/browser/work_items_fixture.html?work_item_project=project-a');
    await page.evaluate(() => window.dispatchEvent(new CustomEvent('codex:open-work-items')));
    await page.locator('.work-source-scope').fill('old-save'); await page.locator('.work-source-save').click();
    await expect.poll(() => requested).toBe(true);
    await page.evaluate(() => window.dispatchEvent(new CustomEvent('codex:project-changed', { detail: { projectId: 'project-b' } })));
    await expect(page.locator('.work-source-scope')).toHaveValue('scope-b');
    if (backToOriginal) {
      await page.evaluate(() => window.dispatchEvent(new CustomEvent('codex:project-changed', { detail: { projectId: 'project-a' } })));
      await expect(page.locator('.work-source-scope')).toHaveValue('team/project-a');
    }
    const response = page.waitForResponse(url => url.url().endsWith('/api/projects/project-a/task-source'));
    release(); await response;
    await expect(page.locator('.work-source-scope')).toHaveValue(backToOriginal ? 'team/project-a' : 'scope-b');
    await expect(page.locator('.work-items-status')).not.toHaveText('Authoritative source saved');
  });
}

test('ServiceNow editor preserves all canonical mappings and edits schema-declared field names', async ({ page }) => {
  await mockOperatorApis(page, []);
  const projects = projectPayload();
  projects[0].authoritative_task_source = { source_type: 'servicenow', source_instance: 'https://snow.example', scope: 'team', credential_secret_id: 'secret-snow', provider_settings: {
    kind: 'servicenow', table: 'task', fields: { title: 'custom_title', assignee: 'custom_owner' },
    canonical_state_values: { implementation_active: '2', ready_for_validation: '3', validation_running: '4', closed: '7' },
  } };
  await page.route('**/api/projects', route => route.fulfill({ json: projects }));
  await page.route('**/api/task-sources', route => route.fulfill({ json: { items: [], configuration_schema: { $defs: {
    ServiceNowFieldSettings: { properties: { title: { type: 'string', title: 'Title', default: 'short_description' }, assignee: { type: 'string', title: 'Assignee', default: 'assigned_to' } } },
    ServiceNowTaskSourceSettings: { properties: { canonical_state_values: { propertyNames: { enum: ['implementation_active', 'ready_for_validation', 'validation_running', 'closed'] } } } },
  } } } }));
  let saved;
  await page.route('**/api/projects/project-a/task-source', route => { saved = route.request().postDataJSON(); projects[0].authoritative_task_source = saved; return route.fulfill({ json: projects[0] }); });
  await page.goto('http://127.0.0.1:18766/tests/browser/work_items_fixture.html');
  await page.evaluate(() => window.dispatchEvent(new CustomEvent('codex:open-work-items')));
  await page.locator('[data-source-provider-details] summary').click();
  await expect(page.getByLabel('Title field name', { exact: true })).toHaveValue('custom_title');
  await page.getByLabel('Title field name', { exact: true }).fill('new_title');
  await page.getByLabel('validation_running state value', { exact: true }).fill('5');
  await page.locator('.work-source-save').click();
  await expect.poll(() => saved?.provider_settings?.fields?.title).toBe('new_title');
  expect(saved.provider_settings.fields.assignee).toBe('custom_owner');
  expect(saved.provider_settings.canonical_state_values).toEqual({ implementation_active: '2', ready_for_validation: '3', validation_running: '5', closed: '7' });
});
