import { esc as escapeHtml } from './reference_links.js';
import { referenceLink } from './reference_navigation.js';
import { apiBase } from './api_client.js';
const BASE = apiBase();

export function timeText(value) {
    if (!value) return "none";
    const date = new Date(Number(value) * 1000);
    return Number.isNaN(date.valueOf()) ? "unknown" : date.toLocaleString();
  }


export function listText(values) {
    return values?.length ? values.map(escapeHtml).join(", ") : "none";
  }


export function safeLink(value) {
    if (!value) return "";
    try {
      const url = new URL(value, window.location.origin);
      if (!["http:", "https:"].includes(url.protocol)) return "";
      return `<a href="${escapeHtml(url.href)}" target="_blank" rel="noopener noreferrer">open source</a>`;
    } catch (_) {
      return "";
    }
  }


export function governanceHtml(record) {
    if (!record) {
      return "<small>Governance metadata: not linked/backfilled.</small>";
    }
    const hold = record.legal_hold_at
      ? `legal hold since ${timeText(record.legal_hold_at)} by ${record.legal_hold_by || "unknown"}`
      : "no legal hold";
    return `<small>Governance: ${escapeHtml(record.classification)} · ${escapeHtml(record.lifecycle)} · retention ${escapeHtml(record.retention_action)} at ${timeText(record.retention_expires_at)} · ${escapeHtml(hold)}</small>
      <small>Residency: ${listText(record.residency_tags)} · deny model context: ${record.deny_model_context ? "yes" : "no"} · governance ID: ${escapeHtml(record.id)}</small><small>Source governance records: ${listText(record.source_record_ids)}</small>`;
  }


export function researchLinks(item) {
    const project = item.project_id || document.body?.dataset.projectId || document.body?.dataset.activeProject;
    const route = project ? `${BASE}/projects/${encodeURIComponent(project)}/memory` : `${BASE}/`;
    const snapshots = new Map((item.research_sources || []).map(source => [source.knowledge_id, source]));
    const ids = [...new Set([...(item.knowledge_ids || []), ...snapshots.keys()])];
    return ids.map(id => {
      const source = snapshots.get(id);
      const label = source ? `${id}@v${source.version}: ${source.origin}` : id;
      return `<a href="${route}?knowledge_id=${encodeURIComponent(id)}">${escapeHtml(label)}</a>`;
    }).join(', ') || 'none';
  }

export function observationPolicyHtml(item) {
  const metadata = item.metadata || {};
  if (!metadata.observation_policy) return '';
  try {
    const policy = JSON.parse(metadata.observation_policy);
    if (policy.version !== '2.0' || !Number.isInteger(policy.seconds) || policy.seconds < 1 || policy.seconds > 300) throw new Error('invalid policy');
    const origin = policy.source === 'default' ? 'code default' : `${policy.record_id} r${policy.revision}`;
    return `<small>Observation v2: ${escapeHtml(policy.seconds)}s · Configuration ${escapeHtml(policy.key)} · ${escapeHtml(origin)} · resolved ${timeText(policy.resolved_at)} · digest ${escapeHtml(policy.record_sha256 || 'default')}</small>
      <small>Observation expiry ${metadata.observation_expires_at ? timeText(metadata.observation_expires_at) : 'see linked Evidence'}. Historical provenance only; current use and revocation are revalidated by the server.</small>`;
  } catch (_) {
    return '<small>Observation policy provenance is invalid; server validation is required.</small>';
  }
}

export { continuationInputHtml } from './mr_continuation_input_presenter.js';

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
