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

  function timeText(value) {
    if (!value) return "none";
    const date = new Date(Number(value) * 1000);
    return Number.isNaN(date.valueOf()) ? "unknown" : date.toLocaleString();
  }

  function setStatus(message) {
    const host = document.getElementById("operations-log-status");
    if (host) {
      host.hidden = false;
      host.textContent = message;
    }
  }

  function value(id) {
    return document.getElementById(id)?.value.trim() || "";
  }

  function queryParams() {
    const params = new URLSearchParams({
      window_seconds: value("operations-log-window") || "900",
      limit: "100",
    });
    const filters = [
      ["level", "operations-log-level"],
      ["logger", "operations-log-logger"],
      ["event", "operations-log-event"],
      ["correlation_id", "operations-log-correlation"],
      ["causation_id", "operations-log-causation"],
      ["work_item_ref", "operations-log-work-item"],
      ["execution_id", "operations-log-execution"],
      ["action_intent_id", "operations-log-action-intent"],
    ];
    for (const [parameter, id] of filters) {
      const current = value(id);
      if (current) params.set(parameter, current);
    }
    return params;
  }

  function fieldText(fields) {
    const entries = Object.entries(fields || {});
    if (!entries.length) return "none";
    return entries.map(([key, item]) => {
      const rendered = typeof item === "object" ? JSON.stringify(item) : item;
      return `${escapeHtml(key)}=${escapeHtml(rendered)}`;
    }).join(" · ");
  }

  function render(payload) {
    const host = document.getElementById("operations-logs");
    if (!host) return;
    const rows = payload.items || [];
    const retention = payload.retention || {};
    host.innerHTML = `<div class="comm-entry">
      <strong>Structured runtime logs · ${escapeHtml(payload.classification || "unknown")}</strong>
      <small>${escapeHtml(payload.count || 0)} row(s) · storage ${escapeHtml(retention.storage || "unknown")} · max ${escapeHtml(retention.maxEntries || "unknown")} entries / ${escapeHtml(retention.maxAgeSeconds || "unknown")} seconds.</small>
      <small>${escapeHtml(payload.payloadPolicy || "")}</small>
      <small>Logs are bounded runtime telemetry for diagnosis. They are not canonical Work/Goal/Decision/ActionIntent state.</small>
    </div>` + rows.map((item) => `<details class="comm-entry">
      <summary><strong>${escapeHtml(item.level)} · ${escapeHtml(item.event || item.logger)} · ${timeText(item.timestamp)}</strong></summary>
      <small>Logger: ${escapeHtml(item.logger)} · correlation: ${escapeHtml(item.correlationId || "none")} · causation: ${escapeHtml(item.causationId || "none")}</small>
      <small>Work: ${escapeHtml(item.workItemRef || "none")} · execution: ${escapeHtml(item.executionId || "none")} · ActionIntent: ${escapeHtml(item.actionIntentId || "none")}</small>
      <small>Safe structured fields: ${fieldText(item.fields)}</small>
    </details>`).join("");
    if (!rows.length) {
      host.innerHTML += '<div class="comm-entry"><strong>No tenant-scoped structured logs match the current filters.</strong></div>';
    }
  }

  async function loadLogs() {
    setStatus("Loading bounded tenant-scoped structured logs...");
    try {
      const payload = await apiRequest(`/api/logs?${queryParams().toString()}`);
      render(payload);
      setStatus(`Loaded ${payload.count || 0} structured log row(s). Free-form messages, exceptions, prompts, tokens and secret values are not returned by this API.`);
    } catch (error) {
      setStatus(`Structured log query failed: ${error.message}`);
      const host = document.getElementById("operations-logs");
      if (host) host.innerHTML = "";
    }
  }

  function clearFilters() {
    for (const id of [
      "operations-log-level",
      "operations-log-logger",
      "operations-log-event",
      "operations-log-correlation",
      "operations-log-causation",
      "operations-log-work-item",
      "operations-log-execution",
      "operations-log-action-intent",
    ]) {
      const element = document.getElementById(id);
      if (element) element.value = "";
    }
    loadLogs().catch(console.error);
  }

  function drillFromTrace(button) {
    const correlationId = button.dataset.operationsLogCorrelation || "";
    if (!correlationId) return;
    const input = document.getElementById("operations-log-correlation");
    if (input) input.value = correlationId;
    const panel = document.getElementById("operations-logs")?.closest("details");
    if (panel) panel.open = true;
    loadLogs().catch(console.error);
  }

  function bind() {
    document.getElementById("load-structured-logs")?.addEventListener("click", () => loadLogs().catch(console.error));
    document.getElementById("clear-structured-log-filters")?.addEventListener("click", clearFilters);
    document.getElementById("operations-log-window")?.addEventListener("change", () => loadLogs().catch(console.error));
    document.getElementById("operations-log-level")?.addEventListener("change", () => loadLogs().catch(console.error));
    document.getElementById("operations-traces")?.addEventListener("click", (event) => {
      const button = event.target.closest?.("[data-operations-log-correlation]");
      if (button) drillFromTrace(button);
    });
  }

  window.addEventListener("DOMContentLoaded", bind);
})();
