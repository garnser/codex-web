const { test, expect } = require('@playwright/test');

function decision(status = 'awaiting_approval') {
  return {
    id: 'decision-a',
    organization_id: 'org-a',
    workspace_id: 'ws-a',
    project_id: 'project-a',
    title: 'Choose rollout strategy',
    question: 'Which bounded rollout strategy should we use?',
    initiator_identity_id: 'owner-a',
    participants: [
      { id: 'participant-risk', role: 'risk', perspective: 'Identify governance failure modes.', identity_id: null },
      { id: 'participant-operator', role: 'operator', perspective: 'Assess operational practicality.', identity_id: null },
    ],
    evidence: [
      {
        id: 'decision-evidence-1',
        kind: 'metric_snapshot',
        metric_id: 'metric-a',
        metric_snapshot_id: 'metric-snapshot-a',
        metric_revision: 3,
        metric_freshness: 'stale',
        observed_value: 250,
        unit: 'ms',
        observation_ids: ['observation-a'],
        window_start: 1889999700,
        window_end: 1890000000,
        summary: 'newest observation is older than freshness policy',
      },
      {
        id: 'decision-evidence-2',
        kind: 'evidence',
        evidence_id: 'evidence-a',
        summary: 'Architecture review completed.',
      },
    ],
    assumptions: ['Provider contracts remain available.'],
    constraints: ['No direct provider mutation.'],
    options: [
      {
        id: 'option-a',
        title: 'Bounded rollout',
        description: 'Ship the bounded canonical implementation.',
        pros: ['small blast radius'],
        cons: ['migration work'],
        risks: ['integration defects'],
      },
      {
        id: 'option-b',
        title: 'Defer',
        description: 'Defer the implementation.',
        pros: ['no immediate change'],
        cons: ['capability gap remains'],
        risks: ['chat-only state'],
      },
    ],
    importance: 'high',
    budget: {
      max_input_tokens: 24000,
      max_output_tokens: 6000,
      max_model_calls: 3,
      max_cost_usd: 5,
    },
    limits: { max_participants: 2, max_rounds: 1 },
    status,
    deliberation_rounds: [
      {
        id: 'round-a',
        round_number: 1,
        analyses: [
          {
            participant_id: 'participant-risk',
            preferred_option_id: 'option-a',
            analysis: 'Viable with explicit stale-evidence uncertainty.',
            pros: ['bounded'],
            cons: [],
            risks: ['stale evidence'],
            uncertainty: ['metric freshness'],
            model_invocation_id: 'invocation-risk',
          },
          {
            participant_id: 'participant-operator',
            preferred_option_id: 'option-a',
            analysis: 'Operationally tractable.',
            pros: ['simple'],
            cons: [],
            risks: [],
            uncertainty: [],
            model_invocation_id: 'invocation-operator',
          },
        ],
        recommendation: {
          option_id: 'option-a',
          rationale: 'Minimum sufficient bounded path.',
          confidence: 0.72,
          uncertainty: ['stale metric'],
        },
        dissent: [],
        synthesis_model_invocation_id: 'invocation-synthesis',
        started_at: 1890000000,
        completed_at: 1890000010,
      },
    ],
    recommendation: {
      option_id: 'option-a',
      rationale: 'Minimum sufficient bounded path.',
      confidence: 0.72,
      uncertainty: ['stale metric'],
    },
    dissent: [],
    approval_request_id: status === 'awaiting_approval' ? 'approval-a' : null,
    approval_target_revision: status === 'awaiting_approval' ? 3 : null,
    final_decision: null,
    review_at: 1893456000,
    expires_at: null,
    superseded_by_decision_id: null,
    post_execution_reviews: [],
    revision: 3,
    created_at: 1889999000,
    updated_at: 1890000010,
  };
}

function approval() {
  return {
    id: 'approval-a',
    status: 'approved',
    target: {
      operation: 'decision.approve',
      object_type: 'decision',
      object_id: 'decision-a',
      target_version: '3',
      target_digest: 'abc123',
    },
    requirement: {
      quorum: 1,
      required_assurance: 'mfa',
    },
  };
}

async function mockDecisionApis(page, posts, initialStatus = 'awaiting_approval') {
  let current = decision(initialStatus);
  await page.route(/\/api\/decisions(?:[/?].*)?$/, async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    const method = route.request().method();

    if (path === '/api/decisions' && method === 'GET') {
      return route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({ items: [current], count: 1 }),
      });
    }
    if (path === '/api/decisions/decision-a' && method === 'GET') {
      return route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          item: current,
          approval_request: current.approval_request_id ? approval() : null,
          model_usage: { decision_id: 'decision-a', calls: 3, input_tokens: 300, output_tokens: 150, cost_usd: 0.01 },
        }),
      });
    }
    if (path === '/api/decisions/decision-a/revisions' && method === 'GET') {
      return route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          items: [
            { decision_id: 'decision-a', revision: 1, reason: 'Decision created', revised_by: 'owner-a', revised_at: 1889999000 },
            { decision_id: 'decision-a', revision: 2, reason: 'bounded deliberation round 1', revised_by: 'owner-a', revised_at: 1890000005 },
            { decision_id: 'decision-a', revision: 3, reason: 'Review recommendation', revised_by: 'owner-a', revised_at: 1890000010 },
          ],
          count: 3,
        }),
      });
    }
    if (path === '/api/decisions/decision-a/events' && method === 'GET') {
      return route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          items: [
            { event_type: 'decision.created', revision: 1, reason: 'Decision created' },
            { event_type: 'decision.deliberated', revision: 2, reason: 'bounded deliberation round 1' },
            { event_type: 'decision.awaiting_approval', revision: 3, reason: 'Review recommendation' },
          ],
          count: 3,
        }),
      });
    }
    if (path === '/api/decisions/decision-a/approval/finalize' && method === 'POST') {
      posts.push({ action: 'finalize', body: route.request().postDataJSON() });
      current = {
        ...current,
        status: 'approved',
        revision: 4,
        approval_request_id: 'approval-a',
        final_decision: {
          option_id: 'option-a',
          rationale: current.recommendation.rationale,
          confidence: 0.72,
          uncertainty: ['stale metric'],
          approval_request_id: 'approval-a',
          decided_at: 1890000020,
        },
      };
      return route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({ item: current }),
      });
    }
    if (path === '/api/decisions/decision-a/deliberate' && method === 'POST') {
      posts.push({ action: 'deliberate', body: route.request().postDataJSON() });
      return route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({ item: current }),
      });
    }
    if (path === '/api/decisions/decision-a/approval-request' && method === 'POST') {
      posts.push({ action: 'approval-request', body: route.request().postDataJSON() });
      current = { ...current, status: 'awaiting_approval', approval_request_id: 'approval-a' };
      return route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({ item: current, approval_request: approval() }),
      });
    }
    return route.fulfill({ status: 404, contentType: 'application/json', body: '{}' });
  });
}

test('Decision workspace exposes canonical evidence, bounded reasoning and ApprovalRequest state', async ({ page }) => {
  const posts = [];
  await mockDecisionApis(page, posts);
  await page.goto('http://127.0.0.1:18766/tests/browser/decisions_fixture.html');

  await expect(page.locator('#decisions-button')).toBeVisible();
  await page.locator('#decisions-button').click();

  const dialog = page.locator('#decisions-dialog');
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText('Choose rollout strategy');
  await expect(dialog).toContainText('metric-snapshot-a');
  await expect(dialog.locator('.decision-freshness.stale')).toHaveText('stale');
  await expect(dialog).toContainText('observation-a');
  await expect(dialog).toContainText('evidence-a');
  await expect(dialog).toContainText('invocation-synthesis');
  await expect(dialog).toContainText('3 / 3');
  await expect(dialog).toContainText('approval-a');
  await expect(dialog).toContainText('abc123');
  await expect(dialog).toContainText('decision.awaiting_approval');

  await dialog.locator('.decision-finalize').click();
  await expect.poll(() => posts.some((item) => item.action === 'finalize')).toBeTruthy();
  await expect(dialog.locator('.decision-status.approved').first()).toHaveText('approved');
  await expect(dialog).toContainText('Approved final: option-a');
});

test('Decision workspace uses canonical mutation endpoints and remains usable on mobile', async ({ page }) => {
  const posts = [];
  await mockDecisionApis(page, posts, 'analysis');
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('http://127.0.0.1:18766/tests/browser/decisions_fixture.html');
  await page.locator('#decisions-button').click();

  const dialog = page.locator('#decisions-dialog');
  await expect(dialog).toBeVisible();
  await expect(dialog.locator('.decision-request-approval')).toBeVisible();
  await dialog.locator('.decision-request-approval').click();

  await expect.poll(() => posts.some((item) => item.action === 'approval-request')).toBeTruthy();
  const requestPost = posts.find((item) => item.action === 'approval-request');
  expect(requestPost.body.reason).toContain('canonical');
  await expect(dialog).toContainText('awaiting_approval');

  const box = await dialog.boundingBox();
  expect(box.width).toBeLessThanOrEqual(390);
});
