import { esc } from './definition_typed_editor_shared.js';
import { referencePath } from './reference_navigation.js';

export function renderProcedureHistory(host, runs, selectedRun, currentRun, assignmentId, projectId) {
  const historySelect = host.querySelector('[data-procedure-history]');
  historySelect.innerHTML = runs.map(pin => `<option value="${esc(pin.id)}">${esc(pin.id)} · ${esc(pin.status)}${pin.id === currentRun ? ' · current' : ' · retained'}</option>`).join('');
  historySelect.value = selectedRun;
  const historical = selectedRun && selectedRun !== currentRun;
  host.querySelector('[data-procedure-history-status]').textContent = historical ? 'Retained run: inspection and outcome review only. Advancing never changes this checkpoint.' : 'Current run. A changed input starts a separate admitted run only after verified completion.';
  for (const button of host.querySelectorAll('[data-procedure-action="advance"], [data-procedure-deviation] button')) button.disabled = historical;
  const link = host.querySelector('[data-procedure-run-link]');
  const path = referencePath('assignment', assignmentId, {projectId});
  link.href = `${path}&${new URLSearchParams({procedure_run:selectedRun})}`;
}

export function historyPins(assignment) {
  return [...(assignment.procedure_history || []), assignment.procedure_checkpoint]
    .filter(Boolean).map(pin => ({...pin, id: pin.id || 'legacy'}));
}
