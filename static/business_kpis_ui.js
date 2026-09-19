import { request } from './api_client.js';

const state = {
  definitions: [],
  snapshot: null,
  selectedKpiId: '',
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
  const rendered = typeof value === 'number'
    ? new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 }).format(value)
    : String(value);
  return unit ? `${rendered} ${unit}` : rendered;
}

function definitionFor(id) {
  return state.definitions.find((item) => item.id === id) || null;
}

function currentItem() {
  const items = state.snapshot?.items || [];
  return items.find((item) => item.kpi_id === state.selectedKpiId) || items[0] || null;
}

function ensureShell() {
  if (document.querySelector('#business-kpis-dialog')) return;

  const link = document.createElement('link');
  link.rel = 'stylesheet';
  link.href = '/static/business_kpis_ui.css';
  document.head.appendChild(link);

  const button = document.createElement('button');
  button.id = 'business-kpis-button';
  button.type = 'button';
  button.className = 'ghost-button';
  button.textContent = 'Company KPIs';
  button.title = 'Open deterministic company operating metrics';
  (document.querySelector('.topbar .controls') || document.body).prepend(button);

  const dialog = document.createElement('dialog');
  dialog.id = 'business-kpis-dialog';
  dialog.className = 'business-kpis-dialog';
  dialog.innerHTML = `
    <div class="business-kpis-shell">
      <header class="business-kpis-header">
        <div>
          <h2>Company operating metrics</h2>
          <p>Deterministic business KPIs backed by canonical Metrics and exact CompanyFact provenance.</p>
        </div>
        <button type="button" class="icon-button business-kpis-close" aria-label="Close">×</button>
      </header>
      <div class="business-kpis-toolbar">
        <button type="button" class="primary-button business-kpis-capture">Capture current</button>
        <button type="button" class="ghost-button business-kpis-refresh">Refresh</button>
        <span class="business-kpis-status" aria-live="polite"></span>
      </div>
      <div class="business-kpis-meta"></div>
      <div class="business-kpis-layout">
        <aside class="business-kpis-list" aria-label="Business KPIs"></aside>
        <main class="business-kpi-detail">
          <div class="business-kpi-empty">Capture or load a company operating snapshot.</div>
        </main>
      </div>
    </div>`;
  document.body.appendChild(dialog);

  button.addEventListener('click', async () => {
    dialog.showModal();
    await refreshAll();
  });
  dialog.querySelector('.business-kpis-close').addEventListener('click', () => dialog.close());
  dialog.querySelector('.business-kpis-refresh').addEventListener('click', refreshAll);
  dialog.querySelector('.business-kpis-capture').addEventListener('click', captureCurrent);
}

function setStatus(message, error = false) {
  const target = document.querySelector('.business-kpis-status');
  if (!target) return;
  target.textContent = message || '';
  target.classList.toggle('error', Boolean(error));
}

async function refreshAll() {
  setStatus('Loading…');
  try {
    const [definitions, snapshots] = await Promise.all([
      request('/api/business-kpis'),
      request('/api/business-kpis/operating-snapshots?limit=1'),
    ]);
    state.definitions = definitions?.items || [];
    state.snapshot = snapshots?.items?.[0] || null;
    const itemIds = new Set((state.snapshot?.items || []).map((item) => item.kpi_id));
    if (!state.selectedKpiId || !itemIds.has(state.selectedKpiId)) {
      state.selectedKpiId = state.snapshot?.items?.[0]?.kpi_id || state.definitions[0]?.id || '';
    }
    render();
    setStatus('Up to date');
  } catch (error) {
    setStatus(error.message || 'Failed to load company KPIs', true);
  }
}

async function captureCurrent() {
  setStatus('Capturing deterministic operating state…');
  try {
    const payload = await request('/api/business-kpis/operating-snapshots', {
      method: 'POST',
      body: '{}',
    });
    state.snapshot = payload?.item || null;
    if (!state.selectedKpiId && state.snapshot?.items?.length) {
      state.selectedKpiId = state.snapshot.items[0].kpi_id;
    }
    render();
    setStatus('Operating snapshot captured');
  } catch (error) {
    setStatus(error.message || 'Failed to capture operating snapshot', true);
  }
}

function freshness(value) {
  const stateValue = value || 'missing';
  return `<span class="business-kpi-freshness ${esc(stateValue)}">${esc(stateValue)}</span>`;
}

function targetLabel(target) {
  if (!target) return 'No target';
  const variance = target.variance_percent === null || target.variance_percent === undefined
    ? target.variance
    : `${Number(target.variance_percent).toFixed(1)}%`;
  return `${target.passed ? 'target met' : 'target missed'} · variance ${variance}`;
}

function trendLabel(trend) {
  if (!trend?.previous_observation_id) return 'No previous observation';
  if (trend.previous_partial || trend.absolute_delta === null || trend.absolute_delta === undefined) {
    return 'Trend unavailable: partial baseline/current observation';
  }
  const percent = trend.percent_delta === null || trend.percent_delta === undefined
    ? ''
    : ` (${Number(trend.percent_delta).toFixed(1)}%)`;
  return `${trend.absolute_delta >= 0 ? '+' : ''}${Number(trend.absolute_delta).toFixed(2)}${percent}`;
}

function render() {
  const meta = document.querySelector('.business-kpis-meta');
  const list = document.querySelector('.business-kpis-list');
  const detail = document.querySelector('.business-kpi-detail');
  if (!meta || !list || !detail) return;

  if (state.snapshot) {
    meta.innerHTML = `
      <span>Snapshot <strong>${esc(state.snapshot.id)}</strong></span>
      <span>Captured ${esc(fmtTime(state.snapshot.captured_at))}</span>
      <span>By ${esc(state.snapshot.captured_by)}</span>`;
  } else {
    meta.innerHTML = '<span>No persisted operating snapshot. Capture current state to materialize exact Metric snapshots.</span>';
  }

  const items = state.snapshot?.items || [];
  if (!items.length) {
    list.innerHTML = state.definitions.length
      ? state.definitions.map((item) => `
          <button type="button" class="business-kpi-row" data-kpi-id="${esc(item.id)}">
            <strong>${esc(item.name)}</strong>
            <span>r${esc(item.revision)} · ${esc(item.domain)}</span>
            <small>No captured operating value</small>
          </button>`).join('')
      : '<div class="business-kpi-empty">No business KPI definitions.</div>';
    detail.innerHTML = '<div class="business-kpi-empty">Capture current state to create a deterministic operating snapshot.</div>';
    return;
  }

  list.innerHTML = items.map((item) => `
    <button type="button" class="business-kpi-row ${item.kpi_id === state.selectedKpiId ? 'selected' : ''}" data-kpi-id="${esc(item.kpi_id)}">
      <strong>${esc(item.name)}</strong>
      <span>${esc(fmtValue(item.value, item.unit))}</span>
      <small>${freshness(item.freshness)} · KPI r${esc(item.kpi_revision)} / Metric r${esc(item.metric_revision)}</small>
    </button>`).join('');

  list.querySelectorAll('.business-kpi-row').forEach((row) => row.addEventListener('click', () => {
    state.selectedKpiId = row.dataset.kpiId;
    render();
  }));

  const item = currentItem();
  const definition = item ? definitionFor(item.kpi_id) : null;
  if (!item) {
    detail.innerHTML = '<div class="business-kpi-empty">Select a KPI.</div>';
    return;
  }

  const sourceLinks = (item.source_external_record_ref_ids || []).map((id) =>
    `<a href="/api/business-context/external-records/${encodeURIComponent(id)}" target="_blank" rel="noopener">${esc(id)}</a>`
  ).join('');
  const factLinks = (item.selected_fact_ids || []).map((id) =>
    `<a href="/api/business-context/facts/${encodeURIComponent(id)}" target="_blank" rel="noopener">${esc(id)}</a>`
  ).join('');
  const goalButtons = (item.goal_ids || []).map((id) =>
    `<button type="button" class="link-button business-kpi-open-goal" data-goal-id="${esc(id)}">${esc(id)}</button>`
  ).join('');
  const decisionButtons = (item.decision_ids || []).map((id) =>
    `<button type="button" class="link-button business-kpi-open-decision" data-decision-id="${esc(id)}">${esc(id)}</button>`
  ).join('');

  detail.innerHTML = `
    <section class="business-kpi-card business-kpi-summary">
      <div>
        <small>${esc(item.kpi_id)} · definition r${esc(item.kpi_revision)}</small>
        <h3>${esc(item.name)}</h3>
        <p>${esc(definition?.description || '')}</p>
      </div>
      <div class="business-kpi-current">
        <span>Operating value</span>
        <strong>${esc(fmtValue(item.value, item.unit))}</strong>
        ${freshness(item.freshness)}
      </div>
    </section>
    <section class="business-kpi-card business-kpi-grid">
      <div><span>Domain</span><strong>${esc(item.domain)}</strong></div>
      <div><span>Metric</span><button type="button" class="link-button business-kpi-open-metric" data-metric-id="${esc(item.metric_id)}">${esc(item.metric_id)}</button></div>
      <div><span>Metric revision</span><strong>${esc(item.metric_revision)}</strong></div>
      <div><span>Metric snapshot</span><strong>${esc(item.metric_snapshot_id || 'none')}</strong></div>
      <div><span>Formula unit</span><strong>${esc(item.unit)}</strong></div>
      <div><span>Currency</span><strong>${esc(definition?.currency || 'n/a')}</strong></div>
      <div><span>Target</span><strong>${esc(targetLabel(item.target))}</strong></div>
      <div><span>Trend</span><strong>${esc(trendLabel(item.trend))}</strong></div>
      <div><span>Observation</span><strong>${esc((item.observation_ids || []).join(', ') || 'none')}</strong></div>
    </section>
    <section class="business-kpi-card">
      <h4>Freshness / data quality</h4>
      ${(item.reasons || []).length
        ? `<ul>${item.reasons.map((reason) => `<li>${esc(reason)}</li>`).join('')}</ul>`
        : '<p class="business-kpi-muted">No freshness or conflict warning recorded.</p>'}
    </section>
    <section class="business-kpi-card business-kpi-provenance">
      <h4>Exact provenance</h4>
      <div><span>Selected CompanyFacts</span><div>${factLinks || 'none'}</div></div>
      <div><span>External source records</span><div>${sourceLinks || 'none'}</div></div>
      <div><span>Bound Goals</span><div>${goalButtons || 'none'}</div></div>
      <div><span>Bound Decisions</span><div>${decisionButtons || 'none'}</div></div>
    </section>
    <section class="business-kpi-card">
      <h4>Versioned formula</h4>
      <pre>${esc(JSON.stringify(definition?.expression || {}, null, 2))}</pre>
      <small>Operands: ${esc((definition?.operands || []).map((operand) => `${operand.key} ← ${operand.fact_key} [${operand.aggregation}]`).join('; ') || 'none')}</small>
    </section>`;

  detail.querySelector('.business-kpi-open-metric')?.addEventListener('click', (event) => {
    window.dispatchEvent(new CustomEvent('codex:open-metric', {
      detail: { metricId: event.currentTarget.dataset.metricId },
    }));
  });
  detail.querySelectorAll('.business-kpi-open-goal').forEach((button) => button.addEventListener('click', () => {
    window.dispatchEvent(new CustomEvent('codex:open-goal', {
      detail: { goalId: button.dataset.goalId },
    }));
  }));
  detail.querySelectorAll('.business-kpi-open-decision').forEach((button) => button.addEventListener('click', () => {
    window.dispatchEvent(new CustomEvent('codex:open-decision', {
      detail: { decisionId: button.dataset.decisionId },
    }));
  }));
}

ensureShell();
