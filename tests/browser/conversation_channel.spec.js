const { test, expect } = require('@playwright/test');
const fixtureUrl = `${process.env.CODEX_WEB_BROWSER_BASE_URL || 'http://127.0.0.1:18766'}/tests/browser/conversation_channel_fixture.html`;

test('conversation channel inspector shows capabilities and canonical routing provenance', async ({ page }) => {
  await page.route('**/api/conversation-channels/providers', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        count: 3,
        items: [
          {
            provider_type: 'slack',
            contract_version: '1.0',
            capabilities: ['attachments', 'edits', 'reactions', 'threads'],
            available: true,
            error: null,
          },
          {
            provider_type: 'telegram',
            contract_version: '1.0',
            capabilities: ['attachments', 'edits', 'threads'],
            available: true,
            error: null,
          },
          {
            provider_type: 'teams',
            contract_version: '1.0',
            capabilities: ['attachments', 'deletes', 'edits', 'threads'],
            available: true,
            error: null,
          },
        ],
      }),
    });
  });
  await page.route('**/api/conversation-channels/messages?*', async (route) => {
    const provider = new URL(route.request().url()).searchParams.get('provider_type');
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        count: 1,
        items: [{
          organization_id: 'org-a',
          workspace_id: 'ws-a',
          message: {
            conversation: {
              provider_type: provider,
              provider_instance: provider + '-connection-1',
              conversation_id: 'conversation-1',
              thread_id: 'external-thread-1',
            },
            message_id: 'message-1',
          },
          event_kind: 'message_created',
          occurred_at: 100,
          deleted: false,
          last_canonical_event_id: 'canonical-event-1',
          updated_at: 100,
        }],
      }),
    });
  });
  await page.route('**/api/conversation-channels/receipts?*', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        count: 1,
        items: [{
          canonical_event_id: 'canonical-event-1',
          organization_id: 'org-a',
          workspace_id: 'ws-a',
          message_key: 'slack::slack-connection-1::conversation-1::external-thread-1::message-1',
          outcome: 'routed',
          thread_id: 'canonical-thread-7',
          queued_id: null,
          reason: null,
          routing_result: { ok: true, threadId: 'canonical-thread-7' },
          created_at: 100,
          updated_at: 100,
        }],
      }),
    });
  });

  await page.goto(fixtureUrl);
  const inspector = page.locator('#conversation-channel-inspector');
  await expect(inspector).toBeVisible();
  await inspector.locator('summary').click();

  await expect(page.locator('#conversation-channel-provider-contracts')).toContainText('slack');
  await expect(page.locator('#conversation-channel-provider-contracts')).toContainText('teams');
  await expect(page.locator('#conversation-channel-provider-contracts')).toContainText('reactions');
  await expect(page.locator('#conversation-channel-message-state')).toContainText('canonical-event-1');
  await expect(page.locator('#conversation-channel-receipts')).toContainText('canonical-thread-7');
  await expect(page.locator('body')).not.toContainText('xoxb-');
  await expect(page.locator('body')).not.toContainText('signing_secret');

  await page.locator('#bot-provider').selectOption('telegram');
  await expect(page.locator('#conversation-channel-message-state')).toContainText('telegram');
  await expect(page.locator('#conversation-channel-receipts')).toContainText('No recent routing receipts');
});

test('conversation channel inspector remains usable at phone width', async ({ page }) => {
  await page.route('**/api/conversation-channels/providers', route => route.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({ count: 1, items: [{ provider_type: 'slack', contract_version: '1.0', capabilities: ['threads'], available: true, error: null }] }),
  }));
  await page.route('**/api/conversation-channels/messages?*', route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({ count: 0, items: [] }),
  }));
  await page.route('**/api/conversation-channels/receipts?*', route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({ count: 0, items: [] }),
  }));
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(fixtureUrl);
  await page.locator('#conversation-channel-inspector summary').click();
  const box = await page.locator('#conversation-channel-inspector').boundingBox();
  expect(box.width).toBeLessThanOrEqual(390);
  await expect(page.locator('#refresh-conversation-channels')).toBeVisible();
});

test('canonical retention and reconciliation remain distinct from transport and execution completion', async ({ page }) => {
  await page.route('**/api/conversation-channels/providers', route => route.fulfill({ json: { items: [] } }));
  await page.route('**/api/conversation-channels/messages?*', route => route.fulfill({ json: { items: [] } }));
  const states = ['retained_pending', 'queue_admitted', 'projected', 'failed', 'reconciliation', 'action_admitted'];
  await page.route('**/api/conversation-channels/receipts?*', route => route.fulfill({ json: {
    canonical_state_counts: { retained_pending: 4, queue_admitted: 2, action_admitted: 1, projected: 7, failed: 1, reconciliation: 3 },
    items: states.map((state, i) => ({ canonical_event_id: `evt-${i}`, message_key: `slack::fixture::channel::thread::${i}`,
      delivery_state: state, outcome: state, connection_id: 'fixture-connection', project_id: 'fixture-project',
      input_retained: true, input_protected: true, governance_record_id: `governance-${i}`,
      authority_digest: 'exact-authority', ingest_actor_id: 'runtime-identity', queued_id: i === 2 ? 'queued-proof' : null,
      routing_result: i === 5 ? {actionIntentId:'details-action-proof', actionIntentStatus:'reconciliation_required'} : {},
      reason: i === 4 ? 'Native outcome unknown; canonical reconciliation required' : null }))
  } }));
  await page.goto(fixtureUrl);
  await page.locator('#conversation-channel-inspector summary').click();
  const counts = page.locator('#conversation-channel-canonical-status');
  await expect(counts).toContainText('retained/pending 4');
  await expect(counts).toContainText('queue admitted 2');
  await expect(counts).toContainText('reconciliation 3');
  await expect(counts).toContainText('projection completion does not prove native execution completed');
  const receipts = page.locator('#conversation-channel-receipts');
  for (const state of states) await expect(receipts).toContainText(state);
  await expect(receipts).toContainText('Protected original');
  await expect(receipts).toContainText('fixture-project');
  await expect(receipts).toContainText('queued-proof');
  await expect(receipts).toContainText('Details ActionIntent');
  await expect(receipts.locator('a[href="/api/action-intents/details-action-proof"]')).toHaveText('details-action-proof');
  await page.locator('#refresh-conversation-channels').focus();
  await page.keyboard.press('Enter');
  await expect(page.locator('#conversation-channel-status')).toHaveText('Canonical state loaded');
  await expect(page.locator('body')).not.toContainText('ciphertext');
});
