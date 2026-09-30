(async () => {
  const BASE = window.location.pathname.startsWith("/codex") ? "/codex" : "";
  const { confirmAction } = await import(`${BASE}/static/action_confirmation.js`);
  const { request: apiRequest } = await import(`${BASE}/static/api_client.js`);
  let actor = null;
  let workers = [];
  let assignments = [];

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;").replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;").replaceAll('"', "&quot;");
  }

  function setStatus(message) {
    const host = document.getElementById("execution-worker-management-status");
    if (host) {
      host.hidden = false;
      host.textContent = message;
    }
  }

  function canManage() {
    if (!actor) return false;
    if (actor.principal_kind === "service") {
      return (actor.service_scopes || []).includes("execution-worker:admin");
    }
    return ["mfa", "local_trusted"].includes(actor.assurance)
      && (actor.roles || []).some((role) => ["owner", "admin"].includes(role));
  }

  function renderAssurance() {
    const host = document.getElementById("execution-worker-management-assurance");
    if (!host || !actor) return;
    host.textContent = `Current actor ${actor.identity_id} · ${actor.principal_kind} · assurance ${actor.assurance}. Control-plane mutation: ${canManage() ? "allowed" : "blocked"}. Worker-owned heartbeat/claim/lease operations remain service-identity fenced. Server authorization remains authoritative.`;
    for (const id of ["recover-stale-workers", "recover-expired-assignments"]) {
      const button = document.getElementById(id);
      if (button) button.disabled = !canManage();
    }
  }

  function workerButtons(worker) {
    if (!canManage()) {
      return "<small>Control-plane mutation unavailable for current actor/assurance.</small>";
    }
    if (worker.lifecycle === "revoked") {
      return "<small>Revoked workers are terminal and cannot be reactivated.</small>";
    }
    const actions = [];
    if (worker.lifecycle !== "active") actions.push(["activate", "Activate"]);
    if (worker.lifecycle !== "draining") actions.push(["drain", "Drain"]);
    if (worker.lifecycle !== "quarantined") actions.push(["quarantine", "Quarantine"]);
    actions.push(["revoke", "Revoke"]);
    return `<div class="developer-toolbar">${actions.map(([action, label]) => (
      `<button type="button" class="ghost-button" data-execution-worker-action="${action}" data-worker-id="${escapeHtml(worker.id)}">${escapeHtml(label)}</button>`
    )).join("")}</div>`;
  }

  function assignmentButtons(item) {
    if (!canManage()) {
      return "<small>Assignment recovery unavailable for current actor/assurance.</small>";
    }
    if (!["lost", "failed"].includes(item.status)) {
      return "<small>No manual retry applies to this assignment state.</small>";
    }
    return `<button type="button" class="ghost-button" data-execution-assignment-action="retry" data-assignment-id="${escapeHtml(item.id)}">Retry assignment</button>`;
  }

  function hydrateHosts() {
    for (const worker of workers) {
      const host = Array.from(document.querySelectorAll("[data-execution-worker-management-host]"))
        .find((item) => item.dataset.executionWorkerManagementHost === worker.id);
      if (host) host.innerHTML = workerButtons(worker);
    }
    for (const assignment of assignments) {
      const host = Array.from(document.querySelectorAll("[data-execution-assignment-management-host]"))
        .find((item) => item.dataset.executionAssignmentManagementHost === assignment.id);
      if (host) host.innerHTML = assignmentButtons(assignment);
    }
  }

  async function lifecycle(worker, action) {
    let reason = null;
    if (["drain", "quarantine", "revoke"].includes(action)) {
      reason = window.prompt(`${action} reason for ${worker.id}:`, "");
      if (reason === null) return;
      if (action !== "drain" && !reason.trim()) {
        return setStatus(`${action} requires an explicit reason in the operator UI.`);
      }
    }
    const impacts = {
      activate: "This returns the worker to active assignment eligibility. Existing capability and concurrency checks still apply.",
      drain: "This prevents new assignment claims while allowing already-fenced work to continue under the canonical lease rules.",
      quarantine: "This removes the worker from trusted execution and causes existing lease validation to fail closed.",
      revoke: "This permanently revokes the worker identity from execution. Revoked workers cannot be reactivated.",
    };
    if (!await confirmAction({ action: `${action.toUpperCase()} worker`, target: `${worker.id} · pool ${worker.pool}`, risk: 'high', consequence: impacts[action], impact: `${assignments.filter(item => item.assigned_worker_id === worker.id && ['claimed', 'running'].includes(item.status)).length} claimed/running assignments in the loaded canonical inventory. Lease validation remains authoritative.`, recovery: action === 'revoke' ? 'Revocation is permanent. Enroll a new trusted worker to replace it.' : 'Trusted reactivation remains subject to lifecycle and compatibility checks.' })) return;
    try {
      const options = { method: "POST" };
      if (action !== "activate") {
        options.body = JSON.stringify({ reason: reason.trim() || null });
      }
      const response = await apiRequest(
        `/api/execution-workers/${encodeURIComponent(worker.id)}/${action}`,
        options,
      );
      setStatus(`Worker ${worker.id} is now ${response.item.lifecycle}.`);
      document.getElementById("refresh-execution-workers")?.click();
    } catch (error) {
      setStatus(`Worker ${action} failed: ${error.message}`);
    }
  }

  async function retryAssignment(item) {
    if (!await confirmAction({ action: 'Retry assignment', target: `${item.id} · ${item.work_item_ref} / ${item.execution_id}`, consequence: 'Returns the prior bounded execution contract to pending. A worker must acquire a new fenced lease before execution.', recovery: 'Retry does not undo prior external effects; unknown outcomes remain subject to canonical reconciliation.' })) return;
    try {
      const response = await apiRequest(
        `/api/execution-workers/assignments/${encodeURIComponent(item.id)}/retry`,
        { method: "POST" },
      );
      setStatus(`Assignment ${item.id} returned to ${response.item.status} with fence ${response.item.fence}.`);
      document.getElementById("refresh-execution-workers")?.click();
    } catch (error) {
      setStatus(`Assignment retry failed: ${error.message}`);
    }
  }

  async function recoverStaleWorkers() {
    if (!await confirmAction({ action: 'Recover stale workers', target: 'Workers with expired canonical heartbeats', risk: 'high', consequence: 'The server marks eligible stale workers offline; they cannot claim new work. Fresh workers remain active.', recovery: 'Use the trusted reactivation/heartbeat path to restore eligibility.' })) return;
    try {
      const response = await apiRequest("/api/execution-workers/recover-stale-workers", {
        method: "POST",
      });
      setStatus(`Marked ${response.worker_ids?.length || 0} stale worker(s) offline.`);
      document.getElementById("refresh-execution-workers")?.click();
    } catch (error) {
      setStatus(`Stale-worker recovery failed: ${error.message}`);
    }
  }

  async function recoverExpiredAssignments() {
    if (!await confirmAction({ action: 'Recover expired leases', target: 'Claimed/running assignments with expired canonical leases', risk: 'high', consequence: 'Eligible assignments become LOST and cannot continue under the expired fence.', recovery: 'Explicit retry is required before a worker can claim a new lease. External effects are not undone.' })) return;
    try {
      const response = await apiRequest("/api/execution-workers/assignments/recover-expired", {
        method: "POST",
      });
      setStatus(`Recovered ${response.assignment_ids?.length || 0} expired assignment lease(s) as LOST.`);
      document.getElementById("refresh-execution-workers")?.click();
    } catch (error) {
      setStatus(`Expired-assignment recovery failed: ${error.message}`);
    }
  }

  async function loadActor() {
    try {
      actor = await apiRequest("/api/identity/me");
      renderAssurance();
      hydrateHosts();
    } catch (error) {
      actor = null;
      setStatus(`Worker management identity unavailable: ${error.message}`);
      renderAssurance();
      hydrateHosts();
    }
  }

  function hydrate(detail) {
    workers = detail.workers || [];
    assignments = detail.assignments || [];
    hydrateHosts();
    if (!actor) loadActor().catch(console.error);
  }

  function bind() {
    document.getElementById("recover-stale-workers")?.addEventListener("click", () => recoverStaleWorkers().catch(console.error));
    document.getElementById("recover-expired-assignments")?.addEventListener("click", () => recoverExpiredAssignments().catch(console.error));
    document.getElementById("execution-worker-list")?.addEventListener("click", (event) => {
      const button = event.target.closest?.("[data-execution-worker-action]");
      if (!button) return;
      const worker = workers.find((item) => item.id === button.dataset.workerId);
      if (!worker) return;
      button.disabled = true;
      lifecycle(worker, button.dataset.executionWorkerAction)
        .catch(console.error)
        .finally(() => { if (document.contains(button)) button.disabled = false; });
    });
    document.getElementById("execution-assignment-list")?.addEventListener("click", (event) => {
      const button = event.target.closest?.("[data-execution-assignment-action]");
      if (!button) return;
      const assignment = assignments.find((item) => item.id === button.dataset.assignmentId);
      if (!assignment) return;
      button.disabled = true;
      retryAssignment(assignment)
        .catch(console.error)
        .finally(() => { if (document.contains(button)) button.disabled = false; });
    });
    loadActor().catch(console.error);
    import(`${BASE}/static/execution_worker_enrollment.js`)
      .then(({ installExecutionWorkerEnrollment }) => installExecutionWorkerEnrollment({
        apiRequest,
        canManage,
        setStatus,
        escapeHtml,
        base: BASE,
      }))
      .catch(console.error);
  }

  window.addEventListener("codex:execution-worker-state-rendered", (event) => hydrate(event.detail || {}));
  if (document.readyState === "loading") {
    window.addEventListener("DOMContentLoaded", bind, { once: true });
  } else {
    bind();
  }
})();
