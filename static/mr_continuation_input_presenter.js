import { esc as escapeHtml } from './reference_links.js';
import { referenceLink } from './reference_navigation.js';

// Fixed scalar provenance only: never render provider_snapshot_json or other payload metadata.
export function continuationInputHtml(item) {
  const metadata = item.metadata || {};
  const input = ['code-host-mr-procedure-input.v1', 'code-host-mr-procedure-input.v2'].includes(metadata.input_method);
  const observation = ['code-host-mr-continuation-input.v1', 'code-host-mr-continuation-input.v2'].includes(metadata.verification_method);
  if (!input && !observation) return '';
  const options = {projectId: item.project_id};
  const validRef = value => typeof value === 'string' && value.length > 0
    && value.length <= 200 && !/[\x00-\x1f\x7f]/.test(value);
  const link = (kind, value) => validRef(value) ? referenceLink(kind, value, options) : 'unavailable';
  if (!input) {
    return `<small>Continuation input Artifact: ${link('artifact', metadata.continuation_input_artifact_id)}. Inspect its semantic identity and source Evidence; this observation grants no QA or merge approval.</small>`;
  }
  const fingerprint = typeof metadata.observed_requirements_fingerprint === 'string'
    && /^[a-f0-9]{64}$/.test(metadata.observed_requirements_fingerprint)
    ? metadata.observed_requirements_fingerprint : 'unavailable';
  let sources = null;
  try {
    if (typeof metadata.source_evidence_ids === 'string' && metadata.source_evidence_ids.length <= 4096) {
      const values = JSON.parse(metadata.source_evidence_ids);
      if (Array.isArray(values) && values.length === 3 && values.every(validRef)) sources = values;
    }
  } catch (_) { /* Missing or malformed provenance remains unavailable. */ }
  return `<small>Governed continuation input: ${escapeHtml(metadata.input_method)} · semantic fingerprint: ${escapeHtml(fingerprint)}</small>
    <small>Source Evidence: ${sources ? sources.map(id => link('evidence', id)).join(', ') : 'unavailable'} · Verification: ${link('verification', metadata.verification_id)}</small>
    <small>Historical provenance only; current scope, freshness and revocation are checked by the server. No QA or merge approval.</small>`;
}
