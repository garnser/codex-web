const { test, expect } = require('@playwright/test');

function installRoutes(page, actions = []) {
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
    page.route('**/api/agent-sessions/*/*', async (route) => {
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
