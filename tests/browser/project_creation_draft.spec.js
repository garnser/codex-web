const { test, expect } = require('@playwright/test');
const fs = require('node:fs');
const path = require('node:path');
const index = fs.readFileSync(path.join(__dirname, '../../static/index.html'), 'utf8');
const start = index.indexOf('<dialog id="project-dialog">');
const dialogHtml = index.slice(start, index.indexOf('</dialog>', start) + '</dialog>'.length);

async function mount(page, mode = 'held') {
  await page.goto('http://127.0.0.1:18766/tests/browser/product_workspaces_fixture.html');
  await page.evaluate(async ({ dialogHtml, mode }) => {
    document.body.insertAdjacentHTML('beforeend', dialogHtml);
    const { projectCreationEditor } = await import('/static/project_creation.js');
    window.projectAccepts = []; window.projectCalls = 0; window.projectRefreshes = 0;
    const editor = projectCreationEditor(document.getElementById('project-dialog'));
    window.saveProject = () => editor.save(new Event('submit'), {
      api: async (url, options) => {
        window.projectCalls += 1; window.projectPayload = JSON.parse(options.body);
        if (mode === 'failure') throw new Error('Canonical project validation failed');
        return new Promise(resolve => { window.finishProject = resolve; });
      },
      accept: (project, options) => window.projectAccepts.push({ project, options }),
      refresh: async () => { window.projectRefreshes += 1; },
    });
    editor.open();
  }, { dialogHtml, mode });
}

test('project creation requires deliberate discard before cancellation', async ({ page }) => {
  await mount(page);
  await page.locator('#project-name').fill('Unsaved project');
  page.once('dialog', dialog => dialog.dismiss());
  await page.locator('#cancel-project').click();
  await expect(page.locator('#project-dialog')).toBeVisible();
  await expect(page.locator('#project-name')).toHaveValue('Unsaved project');
  page.once('dialog', dialog => dialog.accept());
  await page.locator('[data-project-discard]').click();
  await expect(page.locator('#project-name')).toHaveValue('');
  await page.locator('#cancel-project').click();
  await expect(page.locator('#project-dialog')).toBeHidden();
});

test('failed project creation retains the submitted draft', async ({ page }) => {
  await mount(page, 'failure');
  await page.locator('#project-name').fill('Retained project');
  await page.locator('#project-path').fill('/workspace/retained');
  await page.evaluate(() => window.saveProject());
  await expect(page.locator('#project-result')).toContainText('Canonical project validation failed');
  await expect(page.locator('#project-name')).toHaveValue('Retained project');
  await expect(page.locator('[data-dirty-editor-status]')).toHaveText('Unsaved changes');
});

test('late project creation retains newer edits and suppresses duplicate submission', async ({ page }) => {
  await mount(page);
  await page.locator('#project-name').fill('Submitted project');
  await page.locator('#project-path').fill('/workspace/submitted');
  await page.evaluate(() => { window.saveProject(); window.saveProject(); });
  await expect.poll(() => page.evaluate(() => window.projectCalls)).toBe(1);
  await page.locator('#project-name').fill('Newer project');
  await page.evaluate(() => window.finishProject({ id: 'submitted', name: 'Submitted project' }));
  await expect(page.locator('#project-result')).toContainText('newer project edits remain unsaved');
  await expect(page.locator('#project-name')).toHaveValue('Newer project');
  await expect(page.locator('#project-dialog')).toBeVisible();
  expect(await page.evaluate(() => window.projectAccepts[0].options.activate)).toBe(false);
  expect(await page.evaluate(() => window.projectPayload.path)).toBe('/workspace/submitted');
  await expect.poll(() => page.evaluate(() => window.projectRefreshes)).toBe(1);
});
