import { request } from './api_client.js';

const state = {
  records: [],
  retrievals: [],
  indexStatus: null,
  selectedKnowledgeId: '',
  selectedRetrievalId: '',
  detail: null,
  versions: [],
  relationships: [],
  liveResult: null,
};

const esc = (value) => String(value ?? '')
  .replaceAll('&', '&amp;')
  .replaceAll('<', '&lt;')
  .replaceAll('>', '&gt;')
  .replaceAll('"', '&quot;')
  .replaceAll("'", '&#039;');

function fmtTime(value) {
  if (!value) return '—';
  const date = new Date(Number(value) * 1000);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
}

function ensureShell() {
  if (document.querySelector('#memory-dialog')) return;

  const link = document.createElement('link');
  link.rel = 'stylesheet';
  link.href = '/static/memory_ui.css';
  document.head.appendChild(link);

  const button = document.createElement('button');
  button.id = 'memory-button';
  button.type = 'button';
  button.className = 'ghost-button';
  button.textContent = 'Memory';
  button.title = 'Open Organizational Memory explorer';
  (document.querySelector('.topbar .controls') || document.body).prepend(button);

  const dialog = document.createElement('dialog');
  dialog.id = 'memory-dialog';
  dialog.className = 'memory-dialog';
  dialog.innerHTML = `
    <div class="memory-shell">
      <header class="memory-header">
        <div>
          <h2>Organizational Memory</h2>
          <p>Canonical, governed knowledge and the bounded retrieval runs that influence reasoning.</p>
        </div>
        <button type="button" class="icon-button memory-close" aria-label="Close">×</button>
      </header>
      <div class="memory-toolbar">
        <button type="button" class="ghost-button memory-refresh">Refresh</button>
        <input class="memory-query" type="search" placeholder="Retrieve relevant decisions, policies, procedures…" aria-label="Memory retrieval query" />
        <select class="memory-type" aria-label="Object type">
          <option value="">All object types</option>
          <option value="architecture_decision">Architecture decisions</option>
          <option value="policy">Policies</option>
          <option value="procedure">Procedures</option>
          <option value="repository">Repositories</option>
          <option value="service">Services</option>
          <option value="incident">Incidents</option>
          <option value="postmortem">Postmortems</option>
          <option value="project">Projects</option>
          <option value="goal">Goals</option>
          <option value="decision">Decisions</option>
          <option value="other">Other</option>
        </select>
        <label>Top K <input class="memory-top-k" type="number" min="1" max="50" value="8" /></label>
        <label>Context <input class="memory-budget" type="number" min="128" max="32000" value="6000" /> tokens</label>
        <button type="button" class="primary-button memory-search">Run retrieval</button>
        <span class="memory-status" aria-live="polite"></span>
      </div>
      <div class="memory-index" aria-label="Retrieval index status"></div>
      <div class="memory-layout">
        <aside class="memory-sidebar">
          <div class="memory-tabs" role="tablist">
            <button type="button" class="memory-tab active" data-tab="records">Knowledge</button>
            <button type="button" class="memory-tab" data-tab="retrievals">Retrievals</button>
          </div>
          <div class="memory-list memory-record-list"></div>
          <div class="memory-list memory-retrieval-list" hidden></div>
        </aside>
        <main class="memory-detail">
          <div class="memory-empty">Select a knowledge object or retrieval run.</div>
        </main>
      </div>
    </div>`;
  document.body.appendChild(dialog);

  button.addEventListener('click', async () => {
    dialog.showModal();
    await refreshAll();
  });
  dialog.querySelector('.memory-close').addEventListener('click', () => dialog.close());
  dialog.querySelector('.memory-refresh').addEventListener('click', refreshAll);
  dialog.querySelector('.memory-search').addEventListener('click', runRetrieval);
  dialog.querySelector('.memory-query').addEventListener('keydown', (event) => {
    if (event.key === 'Enter') runRetrieval();
  });
  dialog.querySelectorAll('.memory-tab').forEach((tab) => {
    tab.addEventListener('click', () => selectTab(tab.dataset.tab));
  });
}

function setStatus(message, isError = false) {
  const node = document.querySelector('.memory-status');
  if (!node) return;
  node.textContent = message || '';
  node.classList.toggle('error', Boolean(isError));
}

function selectTab(tab) {
  document.querySelectorAll('.memory-tab').forEach((node) => {
    node.classList.toggle('active', node.dataset.tab === tab);
  });
  const records = document.querySelector('.memory-record-list');
  const retrievals = document.querySelector('.memory-retrieval-list');
  records.hidden = tab !== 'records';
  retrievals.hidden = tab !== 'retrievals';
  if (tab === 'retrievals' && state.retrievals.length && !state.selectedRetrievalId) {
    selectRetrieval(state.retrievals[0].id);
  }
}

async function refreshAll() {
  setStatus('Loading…');
  try {
    const [memory, retrievals, index] = await Promise.all([
      request('/api/memory?include_inactive=true&limit=500'),
      request('/api/memory/retrievals?limit=200'),
      request('/api/memory/index'),
    ]);
    state.records = Array.isArray(memory?.items) ? memory.items : [];
    state.retrievals = Array.isArray(retrievals?.items) ? retrievals.items : [];
    state.indexStatus = index?.status || null;
    renderIndex();
    renderRecords();
    renderRetrievals();
    setStatus(`${state.records.length} objects · ${state.retrievals.length} retrieval runs`);
  } catch (error) {
    setStatus(error.message || 'Failed to load memory', true);
  }
}

function renderIndex() {
  const host = document.querySelector('.memory-index');
  if (!host) return;
  const status = state.indexStatus || {};
  const identity = status.embedding_identity || {};
  host.innerHTML = `
    <span><strong>Derived index</strong> ${esc(status.backend_id || 'unknown')}</span>
    <span>revision ${esc(status.index_revision || '—')}</span>
    <span>${esc(status.document_count ?? 0)} docs</span>
    <span>embedding ${esc(identity.model_id ? `${identity.provider_id}/${identity.model_id}@${identity.model_revision || 'unknown'}` : 'none')}</span>
    <span class="${status.healthy === false ? 'memory-danger' : ''}">${status.healthy === false ? esc(status.last_error || 'unhealthy') : 'healthy'}</span>
    <small>Derived search state is rebuildable; canonical knowledge remains authoritative.</small>`;
}

function badge(value) {
  const normalized = String(value || 'unknown').toLowerCase();
  return `<span class="memory-badge ${esc(normalized)}">${esc(normalized)}</span>`;
}

function renderRecords() {
  const host = document.querySelector('.memory-record-list');
  if (!host) return;
  if (!state.records.length) {
    host.innerHTML = '<div class="memory-empty">No knowledge objects in this workspace.</div>';
    return;
  }
  host.innerHTML = state.records.map((item) => `
    <button type="button" class="memory-row ${item.id === state.selectedKnowledgeId ? 'selected' : ''}" data-memory-id="${esc(item.id)}">
      <strong>${esc(item.title)}</strong>
      <span>${esc(item.object_type)} · v${esc(item.version)} · ${esc(item.project_id || 'company')}</span>
      <small>${badge(item.freshness)} ${badge(item.lifecycle)} · ${esc(item.provenance?.source_kind || 'unknown')}</small>
    </button>`).join('');
  host.querySelectorAll('.memory-row').forEach((row) => row.addEventListener('click', () => selectKnowledge(row.dataset.memoryId)));
}

function renderRetrievals() {
  const host = document.querySelector('.memory-retrieval-list');
  if (!host) return;
  if (!state.retrievals.length) {
    host.innerHTML = '<div class="memory-empty">No retrieval runs have been recorded yet.</div>';
    return;
  }
  host.innerHTML = state.retrievals.map((item) => `
    <button type="button" class="memory-row ${item.id === state.selectedRetrievalId ? 'selected' : ''}" data-retrieval-id="${esc(item.id)}">
      <strong>${esc(item.id)}</strong>
      <span>${esc(item.selected_knowledge_ids?.length || 0)} selected · ${esc(item.packed_tokens)}/${esc(item.max_context_tokens)} tokens</span>
      <small>${esc(fmtTime(item.created_at))} · ${esc(item.retrieval_backend_id || 'unknown backend')}</small>
    </button>`).join('');
  host.querySelectorAll('[data-retrieval-id]').forEach((row) => row.addEventListener('click', () => selectRetrieval(row.dataset.retrievalId)));
}

async function selectKnowledge(id) {
  state.selectedKnowledgeId = id;
  state.selectedRetrievalId = '';
  renderRecords();
  const host = document.querySelector('.memory-detail');
  host.innerHTML = '<div class="memory-empty">Loading knowledge detail…</div>';
  try {
    const encoded = encodeURIComponent(id);
    const [detail, versions, relationships] = await Promise.all([
      request(`/api/memory/${encoded}?include_inactive=true`),
      request(`/api/memory/${encoded}/versions`),
      request(`/api/memory/${encoded}/relationships`),
    ]);
    state.detail = detail?.item || null;
    state.versions = versions?.items || [];
    state.relationships = relationships?.items || [];
    renderKnowledgeDetail();
  } catch (error) {
    host.innerHTML = `<div class="memory-error">${esc(error.message || 'Failed to load knowledge')}</div>`;
  }
}

function renderKnowledgeDetail() {
  const host = document.querySelector('.memory-detail');
  const item = state.detail || {};
  const provenance = item.provenance || {};
  host.innerHTML = `
    <section class="memory-card memory-summary">
      <div>
        <h3>${esc(item.title)}</h3>
        <p>${esc(item.summary)}</p>
      </div>
      <div>${badge(item.freshness)} ${badge(item.lifecycle)}</div>
    </section>
    <section class="memory-card memory-grid">
      <div><span>Canonical ID</span><strong>${esc(item.id)}</strong></div>
      <div><span>Logical key</span><strong>${esc(item.logical_key)}</strong></div>
      <div><span>Type / version</span><strong>${esc(item.object_type)} · v${esc(item.version)}</strong></div>
      <div><span>Scope</span><strong>${esc(item.project_id || 'company')}</strong></div>
      <div><span>Classification</span><strong>${esc(item.classification)}</strong></div>
      <div><span>Governance record</span><strong>${esc(item.governance_record_id)}</strong></div>
      <div><span>Source</span><strong>${esc(provenance.source_kind)} · ${esc(provenance.source_ref)}</strong></div>
      <div><span>Source revision</span><strong>${esc(provenance.source_revision || '—')}</strong></div>
      <div><span>Author / observed</span><strong>${esc(provenance.authored_by || '—')} · ${esc(fmtTime(provenance.observed_at))}</strong></div>
      <div><span>Review after</span><strong>${esc(fmtTime(item.review_after))}</strong></div>
      <div><span>Valid until</span><strong>${esc(fmtTime(item.valid_until))}</strong></div>
      <div><span>Retention</span><strong>${esc(fmtTime(item.retention_expires_at))} · ${esc(item.retention_action)}</strong></div>
    </section>
    <section class="memory-card">
      <h4>Canonical content</h4>
      <pre class="memory-content">${esc(item.content ?? '[restricted]')}</pre>
      <p class="memory-muted">Tags: ${esc((item.tags || []).join(', ') || 'none')} · SHA-256: ${esc(item.content_sha256 || '—')}</p>
    </section>
    <section class="memory-card">
      <h4>Versions</h4>
      <div class="memory-version-list">${state.versions.map((version) => `
        <article><strong>v${esc(version.version)} · ${esc(version.lifecycle)}</strong><span>${esc(version.id)}</span><small>${esc(fmtTime(version.updated_at))} · source ${esc(version.provenance?.source_ref || '—')}</small></article>
      `).join('') || '<p class="memory-muted">No versions.</p>'}</div>
    </section>
    <section class="memory-card">
      <h4>Relationships</h4>
      <div class="memory-version-list">${state.relationships.map((rel) => `
        <article><strong>${esc(rel.relationship_type)}</strong><span>${esc(rel.source_knowledge_id)} → ${esc(rel.target_knowledge_id)}</span><small>${esc(rel.note || '')}</small></article>
      `).join('') || '<p class="memory-muted">No relationships.</p>'}</div>
    </section>`;
}

async function selectRetrieval(id) {
  state.selectedRetrievalId = id;
  state.selectedKnowledgeId = '';
  renderRetrievals();
  const host = document.querySelector('.memory-detail');
  host.innerHTML = '<div class="memory-empty">Loading retrieval run…</div>';
  try {
    const payload = await request(`/api/memory/retrievals/${encodeURIComponent(id)}`);
    renderRetrieval(payload?.item || {});
  } catch (error) {
    host.innerHTML = `<div class="memory-error">${esc(error.message || 'Failed to load retrieval')}</div>`;
  }
}

function renderRetrieval(run, liveItems = []) {
  const host = document.querySelector('.memory-detail');
  const selected = run.selected_knowledge_ids || [];
  const scores = run.selected_scores || {};
  const freshness = run.selected_freshness || {};
  const itemById = Object.fromEntries((liveItems || []).map((item) => [item.knowledge_id, item]));
  host.innerHTML = `
    <section class="memory-card memory-summary">
      <div><h3>Retrieval run</h3><p>${esc(run.id)}</p></div>
      <div>${esc(run.packed_tokens || 0)}/${esc(run.max_context_tokens || 0)} tokens</div>
    </section>
    <section class="memory-card memory-grid">
      <div><span>Actor</span><strong>${esc(run.actor_id)}</strong></div>
      <div><span>Query hash</span><strong>${esc(run.query_sha256)}</strong></div>
      <div><span>Candidates</span><strong>${esc(run.candidate_count)}</strong></div>
      <div><span>Top K</span><strong>${esc(run.top_k)}</strong></div>
      <div><span>Backend</span><strong>${esc(run.retrieval_backend_id || '—')}</strong></div>
      <div><span>Index revision</span><strong>${esc(run.retrieval_index_revision || '—')}</strong></div>
      <div><span>Embedding</span><strong>${esc(run.embedding_model_id ? `${run.embedding_provider_id}/${run.embedding_model_id}@${run.embedding_model_revision || '—'}` : 'none')}</strong></div>
      <div><span>Created</span><strong>${esc(fmtTime(run.created_at))}</strong></div>
      <div><span>Filters</span><strong>${esc(JSON.stringify(run.filters || {}))}</strong></div>
    </section>
    <section class="memory-card">
      <h4>Selected knowledge · why it influenced the run</h4>
      <div class="memory-version-list">${selected.map((id) => {
        const item = itemById[id] || {};
        return `<article>
          <strong>[memory:${esc(id)}@v${esc(item.version || '?')}] · score ${esc(scores[id] ?? '—')}</strong>
          <span>${esc(item.title || id)} · ${esc(freshness[id] || item.freshness || 'unknown')}</span>
          <small>${esc((item.reasons || []).join(' · ') || 'Selection reasons are preserved on live retrieval results; historical run stores score/freshness and canonical IDs.')}</small>
        </article>`;
      }).join('') || '<p class="memory-muted">No knowledge selected.</p>'}</div>
    </section>
    <section class="memory-card">
      <h4>Denied candidates</h4>
      <div class="memory-version-list">${(run.denied || []).map((item) => `
        <article><strong>${esc(item.knowledge_id)}</strong><small>${esc(item.reason)}</small></article>
      `).join('') || '<p class="memory-muted">No denied candidates recorded.</p>'}</div>
    </section>`;
}

async function runRetrieval() {
  const query = document.querySelector('.memory-query')?.value?.trim() || '';
  const objectType = document.querySelector('.memory-type')?.value || '';
  const topK = Number(document.querySelector('.memory-top-k')?.value || 8);
  const maxContextTokens = Number(document.querySelector('.memory-budget')?.value || 6000);
  setStatus('Retrieving…');
  try {
    const payload = {
      text: query,
      object_types: objectType ? [objectType] : [],
      budget: {
        top_k: Math.max(1, Math.min(topK, 50)),
        candidate_limit: 100,
        max_context_tokens: Math.max(128, Math.min(maxContextTokens, 32000)),
        progressive: true,
      },
    };
    const result = await request('/api/memory/search', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    state.liveResult = result;
    const runPayload = await request(`/api/memory/retrievals/${encodeURIComponent(result.retrieval_id)}`);
    state.selectedRetrievalId = result.retrieval_id;
    selectTab('retrievals');
    renderRetrieval(runPayload?.item || {}, result.items || []);
    await refreshRetrievalHistory();
    setStatus(`Retrieved ${result.items?.length || 0} objects within ${result.packed_tokens}/${result.max_context_tokens} tokens`);
  } catch (error) {
    setStatus(error.message || 'Retrieval failed', true);
  }
}

async function refreshRetrievalHistory() {
  const retrievals = await request('/api/memory/retrievals?limit=200');
  state.retrievals = retrievals?.items || [];
  renderRetrievals();
}

ensureShell();
