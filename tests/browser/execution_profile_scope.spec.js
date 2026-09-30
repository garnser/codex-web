const { test, expect } = require('@playwright/test');

function catalog(name) {
  return {
    default_profile_id: 'repository-write',
    definition: { definition_id: `profiles-${name}`, revision: 2 },
    items: [{ id: 'repository-write', name, workspaceMode: 'repository',
      repositoryAccess: 'mutable', requiredWorkerCapabilities: ['git', 'command_execution'] }],
  };
}

async function mount(page) {
  await page.route('**/codex/profile-scope-test', route => route.fulfill({
    contentType: 'text/html', body: '<select id="execution-profile"></select>'
      + '<div id="execution-profile-summary"></div><select id="repository-target"></select>'
      + '<select id="repository-read-context"></select><select id="repository-write-targets"></select>',
  }));
  await page.goto('http://127.0.0.1:18766/codex/profile-scope-test');
  await page.evaluate(async () => {
    window.profiles = await import('/static/execution_profile_controls.js');
    window.renderProfiles = () => profiles.render('', value => {
      const span = document.createElement('span');
      span.textContent = String(value);
      return span.innerHTML;
    });
  });
}

test('Project switching clears profile controls and keeps management links in the current Project', async ({ page }) => {
  await mount(page);
  await page.evaluate(value => { profiles.setCatalog(value, 'project-a'); renderProfiles(); }, catalog('A'));
  await expect(page.locator('#manage-execution-profiles')).toHaveAttribute('href', '/codex/projects/project-a/definitions?execution_profile=repository-write');
  await page.evaluate(() => window.dispatchEvent(new CustomEvent('codex:project-changed', {
    detail: { projectId: 'project-b' },
  })));
  await expect(page.locator('#execution-profile')).toBeDisabled();
  await expect(page.locator('#repository-target')).toBeDisabled();
  await expect(page.locator('#repository-write-targets')).toBeDisabled();
  await expect(page.locator('#manage-execution-profiles')).toHaveCount(0);
  await page.evaluate(value => { profiles.setCatalog(value, 'project-b'); renderProfiles(); }, catalog('B'));
  await expect(page.locator('#execution-profile')).toBeEnabled();
  await expect(page.locator('#repository-write-targets')).toBeEnabled();
  await expect(page.locator('#execution-profile option')).toHaveText('B');
  await expect(page.locator('#manage-execution-profiles')).toHaveAttribute('href', '/codex/projects/project-b/definitions?execution_profile=repository-write');
  await page.evaluate(() => { profiles.setCatalog({ items: [] }, 'empty'); renderProfiles(); });
  await expect(page.locator('#execution-profile')).toBeDisabled();
  await expect(page.locator('#repository-read-context')).toBeDisabled();
  await expect(page.locator('#execution-profile-summary')).not.toContainText('profiles-B');
});

test('a late profile response cannot replace a newer Project snapshot even if cancellation is ignored', async ({ page }) => {
  await mount(page);
  await page.evaluate(old => {
    const original = window.fetch;
    window.fetch = (url, options) => String(url).includes('/api/execution-profiles')
      ? new Promise(resolve => { window.releaseOld = () => resolve(new Response(JSON.stringify(old))); })
      : original(url, options);
    window.oldLoad = profiles.load('project-a');
  }, catalog('A'));
  await page.evaluate(value => { profiles.setCatalog(value, 'project-b'); renderProfiles(); }, catalog('B'));
  await page.evaluate(async () => { releaseOld(); await oldLoad; renderProfiles(); });
  await expect(page.locator('#execution-profile option')).toHaveText('B');
  await expect(page.locator('#manage-execution-profiles')).toHaveAttribute('href', '/codex/projects/project-b/definitions?execution_profile=repository-write');
});

test('failed or absent Project context leaves controls disabled and never loads a fallback Project', async ({ page }) => {
  await mount(page);
  const requests = [];
  await page.route('**/api/execution-profiles?*', route => {
    requests.push(new URL(route.request().url()).searchParams.get('project_id'));
    return route.fulfill({ status: 404, json: { detail: 'Project not found' } });
  });
  await page.evaluate(value => { profiles.setCatalog(value, 'project-a'); renderProfiles(); }, catalog('A'));
  await page.evaluate(() => profiles.load('foreign'));
  await expect(page.locator('#execution-profile-summary')).toContainText('unavailable');
  await expect(page.locator('#execution-profile')).toBeDisabled();
  await expect(page.locator('#manage-execution-profiles')).toHaveCount(0);
  await page.evaluate(() => profiles.load(''));
  await expect(page.locator('#execution-profile-summary')).toContainText('Select a Project');
  expect(requests).toEqual(['foreign']);
});
