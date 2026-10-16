import { mergeRequirementsFormHtml } from './merge_requirements_view.js';
import { coordinationDeclarationHtml } from './project_coordination_view.js';
import { referenceLink, projectPath } from './reference_navigation.js';
const esc = value => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;')
  .replaceAll('>', '&gt;').replaceAll('"', '&quot;');
const link = (kind, id, projectId) => referenceLink(kind, id, {projectId});
const definition = (ref, projectId) => ref
  ? `${link('definition', ref.record_id, projectId)} · ${esc(ref.definition_id)}@r${esc(ref.revision)}` : 'unavailable';

export { brokerAuditHtml } from './broker_audit_view.js';

export function brokerDeclarationHtml(item, projectId) {
  const capability = item.brokered_control_plane || {};
  const context = capability.assignment_context || {};
  const admin = context.declared_administrative_scope;
  const pinned = context.agent_profile || item.agent_profile;
  const current = admin?.current_agent_profile;
  const observation = admin?.configuration_observation;
  const evidence = (capability.operations || []).find(op => op.id === 'repository.read.delivery-evidence');
  return `<small><strong>Brokered control plane:</strong> ${capability.enabled ? 'configured' : 'disabled'} · Project ${esc(item.project_id)} · execution profile ${link('execution_profile', item.execution_profile_id, projectId)}</small>
    <small>Credential material exposed: ${capability.credential_exposed === false ? 'no' : capability.credential_exposed === true ? 'unexpected exposure reported' : 'not reported'}.</small>
    <small>Declared operation allowlist: ${esc((capability.operations || []).map(op => op.id).join(', ') || 'none')}. Each request requires current canonical authority and a live fenced lease.</small>
    ${evidence ? `<small>Delivery evidence: read-only · supported actions ${esc((evidence.supported_action_ids || []).join(', ') || 'not reported')} · scoped path ${esc(evidence.path_template)}. Historical receipts require matching requester and repository scope; missing receipts or timestamps alone do not prove provider outcome.</small>` : ''}
    ${pinned ? `<small>Pinned Agent Profile: ${link('agent_profile', pinned.profile_id, projectId)}@r${esc(pinned.profile_revision)} · record ${esc(pinned.profile_record_id)} · Agent Role ${esc(pinned.role_id || 'none')}</small>` : '<small>Pinned Agent Profile: unavailable.</small>'}
    ${admin ? `<small>Declared administrative repository targets: ${(admin.repository_ids || []).map(id => link('resource', id, projectId)).join(', ') || 'none'}. These declarations grant no Git workspace access.</small>
      <small>Pinned execution definition: ${definition(admin.definition, projectId)} · current published definition: ${definition(admin.current_definition, projectId)} · ${admin.definition_is_current === true ? 'matches pin' : admin.definition_is_current === false ? 'changed / stale pin' : 'unavailable'}</small>
      <small>Operational authority Role: ${esc(admin.authority_role_id || 'missing')} · pinned Role definition ${definition(admin.authority_definition, projectId)} · assignment requester ${esc(admin.requester_identity_id || 'unavailable')}</small>
      <small>Current Agent Profile: ${current ? `${esc(current.profile_id)}@r${esc(current.revision)} · record ${esc(current.record_id)} · lifecycle ${esc(current.lifecycle)} · Agent Role ${esc(current.role_id)} · authority Role ${esc(current.authority_role_id)}` : 'unavailable'}</small>
      <small>Observed declaration validation: ${esc(observation?.status || 'unavailable')} · ${esc(observation?.reason || admin.inspection_error || 'No validation observation')}${observation?.operation_id ? ` · sampled operation ${esc(observation.operation_id)} / repository ${link('resource', observation.repository_id, projectId)}` : ''}. This observation grants no operation authority.</small>` : ''}
    <small>Git scope: writable ${(context.declared_repository_scope?.writable_repository_ids || []).map(id => link('resource', id, projectId)).join(', ') || 'none'} · read context ${(context.declared_repository_scope?.read_only_repository_ids || []).map(id => link('resource', id, projectId)).join(', ') || 'none'}</small>
    <small>Source Work Item: ${link('work_item', item.work_item_ref, projectId)} · <a href="${esc(projectPath('work-items', {}, projectId))}#work-graph-panel" data-broker-graph>Inspect dependency graph</a> · ${item.execution_id ? `<a href="${esc(projectPath('automations', {action_execution: item.execution_id}, projectId))}">Inspect related ActionIntents and provider receipts</a>` : 'No execution correlation available'}</small>
    ${coordinationDeclarationHtml(context.project_coordination, projectId)}
    ${mergeRequirementsFormHtml(context)}
    <button type="button" class="ghost-button" data-broker-audit>Load broker audit for ${esc(item.id)}</button><div role="status" aria-live="polite" data-broker-status>Broker audit has not been loaded.</div><div data-broker-events></div>`
}
