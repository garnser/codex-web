const { test, expect } = require('@playwright/test');
const fs = require('node:fs');
const path = require('node:path');
const index = fs.readFileSync(path.join(__dirname, '../../static/index.html'), 'utf8');
const start = index.indexOf('<details id="execution-worker-enrollment-panel">');
const fragment = index.slice(start, index.indexOf('</details>', start) + '</details>'.length);

async function mount(page, mode = 'held') {
  await page.goto('http://127.0.0.1:18766/tests/browser/product_workspaces_fixture.html');
  await page.evaluate(async ({ fragment, mode }) => {
    document.body.insertAdjacentHTML('beforeend', fragment);
    document.getElementById('execution-worker-enrollment-panel').open = true;
    const { installExecutionWorkerEnrollment } = await import('/static/execution_worker_enrollment.js');
    window.enrollmentPosts = 0;
    window.enrollmentUi = installExecutionWorkerEnrollment({
      canManage: () => true, setStatus: value => { window.enrollmentStatus = value; },
      escapeHtml: value => String(value),
      apiRequest: async (path, options) => {
        if (options?.method !== 'POST') {
          if (window.failEnrollmentHistory) throw new Error('History unavailable');
          return { items: [] };
        }
        window.enrollmentPosts += 1;
        if (mode === 'failure') throw new Error('Enrollment denied');
        return new Promise(resolve => { window.finishEnrollment = resolve; });
      },
    });
  }, { fragment, mode });
}

test('runtime enrollment failure and history refresh preserve metadata edits', async ({ page }) => {
  await mount(page, 'failure');
  await page.locator('#execution-worker-enrollment-identity').fill('worker-service');
  await page.locator('#execution-worker-enrollment-pool').fill('draft-pool');
  await page.locator('#create-execution-worker-enrollment').click();
  await expect(page.locator('#execution-worker-enrollment-result')).toContainText('Enrollment denied');
  await page.evaluate(() => window.enrollmentUi.refresh());
  await expect(page.locator('#execution-worker-enrollment-pool')).toHaveValue('draft-pool');
  await expect(page.locator('#execution-worker-enrollment-panel [data-dirty-editor-status]')).toHaveText('Unsaved changes');
  page.once('dialog', dialog => dialog.accept());
  await page.locator('[data-enrollment-discard]').click();
  await expect(page.locator('#execution-worker-enrollment-pool')).toHaveValue('local');
});

test('late enrollment creation preserves newer metadata and one-time output when history fails', async ({ page }) => {
  await mount(page);
  await page.locator('#execution-worker-enrollment-identity').fill('worker-service');
  await page.locator('#create-execution-worker-enrollment').click();
  await expect.poll(() => page.evaluate(() => window.enrollmentPosts)).toBe(1);
  await page.locator('#execution-worker-enrollment-pool').fill('newer-pool');
  await page.evaluate(() => {
    window.failEnrollmentHistory = true;
    window.finishEnrollment({ token: 'synthetic-enrollment-value', item: { expires_at: 2000000000 } });
  });
  await expect(page.locator('[data-runtime-enrollment-command]')).toBeVisible();
  await expect(page.locator('#execution-worker-enrollment-pool')).toHaveValue('newer-pool');
  await expect(page.locator('#execution-worker-enrollment-panel [data-dirty-editor-status]')).toHaveText('Unsaved changes');
  await expect.poll(() => page.evaluate(() => window.enrollmentStatus)).toContain('History unavailable');
  await page.evaluate(() => window.dispatchEvent(new CustomEvent('codex:project-changed', { detail: { projectId: 'another-project' } })));
  await expect(page.locator('[data-runtime-enrollment-command]')).toHaveCount(0);
});

test('late enrollment response never displays a one-time token after Project navigation', async ({ page }) => {
  await mount(page);
  await page.locator('#execution-worker-enrollment-identity').fill('worker-service');
  await page.locator('#create-execution-worker-enrollment').click();
  await expect.poll(() => page.evaluate(() => window.enrollmentPosts)).toBe(1);
  await page.evaluate(() => {
    window.dispatchEvent(new CustomEvent('codex:project-changed', { detail: { projectId: 'another-project' } }));
    window.finishEnrollment({ token: 'synthetic-enrollment-value', item: { expires_at: 2000000000 } });
  });
  await expect(page.locator('#create-execution-worker-enrollment')).toBeEnabled();
  await expect(page.locator('[data-runtime-enrollment-command]')).toHaveCount(0);
});
