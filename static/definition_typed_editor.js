import { request as apiRequest } from './api_client.js';
import {
  captureAuthority,
  newBinding,
  newDelegation,
  newGrant,
  newRole as newAuthorityRole,
  renderAuthority,
} from './definition_typed_authority_editor.js';
import {
  captureExecution,
  newRole as newExecutionRole,
  renderExecution,
  STRUCTURAL_EXECUTION_ROLES,
} from './definition_typed_execution_editor.js';
import { clone, esc } from './definition_typed_editor_shared.js';

const SUPPORTED = new Set(['authority-role-catalog', 'execution-role-catalog']);
const state = {
  records: [],
  source: null,
  payload: null,
};

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

function capture() {
  if (!state.source) return;
  state.payload = state.source.kind === 'authority-role-catalog'
    ? captureAuthority()
    : captureExecution();
}

function nextId(prefix, values) {
  const existing = new Set(values.filter(Boolean));
  let index = existing.size + 1;
  let candidate = prefix + '-' + index;
  while (existing.has(candidate)) {
    index += 1;
    candidate = prefix + '-' + index;
  }
  return candidate;
}

function render() {
  const host = document.getElementById('definition-typed-editor-host');
  const addBinding = document.getElementById('definition-typed-add-binding');
  const addDelegation = document.getElementById('definition-typed-add-delegation');
  if (!host) return;
  if (!state.source || !state.payload) {
    host.innerHTML = '';
    if (addBinding) addBinding.hidden = true;
    if (addDelegation) addDelegation.hidden = true;
    return;
  }
  const authority = state.source.kind === 'authority-role-catalog';
  if (addBinding) addBinding.hidden = !authority;
  if (addDelegation) addDelegation.hidden = !authority;
  if (authority) renderAuthority(state.payload, host);
  else renderExecution(state.payload, host);
}

function addRole() {
  if (!state.source) return;
  capture();
  const id = nextId('role', (state.payload.roles || []).map((item) => item.id));
  state.payload.roles.push(
    state.source.kind === 'authority-role-catalog'
      ? newAuthorityRole(id)
      : newExecutionRole(id),
  );
  render();
}

function cloneRole(index) {
  capture();
  const source = state.payload.roles[index];
  if (!source) return;
  const id = window.prompt('New unique Role ID', source.id + '-copy');
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
    state.payload.bindings = (state.payload.bindings || [])
      .filter((item) => item.role_id !== role.id);
    state.payload.delegations = (state.payload.delegations || [])
      .filter((item) => item.role_id !== role.id);
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
  if (state.source?.kind !== 'authority-role-catalog') return;
  capture();
  const role = state.payload.roles[roleIndex];
  if (!role) return;
  role.grants = role.grants || [];
  role.grants.push(newGrant(role, role.grants.length + 1));
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
  const ids = (state.payload.bindings || []).map((item) => item.id);
  const id = nextId('authority-binding', ids);
  state.payload.bindings.push(
    newBinding(id, state.payload.roles[0]?.id || '', state.source),
  );
  render();
}

function addDelegation() {
  if (state.source?.kind !== 'authority-role-catalog') return;
  capture();
  const ids = (state.payload.delegations || []).map((item) => item.id);
  const id = nextId('authority-delegation', ids);
  state.payload.delegations.push(
    newDelegation(id, state.payload.roles[0]?.id || ''),
  );
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
  const roleIndex = Number(button.closest('[data-role-index]')?.dataset.roleIndex);
  const grantIndex = Number(button.closest('[data-grant-index]')?.dataset.grantIndex);
  const bindingIndex = Number(button.closest('[data-binding-index]')?.dataset.bindingIndex);
  const delegationIndex = Number(button.closest('[data-delegation-index]')?.dataset.delegationIndex);
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
  document.getElementById('definition-typed-editor-host')?.addEventListener('click', handleEditorAction);
  render();
}

window.addEventListener('codex:definition-registry-rendered', (event) => {
  populate(event.detail?.records || []);
});
window.addEventListener('DOMContentLoaded', bind);
