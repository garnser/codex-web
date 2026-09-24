const { test, expect } = require('@playwright/test');

const profiles = [
  {
    profile_id: 'maya',
    revision: 4,
    name: 'Maya',
    description: 'Implementation Agent',
    lifecycle: 'active',
    owner_identity_id: 'human-1',
    role_id: 'implementer',
    skill_refs: [{ definition_id: 'python-review', record_id: 'skill-rev-2', revision: 2 }],
    access: { mode: 'tenant' },
    runtime_policy: { preferred_provider_ids: ['openai'], preferred_runtime_ids: ['codex'] },
    model_policy: { model_class: 'reasoning' },
    execution_profile_id: 'repo-write',
    sandbox_requirement: 'workspace-write',
    budgets: { max_concurrency: 2 },
  },
  {
    profile_id: 'nora',
    revision: 2,
    name: 'Nora',
    description: 'Validation Agent',
    lifecycle: 'disabled',
    owner_identity_id: 'human-1',
    role_id: 'validator',
    skill_refs: [],
    runtime_policy: {},
    model_policy: {},
    budgets: { max_concurrency: 1 },
  },
];

const teams = [
  {
    team_id: 'delivery',
    revision: 3,
    name: 'Delivery Team',
    description: 'Implementation and validation collaboration',
    lifecycle: 'active',
    owner_identity_id: 'human-1',
    leader_profile_id: 'maya',
    members: [{ profile_id: 'nora', role: 'validator', capability_tags: ['validation'], required: true }],
    budgets: { max_handoffs: 4, max_participants: 2, max_coordinator_rounds: 2, max_parallel_executions: 2 },
    allowed_identity_ids: [],
    allowed_role_ids: ['operator'],
    escalation_target: 'attention:delivery',
  },
  {
    team_id: 'legacy-team',
    revision: 1,
    name: 'Legacy Team',
    description: '',
    lifecycle: 'archived',
    owner_identity_id: 'human-1',
    leader_profile_id: 'nora',
    members: [],
    budgets: { max_handoffs: 2, max_participants: 1, max_coordinator_rounds: 1, max_parallel_executions: 1 },
    allowed_identity_ids: [],
    allowed_role_ids: [],
  },
];

const skills = [
  {
    skillId: 'python-review',
    name: 'Python Review',
    revision: 2,
    lifecycle: 'active',
    provenance: { source_type: 'verified-execution', source_ref: 'execution-9' },
  },
];

async function mockApis(page, { empty = false } = {}) {
  await page.route('**/api/agent-profiles?**', (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({ items: empty ? [] : profiles }),
  }));
  await page.route('**/api/agent-teams?**', (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({ items: empty ? [] : teams }),
  }));
  await page.route('**/api/skills?**', (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({ items: empty ? [] : skills }),
  }));
  await page.route('**/api/agent-profiles/maya/executions?**', (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({
      available: true,
      count: 2,
      activeCount: 1,
      items: [
        {
          assignmentId: 'assignment-1',
          executionId: 'execution-1',
          projectId: 'project-a',
          status: 'running',
          profileRevision: 4,
          providerId: 'openai',
          runtimeId: 'codex',
          workerId: 'worker-2',
          updatedAt: 1790000000,
        },
      ],
    }),
  }));
  await page.route('**/api/agent-profiles/maya/access**', (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({ decision: { allowed: true, reasons: ['workspace membership'] } }),
  }));
}

test('Agent, Team and Skill surfaces use stable canonical identities and lifecycle state', async ({ page }) => {
  await mockApis(page);
  await page.goto('http://127.0.0.1:18766/tests/browser/collaboration_workspace_fixture.html');

  const card = page.locator('#collaboration-workspace-card');
  await expect(card).toBeVisible();
  await expect(card.locator('[data-collab-status]')).toHaveText('2 Agents · 2 Teams · 1 Skills');

  const maya = card.locator('.collab-agent-card').filter({ hasText: 'Maya' });
  await expect(maya).toContainText('Implementation Agent');
  await expect(maya).toContainText('implementer');
  await expect(maya).toContainText('python-review');
  await expect(maya.locator('[data-identity-id="maya"]')).toBeVisible();
  await expect(maya).not.toContainText('openai');

  const nora = card.locator('.collab-agent-card').filter({ hasText: 'Nora' });
  await expect(nora.locator('[data-status="disabled"]')).toBeVisible();

  const team = card.locator('.collab-team-card').filter({ hasText: 'Delivery Team' });
  await expect(team.locator('[data-identity-id="maya"]')).toBeVisible();
  await expect(team.locator('[data-identity-id="nora"]')).toBeVisible();
  await expect(team).toContainText('validator');
  await expect(team).toContainText('validation');

  const archived = card.locator('.collab-team-card').filter({ hasText: 'Legacy Team' });
  await expect(archived.locator('[data-status="archived"]')).toBeVisible();

  const skill = card.locator('.collab-skill-usage').filter({ hasText: 'Python Review' });
  await expect(skill).toContainText('used by 1 Agent');
  await expect(skill).toContainText('verified-execution');
  await expect(skill.locator('[data-identity-id="maya"]')).toBeVisible();
});

test('Agent execution/provider details load lazily and remain secondary to stable identity', async ({ page }) => {
  await mockApis(page);
  let executionRequests = 0;
  page.on('request', (request) => {
    if (request.url().includes('/api/agent-profiles/maya/executions')) executionRequests += 1;
  });
  await page.goto('http://127.0.0.1:18766/tests/browser/collaboration_workspace_fixture.html');

  const maya = page.locator('.collab-agent-card').filter({ hasText: 'Maya' });
  expect(executionRequests).toBe(0);
  await maya.locator('.collab-agent-context > summary').click();
  await expect.poll(() => executionRequests).toBe(1);
  await expect(maya).toContainText('Execution execution-1');
  await expect(maya).toContainText('worker-2');
  await expect(maya).toContainText('Allowed');

  const secondary = maya.locator('.collab-secondary');
  await expect(secondary).not.toHaveAttribute('open', '');
  await secondary.locator('summary').click();
  await expect(secondary).toContainText('openai');
  await expect(secondary).toContainText('codex');
  await expect(secondary).toContainText('repo-write');
});

test('empty collaboration workspace has explicit Agent, Team and Skill states', async ({ page }) => {
  await mockApis(page, { empty: true });
  await page.goto('http://127.0.0.1:18766/tests/browser/collaboration_workspace_fixture.html');

  const card = page.locator('#collaboration-workspace-card');
  await expect(card).toContainText('No Agent Profiles');
  await expect(card).toContainText('No Teams');
  await expect(card).toContainText('No Skills');
  await expect(card.locator('.cw-state-empty')).toHaveCount(3);
});

test('collaboration cards collapse intentionally at phone width', async ({ page }) => {
  await mockApis(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('http://127.0.0.1:18766/tests/browser/collaboration_workspace_fixture.html');

  const columns = await page.locator('[data-collab-agents]').evaluate((node) => getComputedStyle(node).gridTemplateColumns);
  expect(columns.trim().split(/\s+/)).toHaveLength(1);
  const metrics = await page.evaluate(() => ({
    width: window.innerWidth,
    scrollWidth: document.documentElement.scrollWidth,
  }));
  expect(metrics.scrollWidth).toBeLessThanOrEqual(metrics.width + 1);
});
