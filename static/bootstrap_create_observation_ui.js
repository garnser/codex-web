import { escapeHtml } from './role_admin_shared.js';
import { timeText } from './execution_worker_presenters.js';

export function bootstrapCreateObservationHtml(item) {
  if (item.subject?.kind !== 'thread_bootstrap') return '';
  const observation = item.bootstrap_create_observation;
  return `<section aria-label="Bootstrap lifecycle observations">
    <small>Native creation: ${escapeHtml(observation?.outcome || 'unknown')} · native session: ${escapeHtml(observation?.provider_native_session_id || 'unknown')}</small>
    <small>Original process cleanup: ${observation?.cleanup_joined_at != null ? `joined ${timeText(observation.cleanup_joined_at)}` : 'Unknown'}</small>
    <small>Original creation correlation: ${escapeHtml(observation?.rpc_request_id || 'unknown')} · process generation: ${escapeHtml(observation?.runtime_generation || 'unknown')} · original fence: ${escapeHtml(observation?.fence ?? 'unknown')}</small>
    <small>Creation is not a turn START or ACK. Process cleanup does not prove no effects or grant retry/input.</small>
  </section>`;
}
