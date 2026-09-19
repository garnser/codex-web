import { request as apiRequest } from './api_client.js';

const SUPPORTED = new Set(['authority-role-catalog', 'execution-role-catalog']);
const STRUCTURAL_EXECUTION_ROLES = new Set(['orchestrator', 'quinn', 'release-manager']);

const state = {
  records: [],
  source: null,
  payload: null,
};

function esc(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function csv(value) {
  return String(value ?? '').split(',').map((item) => item.trim()).filter(Boolean);
}

function lines(value) {
  return String(value ?? '').split('\n').map((item) => item.trim()).filter(Boolean);
}

function pairs(value) {
  const result = {};
  for (const row of lines(value)) {
    const split = row.indexOf('=');
    if (split < 1) continue;
    result[row.slice(0, split).trim()] = row.slice(split + 1).trim();
  }
  return result;
}

function pairText(value) {
  return Object.entries(value || {}).map(([key, item]) => key + '=' + item).join('\n');
}

function numberOrNull(value) {
  const raw = String(value ?? '').trim();
  if (!raw) return null;
  const parsed = Number(raw);
  return Number.isFinite(parsed) ? parsed : null;
}

function setStatus(message) {
  const host = document.getElementById('definition-typed-status');
  if (host) host.textContent = message || '';
}

function sourceLabel(record) {
  const scope = record.scope_type === 'global'
    ? 'global'
    : record.scope_type + ':' + (record.scope_id || 'missing');
  return record.kind + ' · ' + record.definition_id + ' · r' + record.revision
    + ' · ' + scope + ' · ' + record.lifecycle;
}

function field(label, html) {
  return '<label><span>' + esc(label) + '</span>' + html + '</label>';
}

function input(cls, value, attrs = '') {
  return '<input class="' + cls + '" value="' + esc(value ?? '') + '" ' + attrs + ' />';
}

function textarea(cls, value, rows = 3) {
  return '<textarea class="' + cls + '" rows="' + rows + '">' + esc(value ?? '') + '</textarea>';
}

function lifecycleSelect(value, cls = 'typed-role-lifecycle') {
  return '<select class="' + cls + '">'
    + ['active', 'deprecated', 'disabled'].map((item) => (
      '<option value="' + item + '"' + (item === value ? ' selected' : '') + '>'
      + item + '</option>'
    )).join('')
    + '</select>';
}

function authorityGrantHtml(grant, roleIndex, grantIndex) {
  return '<div class="comm-entry typed-authority-grant" data-role-index="' + roleIndex
    + '" data-grant-index="' + grantIndex + '">'
    + '<div class="section-title"><strong>Grant ' + esc(grant.id) + '</strong>'
    + '<button type="button" class="ghost-button" data-typed-action="remove-grant">Remove grant</button></div>'
    + '<div class="route-test">'
    + field('Grant ID', input('typed-grant-id', grant.id))
    + field('Capability', input('typed-grant-capability', grant.capability))
    + field('Authority level', '<select class="typed-grant-level">'
      + ['read', 'recommend', 'prepare', 'execute', 'approve'].map((item) => (
        '<option value="' + item + '"' + (item === grant.level ? ' selected' : '') + '>' + item + '</option>'
      )).join('') + '</select>')
    + field('Projects', input('typed-grant-projects', (grant.project_ids || []).join(', ')))
    + field('Resource IDs', input('typed-grant-resource-ids', (grant.resource_ids || []).join(', ')))
    + field('Resource types', input('typed-grant-resource-types', (grant.resource_types || []).join(', ')))
    + field('Resource risks', input('typed-grant-resource-risks', (grant.resource_risks || []).join(', ')))
    + field('Sensitivities', input('typed-grant-sensitivities', (grant.resource_sensitivities || []).join(', ')))
    + field('Environments', input('typed-grant-environments', (grant.environments || []).join(', ')))
    + field('Max amount USD', input('typed-grant-amount', grant.max_amount_usd ?? '', 'type="number" min="0" step="0.01"'))
    + field('Max input tokens', input('typed-grant-input-tokens', grant.max_input_tokens ?? '', 'type="number" min="0"'))
    + field('Max output tokens', input('typed-grant-output-tokens', grant.max_output_tokens ?? '', 'type="number" min="0"'))
    + field('Max model calls', input('typed-grant-model-calls', grant.max_model_calls ?? '', 'type="number" min="0"'))
    + field('Autonomy risk', '<select class="typed-grant-autonomy">'
      + ['low', 'medium', 'high', 'critical'].map((item) => (
        '<option value="' + item + '"' + (item === (grant.max_autonomous_risk || 'low') ? ' selected' : '') + '>'
        + item + '</option>'
      )).join('') + '</select>')
    + field('Required approvals', input('typed-grant-approval-count', grant.approvals?.count ?? 0, 'type="number" min="0" max="20"'))
    + field('Approver roles', input('typed-grant-approval-roles', (grant.approvals?.role_ids || []).join(', ')))
    + '</div></div>';
}

function authorityRoleHtml(role, roleIndex) {
  return '<div class="comm-entry typed-authority-role" data-role-index="' + roleIndex + '">'
    + '<div class="section-title"><strong>' + esc(role.name || role.id) + '</strong>'
    + '<div class="developer-toolbar">'
    + '<button type="button" class="ghost-button" data-typed-action="add-grant">Add grant</button>'
    + '<button type="button" class="ghost-button" data-typed-action="clone-role">Clone role</button>'
    + '<button type="button" class="ghost-button" data-typed-action="remove-role">Remove role</button>'
    + '</div></div>'
    + '<div class="route-test">'
    + field('Role ID', input('typed-role-id', role.id))
    + field('Name', input('typed-role-name', role.name))
    + field('Lifecycle', lifecycleSelect(role.lifecycle || 'active'))
    + field('Inherits', input('typed-role-inherits', (role.inherits || []).join(', ')))
    + '</div>'
    + field('Description', textarea('typed-role-description', role.description, 2))
    + '<div class="typed-grant-list">'
    + (role.grants || []).map((grant, grantIndex) => authorityGrantHtml(grant, roleIndex, grantIndex)).join('')
    + '</div></div>';
}

function authorityBindingHtml(binding, index) {
  return '<div class="comm-entry typed-authority-binding" data-binding-index="' + index + '">'
    + '<div class="section-title"><strong>Binding ' + esc(binding.id) + '</strong>'
    + '<button type="button" class="ghost-button" data-typed-action="remove-binding">Remove</button></div>'
    + '<div class="route-test">'
    + field('Binding ID', input('typed-binding-id', binding.id))
    + field('Role ID', input('typed-binding-role', binding.role_id))
    + field('Subject kind', '<select class="typed-binding-kind"><option value="identity"'
      + (binding.subject_kind === 'identity' ? ' selected' : '') + '>identity</option><option value="team"'
      + (binding.subject_kind === 'team' ? ' selected' : '') + '>team</option></select>')
    + field('Subject ID', input('typed-binding-subject', binding.subject_id))
    + field('Organization', input('typed-binding-org', binding.organization_id || ''))
    + field('Workspace', input('typed-binding-workspace', binding.workspace_id || ''))
    + field('Projects', input('typed-binding-projects', (binding.project_ids || []).join(', ')))
    + '</div></div>';
}

function dateTimeValue(seconds) {
  if (!seconds) return '';
  const value = new Date(Number(seconds) * 1000);
  if (Number.isNaN(value.getTime())) return '';
  const local = new Date(value.getTime() - value.getTimezoneOffset() * 60000);
  return local.toISOString().slice(0, 16);
}

function authorityDelegationHtml(item, index) {
  return '<div class="comm-entry typed-authority-delegation" data-delegation-index="' + index + '">'
    + '<div class="section-title"><strong>Delegation ' + esc(item.id) + '</strong>'
    + '<button type="button" class="ghost-button" data-typed-action="remove-delegation">Remove</button></div>'
    + '<div class="route-test">'
    + field('Delegation ID', input('typed-delegation-id', item.id))
    + field('Role ID', input('typed-delegation-role', item.role_id))
    + field('Delegate identity', input('typed-delegation-identity', item.delegate_identity_id))
    + field('Delegated by', input('typed-delegation-by', item.delegated_by_identity_id))
    + field('Organization', input('typed-delegation-org', item.organization_id || ''))
    + field('Workspace', input('typed-delegation-workspace', item.workspace_id || ''))
    + field('Projects', input('typed-delegation-projects', (item.project_ids || []).join(', ')))
    + field('Expires', input('typed-delegation-expires', dateTimeValue(item.expires_at), 'type="datetime-local"'))
    + '</div>'
    + field('Reason', textarea('typed-delegation-reason', item.reason, 2))
    + '</div>';
}

function renderAuthority() {
  const host = document.getElementById('definition-typed-editor-host');
  if (!host) return;
  const payload = state.payload;
  host.innerHTML = '<div class="section-title"><span>Operational Roles</span></div>'
    + '<div id="definition-typed-role-list">'
    + (payload.roles || []).map(authorityRoleHtml).join('')
    + '</div>'
    + '<div class="section-title"><span>Identity / Team bindings</span></div>'
    + '<div id="definition-typed-binding-list">'
    + (payload.bindings || []).map(authorityBindingHtml).join('')
    + '</div>'
    + '<div class="section-title"><span>Delegated authority</span></div>'
    + '<div id="definition-typed-delegation-list">'
    + (payload.delegations || []).map(authorityDelegationHtml).join('')
    + '</div>';
}

function executionRoleHtml(role, index) {
  return '<div class="comm-entry typed-execution-role" data-role-index="' + index + '">'
    + '<div class="section-title"><strong>' + esc(role.name || role.id) + '</strong>'
    + '<div class="developer-toolbar">'
    + '<button type="button" class="ghost-button" data-typed-action="clone-role">Clone role</button>'
    + '<button type="button" class="ghost-button" data-typed-action="remove-role"'
    + (STRUCTURAL_EXECUTION_ROLES.has(role.id) ? ' disabled title="Required structural role"' : '') + '>Remove role</button>'
    + '</div></div>'
    + '<div class="route-test">'
    + field('Role ID', input('typed-role-id', role.id))
    + field('Name', input('typed-role-name', role.name))
    + field('Lane', input('typed-role-lane', role.lane))
    + field('Lifecycle', lifecycleSelect(role.lifecycle || 'active'))
    + field('Automatic selection', '<input class="typed-role-auto-select" type="checkbox"'
      + (role.auto_select !== false ? ' checked' : '') + ' />')
    + '</div>'
    + field('Description', textarea('typed-role-description', role.description, 2))
    + field('Expected work (one per line)', textarea('typed-role-expected', (role.expected_work || []).join('\n')))
    + field('Must refuse / reroute', textarea('typed-role-refuse', (role.must_refuse || []).join('\n')))
    + field('Required artifacts', textarea('typed-role-artifacts', (role.required_artifacts || []).join('\n')))
    + field('Handoff targets', textarea('typed-role-hands-to', (role.hands_to || []).join('\n')))
    + field('Failure conditions', textarea('typed-role-failures', (role.failure_conditions || []).join('\n')))
    + field('Routing keywords', textarea('typed-role-keywords', (role.keywords || []).join('\n')))
    + '</div>';
}

function renderExecution() {
  const host = document.getElementById('definition-typed-editor-host');
  if (!host) return;
  const payload = state.payload;
  host.innerHTML = '<div class="comm-entry">'
    + field('Change classifications', textarea('typed-exec-classifications', (payload.change_classifications || []).join('\n'), 4))
    + field('Shared execution rules', textarea('typed-exec-shared-rules', (payload.shared_execution_rules || []).join('\n'), 5))
    + field('Executive defaults (key=role)', textarea('typed-exec-defaults', pairText(payload.executive_default_execution_role), 4))
    + field('Owner mappings (owner=role)', textarea('typed-exec-owner-map', pairText(payload.owner_to_execution_role), 5))
    + '</div>'
    + '<div class="section-title"><span>Execution Role contracts</span></div>'
    + '<div id="definition-typed-role-list">'
    + (payload.roles || []).map(executionRoleHtml).join('')
    + '</div>';
}

function render() {
  const addBinding = document.getElementById('definition-typed-add-binding');
  const addDelegation = document.getElementById('definition-typed-add-delegation');
  const retire = document.getElementById('definition-typed-retire');
  if (!state.source || !state.payload) {
    const host = document.getElementById('definition-typed-editor-host');
    if (host) host.innerHTML = '';
    if (addBinding) addBinding.hidden = true;
    if (addDelegation) addDelegation.hidden = true;
    if (retire) retire.hidden = true;
    return;
  }
  const authority = state.source.kind === 'authority-role-catalog';
  if (addBinding) addBinding.hidden = !authority;
  if (addDelegation) addDelegation.hidden = !authority;
  if (retire) retire.hidden = authority || state.source.lifecycle !== 'published';
  if (authority) renderAuthority();
  else renderExecution();
}

function readAuthorityGrant(node) {
  return {
    id: node.querySelector('.typed-grant-id').value.trim(),
    capability: node.querySelector('.typed-grant-capability').value.trim(),
    level: node.querySelector('.typed-grant-level').value,
    project_ids: csv(node.querySelector('.typed-grant-projects').value),
    resource_ids: csv(node.querySelector('.typed-grant-resource-ids').value),
    resource_types: csv(node.querySelector('.typed-grant-resource-types').value),
    resource_risks: csv(node.querySelector('.typed-grant-resource-risks').value),
    resource_sensitivities: csv(node.querySelector('.typed-grant-sensitivities').value),
    environments: csv(node.querySelector('.typed-grant-environments').value),
    max_amount_usd: numberOrNull(node.querySelector('.typed-grant-amount').value),
    max_input_tokens: numberOrNull(node.querySelector('.typed-grant-input-tokens').value),
    max_output_tokens: numberOrNull(node.querySelector('.typed-grant-output-tokens').value),
    max_model_calls: numberOrNull(node.querySelector('.typed-grant-model-calls').value),
    max_autonomous_risk: node.querySelector('.typed-grant-autonomy').value,
    approvals: {
      count: Number(node.querySelector('.typed-grant-approval-count').value || 0),
      role_ids: csv(node.querySelector('.typed-grant-approval-roles').value),
    },
  };
}

function captureAuthority() {
  const roles = [...document.querySelectorAll('.typed-authority-role')].map((node) => ({
    id: node.querySelector('.typed-role-id').value.trim(),
    name: node.querySelector('.typed-role-name').value.trim(),
    description: node.querySelector('.typed-role-description').value.trim(),
    lifecycle: node.querySelector('.typed-role-lifecycle').value,
    inherits: csv(node.querySelector('.typed-role-inherits').value),
    grants: [...node.querySelectorAll('.typed-authority-grant')].map(readAuthorityGrant),
  }));
  const bindings = [...document.querySelectorAll('.typed-authority-binding')].map((node) => ({
    id: node.querySelector('.typed-binding-id').value.trim(),
    role_id: node.querySelector('.typed-binding-role').value.trim(),
    subject_kind: node.querySelector('.typed-binding-kind').value,
    subject_id: node.querySelector('.typed-binding-subject').value.trim(),
    organization_id: node.querySelector('.typed-binding-org').value.trim() || null,
    workspace_id: node.querySelector('.typed-binding-workspace').value.trim() || null,
    project_ids: csv(node.querySelector('.typed-binding-projects').value),
  }));
  const delegations = [...document.querySelectorAll('.typed-authority-delegation')].map((node) => {
    const expiry = node.querySelector('.typed-delegation-expires').value;
    return {
      id: node.querySelector('.typed-delegation-id').value.trim(),
      role_id: node.querySelector('.typed-delegation-role').value.trim(),
      delegate_identity_id: node.querySelector('.typed-delegation-identity').value.trim(),
      delegated_by_identity_id: node.querySelector('.typed-delegation-by').value.trim(),
      organization_id: node.querySelector('.typed-delegation-org').value.trim() || null,
      workspace_id: node.querySelector('.typed-delegation-workspace').value.trim() || null,
      project_ids: csv(node.querySelector('.typed-delegation-projects').value),
      expires_at: expiry ? new Date(expiry).getTime() / 1000 : 0,
      reason: node.querySelector('.typed-delegation-reason').value.trim(),
    };
  });
  state.payload = { roles, bindings, delegations };
}

function captureExecution() {
  state.payload = {
    change_classifications: lines(document.querySelector('.typed-exec-classifications')?.value),
    shared_execution_rules: lines(document.querySelector('.typed-exec-shared-rules')?.value),
    executive_default_execution_role: pairs(document.querySelector('.typed-exec-defaults')?.value),
    owner_to_execution_role: pairs(document.querySelector('.typed-exec-owner-map')?.value),
    roles: [...document.querySelectorAll('.typed-execution-role')].map((node) => ({
      id: node.querySelector('.typed-role-id').value.trim(),
      name: node.querySelector('.typed-role-name').value.trim(),
      lane: node.querySelector('.typed-role-lane').value.trim(),
      description: node.querySelector('.typed-role-description').value.trim(),
      lifecycle: node.querySelector('.typed-role-lifecycle').value,
      expected_work: lines(node.querySelector('.typed-role-expected').value),
      must_refuse: lines(node.querySelector('.typed-role-refuse').value),
      required_artifacts: lines(node.querySelector('.typed-role-artifacts').value),
      hands_to: lines(node.querySelector('.typed-role-hands-to').value),
      failure_conditions: lines(node.querySelector('.typed-role-failures').value),
      keywords: lines(node.querySelector('.typed-role-keywords').value),
      auto_select: node.querySelector('.typed-role-auto-select').checked,
    })),
  };
}

function capture() {
  if (!state.source) return;
  if (state.source.kind === 'authority-role-catalog') captureAuthority();
  else captureExecution();
}

function uniqueId(prefix) {
  return prefix + '-' + Math.random().toString(16).slice(2, 10);
}

function newAuthorityRole() {
  return {
    id: uniqueId('role'),
    name: 'New Role',
    description: 'Describe this operational Role.',
    lifecycle: 'active',
    inherits: [],
    grants: [],
  };
}

function newExecutionRole() {
  return {
    id: uniqueId('role'),
    name: 'New Role',
    lane: 'implementation',
    description: 'Describe this execution contract.',
    lifecycle: 'active',
    expected_work: ['Define expected work.'],
    must_refuse: ['Refuse work outside this role.'],
    required_artifacts: ['Evidence appropriate to the work.'],
    hands_to: ['orchestrator'],
    failure_conditions: ['Escalate when the contract cannot be satisfied.'],
    keywords: [],
    auto_select: false,
  };
}

function addRole() {
  if (!state.source) return;
  capture();
  state.payload.roles.push(
    state.source.kind === 'authority-role-catalog' ? newAuthorityRole() : newExecutionRole(),
  );
  render();
}

function cloneRole(index) {
  capture();
  const source = state.payload.roles[index];
  if (!source) return;
  const suggested = source.id + '-copy';
  const id = window.prompt('New unique Role ID', suggested);
  if (!id?.trim()) return;
  const copy = clone(source);
  copy.id = id.trim();
  copy.name = source.name + ' copy';
  if (state.source.kind === 'authority-role-catalog') {
    copy.grants = (copy.grants || []).map((grant, grantIndex) => ({
      ...grant,
      id: copy.id + '.grant-' + (grantIndex + 1),
    }));
  }
  state.payload.roles.push(copy);
  render();
}

function removeRole(index) {
  capture();
  const role = state.payload.roles[index];
  if (!role) return;
  if (
    state.source.kind === 'execution-role-catalog'
    && STRUCTURAL_EXECUTION_ROLES.has(role.id)
  ) {
    setStatus('Required structural execution roles cannot be removed.');
    return;
  }
  if (!window.confirm('Remove Role ' + role.id + ' from this draft payload?')) return;
  state.payload.roles.splice(index, 1);
  if (state.source.kind === 'authority-role-catalog') {
    state.payload.bindings = (state.payload.bindings || []).filter((item) => item.role_id !== role.id);
    state.payload.delegations = (state.payload.delegations || []).filter((item) => item.role_id !== role.id);
    for (const item of state.payload.roles) {
      item.inherits = (item.inherits || []).filter((parent) => parent !== role.id);
    }
  } else {
    for (const mapping of ['executive_default_execution_role', 'owner_to_execution_role']) {
      for (const [key, value] of Object.entries(state.payload[mapping] || {})) {
        if (value === role.id) delete state.payload[mapping][key];
      }
    }
  }
  render();
}

function addGrant(roleIndex) {
  capture();
  const role = state.payload.roles[roleIndex];
  if (!role) return;
  role.grants = role.grants || [];
  role.grants.push({
    id: role.id + '.grant-' + (role.grants.length + 1),
    capability: 'resource.read',
    level: 'read',
    project_ids: [],
    resource_ids: [],
    resource_types: [],
    resource_risks: [],
    resource_sensitivities: [],
    environments: [],
    max_amount_usd: null,
    max_input_tokens: null,
    max_output_tokens: null,
    max_model_calls: null,
    max_autonomous_risk: 'low',
    approvals: { count: 0, role_ids: [] },
  });
  render();
}

function removeGrant(roleIndex, grantIndex) {
  capture();
  state.payload.roles[roleIndex]?.grants?.splice(grantIndex, 1);
  render();
}

function addBinding() {
  if (state.source?.kind !== 'authority-role-catalog') return;
  capture();
  state.payload.bindings.push({
    id: uniqueId('authority-binding'),
    role_id: state.payload.roles[0]?.id || '',
    subject_kind: 'identity',
    subject_id: '',
    organization_id: state.source.scope_type === 'organization' ? state.source.scope_id : null,
    workspace_id: state.source.scope_type === 'workspace' ? state.source.scope_id : null,
    project_ids: state.source.scope_type === 'project' ? [state.source.scope_id] : [],
  });
  render();
}

function addDelegation() {
  if (state.source?.kind !== 'authority-role-catalog') return;
  capture();
  state.payload.delegations.push({
    id: uniqueId('authority-delegation'),
    role_id: state.payload.roles[0]?.id || '',
    delegate_identity_id: '',
    delegated_by_identity_id: '',
    organization_id: null,
    workspace_id: null,
    project_ids: [],
    expires_at: Date.now() / 1000 + 86400,
    reason: 'Temporary delegated authority.',
  });
  render();
}

function removeIndexed(type, index) {
  capture();
  state.payload[type].splice(index, 1);
  render();
}

async function saveDraft() {
  if (!state.source) {
    setStatus('Select a supported source revision first.');
    return;
  }
  capture();
  const reason = document.getElementById('definition-typed-reason')?.value.trim();
  if (!reason) {
    setStatus('A draft reason is required.');
    return;
  }
  const response = await apiRequest('/api/definitions/drafts', {
    method: 'POST',
    body: JSON.stringify({
      definition_id: state.source.definition_id,
      kind: state.source.kind,
      definition_schema_version: state.source.definition_schema_version,
      scope_type: state.source.scope_type,
      scope_id: state.source.scope_id,
      payload: state.payload,
      reason,
      effective_from: state.source.effective_from,
      effective_until: state.source.effective_until,
      min_engine_version: state.source.min_engine_version,
      max_engine_version: state.source.max_engine_version,
      derived_from_record_id: state.source.record_id,
    }),
  });
  setStatus(
    'Created canonical draft r' + response.record.revision + ' · '
      + response.record.record_id + ' · checksum ' + response.record.checksum
      + '. Source remains ' + state.source.record_id + '; nothing was activated.',
  );
  document.getElementById('refresh-definitions')?.click();
}

async function retireDefinition() {
  if (!state.source || state.source.kind === 'authority-role-catalog') {
    setStatus('Authority catalogs must be disabled through an explicit restrictive draft.');
    return;
  }
  if (state.source.lifecycle !== 'published') {
    setStatus('Only the active published revision can be retired.');
    return;
  }
  const lifecycle = window.prompt('Retire as "deprecated" or "disabled"', 'deprecated');
  if (!['deprecated', 'disabled'].includes(lifecycle || '')) return;
  const reason = window.prompt('Retirement reason', '');
  if (!reason?.trim()) return;
  const response = await apiRequest(
    '/api/definitions/' + encodeURIComponent(state.source.record_id) + '/retire',
    {
      method: 'POST',
      body: JSON.stringify({
        lifecycle,
        reason: reason.trim(),
        expected_active_revision: state.source.revision,
      }),
    },
  );
  setStatus(
    'Created immutable r' + response.record.revision + ' ' + response.record.lifecycle
      + ' retirement revision · ' + response.record.record_id + '.',
  );
  document.getElementById('refresh-definitions')?.click();
}

function loadSource(recordId) {
  const record = state.records.find((item) => item.record_id === recordId) || null;
  state.source = record;
  state.payload = record ? clone(record.payload) : null;
  const meta = document.getElementById('definition-typed-source-meta');
  if (meta) {
    meta.textContent = record
      ? 'Source ' + record.record_id + ' · r' + record.revision + ' · checksum '
        + record.checksum + ' · ' + record.kind + ' · schema '
        + record.definition_schema_version + '. Typed save creates a derived draft only.'
      : 'Choose an authority-role or execution-role revision. Typed edits never activate directly.';
  }
  const reason = document.getElementById('definition-typed-reason');
  if (reason && record) reason.value = 'Typed revision derived from ' + record.record_id;
  render();
}

function populate(records) {
  state.records = records.filter((item) => SUPPORTED.has(item.kind));
  const select = document.getElementById('definition-typed-source');
  if (!select) return;
  const previous = state.source?.record_id || select.value;
  select.innerHTML = '<option value="">Select supported revision</option>'
    + state.records.map((record) => (
      '<option value="' + esc(record.record_id) + '">' + esc(sourceLabel(record)) + '</option>'
    )).join('');
  if (state.records.some((item) => item.record_id === previous)) {
    select.value = previous;
  } else {
    loadSource('');
  }
}

function handleEditorAction(event) {
  const button = event.target.closest?.('[data-typed-action]');
  if (!button || !state.source) return;
  const roleNode = button.closest('[data-role-index]');
  const grantNode = button.closest('[data-grant-index]');
  const bindingNode = button.closest('[data-binding-index]');
  const delegationNode = button.closest('[data-delegation-index]');
  const roleIndex = Number(roleNode?.dataset.roleIndex);
  const grantIndex = Number(grantNode?.dataset.grantIndex);
  const bindingIndex = Number(bindingNode?.dataset.bindingIndex);
  const delegationIndex = Number(delegationNode?.dataset.delegationIndex);
  const action = button.dataset.typedAction;
  if (action === 'clone-role') cloneRole(roleIndex);
  else if (action === 'remove-role') removeRole(roleIndex);
  else if (action === 'add-grant') addGrant(roleIndex);
  else if (action === 'remove-grant') removeGrant(roleIndex, grantIndex);
  else if (action === 'remove-binding') removeIndexed('bindings', bindingIndex);
  else if (action === 'remove-delegation') removeIndexed('delegations', delegationIndex);
}

function bind() {
  document.getElementById('definition-typed-source')?.addEventListener('change', (event) => {
    loadSource(event.target.value);
  });
  document.getElementById('definition-typed-add-role')?.addEventListener('click', addRole);
  document.getElementById('definition-typed-add-binding')?.addEventListener('click', addBinding);
  document.getElementById('definition-typed-add-delegation')?.addEventListener('click', addDelegation);
  document.getElementById('definition-typed-save')?.addEventListener('click', () => {
    saveDraft().catch((error) => setStatus('Typed draft failed: ' + error.message));
  });
  document.getElementById('definition-typed-retire')?.addEventListener('click', () => {
    retireDefinition().catch((error) => setStatus('Retirement failed: ' + error.message));
  });
  document.getElementById('definition-typed-editor-host')?.addEventListener('click', handleEditorAction);
  render();
}

window.addEventListener('codex:definition-registry-rendered', (event) => {
  populate(event.detail?.records || []);
});
window.addEventListener('DOMContentLoaded', bind);
