const { test, expect } = require('@playwright/test');

function knowledge() {
  return {
    id: 'knowledge-adr',
    organization_id: 'local',
    workspace_id: 'default',
    project_id: 'project-a',
    logical_key: 'adr/database/postgres',
    version: 2,
    object_type: 'architecture_decision',
    title: 'PostgreSQL persistence decision',
    summary: 'Use PostgreSQL for durable relational persistence.',
    content: 'The control plane uses PostgreSQL.',
    tags: ['architecture', 'database'],
    canonical_refs: [],
    provenance: {
      source_kind: 'repository',
      source_ref: 'github:garnser/codex-web:docs/adr/database.md',
      source_url: 'https://example.invalid/adr',
      source_revision: 'abc123',
      authored_by: 'architect-a',
      authored_at: 1889999900,
      observed_at: 1890000000,
      evidence_ids: ['evidence-a'],
      source_governance_record_ids: [],
    },
    classification: 'internal',
    governance_record_id: 'data-memory-a',
    retention_expires_at: null,
    retention_action: 'redact',
    deny_model_context: false,
    required_role_ids: [],
    review_after: null,
    valid_until: null,
    lifecycle: 'current',
    freshness: 'fresh',
    previous_version_id: 'knowledge-adr-v1',
    superseded_by_id: null,
    invalid_reason: null,
    content_sha256: 'a'.repeat(64),
    created_by: 'architect-a',
    created_at: 1889999000,
    updated_at: 1890000000,
  };
}

function retrieval() {
  return {
    id: 'memory-retrieval-a',
    organization_id: 'local',
    workspace_id: 'default',
    actor_id: 'owner-a',
    query_sha256: 'b'.repeat(64),
    filters: { object_types: ['architecture_decision'] },
    selected_knowledge_ids: ['knowledge-adr'],
    selected_scores: { 'knowledge-adr': 0.91 },
    selected_freshness: { 'knowledge-adr': 'fresh' },
    denied: [{ knowledge_id: 'knowledge-secret', reason: 'governance:classification_exceeds_context_limit' }],
    packed_tokens: 420,
    candidate_count: 3,
    top_k: 8,
    max_context_tokens: 6000,
    retrieval_backend_id: 'local-vector',
    retrieval_index_revision: 'index-r7',
    embedding_provider_id: 'local',
    embedding_model_id: 'concept-hash',
    embedding_model_revision: '1',
    created_at: 1890000010,
  };
}

async function installRoutes(page) {
  const runs = [retrieval()];
  await page.route(/\/api\/memory(?:[/?].*)?$/, async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    let body;
    if (path === '/api/memory' && request.method() === 'GET') {
      body = { items: [knowledge()], count: 1 };
    } else if (path === '/api/memory/index') {
      body = { status: { backend_id: 'local-vector', index_revision: 'index-r7', document_count: 1, healthy: true, embedding_identity: { provider_id: 'local', model_id: 'concept-hash', model_revision: '1' } } };
    } else if (path === '/api/memory/retrievals' && request.method() === 'GET') {
      body = { items: runs, count: runs.length };
    } else if (path === '/api/memory/retrievals/memory-retrieval-a') {
      body = { item: runs[0] };
    } else if (path === '/api/memory/knowledge-adr') {
      body = { item: knowledge() };
    } else if (path === '/api/memory/knowledge-adr/versions') {
      body = { items: [{ ...knowledge(), version: 1, id: 'knowledge-adr-v1', lifecycle: 'superseded', freshness: 'superseded' }, knowledge()], count: 2 };
    } else if (path === '/api/memory/knowledge-adr/relationships') {
      body = { items: [{ id: 'rel-a', source_knowledge_id: 'knowledge-adr', target_knowledge_id: 'knowledge-adr-v1', relationship_type: 'supersedes', note: 'current decision' }], count: 1 };
    } else if (path === '/api/memory/search' && request.method() === 'POST') {
      body = {
        retrieval_id: 'memory-retrieval-a',
        items: [{ knowledge_id: 'knowledge-adr', version: 2, title: 'PostgreSQL persistence decision', freshness: 'fresh', score: 0.91, reasons: ['semantic:0.9100', 'structured:type'], context_excerpt: 'Use PostgreSQL.' }],
        denied: runs[0].denied,
        packed_tokens: 420,
        candidate_count: 3,
        top_k: 8,
        max_context_tokens: 6000,
      };
    } else {
      return route.fulfill({ status: 404, contentType: 'application/json', body: '{}' });
    }
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
  });
}

test('Memory workspace exposes provenance, versions, relationships and retrieval budgets', async ({ page }) => {
  await installRoutes(page);
  await page.goto('http://127.0.0.1:18766/tests/browser/memory_fixture.html');
  await page.locator('#memory-button').click();

  const dialog = page.locator('#memory-dialog');
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText('local-vector');
  await expect(dialog).toContainText('index-r7');
  await expect(dialog).toContainText('PostgreSQL persistence decision');

  await dialog.locator('[data-memory-id="knowledge-adr"]').click();
  await expect(dialog).toContainText('github:garnser/codex-web:docs/adr/database.md');
  await expect(dialog).toContainText('data-memory-a');
  await expect(dialog).toContainText('v1 · superseded');
  await expect(dialog).toContainText('supersedes');

  await dialog.locator('.memory-query').fill('database architecture');
  await dialog.locator('.memory-search').click();
  await expect(dialog).toContainText('420/6000 tokens');
  await expect(dialog).toContainText('[memory:knowledge-adr@v2]');
  await expect(dialog).toContainText('semantic:0.9100');
  await expect(dialog).toContainText('classification_exceeds_context_limit');
});

test('Memory workspace stays usable on a phone viewport', async ({ page }) => {
  await installRoutes(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('http://127.0.0.1:18766/tests/browser/memory_fixture.html');
  await page.locator('#memory-button').click();
  const dialog = page.locator('#memory-dialog');
  await expect(dialog).toBeVisible();
  await expect(dialog.locator('.memory-layout')).toBeVisible();
  await expect(dialog.locator('.memory-query')).toBeVisible();
});
