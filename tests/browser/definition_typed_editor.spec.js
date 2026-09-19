const { test, expect } = require('@playwright/test');

const authorityRecord = {
  record_id: 'authority-record-1',
  definition_id: 'authority.roles.default',
  kind: 'authority-role-catalog',
  definition_schema_version: '1.0',
  revision: 4,
  scope_type: 'workspace',
  scope_id: 'default',
  lifecycle: 'published',
  checksum: 'a'.repeat(64),
  effective_from: null,
  effective_until: null,
  min_engine_version: null,
  max_engine_version: null,
  payload: {
    roles: [{
      id: 'developer',
      name: 'Developer',
      description: 'Scoped developer authority.',
      lifecycle: 'active',
      inherits: [],
      grants: [{
        id: 'developer.read',
        capability: 'repository.read',
        level: 'read',
        project_ids: [],
        resource_ids: [],
        resource_types: ['repository'],
        resource_risks: [],
        resource_sensitivities: [],
        environments: [],
        max_amount_usd: null,
        max_input_tokens: null,
        max_output_tokens: null,
        max_model_calls: null,
        max_autonomous_risk: 'low',
        approvals: { count: 0, role_ids: [] },
      }],
    }],
    bindings: [],
    delegations: [],
  },
};

test('typed Definition editor clones a Role and submits only a derived draft', async ({ page }) => {
  const requests = [];
  await page.route('**/api/definitions/**', async (route) => {
    const request = route.request();
    requests.push({
      url: request.url(),
      method: request.method(),
      body: request.postDataJSON(),
    });
    if (request.url().endsWith('/api/definitions/drafts')) {
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          record: {
            record_id: 'derived-draft-5',
            revision: 5,
            checksum: 'b'.repeat(64),
          },
        }),
      });
      return;
    }
    await route.abort();
  });

  page.on('dialog', async (dialog) => {
    expect(dialog.type()).toBe('prompt');
    await dialog.accept('developer-copy');
  });

  await page.goto('http://127.0.0.1:18766/tests/browser/definition_typed_editor_fixture.html');
  await page.evaluate((record) => {
    window.dispatchEvent(new CustomEvent('codex:definition-registry-rendered', {
      detail: { records: [record] },
    }));
  }, authorityRecord);

  await page.locator('#definition-typed-source').selectOption('authority-record-1');
  await expect(page.locator('#definition-typed-source-meta')).toContainText('checksum');
  await page.locator('[data-typed-action="clone-role"]').click();
  await expect(page.locator('.typed-authority-role')).toHaveCount(2);

  await page.locator('.typed-authority-role').nth(0).locator('.typed-role-lifecycle').selectOption('deprecated');
  await page.locator('#definition-typed-reason').fill('Typed lifecycle and clone revision');
  await page.locator('#definition-typed-save').click();

  await expect.poll(() => requests.length).toBe(1);
  expect(requests[0].method).toBe('POST');
  expect(requests[0].url).toContain('/api/definitions/drafts');
  expect(requests[0].url).not.toContain('/publish');
  expect(requests[0].body).toMatchObject({
    definition_id: 'authority.roles.default',
    kind: 'authority-role-catalog',
    definition_schema_version: '1.0',
    scope_type: 'workspace',
    scope_id: 'default',
    derived_from_record_id: 'authority-record-1',
    reason: 'Typed lifecycle and clone revision',
  });
  expect(requests[0].body.payload.roles).toHaveLength(2);
  expect(requests[0].body.payload.roles[0].lifecycle).toBe('deprecated');
  expect(requests[0].body.payload.roles[1].id).toBe('developer-copy');
  expect(requests[0].body.payload.roles[1].grants[0].id).toBe('developer-copy.grant-1');
  await expect(page.locator('#definition-typed-status')).toContainText('nothing was activated');
});
