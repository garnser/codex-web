// Canonical event/error observations only; absence never establishes readiness.
export function historyReadinessHtml(value, escapeHtml) {
  if (!value || typeof value !== 'object') return '';
  const esc = value => escapeHtml(String(value ?? 'not reported'));
  const complete = value.history_state === 'complete' && value.readiness === 'ready';
  return `<section role="region" aria-label="Source history readiness"><strong>Source history: ${esc(value.history_state || value.state || 'unknown')} · ${complete ? 'ready' : 'not ready'}</strong>
    <small>Target ${esc(value.revision || value.target_revision)} · local ${esc(value.local_revision)}</small>
    <small>Fetch ${esc(value.fetched === true ? 'performed' : value.fetched === false ? 'not needed' : 'unknown')} · bound ${esc(value.maximum_depth)} commits depth / ${esc(value.deadline_seconds)} seconds</small>
    <small>Next: ${esc(value.next_action)} · provenance ${esc(value.provenance || 'canonical broker observation')}</small></section>`;
}
export function brokerHistoryObservation(reason) {
  try { return JSON.parse(reason)?.history_readiness || null; } catch { return null; }
}

// Workspace event details remain scalar-only. Old readers can display/ignore
// these additional keys; newer readers require the explicit supported shape.
export function workspaceHistoryObservation(details) {
  if (!details || details.history_version !== 1) return null;
  const sha = value => typeof value === 'string' && /^[0-9a-f]{40}$/.test(value);
  if (!sha(details.history_target_revision) || !sha(details.history_local_revision)
      || typeof details.materialization_fetched !== 'boolean'
      || details.history_state !== 'complete' || details.history_readiness !== 'ready'
      || details.history_next_action !== 'refresh_exact_revision'
      || !Number.isSafeInteger(details.history_maximum_depth) || details.history_maximum_depth <= 0
      || !Number.isSafeInteger(details.history_deadline_seconds) || details.history_deadline_seconds <= 0
      || typeof details.history_provenance !== 'string') return null;
  return {history_state:details.history_state, readiness:details.history_readiness,
    revision:details.history_target_revision, local_revision:details.history_local_revision,
    fetched:details.materialization_fetched, next_action:details.history_next_action,
    maximum_depth:details.history_maximum_depth, deadline_seconds:details.history_deadline_seconds,
    provenance:details.history_provenance};
}
