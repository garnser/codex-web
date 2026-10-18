export function workspaceQuotaText(quota) {
  if (!quota) return "Effective workspace limits unavailable.";
  const limit = value => Number.isSafeInteger(value) && value > 0 ? value : "unknown";
  return `Effective limits: ${limit(quota.max_active_per_identity)} active per identity · ${limit(quota.max_active_per_tenant)} active per tenant · ${limit(quota.max_resources_per_workspace)} resources per workspace · ${limit(quota.max_requested_disk_bytes)} requested bytes per workspace.`;
}

export function createWorkspacePresenters({escapeHtml, resourceNames, timeText, bytes}) {
  function integrationHtml(workspace) {
    const integration = workspace.integration || {};
    return `<small>Integration: ${escapeHtml(integration.strategy || "none")} · outcome ${escapeHtml(integration.outcome || "pending")} · target ${escapeHtml(integration.target_revision || "none")} · result ${escapeHtml(integration.resulting_revision || "none")}</small>
      ${integration.conflicts?.length ? `<small>Conflicts: ${integration.conflicts.map(escapeHtml).join(" · ")}</small>` : ""}
      <small>Integration recorded by: ${escapeHtml(integration.recorded_by || "none")} · ${timeText(integration.recorded_at)}</small>`;
  }

  function repositoryMembersHtml(workspace) {
    const context = workspace.resource_context;
    const contextHtml = context ? `<small>Read-only Resource context (${escapeHtml(context.schema_version)}): ${(context.read_only_resource_ids || []).map(id => `${escapeHtml(resourceNames([id]))} · READ`).join(' · ')} · original requester ${escapeHtml(context.requester_identity_id)} · Work Item ${escapeHtml(context.work_item_ref)}. Canonical workspace/lease provenance; no credentials, network or data mount.</small>` : '';
    const members = workspace.repository_members || [];
    if (!members.length) {
      return contextHtml + "<small>Repository members: legacy/single-repository projection.</small>";
    }
    return contextHtml + `<div class="comm-log">${members.map((member) => `<div class="comm-entry">
      <strong>${escapeHtml(resourceNames([member.resource_id]))} · ${escapeHtml(member.access_mode)}</strong>
      <small>Source: ${escapeHtml(member.source_path)} · workspace: ${escapeHtml(member.workspace_path)}</small>
      <small>Sandbox path: ${escapeHtml(member.sandbox_path)} · branch: ${escapeHtml(member.branch_name || "detached/read-only")}</small>
      <small>Revision: ${escapeHtml(member.base_revision)} → ${escapeHtml(member.head_revision)} · disk ${bytes(member.disk_bytes)}</small>
    </div>`).join("")}</div>`;
  }

  return {integrationHtml, repositoryMembersHtml};
}
