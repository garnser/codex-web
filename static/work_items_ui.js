import { createRunTimelineUi } from "./work_item_runs_ui.js";
import { workItemSummaryHtml } from "./work_item_summary_ui.js";
import { request } from './api_client.js';
import { observeRender } from './frontend_perf.js';
import { applyWorkItemSearch, installWorkItemSearch } from './work_items_search_ui.js';
import { installWorkItemsMount, workItemsSurfaceActive } from './work_items_mount_ui.js';
const PAGE_SIZE = 50;
const ROW_WINDOW = 60;
const RUN_PAGE_SIZE = 20;
const WORK_ITEM_PROJECT_KEY='codex-web-work-item-project';
const state={
  projects: [],
  catalog: { items: [], sync: {} },
  projectId: '',
  items: [],
  selectedRef: '',
  secrets: [],
  nextCursor: null,
  hasMore: false,
  windowStart: 0,
  pageError: '',
  listGeneration: 0,
  listController: null,
  detailPayload: null,
  runs: { active: [], items: [], nextCursor: null, hasMore: false, activeTruncated: false },
  runRefreshTimer: null,
};
const esc=(value)=> String(value ?? '')
  .replaceAll('&', '&amp;')
  .replaceAll('<', '&lt;')
  .replaceAll('>', '&gt;')
  .replaceAll('"', '&quot;')
  .replaceAll("'", '&#039;');
function pathRef(ref){return String(ref||'').split('/').map(encodeURIComponent).join('/')}
function fmtTime(value){if(!value)return'—';const d=new Date(Number(value)*1000);return Number.isNaN(d.getTime())?String(value):d.toLocaleString()}
function routedProjectContext(){const m=location.pathname.match(/\/projects\/([^/]+)(?:\/|$)/);if(!m)return'';try{return decodeURIComponent(m[1])}catch{return m[1]}}
function currentProjectContext(){const query=new URLSearchParams(location.search);return document.body?.dataset.activeProject||document.body?.dataset.projectId||routedProjectContext()||query.get('project')||query.get('work_item_project')||sessionStorage.getItem(WORK_ITEM_PROJECT_KEY)||''}
function persistWorkItemProject(projectId){if(!projectId)return;sessionStorage.setItem(WORK_ITEM_PROJECT_KEY,projectId);if(document.body?.dataset.activeProject||routedProjectContext())return;const url=new URL(location.href);url.searchParams.set('work_item_project',projectId);history.replaceState({...history.state,workItemProjectId:projectId},'',url)}
function resetPaging(){state.listController?.abort();state.listController=null;state.listGeneration+=1;state.items=[];state.nextCursor=null;state.hasMore=false;state.windowStart=0;state.pageError=''}
function ensureShell() {
  if (document.querySelector('#work-items-dialog')) return;
  const link = document.createElement('link');
  link.rel = 'stylesheet';
  link.href = 'static/work_items_ui.css';
  document.head.appendChild(link);
  const dialog = document.createElement('dialog');
  dialog.id = 'work-items-dialog';
  dialog.className = 'work-items-dialog';
  dialog.setAttribute('aria-labelledby', 'work-items-dialog-title');
  dialog.innerHTML = `
    <div class="work-items-shell">
      <header class="work-items-header">
        <div>
          <h2 id="work-items-dialog-title">Work-item operator</h2>
          <p>Canonical state, authoritative source, execution contract, diagnostics and history.</p>
        </div>
        <button type="button" class="icon-button work-items-close" aria-label="Close">×</button>
      </header>
      <div class="work-items-toolbar">
        <label>Project <select class="work-items-project"></select></label>
        <label>Search <input class="work-items-search" type="search" placeholder="Search Work Items" autocomplete="off" /></label>
        <button type="button" class="ghost-button work-items-refresh">Refresh</button>
        <button type="button" class="ghost-button work-items-sync">Resync source</button>
        <span class="work-items-status" role="status" aria-live="polite"></span>
      </div>
      <details class="work-source-config" open>
        <summary>Authoritative task source</summary>
        <div class="work-source-grid">
          <label>Type <select class="work-source-type"></select></label>
          <label>Instance <input class="work-source-instance" placeholder="Provider base URL" /></label>
          <label>Scope <input class="work-source-scope" placeholder="Project, queue, or provider scope" /></label>
        </div>
        <div class="work-source-provider-fields" hidden>
          <label class="work-source-secret-row">Credential
            <select class="work-source-secret"></select>
            <small>Canonical SecretReference metadata only; secret values are never loaded here.</small>
          </label>
          <label class="work-source-jira-username-row" hidden>Jira username / account email
            <input class="work-source-jira-username" autocomplete="off" />
          </label>
          <label class="work-source-servicenow-table-row" hidden>ServiceNow table
            <input class="work-source-servicenow-table" value="task" />
          </label>
          <label class="work-source-servicenow-active-row" hidden>ServiceNow active state value
            <input class="work-source-servicenow-active" placeholder="Optional provider state value" />
          </label>
          <label class="work-source-servicenow-closed-row" hidden>ServiceNow closed state value
            <input class="work-source-servicenow-closed" placeholder="Optional provider state value" />
          </label>
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
  installWorkItemsMount(dialog, async () => {
    const contextProject = currentProjectContext();
    if (contextProject) state.projectId = contextProject;
    await refreshAll();
  });
  dialog.querySelector('.work-items-refresh').addEventListener('click', refreshAll);
  installWorkItemSearch(dialog, () => {
    state.selectedRef = '';
    resetPaging();
    void loadItems({ reset: true });
  });
  dialog.querySelector('.work-items-project').addEventListener('change', async (event) => {
    state.projectId = event.target.value;
    state.selectedRef = '';
    persistWorkItemProject(state.projectId);
    resetPaging();
    renderSourceConfig();
    await loadItems({ reset: true });
  });
  dialog.querySelector('.work-source-type').addEventListener('change', (event) => {
    renderProviderFields(event.target.value);
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
function workItemLoadError(error) {
  const code = String(error?.detail?.code || '').toLowerCase();
  const message = error?.message || 'Failed to load Work Items';
  if (error?.status === 504) return `Backend timeout: ${message}`;
  if (
    code.includes('readiness')
    || code.includes('migration')
    || code.includes('bootstrap')
  ) {
    return `Project readiness blocker: ${message}`;
  }
  return message;
}
async function refreshAll() {
  setStatus('Loading…');
  try {
    const [projects, catalog, secretsPayload] = await Promise.all([
      request('/api/projects'),
      request('/api/task-sources'),
      request('/api/secrets').catch(() => ({ items: [] })),
    ]);
    state.projects = Array.isArray(projects) ? projects : [];
    state.catalog = catalog || { items: [], sync: {} };
    state.secrets = Array.isArray(secretsPayload?.items) ? secretsPayload.items : [];
    const contextProject = state.projectId || currentProjectContext();
    if (contextProject && state.projects.some((project) => project.id === contextProject)) {
      state.projectId = contextProject;
    } else if (!state.projectId || !state.projects.some((project) => project.id === state.projectId)) {
      state.projectId = state.projects.length === 1 ? state.projects[0].id : '';
    }
    if (state.projectId) persistWorkItemProject(state.projectId);
    renderProjectSelect();
    renderSourceConfig();
    await loadItems({ reset: true });
    setStatus('Up to date');
  } catch (error) {
    setStatus(error.message || 'Failed to load operator state', true);
  }
}
function renderProjectSelect() {
  const select = document.querySelector('.work-items-project');
  if (!select) return;
  select.innerHTML = [
    '<option value="">Select Project…</option>',
    ...state.projects.map((project) => (
      `<option value="${esc(project.id)}" ${project.id === state.projectId ? 'selected' : ''}>${esc(project.name)}</option>`
    )),
  ].join('');
}
function selectedProject() {
  return state.projects.find((project) => project.id === state.projectId) || null;
}
function catalogEntry(sourceType) {
  const entries = Array.isArray(state.catalog?.items) ? state.catalog.items : [];
  return entries.find((entry) => entry.project_id === state.projectId && entry.source_type === sourceType)
    || entries.find((entry) => entry.project_id == null && entry.source_type === sourceType)
    || entries.find((entry) => entry.source_type === sourceType)
    || null;
}
function renderSecretOptions(selectedId = '') {
  const select = document.querySelector('.work-source-secret');
  if (!select) return;
  const options = state.secrets.map((item) => ({
    id: String(item.id || ''),
    label: item.name || item.label || item.id,
    status: item.status || '',
  })).filter((item) => item.id);
  if (selectedId && !options.some((item) => item.id === selectedId)) {
    options.unshift({ id: selectedId, label: selectedId, status: 'not listed' });
  }
  select.innerHTML = [
    '<option value="">Select a canonical secret…</option>',
    ...options.map((item) => `<option value="${esc(item.id)}" ${item.id === selectedId ? 'selected' : ''}>${esc(item.label)}${item.status ? ` (${esc(item.status)})` : ''}</option>`),
  ].join('');
}
function renderProviderFields(sourceType, config = null) {
  const normalized = String(sourceType || '').toLowerCase();
  const wrapper = document.querySelector('.work-source-provider-fields');
  const secretRow = document.querySelector('.work-source-secret-row');
  const jiraRow = document.querySelector('.work-source-jira-username-row');
  const tableRow = document.querySelector('.work-source-servicenow-table-row');
  const activeRow = document.querySelector('.work-source-servicenow-active-row');
  const closedRow = document.querySelector('.work-source-servicenow-closed-row');
  const isJira = normalized === 'jira';
  const isServiceNow = normalized === 'servicenow';
  const needsCredential = isJira || isServiceNow;
  if (wrapper) wrapper.hidden = !needsCredential;
  if (secretRow) secretRow.hidden = !needsCredential;
  if (jiraRow) jiraRow.hidden = !isJira;
  if (tableRow) tableRow.hidden = !isServiceNow;
  if (activeRow) activeRow.hidden = !isServiceNow;
  if (closedRow) closedRow.hidden = !isServiceNow;
  renderSecretOptions(config?.credential_secret_id || '');
  if (isJira) {
    document.querySelector('.work-source-jira-username').value = config?.provider_settings?.username || '';
  }
  if (isServiceNow) {
    const settings = config?.provider_settings || {};
    const mapping = settings.canonical_state_values || {};
    document.querySelector('.work-source-servicenow-table').value = settings.table || 'task';
    document.querySelector('.work-source-servicenow-active').value = mapping.implementation_active || '';
    document.querySelector('.work-source-servicenow-closed').value = mapping.closed || '';
  }
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
  const sourceTypes = [...new Set([
    'gitlab',
    'jira',
    'servicenow',
    ...(Array.isArray(state.catalog?.items) ? state.catalog.items.map((item) => item.source_type) : []),
    type,
  ].filter(Boolean))];
  typeInput.innerHTML = sourceTypes.map((sourceType) => (
    `<option value="${esc(sourceType)}" ${sourceType === type ? 'selected' : ''}>${esc(sourceType)}</option>`
  )).join('');
  instanceInput.value = config?.source_instance || entry?.source_instance || '';
  scopeInput.value = config?.scope || '';
  renderProviderFields(type, config);
  const caps = entry?.capabilities || [];
  const sync = state.catalog?.sync || {};
  document.querySelector('.work-source-capabilities').textContent = [
    entry ? `Adapter: ${entry.available ? 'ready' : 'not ready'}` : 'Adapter not registered',
    caps.length ? `Capabilities: ${caps.join(', ')}` : 'Capabilities unavailable',
    entry?.error ? `Adapter error: ${entry.error}` : '',
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
  const payload = { source_type, source_instance, scope };
  if (source_type === 'jira' || source_type === 'servicenow') {
    const credential_secret_id = document.querySelector('.work-source-secret').value.trim();
    if (!credential_secret_id) {
      setStatus('A canonical credential SecretReference is required for this provider', true);
      return;
    }
    payload.credential_secret_id = credential_secret_id;
    if (source_type === 'jira') {
      const username = document.querySelector('.work-source-jira-username').value.trim();
      payload.provider_settings = { kind: 'jira' };
      if (username) payload.provider_settings.username = username;
    } else {
      const table = document.querySelector('.work-source-servicenow-table').value.trim() || 'task';
      const active = document.querySelector('.work-source-servicenow-active').value.trim();
      const closed = document.querySelector('.work-source-servicenow-closed').value.trim();
      const canonical_state_values = {};
      if (active) canonical_state_values.implementation_active = active;
      if (closed) canonical_state_values.closed = closed;
      payload.provider_settings = { kind: 'servicenow', table, canonical_state_values };
    }
  }
  setStatus('Saving source…');
  try {
    await request(`/api/projects/${encodeURIComponent(state.projectId)}/task-source`, {
      method: 'PUT',
      body: JSON.stringify(payload),
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
function renderItemList() {
  const startedAt = performance.now();
  const list = document.querySelector('.work-items-list');
  if (!list) return;
  if (!state.items.length) {
    if (state.pageError) {
      list.innerHTML = `
        <div class="work-item-empty work-item-error">
          <strong>Work Items could not be loaded.</strong>
          <span>${esc(state.pageError)}</span>
          <button type="button" class="ghost-button work-items-retry-page">Retry</button>
        </div>`;
      list.querySelector('.work-items-retry-page')?.addEventListener(
        'click',
        () => loadItems({ reset: true }),
      );
      return;
    }
    list.innerHTML = '<p class="work-item-empty">No canonical work items for this project.</p>';
    return;
  }
  const maxStart = Math.max(0, state.items.length - ROW_WINDOW);
  state.windowStart = Math.min(Math.max(0, state.windowStart), maxStart);
  const visible = state.items.slice(
    state.windowStart,
    state.windowStart + ROW_WINDOW,
  );
  const first = state.windowStart + 1;
  const last = state.windowStart + visible.length;
  const controls = `
    <div class="work-items-window-controls">
      <button type="button" class="ghost-button work-items-window-prev" ${state.windowStart <= 0 ? 'disabled' : ''}>Previous rows</button>
      <small>Rows ${first}–${last} of ${state.items.length} loaded</small>
      <button type="button" class="ghost-button work-items-window-next" ${last >= state.items.length ? 'disabled' : ''}>Next rows</button>
      <button type="button" class="ghost-button work-items-load-more" ${state.hasMore ? '' : 'disabled'}>${state.pageError ? 'Retry next page' : (state.hasMore ? 'Load more' : 'All loaded')}</button>
    </div>`;
  list.innerHTML = visible.map((item) => `
    <button type="button" class="work-item-row ${item.ref === state.selectedRef ? 'selected' : ''}" data-ref="${esc(item.ref)}">
      <strong>${esc(item.title || item.ref)}</strong>
      <span>${esc(item.current_stage)} · ${esc(item.current_owner || item.next_owner || 'unowned')}</span>
      ${item.routingError ? `<small class="work-item-warning">${esc(item.routingError)}</small>` : ''}
    </button>`).join('') + controls;
  list.querySelectorAll('.work-item-row').forEach((row) => row.addEventListener('click', async () => {
    state.selectedRef = row.dataset.ref;
    renderItemList();
    await loadDetail(state.selectedRef);
  }));
  list.querySelector('.work-items-window-prev')?.addEventListener('click', () => {
    state.windowStart = Math.max(0, state.windowStart - ROW_WINDOW);
    renderItemList();
  });
  list.querySelector('.work-items-window-next')?.addEventListener('click', () => {
    state.windowStart = Math.min(
      Math.max(0, state.items.length - ROW_WINDOW),
      state.windowStart + ROW_WINDOW,
    );
    renderItemList();
  });
  list.querySelector('.work-items-load-more')?.addEventListener('click', async () => {
    await loadItems({ reset: false });
    if (state.items.length > last) {
      state.windowStart = Math.max(0, state.items.length - ROW_WINDOW);
      renderItemList();
    }
  });
  observeRender('work-items', startedAt, {
    rows: visible.length,
    nodes: list.querySelectorAll('.work-item-row').length,
  });
}
async function loadItems({ reset = false } = {}) {
  const list = document.querySelector('.work-items-list');
  const detail = document.querySelector('.work-item-detail');
  if (!state.projectId) {
    resetPaging();
    if (list) list.innerHTML = '<p class="work-item-empty">Select a Project to view Work Items.</p>';
    if (detail) detail.innerHTML = '<div class="work-item-empty">Select a Project to inspect its Work Items.</div>';
    setStatus('Select a Project');
    return;
  }
  if (reset) resetPaging();
  if (!reset && !state.hasMore && state.items.length) {
    renderItemList();
    return;
  }
  const generation = state.listGeneration;
  const controller = new AbortController();
  state.listController?.abort();
  state.listController = controller;
  state.pageError = '';
  setStatus(state.items.length ? 'Loading more Work Items…' : 'Loading Work Items…');
  const query = new URLSearchParams({
    project_id: state.projectId,
    limit: String(PAGE_SIZE),
  });
  applyWorkItemSearch(query);
  if (!reset && state.nextCursor) query.set('cursor', state.nextCursor);
  try {
    const payload = await request(`/api/work-items?${query}`, {
      signal: controller.signal,
    });
    if (generation !== state.listGeneration || controller.signal.aborted) return;
    const page = Array.isArray(payload?.items) ? payload.items : [];
    const byRef = new Map(state.items.map((item) => [item.ref, item]));
    page.forEach((item) => {
      if (item?.ref) byRef.set(item.ref, item);
    });
    state.items = [...byRef.values()];
    state.nextCursor = payload?.nextCursor || null;
    state.hasMore = Boolean(payload?.hasMore && state.nextCursor);
    state.pageError = '';
    if (!state.items.some((item) => item.ref === state.selectedRef)) {
      state.selectedRef = state.items[0]?.ref || '';
    }
    renderItemList();
    if (reset && state.selectedRef) {
      await loadDetail(state.selectedRef);
    } else if (!state.selectedRef && detail) {
      detail.innerHTML = '<div class="work-item-empty">No work item selected.</div>';
    }
    setStatus(
      state.hasMore
        ? `${state.items.length} Work Items loaded · more available`
        : `${state.items.length} Work Items loaded`,
    );
  } catch (error) {
    if (error?.name === 'AbortError' || controller.signal.aborted) return;
    if (generation !== state.listGeneration) return;
    state.pageError = workItemLoadError(error);
    renderItemList();
    setStatus(state.pageError, true);
  } finally {
    if (state.listController === controller) state.listController = null;
  }
}
function keyValueRows(values){return Object.entries(values).map(([key,value])=>`<div><span>${esc(key.replaceAll('_',' '))}</span><strong>${esc(value??'—')}</strong></div>`).join('');}
const runUi = createRunTimelineUi({ state, request, esc, fmtTime, pathRef, setStatus, pageSize: RUN_PAGE_SIZE });
async function loadDetail(ref) {
  const detail = document.querySelector('.work-item-detail');
  if (!detail) return;
  detail.innerHTML = '<div class="work-item-empty">Loading work-item detail…</div>';
  try {
    const query = new URLSearchParams({ limit: String(RUN_PAGE_SIZE) });
    const [payload, runs] = await Promise.all([
      request(`/api/work-items/${pathRef(ref)}/operator`),
      request(`/api/work-items/${pathRef(ref)}/runs?${query}`).catch(() => ({ active: [], items: [], nextCursor: null, hasMore: false, activeTruncated: false })),
    ]);
    if (ref !== state.selectedRef) return;
    state.detailPayload = payload;
    state.runs = {
      active: Array.isArray(runs.active) ? runs.active : [],
      items: Array.isArray(runs.items) ? runs.items : [],
      nextCursor: runs.nextCursor || null,
      hasMore: Boolean(runs.hasMore),
      activeTruncated: Boolean(runs.activeTruncated),
    };
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
    ${workItemSummaryHtml({ item, diagnostics, esc })}
    <section class="work-detail-card work-run-timeline">
      ${runUi.runTimelineHtml()}
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
          execution_profile: contract.execution_profile?.id,
          workspace_mode: contract.execution_profile?.workspace_mode,
          repository_access: contract.execution_profile?.repository_access,
          worker_capabilities: (contract.execution_profile?.required_worker_capabilities || []).join(', ') || null,
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
  runUi.wireRunTimeline();
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
    await loadItems({ reset: true });
  } catch (error) {
    setStatus(error.message || `${action} failed`, true);
  }
}
window.addEventListener('codex:work-item-run-updated', (event) => {
  const ref = String(event.detail?.workItemRef || '');
  if (!workItemsSurfaceActive() || !ref || ref !== state.selectedRef) return;
  if (state.runRefreshTimer) clearTimeout(state.runRefreshTimer);
  state.runRefreshTimer = setTimeout(() => {
    state.runRefreshTimer = null;
    runUi.refreshRuns({ append: false }).catch((error) => {
      setStatus(error.message || 'Failed to refresh live Run state', true);
    });
  }, 80);
});
window.addEventListener('codex:project-changed', async (event) => {
  const projectId = String(event.detail?.projectId || '').trim();
  if (!projectId || projectId === state.projectId) return;
  state.projectId = projectId;
  state.selectedRef = '';
  persistWorkItemProject(projectId);
  resetPaging();
  if (workItemsSurfaceActive()) {
    renderProjectSelect();
    renderSourceConfig();
    await loadItems({ reset: true });
  }
});
ensureShell();
