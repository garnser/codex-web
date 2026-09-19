const { test, expect } = require('@playwright/test');

const record = {
  record_id: 'draft-1',
  definition_id: 'authority.roles.default',
  kind: 'authority-role-catalog',
  definition_schema_version: '1.0',
  revision: 2,
  scope_type: 'workspace',
  scope_id: 'ws-a',
  lifecycle: 'draft',
  checksum: 'a'.repeat(64),
  created_by: 'creator-a',
  approval_metadata: {},
};

function actor(identity, roles) {
  return {
    identity_id: identity,
    principal_kind: 'human',
    organization_id: 'org-a',
    workspace_id: 'ws-a',
    roles,
    team_ids: [],
    assurance: 'mfa',
    service_scopes: [],
  };
}

async function hydrate(page) {
  await page.waitForFunction(
    () => document.documentElement.dataset.definitionPublicationManagementReady === 'true',
  );
  await expect(page.locator('#definition-lifecycle-assurance')).toContainText('Current actor');
  await page.evaluate((item) => {
    window.dispatchEvent(new CustomEvent('codex:definition-registry-rendered', {
      detail: {
        records: [item],
        schemas: [{ kind: item.kind, schema_version: '1.0' }],
        projects: [],
      },
    }));
  }, record);
}

test('sensitive definition publish blocks without exact canonical approval', async ({ page }) => {
  const posts = [];
  await page.addInitScript(() => {
    window.prompt = () => 'test reason';
    window.confirm = () => true;
  });
  await page.route('**/api/identity/me', (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify(actor('publisher-a', ['admin'])),
  }));
  await page.route('**/api/definitions/draft-1/publication-preflight', (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({
      assessment: {
        record_id: 'draft-1',
        candidate_revision: 2,
        candidate_checksum: 'a'.repeat(64),
        active_record_id: 'active-1',
        active_revision: 1,
        change_classes: ['authority.production_scope_added'],
        reasons: ['grant added production environment authority'],
        requires_approval: true,
        fingerprint: 'f'.repeat(64),
      },
      approvals: [],
    }),
  }));
  await page.route('**/api/definitions/draft-1/publish', async (route) => {
    posts.push(route.request().postDataJSON());
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ record }) });
  });

  await page.goto('http://127.0.0.1:18766/tests/browser/definition_publication_fixture.html');
  await hydrate(page);
  await page.locator('[data-definition-action="publish"]').click();

  await expect(page.locator('#definition-lifecycle-status')).toContainText(
    'sensitive authority/definition expansion requires approval',
  );
  await expect(page.locator('#definition-lifecycle-status')).toContainText(
    'authority.production_scope_added',
  );
  expect(posts).toEqual([]);
});

test('approver-only actor can approve exact sensitive fingerprint', async ({ page }) => {
  const approvals = [];
  await page.addInitScript(() => {
    window.prompt = () => 'reviewed production expansion';
    window.confirm = () => true;
  });
  await page.route('**/api/identity/me', (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify(actor('approver-a', ['approver'])),
  }));
  await page.route('**/api/definitions/draft-1/publication-preflight', (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({
      assessment: {
        record_id: 'draft-1',
        candidate_revision: 2,
        candidate_checksum: 'a'.repeat(64),
        active_record_id: 'active-1',
        active_revision: 1,
        change_classes: ['authority.production_scope_added'],
        reasons: ['grant added production environment authority'],
        requires_approval: true,
        fingerprint: 'f'.repeat(64),
      },
      approvals: [],
    }),
  }));
  await page.route('**/api/definitions/draft-1/publication-approvals', async (route) => {
    approvals.push(route.request().postDataJSON());
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        approval: {
          id: 'definition-approval-1',
          record_id: 'draft-1',
          fingerprint: 'f'.repeat(64),
          approved_by: 'approver-a',
        },
      }),
    });
  });

  await page.goto('http://127.0.0.1:18766/tests/browser/definition_publication_fixture.html');
  await hydrate(page);
  await expect(page.locator('[data-definition-action="publish"]')).toHaveCount(0);
  await page.locator('[data-definition-action="approve-publication"]').click();

  await expect.poll(() => approvals.length).toBe(1);
  expect(approvals[0]).toEqual({ reason: 'reviewed production expansion' });
  await expect(page.locator('#definition-lifecycle-status')).toContainText(
    'Recorded approval definition-approval-1',
  );
});

test('publisher sends exact matching approval id returned by preflight', async ({ page }) => {
  const posts = [];
  await page.addInitScript(() => {
    window.prompt = (message) => message.includes('Additional publication metadata') ? '{}' : 'publish reason';
    window.confirm = () => true;
  });
  await page.route('**/api/identity/me', (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify(actor('publisher-a', ['admin'])),
  }));
  await page.route('**/api/definitions/draft-1/publication-preflight', (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({
      assessment: {
        record_id: 'draft-1',
        candidate_revision: 2,
        candidate_checksum: 'a'.repeat(64),
        active_record_id: 'active-1',
        active_revision: 1,
        change_classes: ['authority.production_scope_added'],
        reasons: ['grant added production environment authority'],
        requires_approval: true,
        fingerprint: 'f'.repeat(64),
      },
      approvals: [{
        id: 'definition-approval-1',
        record_id: 'draft-1',
        fingerprint: 'f'.repeat(64),
        approved_by: 'approver-a',
      }],
    }),
  }));
  await page.route('**/api/definitions/draft-1/publish', async (route) => {
    posts.push(route.request().postDataJSON());
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ record }) });
  });

  await page.goto('http://127.0.0.1:18766/tests/browser/definition_publication_fixture.html');
  await hydrate(page);
  await page.locator('[data-definition-action="publish"]').click();

  await expect.poll(() => posts.length).toBe(1);
  expect(posts[0]).toMatchObject({
    reason: 'publish reason',
    publication_approval_id: 'definition-approval-1',
    approval_metadata: {},
  });
});
