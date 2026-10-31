// Request construction only. Canonical APIs own scope and governance decisions.
export function canAdmitDerivedArtifact(actor) {
  if (!actor) return false;
  if (actor.principal_kind === 'service') return (actor.service_scopes || []).includes('artifact-evidence:admin');
  return ['mfa', 'local_trusted'].includes(actor.assurance)
    && (actor.roles || []).some(role => ['owner', 'admin'].includes(role));
}
export function derivedArtifactRequest(values) {
  const text = key => String(values[key] || '').trim();
  const ids = key => [...new Set(text(key).split(/[\s,]+/).filter(Boolean))];
  const parents = ids('source_evidence_ids'), resources = ids('resource_ids');
  if (!parents.length || parents.length > 64 || !resources.length || resources.length > 64) throw new Error('Enter 1–64 parent Evidence IDs and target resources.');
  if (!/^[a-fA-F0-9]{64}$/.test(text('digest'))) throw new Error('Enter the exact approved document SHA-256.');
  for (const key of ['project_id', 'work_item_ref', 'name', 'idempotency_key']) if (!text(key)) throw new Error('Project, Work, name and idempotency key are required.');
  if (text('idempotency_key').length > 200) throw new Error('Idempotency key exceeds 200 characters.');
  return {artifact: {project_id: text('project_id'), work_item_ref: text('work_item_ref'),
    resource_ids: resources, artifact_type: 'report', name: text('name'),
    source: 'operator-derived-input', digest: {algorithm: 'sha256', value: text('digest').toLowerCase()}},
  source_evidence_ids: parents, idempotency_key: text('idempotency_key')};
}
