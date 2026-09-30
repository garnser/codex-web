import { currentProjectId } from './project_view_scope.js';

// Review evidence only; server lifecycle authorization remains authoritative.
export async function catalogImpact(record, operation) {
  if (record.kind !== 'execution-profile-catalog') return '';
  const project = currentProjectId();
  if (!project) throw new Error('Select a Project to inspect Execution Profile consumers before changing the catalog');
  const query = `project_id=${encodeURIComponent(project)}`;
  const catalog = await operation.request(`/api/execution-profiles?${query}`);
  const ids = (catalog.items || []).map(item => item.id);
  if (!ids.length || ids.length > 25) throw new Error('Catalog impact unavailable for this bounded preview; review consumers before changing its lifecycle');
  let count = 0, restricted = 0;
  for (const id of ids) {
    if (!operation.current()) throw new DOMException('Project changed', 'AbortError');
    const usage = await operation.request(`/api/execution-profiles/${encodeURIComponent(id)}/usage?${query}`);
    if (!usage.available) throw new Error('Execution Profile consumer impact is unavailable');
    count += usage.count; restricted += usage.restricted_count;
  }
  if (!operation.current()) throw new DOMException('Project changed', 'AbortError');
  return ` Current Project ${project}: ${count} profile consumer reference(s), including ${restricted} restricted reference(s). Shared workspace Agent Profiles are included; sibling Project consumers are outside this preview. Existing executions retain pinned revisions; configured and queued consumers may resolve a changed catalog.`;
}
