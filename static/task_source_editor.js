import { renderProjectDelivery } from './project_delivery_status.js';
import { confirmAction } from './action_confirmation.js';
import { request } from './api_client.js';
import { captureProjectView } from './project_view_scope.js';
import { trackDirtyEditor, confirmDiscard } from './dirty_editor.js';
import { formValidation } from './form_validation.js';
import { renderSecretOptions } from './task_source_credentials.js';
import { renderProviderDetails, captureProviderDetails } from './task_source_provider_fields.js';

export function createTaskSourceEditor(state, { refreshAll, setStatus, esc, fmtTime }) {
  let root, dirty, validation, lastProject = '', epoch = 0, busy = false;
  const field = name => root.querySelector(`.work-source-${name}`);
  function capture() {
    const projectId = state.projectId, visit = epoch, view = captureProjectView();
    return { projectId, current: () => projectId === state.projectId && visit === epoch && view.current() };
  }
  function setBusy(value) {
    busy = value;
    root?.querySelectorAll('input,select,button').forEach(control => { control.disabled = value || !state.projectId; });
  }
  function bind(container) {
    root = container; validation = formValidation(root);
    const delivery = document.createElement('div');
    delivery.className = 'work-source-grid work-source-delivery';
    delivery.innerHTML = '<label>Delivery thread ID <input class="work-source-delivery-thread" placeholder="Leave empty to disable automatic delivery" /></label><p class="work-source-delivery-status" aria-live="polite"></p>';
    root.appendChild(delivery);

    const provenance = document.createElement('p'); provenance.dataset.sourceProvenance = ''; root.prepend(provenance);
    const discard = document.createElement('button'); discard.type = 'button'; discard.textContent = 'Discard source edits';
    discard.addEventListener('click', () => { if (confirmDiscard(dirty)) renderSourceConfig(); });
    root.querySelector('.work-source-actions').appendChild(discard);
    for (const name of ['type', 'instance', 'scope']) field(name).required = true;
    dirty = trackDirtyEditor(root, { label: 'Project TaskSource' });
    field('type').addEventListener('change', event => renderProviderFields(event.target.value));
    field('save').addEventListener('click', saveSource); field('clear').addEventListener('click', clearSource);
  }
function selectedProject() {
  return state.projects.find((project) => project.id === state.projectId) || null;
}
function catalogEntry(sourceType) {
  const entries = Array.isArray(state.catalog?.items) ? state.catalog.items : [];
  return entries.find((entry) => entry.project_id === state.projectId && entry.source_type === sourceType)
    || entries.find((entry) => entry.project_id == null && entry.source_type === sourceType)
    || null;
}
function renderProviderFields(sourceType, config = null) {
  renderProviderDetails(root, state.catalog?.configuration_schema, sourceType, config?.provider_settings || {});
  const normalized = String(sourceType || '').toLowerCase();
  const wrapper = document.querySelector('.work-source-provider-fields');
  const secretRow = document.querySelector('.work-source-secret-row');
  const jiraRow = document.querySelector('.work-source-jira-username-row');
  const tableRow = document.querySelector('.work-source-servicenow-table-row');
  const activeRow = document.querySelector('.work-source-servicenow-active-row');
  const closedRow = document.querySelector('.work-source-servicenow-closed-row');
  const isJira = normalized === 'jira';
  const isServiceNow = normalized === 'servicenow';
  const needsCredential = isJira || isServiceNow || normalized === 'gitlab' || Boolean(config?.credential_secret_id);
  if (wrapper) wrapper.hidden = !needsCredential;
  if (secretRow) secretRow.hidden = !needsCredential;
  if (jiraRow) jiraRow.hidden = !isJira;
  if (tableRow) tableRow.hidden = !isServiceNow;
  if (activeRow) activeRow.hidden = !isServiceNow;
  if (closedRow) closedRow.hidden = !isServiceNow;
  renderSecretOptions(document.querySelector('.work-source-secret'), state.secrets, config?.credential_secret_id || '', state.projectId);
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
  validation?.clear();
  if (lastProject !== state.projectId) { epoch += 1; lastProject = state.projectId; busy = false; }
  setBusy(false);
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
    ...(Array.isArray(state.catalog?.items) ? state.catalog.items.filter(item => item.project_id == null || item.project_id === state.projectId).map(item => item.source_type) : []),
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
  root.querySelector('[data-source-provenance]').textContent = config
    ? `Project ${state.projectId} explicitly binds ${config.source_type} at ${config.source_instance}, scope ${config.scope}. This singular Project field determines authoritative TaskSource selection.`
    : `Project ${state.projectId} has no explicit authoritative TaskSource. Adapter availability is not a Project binding. Clearing this field does not delete provider issues or revoke credentials; server readiness determines whether execution is available.`;
  document.querySelector('.work-source-capabilities').textContent = [
    entry ? `Adapter: ${entry.available ? 'ready' : 'not ready'}` : 'Adapter not registered',
    caps.length ? `Capabilities: ${caps.join(', ')}` : 'Capabilities unavailable',
    entry?.error ? `Adapter error: ${entry.error}` : '',
    sync.last_success_at ? `Shared synchronizer last success: ${fmtTime(sync.last_success_at)}` : 'Shared synchronizer has no recorded success',
    sync.last_error ? `Shared synchronizer last error: ${sync.last_error}` : '',
  ].filter(Boolean).join(' · ');
  renderProjectDelivery(project, field, capture, fmtTime);
  dirty?.markSaved();
}
async function saveSource() {
  if (!state.projectId || busy || !validation.validate()) return;
  const op = capture();
  const source_type = document.querySelector('.work-source-type').value.trim();
  const source_instance = document.querySelector('.work-source-instance').value.trim();
  const scope = document.querySelector('.work-source-scope').value.trim();
  if (!source_type || !source_instance || !scope) {
    setStatus('Source type, instance and scope are required', true);
    return;
  }
  const payload = { source_type, source_instance, scope };
  const credential_secret_id = document.querySelector('.work-source-secret').value.trim();
  if (credential_secret_id) payload.credential_secret_id = credential_secret_id;
  if (source_type === 'jira' || source_type === 'servicenow') {
    if (!credential_secret_id) {
      validation.show([{ field: field('secret'), message: 'Select a usable canonical SecretReference for this provider.' }]);
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
      const previous = selectedProject()?.authoritative_task_source?.provider_settings;
      const details = captureProviderDetails(root, previous?.kind === 'servicenow' ? previous : {});
      if (active) details.canonical_state_values.implementation_active = active;
      else delete details.canonical_state_values.implementation_active;
      if (closed) details.canonical_state_values.closed = closed;
      else delete details.canonical_state_values.closed;
      payload.provider_settings = { kind: 'servicenow', table, ...details };
    }
  }
  setStatus('Saving source…'); setBusy(true);
  try {
    await request(`/api/projects/${encodeURIComponent(op.projectId)}/task-source`, {
      method: 'PUT',
      body: JSON.stringify(payload),
    });
    if (!op.current()) return;
    const thread_id = field('delivery-thread').value.trim() || null;
    if (thread_id || selectedProject()?.delivery_supervision) {
      await request(`/api/projects/${encodeURIComponent(op.projectId)}/delivery-supervision`, {
        method: 'PUT', body: JSON.stringify({ thread_id, enabled: Boolean(thread_id) }),
      });
      if (!op.current()) return;
    }
    dirty.markSaved(); await refreshAll({ preserveSource: false });
    if (op.current()) setStatus('Authoritative source saved');
  } catch (error) {
    if (op.current()) { validation.server(error, { source_type: field('type'), source_instance: field('instance'), scope: field('scope'), credential_secret_id: field('secret') }); setStatus(error.message || 'Failed to save source', true); }
  } finally { if (op.current()) setBusy(false); }
}
async function clearSource() {
  if (!state.projectId || busy || !confirmDiscard(dirty)) return;
  const op = capture();
  if (!await confirmAction({ action: 'Clear TaskSource binding', target: `Project ${op.projectId}`, consequence: 'Readiness will be reevaluated; this editor will not infer a new source. Provider issues and shared credentials remain.', recovery: 'Rebind the Project through its TaskSource settings when ready.', current: op.current, trigger: field('clear') })) return;
  setBusy(true); setStatus('Clearing source…');
  try {
    await request(`/api/projects/${encodeURIComponent(op.projectId)}/task-source`, { method: 'DELETE' });
    if (!op.current()) return;
    dirty.markSaved(); await refreshAll({ preserveSource: false });
    if (op.current()) setStatus('Authoritative source cleared');
  } catch (error) { if (op.current()) { validation.server(error); setStatus(error.message || 'Failed to clear source', true); } }
  finally { if (op.current()) setBusy(false); }
}
  return { bind, render: renderSourceConfig, capture,
    canLeave: () => confirmDiscard(dirty), isDirty: () => Boolean(dirty?.dirty()),
    projectChanged() { epoch += 1; busy = false; dirty?.discard(); if (root) { root.querySelectorAll('input,select').forEach(control => { control.value = ''; }); renderSourceConfig(); } },
  };
}
