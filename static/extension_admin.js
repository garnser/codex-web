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

  function extensionLifecycleActions(item) {
    const lifecycle = String(item.lifecycle || "");
    const actions = [];
    if (["installed", "configured", "disabled"].includes(lifecycle)) {
      actions.push(["enable", "Enable"]);
    }
    if (!["disabled", "removed", "incompatible"].includes(lifecycle)) {
      actions.push(["disable", "Disable"]);
    }
    if (!["quarantined", "removed"].includes(lifecycle)) {
      actions.push(["quarantine", "Quarantine"]);
    }
    if (lifecycle === "quarantined") {
      actions.push(["clear-quarantine", "Clear quarantine"]);
    }
    if (!actions.length) return "";
    const id = escapeHtml(item.id);
    const label = escapeHtml(item.manifest?.id || item.id);
    const state = escapeHtml(lifecycle);
    return `<div class="developer-toolbar">${actions.map(([action, title]) => (
      `<button type="button" class="ghost-button" data-extension-action="${action}" data-extension-id="${id}" data-extension-label="${label}" data-extension-lifecycle="${state}">${title}</button>`
    )).join("")}</div>`;
  }

  function renderExtensionAdmin(installations, packages) {
    const list = document.getElementById("extension-admin-list");
    const status = document.getElementById("extension-admin-status");
    if (!list || !status) return;
    const packageErrors = packages?.errors || [];
    const packageCount = packages?.items?.length || 0;
    status.hidden = false;
    status.textContent = `${installations.length} installed · ${packageCount} discovered package(s)${packageErrors.length ? ` · ${packageErrors.length} package error(s)` : ""}`;
    if (!installations.length) {
      list.innerHTML = '<div class="comm-entry"><strong>No extensions installed</strong><small>Discovered packages remain available through the canonical extension API.</small></div>';
      return;
    }
    list.innerHTML = installations.map((item) => {
      const manifest = item.manifest || {};
      const verification = item.package_verification || {};
      const capabilities = manifest.capabilities || {};
      const requested = capabilities.requested || [];
      return `<div class="comm-entry">
        <strong>${escapeHtml(manifest.id || item.id)} @ ${escapeHtml(manifest.version || "unknown")}</strong>
        <small>Lifecycle: ${escapeHtml(item.lifecycle)} · Health: ${escapeHtml(item.health_status)} · Deployment: ${escapeHtml(item.deployment_mode)}</small>
        <small>Publisher: ${escapeHtml(manifest.publisher?.name || manifest.publisher?.id || "unknown")} · Digest verified: ${verification.digest_verified ? "yes" : "no"} · Signature: ${escapeHtml(verification.signature_status || "unknown")}</small>
        <small>Requested capabilities: ${requested.length ? requested.map(escapeHtml).join(", ") : "none"} · Config refs: ${item.configuration_record_ids?.length || 0} · Secret bindings: ${Object.keys(item.secret_bindings || {}).length}</small>
        ${item.quarantine_reason ? `<small>Quarantine reason: ${escapeHtml(item.quarantine_reason)}</small>` : ""}
        ${item.disabled_reason ? `<small>Disabled reason: ${escapeHtml(item.disabled_reason)}</small>` : ""}
        ${extensionLifecycleActions(item)}
      </div>`;
    }).join("");
  }

  async function refreshExtensionAdmin() {
    const status = document.getElementById("extension-admin-status");
    if (status) {
      status.hidden = false;
      status.textContent = "Loading extension state...";
    }
    try {
      const extensions = await apiRequest("/api/extensions");
      let packages = { items: [], errors: [] };
      try {
        packages = await apiRequest("/api/extensions/packages");
      } catch (error) {
        packages = { items: [], errors: [{ detail: error.message }] };
      }
      renderExtensionAdmin(extensions.items || [], packages);
    } catch (error) {
      if (status) status.textContent = `Unable to load extensions: ${error.message}`;
      const list = document.getElementById("extension-admin-list");
      if (list) list.innerHTML = "";
    }
  }

  async function mutateExtensionLifecycle(button) {
    const action = button.dataset.extensionAction;
    const installationId = button.dataset.extensionId;
    const label = button.dataset.extensionLabel || installationId;
    const lifecycle = button.dataset.extensionLifecycle || "unknown";
    if (!action || !installationId) return;

    let reason = null;
    if (["disable", "quarantine", "clear-quarantine"].includes(action)) {
      reason = window.prompt(`Reason for ${action.replace("-", " ")} of ${label}:`, "");
      if (reason === null) return;
    }
    const confirmation = `${action.replace("-", " ")} ${label}? Current lifecycle: ${lifecycle}. This updates canonical extension state.`;
    if (!window.confirm(confirmation)) return;

    const status = document.getElementById("extension-admin-status");
    button.disabled = true;
    if (status) status.textContent = `Applying ${action.replace("-", " ")} to ${label}...`;
    try {
      const options = { method: "POST" };
      if (action !== "enable") {
        options.body = JSON.stringify({ reason: reason?.trim() || null });
      }
      await apiRequest(
        `/api/extensions/${encodeURIComponent(installationId)}/${action}`,
        options,
      );
      await refreshExtensionAdmin();
    } catch (error) {
      button.disabled = false;
      if (status) status.textContent = `Extension lifecycle update failed: ${error.message}`;
    }
  }

  function bindExtensionAdmin() {
    const panel = document.getElementById("developer-panel");
    const refresh = document.getElementById("refresh-extensions");
    const refreshDeveloper = document.getElementById("refresh-developer");
    const list = document.getElementById("extension-admin-list");

    refresh?.addEventListener("click", refreshExtensionAdmin);
    refreshDeveloper?.addEventListener("click", refreshExtensionAdmin);
    panel?.addEventListener("toggle", () => {
      if (panel.open) refreshExtensionAdmin().catch(console.error);
    });
    list?.addEventListener("click", (event) => {
      const button = event.target.closest?.("[data-extension-action]");
      if (!button) return;
      mutateExtensionLifecycle(button).catch(console.error);
    });

    if (panel?.open) refreshExtensionAdmin().catch(console.error);
  }

  window.addEventListener("DOMContentLoaded", bindExtensionAdmin);
})();
