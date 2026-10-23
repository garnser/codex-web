import { readinessSelection } from './execution_profile_controls.js';

const referenceFields = ['record_id', 'definition_id', 'kind', 'revision', 'checksum', 'definition_schema_version'];
const reference = value => referenceFields.map(field => value?.[field]);
const key = value => value && JSON.stringify([value.project_id, value.profile_id, value.sandbox, reference(value.definition)]);

// One current advisory response, not execution authority or a readiness ledger.
// Every real input still enters canonical identity/profile/worker admission.
export function createExecutionReadinessGate({read, getProjectId, doc = document}) {
  let generic = {}, selectedKey = null, result = null, error = '', controller = null, generation = 0;
  function selection() {
    const value = readinessSelection();
    return value?.project_id === getProjectId() && reference(value.definition).every(x => x != null) ? value : null;
  }
  function render() {
    const contextual = Boolean(doc.getElementById('execution-profile'));
    const current = selection();
    const ready = !generic.loading && generic.projectId === getProjectId()
      && (contextual ? current && key(current) === selectedKey && result?.execution_ready === true
        : generic.readiness?.execution_ready === true);
    const message = contextual ? error || (result
      ? result.execution_ready ? 'Selected execution profile ready.'
        : 'Selected execution profile blocked: ' + (result.checks || [])
          .filter(item => item.required && item.status === 'blocked').slice(0, 3).map(item => item.message).join(' ')
      : 'Checking readiness for the selected execution profile…')
      : 'Project readiness is blocked. Open Project Setup for remediation.';
    for (const id of ['new-thread', 'send', 'prompt']) {
      const node = doc.getElementById(id);
      if (node) { node.disabled = !ready; node.setAttribute('aria-disabled', String(!ready)); node.title = ready ? '' : message; }
    }
    const summary = doc.getElementById('execution-profile-summary');
    if (summary) {
      let status = doc.getElementById('execution-readiness-status');
      if (!status) { status = doc.createElement('p'); status.id = 'execution-readiness-status'; status.setAttribute('role', 'status'); summary.after(status); }
      status.textContent = message;
    }
  }
  async function refresh({force = false} = {}) {
    const current = selection(), nextKey = key(current);
    if (!doc.getElementById('execution-profile')) { render(); return; }
    if (!force && nextKey && nextKey === selectedKey && (controller || result || error)) { render(); return; }
    controller?.abort(); controller = null; generation += 1;
    selectedKey = nextKey; result = null; error = '';
    if (!current) { error = 'Select an available execution profile and sandbox.'; render(); return; }
    const visit = generation, requestController = new AbortController(); controller = requestController; render();
    const stillCurrent = () => visit === generation && key(selection()) === nextKey;
    try {
      const query = new URLSearchParams({execution_profile_id:current.profile_id, sandbox:current.sandbox});
      const value = await read(`/api/projects/${encodeURIComponent(current.project_id)}/readiness?${query}`,
        {signal:requestController.signal});
      if (!stillCurrent()) return;
      const context = value.execution_context;
      if (value.project_id !== current.project_id || context?.profile_id !== current.profile_id
          || context?.sandbox !== current.sandbox || JSON.stringify(reference(context.definition)) !== JSON.stringify(reference(current.definition))) {
        throw new Error('Execution profile changed. Refresh this Project before sending.');
      }
      result = value;
    } catch (failure) {
      if (!stillCurrent() || failure?.name === 'AbortError') return;
      error = 'Selected execution profile readiness unavailable. Refresh this Project.';
    } finally {
      if (controller === requestController) controller = null;
      if (stillCurrent()) render();
    }
  }
  window.addEventListener('codex:execution-context-changed', () => { void refresh(); });
  doc.addEventListener('change', event => {
    if (['execution-profile', 'sandbox'].includes(event.target?.id)) void refresh();
  });
  return {update(value) { generic = value; void refresh(); }, refresh};
}
