(async () => {
  const BASE = window.location.pathname.startsWith("/codex") ? "/codex" : "";
  const { request: apiRequest } = await import(`${BASE}/static/api_client.js`);

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

  function renderAssignment(item, audit) {
    const host = document.querySelector(
      `[data-execution-assignment-management-host="${CSS.escape(item.id)}"]`,
    );
    if (!host) return;
    host.querySelector(".control-plane-broker-status")?.remove();

    const capability = item.brokered_control_plane || {};
    const enabled = capability.enabled === true;
    const operations = capability.operations || [];
    const events = audit.filter((entry) => entry.assignment_id === item.id);
    const denied = events.filter((entry) => entry.decision !== "allow");
    const recent = events[0];

    const panel = document.createElement("div");
    panel.className = "control-plane-broker-status";
    panel.innerHTML = enabled
      ? `<small><strong>Brokered control plane:</strong> enabled · profile ${escapeHtml(item.execution_profile_id || "unknown")} · project ${escapeHtml(capability.project_id || "none")} · transport ${escapeHtml(capability.transport || "unknown")}</small>
         <small>Granted reachability classes: ${escapeHtml(operations.map((op) => op.id).join(", ") || "none")}. Canonical Role authority is evaluated again for every request.</small>
         <small>Credential material exposed: ${capability.credential_exposed ? "yes" : "no"} · audit events: ${events.length} · denials/errors: ${denied.length} · latest: ${escapeHtml(recent?.operation_id || recent?.path || "none")} at ${timeText(recent?.occurred_at)}</small>
         <small><a href="${escapeHtml(capability.audit_href || "/api/control-plane-broker/audit")}" target="_blank" rel="noopener">Open broker audit</a></small>`
      : `<small><strong>Brokered control plane:</strong> disabled for execution profile ${escapeHtml(item.execution_profile_id || "legacy/unprofiled")}.</small>`;
    host.before(panel);
  }

  async function render(assignments) {
    let audit = [];
    try {
      const response = await apiRequest("/api/control-plane-broker/audit?limit=500");
      audit = response.items || [];
    } catch (_error) {
      audit = [];
    }
    assignments.forEach((item) => renderAssignment(item, audit));
  }

  window.addEventListener("codex:execution-worker-state-rendered", (event) => {
    render(event.detail?.assignments || []).catch(console.error);
  });
})();
