(async () => {
  const BASE = window.location.pathname.startsWith("/codex") ? "/codex" : "";
  const { request: apiRequest } = await import(`${BASE}/static/api_client.js`);
  await import(`${BASE}/static/control_plane_ui.js`);
  const { captureProjectView, currentProjectId } = await import(`${BASE}/static/project_view_scope.js`);
  let selectionGeneration = 0, refreshGeneration = 0;
  let refreshTimer = null;

  function currentThreadId() {
    const value = document.getElementById("thread-meta")?.textContent || "";
    const separator = value.indexOf(" · ");
    const candidate = (separator >= 0 ? value.slice(0, separator) : value).trim();
    if (!candidate || candidate === "Create or select a thread.") return null;
    return candidate;
  }

  function setStatus(text, { error = false } = {}) {
    const status = document.getElementById("context-compact-status");
    if (!status) return;
    status.textContent = text;
    status.dataset.error = error ? "true" : "false";
  }

  function captureSelection() {
    const project = captureProjectView(), generation = selectionGeneration;
    const threadId = currentThreadId(), projectId = currentProjectId();
    return { threadId, projectId,
      current: () => project.current() && generation === selectionGeneration && currentThreadId() === threadId,
    };
  }

  function contextRequest(view, suffix, options) {
    const query = new URLSearchParams({ project_id: view.projectId });
    return apiRequest(`/api/threads/${encodeURIComponent(view.threadId)}/${suffix}?${query}`, options);
  }

  function statusText(status) {
    const auto = status.autoEnabled
      ? `Auto-compacts at ${Math.round(status.autoThresholdPercent)}%`
      : "Auto-compaction disabled";
    if (status.inProgress) return "Compacting context…";
    if (status.blockedReason === "turn_active") return `${auto} · waiting for active turn`;
    if (status.blockedReason === "queued_turns") return `${auto} · waiting for queued work`;
    if (status.blockedReason === "approval_pending") return `${auto} · waiting for approval`;
    if (status.usagePercent != null) return `${auto} · ${status.usagePercent}% observed`;
    return auto;
  }

  async function refresh() {
    const button = document.getElementById("compact-context");
    if (!button) return;
    const view = captureSelection(), generation = ++refreshGeneration;
    const threadId = view.threadId;
    if (!threadId) {
      button.disabled = true;
      setStatus("Select a thread to manage context");
      return;
    }
    try {
      const status = await contextRequest(view, "context");
      if (!view.current() || generation !== refreshGeneration) return;
      button.disabled = !status.eligible || status.inProgress;
      setStatus(statusText(status));
    } catch (error) {
      if (!view.current() || generation !== refreshGeneration) return;
      button.disabled = true;
      setStatus(`Context status unavailable: ${error.message}`, { error: true });
    }
  }

  async function compact() {
    const button = document.getElementById("compact-context");
    const view = captureSelection();
    if (!button || !view.threadId) return;
    button.disabled = true;
    setStatus("Requesting native Codex compaction…");
    try {
      await contextRequest(view, "compact", { method: "POST" });
      if (!view.current()) return;
      setStatus("Compaction accepted by Codex; refreshing usage…");
      window.setTimeout(() => { if (view.current()) document.getElementById("refresh-token-usage")?.click(); }, 1000);
      window.setTimeout(() => { if (view.current()) refresh(); }, 1500);
    } catch (error) {
      if (!view.current()) return;
      setStatus(`Compaction failed: ${error.message}`, { error: true });
      window.setTimeout(() => { if (view.current()) refresh(); }, 1500);
    }
  }

  function scheduleRefresh(delay = 100) {
    if (refreshTimer) window.clearTimeout(refreshTimer);
    refreshTimer = window.setTimeout(() => {
      refreshTimer = null;
      refresh();
    }, delay);
  }

  function bind() {
    document.getElementById("compact-context")?.addEventListener("click", compact);

    const meta = document.getElementById("thread-meta");
    if (meta) {
      new MutationObserver(() => {
        selectionGeneration += 1;
        scheduleRefresh(0);
      }).observe(meta, { childList: true, characterData: true, subtree: true });
    }

    const tokenContext = document.getElementById("token-context");
    if (tokenContext) {
      new MutationObserver(() => scheduleRefresh(150)).observe(tokenContext, {
        childList: true,
        characterData: true,
        subtree: true,
      });
    }

    scheduleRefresh(0);
    window.setInterval(refresh, 10000);
  }
  if (document.readyState === "loading") window.addEventListener("DOMContentLoaded", bind, { once: true });
  else bind();
})();
