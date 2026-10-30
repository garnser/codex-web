(async () => {
  const BASE = window.location.pathname.startsWith("/codex") ? "/codex" : "";
  const { referenceLink } = await import(`${BASE}/static/reference_navigation.js`);
  const { referenceAttributes, focusReference, workspaceIsVisible } = await import(`${BASE}/static/reference_links.js`);
  const { request: apiRequest } = await import(`${BASE}/static/api_client.js`);
  const { installProcedureRun, procedureRunsDirty, disposeProcedureRuns } = await import(`${BASE}/static/procedure_run_ui.js`);
  const { captureProjectView } = await import(`${BASE}/static/project_view_scope.js`);
  const { workerDetailsHtml, timeText } = await import(`${BASE}/static/execution_worker_presenters.js`);
  let refreshGeneration = 0;
  const { workerEventsHtml } = await import(`${BASE}/static/execution_worker_presenters.js`);
  let workers = [];
  let assignments = [];
  let events = [];

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;").replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;").replaceAll('"', "&quot;");
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
    host.innerHTML = visibleWorkers().map(worker => workerDetailsHtml(worker, activeAssignmentCount(worker.id))).join("")
      || '<div class="comm-entry"><strong>No workers match the current filters.</strong></div>';
  }

  function leaseHtml(item) {
    if (!item.lease) return "<small>Lease: none.</small>";
    const credentialState = item.lease.lease_token ? "present (hidden)" : "none";
    return `<small>Lease worker: ${escapeHtml(item.lease.worker_id)} · fence: ${escapeHtml(item.lease.fence)} · acquired: ${timeText(item.lease.acquired_at)} · renewed: ${timeText(item.lease.renewed_at)} · expires: ${timeText(item.lease.expires_at)} · lease credential: ${credentialState}</small>`;
  }

  function nativeCredentialCustodyHtml(item) {
    const custody = item.native_credential_custody;
    if (custody == null) {
      return '<section aria-label="Native credential custody"><small>Native credential custody: legacy unknown. No admission record is retained.</small></section>';
    }
    const admitted = custody.schema_version === "1.0"
      && typeof custody.admitted_at === "number"
      && Number.isFinite(custody.admitted_at) && custody.admitted_at > 0;
    const pending = custody.schema_version === "1.0" && custody.admitted_at == null;
    const state = admitted ? "admitted (recorded)" : pending ? "pending" : "unknown";
    const procedures = (custody.procedures || []).map(ref => (
      `${referenceLink("definition", ref.record_id, { label: ref.definition_id })} · kind ${escapeHtml(ref.kind)} · revision ${escapeHtml(ref.revision)} · schema ${escapeHtml(ref.definition_schema_version)} · checksum ${escapeHtml(ref.checksum)}`
    )).join("; ") || "none recorded";
    const bindings = Object.entries(custody.provider_bindings || {}).map(([id, checksum]) => (
      `${escapeHtml(id)} · checksum ${escapeHtml(checksum)}`
    )).join("; ") || "none recorded";
    return `<section aria-label="Native credential custody">
      <small>Native credential custody: ${state} · schema ${escapeHtml(custody.schema_version)}. This record does not grant current authority.</small>
      <small>Runtime authentication reference: ${custody.runtime_secret_ref ? referenceLink("secret", custody.runtime_secret_ref) : "none recorded; no credential grant inferred"}</small>
      <small>Procedure credential references: ${(custody.procedure_secret_refs || []).map(id => referenceLink("secret", id)).join(", ") || "none recorded"}</small>
      <small>Admission: ${admitted ? timeText(custody.admitted_at) : "not established"} · requester: ${referenceLink("identity", custody.requester_identity_id)} · worker identity: ${referenceLink("identity", custody.worker_identity_id)} · fence: ${escapeHtml(custody.fence ?? "not recorded")}</small>
      <small>Admitted procedures: ${procedures}</small>
      <small>Provider binding pins: ${bindings}</small>
    </section>`;
  }

  function renderAssignments() {
    const host = document.getElementById("execution-assignment-list");
    if (!host) return;
    if (procedureRunsDirty(host)) return false;
    disposeProcedureRuns(host);
    const visible = visibleAssignments();
    host.innerHTML = visible.map((item) => {
      const worker = workers.find((candidate) => candidate.id === item.assigned_worker_id);
      const network = item.network || {};
      const limits = item.limits || {};
      return `<details class="comm-entry" ${referenceAttributes("assignment", item.id)}>
        <summary><strong>${escapeHtml(subjectText(item))} · ${escapeHtml(item.execution_id)} · ${escapeHtml(item.status)}</strong></summary>
        <small>Assignment: ${escapeHtml(item.id)} · worker: ${referenceLink("worker", item.assigned_worker_id)}${worker ? ` (${escapeHtml(worker.pool)} / ${escapeHtml(worker.lifecycle)})` : ""} · execution workspace: ${escapeHtml(item.execution_workspace_id || "none")}</small>
        ${item.brokered_control_plane?.assignment_context?.publication_recovery ? `<small>Current and retired publication recovery: ${escapeHtml(JSON.stringify(item.brokered_control_plane.assignment_context.publication_recovery))}</small>` : ""}
        <small>Project: ${escapeHtml(item.project_id || "none")} · resources: ${(item.resource_ids || []).map(id => referenceLink("resource", id)).join(", ") || "none"} · base revision: ${escapeHtml(item.base_revision || "none")} · contract: ${escapeHtml(item.execution_contract_version)}</small>
        <small>Required capabilities: ${listText(item.required_capabilities)} · sandbox: ${escapeHtml(item.sandbox)} · approval policy: ${escapeHtml(item.approval_policy)}</small>
        <small>Network: ${network.enabled ? "enabled" : "disabled"} · allowlist: ${listText(network.allowed_hosts)} · deadline: ${timeText(item.deadline_at)}</small>
        <small>Limits: CPU ${escapeHtml(limits.cpu_seconds)}s · wall ${escapeHtml(limits.wall_seconds)}s · memory ${bytes(limits.memory_bytes)} · disk ${bytes(limits.disk_bytes)} · processes ${escapeHtml(limits.process_count)}</small>
        <small>Secret references: ${(item.secret_refs || []).map(id => referenceLink("secret", id)).join(", ") || "none"}. Raw secret material is never present in an assignment.</small>
        ${nativeCredentialCustodyHtml(item)}
        ${leaseHtml(item)}
        <small>Created by: ${escapeHtml(item.created_by)} · created: ${timeText(item.created_at)} · started: ${timeText(item.started_at)} · completed: ${timeText(item.completed_at)} · fence: ${escapeHtml(item.fence)}</small>
        ${item.failure_code || item.failure_message ? `<small>Failure: ${escapeHtml(item.failure_code || "unknown")} · ${escapeHtml(item.failure_message || "no detail")}</small>` : ""}
        <small>Artifacts: ${listText(item.artifact_ids)} · evidence: ${listText(item.evidence_ids)} · expected artifacts: ${listText(item.expected_artifact_types)} · expected evidence: ${listText(item.expected_evidence_types)}</small>
        <div data-execution-assignment-management-host="${escapeHtml(item.id)}"></div>
        <section data-assignment-procedure-host="${escapeHtml(item.id)}"></section>
      </details>`;
    }).join("") || '<div class="comm-entry"><strong>No assignments match the current filters.</strong></div>';
    for (const item of visible) {
      const panel = [...host.querySelectorAll('[data-assignment-procedure-host]')].find(node => node.dataset.assignmentProcedureHost === item.id);
      installProcedureRun(panel, item);
    }
    return true;
  }

  function renderEvents() {
    const host = document.getElementById("execution-worker-events");
    if (!host) return;
    host.innerHTML = workerEventsHtml(events);
  }

  function renderAll() {
    renderWorkers();
    const assignmentsRendered = renderAssignments();
    renderEvents();
    focusReference(document.getElementById("execution-worker-list"));
    focusReference(document.getElementById("execution-assignment-list"));
    const active = workers.filter((item) => item.lifecycle === "active").length;
    const unhealthy = workers.filter((item) => ["quarantined", "revoked", "offline"].includes(item.lifecycle)).length;
    const running = assignments.filter((item) => ["claimed", "running"].includes(item.status)).length;
    const cancelled = assignments.filter((item) => item.status === "cancelled").length;
    const lost = assignments.filter((item) => ["lost", "failed"].includes(item.status)).length;
    setStatus(`${workers.length} worker(s), ${active} active, ${unhealthy} unavailable · ${assignments.length} assignment(s), ${running} leased/running, ${lost} lost/failed, ${cancelled} cancelled. Worker control-plane state is distinct from execution lease authority.`);
    if (assignmentsRendered === false) setStatus('Assignment display refresh deferred: playbook inputs have unsaved changes. Save or use the corresponding Discard inputs control, then refresh.');
    window.dispatchEvent(new CustomEvent("codex:execution-worker-state-rendered", {
      detail: { workers, assignments, events },
    }));
  }

  async function refresh() {
    const view = captureProjectView(), visit = ++refreshGeneration;
    setStatus("Loading canonical execution-worker state...");
    try {
      const [workerResponse, assignmentResponse, eventResponse] = await Promise.all([
        apiRequest("/api/execution-workers"),
        apiRequest("/api/execution-workers/assignments"),
        apiRequest("/api/execution-workers/events"),
      ]);
      if (!view.current() || visit !== refreshGeneration) return;
      workers = workerResponse.items || [];
      assignments = assignmentResponse.items || [];
      events = eventResponse.items || [];
      renderAll();
    } catch (error) {
      if (!view.current() || visit !== refreshGeneration) return;
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
    const clearScope = () => {
      refreshGeneration += 1; workers = []; assignments = []; events = [];
      const list = document.getElementById('execution-assignment-list');
      if (list) { disposeProcedureRuns(list); list.textContent = 'Scope changed. Refresh canonical assignments.'; }
      document.getElementById('execution-worker-list')?.replaceChildren();
      document.getElementById('execution-worker-events')?.replaceChildren();
    };
    window.addEventListener('codex:project-changed', clearScope);
    window.addEventListener('codex:identity-scope-changed', clearScope);
    panel?.addEventListener("toggle", () => {
      if (panel.open) refresh().catch(console.error);
    });
    if (panel?.open || workspaceIsVisible('workers')) refresh().catch(console.error);
  }

  if (document.readyState === 'loading') window.addEventListener("DOMContentLoaded", bind, {once:true});
  else bind();
})();
