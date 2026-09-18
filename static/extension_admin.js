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
    if (!["enabled", "removed"].includes(lifecycle)) {
      actions.push(["remove", "Remove"]);
    }
    if (!actions.length) return "";
    const id = escapeHtml(item.id);
    const label = escapeHtml(item.manifest?.id || item.id);
    const state = escapeHtml(lifecycle);
    return `<div class="developer-toolbar">${actions.map(([action, title]) => (
      `<button type="button" class="ghost-button" data-extension-action="${action}" data-extension-id="${id}" data-extension-label="${label}" data-extension-lifecycle="${state}">${title}</button>`
    )).join("")}</div>`;
  }

  function resourceScopeText(resourceIds, resources) {
    if (!resourceIds?.length) return "workspace-wide (no resource restriction)";
    const byId = new Map(resources.map((item) => [item.id, item]));
    return resourceIds.map((id) => {
      const resource = byId.get(id);
      return resource ? `${resource.name} (${id})` : id;
    }).join(", ");
  }

  function capabilityAdministration(item, grantsState, resources, resourceError) {
    const capabilities = item.manifest?.capabilities || {};
    const requested = capabilities.requested || [];
    if (!requested.length) return '<small>Authorization: no capabilities requested.</small>';
    if (grantsState.error) {
      return `<small>Authorization unavailable: ${escapeHtml(grantsState.error)}</small>`;
    }

    const mandatory = new Set(capabilities.mandatory || []);
    const grants = grantsState.items || [];
    const active = new Map(
      grants.filter((grant) => grant.revoked_at == null).map((grant) => [grant.capability, grant]),
    );
    const id = escapeHtml(item.id);
    const removed = item.lifecycle === "removed";
    return `<div class="extension-capabilities">
      <small>Authorization (separate from installation/enablement):</small>
      ${requested.map((capability) => {
        const grant = active.get(capability);
        const capabilityText = escapeHtml(capability);
        const requiredText = mandatory.has(capability) ? " · mandatory" : "";
        if (grant) {
          const warning = mandatory.has(capability) && item.lifecycle === "enabled"
            ? " Revoking this mandatory grant will quarantine the enabled extension."
            : "";
          return `<div class="comm-entry" data-extension-capability-row data-extension-id="${id}" data-capability="${capabilityText}">
            <strong>${capabilityText}</strong>
            <small>Granted${requiredText} · Scope: ${escapeHtml(resourceScopeText(grant.resource_ids, resources))}</small>
            ${removed ? "" : `<button type="button" class="ghost-button" data-extension-grant-action="revoke" data-grant-id="${escapeHtml(grant.id)}" data-mandatory="${mandatory.has(capability) ? "true" : "false"}" data-extension-lifecycle="${escapeHtml(item.lifecycle)}" title="${escapeHtml(warning.trim())}">Revoke</button>`}
          </div>`;
        }

        const resourceOptions = resources.map((resource) => (
          `<option value="${escapeHtml(resource.id)}">${escapeHtml(resource.name)} · ${escapeHtml(resource.resource_type)} · ${escapeHtml(resource.id)}</option>`
        )).join("");
        const grantDisabled = removed || Boolean(resourceError);
        return `<div class="comm-entry" data-extension-capability-row data-extension-id="${id}" data-capability="${capabilityText}">
          <strong>${capabilityText}</strong>
          <small>Not granted${requiredText}. No selected resources means an explicit workspace-wide grant.</small>
          ${resourceError ? `<small>Resource catalog unavailable: ${escapeHtml(resourceError)}</small>` : ""}
          <select data-extension-resource-scope multiple size="${Math.min(Math.max(resources.length, 2), 5)}" ${grantDisabled ? "disabled" : ""} aria-label="Resources for ${capabilityText}">
            ${resourceOptions}
          </select>
          <button type="button" class="ghost-button" data-extension-grant-action="grant" ${grantDisabled ? "disabled" : ""}>Grant capability</button>
        </div>`;
      }).join("")}
    </div>`;
  }

  function renderExtensionAdmin(installations, grantsByInstallation, resources, resourceError) {
    const list = document.getElementById("extension-admin-list");
    const status = document.getElementById("extension-admin-status");
    if (!list || !status) return;
    status.hidden = false;
    status.textContent = `${installations.length} installed extension(s)`;
    if (!installations.length) {
      list.innerHTML = '<div class="comm-entry"><strong>No extensions installed</strong><small>Discovered packages remain available through the canonical extension API.</small></div>';
      window.dispatchEvent(new CustomEvent("codex:extension-state-rendered", {
        detail: { installations: [] },
      }));
      return;
    }
    list.innerHTML = installations.map((item) => {
      const manifest = item.manifest || {};
      const verification = item.package_verification || {};
      const capabilities = manifest.capabilities || {};
      const requested = capabilities.requested || [];
      const grantsState = grantsByInstallation.get(item.id) || { items: [], error: null };
      return `<div class="comm-entry">
        <strong>${escapeHtml(manifest.id || item.id)} @ ${escapeHtml(manifest.version || "unknown")}</strong>
        <small>Lifecycle: ${escapeHtml(item.lifecycle)} · Health: ${escapeHtml(item.health_status)} · Deployment: ${escapeHtml(item.deployment_mode)}</small>
        <small>Publisher: ${escapeHtml(manifest.publisher?.name || manifest.publisher?.id || "unknown")} · Digest verified: ${verification.digest_verified ? "yes" : "no"} · Signature: ${escapeHtml(verification.signature_status || "unknown")}</small>
        <small>Requested capabilities: ${requested.length ? requested.map(escapeHtml).join(", ") : "none"} · Config refs: ${item.configuration_record_ids?.length || 0} · Secret bindings: ${Object.keys(item.secret_bindings || {}).length}</small>
        ${item.quarantine_reason ? `<small>Quarantine reason: ${escapeHtml(item.quarantine_reason)}</small>` : ""}
        ${item.disabled_reason ? `<small>Disabled reason: ${escapeHtml(item.disabled_reason)}</small>` : ""}
        ${capabilityAdministration(item, grantsState, resources, resourceError)}
        <div data-extension-config-host="${escapeHtml(item.id)}"></div>
        ${extensionLifecycleActions(item)}
      </div>`;
    }).join("");
    window.dispatchEvent(new CustomEvent("codex:extension-state-rendered", {
      detail: { installations },
    }));
  }

  async function loadGrantState(installations) {
    const entries = await Promise.all(installations.map(async (item) => {
      try {
        const response = await apiRequest(
          `/api/extensions/${encodeURIComponent(item.id)}/grants`,
        );
        return [item.id, { items: response.items || [], error: null }];
      } catch (error) {
        return [item.id, { items: [], error: error.message }];
      }
    }));
    return new Map(entries);
  }

  async function refreshExtensionAdmin() {
    const status = document.getElementById("extension-admin-status");
    if (status) {
      status.hidden = false;
      status.textContent = "Loading extension state...";
    }
    try {
      const extensions = await apiRequest("/api/extensions");
      const installations = extensions.items || [];
      let resources = [];
      let resourceError = null;
      const [grantsByInstallation] = await Promise.all([
        loadGrantState(installations),
        apiRequest("/api/resources")
          .then((value) => { resources = value.items || []; })
          .catch((error) => { resourceError = error.message; }),
      ]);
      renderExtensionAdmin(
        installations,
        grantsByInstallation,
        resources,
        resourceError,
      );
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
    if (["disable", "quarantine", "clear-quarantine", "remove"].includes(action)) {
      reason = window.prompt(`Reason for ${action.replace("-", " ")} of ${label}:`, "");
      if (reason === null) return;
    }
    const confirmation = action === "remove"
      ? `Remove ${label}? Current lifecycle: ${lifecycle}. Removal revokes active grants, stops future runtime use, and preserves the canonical tombstone; hard deletion is not supported.`
      : `${action.replace("-", " ")} ${label}? Current lifecycle: ${lifecycle}. This updates canonical extension state.`;
    if (!window.confirm(confirmation)) return;

    const status = document.getElementById("extension-admin-status");
    button.disabled = true;
    if (status) status.textContent = `Applying ${action.replace("-", " ")} to ${label}...`;
    try {
      const options = { method: "POST" };
      if (action === "remove") {
        options.body = JSON.stringify({
          reason: reason?.trim() || "operator removed",
          preserve_tombstone: true,
        });
      } else if (action !== "enable") {
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

  async function mutateCapabilityGrant(button) {
    const row = button.closest("[data-extension-capability-row]");
    const installationId = row?.dataset.extensionId;
    const capability = row?.dataset.capability;
    const action = button.dataset.extensionGrantAction;
    const status = document.getElementById("extension-admin-status");
    if (!row || !installationId || !capability || !action) return;

    if (action === "grant") {
      const select = row.querySelector("[data-extension-resource-scope]");
      const resourceIds = Array.from(select?.selectedOptions || []).map((option) => option.value);
      const scope = resourceIds.length
        ? `${resourceIds.length} selected canonical resource(s)`
        : "workspace-wide with no resource restriction";
      if (!window.confirm(`Grant ${capability} to this extension with ${scope}? Installation and enablement remain separate operations.`)) return;
      button.disabled = true;
      try {
        await apiRequest(
          `/api/extensions/${encodeURIComponent(installationId)}/grants`,
          {
            method: "POST",
            body: JSON.stringify({
              capabilities: [capability],
              resource_ids: resourceIds,
            }),
          },
        );
        await refreshExtensionAdmin();
      } catch (error) {
        button.disabled = false;
        if (status) status.textContent = `Capability grant failed: ${error.message}`;
      }
      return;
    }

    if (action === "revoke") {
      const grantId = button.dataset.grantId;
      if (!grantId) return;
      const reason = window.prompt(`Reason for revoking ${capability}:`, "");
      if (reason === null) return;
      const quarantineWarning = button.dataset.mandatory === "true"
        && button.dataset.extensionLifecycle === "enabled"
        ? " This is a mandatory capability; the server will quarantine the enabled extension."
        : "";
      if (!window.confirm(`Revoke ${capability}?${quarantineWarning}`)) return;
      button.disabled = true;
      try {
        await apiRequest(
          `/api/extensions/${encodeURIComponent(installationId)}/grants/${encodeURIComponent(grantId)}/revoke`,
          {
            method: "POST",
            body: JSON.stringify({ reason: reason.trim() || "operator revoked" }),
          },
        );
        await refreshExtensionAdmin();
      } catch (error) {
        button.disabled = false;
        if (status) status.textContent = `Capability revocation failed: ${error.message}`;
      }
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
      const target = event.target;
      const lifecycleButton = target.closest?.("[data-extension-action]");
      if (lifecycleButton) {
        mutateExtensionLifecycle(lifecycleButton).catch(console.error);
        return;
      }
      const grantButton = target.closest?.("[data-extension-grant-action]");
      if (grantButton) mutateCapabilityGrant(grantButton).catch(console.error);
    });

    if (panel?.open) refreshExtensionAdmin().catch(console.error);
  }

  window.addEventListener("DOMContentLoaded", bindExtensionAdmin);
})();
