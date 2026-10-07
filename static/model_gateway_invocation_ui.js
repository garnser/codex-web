import { invocationEligibilityHtml } from './model_gateway_eligibility_ui.js';

export function renderModelInvocations(items, { escapeHtml, timeText, listText, referenceLink, referenceAttributes, focusReference, limit = 50 }) {
  const host = document.getElementById("model-invocation-list");
  if (!host) return;
  host.innerHTML = items.slice(0, limit).map((item) => {
    const attempts = (item.attempts || []).map((attempt, index) => `<small>
      Attempt ${index + 1}: ${escapeHtml(attempt.provider_id)}/${escapeHtml(attempt.model_id)} · ${escapeHtml(attempt.outcome)}
      ${attempt.error_code ? ` · ${escapeHtml(attempt.error_code)}` : ""}
      · input ${escapeHtml(attempt.input_tokens ?? "n/a")} · output ${escapeHtml(attempt.output_tokens ?? "n/a")}
      · cost ${attempt.actual_cost == null && attempt.actual_cost_usd == null ? "n/a" : `${escapeHtml(attempt.actual_cost ?? attempt.actual_cost_usd)} ${escapeHtml(attempt.cost_currency || "USD")}${attempt.cost_source === "codex_calculated" ? " estimated" : ""}`}
    </small>`).join("");
    const refs = [
      item.work_item_ref ? `work=${item.work_item_ref}` : null,
      item.goal_id ? `goal=${item.goal_id}` : null,
      item.decision_id ? `decision=${item.decision_id}` : null,
      item.execution_id ? `execution=${item.execution_id}` : null,
    ].filter(Boolean).join(" · ");
    return `<details class="comm-entry" ${referenceAttributes("invocation", item.id)}>
      <summary><strong>${escapeHtml(item.model_class)} · ${escapeHtml(item.workload_class || "unspecified workload")} · ${escapeHtml(item.status)} · ${escapeHtml(item.purpose)}</strong></summary>
      <small>ID: ${escapeHtml(item.id)} · ${timeText(item.created_at)} · actor ${escapeHtml(item.actor_id)}</small>
      <small>Selected: ${referenceLink("model_provider", item.selected_provider_id)} / ${referenceLink("model", item.selected_model_id)} · ${escapeHtml(item.selected_concrete_model || "none")}${item.selected_model_version ? ` @ ${escapeHtml(item.selected_model_version)}` : ""}</small>
      ${invocationEligibilityHtml(item, escapeHtml)}
      <small>Template: ${escapeHtml(item.prompt_template_id)} @ ${escapeHtml(item.prompt_template_version)} · checksum ${escapeHtml(item.prompt_template_checksum_sha256)}</small>
      <small>Route reason: ${escapeHtml(item.route_reason)} · Policy fingerprint: ${escapeHtml(item.policy_fingerprint_sha256)}</small>
      <small>Routing definition: ${escapeHtml(item.routing_definition_id || "legacy class routing")} @ ${escapeHtml(item.routing_definition_revision ?? "n/a")} · role ${escapeHtml(item.routing_role || "primary")} · qualification ${escapeHtml(item.qualification_revision || "not mapped")}</small>
      <small>Catalog: ${escapeHtml(item.selected_catalog_revision || "static")} · discovered ${timeText(item.selected_catalog_discovered_at)} · upstream ${escapeHtml(item.selected_upstream_provider_id || "unknown")} / ${escapeHtml(item.selected_upstream_model_id || "unknown")}</small>
      <small>Override: ${escapeHtml(item.pinned_model_id || "automatic")} · Preferred latency: ${listText(item.preferred_latency_classes)} · Prefer lower cost: ${item.prefer_lower_cost ? "yes" : "no"}</small>
      <small>Capabilities: ${listText(item.required_capabilities)} · Residency: ${listText(item.required_residency_tags)} · Compliance: ${listText(item.required_compliance_tags)} · Max cost: ${item.max_cost_usd == null ? "none" : `$${escapeHtml(item.max_cost_usd)}`}</small>
      ${refs ? `<small>References: ${escapeHtml(refs)}</small>` : ""}${attempts}
    </details>`;
  }).join("") || '<div class="comm-entry"><strong>No model invocation metadata.</strong><small>Prompt/message content is not stored in this feed.</small></div>';
  focusReference(host);
}
