(async () => {
  const BASE = window.location.pathname.startsWith("/codex") ? "/codex" : "";
  const { request: apiRequest } = await import(`${BASE}/static/api_client.js`);
  let generation = 0;

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");
  }

  function configurable(lifecycle) {
    return !["enabled", "removed", "incompatible", "upgrading"].includes(String(lifecycle || ""));
  }

  function hostFor(installationId) {
    return Array.from(document.querySelectorAll("[data-extension-config-host]"))
      .find((element) => element.dataset.extensionConfigHost === installationId) || null;
  }

  function configRecordOptions(item, records) {
    const current = new Set(item.configuration_record_ids || []);
    const available = records.filter((record) => record.state === "published");
    const byId = new Map(available.map((record) => [record.id, record]));
    current.forEach((id) => {
      if (!byId.has(id)) byId.set(id, { id, key: "current reference", revision: "?", state: "unknown" });
    });
    return Array.from(byId.values()).map((record) => {
      const selected = current.has(record.id) ? " selected" : "";
      const scope = record.scope_type
        ? ` · ${record.scope_type}${record.scope_id ? `:${record.scope_id}` : ""}`
        : "";
      return `<option value="${escapeHtml(record.id)}"${selected}>${escapeHtml(record.key || "configuration")} · r${escapeHtml(record.revision ?? "?")}${escapeHtml(scope)} · ${escapeHtml(record.id)}</option>`;
    }).join("");
  }

  function secretOptions(item, slot, secrets) {
    const currentId = item.secret_bindings?.[slot] || "";
    const candidates = secrets.filter((secret) => secret.status === "active" || secret.id === currentId);
    const options = ['<option value="">Unbound</option>'];
    candidates.forEach((secret) => {
      const selected = secret.id === currentId ? " selected" : "";
      const provider = secret.provider ? ` · ${secret.provider}` : "";
      const purpose = secret.purpose ? ` · ${secret.purpose}` : "";
      options.push(`<option value="${escapeHtml(secret.id)}"${selected}>${escapeHtml(secret.name)}${escapeHtml(provider)}${escapeHtml(purpose)} · ${escapeHtml(secret.status)} · ${escapeHtml(secret.id)}</option>`);
    });
    if (currentId && !candidates.some((secret) => secret.id === currentId)) {
      options.push(`<option value="${escapeHtml(currentId)}" selected>Current reference · ${escapeHtml(currentId)}</option>`);
    }
    return options.join("");
  }

  function renderConfiguration(item, secrets, records, secretError, configError) {
    const host = hostFor(item.id);
    if (!host) return;
    const declaration = item.manifest?.configuration || {};
    const slots = declaration.secret_refs || [];
    const schemaPath = declaration.schema || declaration.schema_path || null;
    const hasConfiguration = Boolean(schemaPath || slots.length || item.configuration_record_ids?.length || Object.keys(item.secret_bindings || {}).length);
    if (!hasConfiguration) {
      host.innerHTML = '<small>Configuration: no typed configuration or secret slots declared.</small>';
      return;
    }

    const canConfigure = configurable(item.lifecycle);
    const blockedReason = canConfigure
      ? ""
      : `Configuration is blocked while lifecycle is ${escapeHtml(item.lifecycle)}.`;
    const configUnavailable = Boolean(schemaPath && configError);
    const secretsUnavailable = Boolean(slots.length && secretError);
    const disabled = !canConfigure || configUnavailable || secretsUnavailable;

    const configRecords = schemaPath
      ? `<div class="comm-entry">
          <strong>Typed configuration references</strong>
          <small>Declared schema: ${escapeHtml(schemaPath)}. Only canonical record IDs are stored on the extension.</small>
          ${configError ? `<small>Configuration registry unavailable: ${escapeHtml(configError)}</small>` : ""}
          <select multiple size="4" data-extension-config-records ${disabled ? "disabled" : ""}>
            ${configRecordOptions(item, records)}
          </select>
        </div>`
      : "";

    const secretSlots = slots.map((slot) => `<label class="comm-entry">
        <strong>Secret slot: ${escapeHtml(slot)}</strong>
        <small>Metadata/reference only; secret material is never returned to this surface.</small>
        ${secretError ? `<small>Secret metadata unavailable: ${escapeHtml(secretError)}</small>` : ""}
        <select data-extension-secret-slot="${escapeHtml(slot)}" ${disabled ? "disabled" : ""}>
          ${secretOptions(item, slot, secrets)}
        </select>
      </label>`).join("");

    host.innerHTML = `<div class="extension-configuration" data-extension-configuration data-extension-id="${escapeHtml(item.id)}" data-extension-label="${escapeHtml(item.manifest?.id || item.id)}">
      <small>Configuration (separate from authorization and enablement):</small>
      ${blockedReason ? `<small>${blockedReason}</small>` : ""}
      ${configRecords}
      ${secretSlots}
      <button type="button" class="ghost-button" data-extension-config-save ${disabled ? "disabled" : ""}>Save configuration references</button>
    </div>`;
  }

  async function hydrateConfiguration(installations) {
    const currentGeneration = ++generation;
    if (!installations.length) return;

    let secrets = [];
    let records = [];
    let secretError = null;
    let configError = null;
    await Promise.all([
      apiRequest("/api/secrets")
        .then((value) => { secrets = value.items || []; })
        .catch((error) => { secretError = error.message; }),
      apiRequest("/api/configuration/records")
        .then((value) => { records = value.items || []; })
        .catch((error) => { configError = error.message; }),
    ]);
    if (currentGeneration !== generation) return;
    installations.forEach((item) => {
      renderConfiguration(item, secrets, records, secretError, configError);
    });
  }

  async function saveConfiguration(button) {
    const root = button.closest("[data-extension-configuration]");
    const installationId = root?.dataset.extensionId;
    const label = root?.dataset.extensionLabel || installationId;
    if (!root || !installationId) return;

    const configSelect = root.querySelector("[data-extension-config-records]");
    const configurationRecordIds = Array.from(configSelect?.selectedOptions || [])
      .map((option) => option.value);
    const secretBindings = {};
    root.querySelectorAll("[data-extension-secret-slot]").forEach((select) => {
      const slot = select.dataset.extensionSecretSlot;
      if (slot && select.value) secretBindings[slot] = select.value;
    });

    if (!window.confirm(`Update canonical configuration references for ${label}? Only configuration record IDs and secret IDs are stored; this does not grant authority or reveal secret material.`)) return;
    button.disabled = true;
    try {
      await apiRequest(
        `/api/extensions/${encodeURIComponent(installationId)}/configuration`,
        {
          method: "PUT",
          body: JSON.stringify({
            configuration_record_ids: configurationRecordIds,
            secret_bindings: secretBindings,
          }),
        },
      );
      document.getElementById("refresh-extensions")?.click();
    } catch (error) {
      button.disabled = false;
      const status = document.getElementById("extension-admin-status");
      if (status) status.textContent = `Extension configuration failed: ${error.message}`;
    }
  }

  window.addEventListener("codex:extension-state-rendered", (event) => {
    hydrateConfiguration(event.detail?.installations || []).catch(console.error);
  });

  window.addEventListener("DOMContentLoaded", () => {
    document.getElementById("extension-admin-list")?.addEventListener("click", (event) => {
      const button = event.target.closest?.("[data-extension-config-save]");
      if (button) saveConfiguration(button).catch(console.error);
    });
  });
})();
