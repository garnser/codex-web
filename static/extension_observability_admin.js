(async () => {
  const BASE = window.location.pathname.startsWith("/codex") ? "/codex" : "";
  const { request: apiRequest } = await import(`${BASE}/static/api_client.js`);
  const MAX_AUDIT_ROWS = 100;
  let generation = 0;

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");
  }

  function timeText(value) {
    if (!value) return "unknown";
    const date = new Date(Number(value) * 1000);
    return Number.isNaN(date.valueOf()) ? "unknown" : date.toLocaleString();
  }

  function listText(values) {
    return values?.length ? values.map(escapeHtml).join(", ") : "none";
  }

  function detailHost(installationId) {
    return Array.from(document.querySelectorAll("[data-extension-details-host]"))
      .find((element) => element.dataset.extensionDetailsHost === installationId) || null;
  }

  function renderInstallationDetails(item) {
    const host = detailHost(item.id);
    if (!host) return;
    const manifest = item.manifest || {};
    const provenance = manifest.provenance || {};
    const events = manifest.events || {};
    const history = item.manifest_history || [];
    const verification = item.package_verification || {};
    const health = manifest.health || {};
    host.innerHTML = `<div class="comm-entry">
      <small>Compatibility: ${escapeHtml(manifest.compatibility?.codex_web || "unknown")} · Package ref: ${escapeHtml(item.package_ref || "none")}</small>
      <small>Provenance source: ${escapeHtml(provenance.source || "unknown")} · Declared digest: ${escapeHtml(provenance.digest || "unknown")}</small>
      <small>Observed digest: ${escapeHtml(verification.observed_digest || "unknown")} · Verifier: ${escapeHtml(verification.verifier || "unknown")} · Verified: ${timeText(verification.verified_at)}</small>
      <small>Subscribes: ${listText(events.subscribes)} · Publishes: ${listText(events.publishes)}</small>
      <small>Health interval: ${escapeHtml(health.interval_seconds ?? "unknown")}s · timeout: ${escapeHtml(health.timeout_seconds ?? "unknown")}s · failures: ${escapeHtml(item.consecutive_health_failures ?? 0)}</small>
      <small>Last health: ${timeText(item.last_health_at)}${item.last_health_detail ? ` · ${escapeHtml(item.last_health_detail)}` : ""}</small>
      <small>Migration entrypoint: ${escapeHtml(manifest.migrations?.entrypoint || "none")} · Previous versions: ${history.length ? history.map((entry) => escapeHtml(entry.version)).join(", ") : "none"}</small>
      ${item.incompatible_reason ? `<small>Incompatible: ${escapeHtml(item.incompatible_reason)}</small>` : ""}
      ${item.removal_reason ? `<small>Removal: ${escapeHtml(item.removal_reason)} · ${timeText(item.removed_at)}</small>` : ""}
    </div>`;
  }

  function renderAudit(events) {
    const status = document.getElementById("extension-audit-status");
    const list = document.getElementById("extension-audit-list");
    if (!status || !list) return;
    const rows = (events || []).slice(0, MAX_AUDIT_ROWS);
    status.hidden = false;
    status.textContent = `${events.length} canonical event(s) · showing ${rows.length} most recent`;
    list.innerHTML = rows.map((event) => {
      const details = Object.entries(event.details || {})
        .map(([key, value]) => `${escapeHtml(key)}=${escapeHtml(value)}`)
        .join(" · ");
      return `<div class="comm-entry">
        <strong>${escapeHtml(event.event_type)} · ${escapeHtml(event.extension_id)}</strong>
        <small>${timeText(event.occurred_at)} · actor ${escapeHtml(event.actor_id)} · installation ${escapeHtml(event.installation_id)}</small>
        ${details ? `<small>${details}</small>` : ""}
      </div>`;
    }).join("") || '<div class="comm-entry"><strong>No extension audit events</strong></div>';
  }

  async function hydrate(installations) {
    const current = ++generation;
    installations.forEach(renderInstallationDetails);
    const status = document.getElementById("extension-audit-status");
    if (status) status.textContent = "Loading canonical extension audit...";
    try {
      const response = await apiRequest("/api/extensions/events");
      if (current !== generation) return;
      renderAudit(response.items || []);
    } catch (error) {
      if (current !== generation) return;
      if (status) status.textContent = `Extension audit unavailable: ${error.message}`;
      const list = document.getElementById("extension-audit-list");
      if (list) list.innerHTML = "";
    }
  }

  window.addEventListener("codex:extension-state-rendered", (event) => {
    hydrate(event.detail?.installations || []).catch(console.error);
  });
})();
