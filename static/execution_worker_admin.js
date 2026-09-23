(async () => {
  const BASE = window.location.pathname.startsWith("/codex") ? "/codex" : "";
  const { request: apiRequest } = await import(`${BASE}/static/api_client.js`);
  let workers = [];
  let assignments = [];
  let events = [];

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
    const host = document.getElementById("execution-worker-status");
    if (host) {
      host.hidden = false;
      host.textContent = message;
    }
  }

  function listText(values) {
    return values?.length ? values.map(escapeHtml).join(", ") : "none";
  }

  function workerSearchText(worker) {
    return [
      worker.id,
      worker.service_identity_id,
      worker.pool,
      worker.version,
      worker.lifecycle,
      ...(worker.capabilities || []),
      worker.registered_by,
      worker.quarantine_reason,
    ].filter(Boolean).join(" ").toLowerCase();
  }

  function subjectText(item) {
    if (item.subject?.kind && item.subject?.ref) {
      return `${item.subject.kind}:${item.subject.ref}`;
    }
    return item.work_item_ref ? `work_item:${item.work_item_ref}` : "unknown";
  }

  function assignmentSearchText(item) {
    return [
      item.id,
      subjectText(item),
      item.work_item_ref,
      item.execution_id,
      item.project_id,
      item.assigned_worker_id,
      item.execution_workspace_id,
      item.status,
      item.base_revision,
      item.sandbox,
      item.approval_policy,
      ...(item.resource_ids || []),
      ...(item.required_capabilities || []),
      ...(item.secret_refs || []),
      ...(item.artifact_ids || []),
      ...(item.evidence_ids || []),
      item.failure_code,
      item.failure_message,
    ].filter(Boolean).join(" ").toLowerCase();
  }

  function searchTerm() {
    return (document.getElementById("execution-worker-search")?.value || "").trim().toLowerCase();
  }

  function visibleWorkers() {
    const term = searchTerm();
    const lifecycle = document.getElementById("execution-worker-lifecycle-filter")?.value || "";
    return workers.filter((item) => {
      if (lifecycle && item.lifecycle !== lifecycle) return false;
      return !term || workerSearchText(item).includes(term)
        || assignments.some((assignment) => (
          assignment.assigned_worker_id === item.id
          && assignmentSearchText(assignment).includes(term)
        ));
    });
  }

  function visibleAssignments() {
    const term = searchTerm();
    const status = document.getElementById("execution-assignment-status-filter")?.value || "";
    return assignments.filter((item) => {
      if (status && item.status !== status) return false;
      if (!term) return true;
      if (assignmentSearchText(item).includes(term)) return true;
      const worker = workers.find((candidate) => candidate.id === item.assigned_worker_id);
      return Boolean(worker && workerSearchText(worker).includes(term));
    });
  }

  function activeAssignmentCount(workerId) {
    return assignments.filter((item) => (
      item.assigned_worker_id === workerId
      && ["claimed", "running"].includes(item.status)
      && item.lease
    )).length;
  }

  function renderWorkers() {
    const host = document.getElementById("execution-worker-list");
    if (!host) return;
    const visible = visibleWorkers();
    host.innerHTML = visible.map((worker) => {
      const active = activeAssignmentCount(worker.id);
      const trustWarning = worker.lifecycle === "active"
        ? "Eligible for assignment subject to capability/concurrency checks."
        : worker.lifecycle === "draining"
          ? "No new work should be assigned; existing fenced leases may continue."
          : "Not trusted for execution.";
      return `<details class="comm-entry">
        <summary><strong>${escapeHtml(worker.pool)} · ${escapeHtml(worker.id)} · ${escapeHtml(worker.lifecycle)}</strong></summary>
        <small>Service identity: ${escapeHtml(worker.service_identity_id)} · version: ${escapeHtml(worker.version)} · registered by: ${escapeHtml(worker.registered_by)}</small>
        <small>Capabilities: ${listText(worker.capabilities)} · max concurrency: ${escapeHtml(worker.max_concurrency)} · active leased assignments: ${active}</small>
        <small>Registered: ${timeText(worker.registered_at)} · last heartbeat: ${timeText(worker.last_heartbeat_at)} · revoked: ${timeText(worker.revoked_at)}</small>
        ${worker.quarantine_reason ? `<small>Quarantine reason: ${escapeHtml(worker.quarantine_reason)}</small>` : ""}
        <small>${escapeHtml(trustWarning)}</small>
        <div data-execution-worker-management-host="${escapeHtml(worker.id)}"></div>
      </details>`;
    }).join("") || '<div class="comm-entry"><strong>No workers match the current filters.</strong></div>';
  }

  function leaseHtml(item) {
    if (!item.lease) return "<small>Lease: none.</small>";
    return `<small>Lease worker: ${escapeHtml(item.lease.worker_id)} · fence: ${escapeHtml(item.lease.fence)} · acquired: ${timeText(item.lease.acquired_at)} · renewed: ${timeText(item.lease.renewed_at)} · expires: ${timeText(item.lease.expires_at)} · lease credential: hidden</small>`;
  }

  function renderAssignments() {
    const host = document.getElementById("execution-assignment-list");
    if (!host) return;
    const visible = visibleAssignments();
    host.innerHTML = visible.map((item) => {
      const worker = workers.find((candidate) => candidate.id === item.assigned_worker_id);
      const network = item.network || {};
      const limits = item.limits || {};
      return `<details class="comm-entry">
        <summary><strong>${escapeHtml(subjectText(item))} · ${escapeHtml(item.execution_id)} · ${escapeHtml(item.status)}</strong></summary>
        <small>Assignment: ${escapeHtml(item.id)} · worker: ${escapeHtml(item.assigned_worker_id || "unassigned")}${worker ? ` (${escapeHtml(worker.pool)} / ${escapeHtml(worker.lifecycle)})` : ""} · execution workspace: ${escapeHtml(item.execution_workspace_id || "none")}</small>
        <small>Project: ${escapeHtml(item.project_id || "none")} · resources: ${listText(item.resource_ids)} · base revision: ${escapeHtml(item.base_revision || "none")} · contract: ${escapeHtml(item.execution_contract_version)}</small>
        <small>Required capabilities: ${listText(item.required_capabilities)} · sandbox: ${escapeHtml(item.sandbox)} · approval policy: ${escapeHtml(item.approval_policy)}</small>
        <small>Network: ${network.enabled ? "enabled" : "disabled"} · allowlist: ${listText(network.allowed_hosts)} · deadline: ${timeText(item.deadline_at)}</small>
        <small>Limits: CPU ${escapeHtml(limits.cpu_seconds)}s · wall ${escapeHtml(limits.wall_seconds)}s · memory ${bytes(limits.memory_bytes)} · disk ${bytes(limits.disk_bytes)} · processes ${escapeHtml(limits.process_count)}</small>
        <small>Secret references: ${listText(item.secret_refs)}. Raw secret material is never present in an assignment.</small>
        ${leaseHtml(item)}
        <small>Created by: ${escapeHtml(item.created_by)} · created: ${timeText(item.created_at)} · started: ${timeText(item.started_at)} · completed: ${timeText(item.completed_at)} · fence: ${escapeHtml(item.fence)}</small>
        ${item.failure_code || item.failure_message ? `<small>Failure: ${escapeHtml(item.failure_code || "unknown")} · ${escapeHtml(item.failure_message || "no detail")}</small>` : ""}
        <small>Artifacts: ${listText(item.artifact_ids)} · evidence: ${listText(item.evidence_ids)} · expected artifacts: ${listText(item.expected_artifact_types)} · expected evidence: ${listText(item.expected_evidence_types)}</small>
        <div data-execution-assignment-management-host="${escapeHtml(item.id)}"></div>
      </details>`;
    }).join("") || '<div class="comm-entry"><strong>No assignments match the current filters.</strong></div>';
  }

  function renderEvents() {
    const host = document.getElementById("execution-worker-events");
    if (!host) return;
    host.innerHTML = events.slice(0, 100).map((item) => `<div class="comm-entry">
      <strong>${escapeHtml(item.event_type)} · ${timeText(item.occurred_at)}</strong>
      <small>Worker: ${escapeHtml(item.worker_id || "none")} · assignment: ${escapeHtml(item.assignment_id || "none")} · actor: ${escapeHtml(item.actor_id || "system")}</small>
      <small>Details: ${escapeHtml(JSON.stringify(item.details || {}))}</small>
    </div>`).join("") || '<div class="comm-entry"><strong>No worker events recorded.</strong></div>';
  }

  function renderAll() {
    renderWorkers();
    renderAssignments();
    renderEvents();
    const active = workers.filter((item) => item.lifecycle === "active").length;
    const unhealthy = workers.filter((item) => ["quarantined", "revoked", "offline"].includes(item.lifecycle)).length;
    const running = assignments.filter((item) => ["claimed", "running"].includes(item.status)).length;
    const lost = assignments.filter((item) => ["lost", "failed"].includes(item.status)).length;
    setStatus(`${workers.length} worker(s), ${active} active, ${unhealthy} unavailable · ${assignments.length} assignment(s), ${running} leased/running, ${lost} lost/failed. Worker control-plane state is distinct from execution lease authority.`);
  }

  async function refresh() {
    setStatus("Loading canonical execution-worker state...");
    try {
      const [workerResponse, assignmentResponse, eventResponse] = await Promise.all([
        apiRequest("/api/execution-workers"),
        apiRequest("/api/execution-workers/assignments"),
        apiRequest("/api/execution-workers/events"),
      ]);
      workers = workerResponse.items || [];
      assignments = assignmentResponse.items || [];
      events = eventResponse.items || [];
      renderAll();
      window.dispatchEvent(new CustomEvent("codex:execution-worker-state-rendered", {
        detail: { workers, assignments, events },
      }));
    } catch (error) {
      workers = [];
      assignments = [];
      events = [];
      setStatus(`Execution-worker administration unavailable: ${error.message}`);
      renderAll();
    }
  }

  function bind() {
    const panel = document.getElementById("developer-panel");
    document.getElementById("refresh-execution-workers")?.addEventListener("click", refresh);
    document.getElementById("refresh-developer")?.addEventListener("click", refresh);
    document.getElementById("execution-worker-search")?.addEventListener("input", renderAll);
    document.getElementById("execution-worker-lifecycle-filter")?.addEventListener("change", renderAll);
    document.getElementById("execution-assignment-status-filter")?.addEventListener("change", renderAll);
    panel?.addEventListener("toggle", () => {
      if (panel.open) refresh().catch(console.error);
    });
    if (panel?.open) refresh().catch(console.error);
  }

  window.addEventListener("DOMContentLoaded", bind);
})();
