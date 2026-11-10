// Additive canonical target fields; the server owns validation and permission.
export function procedureTargetFields(target, esc) {
  return `<fieldset><legend>Canonical procedure event target</legend>
    <p>Use an exact published procedure reference and a current supervisor Role. The supervisor must already have the required scope. Only Work events are supported; model budgets must be zero. Waiting and unknown outcomes retain the original checkpoint.</p>
    <label>Procedure Definition reference<textarea name="procedure_ref">${esc(target.procedure_ref ? JSON.stringify(target.procedure_ref) : '')}</textarea></label>
    <label>Supervisor Role<input name="supervisor_role" value="${esc(target.supervisor_role || '')}" /></label>
    <label>Maximum advances<input name="procedure_max_advances" type="number" min="1" max="64" value="${esc(target.max_advances ?? '')}" /></label>
  </fieldset>`;
}

export function readProcedureTarget(form) {
  if (form.elements.target_kind.value !== 'canonical_procedure') return {};
  return {procedure_ref: JSON.parse(form.elements.procedure_ref.value),
    supervisor_role: form.elements.supervisor_role.value.trim(),
    max_advances: Number(form.elements.procedure_max_advances.value)};
}

export function procedureRunSummary(run, esc) {
  const pin = run.procedure_custody;
  return pin ? `<p>Procedure checkpoint ${esc(pin.checkpoint_id || 'not yet started')} · ${esc(pin.advances_started)} advance(s) · ${esc(run.result_code || run.status)}</p>` : '';
}


export function retiredProcedureAction(run, esc) {
  return run.status === 'running' && run.target_kind === 'canonical_procedure'
    ? `<button type="button" class="ghost-button" data-procedure-retirement="${esc(run.id)}">Cancel retired procedure scheduling</button>` : '';
}

const retiringRuns = new Set();

export function wireRetiredProcedureActions(card, {projectContext, confirmAction, api, refresh, error, render}) {
  card.querySelectorAll('[data-procedure-retirement]').forEach(button => {
    button.addEventListener('click', async () => {
      const context = projectContext();
      const id = button.dataset.procedureRetirement;
      const key = `${context.projectId}:${id}`;
      if (button.disabled || retiringRuns.has(key)) return;
      retiringRuns.add(key);
      button.disabled = true;
      try {
        if (!await confirmAction({action: 'Cancel retired procedure scheduling', target: id, risk: 'high',
          consequence: 'The server must prove the original bootstrap is cancelled and its effects are settled. This cancels scheduling only; the checkpoint and historical effects remain unchanged.',
          recovery: 'This does not complete, roll back, transfer or replay the procedure. Unknown effects require their original owner.'}) || !context.current()) return;
        await api(`/api/automation-runs/${encodeURIComponent(id)}/reconcile`, {
          method: 'POST', body: JSON.stringify({cancel_retired_procedure: true})});
        if (context.current()) await refresh();
      } catch (failure) {
        if (context.current()) error(failure.message);
      } finally {
        retiringRuns.delete(key);
        if (context.current()) { button.disabled = false; render(); }
      }
    });
  });
}
