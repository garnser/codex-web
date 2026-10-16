// Canonical event/error observations only; absence never establishes readiness.
export function historyReadinessHtml(value, escapeHtml) {
  if (!value || typeof value !== 'object') return '';
  const esc = value => escapeHtml(String(value ?? 'not reported'));
  return `<section role="region" aria-label="Source history readiness"><strong>Source history: ${esc(value.history_state || value.state || 'unknown')} · ${esc(value.readiness || 'not ready')}</strong>
    <small>Target ${esc(value.revision || value.target_revision)} · local ${esc(value.local_revision)}</small>
    <small>Fetch ${esc(value.fetched === true ? 'performed' : value.fetched === false ? 'not needed' : 'unknown')} · bound ${esc(value.maximum_depth)} commits depth / ${esc(value.deadline_seconds)} seconds</small>
    <small>Next: ${esc(value.next_action)} · provenance ${esc(value.provenance || 'canonical broker observation')}</small></section>`;
}
export function brokerHistoryObservation(reason) {
  try { return JSON.parse(reason).history_readiness || null; } catch { return null; }
}
