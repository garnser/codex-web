const { test, expect } = require('@playwright/test');

async function mount(page, mode = 'held') {
  await page.goto('http://127.0.0.1:18766/tests/browser/product_workspaces_fixture.html');
  await page.evaluate(async mode => {
    document.body.insertAdjacentHTML('beforeend', `
      <section class="gitlab-routing"><input id="gitlab-enabled" type="checkbox"><textarea id="gitlab-project-paths"></textarea><button id="save-gitlab-routing">Save</button><div id="gitlab-routing-result" hidden></div></section>
      <section class="gitlab-routing"><div id="agent-channel-presence"><select multiple><option value="a">A</option><option value="b">B</option></select></div><button id="save-agent-channel-presence">Save</button><div id="agent-channel-presence-result" hidden></div></section>`);
    const { integrationEditors } = await import('/static/integration_editor_state.js');
    const { saveIntegrationDraft } = await import('/static/integration_mutation.js');
    window.integrationEditors = integrationEditors(); window.integrationCalls = 0; window.integrationRefreshes = 0;
    window.saveIntegration = key => saveIntegrationDraft({
      editors: window.integrationEditors, key,
      button: document.getElementById(key === 'gitlab' ? 'save-gitlab-routing' : 'save-agent-channel-presence'),
      result: document.getElementById(key === 'gitlab' ? 'gitlab-routing-result' : 'agent-channel-presence-result'),
      path: `/api/${key}`, payload: { submitted: true },
      api: async () => {
        window.integrationCalls += 1;
        if (mode === 'failure') throw new Error('Canonical integration validation failed');
        return new Promise(resolve => { window.finishIntegration = resolve; });
      },
      apply: response => { window.integrationResponse = response; },
      refresh: async () => { window.integrationRefreshes += 1; },
    });
  }, mode);
}

test('failed integration save retains input and deliberate discard restores the baseline', async ({ page }) => {
  await mount(page, 'failure');
  await page.locator('#gitlab-project-paths').fill('group/project');
  await page.evaluate(() => window.saveIntegration('gitlab'));
  await expect(page.locator('#gitlab-routing-result')).toContainText('Canonical integration validation failed');
  await expect(page.locator('#gitlab-project-paths')).toHaveValue('group/project');
  page.once('dialog', dialog => dialog.accept());
  await page.locator('[data-integration-discard=gitlab]').click();
  await expect(page.locator('#gitlab-project-paths')).toHaveValue('');
});

test('late integration save acknowledges its snapshot, retains newer input and suppresses duplicates', async ({ page }) => {
  await mount(page);
  await page.locator('#gitlab-project-paths').fill('submitted/project');
  await page.evaluate(() => { window.saveIntegration('gitlab'); window.saveIntegration('gitlab'); });
  await expect.poll(() => page.evaluate(() => window.integrationCalls)).toBe(1);
  await page.locator('#gitlab-project-paths').fill('newer/project');
  await page.evaluate(() => window.finishIntegration({ projects: {} }));
  await expect(page.locator('#gitlab-routing-result')).toContainText('newer gitlab edits remain unsaved');
  await expect(page.locator('#gitlab-project-paths')).toHaveValue('newer/project');
  await expect.poll(() => page.evaluate(() => window.integrationRefreshes)).toBe(1);
  page.once('dialog', dialog => dialog.accept());
  await page.locator('[data-integration-discard=gitlab]').click();
  await expect(page.locator('#gitlab-project-paths')).toHaveValue('submitted/project');
});

test('independent GitLab and presence drafts do not overwrite one another', async ({ page }) => {
  await mount(page);
  await page.locator('#gitlab-project-paths').fill('gitlab/draft');
  await page.locator('#agent-channel-presence select').selectOption(['b']);
  await expect(page.locator('.gitlab-routing [data-dirty-editor-status]')).toHaveText(['Unsaved changes', 'Unsaved changes']);
  page.once('dialog', dialog => dialog.accept());
  await page.locator('[data-integration-discard=presence]').click();
  await expect(page.locator('#agent-channel-presence select')).toHaveValues([]);
  await expect(page.locator('#gitlab-project-paths')).toHaveValue('gitlab/draft');
});
