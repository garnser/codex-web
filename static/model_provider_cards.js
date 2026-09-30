import { esc as escapeHtml, secretLinks, referenceAttributes } from './reference_links.js';
import { bindingPath } from './provider_binding_links.js';
export function providerCards(items, capacityRecords, timeText, listText) {
  return items.map((item) => {
      const capacity = capacityRecords.find((record) => (
        record.provider_id === item.id && !record.runtime_id
      ));
      const capacityLine = capacity
        ? `<small>Capacity: ${escapeHtml(capacity.status)} · retry/reset: ${timeText(capacity.retry_at)}${capacity.reason ? ` · ${escapeHtml(capacity.reason)}` : ""}</small>`
        : "<small>Capacity: no active throttle/depletion record</small>";
      return `<div class="comm-entry" ${referenceAttributes("model_provider", item.id)}>
      <strong>${escapeHtml(item.display_name)} · ${escapeHtml(item.status)}</strong>
      <small>ID: ${escapeHtml(item.id)} · Adapter: ${escapeHtml(item.adapter_type)} · Base URL: ${escapeHtml(item.base_url || "provider default")}</small>
      <small>Credential reference: ${secretLinks([item.credential_ref])} · Credential required: ${item.credential_required ? "yes" : "no"}</small>
      <small>Residency: ${listText(item.residency_tags)} · Compliance: ${listText(item.compliance_tags)}</small>
      ${capacityLine}
      ${bindingPath(item.id, "model") ? `<small><a href="${escapeHtml(bindingPath(item.id, "model"))}">Manage ModelGateway binding</a></small>` : ""}
      <small>Updated by: ${escapeHtml(item.updated_by)} · ${timeText(item.updated_at)}</small>
    </div>`;
    }).join("") || '<div class="comm-entry"><strong>No model providers registered.</strong></div>';
}
