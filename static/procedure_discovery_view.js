import { discoveryGuidanceHtml, discoveryCandidatesHtml, listText } from './procedure_discovery_guidance.js';
import { esc as escapeHtml } from './reference_links.js';
import { referenceLink } from './reference_navigation.js';
export function procedureDiscoveriesHtml(intent, projectId) {
  return (intent?.procedure_discoveries || []).map((item) => `<div class="comm-entry">
    <strong>Procedure discovery · ${escapeHtml(item.outcome)}</strong>
    ${discoveryGuidanceHtml(item)}
    <small>Scope: ${escapeHtml(item.organization_id)} / ${escapeHtml(item.workspace_id)} / ${escapeHtml(item.project_id || "none")} · actor ${escapeHtml(item.actor_identity_id)} · operation ${escapeHtml(item.operation || "unresolved")}</small>
    <small>Targets: ${(item.resource_ids || []).map(id => referenceLink('resource', id, {projectId:item.project_id || projectId})).join(', ') || 'none'} · context checksum ${escapeHtml(item.context_checksum)}</small>
    <small>Selected: ${(item.selected || []).map((ref) => `${referenceLink("definition", ref.record_id, {label: `${ref.definition_id}@${ref.revision}`, projectId: intent?.project_id || projectId})} · ${escapeHtml(ref.record_id)} · ${escapeHtml(ref.checksum)}`).join("; ") || "none"}</small>
    <small>Policy: ${escapeHtml(item.policy_source)} · ${item.policy_allows ? "permits" : "blocks"} · blockers: ${listText(item.blockers)}</small>
    <small>Capability evidence: ${listText(item.capability_sources)} · Sources: ${listText(item.supporting_sources)} · instructions checksum ${escapeHtml(item.instruction_checksum || "none")}</small>
    ${discoveryCandidatesHtml(item.candidates, intent?.project_id || projectId)}
        </div>`).join("");
}

export function procedureHistoryHtml(history, projectId) {
  const intent = history.intent || {};
  const receipts = history.receipts || [];
  const verifications = history.verifications || [];
  const inbox = history.inbox || [];
  const rollback = intent.rollback_checkpoint;
  const rollbackTrace = rollback ? `<div class="comm-entry">
    <strong>Originating-intent rollback · ${escapeHtml(rollback.status)}</strong>
    <small>Origin receipt: ${escapeHtml(rollback.origin_receipt_id)} · rollback receipt: ${escapeHtml(rollback.receipt_id || 'pending reconciliation')}</small>
    <small>${escapeHtml(rollback.error_code || 'Inspect declared restoration Evidence before reporting recovery.')}</small>
  </div>` : '';
  return rollbackTrace + `${intent.procedure_deviation_reason ? `<small>Procedure deviation rationale (request data; grants no authority): ${escapeHtml(intent.procedure_deviation_reason)}</small>` : ""}` + `<div class="comm-entry">
        <strong>Intent → provider execution → receipt → verification/reconciliation</strong>
        <small>${receipts.length} provider receipt(s) · ${verifications.length} verification receipt(s) · ${inbox.length} callback message(s)</small>
      </div>` + procedureDiscoveriesHtml(intent, projectId);
}
