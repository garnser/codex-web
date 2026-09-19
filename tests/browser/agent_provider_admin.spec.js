const { test, expect } = require('@playwright/test');

function installRoutes(page, actions = [], state = {}) {
  state.threadCreates ||= [];
  state.configurationDrafts ||= [];
  state.configurationPublishes ||= [];
  return Promise.all([
    page.route('**/api/agent-providers/discover', async (route) => route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        items: [
          {
            provider: {
              id: 'openai',
              display_name: 'OpenAI Codex',
              lifecycle: 'active',
              compatibility: 'compatible',
              health: 'healthy',
              declared_capabilities: ['agent_execution', 'model_inference'],
              granted_capabilities: ['agent_execution', 'model_inference'],
              credential_refs: ['secret-codex'],
            },
            effective_capabilities: ['agent_execution', 'model_inference'],
            eligible: true,
            reasons: [],
          },
          {
            provider: {
              id: 'anthropic',
              display_name: 'Anthropic Claude',
              lifecycle: 'active',
              compatibility: 'compatible',
              health: 'healthy',
              declared_capabilities: ['agent_execution'],
              granted_capabilities: ['agent_execution'],
              credential_refs: ['secret-claude'],
            },
            effective_capabilities: ['agent_execution'],
            eligible: true,
            reasons: [],
          },
        ],
      }),
    })),
    page.route('**/api/agent-runtimes', async (route) => route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        items: [
          {
            provider_id: 'openai',
            runtime_id: 'codex',
            runtime_type: 'codex-app-server',
            capabilities: ['agent_execution', 'interrupt_cancel', 'native_context_compaction'],
            capability_revision: 3,
            health: 'healthy',
          },
          {
            provider_id: 'anthropic',
            runtime_id: 'claude-code',
            runtime_type: 'claude-agent-sdk',
            capabilities: ['agent_execution', 'interrupt_cancel'],
            capability_revision: 2,
            health: 'healthy',
          },
        ],
      }),
    })),
    page.route('**/api/agent-sessions', async (route) => {
      if (route.request().method() !== 'GET') return route.fallback();
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          items: [
            {
              id: 'agent-session-codex',
              provider_id: 'openai',
              runtime_id: 'codex',
              model: 'gpt-test',
              status: 'ready',
              provider_native_session_id: 'native-codex',
              assignment_id: 'assignment-c',
              execution_workspace_id: 'workspace-c',
              capability_snapshot: ['agent_execution', 'interrupt_cancel', 'native_context_compaction'],
              runtime_health: 'healthy',
              runtime_registration: { capabilities: ['agent_execution', 'interrupt_cancel', 'native_context_compaction'] },
              latest_usage: { telemetry_completeness: 'partial', input_tokens: 100, output_tokens: 20 },
            },
            {
              id: 'agent-session-claude',
              provider_id: 'anthropic',
              runtime_id: 'claude-code',
              model: 'claude-test',
              status: 'ready',
              provider_native_session_id: 'native-claude',
              assignment_id: 'assignment-a',
              execution_workspace_id: 'workspace-a',
              capability_snapshot: ['agent_execution', 'interrupt_cancel'],
              runtime_health: 'healthy',
              runtime_registration: { capabilities: ['agent_execution', 'interrupt_cancel'] },
              latest_usage: { telemetry_completeness: 'partial', input_tokens: 80, output_tokens: 15 },
            },
          ],
          count: 2,
        }),
      });
    }),
    page.route('**/api/agent-sessions/*/trace', async (route) => route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        trace: {
          assignment: { id: 'assignment-a', status: 'running', fence: 4 },
          worker: { id: 'worker-a', lifecycle: 'active', version: 'worker-v1' },
          execution_workspace: { id: 'execws-a', status: 'active', branch_name: 'codex/work' },
          runtime_events: [
            {
              id: 'usage-a',
              provider_native_turn_id: 'turn-a',
              terminal_outcome: 'succeeded',
              telemetry_completeness: 'partial',
            },
          ],
          action_intents: [{ id: 'intent-a', status: 'completed' }],
          evidence: [{ id: 'evidence-a', result: 'pass' }],
          verifications: [{ id: 'verification-a', result: 'pass' }],
        },
      }),
    })),
    page.route('**/api/agent-sessions/*/*', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      actions.push({
        url: route.request().url(),
        method: route.request().method(),
      });
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({ result: {} }),
      });
    }),
    page.route('**/api/agent-routing/route', async (route) => route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        route: {
          selected_runtime: {
            provider_id: 'anthropic',
            runtime_id: 'claude-code',
            routing_reason: 'preferred provider; capabilities satisfied',
          },
          runtime_candidates: [
            {
              provider_id: 'anthropic',
              runtime_id: 'claude-code',
              routing_reason: 'preferred provider; capabilities satisfied',
            },
          ],
          rejected_reasons: ['openai/codex:provider_preference_penalty'],
        },
      }),
    })),
    page.route('**/api/identity/me', async (route) => route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        identity_id: 'admin',
        organization_id: 'org-a',
        workspace_id: 'workspace-a',
        assurance: 'mfa',
      }),
    })),
    page.route('**/api/threads?*', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      state.threadCreates.push(route.request().url());
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          thread: { id: 'thread-created', cwd: '/workspace' },
          agentSessionId: 'agent-session-new',
        }),
      });
    }),
    page.route('**/api/configuration/resolve', async (route) => {
      const payload = route.request().postDataJSON();
      const values = {
        'agent.routing.preferred_provider_ids': ['anthropic'],
        'agent.routing.preferred_runtime_ids': ['claude-code'],
        'agent.routing.allow_fallback': false,
      };
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          effective: {
            key: payload.key,
            value: values[payload.key],
            source: 'project',
            scope_type: 'project',
            scope_id: payload.context?.project_id || 'project-a',
          },
        }),
      });
    }),
    page.route('**/api/configuration/drafts', async (route) => {
      const payload = route.request().postDataJSON();
      state.configurationDrafts.push(payload);
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          record: {
            id: `draft-${state.configurationDrafts.length}`,
            revision: state.configurationDrafts.length,
          },
        }),
      });
    }),
    page.route('**/api/configuration/*/validate', async (route) => route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({ valid: true }),
    })),
    page.route('**/api/configuration/*/publish', async (route) => {
      state.configurationPublishes.push({
        url: route.request().url(),
        payload: route.request().postDataJSON(),
      });
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({ record: { state: 'published' } }),
      });
    }),
  ]);
}

test('shared AgentSession UI is capability-aware for Claude and Codex', async ({ page }) => {
  const actions = [];
  await installRoutes(page, actions);
  await page.goto('http://127.0.0.1:18766/tests/browser/agent_provider_admin_fixture.html');

  const card = page.locator('#agent-provider-card');
  await expect(card).toBeVisible();
  await expect(card).toContainText('OpenAI Codex');
  await expect(card).toContainText('Anthropic Claude');
  await expect(card).toContainText('agent-session-codex');
  await expect(card).toContainText('agent-session-claude');
  await expect(card).toContainText('partial');

  const claude = card.locator('.agent-session-item', { hasText: 'agent-session-claude' });
  const codex = card.locator('.agent-session-item', { hasText: 'agent-session-codex' });

  await expect(claude.getByRole('button', { name: /Compact unavailable/i })).toBeDisabled();
  await expect(codex.getByRole('button', { name: 'Compact' })).toBeEnabled();

  await claude.getByRole('button', { name: 'Interrupt' }).click();
  await expect.poll(() => actions.length).toBe(1);
  expect(actions[0].url).toContain('/api/agent-sessions/agent-session-claude/interrupt');
});

test('AgentSession execution trace links canonical assignment/runtime/action/evidence state', async ({ page }) => {
  await installRoutes(page);
  await page.goto('http://127.0.0.1:18766/tests/browser/agent_provider_admin_fixture.html');

  const claude = page.locator('.agent-session-item', { hasText: 'agent-session-claude' });
  const trace = claude.locator('.agent-session-trace');
  await trace.locator('summary').click();

  await expect(trace.locator('.agent-trace-body')).toContainText('Assignment: assignment-a · running · fence 4');
  await expect(trace.locator('.agent-trace-body')).toContainText('Runtime events: turn-a:succeeded/partial');
  await expect(trace.locator('.agent-trace-body')).toContainText('ActionIntents: intent-a:completed');
  await expect(trace.locator('.agent-trace-body')).toContainText('Evidence: evidence-a:pass');
  await expect(trace.getByRole('link', { name: 'Assignments' })).toHaveAttribute('href', '#execution-assignment-list');
  await expect(trace.getByRole('link', { name: 'Artifacts & evidence' })).toHaveAttribute('href', '#artifact-list');
});

test('routing explanation is rendered from canonical route response', async ({ page }) => {
  await installRoutes(page);
  await page.goto('http://127.0.0.1:18766/tests/browser/agent_provider_admin_fixture.html');

  const card = page.locator('#agent-provider-card');
  await card.locator('.agent-route-project').fill('project-a');
  await card.locator('.agent-route-provider').fill('anthropic');
  await card.locator('.agent-route-run').click();

  const result = card.locator('.agent-route-result');
  await expect(result).toContainText('Selected: anthropic /claude-code');
  await expect(result).toContainText('preferred provider; capabilities satisfied');
  await expect(result).toContainText('Rejected:');
});

test('explicit runtime thread creation forces the selected runtime through the thread API', async ({ page }) => {
  const state = {};
  await installRoutes(page, [], state);
  await page.goto('http://127.0.0.1:18766/tests/browser/agent_provider_admin_fixture.html');

  const card = page.locator('#agent-provider-card');
  await card.locator('.agent-new-project').fill('project-a');
  await card.locator('.agent-new-runtime').selectOption('anthropic/claude-code');
  await card.locator('.agent-new-thread').click();

  await expect.poll(() => state.threadCreates.length).toBe(1);
  const url = new URL(state.threadCreates[0]);
  expect(url.searchParams.get('project_id')).toBe('project-a');
  expect(url.searchParams.get('provider_id')).toBe('anthropic');
  expect(url.searchParams.get('runtime_id')).toBe('claude-code');
  await expect(card.locator('.agent-new-thread-status')).toContainText('AgentSession agent-session-new');
});

test('project runtime preference publishes existing typed routing configuration keys', async ({ page }) => {
  const state = {};
  await installRoutes(page, [], state);
  await page.goto('http://127.0.0.1:18766/tests/browser/agent_provider_admin_fixture.html');

  const card = page.locator('#agent-provider-card');
  await card.locator('.agent-preference-project').fill('project-a');
  await card.locator('.agent-preference-load').click();
  await expect(card.locator('.agent-preference-runtime')).toHaveValue('anthropic/claude-code');
  await expect(card.locator('.agent-preference-fallback')).not.toBeChecked();

  await card.locator('.agent-preference-runtime').selectOption('openai/codex');
  await card.locator('.agent-preference-fallback').check();
  await card.locator('.agent-preference-save').click();

  await expect.poll(() => state.configurationDrafts.length).toBe(3);
  expect(state.configurationDrafts.map((item) => item.key)).toEqual([
    'agent.routing.preferred_provider_ids',
    'agent.routing.preferred_runtime_ids',
    'agent.routing.allow_fallback',
  ]);
  expect(state.configurationDrafts[0]).toMatchObject({
    scope_type: 'project',
    scope_id: 'project-a',
    value: ['openai'],
  });
  expect(state.configurationDrafts[1].value).toEqual(['codex']);
  expect(state.configurationDrafts[2].value).toBe(true);
  await expect.poll(() => state.configurationPublishes.length).toBe(3);
  await expect(card.locator('.agent-preference-status')).toContainText('Effective source');
});

test('Agent Providers surface remains usable on narrow screens', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await installRoutes(page);
  await page.goto('http://127.0.0.1:18766/tests/browser/agent_provider_admin_fixture.html');

  const card = page.locator('#agent-provider-card');
  await expect(card).toBeVisible();
  const layout = card.locator('.agent-provider-layout');
  await expect(layout).toHaveCSS('grid-template-columns', /^\d+(?:\.\d+)?px$/);
  await expect(card.locator('.agent-route-run')).toBeVisible();
});
