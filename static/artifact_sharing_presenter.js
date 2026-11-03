import { esc as escapeHtml } from './reference_links.js';
import { referenceLink } from './reference_navigation.js';

// Only canonical identifiers and bounded Definition provenance are presented.
// Current read eligibility remains an authenticated server decision.
export function artifactSharingHtml(item) {
  if (item.provider !== 'governed-artifact-sharing.v1') return '';
  const metadata = item.metadata || {};
  const id = value => typeof value === 'string' && /^[A-Za-z0-9_.:/!@-]{1,512}$/.test(value) ? value : null;
  const link = (kind, value) => id(value) ? referenceLink(kind, value, {projectId:item.project_id}) : 'unavailable';
  let policy = 'unavailable';
  try {
    const ref = JSON.parse(metadata.sharing_policy_ref);
    if (ref.kind === 'artifact-sharing-policy' && ref.definition_id === 'artifact.sharing.default'
        && id(ref.record_id) && Number.isInteger(ref.revision) && ref.revision > 0
        && /^[a-f0-9]{64}$/.test(ref.checksum)) {
      policy = `${escapeHtml(ref.record_id)} r${ref.revision} · ${escapeHtml(ref.checksum)}`;
    }
  } catch (_) { /* Invalid provenance is never interpreted as authority. */ }
  return `<small>Governed shared source information · INFO only; no independent QA or repository execution grant.</small>
    <small>Source Artifact: ${link('artifact', metadata.source_artifact_id)} · source Evidence: ${link('evidence', metadata.source_evidence_id)} · source Work: ${link('work_item', metadata.source_work_item_ref)}</small>
    <small>Creation policy: ${policy} · rule: ${escapeHtml(id(metadata.sharing_rule_id) || 'unavailable')}. Current policy, source lifecycle and recipient access are revalidated by the server.</small>`;
}
