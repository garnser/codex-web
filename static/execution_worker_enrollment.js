export function installExecutionWorkerEnrollment({
  apiRequest,
  canManage,
  setStatus,
  escapeHtml,
  base = "",
}) {
  function capabilities() {
    return Array.from(
      document.querySelectorAll("#execution-worker-enrollment-capabilities input[type='checkbox']:checked"),
      (node) => node.value,
    );
  }

  function render(items = []) {
    const host = document.getElementById("execution-worker-enrollment-list");
    if (!host) return;
    const now = Date.now() / 1000;
    host.innerHTML = items.slice(0, 20).map((item) => {
      const state = item.used_at ? "used" : item.expires_at <= now ? "expired" : "ready";
      return `<div class="comm-entry">
        <strong>${escapeHtml(item.pool)} · ${escapeHtml(item.service_identity_id)} · ${escapeHtml(state)}</strong>
        <small>Enrollment: ${escapeHtml(item.id)} · expires: ${new Date(Number(item.expires_at) * 1000).toLocaleString()} · worker: ${escapeHtml(item.worker_id || "not enrolled")}</small>
        <small>Capabilities: ${escapeHtml((item.allowed_capabilities || []).join(", "))} · max concurrency: ${escapeHtml(item.max_concurrency_ceiling)}</small>
      </div>`;
    }).join("") || '<div class="comm-entry"><small>No runtime enrollments created.</small></div>';
  }

  async function refresh() {
    if (!canManage()) return render([]);
    try {
      const response = await apiRequest("/api/execution-workers/enrollments");
      render(response.items || []);
    } catch (error) {
      const host = document.getElementById("execution-worker-enrollment-result");
      if (host) host.textContent = `Enrollment history unavailable: ${error.message}`;
    }
  }

  async function create() {
    if (!canManage()) {
      setStatus("Runtime enrollment requires canonical admin authority and elevated assurance.");
      return;
    }
    const identity = document.getElementById("execution-worker-enrollment-identity")?.value?.trim() || "";
    const pool = document.getElementById("execution-worker-enrollment-pool")?.value?.trim() || "local";
    const expires = Number(document.getElementById("execution-worker-enrollment-expiry")?.value || 300);
    const concurrency = Number(document.getElementById("execution-worker-enrollment-concurrency")?.value || 1);
    const allowed = capabilities();
    const result = document.getElementById("execution-worker-enrollment-result");
    if (!identity || !allowed.length) {
      if (result) result.textContent = "Service identity and at least one allowed capability are required.";
      return;
    }
    try {
      const response = await apiRequest("/api/execution-workers/enrollments", {
        method: "POST",
        body: JSON.stringify({
          service_identity_id: identity,
          pool,
          expires_in_seconds: expires,
          allowed_capabilities: allowed,
          max_concurrency_ceiling: concurrency,
        }),
      });
      const endpoint = `${window.location.origin}${base}/api/execution-workers/enroll`;
      const command = `curl -sS -X POST '${endpoint}' -H 'Content-Type: application/json' --data '{"token":"${response.token}","version":"<runtime-version>","capabilities":${JSON.stringify(allowed)},"max_concurrency":${concurrency},"probe_results":{}}'`;
      if (result) {
        result.innerHTML = `<strong>Enrollment created. Token is shown only in this response and expires at ${escapeHtml(new Date(Number(response.item.expires_at) * 1000).toLocaleString())}.</strong><pre data-runtime-enrollment-command></pre>`;
        result.querySelector("[data-runtime-enrollment-command]").textContent = command;
      }
      await refresh();
    } catch (error) {
      if (result) result.textContent = `Runtime enrollment failed: ${error.message}`;
    }
  }

  document.getElementById("create-execution-worker-enrollment")
    ?.addEventListener("click", () => create().catch(console.error));
  document.getElementById("execution-worker-enrollment-panel")
    ?.addEventListener("toggle", (event) => {
      if (event.currentTarget.open) refresh().catch(console.error);
    });
  refresh().catch(console.error);
  return { refresh };
}
