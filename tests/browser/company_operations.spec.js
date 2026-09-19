const { test, expect } = require('@playwright/test');

function overview() {
  return {
    organization_id: 'org-a',
    workspace_id: 'ws-a',
    evaluated_at: 1890000000,
    overall_health: 'degraded',
    counts: {
      business_entities: 1, external_records: 1, fact_diagnostics: 1,
      sources: 1, kpis: 1, goals: 1, decisions: 1,
      executive_activations: 1, attention_items: 1,
      pending_approvals: 1, action_intents: 1,
    },
    business_domains: ['revenue'],
    business_entities: [{
      id: 'business-entity-1', name: 'Northstar', entity_type: 'customer',
      lifecycle: 'active', classification: 'confidential',
    }],
    fact_diagnostics: [{
      business_entity_id: 'business-entity-1', entity_name: 'Northstar',
      entity_type: 'customer', fact_key: 'arr', freshness: 'fresh',
      conflict: true, selected_fact_id: 'company-fact-1', selected_value: 1200,
      unit: 'usd', provider: 'crm', external_record_ref_id: 'external-record-1',
      candidate_fact_ids: ['company-fact-1','company-fact-2'],
      conflict_fact_ids: ['company-fact-1','company-fact-2'],
      stale_fact_ids: [], revoked_source_fact_ids: [],
      reason: 'provider conflict retained',
    }],
    external_records: [{
      id: 'external-record-1', provider: 'crm', object_type: 'account',
      external_id: 'acct-1', lifecycle: 'active',
    }],
    sources: [{
      source_id: 'business-source-1', name: 'CRM Accounts',
      source_type: 'reference-crm', provider_id: 'crm',
      provider_instance: 'crm://prod', object_type: 'account',
      entity_type: 'customer', status: 'active',
      capabilities: ['incremental_sync','events'], credential_ref: 'secret://crm',
      cursor: '42', checkpoint: '42', last_success_at: 1889999900,
      last_error: null, projected_records: 12, stale_events: 2, duplicate_events: 1,
      capacity_status: 'throttled', capacity_reason: 'HTTP 429', retry_at: 1890000300,
      consecutive_failures: 2, drift_count: 1, conflict_count: 1,
      health: 'degraded',
      issues: ['provider capacity throttled: HTTP 429','1 reconciliation conflict(s) require inspection'],
    }],
    kpi_view: {
      current: false, blockers: ['arr: partial'], evaluated_at: 1890000000,
      items: [{
        kpi_id: 'business-kpi-arr', name: 'ARR', domain: 'revenue',
        value: 1200, unit: 'usd', currency: 'USD', readiness: 'partial',
        freshness: 'partial', kpi_revision: 2, metric_revision: 3,
        readiness_reasons: ['provider conflict'],
      }],
    },
    goals: [{ id: 'goal-1', title: 'Grow revenue', status: 'active' }],
    decisions: [{ id: 'decision-1', title: 'Pricing plan', status: 'approved' }],
    executive_activations: [{
      id: 'executive-1', subject: 'Revenue review', status: 'planned',
      trigger_kind: 'event', selections: [{ role_id: 'cro', score: 50 }],
    }],
    attention_items: [{
      id: 'attention-1', reason: 'Provider conflict requires review',
      type: 'business_sync', severity: 'high', status: 'open',
    }],
    approval_requests: [{
      id: 'approval-1', status: 'pending', target_fingerprint: 'fp-1',
      target: { operation: 'execute', object_type: 'decision', object_id: 'decision-1', resource_ids: ['resource-billing'] },
    }],
    action_intents: [{
      id: 'action-intent-1', status: 'requires_reconciliation',
      provider_type: 'billing', resource_ids: ['resource-billing'],
      action_id: 'plan.change',
      action_definition: { title: 'Change billing plan', risk_class: 'high' },
    }],
    blockers: ['arr: partial','fact Northstar/arr: conflict','action action-intent-1: requires_reconciliation'],
  };
}

function explain(subjectType = 'executive_activation') {
  return {
    organization_id: 'org-a', workspace_id: 'ws-a',
    subject_type: subjectType,
    subject_id: subjectType === 'executive_activation' ? 'executive-1' : 'action-intent-1',
    unresolved: ['action-intent-1: requires_reconciliation'],
    evaluated_at: 1890000000,
    stages: [
      { kind: 'trigger', label: 'Source request / event', object_id: 'event-1', status: 'event', summary: 'Review revenue', refs: ['business_data.event'], details: {} },
      { kind: 'company_fact', label: 'Governed CompanyFact', object_id: 'company-fact-1', status: 'fresh', summary: 'arr: 1200', refs: ['external-record-1'], details: {} },
      { kind: 'business_kpi', label: 'Canonical business KPI / Metric', object_id: 'business-kpi-arr', status: 'partial', summary: 'ARR: 1200', refs: ['metric-arr'], details: {} },
      { kind: 'executive_role', label: 'Executive role selection', object_id: 'cro', status: 'selected', summary: 'business-domain:revenue', refs: [], details: {} },
      { kind: 'executive_proposal', label: 'Advisory proposal / authority result', object_id: 'proposal-1', status: 'materialized', summary: 'Pricing review', refs: ['decision:decision-1'], details: {} },
      { kind: 'approval', label: 'Approval / authority gate', object_id: 'approval-1', status: 'pending', summary: 'High-impact action', refs: ['resource-billing'], details: {} },
      { kind: 'action_intent', label: 'Canonical external action intent', object_id: 'action-intent-1', status: 'requires_reconciliation', summary: 'Change billing plan', refs: ['decision-1'], details: { risk_class: 'high', resource_ids: ['resource-billing'], required_authority_level: 'execute' } },
      { kind: 'provider_receipt', label: 'Provider receipt', object_id: 'receipt-1', status: 'unknown', summary: null, refs: ['billing-change-42'], details: {} },
      { kind: 'evidence', label: 'Evidence / result', object_id: 'evidence-1', status: 'pass', summary: 'Reconciled provider state', refs: ['billing-change-42'], details: {} },
    ],
  };
}

async function routes(page) {
  await page.route('**/api/company-operations/overview', route => route.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({ item: overview() }),
  }));
  await page.route('**/api/business-context/entities/business-entity-1/context', route => route.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({
      entity: overview().business_entities[0],
      facts: [{
        id: 'company-fact-1', key: 'arr', value: 1200, unit: 'usd',
        lifecycle: 'active', classification: 'confidential',
        source: { authority: 'authoritative' },
      }],
      external_records: [{
        id: 'external-record-1', provider: 'crm', object_type: 'account',
        external_id: 'acct-1', lifecycle: 'active', synced_at: 1890000000,
        external_url: 'https://example.invalid/account/acct-1',
      }],
      relationships: [],
    }),
  }));
  await page.route('**/api/company-operations/explain/executive/executive-1', route => route.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({ item: explain('executive_activation') }),
  }));
  await page.route('**/api/company-operations/explain/action-intent/action-intent-1', route => route.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({ item: explain('action_intent') }),
  }));
  await page.route(/\/api\/business-data-sources\/business-source-1\/(sync|status)/, route => route.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({ item: {} }),
  }));
}

test('Company Operations exposes canonical state, provider health and bounded source controls', async ({ page }) => {
  await routes(page);
  await page.goto('http://127.0.0.1:18766/tests/browser/company_operations_fixture.html');
  await page.locator('#company-operations-button').click();

  const dialog = page.locator('#company-operations-dialog');
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText('DEGRADED');
  await expect(dialog).toContainText('fact Northstar/arr: conflict');

  await dialog.locator('[data-company-tab="sources"]').click();
  await expect(dialog).toContainText('CRM Accounts');
  await expect(dialog).toContainText('throttled');
  await expect(dialog).toContainText('HTTP 429');
  await expect(dialog).toContainText('Cursor');
  await expect(dialog).toContainText('42');
  await expect(dialog).toContainText('1 conflict(s)');
  await expect(dialog.locator('[data-company-sync="business-source-1"]')).toBeVisible();
  await expect(dialog.locator('[data-company-resync="business-source-1"]')).toContainText('Bounded full reconcile');
});

test('entity explorer visually separates canonical facts from external provider references', async ({ page }) => {
  await routes(page);
  await page.goto('http://127.0.0.1:18766/tests/browser/company_operations_fixture.html');
  await page.locator('#company-operations-button').click();
  const dialog = page.locator('#company-operations-dialog');

  await dialog.locator('[data-company-tab="entities"]').click();
  await dialog.locator('[data-company-entity="business-entity-1"]').click();

  await expect(dialog.locator('.company-ops-card.canonical')).toContainText('Canonical CompanyFacts');
  await expect(dialog.locator('.company-ops-card.canonical')).toContainText('arr = 1200 usd');
  await expect(dialog.locator('.company-ops-card.external')).toContainText('External provider references');
  await expect(dialog.locator('.company-ops-card.external')).toContainText('crm / account');
  await expect(dialog.locator('.company-ops-card.external a')).toHaveAttribute('href', 'https://example.invalid/account/acct-1');
});

test('Executive explain timeline reaches approval, blast radius, unknown receipt and evidence', async ({ page }) => {
  await routes(page);
  await page.goto('http://127.0.0.1:18766/tests/browser/company_operations_fixture.html');
  await page.locator('#company-operations-button').click();
  const dialog = page.locator('#company-operations-dialog');

  await dialog.locator('[data-company-tab="executive"]').click();
  await dialog.locator('[data-company-explain-exec="executive-1"]').click();

  await expect(dialog).toContainText('Governed CompanyFact');
  await expect(dialog).toContainText('Canonical business KPI / Metric');
  await expect(dialog).toContainText('Executive role selection');
  await expect(dialog).toContainText('Approval / authority gate');
  await expect(dialog).toContainText('Canonical external action intent');
  await expect(dialog).toContainText('risk high');
  await expect(dialog).toContainText('resource-billing');
  await expect(dialog).toContainText('Provider receipt');
  await expect(dialog).toContainText('unknown');
  await expect(dialog).toContainText('Evidence / result');
  await expect(dialog).toContainText('requires_reconciliation');
});

test('Company Operations remains usable at phone width', async ({ page }) => {
  await routes(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('http://127.0.0.1:18766/tests/browser/company_operations_fixture.html');
  await page.locator('#company-operations-button').click();
  const dialog = page.locator('#company-operations-dialog');
  await expect(dialog).toBeVisible();
  await dialog.locator('[data-company-tab="sources"]').click();
  const card = dialog.locator('.company-ops-source').first();
  const box = await card.boundingBox();
  expect(box.width).toBeLessThanOrEqual(390);
  await expect(dialog.locator('[data-company-tab="actions"]')).toBeVisible();
});
