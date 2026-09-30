const { test, expect } = require('@playwright/test');

function goalSnapshot(status = 'active') {
  return {
    goal: {
      id: 'goal-a',
      title: 'Ship verified Goal flow',
      description: 'Deliver bounded reviewed work with verified completion.',
      owner_identity_id: 'owner-a',
      status,
      priority: 'high',
      target_date: 1893456000,
      revision: 4,
      success_criteria: [
        {
          id: 'manual-review',
          description: 'Release owner verified the result',
          kind: 'manual',
          required: true,
        },
        {
          id: 'pass-rate',
          description: 'Release checks pass',
          kind: 'metric',
          metric_key: 'pass_rate',
          operator: 'gte',
          target_value: 90,
          unit: 'percent',
          required: true,
        },
      ],
      constraints: [{ id: 'c1', kind: 'policy', description: 'Use canonical ActionIntent boundary', reference: 'policy-1' }],
      risks: [{ id: 'r1', level: 'high', category: 'delivery', description: 'Provider outcome may be uncertain', mitigation: 'Reconcile intent' }],
      budget: { max_input_tokens: 12000, max_output_tokens: 3000, max_model_calls: 2, max_cost_usd: 1.5 },
      approval_requirements: [{ id: 'a1', trigger: 'completion', description: 'Admin approval required', required_role: 'admin' }],
      work_graph_bindings: [{ project_id: 'project-a', root_work_item_refs: ['team/project-a#42'] }],
    },
    progress: {
      project_count: 1,
      work_item_count: 2,
      completed: 1,
      failed: 0,
      cancelled: 0,
      active: 1,
      runnable: 0,
      blocked: 1,
      completion_fraction: 0.5,
    },
    health: {
      health: 'blocked',
      reasons: ['all non-terminal bound work is blocked'],
    },
  };
}

function proposals() {
  return [
    {
      id: 'proposal-a',
      goal_id: 'goal-a',
      goal_revision: 4,
      revision: 1,
      status: 'proposed',
      model_invocation_id: 'model-invocation-7',
      limits: { max_depth: 3, max_items: 20 },
      items: [{
        id: 'work-plan-a',
        project_id: 'project-a',
        title: 'Implement bounded slice',
        description: 'Implement the reviewed slice.',
        parent_item_id: null,
        blocked_by_item_ids: [],
        expected_result: 'Focused validation passes.',
        labels: [],
      }],
      commit_items: [],
    },
    {
      id: 'proposal-b',
      goal_id: 'goal-a',
      goal_revision: 4,
      revision: 3,
      status: 'committing',
      model_invocation_id: null,
      limits: { max_depth: 2, max_items: 4 },
      items: [{
        id: 'work-plan-b',
        project_id: 'project-a',
        title: 'Reconcile authoritative work',
        description: 'Wait for authoritative outcome.',
        parent_item_id: null,
        blocked_by_item_ids: [],
        expected_result: 'Provider receipt reconciled.',
        labels: [],
      }],
      commit_items: [{
        proposal_item_id: 'work-plan-b',
        project_id: 'project-a',
        binding_id: 'binding-1',
        correlation_id: 'corr-1',
        intent_id: 'action-intent-1',
        work_item_ref: null,
        state: 'uncertain',
        last_error: 'provider outcome unknown',
      }],
      commit_error: 'one authoritative create requires reconciliation',
    },
  ];
}

function completion(eligible = false) {
  return {
    id: eligible ? 'completion-pass' : 'completion-blocked',
    goal_id: 'goal-a',
    goal_revision: 4,
    eligible,
    evaluated_by: 'admin',
    reason: eligible ? 'all checks passed' : 'work still blocked',
    evaluated_at: 1890000000,
    blockers: eligible ? [] : ['work_item:team/project-a#43:bound Work Item is not completed'],
    criteria: [
      {
        criterion_id: 'manual-review',
        kind: 'manual',
        required: true,
        passed: eligible,
        description: 'Release owner verified the result',
        observed_value: null,
        source: 'approval:release-owner',
        reference: 'approval-7',
        findings: eligible ? [] : ['manual criterion was explicitly not verified'],
      },
      {
        criterion_id: 'pass-rate',
        kind: 'metric',
        required: true,
        passed: true,
        description: 'Release checks pass',
        metric_key: 'pass_rate',
        operator: 'gte',
        target_value: 90,
        observed_value: 96,
        source: 'ci:release',
        reference: 'run-42',
        findings: [],
      },
    ],
    work_items: [{
      project_id: 'project-a',
      work_item_ref: 'team/project-a#43',
      terminal_outcome: eligible ? 'completed' : null,
      passed: eligible,
      findings: eligible ? [] : ['bound Work Item is not completed'],
    }],
  };
}

async function mockGoalApis(page, posts) {
  let completionEligible = false;

  // Match both the collection and nested routes. A glob ending in goals**
  // matches the scoped collection query but not slash-separated detail paths.
  await page.route((url) => (
    url.pathname === '/api/goals' || url.pathname.startsWith('/api/goals/')
  ), async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const { pathname } = url;
    const method = request.method();
    let body;

    expect(url.searchParams.get('project_id')).toBe('project-a');

    if (method === 'POST' && pathname === '/api/goals/goal-a/completion-evaluations') {
      posts.push({ action: 'evaluate', body: request.postDataJSON() });
      completionEligible = true;
      body = { item: completion(true) };
    } else if (method === 'POST' && pathname === '/api/goals/goal-a/decompositions/generate') {
      posts.push({ action: 'generate', body: request.postDataJSON() });
      body = { proposal: proposals()[0] };
    } else if (method === 'POST' && pathname === '/api/goals/goal-a/decompositions/proposal-a/review') {
      posts.push({ action: 'review', body: request.postDataJSON() });
      body = { proposal: { ...proposals()[0], status: 'accepted' } };
    } else if (method === 'POST' && pathname === '/api/goals/goal-a/decompositions/proposal-b/commit/reconcile') {
      posts.push({ action: 'reconcile', body: request.postDataJSON() });
      body = { proposal: proposals()[1] };
    } else if (method === 'POST' && pathname === '/api/goals/goal-a/transition') {
      posts.push({ action: 'transition', body: request.postDataJSON() });
      body = { snapshot: goalSnapshot('completed') };
    } else if (pathname === '/api/goals/runtime-objectives/unbound') {
      body = { items: [], count: 0 };
    } else if (pathname === '/api/goals/goal-a/execution-bindings') {
      body = {
        items: [{
          id: 'goal-binding-a',
          goal_id: 'goal-a',
          goal_revision: 4,
          project_id: 'project-a',
          work_item_refs: ['team/project-a#42'],
          provider_id: 'openai',
          runtime_id: 'codex',
          agent_session_id: 'agent-session-7',
          thread_id: 'thread-7',
          execution_owner_id: 'release-validator',
          provider_native_objective_id: 'native-goal-9',
          native_objective_supported: true,
          capability_snapshot: ['persistent_sessions', 'native_execution_objectives'],
          status: 'blocked',
          cursor_ref: 'cursor-285',
          checkpoint_ref: 'checkpoint-284',
          last_turn_id: 'turn-285',
          last_execution_id: 'execution-285',
          stop_reason: 'approval required',
          heartbeat_at: 1890000100,
          updated_at: 1890000100,
        }],
        count: 1,
      };
    } else if (pathname === '/api/goals/goal-a/completion-evaluation') {
      body = { item: completion(completionEligible) };
    } else if (pathname === '/api/goals/goal-a/decompositions/events') {
      body = {
        items: [{ event_type: 'goal_decomposition.proposed', actor_id: 'planner', reason: 'bounded plan', occurred_at: 1889999000 }],
        count: 1,
      };
    } else if (pathname === '/api/goals/goal-a/decompositions') {
      body = { items: proposals(), count: 2 };
    } else if (pathname === '/api/goals/goal-a/revisions') {
      body = {
        items: [{ revision: 4, revised_by: 'admin', reason: 'commit Goal work', revised_at: 1889998000 }],
        count: 1,
      };
    } else if (pathname === '/api/goals/events') {
      body = {
        items: [{ event_type: 'goal_revised', actor_id: 'admin', reason: 'commit Goal work', occurred_at: 1889998000 }],
        count: 1,
      };
    } else if (pathname === '/api/goals/goal-a') {
      body = { snapshot: goalSnapshot() };
    } else if (pathname === '/api/goals') {
      body = { items: [goalSnapshot()], count: 1 };
    } else {
      await route.fulfill({
        status: 404,
        contentType: 'application/json',
        body: JSON.stringify({ detail: `unmocked Goal route: ${method} ${pathname}` }),
      });
      return;
    }

    await route.fulfill({ contentType: 'application/json', body: JSON.stringify(body) });
  });
}

test('Goal API mocks cover scoped collection, detail, events and nested mutations', async ({ page }) => {
  const posts = [];
  await mockGoalApis(page, posts);
  await page.goto('http://127.0.0.1:18766/tests/browser/goals_fixture.html');

  const responses = await page.evaluate(async () => {
    const paths = [
      '/api/goals?project_id=project-a',
      '/api/goals/goal-a?project_id=project-a',
      '/api/goals/events?goal_id=goal-a&project_id=project-a',
      '/api/goals/goal-a/decompositions/generate?project_id=project-a',
    ];
    return Promise.all(paths.map(async (path, index) => {
      const response = await fetch(path, index === 3 ? {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project_ids: ['project-a'] }),
      } : undefined);
      return { status: response.status, body: await response.json() };
    }));
  });

  expect(responses.map((response) => response.status)).toEqual([200, 200, 200, 200]);
  expect(responses[0].body.items[0].goal.id).toBe('goal-a');
  expect(responses[1].body.snapshot.goal.id).toBe('goal-a');
  expect(responses[2].body.items[0].event_type).toBe('goal_revised');
  expect(responses[3].body.proposal.id).toBe('proposal-a');
  expect(posts).toEqual([{ action: 'generate', body: { project_ids: ['project-a'] } }]);
});

test('Goal workspace exposes canonical health, provenance, decomposition and commit blockers', async ({ page }) => {
  const posts = [];
  await mockGoalApis(page, posts);
  await page.goto('http://127.0.0.1:18766/tests/browser/goals_fixture.html');
  await page.locator('#goals-button').click();

  const dialog = page.locator('#goals-dialog');
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText('Ship verified Goal flow');
  await expect(dialog).toContainText('all non-terminal bound work is blocked');
  await expect(dialog).toContainText('model-invocation-7');
  await expect(dialog).toContainText('action-intent-1');
  await expect(dialog).toContainText('provider outcome unknown');
  await expect(dialog).toContainText('Runtime: blocked');
  await expect(dialog).toContainText('Canonical Goal remains authoritative');
  await expect(dialog).toContainText('agent-session-7');
  await expect(dialog).toContainText('release-validator');
  await expect(dialog).toContainText('cursor-285');
  await expect(dialog).toContainText('checkpoint-284');
  await expect(dialog).toContainText('approval required');
  await expect(dialog.locator('[data-criterion-id="manual-review"] .goal-observation-source')).toHaveValue('approval:release-owner');
  await expect(dialog).toContainText('work_item:team/project-a#43');
  await expect(dialog).toContainText('Revision 4');
  expect(posts).toEqual([]);
});

test('Goal planning is explicit and review/reconcile mutations use canonical APIs', async ({ page }) => {
  const posts = [];
  await mockGoalApis(page, posts);
  await page.goto('http://127.0.0.1:18766/tests/browser/goals_fixture.html');
  await page.locator('#goals-button').click();

  const dialog = page.locator('#goals-dialog');
  await dialog.locator('.goal-generate').click();
  await expect.poll(() => posts.some((item) => item.action === 'generate')).toBeTruthy();
  const generated = posts.find((item) => item.action === 'generate');
  expect(generated.body.project_ids).toEqual(['project-a']);
  expect(generated.body.limits).toEqual({ max_depth: 3, max_items: 20 });

  await dialog.locator('[data-proposal-id="proposal-a"] .goal-proposal-accept').click();
  await expect.poll(() => posts.some((item) => item.action === 'review')).toBeTruthy();
  expect(posts.find((item) => item.action === 'review').body.decision).toBe('accept');

  await dialog.locator('[data-proposal-id="proposal-b"] .goal-proposal-reconcile').click();
  await expect.poll(() => posts.some((item) => item.action === 'reconcile')).toBeTruthy();
});

test('Goal completion posts explicit observations then uses returned evaluation id', async ({ page }) => {
  const posts = [];
  await mockGoalApis(page, posts);
  await page.goto('http://127.0.0.1:18766/tests/browser/goals_fixture.html');
  await page.locator('#goals-button').click();

  const dialog = page.locator('#goals-dialog');
  const manual = dialog.locator('[data-criterion-id="manual-review"] .goal-observation-verified');
  await manual.check();
  await dialog.locator('.goal-evaluate-completion').click();

  await expect.poll(() => posts.some((item) => item.action === 'evaluate')).toBeTruthy();
  const evaluation = posts.find((item) => item.action === 'evaluate');
  expect(evaluation.body.observations).toEqual(expect.arrayContaining([
    expect.objectContaining({
      criterion_id: 'manual-review',
      verified: true,
      source: 'approval:release-owner',
      reference: 'approval-7',
    }),
    expect.objectContaining({
      criterion_id: 'pass-rate',
      observed_value: 96,
      source: 'ci:release',
      reference: 'run-42',
    }),
  ]));

  await expect(dialog.locator('.goal-complete')).toBeEnabled();
  await dialog.locator('.goal-complete').click();
  await expect.poll(() => posts.some((item) => item.action === 'transition')).toBeTruthy();
  expect(posts.find((item) => item.action === 'transition').body).toMatchObject({
    status: 'completed',
    completion_evaluation_id: 'completion-pass',
  });
});


function projectSnapshot(goalId, projectId, title) {
  const snapshot = goalSnapshot();
  snapshot.goal.id = goalId;
  snapshot.goal.title = title;
  snapshot.goal.work_graph_bindings = [{
    project_id: projectId,
    root_work_item_refs: [],
  }];
  snapshot.progress = {
    project_count: 1,
    work_item_count: 0,
    completed: 0,
    failed: 0,
    cancelled: 0,
    active: 0,
    runnable: 0,
    blocked: 0,
    completion_fraction: 0,
  };
  snapshot.health = { health: 'unknown', reasons: ['goal has no bound subordinate work'] };
  return snapshot;
}

async function mockProjectScopedGoalReads(page, { listDelay = {} } = {}) {
  const snapshots = {
    'project-a': projectSnapshot('goal-a', 'project-a', 'Project A Goal'),
    'project-b': projectSnapshot('goal-b', 'project-b', 'Project B Goal'),
  };

  await page.route('**/api/goals/runtime-objectives/unbound?*', async (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({ items: [], count: 0 }),
  }));
  await page.route('**/api/goals/events?*', async (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({ items: [], count: 0 }),
  }));
  await page.route('**/api/goals/**', async (route) => {
    const url = new URL(route.request().url());
    if (
      url.pathname === '/api/goals/events'
      || url.pathname === '/api/goals/runtime-objectives/unbound'
    ) {
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({ items: [], count: 0 }),
      });
      return;
    }
    const parts = url.pathname.split('/').filter(Boolean);
    const goalId = parts[2];
    const snapshot = Object.values(snapshots).find((item) => item.goal.id === goalId);
    if (!snapshot) {
      await route.fulfill({ status: 404, contentType: 'application/json', body: JSON.stringify({ detail: 'goal not found' }) });
      return;
    }
    if (url.pathname.endsWith('/revisions')) {
      await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ items: [], count: 0 }) });
      return;
    }
    if (url.pathname.endsWith('/decompositions/events')) {
      await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ items: [], count: 0 }) });
      return;
    }
    if (url.pathname.endsWith('/decompositions')) {
      await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ items: [], count: 0 }) });
      return;
    }
    if (url.pathname.endsWith('/completion-evaluation')) {
      await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ item: null }) });
      return;
    }
    if (url.pathname.endsWith('/execution-bindings')) {
      await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ items: [], count: 0 }) });
      return;
    }
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({ snapshot }),
    });
  });
  await page.route('**/api/goals?*', async (route) => {
    const url = new URL(route.request().url());
    const projectId = url.searchParams.get('project_id');
    const delay = Number(listDelay[projectId] || 0);
    if (delay) await new Promise((resolve) => setTimeout(resolve, delay));
    const snapshot = snapshots[projectId];
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        items: snapshot ? [snapshot] : [],
        count: snapshot ? 1 : 0,
      }),
    });
  });
}

async function switchProject(page, projectId) {
  await page.evaluate((nextProjectId) => {
    document.body.dataset.projectId = nextProjectId;
    window.dispatchEvent(new CustomEvent('codex:project-changed', {
      detail: { projectId: nextProjectId },
    }));
  }, projectId);
}

test('Goal workspace replaces populated Project state and shows an empty Project without stale Goals', async ({ page }) => {
  await mockProjectScopedGoalReads(page);
  await page.goto('http://127.0.0.1:18766/tests/browser/goals_fixture.html');
  await page.locator('#goals-button').click();

  const dialog = page.locator('#goals-dialog');
  await expect(dialog).toContainText('Project A Goal');

  await switchProject(page, 'project-b');
  await expect(dialog).toContainText('Project B Goal');
  await expect(dialog).not.toContainText('Project A Goal');

  await switchProject(page, 'project-empty');
  await expect(dialog).toContainText('No Goals exist in this Project.');
  await expect(dialog).not.toContainText('Project B Goal');
});

test('rapid Project switching ignores late Goal responses from the previous Project', async ({ page }) => {
  await mockProjectScopedGoalReads(page, {
    listDelay: { 'project-a': 250 },
  });
  await page.goto('http://127.0.0.1:18766/tests/browser/goals_fixture.html');

  await page.locator('#goals-button').click();
  await switchProject(page, 'project-b');

  const dialog = page.locator('#goals-dialog');
  await expect(dialog).toContainText('Project B Goal');
  await page.waitForTimeout(350);
  await expect(dialog).toContainText('Project B Goal');
  await expect(dialog).not.toContainText('Project A Goal');
});

test('late Goal detail failures cannot replace the newly selected Project', async ({ page }) => {
  await mockProjectScopedGoalReads(page);
  let release;
  let pending = false;
  const held = new Promise(resolve => { release = resolve; });
  await page.route('**/api/goals/goal-a?*', async route => {
    pending = true;
    await held;
    await route.fulfill({ status: 503, json: { detail: 'Old Project A detail failed' } });
  });
  await page.goto('http://127.0.0.1:18766/tests/browser/goals_fixture.html');
  await page.locator('#goals-button').click();
  await expect.poll(() => pending).toBe(true);
  await switchProject(page, 'project-b');
  await expect(page.locator('.goal-detail')).toContainText('Project B Goal');
  const response = page.waitForResponse('**/api/goals/goal-a?*');
  release();
  await (await response).finished();
  await page.evaluate(() => new Promise(requestAnimationFrame));
  await expect(page.locator('.goal-detail')).toContainText('Project B Goal');
  await expect(page.locator('#goals-dialog')).not.toContainText('Old Project A detail failed');
});

test('Goal mutation responses are fenced even after A to B to A navigation', async ({ page }) => {
  await mockProjectScopedGoalReads(page);
  let release;
  let pending = false;
  const held = new Promise(resolve => { release = resolve; });
  await page.route('**/api/goals/goal-a/decompositions/generate?*', async route => {
    expect(new URL(route.request().url()).searchParams.get('project_id')).toBe('project-a');
    pending = true;
    await held;
    await route.fulfill({ status: 409, json: { detail: 'Old generation mutation failed' } });
  });
  await page.goto('http://127.0.0.1:18766/tests/browser/goals_fixture.html');
  await page.locator('#goals-button').click();
  await expect(page.locator('.goal-detail')).toContainText('Project A Goal');
  await page.locator('.goal-generate-reason').fill('Explicit operator request');
  await page.locator('.goal-generate').click();
  await expect.poll(() => pending).toBe(true);
  await switchProject(page, 'project-b');
  await expect(page.locator('.goal-detail')).toContainText('Project B Goal');
  await switchProject(page, 'project-a');
  await expect(page.locator('.goals-status')).toHaveText('Up to date');
  const response = page.waitForResponse('**/api/goals/goal-a/decompositions/generate?*');
  release();
  await (await response).finished();
  await page.evaluate(() => new Promise(requestAnimationFrame));
  await expect(page.locator('.goals-status')).toHaveText('Up to date');
  await expect(page.locator('#goals-dialog')).not.toContainText('Old generation mutation failed');
});

test('a denied Goal deep link does not silently substitute the first Goal', async ({ page }) => {
  await mockGoalApis(page, []);
  await page.route('**/api/goals/not-visible**', route => route.fulfill({ status: 403, json: { detail: 'Referenced Goal is not visible in this Project' } }));
  await page.goto('http://127.0.0.1:18766/tests/browser/goals_fixture.html?consumer_type=goal&consumer_id=not-visible');
  await page.locator('#goals-button').click();
  await expect(page.locator('.goal-detail')).toContainText('Referenced Goal is not visible');
  await expect(page.locator('.goal-detail')).not.toContainText('Ship verified Goal flow');
});
