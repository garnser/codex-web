import { request } from './api_client.js';

const state = {
  workspaceWide: false,
  projectId: '',
  generation: 0,
  listController: null,
  detailController: null,
  metrics: [],
  selectedMetricId: '',
  detail: null,
  observations: [],
  snapshots: [],
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

function fmtValue(value, unit = '') {
  if (value === null || value === undefined) return '—';
  return `${value}${unit ? ` ${unit}` : ''}`;
}

function activeProjectId() {
  const route = location.pathname.match(/\/projects\/([^/]+)(?:\/|$)/);
  let routed = route?.[1] || '';
  try { routed = decodeURIComponent(routed); } catch { /* Invalid scope stays explicit. */ }
  return String(routed || document.body?.dataset?.activeProject
    || document.body?.dataset?.projectId
    || new URLSearchParams(location.search).get('project') || '').trim();
}

function projectPath(path, projectId) {
  if (projectId === null) return path;
  return `${path}${path.includes('?') ? '&' : '?'}project_id=${encodeURIComponent(projectId)}`;
}

function activeMetricScope() {
  // Only the explicit company KPI drill-down selects the workspace-wide view.
  // This is presentation scope; canonical API authentication still applies.
  return state.workspaceWide ? null : activeProjectId();
}

function clearProjectState(projectId) {
  state.listController?.abort();
  state.detailController?.abort();
  state.generation += 1;
  state.projectId = projectId;
  state.metrics = [];
  state.selectedMetricId = '';
  state.detail = null;
  state.observations = [];
  state.snapshots = [];
  renderList();
  const host = document.querySelector('.metric-detail');
  const scope = document.querySelector('.metrics-scope');
  if (scope) scope.textContent = projectId === null
    ? 'Workspace-wide · company KPI drill-down'
    : projectId ? `Project: ${projectId}` : 'No Project selected';
  if (host) host.innerHTML = projectId === null
    ? '<div class="metric-empty">Loading workspace metrics…</div>'
    : projectId
    ? '<div class="metric-empty">Loading metrics for this Project…</div>'
    : '<div class="metric-empty">Select a Project to view metrics.</div>';
}

function ensureShell() {
  if (document.querySelector('#metrics-dialog')) return;

  const link = document.createElement('link');
  link.rel = 'stylesheet';
  link.href = '/static/metrics_ui.css';
  document.head.appendChild(link);

  const button = document.createElement('button');
  button.id = 'metrics-button';
  button.type = 'button';
  button.className = 'ghost-button';
  button.textContent = 'Metrics';
  button.title = 'Open Metric/KPI explorer';
  (document.querySelector('.topbar .controls') || document.body).prepend(button);

  const dialog = document.createElement('dialog');
  dialog.id = 'metrics-dialog';
  dialog.className = 'metrics-dialog';
  dialog.innerHTML = `
    <div class="metrics-shell">
      <header class="metrics-header">
        <div>
          <h2>Metric / KPI explorer</h2>
          <p>Canonical definitions and observations, with deterministic freshness, aggregation and provenance.</p>
          <p class="metrics-scope" aria-live="polite"></p>
        </div>
        <button type="button" class="icon-button metrics-close" aria-label="Close">×</button>
      </header>
      <div class="metrics-toolbar">
        <button type="button" class="ghost-button metrics-refresh">Refresh</button>
        <span class="metrics-status" aria-live="polite"></span>
      </div>
      <div class="metrics-layout">
        <aside class="metrics-list" aria-label="Metric definitions"></aside>
        <main class="metric-detail">
          <div class="metric-empty">Select a metric to inspect canonical state.</div>
        </main>
      </div>
    </div>`;
  document.body.appendChild(dialog);

  button.addEventListener('click', async () => {
    state.workspaceWide = false;
    dialog.showModal();
    await refreshAll();
  });
  dialog.querySelector('.metrics-close').addEventListener('click', () => dialog.close());
  dialog.querySelector('.metrics-refresh').addEventListener('click', refreshAll);
}

function setStatus(message, isError = false) {
  const node = document.querySelector('.metrics-status');
  if (!node) return;
  node.textContent = message || '';
  node.classList.toggle('error', Boolean(isError));
}

async function refreshAll() {
  const projectId = activeMetricScope();
  if (projectId !== state.projectId) clearProjectState(projectId);
  if (projectId === '') {
    clearProjectState('');
    setStatus('Select a Project');
    return false;
  }
  state.listController?.abort();
  state.detailController?.abort();
  const controller = new AbortController();
  state.listController = controller;
  const generation = ++state.generation;
  const current = () => !controller.signal.aborted
    && generation === state.generation && projectId === activeMetricScope();
  setStatus('Loading…');
  try {
    const payload = await request(projectPath('/api/metrics', projectId), { signal: controller.signal });
    if (!current()) return false;
    state.metrics = Array.isArray(payload?.items) ? payload.items : [];
    if (!state.selectedMetricId || !state.metrics.some((row) => row.definition?.id === state.selectedMetricId)) {
      state.selectedMetricId = state.metrics[0]?.definition?.id || '';
    }
    renderList();
    if (state.selectedMetricId) {
      await loadMetric(state.selectedMetricId);
    } else {
      document.querySelector('.metric-detail').innerHTML = projectId === null
        ? '<div class="metric-empty">No metric definitions exist in this workspace.</div>'
        : '<div class="metric-empty">No metric definitions exist in this Project.</div>';
    }
    if (!current()) return false;
    setStatus('Up to date');
    return true;
  } catch (error) {
    if (current() && error?.name !== 'AbortError') setStatus(error.message || 'Failed to load metrics', true);
    return false;
  }
}

function freshnessBadge(current) {
  const freshness = current?.freshness || 'missing';
  return `<span class="metric-freshness ${esc(freshness)}">${esc(freshness)}</span>`;
}

function renderList() {
  const host = document.querySelector('.metrics-list');
  if (!host) return;
  if (!state.metrics.length) {
    host.innerHTML = '<div class="metric-empty">No metrics.</div>';
    return;
  }
  host.innerHTML = state.metrics.map(({ definition = {}, current = {} }) => {
    const selected = definition.id === state.selectedMetricId ? 'selected' : '';
    return `
      <button type="button" class="metric-row ${selected}" data-metric-id="${esc(definition.id)}">
        <strong>${esc(definition.name || definition.key || definition.id)}</strong>
        <span>${esc(fmtValue(current.value, definition.unit))}</span>
        <small>${freshnessBadge(current)} · ${esc(definition.aggregation || 'last')}</small>
      </button>`;
  }).join('');
  host.querySelectorAll('.metric-row').forEach((row) => row.addEventListener('click', async () => {
    state.selectedMetricId = row.dataset.metricId;
    renderList();
    await loadMetric(state.selectedMetricId);
  }));
}

async function loadMetric(metricId) {
  const host = document.querySelector('.metric-detail');
  const projectId = state.projectId;
  if (!host || projectId === '' || projectId !== activeMetricScope()) return;
  state.detailController?.abort();
  const controller = new AbortController();
  state.detailController = controller;
  const generation = state.generation;
  const current = () => !controller.signal.aborted
    && generation === state.generation && metricId === state.selectedMetricId
    && projectId === activeMetricScope();
  host.innerHTML = '<div class="metric-empty">Loading metric detail…</div>';
  try {
    const encoded = encodeURIComponent(metricId);
    const [detail, observations, snapshots] = await Promise.all([
      request(projectPath(`/api/metrics/${encoded}`, projectId), { signal: controller.signal }),
      request(projectPath(`/api/metrics/${encoded}/observations?limit=100`, projectId), { signal: controller.signal }),
      request(projectPath(`/api/metrics/${encoded}/snapshots?limit=50`, projectId), { signal: controller.signal }),
    ]);
    if (!current()) return;
    state.detail = detail;
    state.observations = observations?.items || [];
    state.snapshots = snapshots?.items || [];
    renderDetail();
  } catch (error) {
    if (current() && error?.name !== 'AbortError') {
      host.innerHTML = `<div class="metric-error">${esc(error.message || 'Failed to load metric')}</div>`;
    }
  }
}

function renderDetail() {
  const host = document.querySelector('.metric-detail');
  if (!host) return;
  const definition = state.detail?.definition || {};
  const current = state.detail?.current || {};
  const requirements = definition.source_requirements || [];
  const thresholds = definition.thresholds || [];

  host.innerHTML = `
    <section class="metric-card metric-summary">
      <div>
        <h3>${esc(definition.name || definition.key || definition.id)}</h3>
        <p>${esc(definition.description || '')}</p>
      </div>
      <div class="metric-current">
        <span>Derived current value</span>
        <strong>${esc(fmtValue(current.value, definition.unit))}</strong>
        ${freshnessBadge(current)}
        <small>${esc(current.freshness_reason || '')}</small>
      </div>
    </section>

    <section class="metric-card metric-grid">
      <div><span>Canonical ID</span><strong>${esc(definition.id)}</strong></div>
      <div><span>Key</span><strong>${esc(definition.key)}</strong></div>
      <div><span>Revision</span><strong>${esc(definition.revision)}</strong></div>
      <div><span>Owner</span><strong>${esc(definition.owner_identity_id)}</strong></div>
      <div><span>Value type</span><strong>${esc(definition.value_type)}</strong></div>
      <div><span>Aggregation</span><strong>${esc(definition.aggregation)}</strong></div>
      <div><span>Window</span><strong>${esc(definition.window_seconds ? `${definition.window_seconds}s` : 'latest/all')}</strong></div>
      <div><span>Freshness policy</span><strong>${esc(definition.freshness_seconds)}s</strong></div>
      <div><span>Project scope</span><strong>${esc(definition.project_id || 'workspace')}</strong></div>
      <div><span>Resource scope</span><strong>${esc(definition.resource_id || 'none')}</strong></div>
    </section>

    <section class="metric-card">
      <h4>Source requirements & thresholds</h4>
      <p class="metric-muted">Permitted sources: ${esc(requirements.join(', ') || 'any explicit source')}</p>
      <div class="metric-thresholds">
        ${thresholds.length ? thresholds.map((item) => `
          <div><strong>${esc(item.label)}</strong><span>${esc(item.operator)} ${esc(item.value)} ${esc(definition.unit)}</span></div>
        `).join('') : '<p class="metric-muted">No thresholds recorded.</p>'}
      </div>
    </section>

    <section class="metric-card">
      <h4>Observation history</h4>
      <div class="metric-table-wrap">
        <table class="metric-table">
          <thead><tr><th>Observed</th><th>Value</th><th>Source</th><th>External ref</th><th>Evidence</th><th>State</th></tr></thead>
          <tbody>
            ${state.observations.length ? state.observations.map((item) => `
              <tr>
                <td>${esc(fmtTime(item.observed_at))}</td>
                <td>${esc(fmtValue(item.value, item.unit))}</td>
                <td>${esc(item.provider ? `${item.provider} / ${item.source}` : item.source)}</td>
                <td>${esc(item.external_record_ref || '—')}</td>
                <td>${esc((item.evidence_ids || []).join(', ') || '—')}</td>
                <td>${item.partial ? '<span class="metric-freshness partial">partial</span>' : 'complete'}</td>
              </tr>
            `).join('') : '<tr><td colspan="6">No observations.</td></tr>'}
          </tbody>
        </table>
      </div>
    </section>

    <section class="metric-card">
      <h4>Immutable snapshots</h4>
      <p class="metric-muted">Snapshots preserve the exact metric revision and observation IDs used by later Decisions or audits.</p>
      <div class="metric-snapshots">
        ${state.snapshots.length ? state.snapshots.map((item) => `
          <article>
            <strong>${esc(fmtValue(item.value, item.unit))} · ${esc(item.freshness)}</strong>
            <span>${esc(item.id)}</span>
            <small>metric r${esc(item.metric_revision)} · ${esc(item.aggregation)} · captured ${esc(fmtTime(item.captured_at))}</small>
            <small>observations: ${esc((item.observation_ids || []).join(', ') || 'none')}</small>
          </article>
        `).join('') : '<p class="metric-muted">No persisted snapshots yet.</p>'}
      </div>
    </section>`;
}

ensureShell();

window.addEventListener('codex-open-metric', async (event) => {
  const metricId = event?.detail?.metricId || '';
  const dialog = document.querySelector('#metrics-dialog');
  if (!dialog || !metricId) return;
  state.workspaceWide = event?.detail?.scope === 'workspace';
  if (!dialog.open) dialog.showModal();
  if (!(await refreshAll())) return;
  if (state.metrics.some((row) => row.definition?.id === metricId)) {
    state.selectedMetricId = metricId;
    renderList();
    await loadMetric(metricId);
  }
});

window.addEventListener('codex:project-changed', () => {
  state.workspaceWide = false;
  clearProjectState(activeProjectId());
  setStatus(state.projectId ? 'Project changed' : 'Select a Project');
  if (document.querySelector('#metrics-dialog')?.open && state.projectId) void refreshAll();
});
