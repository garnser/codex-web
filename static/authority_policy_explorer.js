import { request as apiRequest } from './api_client.js';

function esc(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

function csv(value) {
  return String(value || '').split(',').map((item) => item.trim()).filter(Boolean);
}

function optionalNumber(id) {
  const raw = document.getElementById(id)?.value?.trim();
  return raw ? Number(raw) : null;
}

function paramsFromContext() {
  const params = new URLSearchParams();
  for (const [key, id] of [
    ['identity_id', 'authority-explorer-identity'],
    ['project_id', 'authority-explorer-project'],
    ['work_item_ref', 'authority-explorer-work-item'],
    ['role_id', 'authority-explorer-role'],
  ]) {
    const value = document.getElementById(id)?.value?.trim();
    if (value) params.set(key, value);
  }
  return params;
}

function definitionLine(definition) {
  if (!definition) return 'No active Definition revision.';
  const ref = definition.reference || {};
  return [
    esc(ref.kind),
    esc(ref.definition_id),
    'r' + esc(ref.revision),
    esc(definition.scope_type) + (definition.scope_id ? ':' + esc(definition.scope_id) : ''),
    'checksum ' + esc(ref.checksum),
  ].join(' · ');
}

function renderEffective(data) {
  const host = document.getElementById('authority-explorer-result');
  if (!host) return;
  const assignments = (data.assignments || []).map((item) => (
    '<div class="comm-entry"><strong>' + esc(item.role_id) + '</strong> via '
      + esc(item.source_type) + ' <code>' + esc(item.source_id) + '</code>'
      + ' · lifecycle ' + esc(item.role_lifecycle)
      + (item.project_ids?.length ? ' · projects ' + esc(item.project_ids.join(', ')) : '')
      + '</div>'
  )).join('') || '<div class="comm-entry">No matching Role binding or delegation.</div>';

  const matrix = (data.permission_matrix || []).map((item) => (
    '<div class="comm-entry"><strong>' + esc(item.capability) + '</strong> · '
      + esc(item.level) + ' · role <code>' + esc(item.role_id) + '</code>'
      + ' · grant <code>' + esc(item.grant_id) + '</code>'
      + '<br><small>source ' + esc(item.source_type) + ':' + esc(item.source_id)
      + ' · path ' + esc((item.inheritance_path || []).join(' → '))
      + (item.environments?.length ? ' · env ' + esc(item.environments.join(', ')) : '')
      + (item.project_ids?.length ? ' · projects ' + esc(item.project_ids.join(', ')) : '')
      + (item.max_amount_usd != null ? ' · $' + esc(item.max_amount_usd) : '')
      + (item.max_model_calls != null ? ' · model calls ≤ ' + esc(item.max_model_calls) : '')
      + ' · autonomy ≤ ' + esc(item.max_autonomous_risk)
      + '</small></div>'
  )).join('') || '<div class="comm-entry">No effective grants.</div>';

  const role = data.role_view
    ? '<div class="comm-entry"><strong>Role inspection:</strong> '
      + esc(data.role_view.role.name) + ' <code>' + esc(data.role_view.role.id) + '</code>'
      + ' · lifecycle ' + esc(data.role_view.role.lifecycle || 'active')
      + ' · inherited grants ' + esc(data.role_view.effective_grants.length) + '</div>'
    : '';

  host.innerHTML =
    '<div class="comm-entry"><strong>Effective Definition:</strong> '
      + definitionLine(data.definition) + '</div>'
    + '<div class="comm-entry"><strong>Subject:</strong> '
      + esc(data.actor?.identity_id) + ' · ' + esc(data.actor?.principal_kind)
      + (data.project_id ? ' · project ' + esc(data.project_id) : '')
      + (data.work_item_ref ? ' · work item ' + esc(data.work_item_ref) : '')
      + '</div>'
    + role
    + '<div class="section-title"><span>Matching assignments</span></div>'
    + assignments
    + '<div class="section-title"><span>Effective permission matrix</span></div>'
    + matrix;
}

function renderImpact(data) {
  const host = document.getElementById('authority-impact-result');
  if (!host) return;
  const affected = data.affected || {};
  const reasons = data.assessment?.reasons || [];
  const work = affected.active_work || [];
  host.innerHTML =
    '<div class="comm-entry"><strong>Candidate:</strong> ' + definitionLine(data.candidate) + '</div>'
    + '<div class="comm-entry"><strong>Current:</strong> ' + definitionLine(data.active) + '</div>'
    + '<div class="comm-entry"><strong>Approval gate:</strong> '
      + (data.assessment?.requires_independent_approval ? 'independent approval required' : 'no expansion approval required')
      + (reasons.length ? '<br><small>' + reasons.map(esc).join('<br>') + '</small>' : '')
      + '</div>'
    + '<div class="comm-entry"><strong>Affected:</strong> roles '
      + esc((affected.role_ids || []).join(', ') || 'none')
      + ' · identities ' + esc((affected.identity_ids || []).join(', ') || 'none')
      + ' · teams ' + esc((affected.team_ids || []).join(', ') || 'none')
      + ' · projects ' + esc(affected.all_projects_in_scope ? 'all in scope' : ((affected.project_ids || []).join(', ') || 'none'))
      + ' · active work ' + esc(affected.active_work_count || 0)
      + '</div>'
    + work.map((item) => (
      '<div class="comm-entry"><small>active ' + esc(item.object_type) + ' '
        + esc(item.object_id) + (item.project_id ? ' · ' + esc(item.project_id) : '') + '</small></div>'
    )).join('');
}

function renderSimulation(data) {
  const host = document.getElementById('authority-simulation-result');
  if (!host) return;
  const decision = data.decision || {};
  const ref = decision.definition_ref || {};
  host.innerHTML =
    '<div class="comm-entry"><strong>' + esc(String(decision.outcome || '').toUpperCase())
      + '</strong> · ' + esc(data.mode)
      + (data.record_id ? ' · simulated ' + esc(data.record_id) : '')
      + '</div>'
    + '<div class="comm-entry"><strong>Canonical reasons</strong><br><small>'
      + (decision.reasons || []).map(esc).join('<br>')
      + '</small></div>'
    + '<div class="comm-entry"><small>Definition '
      + esc(ref.kind || 'unresolved') + ':' + esc(ref.definition_id || '')
      + (ref.revision ? ' r' + esc(ref.revision) : '')
      + (ref.checksum ? ' · checksum ' + esc(ref.checksum) : '')
      + '</small></div>';
}

async function loadEffective() {
  const host = document.getElementById('authority-explorer-result');
  if (host) host.textContent = 'Loading effective policy...';
  const params = paramsFromContext();
  const data = await apiRequest('/api/authority/effective?' + params.toString());
  renderEffective(data);
}

async function loadImpact() {
  const recordId = document.getElementById('authority-impact-record')?.value?.trim();
  if (!recordId) throw new Error('Candidate record ID is required.');
  const host = document.getElementById('authority-impact-result');
  if (host) host.textContent = 'Calculating deterministic impact...';
  const data = await apiRequest('/api/authority/impact/' + encodeURIComponent(recordId));
  renderImpact(data);
}

async function simulate() {
  const capability = document.getElementById('authority-sim-capability')?.value?.trim();
  if (!capability) throw new Error('Capability is required.');
  const projectId = document.getElementById('authority-explorer-project')?.value?.trim() || null;
  const environment = document.getElementById('authority-sim-environment')?.value || null;
  const body = {
    identity_id: document.getElementById('authority-explorer-identity')?.value?.trim() || null,
    work_item_ref: document.getElementById('authority-explorer-work-item')?.value?.trim() || null,
    record_id: document.getElementById('authority-sim-record')?.value?.trim() || null,
    request: {
      capability,
      level: document.getElementById('authority-sim-level')?.value || 'read',
      project_id: projectId,
      resource_ids: csv(document.getElementById('authority-sim-resources')?.value),
      environment,
      amount_usd: optionalNumber('authority-sim-amount'),
      input_tokens: optionalNumber('authority-sim-input-tokens'),
      output_tokens: optionalNumber('authority-sim-output-tokens'),
      model_calls: optionalNumber('authority-sim-model-calls'),
      autonomous_risk: document.getElementById('authority-sim-risk')?.value || 'low',
      approval_role_ids: csv(document.getElementById('authority-sim-approvals')?.value),
    },
  };
  const host = document.getElementById('authority-simulation-result');
  if (host) host.textContent = 'Evaluating with canonical runtime authority logic...';
  const data = await apiRequest('/api/authority/simulate', {
    method: 'POST',
    body: JSON.stringify(body),
  });
  renderSimulation(data);
}

function status(error, targetId) {
  const host = document.getElementById(targetId);
  if (host) host.textContent = error.message || String(error);
}

function bind() {
  document.getElementById('authority-explorer-load')?.addEventListener('click', () => {
    loadEffective().catch((error) => status(error, 'authority-explorer-result'));
  });
  document.getElementById('authority-impact-load')?.addEventListener('click', () => {
    loadImpact().catch((error) => status(error, 'authority-impact-result'));
  });
  document.getElementById('authority-sim-run')?.addEventListener('click', () => {
    simulate().catch((error) => status(error, 'authority-simulation-result'));
  });
}

window.addEventListener('DOMContentLoaded', bind);
