import { request } from './api_client.js';

const companyOps = {
  overview: null,
  tab: 'overview',
  entityDetail: null,
  explain: null,
};

const esc = (value) => String(value ?? '')
  .replaceAll('&', '&amp;')
  .replaceAll('<', '&lt;')
  .replaceAll('>', '&gt;')
  .replaceAll('"', '&quot;')
  .replaceAll("'", '&#039;');

const list = (value) => Array.isArray(value) ? value : [];

function fmtTime(value) {
  if (!value) return '—';
  const date = new Date(Number(value) * 1000);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
}

function badge(value, prefix = '') {
  const text = String(value || 'unknown');
  return `<span class="company-ops-badge ${esc(prefix)} ${esc(text)}">${esc(text)}</span>`;
}

function installCompanyOps() {
  if (document.querySelector('#company-operations-dialog')) return;
  const style = document.createElement('link');
  style.rel = 'stylesheet';
  style.href = '/static/company_operations_ui.css';
  document.head.appendChild(style);

  const button = document.createElement('button');
  button.id = 'company-operations-button';
  button.type = 'button';
  button.className = 'ghost-button';
  button.textContent = 'Company Ops';
  button.title = 'Open canonical Company Operations workspace';
  (document.querySelector('.topbar .controls') || document.body).prepend(button);

  const dialog = document.createElement('dialog');
  dialog.id = 'company-operations-dialog';
  dialog.className = 'company-ops-dialog';
  dialog.innerHTML = `
    <div class="company-ops-shell">
      <header class="company-ops-header">
        <div>
          <h2>Company Operations</h2>
          <p>Canonical business state, provider synchronization, KPI health and explainable consequences.</p>
        </div>
        <button type="button" class="icon-button company-ops-close" aria-label="Close">×</button>
      </header>
      <div class="company-ops-toolbar">
        <button type="button" class="ghost-button company-ops-refresh">Refresh</button>
        <span class="company-ops-status" aria-live="polite"></span>
      </div>
      <nav class="company-ops-tabs" aria-label="Company Operations sections">
        ${['overview','entities','sources','kpis','executive','actions'].map((tab) =>
          `<button type="button" class="company-ops-tab" data-company-tab="${tab}" aria-selected="false">${tab[0].toUpperCase()+tab.slice(1)}</button>`
        ).join('')}
      </nav>
      <main class="company-ops-content"><div class="company-ops-empty">Load canonical company state.</div></main>
    </div>`;
  document.body.appendChild(dialog);

  button.addEventListener('click', async () => {
    dialog.showModal();
    await loadOverview();
  });
  dialog.querySelector('.company-ops-close').addEventListener('click', () => dialog.close());
  dialog.querySelector('.company-ops-refresh').addEventListener('click', loadOverview);
  dialog.querySelectorAll('[data-company-tab]').forEach((tab) => {
    tab.addEventListener('click', () => {
      companyOps.tab = tab.dataset.companyTab;
      companyOps.entityDetail = null;
      companyOps.explain = null;
      renderCompanyOps();
    });
  });
}

function setStatus(message, error = false) {
  const node = document.querySelector('.company-ops-status');
  if (!node) return;
  node.textContent = message || '';
  node.classList.toggle('error', Boolean(error));
}

async function loadOverview() {
  setStatus('Loading canonical company state…');
  try {
    const payload = await request('/api/company-operations/overview');
    companyOps.overview = payload?.item || null;
    renderCompanyOps();
    setStatus('Up to date · deterministic projection');
  } catch (error) {
    setStatus(error.message || 'Company Operations unavailable', true);
  }
}

function renderCompanyOps() {
  const host = document.querySelector('.company-ops-content');
  if (!host) return;
  document.querySelectorAll('[data-company-tab]').forEach((tab) => {
    const selected = tab.dataset.companyTab === companyOps.tab;
    tab.classList.toggle('selected', selected);
    tab.setAttribute('aria-selected', String(selected));
  });
  if (!companyOps.overview) {
    host.innerHTML = '<div class="company-ops-empty">No Company Operations projection loaded.</div>';
    return;
  }
  const renderers = {
    overview: renderOverview,
    entities: renderEntities,
    sources: renderSources,
    kpis: renderKpis,
    executive: renderExecutive,
    actions: renderActions,
  };
  host.innerHTML = renderers[companyOps.tab]();
  bindCompanyOps(host);
}

function renderOverview() {
  const o = companyOps.overview;
  const c = o.counts || {};
  return `
    <section class="company-ops-health ${esc(o.overall_health)}">
      <div><span>Company state</span><strong>${esc(String(o.overall_health || 'unknown').toUpperCase())}</strong></div>
      <div><span>Domains</span><strong>${esc(list(o.business_domains).join(', ') || 'not configured')}</strong></div>
      <div><span>Evaluated</span><strong>${esc(fmtTime(o.evaluated_at))}</strong></div>
    </section>
    <section class="company-ops-counts">
      ${[
        ['Entities', c.business_entities],
        ['Sources', c.sources],
        ['KPIs', c.kpis],
        ['Goals', c.goals],
        ['Decisions', c.decisions],
        ['Executive', c.executive_activations],
        ['Attention', c.attention_items],
        ['Approvals', c.pending_approvals],
        ['Actions', c.action_intents],
      ].map(([label,value]) => `<article><span>${esc(label)}</span><strong>${esc(value ?? 0)}</strong></article>`).join('')}
    </section>
    <section class="company-ops-card">
      <h3>Current blockers & reconciliation signals</h3>
      ${list(o.blockers).length
        ? `<ul class="company-ops-list warning">${list(o.blockers).map((v)=>`<li>${esc(v)}</li>`).join('')}</ul>`
        : '<p class="company-ops-muted">No current blockers reported by canonical services.</p>'}
    </section>
    <section class="company-ops-grid">
      <article class="company-ops-card">
        <h3>Source health</h3>
        ${list(o.sources).map((s)=>`<div class="company-ops-line"><strong>${esc(s.name)}</strong>${badge(s.health)}<small>${esc(s.capacity_status || 'capacity unknown')} · last success ${esc(fmtTime(s.last_success_at))}</small></div>`).join('') || '<p class="company-ops-muted">No business-data sources.</p>'}
      </article>
      <article class="company-ops-card">
        <h3>Human attention</h3>
        ${list(o.attention_items).slice(0,8).map((a)=>`<div class="company-ops-line"><strong>${esc(a.reason)}</strong>${badge(a.severity)}<small>${esc(a.type)} · ${esc(a.status)}</small></div>`).join('') || '<p class="company-ops-muted">No active attention items.</p>'}
        <button type="button" class="ghost-button" data-company-open="attention">Open Inbox</button>
      </article>
    </section>`;
}

function renderEntities() {
  const o = companyOps.overview;
  if (companyOps.entityDetail) return renderEntityDetail();
  return `
    <section class="company-ops-card">
      <h3>Business entities</h3>
      <p class="company-ops-muted">Canonical entities are codex-web state. Provider records remain separately labeled external references.</p>
      <div class="company-ops-entity-list">
        ${list(o.business_entities).map((entity)=>`
          <button type="button" class="company-ops-entity" data-company-entity="${esc(entity.id)}">
            <strong>${esc(entity.name)}</strong>
            <span>${esc(entity.entity_type)} · ${esc(entity.lifecycle)}</span>
            <small>${esc(entity.classification)} · ${esc(entity.id)}</small>
          </button>`).join('') || '<p class="company-ops-muted">No business entities.</p>'}
      </div>
    </section>
    <section class="company-ops-card">
      <h3>Fact health</h3>
      <div class="company-ops-table-wrap"><table class="company-ops-table">
        <thead><tr><th>Entity</th><th>Fact</th><th>Current value</th><th>Freshness</th><th>Source</th><th>Conflict</th></tr></thead>
        <tbody>${list(o.fact_diagnostics).map((f)=>`<tr>
          <td>${esc(f.entity_name)}</td><td>${esc(f.fact_key)}</td><td>${esc(f.selected_value ?? '—')} ${esc(f.unit || '')}</td>
          <td>${badge(f.freshness)}</td><td>${esc(f.provider || '—')}</td><td>${f.conflict ? badge('conflict') : 'no'}</td>
        </tr>`).join('') || '<tr><td colspan="6">No CompanyFacts.</td></tr>'}</tbody>
      </table></div>
    </section>`;
}

function renderEntityDetail() {
  const d = companyOps.entityDetail;
  const entity = d.entity || {};
  return `
    <section class="company-ops-card">
      <div class="company-ops-title-row"><div><small>Canonical BusinessEntity</small><h3>${esc(entity.name)}</h3></div><button type="button" class="ghost-button" data-company-back-entities>Back</button></div>
      <dl class="company-ops-dl"><dt>ID</dt><dd>${esc(entity.id)}</dd><dt>Type</dt><dd>${esc(entity.entity_type)}</dd><dt>Classification</dt><dd>${esc(entity.classification)}</dd><dt>Lifecycle</dt><dd>${esc(entity.lifecycle)}</dd></dl>
    </section>
    <section class="company-ops-grid">
      <article class="company-ops-card canonical">
        <h3>Canonical CompanyFacts</h3>
        ${list(d.facts).map((f)=>`<div class="company-ops-record"><strong>${esc(f.key)} = ${esc(f.value ?? '—')} ${esc(f.unit || '')}</strong><small>${esc(f.lifecycle)} · ${esc(f.classification)} · ${esc(f.source?.authority || '')}</small><code>${esc(f.id)}</code></div>`).join('') || '<p class="company-ops-muted">No facts.</p>'}
      </article>
      <article class="company-ops-card external">
        <h3>External provider references</h3>
        ${list(d.external_records).map((r)=>`<div class="company-ops-record"><strong>${esc(r.provider)} / ${esc(r.object_type)}</strong><small>${esc(r.lifecycle)} · synced ${esc(fmtTime(r.synced_at))}</small><code>${esc(r.external_id)}</code>${r.external_url ? `<a target="_blank" rel="noreferrer" href="${esc(r.external_url)}">Open provider record</a>` : ''}</div>`).join('') || '<p class="company-ops-muted">No provider references.</p>'}
      </article>
    </section>`;
}

function renderSources() {
  const o = companyOps.overview;
  return `
    <section class="company-ops-card">
      <h3>BusinessDataSource administration & diagnostics</h3>
      <p class="company-ops-muted">Controls call the canonical synchronization API. No sync cursor or provider health is computed in the browser.</p>
      <div class="company-ops-source-grid">
        ${list(o.sources).map((s)=>`
          <article class="company-ops-source ${esc(s.health)}" data-company-source-card="${esc(s.source_id)}">
            <header><div><strong>${esc(s.name)}</strong><small>${esc(s.source_type)} · ${esc(s.provider_id)}</small></div>${badge(s.health)}</header>
            <dl>
              <dt>Status</dt><dd>${esc(s.status)}</dd>
              <dt>Adapter capabilities</dt><dd>${esc(list(s.capabilities).join(', ') || 'none')}</dd>
              <dt>Extension</dt><dd>${esc(s.extension_id ? `${s.extension_id}@${s.extension_version || '?'} · ${s.extension_lifecycle || 'unknown'} · ${s.extension_health || 'health unknown'}` : (s.extension_installation_id || 'built-in / not linked'))}</dd>
              <dt>Requested grants</dt><dd>${esc(list(s.extension_requested_capabilities).join(', ') || 'none')}</dd>
              <dt>Active grants</dt><dd>${esc(list(s.extension_granted_capabilities).join(', ') || 'none')}</dd>
              <dt>Config records</dt><dd>${esc(list(s.extension_configuration_record_ids).join(', ') || 'none')}</dd>
              <dt>Credential ref</dt><dd>${esc(s.credential_ref || 'not configured')}</dd>
              <dt>Cursor</dt><dd>${esc(s.cursor || '—')}</dd><dt>Checkpoint</dt><dd>${esc(s.checkpoint || '—')}</dd>
              <dt>Capacity</dt><dd>${esc(s.capacity_status || 'unknown')}${s.retry_at ? ` until ${esc(fmtTime(s.retry_at))}` : ''}</dd>
              <dt>Last success</dt><dd>${esc(fmtTime(s.last_success_at))}</dd>
              <dt>Drift</dt><dd>${esc(s.drift_count)} record(s) · ${esc(s.conflict_count)} conflict(s)</dd>
            </dl>
            ${list(s.issues).length ? `<ul class="company-ops-list warning">${list(s.issues).map((v)=>`<li>${esc(v)}</li>`).join('')}</ul>` : ''}
            <div class="company-ops-actions">
              <button type="button" class="ghost-button" data-company-sync="${esc(s.source_id)}">Sync ≤5 pages</button>
              <button type="button" class="ghost-button" data-company-resync="${esc(s.source_id)}">Bounded full reconcile</button>
              <button type="button" class="ghost-button" data-company-source-status="paused" data-source-id="${esc(s.source_id)}">Pause</button>
              <button type="button" class="ghost-button" data-company-source-status="quarantined" data-source-id="${esc(s.source_id)}">Quarantine</button>
              <button type="button" class="ghost-button" data-company-source-status="active" data-source-id="${esc(s.source_id)}">Activate</button>
            </div>
          </article>`).join('') || '<p class="company-ops-muted">No BusinessDataSources configured.</p>'}
      </div>
    </section>`;
}

function renderKpis() {
  const view = companyOps.overview.kpi_view || {};
  return `
    <section class="company-ops-health ${view.current ? 'healthy' : 'degraded'}">
      <div><span>Operating KPI state</span><strong>${view.current ? 'CURRENT' : 'NOT CURRENT'}</strong></div>
      <div><span>Blockers</span><strong>${esc(list(view.blockers).length)}</strong></div>
      <button type="button" class="ghost-button" data-company-open="kpis">Open detailed Operating view</button>
    </section>
    <section class="company-ops-grid">
      ${list(view.items).map((k)=>`<article class="company-ops-card"><div class="company-ops-title-row"><div><small>${esc(k.domain)}</small><h3>${esc(k.name)}</h3></div>${badge(k.readiness)}</div><strong class="company-ops-big">${esc(k.value ?? '—')} ${esc(k.currency || k.unit || '')}</strong><small>KPI r${esc(k.kpi_revision)} · Metric r${esc(k.metric_revision)} · ${esc(k.freshness)}</small><p class="company-ops-muted">${esc(list(k.readiness_reasons).join(' · ') || 'Current canonical Metric state')}</p></article>`).join('') || '<div class="company-ops-card">No KPI definitions.</div>'}
    </section>`;
}

function renderExecutive() {
  if (companyOps.explain?.subject_type === 'executive_activation') return renderExplain();
  const rows = list(companyOps.overview.executive_activations);
  return `
    <section class="company-ops-card">
      <div class="company-ops-title-row"><div><h3>Active Executive work</h3><p class="company-ops-muted">Recommendations remain advisory until canonical materialization and action paths authorize consequences.</p></div><button type="button" class="ghost-button" data-company-open="executive">Open Organization workspace</button></div>
      ${rows.map((a)=>`<article class="company-ops-record"><div class="company-ops-title-row"><strong>${esc(a.subject)}</strong>${badge(a.status)}</div><small>${esc(a.trigger_kind)} · roles ${esc(list(a.selections).map((s)=>s.role_id).join(', '))}</small><button type="button" class="ghost-button" data-company-explain-exec="${esc(a.id)}">Explain source → consequence</button></article>`).join('') || '<p class="company-ops-muted">No active Executive activations.</p>'}
    </section>`;
}

function renderActions() {
  if (companyOps.explain?.subject_type === 'action_intent') return renderExplain();
  const approvals = list(companyOps.overview.approval_requests);
  const intents = list(companyOps.overview.action_intents);
  return `
    <section class="company-ops-grid">
      <article class="company-ops-card">
        <h3>Pending approvals</h3>
        ${approvals.map((a)=>`<div class="company-ops-record"><div class="company-ops-title-row"><strong>${esc(a.target?.operation)}</strong>${badge(a.status)}</div><small>${esc(a.target?.object_type)} / ${esc(a.target?.object_id)}</small><small>resources: ${esc(list(a.target?.resource_ids).join(', ') || 'none')} · fingerprint ${esc(a.target_fingerprint)}</small></div>`).join('') || '<p class="company-ops-muted">No pending approvals.</p>'}
      </article>
      <article class="company-ops-card">
        <h3>Canonical ActionIntents</h3>
        ${intents.map((i)=>`<div class="company-ops-record"><div class="company-ops-title-row"><strong>${esc(i.action_definition?.title || i.action_id)}</strong>${badge(i.status)}</div><small>${esc(i.provider_type)} · risk ${esc(i.action_definition?.risk_class)} · resources ${esc(list(i.resource_ids).join(', ') || 'none')}</small><button type="button" class="ghost-button" data-company-explain-action="${esc(i.id)}">Explain action chain</button></div>`).join('') || '<p class="company-ops-muted">No active ActionIntents.</p>'}
      </article>
    </section>`;
}

function renderExplain() {
  const x = companyOps.explain || {};
  return `
    <section class="company-ops-card">
      <div class="company-ops-title-row"><div><small>Deterministic explain chain</small><h3>${esc(x.subject_type)} · ${esc(x.subject_id)}</h3></div><button type="button" class="ghost-button" data-company-clear-explain>Back</button></div>
      ${list(x.unresolved).length ? `<ul class="company-ops-list warning">${list(x.unresolved).map((v)=>`<li>${esc(v)}</li>`).join('')}</ul>` : ''}
      <div class="company-ops-timeline">
        ${list(x.stages).map((stage, index)=>`<article><span class="company-ops-step">${index+1}</span><div><div class="company-ops-title-row"><strong>${esc(stage.label)}</strong>${stage.status ? badge(stage.status) : ''}</div><small>${esc(stage.kind)} · ${esc(stage.object_id || 'no canonical id')}</small><p>${esc(stage.summary || '')}</p>${list(stage.refs).length ? `<code>${esc(list(stage.refs).join(' · '))}</code>` : ''}${stage.kind === 'action_intent' ? `<div class="company-ops-impact"><strong>Blast radius</strong><span>risk ${esc(stage.details?.risk_class)} · resources ${esc(list(stage.details?.resource_ids).join(', ') || 'none')} · authority ${esc(stage.details?.required_authority_level)}</span></div>` : ''}</div></article>`).join('') || '<p class="company-ops-muted">No chain stages.</p>'}
      </div>
    </section>`;
}

async function loadEntity(entityId) {
  setStatus('Loading canonical entity provenance…');
  try {
    const payload = await request(`/api/business-context/entities/${encodeURIComponent(entityId)}/context`);
    companyOps.entityDetail = payload;
    renderCompanyOps();
    setStatus('Entity provenance loaded');
  } catch (error) {
    setStatus(error.message || 'Failed to load entity', true);
  }
}

async function syncSource(sourceId, full = false) {
  setStatus(full ? 'Running bounded full reconcile…' : 'Synchronizing source…');
  try {
    await request(`/api/business-data-sources/${encodeURIComponent(sourceId)}/sync?max_pages=5&full_resync=${full ? 'true' : 'false'}`, { method: 'POST' });
    await loadOverview();
  } catch (error) {
    setStatus(error.message || 'Source synchronization failed', true);
  }
}

async function setSourceStatus(sourceId, status) {
  setStatus(`Setting source ${status}…`);
  try {
    await request(`/api/business-data-sources/${encodeURIComponent(sourceId)}/status`, {
      method: 'POST',
      body: JSON.stringify({ status }),
    });
    await loadOverview();
  } catch (error) {
    setStatus(error.message || 'Source status update failed', true);
  }
}

async function explain(path) {
  setStatus('Building deterministic consequence chain…');
  try {
    const payload = await request(path);
    companyOps.explain = payload?.item || null;
    renderCompanyOps();
    setStatus('Explain chain loaded');
  } catch (error) {
    setStatus(error.message || 'Explain chain failed', true);
  }
}

function openExisting(kind) {
  if (kind === 'attention') document.querySelector('[data-attention-launch]')?.click();
  if (kind === 'kpis') document.querySelector('#business-kpi-button')?.click();
  if (kind === 'executive') document.querySelector('#executive-management-button')?.click();
}

function bindCompanyOps(host) {
  host.querySelectorAll('[data-company-entity]').forEach((b)=>b.addEventListener('click',()=>loadEntity(b.dataset.companyEntity)));
  host.querySelector('[data-company-back-entities]')?.addEventListener('click',()=>{ companyOps.entityDetail=null; renderCompanyOps(); });
  host.querySelectorAll('[data-company-sync]').forEach((b)=>b.addEventListener('click',()=>syncSource(b.dataset.companySync,false)));
  host.querySelectorAll('[data-company-resync]').forEach((b)=>b.addEventListener('click',()=>syncSource(b.dataset.companyResync,true)));
  host.querySelectorAll('[data-company-source-status]').forEach((b)=>b.addEventListener('click',()=>setSourceStatus(b.dataset.sourceId,b.dataset.companySourceStatus)));
  host.querySelectorAll('[data-company-open]').forEach((b)=>b.addEventListener('click',()=>openExisting(b.dataset.companyOpen)));
  host.querySelectorAll('[data-company-explain-exec]').forEach((b)=>b.addEventListener('click',()=>explain(`/api/company-operations/explain/executive/${encodeURIComponent(b.dataset.companyExplainExec)}`)));
  host.querySelectorAll('[data-company-explain-action]').forEach((b)=>b.addEventListener('click',()=>explain(`/api/company-operations/explain/action-intent/${encodeURIComponent(b.dataset.companyExplainAction)}`)));
  host.querySelector('[data-company-clear-explain]')?.addEventListener('click',()=>{ companyOps.explain=null; renderCompanyOps(); });
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', installCompanyOps, { once: true });
} else {
  installCompanyOps();
}
