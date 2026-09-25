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

  await page.route('**/api/goals/runtime-objectives/unbound', async (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({ items: [], count: 0 }),
  }));
  await page.route('**/api/goals/goal-a/execution-bindings', async (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({
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
    }),
  }));
  await page.route('**/api/goals/goal-a/completion-evaluation', async (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({ item: completion(completionEligible) }),
  }));
  await page.route('**/api/goals/goal-a/completion-evaluations', async (route) => {
    posts.push({ action: 'evaluate', body: route.request().postDataJSON() });
    completionEligible = true;
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ item: completion(true) }) });
  });
  await page.route('**/api/goals/goal-a/decompositions/generate', async (route) => {
    posts.push({ action: 'generate', body: route.request().postDataJSON() });
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ proposal: proposals()[0] }) });
  });
  await page.route('**/api/goals/goal-a/decompositions/proposal-a/review', async (route) => {
    posts.push({ action: 'review', body: route.request().postDataJSON() });
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ proposal: { ...proposals()[0], status: 'accepted' } }) });
  });
  await page.route('**/api/goals/goal-a/decompositions/proposal-b/commit/reconcile', async (route) => {
    posts.push({ action: 'reconcile', body: route.request().postDataJSON() });
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ proposal: proposals()[1] }) });
  });
  await page.route('**/api/goals/goal-a/transition', async (route) => {
    posts.push({ action: 'transition', body: route.request().postDataJSON() });
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ snapshot: goalSnapshot('completed') }) });
  });
  await page.route('**/api/goals/goal-a/decompositions/events', async (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({ items: [{ event_type: 'goal_decomposition.proposed', actor_id: 'planner', reason: 'bounded plan', occurred_at: 1889999000 }], count: 1 }),
  }));
  await page.route('**/api/goals/goal-a/decompositions', async (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({ items: proposals(), count: 2 }),
  }));
  await page.route('**/api/goals/goal-a/revisions', async (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({ items: [{ revision: 4, revised_by: 'admin', reason: 'commit Goal work', revised_at: 1889998000 }], count: 1 }),
  }));
  await page.route('**/api/goals/events?goal_id=*', async (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({ items: [{ event_type: 'goal_revised', actor_id: 'admin', reason: 'commit Goal work', occurred_at: 1889998000 }], count: 1 }),
  }));
  await page.route('**/api/goals/goal-a', async (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({ snapshot: goalSnapshot() }),
  }));
  await page.route('**/api/goals', async (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({ items: [goalSnapshot()], count: 1 }),
  }));
}

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
