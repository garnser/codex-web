import { esc as escapeHtml } from './reference_links.js';
import { referenceLink } from './reference_navigation.js';
import { timeText } from './execution_worker_presenters.js';

export function nativeCredentialCustodyHtml(item) {
    const custody = item.native_credential_custody;
    if (custody == null) {
      return '<section aria-label="Native credential custody"><small>Native credential custody: legacy unknown. No admission record is retained.</small></section>';
    }
    const admitted = custody.schema_version === "1.0"
      && typeof custody.admitted_at === "number"
      && Number.isFinite(custody.admitted_at) && custody.admitted_at > 0;
    const pending = custody.schema_version === "1.0" && custody.admitted_at == null;
    const state = admitted ? "admitted (recorded)" : pending ? "pending" : "unknown";
    const procedures = (custody.procedures || []).map(ref => (
      `${referenceLink("definition", ref.record_id, { label: ref.definition_id })} · kind ${escapeHtml(ref.kind)} · revision ${escapeHtml(ref.revision)} · schema ${escapeHtml(ref.definition_schema_version)} · checksum ${escapeHtml(ref.checksum)}`
    )).join("; ") || "none recorded";
    const bindings = Object.entries(custody.provider_bindings || {}).map(([id, checksum]) => (
      `${escapeHtml(id)} · checksum ${escapeHtml(checksum)}`
    )).join("; ") || "none recorded";
    return `<section aria-label="Native credential custody">
      <small>Native credential custody: ${state} · schema ${escapeHtml(custody.schema_version)}. This record does not grant current authority.</small>
      <small>Runtime authentication reference: ${custody.runtime_secret_ref ? referenceLink("secret", custody.runtime_secret_ref) : "none recorded; no credential grant inferred"}</small>
      <small>Procedure credential references: ${(custody.procedure_secret_refs || []).map(id => referenceLink("secret", id)).join(", ") || "none recorded"}</small>
      <small>Admission: ${admitted ? timeText(custody.admitted_at) : "not established"} · requester: ${referenceLink("identity", custody.requester_identity_id)} · worker identity: ${referenceLink("identity", custody.worker_identity_id)} · fence: ${escapeHtml(custody.fence ?? "not recorded")}</small>
      <small>Admitted procedures: ${procedures}</small>
      <small>Provider binding pins: ${bindings}</small>
    </section>`;
  }
