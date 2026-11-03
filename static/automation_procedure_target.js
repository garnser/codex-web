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
