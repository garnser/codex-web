const { test, expect } = require('@playwright/test');

function installRoutes(page, actions = [], state = {}) {
  state.threadCreates ||= [];
  state.configurationDrafts ||= [];
  state.configurationPublishes ||= [];
  state.routingRequests ||= [];
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
    page.route('**/api/provider-capacity', async (route) => route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        items: [
          {
            provider_id: 'openai',
            runtime_id: 'codex',
            status: 'depleted',
            retry_at: 1900000120,
            reason: 'primary usage window exhausted',
          },
        ],
        waits: [
          {
            id: 'capacity-wait-1',
            status: 'waiting',
            retry_at: 1900000120,
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
          agent_session: { assignment_id: 'assignment-a', execution_id: 'execution-a', execution_workspace_id: 'execws-a' },
          assignment: { id: 'assignment-a', status: 'running', fence: 4 },
          worker: { id: 'worker-a', lifecycle: 'active', version: 'worker-v1' },
          execution_workspace: { id: 'execws-a', status: 'active', branch_name: 'codex/work' },
          runtime_events: [
            {
              id: 'usage-a',
              assignment_id: 'assignment-a',
              execution_id: 'execution-a',
              execution_workspace_id: 'execws-a',
              provider_native_turn_id: 'turn-a',
              terminal_outcome: 'succeeded',
              telemetry_completeness: 'partial',
            },
            {
              id: 'usage-old', provider_native_turn_id: 'turn-old',
              assignment_id: 'assignment-old', execution_id: 'execution-old',
              execution_workspace_id: 'execws-old',
              terminal_outcome: 'succeeded', telemetry_completeness: 'complete',
            },
            {
              id: 'usage-unknown', terminal_outcome: 'unknown', telemetry_completeness: 'partial',
            },
            {
              id: 'usage-partial', assignment_id: 'assignment-a', execution_id: 'execution-a',
              terminal_outcome: 'unknown', telemetry_completeness: 'partial',
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
    page.route('**/api/agent-routing/route', async (route) => {
      state.routingRequests.push(route.request().postDataJSON());
      await route.fulfill({
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
          model_route: {
            candidates: [{
              provider_id: 'openai',
              model_id: 'gpt-coding',
              routing_reason: 'turn preference then project configuration',
            }],
          },
          effective_model_preferences: {
            model_class: 'primary-coding',
            workload_class: 'code-review',
            pinned_model_id: 'gpt-coding',
            preferred_provider_ids: ['openai'],
            preferred_latency_classes: ['standard'],
            prefer_lower_cost: true,
            max_cost_usd: 0.04,
            allow_fallback: false,
          },
          configuration_sources: [{
            key: 'model.routing.preferred_provider_ids',
            source: 'record',
            scope_type: 'project',
            scope_id: 'project-a',
            revision: 3,
          }],
        },
        }),
      });
    }),
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
  await expect(card).toContainText('capacity depleted');
  await expect(card).toContainText('1 capacity wait');

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
  await expect(trace.locator('.agent-trace-body')).toContainText('Runtime events: 4');
  const current = trace.locator('[data-runtime-event-id="usage-a"]');
  await expect(current).toContainText('turn-a:succeeded/partial · Current custody');
  await expect(current).toContainText('assignment: assignment-a · execution: execution-a · workspace: execws-a');
  const recorded = trace.locator('[data-runtime-event-id="usage-old"]');
  await expect(recorded).toContainText('Recorded under another assignment');
  await expect(recorded).toContainText('assignment: assignment-old · execution: execution-old · workspace: execws-old');
  await expect(recorded).not.toContainText('Current custody');
  for (const id of ['usage-unknown', 'usage-partial']) {
    const unknown = trace.locator(`[data-runtime-event-id="${id}"]`);
    await expect(unknown).toContainText('Custody attribution unavailable');
    await expect(unknown).toContainText('workspace: unknown');
    await expect(unknown).not.toContainText('workspace: execws-a');
  }
  await expect(trace.locator('.agent-trace-body')).toContainText('ActionIntents: intent-a:completed');
  await expect(trace.locator('.agent-trace-body')).toContainText('Evidence: evidence-a:pass');
  await expect(trace.getByRole('link', { name: 'Assignments' })).toHaveAttribute('href', '#execution-assignment-list');
  await expect(trace.getByRole('link', { name: 'Artifacts & evidence' })).toHaveAttribute('href', '#artifact-list');
});

test('routing explanation is rendered from canonical route response', async ({ page }) => {
  const state = {};
  await installRoutes(page, [], state);
  await page.goto('http://127.0.0.1:18766/tests/browser/agent_provider_admin_fixture.html');

  const card = page.locator('#agent-provider-card');
  await card.locator('.agent-route-project').fill('project-a');
  await card.locator('.agent-route-provider').fill('anthropic');
  await card.locator('.agent-route-workload').fill('code-review');
  await card.locator('.agent-route-model-pin').fill('gpt-coding');
  await card.locator('.agent-route-model-provider').fill('openai');
  await card.locator('.agent-route-run').click();

  const result = card.locator('.agent-route-result');
  await expect(result).toContainText('Selected: anthropic /claude-code');
  await expect(result).toContainText('preferred provider; capabilities satisfied');
  await expect(result).toContainText('Rejected:');
  await expect(result).toContainText('Model: openai/gpt-coding');
  await expect(result).toContainText('Effective model request: class=primary-coding; workload=code-review; pin=gpt-coding');
  await expect(result).toContainText('model.routing.preferred_provider_ids ← record project/project-a r3');
  await expect(result).toContainText('workflow/turn, Agent Profile revision, scoped Configuration, defaults');
  expect(state.routingRequests[0]).toMatchObject({
    project_id: 'project-a',
    preferred_provider_ids: ['anthropic'],
    model_request: {
      model_class: 'primary-coding',
      workload_class: 'code-review',
      pinned_model_id: 'gpt-coding',
      preferred_provider_ids: ['openai'],
      messages: [],
    },
  });
});

for (const width of [390, 1440]) {
  test(`recorded custody remains readable with long identifiers at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1000 });
    await installRoutes(page);
    const assignment = 'assignment-0a617509df654be797dd31eb54597538';
    await page.route('**/api/agent-sessions/*/trace', (route) => route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({ trace: {
        runtime_events: [{
          id: 'usage-long', provider_native_turn_id: '<img src=x onerror=alert(1)>',
          assignment_id: assignment,
          execution_id: 'thread-bootstrap-07f73f92717c49f99abb69faa7772ca1',
          execution_workspace_id: 'execws-8a4f38a3645a38c119aa',
          terminal_outcome: 'succeeded', telemetry_completeness: 'partial',
        }],
      } }),
    }));
    await page.goto('http://127.0.0.1:18766/tests/browser/agent_provider_admin_fixture.html');
    const trace = page.locator('.agent-session-item', { hasText: 'agent-session-claude' }).locator('.agent-session-trace');
    await trace.locator('summary').click();
    const row = trace.locator('[data-runtime-event-id="usage-long"]');
    await expect(row).toContainText('Recorded custody; current custody unavailable');
    await expect(row).toContainText(`assignment: ${assignment}`);
    await expect(row).toContainText('<img src=x onerror=alert(1)>');
    await expect(row.locator('img')).toHaveCount(0);
    expect(await row.evaluate((element) => element.scrollWidth <= element.clientWidth + 1)).toBe(true);
  });
}

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

test('provider credential references deep-link to Project Secrets and consumer links focus the provider', async ({ page }) => {
  await installRoutes(page);
  await page.goto('http://127.0.0.1:18766/tests/browser/agent_provider_admin_fixture.html?project=project-a&consumer_type=agent_provider&consumer_id=openai');
  const provider = page.locator('[data-reference-kind="agent_provider"][data-reference-id="openai"]');
  await expect(provider.getByRole('link', { name: 'secret-codex', exact: true })).toHaveAttribute('href', '/projects/project-a/secrets?secret_id=secret-codex');
  await expect(provider).toBeFocused();
});

test('provider cards and selected or resolved runtimes link to the owning binding editor', async ({ page }) => {
  await installRoutes(page);
  await page.goto('http://127.0.0.1:18766/tests/browser/agent_provider_admin_fixture.html?project=project-a');
  const card = page.locator('#agent-provider-card');
  await expect(card.locator('[data-reference-id="openai"]').getByRole('link', { name: 'Manage provider binding' })).toHaveAttribute('href', '/projects/project-a/operations?provider_id=openai');
  await card.locator('.agent-new-runtime').selectOption('openai/codex');
  await expect(card.locator('.agent-new-runtime').locator('..').locator('[data-runtime-binding-link]')).toHaveAttribute('href', '/projects/project-a/operations?provider_id=openai');
  await card.locator('.agent-preference-project').fill('project-a'); await card.locator('.agent-preference-load').click();
  await expect(card.locator('.agent-preference-runtime').locator('..').locator('[data-runtime-binding-link]')).toHaveAttribute('href', '/projects/project-a/operations?provider_id=anthropic');
});

test('a canonical Session deep link focuses the requested session without mutating it', async ({ page }) => {
  const actions = [];
  await installRoutes(page, actions);
  await page.goto('http://127.0.0.1:18766/tests/browser/agent_provider_admin_fixture.html?project=project-a&consumer_type=session&consumer_id=agent-session-claude');
  await expect(page.locator('[data-reference-kind=session][data-reference-id=agent-session-claude]')).toBeFocused();
  expect(actions).toEqual([]);
});

// Compose the real Operations host and shared styles: a row can fit its own
// oversized grid track while still being clipped by the session list.
for (const width of [390, 1440]) {
  for (const theme of ['light', 'dark']) {
    test(`Operations session trace fits its visible ancestors at ${width}px in ${theme}`, async ({ page }) => {
      await page.setViewportSize({ width, height: 1000 });
      await installRoutes(page);
      const sessionId = 'agent-session-793df6cc28ef4216a09e141d3b18ae6e';
      await page.route('**/api/agent-sessions', route => route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({ items: [{
          id: sessionId, provider_id: 'openai', runtime_id: 'codex', status: 'ready',
          provider_native_session_id: '01a14127-d4f0-70e3-b9b0-ccc5ee653d4f',
          capability_snapshot: ['agent_execution', 'interrupt_cancel', 'native_context_compaction'],
        }] }),
      }));
      const custody = {
        assignment_id: 'assignment-d24aad4e1f2b49708b1706bd17b1ab0c',
        execution_id: 'thread-bootstrap-107637cab32243a7b8499fb19451188f',
        execution_workspace_id: 'execws-a7684b7e1ce349d40938',
      };
      await page.route('**/api/agent-sessions/*/trace', route => route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({ trace: {
          agent_session: custody,
          runtime_events: [
            { id: 'current', ...custody, terminal_outcome: 'succeeded', telemetry_completeness: 'partial' },
            { id: 'historical', assignment_id: 'assignment-0a617509df654be797dd31eb54597538',
              execution_id: 'thread-bootstrap-07f73f92717c49f99abb69faa7772ca1',
              execution_workspace_id: 'execws-8a4f38a3645a38c119aa',
              terminal_outcome: 'succeeded', telemetry_completeness: 'partial' },
          ],
        } }),
      }));
      await page.goto('http://127.0.0.1:18766/tests/browser/agent_provider_operations_fixture.html');
      const card = page.locator('#agent-provider-card');
      await expect(card).toBeAttached();
      // product_workspaces adopts this existing card into Operations.
      await page.evaluate(theme => {
        document.documentElement.dataset.theme = theme;
        const card = document.getElementById('agent-provider-card');
        card.classList.add('product-adopted-card');
        document.querySelector('.product-workspace-host').append(card);
      }, theme);
      await card.locator('.agent-provider-refresh').click();
      const session = card.locator(`[data-reference-id="${sessionId}"]`);
      await session.locator('.agent-session-trace summary').click();
      await expect(session.locator('[data-runtime-event-id="current"]')).toContainText('Current custody');
      await expect(session.locator('[data-runtime-event-id="historical"]')).toContainText('Recorded under another assignment');
      await session.locator('[data-runtime-event-id="historical"]').scrollIntoViewIfNeeded();
      const geometry = await session.evaluate(item => {
        const list = item.closest('.agent-session-list');
        const bounds = list.getBoundingClientRect();
        const elements = [item, ...item.querySelectorAll('.agent-runtime-event, .cw-identity-chip, button')];
        return {
          overflow: list.scrollWidth - list.clientWidth,
          outside: elements.filter(element => {
            const rect = element.getBoundingClientRect();
            return rect.left < Math.max(0, bounds.left) - 1 ||
              rect.right > Math.min(window.innerWidth, bounds.right) + 1;
          }).map(element => element.className),
        };
      });
      expect(geometry).toEqual({ overflow: 0, outside: [] });
    });
  }
}
