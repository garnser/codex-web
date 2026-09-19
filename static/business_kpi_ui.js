import { request } from './api_client.js';

const operatingState = { view: null };

const esc = (value) => String(value ?? '')
  .replaceAll('&', '&amp;')
  .replaceAll('<', '&lt;')
  .replaceAll('>', '&gt;')
  .replaceAll('"', '&quot;')
  .replaceAll("'", '&#039;');

function fmt(value, unit = '', currency = '') {
  if (value === null || value === undefined) return '—';
  const numeric = typeof value === 'number' ? value.toLocaleString(undefined, { maximumFractionDigits: 2 }) : value;
  return [numeric, currency || unit].filter(Boolean).join(' ');
}

function ensureOperatingShell() {
  if (document.querySelector('#business-kpi-dialog')) return;

  const link = document.createElement('link');
  link.rel = 'stylesheet';
  link.href = '/static/business_kpi_ui.css';
  document.head.appendChild(link);

  const button = document.createElement('button');
  button.id = 'business-kpi-button';
  button.type = 'button';
  button.className = 'ghost-button';
  button.textContent = 'Operating view';
  button.title = 'Open company operating KPI view';
  (document.querySelector('.topbar .controls') || document.body).prepend(button);

  const dialog = document.createElement('dialog');
  dialog.id = 'business-kpi-dialog';
  dialog.className = 'business-kpi-dialog';
  dialog.innerHTML = `
    <div class="business-kpi-shell">
      <header class="business-kpi-header">
        <div>
          <h2>Company operating view</h2>
          <p>Structured business KPIs from canonical Metric observations with exact provenance and freshness.</p>
        </div>
        <button type="button" class="icon-button business-kpi-close" aria-label="Close">×</button>
      </header>
      <div class="business-kpi-toolbar">
        <button type="button" class="ghost-button business-kpi-reload">Reload</button>
        <button type="button" class="ghost-button business-kpi-refresh">Refresh KPI observations</button>
        <span class="business-kpi-status" aria-live="polite"></span>
      </div>
      <main class="business-kpi-content">
        <div class="business-kpi-empty">Load the operating view to inspect company metrics.</div>
      </main>
    </div>`;
  document.body.appendChild(dialog);

  button.addEventListener('click', async () => {
    dialog.showModal();
    await loadOperatingView();
  });
  dialog.querySelector('.business-kpi-close').addEventListener('click', () => dialog.close());
  dialog.querySelector('.business-kpi-reload').addEventListener('click', loadOperatingView);
  dialog.querySelector('.business-kpi-refresh').addEventListener('click', refreshKpis);
}

function setOperatingStatus(message, error = false) {
  const node = document.querySelector('.business-kpi-status');
  if (!node) return;
  node.textContent = message || '';
  node.classList.toggle('error', Boolean(error));
}

async function refreshKpis() {
  setOperatingStatus('Refreshing deterministic KPI observations…');
  try {
    await request('/api/business-kpis/refresh', { method: 'POST' });
    await loadOperatingView();
  } catch (error) {
    setOperatingStatus(error.message || 'KPI refresh failed', true);
  }
}

async function loadOperatingView() {
  setOperatingStatus('Loading…');
  try {
    const payload = await request('/api/business-kpis/operating-view');
    operatingState.view = payload?.item || null;
    renderOperatingView();
    setOperatingStatus('Up to date');
  } catch (error) {
    setOperatingStatus(error.message || 'Failed to load operating view', true);
  }
}

function links(kind, ids) {
  if (!Array.isArray(ids) || !ids.length) return '—';
  const base = kind === 'goal' ? '/api/goals/' : '/api/decisions/';
  return ids.map((id) => `<a target="_blank" rel="noreferrer" href="${base}${encodeURIComponent(id)}">${esc(id)}</a>`).join(', ');
}

function sourceLinks(ids) {
  if (!Array.isArray(ids) || !ids.length) return '—';
  return ids.map((id) => `<a target="_blank" rel="noreferrer" href="/api/business-context/external-records/${encodeURIComponent(id)}">${esc(id)}</a>`).join(', ');
}

function thresholdMarkup(rows = []) {
  if (!rows.length) return '<span class="business-kpi-muted">No thresholds.</span>';
  return rows.map((item) => `
    <div class="business-kpi-threshold ${esc(item.state)}">
      <strong>${esc(item.label)}</strong>
      <span>${esc(item.operator)} ${esc(item.target_value)}</span>
      <small>${esc(item.state)}${item.variance === null || item.variance === undefined ? '' : ` · variance ${esc(item.variance)}`}</small>
    </div>`).join('');
}

function renderOperatingView() {
  const host = document.querySelector('.business-kpi-content');
  const view = operatingState.view;
  if (!host) return;
  if (!view) {
    host.innerHTML = '<div class="business-kpi-empty">No operating view is available.</div>';
    return;
  }
  const items = Array.isArray(view.items) ? view.items : [];
  host.innerHTML = `
    <section class="business-kpi-overall ${view.current ? 'current' : 'blocked'}">
      <div>
        <span>Operating state</span>
        <strong>${view.current ? 'CURRENT' : 'NOT CURRENT'}</strong>
      </div>
      <div>
        <span>Evaluated</span>
        <strong>${view.evaluated_at ? new Date(view.evaluated_at * 1000).toLocaleString() : '—'}</strong>
      </div>
      <p>${esc((view.blockers || []).join(' · ') || 'All configured KPIs satisfy current freshness/completeness requirements.')}</p>
    </section>
    <div class="business-kpi-grid">
      ${items.length ? items.map((item) => `
        <article class="business-kpi-card" data-kpi-id="${esc(item.kpi_id)}">
          <header>
            <div>
              <small>${esc(item.domain)}</small>
              <h3>${esc(item.name)}</h3>
            </div>
            <span class="business-kpi-readiness ${esc(item.readiness)}">${esc(item.readiness)}</span>
          </header>
          <div class="business-kpi-value">${esc(fmt(item.value, item.unit, item.currency))}</div>
          <div class="business-kpi-trend">
            <span>Trend</span>
            <strong>${item.trend_delta === null || item.trend_delta === undefined ? '—' : esc(item.trend_delta)}</strong>
            <small>${item.trend_percent === null || item.trend_percent === undefined ? 'No same-revision comparison' : `${esc(item.trend_percent.toFixed(2))}%`}</small>
          </div>
          <div class="business-kpi-thresholds">${thresholdMarkup(item.thresholds)}</div>
          <dl>
            <dt>Freshness</dt><dd>${esc(item.freshness)}</dd>
            <dt>Revisions</dt><dd>KPI r${esc(item.kpi_revision)} · Metric r${esc(item.metric_revision)}</dd>
            <dt>Observations</dt><dd>${esc((item.observation_ids || []).join(', ') || 'none')}</dd>
            <dt>Fact keys</dt><dd>${esc((item.fact_keys || []).join(', ') || 'none')}</dd>
            <dt>Sources</dt><dd>${sourceLinks(item.external_record_ref_ids)}</dd>
            <dt>Goals</dt><dd>${links('goal', item.goal_ids)}</dd>
            <dt>Decisions</dt><dd>${links('decision', item.decision_ids)}</dd>
          </dl>
          ${(item.readiness_reasons || []).length ? `<p class="business-kpi-warning">${esc(item.readiness_reasons.join(' · '))}</p>` : ''}
          <button type="button" class="ghost-button business-kpi-open-metric" data-metric-id="${esc(item.metric_id)}">Open Metric definition</button>
        </article>`).join('') : '<div class="business-kpi-empty">No business KPI definitions exist in this workspace.</div>'}
    </div>`;

  host.querySelectorAll('.business-kpi-open-metric').forEach((button) => {
    button.addEventListener('click', () => {
      window.dispatchEvent(new CustomEvent('codex-open-metric', {
        detail: { metricId: button.dataset.metricId || '' },
      }));
    });
  });
}

ensureOperatingShell();
