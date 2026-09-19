const { test, expect } = require('@playwright/test');

test('authority explorer renders provenance and simulates an exact revision without mutation', async ({ page }) => {
  const requests = [];
  await page.route('**/api/authority/**', async (route) => {
    const request = route.request();
    requests.push({
      url: request.url(),
      method: request.method(),
      body: request.postDataJSON(),
    });
    if (request.url().includes('/api/authority/effective')) {
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          actor: {
            identity_id: 'developer-a',
            principal_kind: 'human',
            organization_id: 'local',
            workspace_id: 'default',
            team_ids: ['platform'],
          },
          project_id: 'project-a',
          work_item_ref: null,
          definition: {
            reference: {
              kind: 'authority-role-catalog',
              definition_id: 'authority.roles.default',
              revision: 4,
              record_id: 'active-record',
              checksum: 'a'.repeat(64),
            },
            scope_type: 'project',
            scope_id: 'project-a',
          },
          assignments: [{
            source_type: 'binding',
            source_id: 'developer-binding',
            role_id: 'developer',
            role_lifecycle: 'active',
            project_ids: ['project-a'],
          }],
          permission_matrix: [{
            capability: 'deploy.release',
            level: 'execute',
            role_id: 'developer',
            grant_id: 'developer.deploy',
            source_type: 'binding',
            source_id: 'developer-binding',
            inheritance_path: ['developer'],
            project_ids: ['project-a'],
            resource_ids: [],
            resource_types: [],
            resource_risks: [],
            resource_sensitivities: [],
            environments: ['production'],
            max_amount_usd: null,
            max_input_tokens: null,
            max_output_tokens: null,
            max_model_calls: 1,
            max_autonomous_risk: 'medium',
            approvals: { count: 1, role_ids: ['release-approver'] },
          }],
          role_view: null,
        }),
      });
      return;
    }
    if (request.url().includes('/api/authority/simulate')) {
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          mode: 'exact-revision',
          record_id: 'candidate-record',
          decision: {
            outcome: 'deny',
            reasons: ['developer/developer.deploy: grant requires 1 qualifying approval(s)'],
            definition_ref: {
              kind: 'authority-role-catalog',
              definition_id: 'authority.roles.default',
              revision: 5,
              record_id: 'candidate-record',
              checksum: 'b'.repeat(64),
            },
          },
        }),
      });
      return;
    }
    await route.abort();
  });

  await page.goto('http://127.0.0.1:18766/tests/browser/authority_policy_explorer_fixture.html');
  await page.locator('#authority-explorer-project').fill('project-a');
  await page.locator('#authority-explorer-load').click();
  await expect(page.locator('#authority-explorer-result')).toContainText('Effective Definition');
  await expect(page.locator('#authority-explorer-result')).toContainText('developer-binding');
  await expect(page.locator('#authority-explorer-result')).toContainText('deploy.release');

  await page.locator('#authority-sim-record').fill('candidate-record');
  await page.locator('#authority-sim-capability').fill('deploy.release');
  await page.locator('#authority-sim-approvals').fill('');
  await page.locator('#authority-sim-run').click();

  await expect(page.locator('#authority-simulation-result')).toContainText('DENY');
  await expect(page.locator('#authority-simulation-result')).toContainText('Canonical reasons');
  const simulation = requests.find((item) => item.url.includes('/api/authority/simulate'));
  expect(simulation.method).toBe('POST');
  expect(simulation.body.record_id).toBe('candidate-record');
  expect(simulation.body.request).toMatchObject({
    capability: 'deploy.release',
    level: 'execute',
    project_id: 'project-a',
  });
  expect(requests.every((item) => !item.url.includes('/publish'))).toBeTruthy();
});
