(async () => {
  const BASE = window.location.pathname.startsWith("/codex") ? "/codex" : "";
  const { request: apiRequest } = await import(`${BASE}/static/api_client.js`);

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");
  }

  function listText(values) {
    return values?.length ? values.map(escapeHtml).join(", ") : "none";
  }

  function setStatus(message) {
    const status = document.getElementById("action-provider-status");
    if (status) {
      status.hidden = false;
      status.textContent = message;
    }
  }

  function capabilityText(capabilities) {
    const names = [
      "read", "prepare", "execute", "dry_run", "idempotency",
      "rollback", "verification", "progress", "evidence",
    ];
    return names.map((name) => `${name}=${capabilities?.[name] ? "yes" : "no"}`).join(" · ");
  }

  function resourceText(ids, resources) {
    if (!ids?.length) return "none";
    const byId = new Map(resources.map((item) => [item.id, item]));
    return ids.map((id) => {
      const item = byId.get(id);
      return item ? `${item.name} (${id})` : id;
    }).join(", ");
  }

  function projectText(id, projects) {
    if (!id) return "workspace-wide";
    const item = projects.find((project) => project.id === id);
    return item ? `${item.name} (${id})` : id;
  }

  function renderSecurity(policy) {
    const network = policy?.network || {};
    const filesystem = policy?.filesystem || {};
    const process = policy?.process || {};
    return `<div class="comm-entry">
      <small>Security policy · sandbox: ${escapeHtml(policy?.sandbox || "unknown")} · executable digest required: ${policy?.require_digest_for_executable_artifacts ? "yes" : "no"}</small>
      <small>Network: ${network.enabled ? "enabled" : "disabled"} · schemes: ${listText(network.allowed_schemes)} · hosts: ${listText(network.allowed_hosts)} · ports: ${listText(network.allowed_ports)} · redirects: ${escapeHtml(network.max_redirects ?? 0)}</small>
      <small>Filesystem read roots: ${listText(filesystem.allowed_read_roots)} · write roots: ${listText(filesystem.allowed_write_roots)} · symlink escape: ${filesystem.allow_symlink_escape ? "allowed" : "blocked"}</small>
      <small>Process execution: ${process.allow_process_execution ? "allowed" : "blocked"} · shell: ${process.allow_shell ? "allowed" : "blocked"} · executables: ${listText(process.allowed_executables)}</small>
    </div>`;
  }

  function renderAction(action) {
    return `<details class="comm-entry">
      <summary><strong>${escapeHtml(action.title)} · ${escapeHtml(action.action_id)} · risk ${escapeHtml(action.risk_class)}</strong></summary>
      ${action.description ? `<small>${escapeHtml(action.description)}</small>` : ""}
      <small>Capabilities: ${escapeHtml(capabilityText(action.capabilities))}</small>
      <small>Required authority: ${listText(action.required_authority)} · Resource types: ${listText(action.required_resource_types)}</small>
      <small>Credential: ${action.credential_required ? `required for ${escapeHtml(action.credential_purpose || "unspecified purpose")}` : "not required"}</small>
      <small>Expected evidence: ${listText(action.expected_evidence)} · Verification: ${action.capabilities?.verification ? "supported" : "not supported"} · Rollback: ${action.capabilities?.rollback ? "supported" : "not supported"} · Reversible: ${action.reversible ? "yes" : "no"}</small>
      <small>Timeout: ${escapeHtml(action.timeout_seconds)}s · Retry max attempts: ${escapeHtml(action.retry_max_attempts)} · Network access: ${action.network_access ? "yes" : "no"} · Filesystem: ${escapeHtml(action.filesystem_access)} · Process access: ${action.process_access ? "yes" : "no"}</small>
    </details>`;
  }

  function renderCatalog(items, resources, projects) {
    const list = document.getElementById("action-provider-list");
    if (!list) return;
    list.innerHTML = items.map((entry) => {
      const binding = entry.binding || {};
      const actions = entry.actions || [];
      const status = entry.status || "unknown";
      return `<div class="comm-entry">
        <strong>${escapeHtml(binding.provider_type)}/${escapeHtml(binding.provider_instance)} · ${escapeHtml(status)}</strong>
        <small>Binding: ${escapeHtml(binding.id)} · Enabled: ${binding.enabled ? "yes" : "no"} · Tenant: ${escapeHtml(binding.organization_id)}/${escapeHtml(binding.workspace_id)}</small>
        <small>Project scope: ${escapeHtml(projectText(binding.project_id, projects))} · Resources: ${escapeHtml(resourceText(binding.resource_ids, resources))}</small>
        <small>Credential reference: ${escapeHtml(binding.credential_ref || "none")} · ${actions.length} action(s) advertised</small>
        ${renderSecurity(binding.security_policy)}
        ${actions.length ? actions.map(renderAction).join("") : '<small>No action contracts currently available from this binding.</small>'}
      </div>`;
    }).join("") || '<div class="comm-entry"><strong>No ActionProvider bindings in this workspace.</strong></div>';
  }

  async function refresh() {
    setStatus("Loading canonical ActionProvider state...");
    let resources = [];
    let projects = [];
    let resourceError = null;
    let projectError = null;
    try {
      const [catalog] = await Promise.all([
        apiRequest("/api/action-providers"),
        apiRequest("/api/resources")
          .then((value) => { resources = value.items || []; })
          .catch((error) => { resourceError = error.message; }),
        apiRequest("/api/projects")
          .then((value) => { projects = Array.isArray(value) ? value : (value.items || []); })
          .catch((error) => { projectError = error.message; }),
      ]);
      const items = catalog.items || [];
      renderCatalog(items, resources, projects);
      const unavailable = items.filter((entry) => entry.status !== "available").length;
      const warnings = [
        resourceError ? `resource labels unavailable: ${resourceError}` : null,
        projectError ? `project labels unavailable: ${projectError}` : null,
      ].filter(Boolean);
      setStatus(`${items.length} provider binding(s) · ${unavailable} unavailable/disabled.${warnings.length ? ` ${warnings.join(" · ")}` : ""} This view does not prepare or execute actions.`);
      window.dispatchEvent(new CustomEvent("codex:action-provider-rendered", {
        detail: { items, resources, projects },
      }));
    } catch (error) {
      setStatus(`ActionProvider catalog unavailable: ${error.message}`);
      const list = document.getElementById("action-provider-list");
      if (list) list.innerHTML = "";
    }
  }

  function bind() {
    const panel = document.getElementById("developer-panel");
    document.getElementById("refresh-action-providers")?.addEventListener("click", refresh);
    document.getElementById("refresh-developer")?.addEventListener("click", refresh);
    panel?.addEventListener("toggle", () => {
      if (panel.open) refresh().catch(console.error);
    });
    if (panel?.open) refresh().catch(console.error);
  }

  window.addEventListener("DOMContentLoaded", bind);
})();
