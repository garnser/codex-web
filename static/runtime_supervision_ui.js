// A process observation describes diagnosis, never completion or lease authority.
export function supervisionObservationText(event) {
  if (event?.event_type !== 'agent_runtime_supervision_failed') return '';
  const detail = event.details || {};
  const evidence = detail.evidence_id ? `Evidence: ${detail.evidence_id}` : 'Diagnostic evidence unavailable';
  const observation = detail.phase ? ` · phase ${detail.phase} · cleanup ${detail.cleanup_state || 'unknown'} · validation custody ${detail.validation_custody_state || 'unavailable'}` : '';
  return `Process supervision: ${detail.failure_code || 'unknown'} · observed fence ${detail.observed_fence ?? 'unknown'} · current fence ${detail.current_fence ?? 'unknown'} · canonical state ${detail.current_status || 'unknown'} · ${evidence}${observation} · completion not asserted`;
}
