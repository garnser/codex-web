import { referenceLink } from './reference_navigation.js';
const esc = value => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;')
  .replaceAll('>', '&gt;').replaceAll('"', '&quot;');
const link = (kind, id, projectId) => referenceLink(kind, id, {projectId});
const definition = (ref, projectId) => ref
  ? `${link('definition', ref.record_id, projectId)} · ${esc(ref.definition_id)}@r${esc(ref.revision)}` : 'unavailable';

export function brokerAuditHtml(entry, projectId) {
  const target = entry.operation_id?.startsWith('work_item.')
    ? link('work_item', entry.target_ref, projectId) : esc(entry.target_ref || 'not recorded');
  return `<details class="comm-entry"><summary>${esc(entry.operation_id || entry.path)} · ${esc(entry.decision)} · HTTP ${esc(entry.response_status ?? 'unknown')}</summary>
    <small>Recorded target: ${target} · authenticated broker actor: ${esc(entry.actor_identity_id || 'not recorded')} · worker ${link('worker', entry.worker_id, projectId)} · fence ${esc(entry.fence)}</small>
    <small>Authority decision: ${esc(entry.authority_decision_id || 'not evaluated')} · definition ${definition(entry.authority_definition, projectId)}</small>
    <small>Reason: ${esc(entry.denial_reason || 'No denial recorded')} · correlation ${esc(entry.correlation_id)} · causation ${esc(entry.causation_id || 'none')}</small>
    <small>A broker admission is distinct from a provider result or recipient acknowledgement. Inspect ActionIntents for selected repository resources, receipts and verification.</small></details>`;
}
