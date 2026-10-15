import { esc as escapeHtml, referenceAttributes } from './reference_links.js';
import { supervisionObservationText } from './runtime_supervision_ui.js';
import { referenceLink } from './reference_navigation.js';
const listText = values => values?.length ? values.map(escapeHtml).join(', ') : 'none';

export function timeText(value) {
    if (!value) return "none";
    const date = new Date(Number(value) * 1000);
    return Number.isNaN(date.valueOf()) ? "unknown" : date.toLocaleString();
  }


export function workerDetailsHtml(worker, active) {
      const trustWarning = worker.lifecycle === "active"
        ? "Eligible for assignment subject to capability/concurrency checks."
        : worker.lifecycle === "draining"
          ? "No new work should be assigned; existing fenced leases may continue."
          : "Not trusted for execution.";
      return `<details class="comm-entry" ${referenceAttributes("worker", worker.id)}>
        <summary><strong>${escapeHtml(worker.pool)} · ${escapeHtml(worker.id)} · ${escapeHtml(worker.lifecycle)}</strong></summary>
        <small>Service identity: ${escapeHtml(worker.service_identity_id)} · version: ${escapeHtml(worker.version)} · registered by: ${escapeHtml(worker.registered_by)}</small>
        <small>Capabilities: ${listText(worker.capabilities)} · max concurrency: ${escapeHtml(worker.max_concurrency)} · active leased assignments: ${active}</small>
        <small>Registered: ${timeText(worker.registered_at)} · last heartbeat: ${timeText(worker.last_heartbeat_at)} · revoked: ${timeText(worker.revoked_at)}</small>
        ${worker.quarantine_reason ? `<small>Quarantine reason: ${escapeHtml(worker.quarantine_reason)}</small>` : ""}
        <small>${escapeHtml(trustWarning)}</small>
        <div data-execution-worker-management-host="${escapeHtml(worker.id)}"></div>
      </details>`;
}

// Present canonical worker events separately from refresh/mutation lifecycle.
export function workerEventsHtml(events) {
  return events.slice(0, 100).map(item => {
    const observation = supervisionObservationText(item);
    return `<div class="comm-entry">
      <strong>${escapeHtml(item.event_type)} · ${timeText(item.occurred_at)}</strong>
      <small>Worker: ${escapeHtml(item.worker_id || "none")} · assignment: ${escapeHtml(item.assignment_id || "none")} · actor: ${escapeHtml(item.actor_id || "system")}</small>
      <small>Details: ${escapeHtml(JSON.stringify(item.details || {}))}</small>
      ${observation ? `<small>${escapeHtml(observation)}</small>` : ""}
    </div>`;
  }).join("") || '<div class="comm-entry"><strong>No worker events recorded.</strong></div>';
}
