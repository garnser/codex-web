import { request } from './api_client.js';

const state = {
  projects: [],
  catalog: { items: [], sync: {} },
  projectId: '',
  items: [],
  selectedRef: '',
};

const esc = (value) => String(value ?? '')
  .replaceAll('&', '&amp;')
  .replaceAll('<', '&lt;')
  .replaceAll('>', '&gt;')
  .replaceAll('"', '&quot;')
  .replaceAll("'", '&#039;');

function pathRef(ref) {
  return String(ref || '').split('/').map((part) => encodeURIComponent(part)).join('/');
}

function fmtTime(value) {
  if (!value) return '—';
  const date = new Date(Number(value) * 1000);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
}

function ensureShell() {
  if (document.querySelector('#work-items-dialog')) return;

  const link = document.createElement('link');
  link.rel = 'stylesheet';
  link.href = 'static/work_items_ui.css';
  document.head.appendChild(link);

  const button = document.createElement('button');
  button.id = 'work-items-button';
  button.type = 'button';
  button.className = 'ghost-button';
  button.textContent = 'Work Items';
  button.title = 'Open canonical work-item operator';
  (document.querySelector('.topbar .controls') || document.body).prepend(button);

  const dialog = document.createElement('dialog');
  dialog.id = 'work-items-dialog';
  dialog.className = 'work-items-dialog';
  dialog.innerHTML = `
    <div class="work-items-shell">
      <header class="work-items-header">
        <div>
          <h2>Work-item operator</h2>
          <p>Canonical state, authoritative source, execution contract, diagnostics and history.</p>
        </div>
        <button type="button" class="icon-button work-items-close" aria-label="Close">×</button>
      </header>
      <div class="work-items-toolbar">
        <label>Project <select class="work-items-project"></select></label>
        <button type="button" class="ghost-button work-items-refresh">Refresh</button>
        <button type="button" class="ghost-button work-items-sync">Resync source</button>
        <span class="work-items-status" aria-live="polite"></span>
      </div>
      <details class="work-source-config" open>
        <summary>Authoritative task source</summary>
        <div class="work-source-grid">
          <label>Type <input class="work-source-type" value="gitlab" /></label>
          <label>Instance <input class="work-source-instance" placeholder="https://gitlab.example/api/v4" /></label>
          <label>Scope <input class="work-source-scope" placeholder="group/project or group" /></label>
        </div>
        <div class="work-source-actions">
          <button type="button" class="primary-button work-source-save">Save canonical source</button>
          <button type="button" class="ghost-button work-source-clear">Clear source</button>
          <small class="work-source-capabilities"></small>
        </div>
      </details>
      <div class="work-items-layout">
        <aside class="work-items-list" aria-label="Work items"></aside>
        <main class="work-item-detail">
          <div class="work-item-empty">Select a work item to inspect its canonical state.</div>
        </main>
      </div>
    </div>`;
  document.body.appendChild(dialog);

  button.addEventListener('click', async () => {
    dialog.showModal();
    await refreshAll();
  });
  dialog.querySelector('.work-items-close').addEventListener('click', () => dialog.close());
  dialog.querySelector('.work-items-refresh').addEventListener('click', refreshAll);
  dialog.querySelector('.work-items-project').addEventListener('change', async (event) => {
    state.projectId = event.target.value;
    state.selectedRef = '';
    renderSourceConfig();
    await loadItems();
  });
  dialog.querySelector('.work-source-save').addEventListener('click', saveSource);
  dialog.querySelector('.work-source-clear').addEventListener('click', clearSource);
  dialog.querySelector('.work-items-sync').addEventListener('click', syncSource);
}

function setStatus(message, isError = false) {
  const target = document.querySelector('.work-items-status');
  if (!target) return;
  target.textContent = message || '';
  target.classList.toggle('error', Boolean(isError));
}

async function refreshAll() {
  setStatus('Loading…');
  try {
    const [projects, catalog] = await Promise.all([
      request('/api/projects'),
      request('/api/task-sources'),
    ]);
    state.projects = Array.isArray(projects) ? projects : [];
    state.catalog = catalog || { items: [], sync: {} };
    if (!state.projectId || !state.projects.some((project) => project.id === state.projectId)) {
      state.projectId = state.projects[0]?.id || '';
    }
    renderProjectSelect();
    renderSourceConfig();
    await loadItems();
    setStatus('Up to date');
  } catch (error) {
    setStatus(error.message || 'Failed to load operator state', true);
  }
}

function renderProjectSelect() {
  const select = document.querySelector('.work-items-project');
  if (!select) return;
  select.innerHTML = state.projects.map((project) => (
    `<option value="${esc(project.id)}" ${project.id === state.projectId ? 'selected' : ''}>${esc(project.name)}</option>`
  )).join('');
}

function selectedProject() {
  return state.projects.find((project) => project.id === state.projectId) || null;
}

function catalogEntry(sourceType) {
  const entries = Array.isArray(state.catalog?.items) ? state.catalog.items : [];
  return entries.find((entry) => entry.project_id === state.projectId)
    || entries.find((entry) => entry.source_type === sourceType)
    || null;
}

function renderSourceConfig() {
  const project = selectedProject();
  const config = project?.authoritative_task_source || null;
  const type = config?.source_type || 'gitlab';
  const entry = catalogEntry(type);
  const typeInput = document.querySelector('.work-source-type');
  const instanceInput = document.querySelector('.work-source-instance');
  const scopeInput = document.querySelector('.work-source-scope');
  if (!typeInput || !instanceInput || !scopeInput) return;
  typeInput.value = type;
  instanceInput.value = config?.source_instance || entry?.source_instance || '';
  scopeInput.value = config?.scope || '';
  const caps = entry?.capabilities || [];
  const sync = state.catalog?.sync || {};
  document.querySelector('.work-source-capabilities').textContent = [
    caps.length ? `Capabilities: ${caps.join(', ')}` : 'Capabilities unavailable until the adapter is connected',
    sync.last_success_at ? `Last sync: ${fmtTime(sync.last_success_at)}` : 'No successful sync recorded',
    sync.last_error ? `Last error: ${sync.last_error}` : '',
  ].filter(Boolean).join(' · ');
}

async function saveSource() {
  if (!state.projectId) return;
  const source_type = document.querySelector('.work-source-type').value.trim();
  const source_instance = document.querySelector('.work-source-instance').value.trim();
  const scope = document.querySelector('.work-source-scope').value.trim();
  if (!source_type || !source_instance || !scope) {
    setStatus('Source type, instance and scope are required', true);
    return;
  }
  setStatus('Saving source…');
  try {
    await request(`/api/projects/${encodeURIComponent(state.projectId)}/task-source`, {
      method: 'PUT',
      body: JSON.stringify({ source_type, source_instance, scope }),
    });
    await refreshAll();
    setStatus('Authoritative source saved');
  } catch (error) {
    setStatus(error.message || 'Failed to save source', true);
  }
}

async function clearSource() {
  if (!state.projectId) return;
  setStatus('Clearing source…');
  try {
    await request(`/api/projects/${encodeURIComponent(state.projectId)}/task-source`, { method: 'DELETE' });
    await refreshAll();
    setStatus('Authoritative source cleared');
  } catch (error) {
    setStatus(error.message || 'Failed to clear source', true);
  }
}

async function syncSource() {
  if (!state.projectId) return;
  setStatus('Reconciling authoritative source…');
  try {
    const result = await request(`/api/work-items/sync/${encodeURIComponent(state.projectId)}`, {
      method: 'POST',
      body: JSON.stringify({ actor: 'operator', reason: 'operator requested source resync' }),
    });
    await refreshAll();
    setStatus(`Synced ${result.synced ?? 0} work items`);
  } catch (error) {
    setStatus(error.message || 'Source sync failed', true);
  }
}

async function loadItems() {
  const list = document.querySelector('.work-items-list');
  const detail = document.querySelector('.work-item-detail');
  if (!state.projectId) {
    state.items = [];
    if (list) list.innerHTML = '<p class="work-item-empty">No project available.</p>';
    return;
  }
  const payload = await request(`/api/work-items?project_id=${encodeURIComponent(state.projectId)}`);
  state.items = Array.isArray(payload?.items) ? payload.items : [];
  if (!state.items.some((item) => item.ref === state.selectedRef)) {
    state.selectedRef = state.items[0]?.ref || '';
  }
  if (list) {
    list.innerHTML = state.items.length ? state.items.map((item) => `
      <button type="button" class="work-item-row ${item.ref === state.selectedRef ? 'selected' : ''}" data-ref="${esc(item.ref)}">
        <strong>${esc(item.title || item.ref)}</strong>
        <span>${esc(item.current_stage)} · ${esc(item.current_owner || item.next_owner || 'unowned')}</span>
        ${item.routingError ? `<small class="work-item-warning">${esc(item.routingError)}</small>` : ''}
      </button>`).join('') : '<p class="work-item-empty">No canonical work items for this project.</p>';
    list.querySelectorAll('.work-item-row').forEach((row) => row.addEventListener('click', async () => {
      state.selectedRef = row.dataset.ref;
      await loadItems();
    }));
  }
  if (state.selectedRef) {
    await loadDetail(state.selectedRef);
  } else if (detail) {
    detail.innerHTML = '<div class="work-item-empty">No work item selected.</div>';
  }
}

function keyValueRows(values) {
  return Object.entries(values).map(([key, value]) => `
    <div><span>${esc(key.replaceAll('_', ' '))}</span><strong>${esc(value ?? '—')}</strong></div>`).join('');
}

async function loadDetail(ref) {
  const detail = document.querySelector('.work-item-detail');
  if (!detail) return;
  detail.innerHTML = '<div class="work-item-empty">Loading work-item detail…</div>';
  try {
    const payload = await request(`/api/work-items/${pathRef(ref)}/operator`);
    renderDetail(payload);
  } catch (error) {
    detail.innerHTML = `<div class="work-item-error">${esc(error.message || 'Failed to load work item')}</div>`;
  }
}

function renderDetail(payload) {
  const detail = document.querySelector('.work-item-detail');
  const item = payload.item || {};
  const external = payload.external || {};
  const execution = item.execution || {};
  const retry = execution.retry || {};
  const retryPolicy = retry.policy || {};
  const checkpoint = execution.latest_checkpoint || null;
  const usage = execution.usage || {};
  const contract = payload.execution_contract || {};
  const definitionRefs = Array.isArray(contract.definition_refs) ? contract.definition_refs : [];
  const history = payload.history?.items || [];
  const diagnostics = payload.diagnostics || [];
  const actions = payload.actions || {};
  const identity = external.identity || {};
  const config = external.configuration || {};
  const policy = payload.execution_policy || {};

  detail.innerHTML = `
    <section class="work-item-title">
      <div><small>${esc(item.ref)}</small><h3>${esc(item.title || item.ref)}</h3></div>
      <div class="work-item-action-bar">
        <button type="button" class="ghost-button work-item-retry" ${actions.retry?.allowed ? '' : 'disabled'}>Retry</button>
        <button type="button" class="ghost-button work-item-reconcile" ${actions.reconcile?.allowed ? '' : 'disabled'}>Reconcile</button>
      </div>
    </section>
    <div class="work-detail-columns">
      <section class="work-detail-card canonical-card">
        <h4>Canonical codex-web state</h4>
        <div class="work-kv">${keyValueRows({
          stage: item.current_stage,
          owner: item.current_owner,
          next_owner: item.next_owner,
          artifact: item.artifact_state,
          blocker: item.blocker,
          release_gate: item.release_gate,
          handoff: item.handoff ? `${item.handoff.from_agent} → ${item.handoff.to_agent} (${item.handoff.status})` : '—',
        })}</div>
      </section>
      <section class="work-detail-card external-card">
        <h4>Authoritative external source</h4>
        <div class="work-kv">${keyValueRows({
          type: identity.source_type || config.source_type,
          instance: identity.source_instance || config.source_instance,
          external_id: identity.external_id,
          revision: identity.revision,
          event_cursor: identity.event_cursor,
          projected_status: external.projected_status_label,
          last_projected_event: fmtTime(external.last_projected_event_at),
          adapter: external.available ? 'available' : 'unavailable',
        })}</div>
        <small>${esc((external.capabilities || []).join(', ') || 'No live capabilities reported')}</small>
      </section>
    </div>
    <div class="work-detail-columns">
      <section class="work-detail-card">
        <h4>Execution lifecycle</h4>
        <div class="work-kv">${keyValueRows({
          retry: `${retry.attempt ?? 0}/${retryPolicy.max_attempts ?? 0}`,
          backoff_seconds: retryPolicy.backoff_seconds,
          timeout_seconds: execution.timeout_seconds,
          deadline: fmtTime(execution.deadline_at),
          failure: execution.failure_reason?.message,
          checkpoint: checkpoint?.id,
        })}</div>
        ${checkpoint ? `<div class="checkpoint"><strong>${esc(checkpoint.summary)}</strong><small>Next: ${esc((checkpoint.next_actions || []).join(' · ') || '—')}</small></div>` : ''}
      </section>
      <section class="work-detail-card">
        <h4>Contract & policy</h4>
        <div class="work-kv">${keyValueRows({
          schema: contract.schema_version,
          role: contract.role_id,
          agent: contract.agent_id,
          sandbox: policy.sandbox || contract.permissions?.sandbox,
          approval_policy: policy.approval_policy || contract.permissions?.approval_policy,
        })}</div>
        <small>Definition: ${definitionRefs.length ? definitionRefs.map((ref) => (
          esc(ref.kind) + ':' + esc(ref.definition_id) + '@r' + esc(ref.revision)
            + ' · ' + esc(ref.checksum)
        )).join('<br>') : '—'}</small>
        <small>Success: ${esc((contract.success_criteria || []).join(' · ') || '—')}</small>
        <small>Failure: ${esc((contract.failure_conditions || []).join(' · ') || '—')}</small>
      </section>
    </div>
    <section class="work-detail-card">
      <h4>Usage attribution</h4>
      <div class="work-kv work-kv-wide">${keyValueRows({
        calls: usage.calls,
        input_tokens: usage.input_tokens,
        output_tokens: usage.output_tokens,
        reasoning_tokens: usage.reasoning_tokens,
        estimated_cost_usd: usage.estimated_cost_usd,
        goal_id: usage.goal_id,
        decision_id: usage.decision_id,
      })}</div>
    </section>
    <section class="work-detail-card">
      <h4>Diagnostics</h4>
      <div class="work-diagnostics">${diagnostics.length ? diagnostics.map((entry) => `
        <div class="work-diagnostic ${esc(entry.severity)}"><strong>${esc(entry.kind)}</strong><span>${esc(entry.message)}</span><small>${esc(fmtTime(entry.created_at))}</small></div>`).join('') : '<small>No reconciliation or execution diagnostics.</small>'}</div>
    </section>
    <section class="work-detail-card">
      <h4>Chronological history</h4>
      <div class="work-history">${history.length ? history.slice().reverse().map((event) => `
        <div class="work-history-event">
          <strong>${esc(event.event_type)}</strong>
          <span>${esc(event.actor || 'system')} · ${esc(event.source || 'canonical')}</span>
          <small>${esc(event.reason || '')} ${esc(fmtTime(event.created_at))}</small>
        </div>`).join('') : '<small>No work-item events recorded.</small>'}</div>
    </section>`;

  detail.querySelector('.work-item-retry')?.addEventListener('click', () => runItemAction('retry'));
  detail.querySelector('.work-item-reconcile')?.addEventListener('click', () => runItemAction('reconcile'));
}

async function runItemAction(action) {
  if (!state.selectedRef) return;
  setStatus(`${action === 'retry' ? 'Retrying' : 'Reconciling'} work item…`);
  try {
    const payload = await request(`/api/work-items/${pathRef(state.selectedRef)}/${action}`, {
      method: 'POST',
      body: JSON.stringify({ actor: 'operator', reason: `operator requested ${action}` }),
    });
    renderDetail(payload);
    setStatus(action === 'retry' ? 'Retry dispatched' : 'Reconciled with authoritative source');
    await loadItems();
  } catch (error) {
    setStatus(error.message || `${action} failed`, true);
  }
}

ensureShell();
