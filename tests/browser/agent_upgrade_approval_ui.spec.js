import { expect, test } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';

test('agent Upgrade approval exposes canonical provenance and submits only a decision', async ({ page }) => {
  const request = {
    id: 'approval-agent', revision: 1, status: 'pending', organization_id: 'org', workspace_id: 'workspace',
    requester_identity_id: 'execution-worker-local', requester_principal_kind: 'service',
    reason: 'Agent execution-worker-local requests scanner-contract. Execution execution-1; original request authorizer local-admin.',
    audit_refs: ['broker-audit-1', '<img src=x onerror="window.bad=true">'], decisions: [],
    requirement: { quorum: 1, required_assurance: 'mfa', allow_self_approval: false },
    target: { operation: 'upgrade.step.execute', object_type: 'upgrade_step', object_id: 'plan:scanner-contract',
      target_version: 'reviewed-revision', target_digest: 'sha256:reviewed-digest' },
  };
  await page.route('http://127.0.0.1:18766/approval-fixture', route => route.fulfill({ contentType: 'text/html',
    body: '<div class="topbar"><div class="controls"></div></div><script src="/static/approval_requests_ui.js"></script>' }));
  await page.route('**/static/approval_requests_ui.js', route => route.fulfill({ contentType: 'text/javascript',
    body: fs.readFileSync(path.join(__dirname, '../../static/approval_requests_ui.js'), 'utf8') }));
  const decisions = [];
  await page.route('**/api/approval-requests**', route => {
    if (route.request().method() === 'POST') {
      decisions.push({ path: new URL(route.request().url()).pathname, body: route.request().postDataJSON() });
      return route.fulfill({ json: { approval_request: request } });
    }
    return route.fulfill({ json: { approval_requests: [request] } });
  });
  await page.goto('http://127.0.0.1:18766/approval-fixture');
  await page.getByRole('button', { name: /Approvals/ }).click();
  const card = page.locator('[data-approval-id="approval-agent"]');
  await expect(card).toContainText('execution-worker-local (service)');
  await expect(card).toContainText('original request authorizer local-admin');
  await expect(card).toContainText('execution-1');
  await expect(card).toContainText('broker-audit-1');
  await expect(card).toContainText('plan:scanner-contract');
  await expect(card).toContainText('sha256:reviewed-digest');
  await expect(card.locator('img')).toHaveCount(0);
  page.once('dialog', dialog => dialog.accept('Reviewed exact scanner target'));
  await card.getByRole('button', { name: 'Approve', exact: true }).click();
  await expect.poll(() => decisions.length).toBe(1);
  expect(decisions[0]).toEqual({ path: '/api/approval-requests/approval-agent/decisions', body: {
    outcome: 'approve', reason: 'Reviewed exact scanner target', idempotency_key: 'ui:approval-agent:r1:approve',
  } });
});
