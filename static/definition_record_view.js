import { referenceLink } from './reference_navigation.js';
import { referenceAttributes } from './reference_links.js';
function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

export function timeText(value) {
  if (!value) return "none";
  const date = new Date(Number(value) * 1000);
  return Number.isNaN(date.valueOf()) ? "unknown" : date.toLocaleString();
}

export function scopeText(record) {
  return record.scope_type === "global"
    ? "global"
    : `${record.scope_type}:${record.scope_id || "missing"}`;
}

function effectiveText(record) {
  const from = record.effective_from ? timeText(record.effective_from) : "immediate";
  const until = record.effective_until ? timeText(record.effective_until) : "no expiry";
  return `${from} → ${until}`;
}

function compatibilityText(record) {
  if (!record.min_engine_version && !record.max_engine_version) return "engine unrestricted";
  return `${record.min_engine_version || "any"} ≤ engine ≤ ${record.max_engine_version || "any"}`;
}

function provenanceHtml(record) {
  const links = [
    record.supersedes_record_id ? `supersedes ${referenceLink("definition", record.supersedes_record_id)}` : null,
    record.superseded_by_record_id ? `superseded by ${referenceLink("definition", record.superseded_by_record_id)}` : null,
    record.rollback_of_record_id ? `rollback of ${referenceLink("definition", record.rollback_of_record_id)}` : null,
  ].filter(Boolean);
  const approvals = Object.entries(record.approval_metadata || {})
    .map(([key, value]) => `${key}=${value}`)
    .join(" · ");
  return `<small>Created: ${escapeHtml(record.created_by)} · ${timeText(record.created_at)}${record.create_reason ? ` · reason: ${escapeHtml(record.create_reason)}` : ""}</small>
    <small>Validated: ${escapeHtml(record.validated_by || "none")} · ${timeText(record.validated_at)} · Published: ${escapeHtml(record.published_by || "none")} · ${timeText(record.published_at)}</small>
    ${record.publish_reason ? `<small>Publish/lifecycle reason: ${escapeHtml(record.publish_reason)}</small>` : ""}
    ${approvals ? `<small>Approvals: ${escapeHtml(approvals)}</small>` : ""}
    ${links.length ? `<small>Revision links: ${links.join(" · ")}</small>` : ""}`;
}

export function renderRecord(record) {
  const payload = JSON.stringify(record.payload || {}, null, 2);
  return `<details class="comm-entry" data-definition-record="${escapeHtml(record.record_id)}" data-definition-id="${escapeHtml(record.definition_id)}" data-reference-revision="${escapeHtml(record.revision)}" ${referenceAttributes("definition", record.record_id)}>
    <summary><strong>${escapeHtml(record.kind)} · ${escapeHtml(record.definition_id)} · r${escapeHtml(record.revision)} · ${escapeHtml(record.lifecycle)}</strong></summary>
    <small>Record: ${escapeHtml(record.record_id)} · Scope: ${escapeHtml(scopeText(record))} · Definition schema: ${escapeHtml(record.definition_schema_version)}</small>
    <small>Checksum: ${escapeHtml(record.checksum)} · Effective: ${escapeHtml(effectiveText(record))} · Compatibility: ${escapeHtml(compatibilityText(record))}</small>
    ${provenanceHtml(record)}
    <pre>${escapeHtml(payload)}</pre>
    <button type="button" class="ghost-button" data-definition-usage="${escapeHtml(record.record_id)}">Load usage/references</button>
    <div id="definition-usage-${escapeHtml(record.record_id)}"></div>
    <div data-definition-management-host="${escapeHtml(record.record_id)}"></div>
  </details>`;
}
