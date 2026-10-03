const { test, expect } = require('@playwright/test');
const fs = require('node:fs');
const path = require('node:path');
const index = fs.readFileSync(path.join(__dirname, '../../static/index.html'), 'utf8');
const start = index.indexOf('<dialog id="bot-dialog">');
const dialog = index.slice(start, index.indexOf('</dialog>', start) + '</dialog>'.length);

async function mount(page) {
  await page.goto('http://127.0.0.1:18766/tests/browser/product_workspaces_fixture.html');
  await page.evaluate(async html => {
    document.body.insertAdjacentHTML('beforeend', html);
    const dialog = document.getElementById('bot-dialog');
    dialog.querySelector('#close-bot-dialog').addEventListener('click', () => dialog.close());
    dialog.querySelector('#cancel-bot-integration').addEventListener('click', () => dialog.close());
    const { showBotEditor } = await import('/static/bot_page_editor.js');
    showBotEditor(dialog);
  }, dialog);
}

test('Bot Integration metadata requires deliberate discard while write-only inputs do not enter snapshots', async ({ page }) => {
  await mount(page);
  const editor = page.locator('#bot-dialog');
  await editor.locator('#bot-name').fill('Unsaved bot metadata');
  await editor.locator('#bot-token').fill('synthetic-write-only-token');
  page.once('dialog', dialog => dialog.dismiss());
  await editor.locator('#close-bot-dialog').click();
  await expect(editor).toBeVisible();
  await expect(editor.locator('#bot-name')).toHaveValue('Unsaved bot metadata');
  page.once('dialog', dialog => dialog.accept());
  await editor.locator('[data-bot-discard]').click();
  await expect(editor.locator('#bot-name')).toHaveValue('');
  await expect(editor.locator('#bot-token')).toHaveValue('synthetic-write-only-token');
  await editor.locator('#close-bot-dialog').click();
  await expect(editor).toBeHidden();
  await expect(editor.locator('#bot-token')).toHaveValue('');
});

test('late Bot Integration save retains newer metadata, clears credentials and suppresses duplicate submission', async ({ page }) => {
  await mount(page);
  const editor = page.locator('#bot-dialog');
  await editor.locator('#bot-name').fill('Submitted bot');
  await editor.locator('#bot-conversation-id').fill('channel-a');
  await editor.locator('#bot-token').fill('synthetic-write-only-token');
  await page.evaluate(async () => {
    const { saveBotIntegration } = await import('/static/bot_integration_save.js');
    window.botWrites = [];
    window.saveBot = () => saveBotIntegration(new Event('submit'), {
      target: { scope: 'project', projectId: 'project-a', title: 'Project A' },
      runSettings: () => ({ sandbox: 'workspace-write', approvalPolicy: 'on-request' }),
      refreshConnections: async () => {}, refresh: async () => {},
      api: async (url, options) => {
        window.botWrites.push({ url, body: JSON.parse(options.body) });
        if (url.endsWith('/connections')) return new Promise(resolve => { window.finishBotConnection = resolve; });
        return { thread_id: 'thread-a' };
      },
    });
    window.saveBot(); window.saveBot();
  });
  await expect.poll(() => page.evaluate(() => window.botWrites.length)).toBe(1);
  await expect(editor.locator('#bot-token')).toHaveValue('');
  await editor.locator('#bot-name').fill('Newer bot metadata');
  await page.evaluate(() => window.finishBotConnection({ id: 'connection-a', name: 'Submitted bot' }));
  await expect.poll(() => page.evaluate(() => window.botWrites.length)).toBe(2);
  await expect(editor.locator('#bot-result')).toContainText('newer metadata remains unsaved');
  await expect(editor.locator('#bot-name')).toHaveValue('Newer bot metadata');
  expect(await page.evaluate(() => window.botWrites[1].body.route_prefix)).toBe('Project A');
  expect(await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage }))).not.toContain('synthetic-write-only-token');
});

test('failed Bot Integration save preserves metadata and clears submitted credentials', async ({ page }) => {
  await mount(page);
  const editor = page.locator('#bot-dialog');
  await editor.locator('#bot-name').fill('Retained bot');
  await editor.locator('#bot-conversation-id').fill('channel-a');
  await editor.locator('#bot-token').fill('synthetic-write-only-token');
  await page.evaluate(async () => {
    const { saveBotIntegration } = await import('/static/bot_integration_save.js');
    await saveBotIntegration(new Event('submit'), {
      target: { scope: 'project', projectId: 'project-a', title: 'Project A' },
      runSettings: () => ({ sandbox: 'workspace-write', approvalPolicy: 'on-request' }),
      refreshConnections: async () => {}, refresh: async () => {},
      api: async () => { throw new Error('Canonical bot validation failed'); },
    }).catch(error => { const result = document.getElementById('bot-result'); result.hidden = false; result.textContent = error.message; });
  });
  await expect(editor.locator('#bot-result')).toContainText('Canonical bot validation failed');
  await expect(editor.locator('#bot-name')).toHaveValue('Retained bot');
  await expect(editor.locator('#bot-token')).toHaveValue('');
  await expect(editor.locator('[data-dirty-editor-status]')).toHaveText('Unsaved changes');
});
