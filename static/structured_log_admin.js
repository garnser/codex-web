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
    const host = document.getElementById("structured-log-status");
    if (host) {
      host.hidden = false;
      host.textContent = message;
    }
  }

  function value(id) {
    return (document.getElementById(id)?.value || "").trim();
  }

  function queryParams() {
    const params = new URLSearchParams();
    const windowSeconds = value("structured-log-window") || "900";
    const limit = value("structured-log-limit") || "50";
    params.set("window_seconds", windowSeconds);
    params.set("limit", limit);
    for (const [id, key] of [
      ["structured-log-level", "level"],
      ["structured-log-logger", "logger_name"],
      ["structured-log-event", "event"],
      ["structured-log-correlation", "correlation_id"],
      ["structured-log-causation", "causation_id"],
      ["structured-log-work-item", "work_item_ref"],
      ["structured-log-execution", "execution_id"],
      ["structured-log-action-intent", "action_intent_id"],
    ]) {
      const current = value(id);
      if (current) params.set(key, current);
    }
    return params;
  }

  function renderItem(item) {
    const fields = item.fields || {};
    return `<details class="comm-entry">
      <summary><strong>${escapeHtml(item.level)} · ${escapeHtml(item.event || "structured event")} · ${escapeHtml(item.logger)}</strong></summary>
      <small>${timeText(item.occurredAt)} · classification ${escapeHtml(item.classification)} · retention until ${timeText(item.retentionExpiresAt)}</small>
      <small>Correlation: ${escapeHtml(item.correlationId || "none")} · causation: ${escapeHtml(item.causationId || "none")}</small>
      <small>Work: ${escapeHtml(item.workItemRef || "none")} · execution: ${escapeHtml(item.executionId || "none")} · ActionIntent: ${escapeHtml(item.actionIntentId || "none")}</small>
      <small>Runtime telemetry only. Free-form log message and exception text are intentionally not returned by the canonical log API.</small>
      <pre>${escapeHtml(JSON.stringify(fields, null, 2))}</pre>
    </details>`;
  }

  async function refresh() {
    const host = document.getElementById("structured-log-list");
    if (!host) return;
    setStatus("Loading bounded tenant-scoped structured logs...");
    try {
      const response = await apiRequest(`/api/logs/recent?${queryParams().toString()}`);
      const items = response.items || [];
      host.innerHTML = items.map(renderItem).join("")
        || '<div class="comm-entry"><strong>No structured logs match the current filters.</strong></div>';
      setStatus(
        `${response.count || 0} structured log(s)${response.truncated ? " · truncated by row/payload cap" : ""} · classification ${response.classification || "internal"} · retention ${response.retentionSeconds || "?"}s · max payload ${response.maxResultBytes || "?"} bytes. Telemetry is explanatory, not canonical business state.`,
      );
    } catch (error) {
      host.innerHTML = "";
      setStatus(`Structured logs unavailable: ${error.message}`);
    }
  }

  function useTraceCorrelation(button) {
    const correlationId = button.dataset.logCorrelation;
    if (!correlationId) return;
    const input = document.getElementById("structured-log-correlation");
    if (input) input.value = correlationId;
    document.getElementById("structured-log-panel")?.scrollIntoView({ block: "nearest" });
    refresh().catch(console.error);
  }

  function bind() {
    const panel = document.getElementById("developer-panel");
    document.getElementById("refresh-structured-logs")?.addEventListener("click", refresh);
    document.getElementById("refresh-developer")?.addEventListener("click", refresh);
    for (const id of [
      "structured-log-window",
      "structured-log-limit",
      "structured-log-level",
    ]) {
      document.getElementById(id)?.addEventListener("change", () => refresh().catch(console.error));
    }
    document.getElementById("structured-log-filters")?.addEventListener("submit", (event) => {
      event.preventDefault();
      refresh().catch(console.error);
    });
    document.addEventListener("click", (event) => {
      const button = event.target.closest?.("[data-log-correlation]");
      if (button) useTraceCorrelation(button);
    });
    panel?.addEventListener("toggle", () => {
      if (panel.open) refresh().catch(console.error);
    });
    if (panel?.open) refresh().catch(console.error);
  }

  window.addEventListener("DOMContentLoaded", bind);
})();
