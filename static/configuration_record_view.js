import { referenceLink } from './reference_navigation.js';
import { referenceAttributes } from './reference_links.js';
export function renderConfigurationRecord(record, { specFor, escapeHtml, timeText, scopeText, valueText, targetingHtml }) {
  const spec = specFor(record.key);
  const links = [
    record.supersedes_id ? `supersedes ${referenceLink("configuration", record.supersedes_id)}` : null,
    record.superseded_by_id ? `superseded by ${referenceLink("configuration", record.superseded_by_id)}` : null,
    record.rollback_of_id ? `rollback of ${referenceLink("configuration", record.rollback_of_id)}` : null,
  ].filter(Boolean);
  return `<details class="comm-entry" data-configuration-record="${escapeHtml(record.id)}" data-configuration-key="${escapeHtml(record.key)}" data-reference-revision="${escapeHtml(record.revision)}" ${referenceAttributes("configuration", record.id)}>
    <summary><strong>${escapeHtml(record.key)} · r${escapeHtml(record.revision)} · ${escapeHtml(record.state)} · ${escapeHtml(scopeText(record))}</strong></summary>
    <small>Record: ${escapeHtml(record.id)} · Type: ${escapeHtml(spec?.value_kind || "unknown")} · Value: ${escapeHtml(valueText(record.value, spec))}</small>
    <small>Created by: ${escapeHtml(record.created_by)} · ${timeText(record.created_at)}${record.create_reason ? ` · reason: ${escapeHtml(record.create_reason)}` : ""}</small>
    <small>Published by: ${escapeHtml(record.published_by || "none")} · ${timeText(record.published_at)}${record.publish_reason ? ` · reason: ${escapeHtml(record.publish_reason)}` : ""}</small>
    ${spec?.value_kind === "secret_ref" ? referenceLink("secret", record.value?.secret_id) : ""}
    ${targetingHtml(record)}
    <small>Force-disabled kill switch: ${record.force_disabled ? "yes" : "no"} · Hot reloadable: ${spec?.hot_reloadable ? "yes" : "no"} · Startup only: ${spec?.startup_only ? "yes" : "no"}</small>
    ${links.length ? `<small>Revision links: ${links.join(" · ")}</small>` : ""}
    <button type="button" class="ghost-button" data-configuration-impact="${escapeHtml(record.id)}">Load impact preview</button>
    <div id="configuration-impact-${escapeHtml(record.id)}"></div>
    <div data-configuration-management-host="${escapeHtml(record.id)}"></div>
  </details>`;
}
