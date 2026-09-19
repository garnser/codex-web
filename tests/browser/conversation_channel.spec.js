const { test, expect } = require('@playwright/test');

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

  await page.goto('http://127.0.0.1:18766/tests/browser/conversation_channel_fixture.html');
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
  await page.goto('http://127.0.0.1:18766/tests/browser/conversation_channel_fixture.html');
  await page.locator('#conversation-channel-inspector summary').click();
  const box = await page.locator('#conversation-channel-inspector').boundingBox();
  expect(box.width).toBeLessThanOrEqual(390);
  await expect(page.locator('#refresh-conversation-channels')).toBeVisible();
});
