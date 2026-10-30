import { esc as escapeHtml } from './reference_links.js';
export function timeText(value) {
  if (!value) return "none";
  const date = new Date(Number(value) * 1000);
  return Number.isNaN(date.valueOf()) ? "unknown" : date.toLocaleString();
}

export function listText(values) {
  return values?.length ? values.map(escapeHtml).join(", ") : "none";
}

export function decisionHtml(label, decision) {
  return `<small>${escapeHtml(label)}: ${escapeHtml(decision?.outcome || "not_evaluated")} · source ${escapeHtml(decision?.source || "unknown")} · decision ${escapeHtml(decision?.decision_id || "none")} · reason ${escapeHtml(decision?.reason || "none")} · evaluated ${timeText(decision?.evaluated_at)}</small>`;
}

export function leaseHtml(lease) {
  if (!lease) return "<small>Worker lease: none.</small>";
  return `<small>Worker lease: owner ${escapeHtml(lease.owner)} · acquired ${timeText(lease.acquired_at)} · renewed ${timeText(lease.renewed_at)} · expires ${timeText(lease.expires_at)}</small>`;
}

export function failureHtml(failure) {
  if (!failure) return "";
  const d = failure.details || {};
  const phases = {execute_admission: 'Execution admission', provider_execution: 'Provider execution (including guards)', result_admission_check: 'Result admission check', result_admission: 'Result admission'};
  let phase = '';
  if (typeof d.timeout_phase === 'string' && Object.hasOwn(phases, d.timeout_phase)) {
    const times = [['phase_elapsed_seconds', 'elapsed'], ['shared_remaining_at_phase_entry', 'remaining at entry'], ['shared_remaining_at_timeout', 'remaining at timeout']]
      .filter(([key]) => Number.isFinite(d[key]) && d[key] >= 0)
      .map(([key, label]) => `${label} ${escapeHtml(Number(d[key].toFixed(2)))}s`);
    phase = `<small>Timeout phase: ${escapeHtml(phases[d.timeout_phase])}${times.length ? ' · ' + times.join(' · ') : ''}</small> `;
  }
  return `${phase}<small>Canonical failure: ${escapeHtml(failure.reason_code || "unclassified")} · outcome ${escapeHtml(failure.outcome || "unknown")} · retry ${escapeHtml(failure.retryability || "unknown")} · remediation ${escapeHtml(failure.remediation_key || "failure.inspect")} · ${escapeHtml(failure.summary || "No summary recorded.")}</small>`;
}

export function recoveryHtml(intent, retry) {
  const attempt = Number(intent.attempt);
  const maxAttempts = Number(retry.max_attempts);
  const exhausted = Number.isFinite(attempt) && Number.isFinite(maxAttempts)
    && attempt >= maxAttempts;
  if (!exhausted || !["failed", "uncertain", "requires_reconciliation"].includes(intent.status)) return "";
  return "<small>Recovery: retry authority is exhausted and this intent is not claimable. Reconcile the provider outcome, then create a newly authorized replacement ActionIntent if delivery is still required; the replacement must pass current policy and provider gates.</small>";
}
