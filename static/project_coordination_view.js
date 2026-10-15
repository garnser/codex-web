import { referenceLink } from './reference_navigation.js';
const esc = value => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;')
  .replaceAll('>', '&gt;').replaceAll('"', '&quot;');
const link = (kind, id, projectId) => referenceLink(kind, id, {projectId});
const definition = (ref, projectId) => ref
  ? `${link('definition', ref.record_id, projectId)} · ${esc(ref.definition_id)}@r${esc(ref.revision)}` : 'unavailable';

export function coordinationDeclarationHtml(coordination, projectId) {
  return coordination ? `<details data-project-coordination><summary>Project governor scope and coordination</summary>
      <small>Explicit repository: ${link('resource', coordination.declaration?.repository_resource_id, projectId)} · delivery-thread bound ${esc(coordination.declaration?.max_delivery_threads)}. Declarations do not grant authority.</small>
      <small>Pinned definition: ${definition(coordination.definition, projectId)} · current ${definition(coordination.current_definition, projectId)} · ${coordination.definition_is_current ? 'matches pin' : 'changed / stale pin'}</small>
      <small>Allowed child profiles: ${(coordination.declaration?.child_profiles || []).map(p => `${link('agent_profile',p.profile_id,projectId)}@r${esc(p.revision)}`).join(', ') || 'none'}. No inherited governor rights.</small>
      ${(coordination.operations || []).map(op => `<small>${esc(op.id)} · required ${esc(op.capability)} · declared ${op.declared ? 'yes' : 'no'} · authority observation ${esc(op.status || 'unknown')} · ${esc(op.reason || 'unavailable')}</small>`).join('')}
      <small>Queued receipts do not prove native start or completion. Elevation requests never grant permissions. Operator views use current canonical services and independently check access.</small>
      <button type="button" class="ghost-button" data-coordination-view="threads">Inspect current threads</button>
      <button type="button" class="ghost-button" data-coordination-view="elevation">Inspect elevation feedback</button>
      <button type="button" class="ghost-button" data-coordination-view="memory">Inspect project checkpoints</button>
      <div role="status" aria-live="polite" data-coordination-status>Coordination has not been loaded.</div><div data-coordination-results></div>
      </details>` : '';
}

export function coordinationResultsHtml(view, rows, projectId) {
  return rows.map(row => `<details><summary>${esc(row.thread_id || row.summary || row.kind)} · ${esc(row.activity || row.status || row.freshness || 'unknown')}</summary>
    <small>${view === 'threads' ? `Thread ${esc(row.thread_id)} · assignment ${esc(row.assignment_id)} · workspace ${esc(row.execution_workspace_id)} · queue ${esc(row.queue_depth)} · blocker ${esc(row.blocker || 'none')} · execution ${esc(row.active_execution_id || 'idle / unknown')}` : ''}</small>
    <small>${view === 'memory' ? `Checkpoint ${link('knowledge',row.id,projectId)} · version ${esc(row.version)} · ${esc(row.content_digest)} · source ${esc(row.provenance?.source_ref)}` : ''}</small>
    <small>${view === 'elevation' ? `${row.kind === 'approval' ? link('approval',row.id,projectId) : row.kind === 'attention' ? link('attention',row.id,projectId) : esc(row.id)} · actor ${esc(row.actor || 'unavailable')} · correlation ${esc(row.correlation_id || 'see canonical source')} · ${esc(row.reason || 'Inspect canonical policy and approval')} · next: ${esc(row.next_action)}` : ''}</small>
    </details>`).join('');
}
