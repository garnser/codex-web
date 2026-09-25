export function renderEffectiveConfiguration(effective, helpers) {
  const { escapeHtml, timeText, valueText, spec } = helpers;
  const scope = (item) => item.scope_type
    ? `${item.scope_type}${item.scope_id ? `:${item.scope_id}` : ""}`
    : "code default";
  const chain = (effective.resolution_chain || []).map((item) => `
    <div class="comm-entry">
      <strong>${item.selected ? "Selected" : "Fallback"} · ${escapeHtml(scope(item))}</strong>
      <small>Value: ${escapeHtml(valueText(item.value, spec))} · revision: ${escapeHtml(item.revision ?? "none")}</small>
      <small>Published by: ${escapeHtml(item.published_by || "none")} · ${timeText(item.published_at)}${item.publish_reason ? ` · reason: ${escapeHtml(item.publish_reason)}` : ""}</small>
      ${item.force_disabled ? "<small>Force-disabled kill switch.</small>" : ""}
    </div>
  `).join("");

  return `<div class="comm-entry">
    <strong>${escapeHtml(effective.key)} · source ${escapeHtml(effective.source)} · reason ${escapeHtml(effective.reason)}</strong>
    <small>Effective value: ${escapeHtml(valueText(effective.value, spec))}</small>
    <small>Record: ${escapeHtml(effective.record_id || "default/unset")} · revision: ${escapeHtml(effective.revision ?? "none")} · scope: ${escapeHtml(effective.scope_type || "default")}${effective.scope_id ? `:${escapeHtml(effective.scope_id)}` : ""}</small>
    <small>Published by: ${escapeHtml(effective.published_by || "none")} · ${timeText(effective.published_at)}${effective.publish_reason ? ` · reason: ${escapeHtml(effective.publish_reason)}` : ""}</small>
    <small>Hot reloadable: ${effective.hot_reloadable ? "yes" : "no"} · Startup only: ${effective.startup_only ? "yes" : "no"} · Feature flag: ${effective.feature_flag ? "yes" : "no"}</small>
    <details>
      <summary>Resolution chain · ${effective.resolution_chain?.length || 0} step(s)</summary>
      ${chain || "<small>No resolution provenance returned.</small>"}
    </details>
  </div>`;
}
