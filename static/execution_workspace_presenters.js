export function createWorkspacePresenters({escapeHtml, resourceNames, timeText, bytes}) {
  function integrationHtml(workspace) {
    const integration = workspace.integration || {};
    return `<small>Integration: ${escapeHtml(integration.strategy || "none")} · outcome ${escapeHtml(integration.outcome || "pending")} · target ${escapeHtml(integration.target_revision || "none")} · result ${escapeHtml(integration.resulting_revision || "none")}</small>
      ${integration.conflicts?.length ? `<small>Conflicts: ${integration.conflicts.map(escapeHtml).join(" · ")}</small>` : ""}
      <small>Integration recorded by: ${escapeHtml(integration.recorded_by || "none")} · ${timeText(integration.recorded_at)}</small>`;
  }

  function repositoryMembersHtml(workspace) {
    const members = workspace.repository_members || [];
    if (!members.length) {
      return "<small>Repository members: legacy/single-repository projection.</small>";
    }
    return `<div class="comm-log">${members.map((member) => `<div class="comm-entry">
      <strong>${escapeHtml(resourceNames([member.resource_id]))} · ${escapeHtml(member.access_mode)}</strong>
      <small>Source: ${escapeHtml(member.source_path)} · workspace: ${escapeHtml(member.workspace_path)}</small>
      <small>Sandbox path: ${escapeHtml(member.sandbox_path)} · branch: ${escapeHtml(member.branch_name || "detached/read-only")}</small>
      <small>Revision: ${escapeHtml(member.base_revision)} → ${escapeHtml(member.head_revision)} · disk ${bytes(member.disk_bytes)}</small>
    </div>`).join("")}</div>`;
  }

  return {integrationHtml, repositoryMembersHtml};
}
