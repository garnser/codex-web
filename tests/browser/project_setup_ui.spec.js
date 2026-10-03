const { test, expect } = require('@playwright/test');

for (const delayed of ['readiness', 'plan', 'apply']) {
  test(`late Project ${delayed} cannot replace the new Project setup or execution gate`, async ({ page }) => {
    await page.goto('http://127.0.0.1:18766/tests/browser/project_setup_ui_fixture.html');
    await page.locator('#project-setup-launch').click();
    await page.evaluate(async () => {
      window.setupRace = { calls: [], pending: false, settled: false, delay: '' };
      const held = new Promise(resolve => { window.setupRace.release = resolve; });
      const projects = ['project-a', 'project-b', 'empty'].map(id => ({ id, name: id, organization_id: 'local', workspace_id: 'default' }));
      window.fetch = async (input, options = {}) => {
        const url = new URL(input, location.origin);
        const projectId = url.pathname.match(/\/projects\/([^/]+)/)?.[1];
        const kind = url.pathname.split('/').at(-1);
        window.setupRace.calls.push({ path: url.pathname, method: options.method || 'GET' });
        if (projectId === 'project-a' && kind === window.setupRace.delay) {
          window.setupRace.pending = true;
          await held;
          window.setupRace.settled = true;
        }
        let payload;
        if (url.pathname === '/api/projects') payload = projects;
        else if (kind === 'readiness') payload = { project_id: projectId, semantic_ready: true,
          execution_ready: projectId !== 'project-b', status: projectId === 'project-b' ? 'blocked' : 'ready', checks: [] };
        else if (kind === 'ui-state') payload = { project: projects.find(row => row.id === projectId),
          resources: { items: projectId === 'empty' ? [] : [{ id: `repo-${projectId}`, path: `/workspace/${projectId}` }] } };
        else if (kind === 'plan') payload = { plan: { id: `plan-${projectId}`, operations: [] }, blocked: false };
        else payload = { items: [] };
        return new Response(JSON.stringify(payload), { status: 200, headers: { 'Content-Type': 'application/json' } });
      };
      await window.CodexProjectSetup.refresh();
    });
    const dialog = page.locator('#project-setup-dialog');
    await expect(page.locator('#send')).toBeEnabled();
    await dialog.locator('[data-setup-tab="plan"]').click();
    if (delayed === 'apply') {
      await dialog.locator('[data-setup-plan]').click();
      await expect(dialog.locator('.project-setup-plan-summary')).toContainText('plan-project-a');
    }
    await page.evaluate(kind => { window.setupRace.delay = kind; }, delayed);
    if (delayed === 'readiness') await page.evaluate(() => { void window.CodexProjectSetup.refresh(); });
    else await dialog.locator(`[data-setup-${delayed}]`).click();
    await expect.poll(() => page.evaluate(() => window.setupRace.pending)).toBe(true);
    await page.evaluate(() => {
      document.body.dataset.projectId = 'project-b';
      window.dispatchEvent(new CustomEvent('codex:project-changed', { detail: { projectId: 'project-b' } }));
    });
    await expect(dialog.locator('.project-setup-hero h3')).toHaveText('project-b');
    await expect(page.locator('#send')).toBeDisabled();
    await page.evaluate(() => window.setupRace.release());
    await expect.poll(() => page.evaluate(() => window.setupRace.settled)).toBe(true);
    await expect(dialog.locator('.project-setup-hero h3')).toHaveText('project-b');
    await expect(dialog).not.toContainText('plan-project-a');
    await expect(dialog.locator('[data-setup-action-feedback]')).toHaveCount(0);
    await expect(page.locator('#send')).toBeDisabled();
    await page.evaluate(async () => {
      document.body.dataset.projectId = 'empty';
      await window.CodexProjectSetup.refresh();
    });
    expect(await page.evaluate(() => window.CodexProjectSetup.getState().resources)).toEqual([]);
    const count = await page.evaluate(() => window.setupRace.calls.length);
    await page.evaluate(async () => {
      document.body.dataset.projectId = '';
      await window.CodexProjectSetup.refresh();
    });
    await expect(dialog).toContainText('Select a Project');
    await expect(page.locator('#send')).toBeDisabled();
    expect(await page.evaluate(() => window.setupRace.calls.length)).toBe(count);
    await page.evaluate(async () => {
      document.body.dataset.projectId = 'project-a';
      await window.CodexProjectSetup.refresh();
    });
    await expect(dialog.locator('.project-setup-hero h3')).toHaveText('project-a');
    await expect(page.locator('#send')).toBeEnabled();
  });
}

test('clean first-run surface exposes setup immediately without an indefinite loading state', async ({ page }) => {
  await page.goto('http://127.0.0.1:18766/tests/browser/project_setup_ui_fixture.html');

  const launch = page.locator('#project-setup-launch');
  await expect(launch).toBeVisible();
  await expect(launch).toHaveText('Project setup');

  await launch.click();
  const dialog = page.locator('#project-setup-dialog');
  await expect(dialog).toBeVisible();
  await expect(dialog.locator('.project-setup-summary')).toBeVisible();
  await expect(dialog.locator('[role="progressbar"]')).toHaveCount(0);
  await expect(page.locator('#new-thread')).toBeDisabled();
  await expect(page.locator('#send')).toBeDisabled();
});

test('blocked Project exposes setup, exact readiness blocker, and gates execution controls', async ({ page }) => {
  await page.goto('http://127.0.0.1:18766/tests/browser/project_setup_ui_fixture.html');

  await expect(page.locator('#project-setup-launch')).toHaveText('Project setup');
  await expect(page.locator('#new-thread')).toBeDisabled();
  await expect(page.locator('#send')).toBeDisabled();
  await expect(page.locator('#prompt')).toBeDisabled();

  await page.locator('#project-setup-launch').click();
  const dialog = page.locator('#project-setup-dialog');
  await expect(dialog).toBeVisible();
  await expect(dialog.locator('[data-check-id="execution:worker"]')).toContainText('worker_capability_missing');
  await expect(dialog.locator('[data-check-id="execution:worker"]')).toContainText('Start a qualified worker');

  await dialog.locator('[data-check-id="execution:worker"] [data-setup-route]').click();
  await expect.poll(async () => page.evaluate(() => window.fixture.openedWorkspace)).toBe('workers');
});

test('fresh setup retry moves blocked Project to ready without a hand-authored manifest', async ({ page }) => {
  await page.goto('http://127.0.0.1:18766/tests/browser/project_setup_ui_fixture.html');
  await page.locator('#project-setup-launch').click();
  const dialog = page.locator('#project-setup-dialog');

  await dialog.locator('[data-setup-fresh]').click();
  await expect(page.locator('#project-setup-launch')).toHaveText('Project ready');
  await expect(page.locator('#new-thread')).toBeEnabled();
  await expect(page.locator('#send')).toBeEnabled();

  const calls = await page.evaluate(() => window.fixture.calls);
  expect(calls.some((item) => item.path.endsWith('/fresh-bootstrap') && item.method === 'POST')).toBeTruthy();
});

test('plan/apply uses canonical bootstrap API and normalized resource topology', async ({ page }) => {
  await page.goto('http://127.0.0.1:18766/tests/browser/project_setup_ui_fixture.html');
  await page.locator('#project-setup-launch').click();
  const dialog = page.locator('#project-setup-dialog');

  await dialog.locator('[data-setup-tab="plan"]').click();
  await dialog.locator('[data-setup-plan]').click();
  await expect(dialog.locator('.project-setup-plan-summary')).toContainText('bootstrap-plan-1');
  expect(await dialog.locator('[data-project-setup-manifest]').inputValue()).toContain('/workspace/project-a');

  await dialog.locator('[data-setup-apply]').click();
  await expect(page.locator('#project-setup-launch')).toHaveText('Project ready');

  const calls = await page.evaluate(() => window.fixture.calls);
  const plan = calls.find((item) => item.path.endsWith('/bootstrap/plan'));
  const apply = calls.find((item) => item.path.endsWith('/bootstrap/apply'));
  expect(JSON.parse(plan.body).manifest.repositories[0].path).toBe('/workspace/project-a');
  expect(JSON.parse(apply.body).expected_plan_id).toBe('bootstrap-plan-1');
});

test('apply preserves the reviewed manifest and explicit approval across acknowledgement renders', async ({ page }) => {
  await page.goto('http://127.0.0.1:18766/tests/browser/project_setup_ui_fixture.html');
  await page.locator('#project-setup-launch').click();
  const dialog = page.locator('#project-setup-dialog');
  await dialog.locator('[data-setup-tab="plan"]').click();
  await dialog.locator('.project-setup-manifest-panel summary').click();
  const manifest = JSON.parse(await dialog.locator('[data-project-setup-manifest]').inputValue());
  manifest.project.name = 'Reviewed Project A';
  await dialog.locator('[data-project-setup-manifest]').fill(JSON.stringify(manifest));
  await dialog.locator('[data-setup-plan]').click();
  await expect(dialog.locator('.project-setup-plan-summary')).toContainText('bootstrap-plan-1');
  expect(JSON.parse(await dialog.locator('[data-project-setup-manifest]').inputValue())).toEqual(manifest);
  await dialog.locator('[data-setup-approve]').check();
  await dialog.locator('[data-setup-apply]').click();
  await expect(page.locator('#project-setup-launch')).toHaveText('Project ready');
  const apply = await page.evaluate(() => window.fixture.calls.find(call => call.path.endsWith('/bootstrap/apply')));
  expect(JSON.parse(apply.body)).toMatchObject({ manifest, expected_plan_id: 'bootstrap-plan-1', approve_authority_changes: true });
});

test('plan apply acknowledges once, prevents duplicate submission, and confirms canonical readiness', async ({ page }) => {
  await page.goto('http://127.0.0.1:18766/tests/browser/project_setup_ui_fixture.html?explicit=1');
  await page.locator('#project-setup-launch').click();
  const dialog = page.locator('#project-setup-dialog');
  await dialog.locator('[data-setup-tab="plan"]').click();
  await dialog.locator('[data-setup-plan]').click();
  await dialog.locator('[data-setup-apply]').click();
  await expect(dialog.locator('[data-setup-action-feedback] [data-action-state="succeeded"]')).toContainText('Canonical readiness now reports execution ready');
  expect(await page.evaluate(() => window.fixture.applyCalls)).toBe(1);
});

test('plan apply exposes recovery context when canonical apply fails', async ({ page }) => {
  await page.goto('http://127.0.0.1:18766/tests/browser/project_setup_ui_fixture.html?failApply=1');
  await page.locator('#project-setup-launch').click();
  const dialog = page.locator('#project-setup-dialog');
  await dialog.locator('[data-setup-tab="plan"]').click();
  await dialog.locator('[data-setup-plan]').click();
  await dialog.locator('[data-setup-apply]').click();
  await expect(dialog.locator('[data-setup-action-feedback] [data-action-state="failed"]')).toContainText('worker stopped during apply');
  await expect(dialog.locator('[data-setup-tab="plan"]')).toBeVisible();
});


test('setup UI never renders raw credential-shaped fixture data', async ({ page }) => {
  await page.goto('http://127.0.0.1:18766/tests/browser/project_setup_ui_fixture.html');
  await page.locator('#project-setup-launch').click();

  const text = await page.locator('#project-setup-dialog').innerText();
  expect(text).not.toContain('ULTRA_PRIVATE_VALUE');
  expect(text).not.toContain('xoxb-');
});


test('explicit repository policy is ready while requiring a target per turn', async ({ page }) => {
  await page.goto('http://127.0.0.1:18766/tests/browser/project_setup_ui_fixture.html?explicit=1');

  await expect(page.locator('#project-setup-launch')).toHaveText('Project ready');
  await expect(page.locator('#new-thread')).toBeEnabled();
  await expect(page.locator('#send')).toBeEnabled();

  await page.locator('#project-setup-launch').click();
  const dialog = page.locator('#project-setup-dialog');
  await expect(dialog.locator('.project-setup-hero')).toContainText('Repository target required per turn');
  await expect(dialog.locator('.project-setup-metrics')).toContainText('Explicit per turn');
  await expect(dialog.locator('[data-check-id="repository:execution-target"]')).toContainText('repository_target_required_per_turn');
});

test('readiness remediation opens canonical configuration, Secrets and TaskSource management', async ({ page }) => {
  await page.goto('http://127.0.0.1:18766/tests/browser/project_setup_ui_fixture.html');
  await page.locator('#project-setup-launch').click();
  await page.evaluate(async () => {
    window.remediationDestinations = [];
    window.CodexProductUI = { openWorkspace: (workspace, options) => window.remediationDestinations.push({ workspace, options }) };
    const original = window.fetch;
    window.fetch = (url, options) => String(url).endsWith('/readiness')
      ? Promise.resolve(new Response(JSON.stringify({ project_id: 'project-a', execution_ready: false, checks: [
        { id: 'configuration', status: 'blocked', code: 'configuration', message: 'Configure runtime', remediation_route: '/api/configuration' },
        { id: 'secret', status: 'blocked', code: 'secret', message: 'Bind secret', remediation_route: '/api/secrets' },
        { id: 'source', status: 'blocked', code: 'source', message: 'Configure source', remediation_route: '/api/projects/project-a/task-source' },
      ] }), { headers: { 'Content-Type': 'application/json' } })) : original(url, options);
    await window.CodexProjectSetup.refresh();
  });
  for (const id of ['configuration', 'secret', 'source']) await page.locator(`[data-check-id="${id}"] [data-setup-route]`).click();
  expect(await page.evaluate(() => remediationDestinations.map(item => item.workspace))).toEqual(['settings', 'secrets', 'work']);
  expect(await page.evaluate(() => location.hash)).not.toContain('setup/route');
});

async function openManifest(page, query = '') {
  await page.goto(`http://127.0.0.1:18766/tests/browser/project_setup_ui_fixture.html${query}`);
  await page.locator('#project-setup-launch').click();
  const dialog = page.locator('#project-setup-dialog');
  await dialog.locator('[data-setup-tab="plan"]').click();
  await dialog.locator('.project-setup-manifest-panel summary').click();
  return dialog;
}

test('invalid setup drafts survive validation and readiness refresh; Back and reset require discard', async ({ page }) => {
  const dialog = await openManifest(page);
  const input = dialog.locator('[data-project-setup-manifest]');
  const initial = await input.inputValue();
  await input.fill('{ unfinished desired topology');
  await dialog.locator('[data-setup-plan]').click();
  await expect(dialog.locator('.project-setup-error')).toContainText('normalized JSON');
  await expect(input).toHaveValue('{ unfinished desired topology');
  await page.evaluate(() => window.CodexProjectSetup.refresh());
  await expect(input).toHaveValue('{ unfinished desired topology');
  await expect(dialog.locator('[data-dirty-editor-status]')).toHaveText('Unsaved changes');
  page.once('dialog', value => value.dismiss());
  await dialog.locator('[data-project-setup-close]').click();
  await expect(dialog).toBeVisible();
  page.once('dialog', value => value.dismiss());
  await dialog.locator('[data-setup-reset]').click();
  await expect(input).toHaveValue('{ unfinished desired topology');
  page.once('dialog', value => value.accept());
  await dialog.locator('[data-setup-reset]').click();
  await expect(input).toHaveValue(initial);
  await expect(dialog.locator('[data-dirty-editor-status]')).toHaveText('No unsaved changes');
  expect(await page.evaluate(() => `${JSON.stringify(localStorage)} ${JSON.stringify(sessionStorage)}`)).not.toContain('unfinished desired topology');
});

test('failed setup apply retains the edited normalized manifest', async ({ page }) => {
  const dialog = await openManifest(page, '?failApply=1');
  const input = dialog.locator('[data-project-setup-manifest]');
  const manifest = JSON.parse(await input.inputValue()); manifest.project.name = 'Unsaved setup revision';
  const raw = JSON.stringify(manifest);
  await input.fill(raw);
  await dialog.locator('[data-setup-plan]').click();
  await expect(dialog.locator('.project-setup-plan-summary')).toContainText('bootstrap-plan-1');
  await dialog.locator('[data-setup-apply]').click();
  await expect(dialog.locator('[data-setup-action-feedback]')).toContainText('worker stopped during apply');
  await expect(input).toHaveValue(raw);
  await expect(dialog.locator('[data-dirty-editor-status]')).toHaveText('Unsaved changes');
});

test('late setup plan and apply preserve newer typing and acknowledge only submitted inputs', async ({ page }) => {
  const dialog = await openManifest(page);
  const input = dialog.locator('[data-project-setup-manifest]');
  const manifest = JSON.parse(await input.inputValue()); manifest.project.name = 'Reviewed setup';
  await input.fill(JSON.stringify(manifest));
  await page.evaluate(() => {
    const original = window.fetch;
    window.setupHold = { kind: 'plan', pending: false };
    window.fetch = async (url, options) => {
      if (String(url).endsWith(`/bootstrap/${window.setupHold.kind}`)) {
        window.setupHold.pending = true;
        await new Promise(resolve => { window.setupHold.release = resolve; });
      }
      return original(url, options);
    };
  });
  await dialog.locator('[data-setup-plan]').click();
  await expect.poll(() => page.evaluate(() => window.setupHold.pending)).toBe(true);
  manifest.project.name = 'Newer than plan'; const newer = JSON.stringify(manifest);
  await input.fill(newer);
  await page.evaluate(() => window.setupHold.release());
  await expect(dialog.locator('.project-setup-plan-summary')).toContainText('bootstrap-plan-1');
  await expect(input).toHaveValue(newer);
  await page.evaluate(() => { window.setupHold.kind = ''; window.setupHold.pending = false; });
  await dialog.locator('[data-setup-plan]').click();
  await expect.poll(() => page.evaluate(() => window.fixture.calls.filter(item => item.path.endsWith('/bootstrap/plan')).length)).toBe(2);
  await page.evaluate(() => { window.setupHold.kind = 'apply'; });
  await dialog.locator('[data-setup-apply]').click();
  await expect.poll(() => page.evaluate(() => window.setupHold.pending)).toBe(true);
  manifest.project.name = 'Newer than apply'; const latest = JSON.stringify(manifest);
  await input.fill(latest);
  await page.evaluate(() => window.setupHold.release());
  await expect(dialog.locator('[data-setup-action-feedback]')).toContainText('Project setup completed');
  await dialog.locator('[data-setup-tab="plan"]').click();
  await expect(input).toHaveValue(latest);
  await expect(dialog.locator('[data-dirty-editor-status]')).toHaveText('Unsaved changes');
});
