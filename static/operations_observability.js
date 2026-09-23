(async () => {
  const BASE = window.location.pathname.startsWith("/codex") ? "/codex" : "";
  const { request: apiRequest } = await import(`${BASE}/static/api_client.js`);
  const { statusBadge, metadataGrid, surfaceCard, statePanel } = await import(`${BASE}/static/workspace_components.js`);
  let actor = null;
  let observability = null;
  let operations = null;
  let assignments = [];
  let workers = [];
  let projects = [];
  let refreshPromise = null;
  const MAX_TRACE_ROWS = 100;

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

  function duration(value) {
    const seconds = Number(value || 0);
    if (!Number.isFinite(seconds)) return "unknown";
    if (seconds < 1) return `${Math.round(seconds * 1000)} ms`;
    if (seconds < 60) return `${seconds.toFixed(2)} s`;
    if (seconds < 3600) return `${(seconds / 60).toFixed(1)} min`;
    return `${(seconds / 3600).toFixed(1)} h`;
  }

  function setStatus(message) {
    const host = document.getElementById("operations-status");
    if (host) {
      host.hidden = false;
      host.textContent = message;
    }
  }

  function listText(values) {
    if (Array.isArray(values)) return values.length ? values.map(escapeHtml).join(", ") : "none";
    if (values && typeof values === "object") {
      const rows = Object.entries(values);
      return rows.length ? rows.map(([key, value]) => `${escapeHtml(key)}=${escapeHtml(value)}`).join(" · ") : "none";
    }
    return values == null ? "none" : escapeHtml(values);
  }

  function assignmentFailure(item) {
    const failure = item?.failure || null;
    return {
      reason: failure?.reason || item?.failure_code || null,
      summary: failure?.summary || item?.failure_message || null,
      remediation: failure?.remediation_key || null,
      retryability: failure?.retryability || null,
    };
  }

  function navigationButton(label, target, selector = null) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "ghost-button";
    button.textContent = label;
    button.addEventListener("click", () => {
      if (target === "attention") {
        document.querySelector("[data-attention-launch]")?.click();
        return;
      }
      const nav = document.querySelector(`[data-product-workspace-nav="${target}"]`);
      if (nav) nav.click();
      else if (target) window.location.hash = `#workspace/${target}`;
      if (selector) {
        window.setTimeout(() => {
          const node = document.querySelector(selector);
          node?.scrollIntoView?.({ block: "center", behavior: "smooth" });
          node?.focus?.({ preventScroll: true });
        }, 0);
      }
    });
    return button;
  }

  function overviewFooter() {
    const footer = document.createElement("div");
    footer.className = "developer-toolbar";
    footer.append(
      navigationButton("Runs / Work", "work"),
      navigationButton("Attention", "attention"),
      navigationButton("Workers", "workers", "#execution-worker-list"),
      navigationButton("Evidence", "operations", "#artifact-list"),
      navigationButton("Add runtime", "workers", "#execution-worker-enrollment-panel"),
    );
    return footer;
  }

  function renderOverview() {
    const host = document.getElementById("operations-overview");
    if (!host) return;
    if (!observability || !operations) {
      host.replaceChildren(statePanel({
        kind: "loading",
        title: "Loading canonical operations state",
        detail: "Runtime, provider and worker summaries use existing canonical read models.",
        busy: true,
      }));
      return;
    }

    const health = observability.health || {};
    const runtime = operations.runtime || {};
    const provider = operations.providers || {};
    const lifecycleCounts = workers.reduce((counts, item) => {
      const key = String(item.lifecycle || "unknown");
      counts[key] = (counts[key] || 0) + 1;
      return counts;
    }, {});
    const assignmentFailures = assignments
      .filter((item) => ["failed", "lost"].includes(item.status) || item.failure || item.failure_code)
      .slice(0, 5);

    const runtimeBody = metadataGrid([
      { label: "Readiness", value: health.readiness ? "ready" : "blocked" },
      { label: "Active turns", value: runtime.activeTurns ?? 0 },
      { label: "Queued turns", value: runtime.queuedTurns ?? 0 },
      { label: "Pending approvals", value: runtime.pendingApprovals ?? 0 },
    ]);
    const runtimeCard = surfaceCard({
      title: "Runtime",
      description: "Canonical service health and current execution pressure.",
      status: health.status || (runtime.healthy ? "healthy" : "degraded"),
      body: runtimeBody,
    });

    const workerStatus = lifecycleCounts.quarantined || lifecycleCounts.revoked || lifecycleCounts.offline
      ? "degraded"
      : lifecycleCounts.draining
        ? "warning"
        : (workers.length ? "healthy" : "unknown");
    const workerBody = metadataGrid([
      { label: "Active", value: lifecycleCounts.active || 0 },
      { label: "Draining", value: lifecycleCounts.draining || 0 },
      { label: "Quarantined", value: lifecycleCounts.quarantined || 0 },
      { label: "Offline / revoked", value: (lifecycleCounts.offline || 0) + (lifecycleCounts.revoked || 0) },
    ]);
    const workerCard = surfaceCard({
      title: "Execution workers",
      description: workers.length
        ? "Lifecycle is canonical worker state; assignment eligibility remains server-authoritative."
        : "Worker inventory has not been loaded yet.",
      status: workerStatus,
      body: workerBody,
    });

    const providerBody = metadataGrid([
      { label: "Runtime connections", value: provider.runtimeConnections ?? "—" },
      { label: "Recent delivery failures", value: provider.recentDeliveryFailures ?? "—" },
      { label: "GitLab sync failures", value: provider.gitlabSyncConsecutiveFailures ?? "—" },
      { label: "Last GitLab success", value: timeText(provider.gitlabSyncLastSuccessAt) },
    ]);
    const providerCard = surfaceCard({
      title: "Provider activity",
      description: "Observed connectivity and delivery telemetry; this does not invent provider readiness.",
      body: providerBody,
    });

    const failureBody = document.createElement("div");
    failureBody.className = "operations-failure-summary";
    if (!assignmentFailures.length) {
      failureBody.appendChild(statePanel({
        kind: "empty",
        title: "No failed or lost assignments in the loaded window",
        detail: "Canonical failure records will appear here with their reason and remediation key.",
      }));
    } else {
      for (const item of assignmentFailures) {
        const failure = assignmentFailure(item);
        const row = document.createElement("div");
        row.className = "comm-entry";
        const title = document.createElement("strong");
        title.textContent = `${item.execution_id || item.id} · ${item.status}`;
        const detail = document.createElement("small");
        detail.textContent = [
          failure.reason ? `reason ${failure.reason}` : null,
          failure.summary,
          failure.retryability ? `retry ${failure.retryability}` : null,
          failure.remediation ? `remediation ${failure.remediation}` : null,
        ].filter(Boolean).join(" · ") || "No canonical failure detail is attached.";
        row.append(title, detail);
        failureBody.appendChild(row);
      }
    }
    const failureCard = surfaceCard({
      title: "Failures & remediation",
      description: "Canonical failure taxonomy from execution assignments; no error-string classification in the UI.",
      status: assignmentFailures.length ? "degraded" : "healthy",
      body: failureBody,
    });

    const grid = document.createElement("div");
    grid.className = "cw-card-grid operations-overview-grid";
    grid.append(runtimeCard, workerCard, providerCard, failureCard);
    host.replaceChildren(grid, overviewFooter());
  }

  function canRecover() {
    if (!actor) return false;
    if (actor.principal_kind === "service") {
      return (actor.service_scopes || []).includes("runtime:admin");
    }
    return ["mfa", "local_trusted"].includes(actor.assurance)
      && (actor.roles || []).some((role) => ["owner", "admin"].includes(role));
  }

  function renderHealth() {
    const host = document.getElementById("operations-health");
    if (!host || !observability) return;
    const health = observability.health || {};
    const dependencies = health.dependencies || [];
    host.innerHTML = `<div class="comm-entry">
      <strong>${escapeHtml(health.status || "unknown")} · liveness ${health.liveness ? "ok" : "failed"} · readiness ${health.readiness ? "ok" : "blocked"}</strong>
      <small>Degraded: ${health.degraded ? "yes" : "no"} · autonomous execution eligible: ${health.autonomousExecutionEligible ? "yes" : "no"} · uptime: ${duration(health.uptimeSeconds)}</small>
      <small>Health is operational telemetry only; it does not advance Work/Goal/Decision state.</small>
    </div>` + dependencies.map((item) => `<div class="comm-entry">
      <strong>${escapeHtml(item.name)} · ${escapeHtml(item.status)}</strong>
      <small>Required for readiness: ${item.requiredForReadiness ? "yes" : "no"} · required for autonomy: ${item.requiredForAutonomy ? "yes" : "no"} · checked: ${timeText(item.checkedAt)}</small>
      ${item.reason ? `<small>Reason: ${escapeHtml(item.reason)}</small>` : ""}
    </div>`).join("");
  }

  function leaseSummary() {
    const now = Date.now() / 1000;
    const leased = assignments.filter((item) => ["claimed", "running"].includes(item.status) && item.lease);
    const expired = leased.filter((item) => Number(item.lease?.expires_at || 0) <= now);
    const pending = assignments.filter((item) => item.status === "pending").length;
    const lost = assignments.filter((item) => item.status === "lost").length;
    return { leased: leased.length, expired: expired.length, pending, lost };
  }

  function renderRuntime() {
    const host = document.getElementById("operations-runtime");
    if (!host || !operations) return;
    const runtime = operations.runtime || {};
    const work = operations.workItems || {};
    const leases = leaseSummary();
    const supervisors = runtime.supervisorTasks || {};
    host.innerHTML = `<div class="comm-entry">
      <strong>Runtime · ${runtime.healthy ? "healthy" : "unhealthy"}</strong>
      <small>Active turns: ${escapeHtml(runtime.activeTurns)} · queued turns: ${escapeHtml(runtime.queuedTurns)} across ${escapeHtml(runtime.queueThreads)} thread(s) · pending approvals: ${escapeHtml(runtime.pendingApprovals)}</small>
      <small>Queue age: oldest ${duration(runtime.oldestQueueAgeSeconds)} · average ${duration(runtime.averageQueueAgeSeconds)}</small>
      <small>Health problems: ${listText(runtime.healthProblems)}</small>
    </div>
    <div class="comm-entry">
      <strong>Supervisor / scheduler tasks</strong>
      <small>${listText(supervisors)}</small>
    </div>
    <div class="comm-entry">
      <strong>Execution lease health</strong>
      <small>Leased/running: ${leases.leased} · already expired: ${leases.expired} · pending: ${leases.pending} · lost: ${leases.lost}</small>
      <small>Expired lease detection here is diagnostic only; canonical recovery uses the execution-worker recovery API.</small>
    </div>
    <div class="comm-entry">
      <strong>Canonical Work Item pressure</strong>
      <small>Open: ${escapeHtml(work.open)} · blocked: ${escapeHtml(work.blocked)} · pending handoffs: ${escapeHtml(work.pendingHandoffs)} · release gates: ${escapeHtml(work.releaseGates)} · split-brain findings: ${escapeHtml(work.splitBrain)}</small>
      <small>Stages: ${listText(work.stages)} · owners: ${listText(work.owners)} · oldest pending handoff: ${duration(work.oldestPendingHandoffAgeSeconds)}</small>
    </div>`;
  }

  function renderProviders() {
    const host = document.getElementById("operations-providers");
    if (!host || !operations) return;
    const providers = operations.providers || {};
    const activity = operations.activity || {};
    host.innerHTML = `<div class="comm-entry">
      <strong>Provider/runtime connectivity</strong>
      <small>Runtime connections: ${escapeHtml(providers.runtimeConnections)} · recent delivery failures: ${escapeHtml(providers.recentDeliveryFailures)} · Slack backfill cooldown: ${duration(providers.slackBackfillCooldownRemainingSeconds)}</small>
      <small>GitLab sync consecutive failures: ${escapeHtml(providers.gitlabSyncConsecutiveFailures)} · last success: ${timeText(providers.gitlabSyncLastSuccessAt)}${providers.gitlabSyncLastError ? ` · last error: ${escapeHtml(providers.gitlabSyncLastError)}` : ""}</small>
    </div>
    <div class="comm-entry">
      <strong>Recent activity · ${escapeHtml(activity.events)} event(s)</strong>
      <small>Event types: ${listText(activity.eventTypes)} · recovery events: ${escapeHtml(activity.recoveryEvents)} · Executive events: ${escapeHtml(activity.executiveEvents)}</small>
    </div>`;
  }

  function renderMetrics() {
    const host = document.getElementById("operations-metrics");
    if (!host || !observability) return;
    const metrics = observability.metrics || {};
    const counters = Object.entries(metrics.counters || {});
    const timers = Object.entries(metrics.timers || {});
    host.innerHTML = `<div class="comm-entry">
      <strong>Runtime metrics · uptime ${duration(metrics.uptimeSeconds)}</strong>
      <small>Bounded/low-cardinality telemetry; tenant IDs, secrets and prompt content are not metric labels.</small>
    </div>`
      + counters.map(([name, value]) => `<div class="comm-entry"><strong>${escapeHtml(name)}</strong><small>counter = ${escapeHtml(value)}</small></div>`).join("")
      + timers.map(([name, value]) => `<div class="comm-entry">
        <strong>${escapeHtml(name)}</strong>
        <small>count ${escapeHtml(value.count)} · avg ${duration(value.averageSeconds)} · max ${duration(value.maxSeconds)} · total ${duration(value.totalSeconds)}</small>
      </div>`).join("");
  }

  function traceMatches(item) {
    const term = (document.getElementById("operations-trace-search")?.value || "").trim().toLowerCase();
    if (!term) return true;
    return [
      item.spanId,
      item.name,
      item.correlationId,
      item.causationId,
      item.parentSpanId,
      item.status,
      JSON.stringify(item.attributes || {}),
    ].filter(Boolean).join(" ").toLowerCase().includes(term);
  }

  function renderTraces() {
    const host = document.getElementById("operations-traces");
    if (!host || !observability) return;
    const matches = (observability.recentTraces || []).filter(traceMatches).slice().reverse();
    const rows = matches.slice(0, MAX_TRACE_ROWS);
    const windowNotice = matches.length > rows.length
      ? `<div class="comm-entry"><strong>Showing latest ${rows.length} of ${matches.length} matching traces.</strong><small>Refine the trace filter to inspect older rows without rendering the full telemetry set.</small></div>`
      : "";
    host.innerHTML = windowNotice + rows.map((item) => `<details class="comm-entry">
      <summary><strong>${escapeHtml(item.name)} · ${escapeHtml(item.status)} · ${duration(item.durationSeconds)}</strong></summary>
      <small>Correlation: ${escapeHtml(item.correlationId)} · causation: ${escapeHtml(item.causationId || "none")} · span: ${escapeHtml(item.spanId)} · parent: ${escapeHtml(item.parentSpanId || "none")}</small>
      <button type="button" class="ghost-button" data-log-correlation="${escapeHtml(item.correlationId)}">View structured logs</button>
      <small>${timeText(item.startedAt)} → ${timeText(item.endedAt)}</small>
      <pre>${escapeHtml(JSON.stringify(item.attributes || {}, null, 2))}</pre>
    </details>`).join("") || '<div class="comm-entry"><strong>No recent traces match the filter.</strong></div>';
  }

  function populateProjects() {
    const select = document.getElementById("operations-diagnostic-project");
    if (!select) return;
    const previous = select.value;
    select.innerHTML = '<option value="">All/current runtime</option>' + projects.map((item) => (
      `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)} · ${escapeHtml(item.id)}</option>`
    )).join("");
    if (projects.some((item) => item.id === previous)) select.value = previous;
  }

  async function loadDiagnostics() {
    const projectId = document.getElementById("operations-diagnostic-project")?.value || "";
    const host = document.getElementById("operations-diagnostics");
    if (!host) return;
    host.innerHTML = "<small>Loading diagnostics...</small>";
    try {
      const suffix = projectId ? `?project_id=${encodeURIComponent(projectId)}` : "";
      const value = await apiRequest(`/api/diagnostics${suffix}`);
      host.innerHTML = `<div class="comm-entry">
        <strong>Diagnostics snapshot${projectId ? ` · project ${escapeHtml(projectId)}` : ""}</strong>
        <small>Server-produced diagnostic state is telemetry/support data and is not canonical business state.</small>
        <pre>${escapeHtml(JSON.stringify(value, null, 2))}</pre>
      </div>`;
    } catch (error) {
      host.innerHTML = `<div class="comm-entry"><strong>Diagnostics unavailable</strong><small>${escapeHtml(error.message)}</small></div>`;
    }
  }

  async function resumeRecovery() {
    if (!canRecover()) {
      setStatus("Runtime recovery requires human admin + MFA/step-up or the runtime:admin service scope.");
      return;
    }
    if (!window.confirm(
      "Resume stale active threads and schedule drains for queued turns? This changes runtime execution scheduling but does not mark Work Items successful or bypass approvals.",
    )) return;
    try {
      const result = await apiRequest("/api/recovery/resume", { method: "POST" });
      setStatus(`Recovery scheduled · stale threads: ${result.resumingStaleThreads?.length || 0} · active turns: ${result.activeTurns} · queued turns: ${result.queuedTurns}.`);
      await refresh();
    } catch (error) {
      setStatus(`Runtime recovery failed: ${error.message}`);
    }
  }

  async function refreshOnce() {
    const windowSeconds = Number(document.getElementById("operations-window")?.value || 900);
    setStatus("Loading operator telemetry...");
    try {
      const [obs, ops, assignmentResponse, me] = await Promise.all([
        apiRequest("/api/observability"),
        apiRequest(`/api/operations?window_seconds=${encodeURIComponent(windowSeconds)}`),
        apiRequest("/api/execution-workers/assignments").catch(() => ({ items: [] })),
        apiRequest("/api/identity/me"),
        apiRequest("/api/projects")
          .then((value) => { projects = Array.isArray(value) ? value : (value.items || []); })
          .catch(() => { projects = []; }),
      ]);
      observability = obs;
      operations = ops;
      assignments = assignmentResponse.items || [];
      actor = me;
      renderOverview();
      renderHealth();
      renderRuntime();
      renderProviders();
      renderMetrics();
      renderTraces();
      populateProjects();
      const recovery = document.getElementById("resume-runtime-recovery");
      if (recovery) recovery.disabled = !canRecover();
      setStatus(`Operator telemetry loaded · ${observability.traceCount || 0} recent trace(s) · window ${duration(operations.windowSeconds)}. Telemetry is explanatory, not canonical state.`);
    } catch (error) {
      setStatus(`Operations/Observability unavailable: ${error.message}`);
    }
  }

  function refresh() {
    if (refreshPromise) return refreshPromise;
    refreshPromise = refreshOnce().finally(() => {
      refreshPromise = null;
    });
    return refreshPromise;
  }

  function bind() {
    window.addEventListener("codex:execution-worker-state-rendered", (event) => {
      workers = event.detail?.workers || [];
      if (event.detail?.assignments?.length) assignments = event.detail.assignments;
      renderOverview();
    });
    const panel = document.getElementById("developer-panel");
    document.getElementById("refresh-operations")?.addEventListener("click", refresh);
    document.getElementById("refresh-developer")?.addEventListener("click", refresh);
    document.getElementById("operations-window")?.addEventListener("change", () => refresh().catch(console.error));
    document.getElementById("operations-trace-search")?.addEventListener("input", renderTraces);
    document.getElementById("load-diagnostics")?.addEventListener("click", () => loadDiagnostics().catch(console.error));
    document.getElementById("resume-runtime-recovery")?.addEventListener("click", () => resumeRecovery().catch(console.error));
    panel?.addEventListener("toggle", () => {
      if (panel.open) refresh().catch(console.error);
    });
    if (panel?.open) refresh().catch(console.error);
    window.__codexOperationsReady = true;
    window.dispatchEvent(new CustomEvent("codex:operations-ready"));
  }

  if (document.readyState === "loading") window.addEventListener("DOMContentLoaded", bind, { once: true });
  else bind();
})();
