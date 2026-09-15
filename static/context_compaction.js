(() => {
  const BASE = window.location.pathname.startsWith("/codex") ? "/codex" : "";
  let refreshTimer = null;
  let lastThreadId = null;

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

  async function getStatus(threadId) {
    const response = await fetch(`${BASE}/api/threads/${encodeURIComponent(threadId)}/context`, {
      headers: { "Content-Type": "application/json" },
    });
    if (!response.ok) throw new Error(await response.text() || response.statusText);
    return response.json();
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
    const threadId = currentThreadId();
    lastThreadId = threadId;
    if (!threadId) {
      button.disabled = true;
      setStatus("Select a thread to manage context");
      return;
    }
    try {
      const status = await getStatus(threadId);
      if (currentThreadId() !== threadId) return;
      button.disabled = !status.eligible || status.inProgress;
      setStatus(statusText(status));
    } catch (error) {
      button.disabled = true;
      setStatus(`Context status unavailable: ${error.message}`, { error: true });
    }
  }

  async function compact() {
    const button = document.getElementById("compact-context");
    const threadId = currentThreadId();
    if (!button || !threadId) return;
    button.disabled = true;
    setStatus("Requesting native Codex compaction…");
    try {
      const response = await fetch(`${BASE}/api/threads/${encodeURIComponent(threadId)}/compact`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        const detail = payload?.detail;
        const reason = typeof detail === "object" ? detail.reason || detail.code : detail;
        throw new Error(reason || response.statusText);
      }
      setStatus("Compaction accepted by Codex; refreshing usage…");
      window.setTimeout(() => document.getElementById("refresh-token-usage")?.click(), 1000);
      window.setTimeout(refresh, 1500);
    } catch (error) {
      setStatus(`Compaction failed: ${error.message}`, { error: true });
      window.setTimeout(refresh, 1500);
    }
  }

  function scheduleRefresh(delay = 100) {
    if (refreshTimer) window.clearTimeout(refreshTimer);
    refreshTimer = window.setTimeout(() => {
      refreshTimer = null;
      refresh();
    }, delay);
  }

  window.addEventListener("DOMContentLoaded", () => {
    document.getElementById("compact-context")?.addEventListener("click", compact);

    const meta = document.getElementById("thread-meta");
    if (meta) {
      new MutationObserver(() => {
        const threadId = currentThreadId();
        if (threadId !== lastThreadId) scheduleRefresh(0);
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
  });
})();
