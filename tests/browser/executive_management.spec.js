const { test, expect } = require('@playwright/test');

function roleCatalog() {
  return {
    roles: [
      {
        id: 'cto',
        name: 'CTO',
        title: 'Chief Technology Officer',
        description: 'Architecture and technical risk.',
        lifecycle: 'active',
        responsibilities: ['govern architecture', 'surface migration risk'],
        observable_information: ['goal', 'decision', 'work_item', 'work_graph', 'evidence'],
        event_subscriptions: ['approval.transition', 'failure.observed'],
        keywords: ['architecture', 'cloud'],
        consultation_roles: ['cfo'],
        authority: {
          allowed_proposal_kinds: ['goal', 'decision', 'work', 'escalation'],
          can_materialize: true,
          external_side_effects: false,
          required_materialization_capabilities: {
            goal: 'executive.goal.materialize',
            decision: 'executive.decision.materialize',
            work: 'executive.work.materialize',
          },
        },
        max_context_items: 20,
        max_consultations: 3,
        instructions: 'Use canonical state.',
      },
      {
        id: 'cfo',
        name: 'CFO',
        title: 'Chief Financial Officer',
        description: 'Cost, budget and runway.',
        lifecycle: 'active',
        responsibilities: ['evaluate cost trade-offs'],
        observable_information: ['goal', 'decision', 'evidence'],
        event_subscriptions: ['approval.transition'],
        keywords: ['cost', 'budget'],
        consultation_roles: ['cto'],
        authority: {
          allowed_proposal_kinds: ['goal', 'decision', 'escalation'],
          can_materialize: true,
          external_side_effects: false,
          required_materialization_capabilities: {
            goal: 'executive.goal.materialize',
            decision: 'executive.decision.materialize',
            work: 'executive.work.materialize',
          },
        },
        max_context_items: 20,
        max_consultations: 3,
        instructions: 'Use measured evidence.',
      },
    ],
    fallback_role_id: 'cto',
    max_roles_per_activation: 3,
  };
}

function activation(status = 'escalated') {
  return {
    id: 'executive-activation-a',
    organization_id: 'local',
    workspace_id: 'default',
    initiated_by: 'owner-a',
    subject: 'Architecture cost trade-off',
    request: 'Review the bounded architecture migration and cost trade-off.',
    trigger_kind: 'event',
    trigger_ref: 'event-approval-a',
    event_type: 'approval.transition',
    project_id: 'project-a',
    goal_ids: ['goal-a'],
    decision_ids: ['decision-a'],
    work_item_refs: ['project-a:reference:7'],
    evidence_ids: ['evidence-a'],
    role_catalog: {
      definition_id: 'executive.roles.default',
      kind: 'executive-role-catalog',
      schema_version: '1.0',
      record_id: 'definition-executive-r7',
      checksum_sha256: 'abc',
      scope_type: 'global',
      scope_id: null,
      revision: 7,
    },
    selections: [
      { role_id: 'cto', score: 8, reasons: ['keyword:architecture', 'subscribed to approval.transition'], explicit: false },
      { role_id: 'cfo', score: 6, reasons: ['keyword:cost', 'subscribed to approval.transition'], explicit: false },
    ],
    budget: {
      max_input_tokens: 40000,
      max_output_tokens: 12000,
      max_model_calls: 3,
      max_cost_usd: 5,
    },
    status,
    context: {
      goals: [
        {
          goal: { id: 'goal-a', title: 'Improve governed delivery', status: 'active' },
          health: { health: 'at_risk' },
          progress: { work_item_count: 2, completed: 1 },
        },
      ],
      decisions: [
        { id: 'decision-a', title: 'Choose migration path', status: 'approved' },
      ],
      work_items: [
        { ref: 'project-a:reference:7', title: 'Implement migration', current_stage: 'implementation_active' },
      ],
      work_graphs: [],
      evidence: [
        { id: 'evidence-a', summary: 'Architecture review passed.', lifecycle: 'valid' },
      ],
    },
    consultations: [
      {
        id: 'consult-cto',
        role_id: 'cto',
        role_definition: { record_id: 'definition-executive-r7' },
        output: {
          summary: 'Migration is technically viable.',
          recommendation: 'Use the bounded migration.',
          risks: ['migration defects'],
          assumptions: [],
          disagreement: ['timing'],
          proposals: [],
        },
        model_invocation_id: 'invocation-cto',
        started_at: 1890000000,
        completed_at: 1890000010,
      },
      {
        id: 'consult-cfo',
        role_id: 'cfo',
        role_definition: { record_id: 'definition-executive-r7' },
        output: {
          summary: 'Current cost evidence is incomplete.',
          recommendation: 'Limit irreversible spend.',
          risks: ['unbounded spend'],
          assumptions: ['cost data is incomplete'],
          disagreement: ['timing'],
          proposals: [],
        },
        model_invocation_id: 'invocation-cfo',
        started_at: 1890000000,
        completed_at: 1890000010,
      },
    ],
    synthesis: {
      recommendation: 'Proceed only with the reversible bounded phase.',
      rationale: 'Technical direction aligns; timing remains disputed.',
      disagreement: ['timing remains disputed'],
      escalation_required: true,
      escalation_reason: 'Material CTO/CFO timing disagreement.',
      model_invocation_id: 'invocation-synthesis',
    },
    proposals: [
      {
        id: 'proposal-work',
        role_id: 'cto',
        kind: 'work',
        title: 'Materialize approved migration work',
        rationale: 'The approved Decision can now create canonical Work.',
        payload: {},
        status: 'materialized',
        authority_capability: 'executive.work.materialize',
        authority_reasons: ['allowed by role local-admin grant local-admin.all'],
        resulting_ref: 'decision:decision-a:work:intent-a,intent-b',
        materialized_by: 'owner-a',
        materialized_at: 1890000100,
      },
      {
        id: 'proposal-goal',
        role_id: 'cfo',
        kind: 'goal',
        title: 'Track migration cost',
        rationale: 'Make the cost outcome measurable.',
        payload: {},
        status: 'proposed',
        authority_capability: null,
        authority_reasons: [],
        resulting_ref: null,
        materialized_by: null,
        materialized_at: null,
      },
    ],
    failure_reason: null,
    created_at: 1890000000,
    updated_at: 1890000100,
    completed_at: 1890000100,
    revision: 3,
  };
}

async function installRoutes(page, options = {}) {
  const current = { activation: activation(options.status || 'escalated') };
  await page.route(/\/api\/executive\/(?:roles|activations(?:[/?].*)?)$/, async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();
    let body;

    if (path === '/api/executive/roles' && method === 'GET') {
      body = {
        catalog: roleCatalog(),
        definition: {
          definition_id: 'executive.roles.default',
          kind: 'executive-role-catalog',
          schema_version: '1.0',
          record_id: 'definition-executive-r7',
          revision: 7,
        },
      };
    } else if (path === '/api/executive/activations' && method === 'GET') {
      body = { items: [current.activation], count: 1 };
    } else if (path === '/api/executive/activations' && method === 'POST') {
      const submitted = JSON.parse(request.postData() || '{}');
      current.activation = {
        ...activation('planned'),
        id: 'executive-activation-new',
        subject: submitted.subject,
        request: submitted.request,
        project_id: submitted.project_id,
        selections: [
          { role_id: 'cto', score: 100, reasons: ['explicitly requested'], explicit: true },
        ],
        consultations: [],
        synthesis: null,
        proposals: [],
        revision: 1,
      };
      body = { item: current.activation };
    } else if (
      path === `/api/executive/activations/${current.activation.id}`
      && method === 'GET'
    ) {
      body = { item: current.activation };
    } else if (
      path === `/api/executive/activations/${current.activation.id}/revisions`
      && method === 'GET'
    ) {
      body = {
        items: [
          {
            activation_id: current.activation.id,
            revision: current.activation.revision,
            snapshot: current.activation,
            reason: 'Executive consultation completed',
            revised_by: 'owner-a',
            revised_at: 1890000100,
          },
        ],
        count: 1,
      };
    } else if (
      path === `/api/executive/activations/${current.activation.id}/consult`
      && method === 'POST'
    ) {
      current.activation = {
        ...current.activation,
        status: 'completed',
        consultations: [activation().consultations[0]],
        synthesis: {
          recommendation: 'Proceed with the bounded option.',
          rationale: 'One relevant role completed the review.',
          disagreement: [],
          escalation_required: false,
          escalation_reason: null,
          model_invocation_id: null,
        },
        revision: current.activation.revision + 1,
      };
      body = { item: current.activation };
    } else if (
      path.endsWith('/proposals/proposal-goal/materialize')
      && method === 'POST'
    ) {
      if (options.denyMaterialization) {
        return route.fulfill({
          status: 403,
          contentType: 'application/json',
          body: JSON.stringify({ detail: 'Executive proposal materialization denied: approval required' }),
        });
      }
      current.activation = {
        ...current.activation,
        proposals: current.activation.proposals.map((item) =>
          item.id === 'proposal-goal'
            ? {
                ...item,
                status: 'materialized',
                authority_capability: 'executive.goal.materialize',
                authority_reasons: ['allowed by canonical authority'],
                resulting_ref: 'goal:goal-new',
                materialized_by: 'owner-a',
              }
            : item
        ),
        revision: current.activation.revision + 1,
      };
      body = { item: current.activation };
    } else {
      return route.fulfill({ status: 404, contentType: 'application/json', body: '{}' });
    }

    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(body),
    });
  });
  return current;
}

test('Executive management explains activation, disagreement, authority and canonical consequences', async ({ page }) => {
  await installRoutes(page);
  await page.goto('http://127.0.0.1:18766/tests/browser/executive_management_fixture.html');

  await page.evaluate(() => {
    window.__openedCanonical = [];
    window.addEventListener('codex:open-goal', (event) => {
      window.__openedCanonical.push({ kind: 'goal', id: event.detail.goalId });
    });
    window.addEventListener('codex:open-decision', (event) => {
      window.__openedCanonical.push({ kind: 'decision', id: event.detail.decisionId });
    });
  });

  await page.locator('#executive-management-button').click();
  const dialog = page.locator('#executive-management-dialog');
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText('Why was this Executive activated?');
  await expect(dialog).toContainText('approval.transition');
  await expect(dialog).toContainText('keyword:architecture');
  await expect(dialog).toContainText('subscribed to approval.transition');
  await expect(dialog).toContainText('Chief Technology Officer');
  await expect(dialog).toContainText('Chief Financial Officer');
  await expect(dialog).toContainText('Migration is technically viable.');
  await expect(dialog).toContainText('Current cost evidence is incomplete.');
  await expect(dialog).toContainText('Material CTO/CFO timing disagreement.');
  await expect(dialog.locator('.exec-mgmt-badge.escalated').first()).toHaveText('escalated');
  await expect(dialog).toContainText('executive.work.materialize');
  await expect(dialog).toContainText('allowed by role local-admin grant local-admin.all');
  await expect(dialog).toContainText('External consequences still flow through approved Decision');

  await dialog.locator('[data-canonical-kind="goal"][data-canonical-id="goal-a"]').click();
  await dialog.locator('[data-canonical-kind="decision"][data-canonical-id="decision-a"]').first().click();
  expect(await page.evaluate(() => window.__openedCanonical)).toEqual([
    { kind: 'goal', id: 'goal-a' },
    { kind: 'decision', id: 'decision-a' },
  ]);

  await dialog.locator('[data-canonical-kind="action-intent"][data-canonical-id="intent-a"]').click();
  await expect(page.locator('#developer-panel')).toHaveAttribute('open', '');
  await expect(page.locator('#action-intent-search')).toHaveValue('intent-a');

  await dialog.locator('[data-canonical-kind="evidence"][data-canonical-id="evidence-a"]').click();
  await expect(page.locator('#artifact-evidence-search')).toHaveValue('evidence-a');
});

test('Executive management creates a planned activation separately from consultation', async ({ page }) => {
  const current = await installRoutes(page);
  await page.goto('http://127.0.0.1:18766/tests/browser/executive_management_fixture.html');
  await page.locator('#executive-management-button').click();

  await page.locator('.exec-mgmt-new').click();
  const form = page.locator('.exec-mgmt-create-form');
  await form.locator('[name="subject"]').fill('Architecture review');
  await form.locator('[name="request"]').fill('Review the architecture migration.');
  await form.locator('[name="project_id"]').fill('project-a');
  await form.locator('[name="requested_role_ids"]').fill('cto');
  await form.locator('button[type="submit"]').click();

  await expect(page.locator('.exec-mgmt-detail')).toContainText('Architecture review');
  await expect(page.locator('.exec-mgmt-detail')).toContainText('planned');
  await expect(page.locator('.exec-mgmt-detail')).toContainText('explicitly requested');
  await expect(page.locator('.exec-mgmt-consult')).toBeVisible();

  expect(current.activation.status).toBe('planned');
  await page.locator('.exec-mgmt-consult').click();
  await expect(page.locator('.exec-mgmt-detail')).toContainText('Synthesis complete');
  await expect(page.locator('.exec-mgmt-detail')).toContainText('Proceed with the bounded option.');
  expect(current.activation.status).toBe('completed');
});

test('authority denial leaves Executive proposal advisory and uses only canonical materialization endpoint', async ({ page }) => {
  await installRoutes(page, { denyMaterialization: true });
  const requests = [];
  page.on('request', (request) => {
    if (request.method() === 'POST') requests.push(new URL(request.url()).pathname);
  });

  await page.goto('http://127.0.0.1:18766/tests/browser/executive_management_fixture.html');
  await page.locator('#executive-management-button').click();

  const proposal = page.locator('.exec-mgmt-proposal', { hasText: 'Track migration cost' });
  await expect(proposal).toContainText('proposed');
  await proposal.locator('[data-materialize-proposal="proposal-goal"]').click();

  await expect(page.locator('.exec-mgmt-status')).toContainText('denied');
  await expect(proposal).toContainText('proposed');
  expect(requests).toEqual([
    '/api/executive/activations/executive-activation-a/proposals/proposal-goal/materialize',
  ]);
});

test('Executive management remains navigable on mobile', async ({ page }) => {
  await installRoutes(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('http://127.0.0.1:18766/tests/browser/executive_management_fixture.html');
  await page.locator('#executive-management-button').click();

  const dialog = page.locator('#executive-management-dialog');
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText('Architecture cost trade-off');
  await page.locator('[data-role-id="cto"]').click();
  await expect(dialog).toContainText('Responsibilities');
  await expect(dialog).toContainText('External side effects');
  await expect(dialog).toContainText('forbidden');
});
