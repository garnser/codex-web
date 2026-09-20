(async () => {
  const BASE = window.location.pathname.startsWith("/codex") ? "/codex" : "";
  const { request: apiRequest } = await import(`${BASE}/static/api_client.js`);
  let actor = null;
  let inspections = [];
  let projects = [];
  let resources = [];

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;").replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;").replaceAll('"', "&quot;");
  }

  function timeText(value) {
    if (!value) return "none";
    const date = new Date(Number(value) * 1000);
    return Number.isNaN(date.valueOf()) ? "unknown" : date.toLocaleString();
  }

  function bytes(value) {
    const size = Number(value || 0);
    if (!Number.isFinite(size)) return "unknown";
    if (size >= 1024 ** 3) return `${(size / 1024 ** 3).toFixed(2)} GiB`;
    if (size >= 1024 ** 2) return `${(size / 1024 ** 2).toFixed(1)} MiB`;
    if (size >= 1024) return `${(size / 1024).toFixed(1)} KiB`;
    return `${size} B`;
  }

  function setStatus(message) {
    const host = document.getElementById("execution-workspace-status");
    if (host) {
      host.hidden = false;
      host.textContent = message;
    }
  }

  function projectName(id) {
    const item = projects.find((value) => value.id === id);
    return item ? `${item.name} (${id})` : id || "none";
  }

  function resourceNames(ids) {
    const byId = new Map(resources.map((item) => [item.id, item]));
    return (ids || []).map((id) => {
      const item = byId.get(id);
      return item ? `${item.name} [${item.resource_type}] (${id})` : id;
    }).join(", ") || "none";
  }

  function canControl(item) {
    if (!actor) return false;
    const workspace = item.workspace;
    if (workspace.owner_identity_id === actor.identity_id) return true;
    if (actor.principal_kind === "service") {
      return (actor.service_scopes || []).includes("execution-workspace:admin");
    }
    return (actor.roles || []).some((role) => ["owner", "admin"].includes(role));
  }

  function canRecover() {
    if (!actor) return false;
    if (actor.principal_kind === "service") {
      return (actor.service_scopes || []).includes("execution-workspace:admin");
    }
    return ["mfa", "local_trusted"].includes(actor.assurance)
      && (actor.roles || []).some((role) => ["owner", "admin"].includes(role));
  }

  function subjectText(workspace) {
    if (workspace.subject?.kind && workspace.subject?.ref) {
      return `${workspace.subject.kind}:${workspace.subject.ref}`;
    }
    return workspace.work_item_ref ? `work_item:${workspace.work_item_ref}` : "unknown";
  }

  function searchable(item) {
    const workspace = item.workspace;
    const lease = item.lease || {};
    return [
      workspace.id, subjectText(workspace), workspace.work_item_ref, workspace.execution_id,
      workspace.project_id, workspace.owner_identity_id, workspace.kind,
      workspace.status, workspace.path, workspace.branch_name,
      workspace.base_revision, workspace.head_revision,
      workspace.repository_resource_id, ...(workspace.resource_ids || []),
      ...(workspace.repository_members || []).flatMap((member) => [
        member.resource_id, member.access_mode, member.source_path,
        member.workspace_path, member.sandbox_path, member.branch_name,
        member.base_revision, member.head_revision,
      ]),
      lease.id, lease.mode, lease.release_reason,
      workspace.integration?.strategy, workspace.integration?.outcome,
      ...(workspace.integration?.conflicts || []), workspace.error,
    ].filter(Boolean).join(" ").toLowerCase();
  }

  function visibleItems() {
    const term = (document.getElementById("execution-workspace-search")?.value || "").trim().toLowerCase();
    const status = document.getElementById("execution-workspace-state-filter")?.value || "";
    return inspections.filter((item) => {
      if (status && item.workspace.status !== status) return false;
      return !term || searchable(item).includes(term);
    });
  }

  function controls(item) {
    const workspace = item.workspace;
    if (!canControl(item)) {
      return "<small>Workspace lifecycle controls are unavailable to the current identity.</small>";
    }
    if (["released", "discarded", "abandoned"].includes(workspace.status)) {
      return "<small>This workspace is terminal; no owner lifecycle action applies.</small>";
    }
    return `<div class="developer-toolbar">
      ${item.lease_active ? `<button type="button" class="ghost-button" data-workspace-action="renew" data-workspace-id="${escapeHtml(workspace.id)}">Renew lease</button>` : ""}
      <button type="button" class="ghost-button" data-workspace-action="release" data-workspace-id="${escapeHtml(workspace.id)}">Release & clean up</button>
      <button type="button" class="ghost-button" data-workspace-action="discard" data-workspace-id="${escapeHtml(workspace.id)}">Discard branch/workspace</button>
    </div>`;
  }

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

  function renderItem(item) {
    const workspace = item.workspace;
    const lease = item.lease;
    const leaseState = lease
      ? item.lease_active ? "active" : item.lease_expired ? "expired-unrecovered" : lease.released_at ? "released" : "inactive"
      : "missing";
    return `<details class="comm-entry" data-workspace-row="${escapeHtml(workspace.id)}">
      <summary><strong>${escapeHtml(subjectText(workspace))} · ${escapeHtml(workspace.execution_id)} · ${escapeHtml(workspace.status)}</strong></summary>
      <small>Workspace: ${escapeHtml(workspace.id)} · kind: ${escapeHtml(workspace.kind)} · project: ${escapeHtml(projectName(workspace.project_id))}</small>
      <small>Owner: ${escapeHtml(workspace.owner_identity_id)} · resources: ${escapeHtml(resourceNames(workspace.resource_ids))}</small>
      <small>Repository resource: ${escapeHtml(workspace.repository_resource_id || "none")} · branch: ${escapeHtml(workspace.branch_name || "none")}</small>
      <small>Path: ${escapeHtml(workspace.path || "none")} · base revision: ${escapeHtml(workspace.base_revision || "none")} · head revision: ${escapeHtml(workspace.head_revision || "none")}</small>
      ${repositoryMembersHtml(workspace)}
      <small>Disk: requested ${bytes(workspace.requested_disk_bytes)} · actual ${workspace.actual_disk_bytes == null ? "unknown" : bytes(workspace.actual_disk_bytes)}</small>
      <small>Created: ${timeText(workspace.created_at)} · updated: ${timeText(workspace.updated_at)} · cleaned: ${timeText(workspace.cleaned_at)}</small>
      ${workspace.error ? `<small>Error: ${escapeHtml(workspace.error)}</small>` : ""}
      ${lease ? `<small>Lease: ${escapeHtml(lease.id)} · ${escapeHtml(lease.mode)} · ${leaseState} · acquired ${timeText(lease.acquired_at)} · renewed ${timeText(lease.renewed_at)} · expires ${timeText(lease.expires_at)}</small>
        <small>Lease owner: ${escapeHtml(lease.owner_identity_id)} · released ${timeText(lease.released_at)} · reason ${escapeHtml(lease.release_reason || "none")}</small>` : '<small>Canonical lease metadata is missing for this workspace.</small>'}
      ${integrationHtml(workspace)}
      <button type="button" class="ghost-button" data-workspace-events="${escapeHtml(workspace.id)}">Load event history</button>
      <div id="workspace-events-${escapeHtml(workspace.id)}"></div>
      ${controls(item)}
    </details>`;
  }

  function render() {
    const host = document.getElementById("execution-workspace-list");
    if (!host) return;
    const visible = visibleItems();
    host.innerHTML = visible.map(renderItem).join("")
      || '<div class="comm-entry"><strong>No execution workspaces match the current filters.</strong></div>';
    const active = inspections.filter((item) => item.lease_active).length;
    const expired = inspections.filter((item) => item.lease_expired).length;
    const conflicted = inspections.filter((item) => item.workspace.status === "conflicted").length;
    setStatus(`${inspections.length} workspace(s) · ${active} active lease(s) · ${expired} expired/unrecovered lease(s) · ${conflicted} conflicted. Workspace/lease state is canonical execution isolation state, not inferred from agent prose.`);
  }

  async function loadEvents(button) {
    const id = button.dataset.workspaceEvents;
    if (!id) return;
    const host = document.getElementById(`workspace-events-${id}`);
    if (!host) return;
    button.disabled = true;
    host.innerHTML = "<small>Loading canonical workspace events...</small>";
    try {
      const response = await apiRequest(`/api/execution-workspaces/${encodeURIComponent(id)}/events`);
      host.innerHTML = (response.items || []).map((event) => `<div class="comm-entry">
        <strong>${escapeHtml(event.event_type)} · ${timeText(event.occurred_at)}</strong>
        <small>Actor: ${escapeHtml(event.actor_identity_id || "system")} · ${escapeHtml(JSON.stringify(event.details || {}))}</small>
      </div>`).join("") || "<small>No workspace events.</small>";
    } catch (error) {
      host.innerHTML = `<small>Workspace events unavailable: ${escapeHtml(error.message)}</small>`;
    } finally {
      button.disabled = false;
    }
  }

  async function renew(item) {
    const raw = window.prompt("Lease extension in seconds (30–86400):", "1800");
    if (raw === null) return;
    const ttl = Number(raw);
    if (!Number.isInteger(ttl) || ttl < 30 || ttl > 86400) {
      return setStatus("Lease extension must be an integer between 30 and 86400 seconds.");
    }
    try {
      await apiRequest(`/api/execution-workspaces/${encodeURIComponent(item.workspace.id)}/renew`, {
        method: "POST",
        body: JSON.stringify({ ttl_seconds: ttl }),
      });
      setStatus(`Renewed workspace ${item.workspace.id} lease for ${ttl} seconds.`);
      await refresh();
    } catch (error) {
      setStatus(`Workspace renewal failed: ${error.message}`);
    }
  }

  async function release(item, discard) {
    const label = discard ? "DISCARD" : "RELEASE";
    const consequence = discard
      ? "The workspace is terminal and the backend may delete its branch/worktree according to canonical cleanup semantics."
      : "The lease is released and the isolated workspace is cleaned up; the branch is retained where the backend supports it.";
    if (!window.confirm(`${label} ${subjectText(item.workspace)} / ${item.workspace.execution_id}? ${consequence}`)) return;
    const reason = window.prompt("Release reason (optional):", discard ? "operator discard" : "operator release");
    if (reason === null) return;
    try {
      await apiRequest(`/api/execution-workspaces/${encodeURIComponent(item.workspace.id)}/release`, {
        method: "POST",
        body: JSON.stringify({ discard, reason: reason.trim() || null }),
      });
      setStatus(`${discard ? "Discarded" : "Released"} workspace ${item.workspace.id}.`);
      await refresh();
    } catch (error) {
      setStatus(`Workspace ${discard ? "discard" : "release"} failed: ${error.message}`);
    }
  }

  async function recoverExpired() {
    if (!canRecover()) {
      return setStatus("Expired-workspace recovery requires human admin + MFA/step-up or execution-workspace:admin service authority.");
    }
    if (!window.confirm(
      "Recover expired workspace leases? Expired workspaces become ABANDONED and git worktrees are cleaned without claiming successful integration.",
    )) return;
    try {
      const result = await apiRequest("/api/execution-workspaces/recover", { method: "POST" });
      setStatus(`Recovered ${result.items?.length || 0} expired workspace(s).`);
      await refresh();
    } catch (error) {
      setStatus(`Workspace recovery failed: ${error.message}`);
    }
  }

  async function workspaceAction(button) {
    const item = inspections.find((value) => value.workspace.id === button.dataset.workspaceId);
    if (!item) return;
    if (button.dataset.workspaceAction === "renew") return renew(item);
    if (button.dataset.workspaceAction === "release") return release(item, false);
    if (button.dataset.workspaceAction === "discard") return release(item, true);
  }

  async function refresh() {
    setStatus("Loading canonical execution workspaces and leases...");
    try {
      const [inspectionResponse, me] = await Promise.all([
        apiRequest("/api/execution-workspaces/inspection"),
        apiRequest("/api/identity/me"),
        apiRequest("/api/projects")
          .then((value) => { projects = Array.isArray(value) ? value : (value.items || []); })
          .catch(() => { projects = []; }),
        apiRequest("/api/resources")
          .then((value) => { resources = value.items || []; })
          .catch(() => { resources = []; }),
      ]);
      inspections = inspectionResponse.items || [];
      actor = me;
      const recover = document.getElementById("recover-execution-workspaces");
      if (recover) recover.disabled = !canRecover();
      render();
    } catch (error) {
      inspections = [];
      setStatus(`Execution workspace inspection unavailable: ${error.message}`);
      render();
    }
  }

  function bind() {
    const panel = document.getElementById("developer-panel");
    document.getElementById("refresh-execution-workspaces")?.addEventListener("click", refresh);
    document.getElementById("refresh-developer")?.addEventListener("click", refresh);
    document.getElementById("execution-workspace-search")?.addEventListener("input", render);
    document.getElementById("execution-workspace-state-filter")?.addEventListener("change", render);
    document.getElementById("recover-execution-workspaces")?.addEventListener("click", () => recoverExpired().catch(console.error));
    document.getElementById("execution-workspace-list")?.addEventListener("click", (event) => {
      const eventsButton = event.target.closest?.("[data-workspace-events]");
      if (eventsButton) {
        loadEvents(eventsButton).catch(console.error);
        return;
      }
      const action = event.target.closest?.("[data-workspace-action]");
      if (action) workspaceAction(action).catch(console.error);
    });
    panel?.addEventListener("toggle", () => {
      if (panel.open) refresh().catch(console.error);
    });
    if (panel?.open) refresh().catch(console.error);
  }

  window.addEventListener("DOMContentLoaded", bind);
})();
