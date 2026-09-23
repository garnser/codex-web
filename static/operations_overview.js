import { metadataGrid, surfaceCard, statePanel } from "./workspace_components.js";

function timeText(value) {
  if (!value) return "none";
  const date = new Date(Number(value) * 1000);
  return Number.isNaN(date.valueOf()) ? "unknown" : date.toLocaleString();
}

function assignmentFailure(item) {
  const failure = item?.failure || null;
  return {
    reason: failure?.reason_code || failure?.reason || item?.failure_code || null,
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

export function renderOperationsOverview({
  observability,
  operations,
  assignments = [],
  workers = [],
} = {}) {
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

  const runtimeCard = surfaceCard({
    title: "Runtime",
    description: "Canonical service health and current execution pressure.",
    status: health.status || (runtime.healthy ? "healthy" : "degraded"),
    body: metadataGrid([
      { label: "Readiness", value: health.readiness ? "ready" : "blocked" },
      { label: "Active turns", value: runtime.activeTurns ?? 0 },
      { label: "Queued turns", value: runtime.queuedTurns ?? 0 },
      { label: "Pending approvals", value: runtime.pendingApprovals ?? 0 },
    ]),
  });

  const workerStatus = lifecycleCounts.quarantined || lifecycleCounts.revoked || lifecycleCounts.offline
    ? "degraded"
    : lifecycleCounts.draining
      ? "warning"
      : (workers.length ? "healthy" : "unknown");
  const workerCard = surfaceCard({
    title: "Execution workers",
    description: workers.length
      ? "Lifecycle is canonical worker state; assignment eligibility remains server-authoritative."
      : "Worker inventory has not been loaded yet.",
    status: workerStatus,
    body: metadataGrid([
      { label: "Active", value: lifecycleCounts.active || 0 },
      { label: "Draining", value: lifecycleCounts.draining || 0 },
      { label: "Quarantined", value: lifecycleCounts.quarantined || 0 },
      { label: "Offline / revoked", value: (lifecycleCounts.offline || 0) + (lifecycleCounts.revoked || 0) },
    ]),
  });

  const providerCard = surfaceCard({
    title: "Provider activity",
    description: "Observed connectivity and delivery telemetry; this does not invent provider readiness.",
    body: metadataGrid([
      { label: "Runtime connections", value: provider.runtimeConnections ?? "—" },
      { label: "Recent delivery failures", value: provider.recentDeliveryFailures ?? "—" },
      { label: "GitLab sync failures", value: provider.gitlabSyncConsecutiveFailures ?? "—" },
      { label: "Last GitLab success", value: timeText(provider.gitlabSyncLastSuccessAt) },
    ]),
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
