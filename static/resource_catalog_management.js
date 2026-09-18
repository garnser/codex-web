(async () => {
  const BASE = window.location.pathname.startsWith("/codex") ? "/codex" : "";
  const { request: apiRequest } = await import(`${BASE}/static/api_client.js`);
  let resources = [];
  let owners = [];
  let identityError = null;
  let generation = 0;

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");
  }

  function setStatus(message) {
    const status = document.getElementById("resource-management-status");
    if (status) {
      status.hidden = false;
      status.textContent = message;
    }
  }

  function refreshCatalog() {
    document.getElementById("refresh-resources")?.click();
  }

  async function loadOwners() {
    const [identity, actor] = await Promise.all([
      apiRequest("/api/identity"),
      apiRequest("/api/identity/me"),
    ]);
    const memberIds = new Set(
      (identity.memberships || [])
        .filter((membership) => (
          membership.organization_id === actor.organization_id
          && membership.workspace_id === actor.workspace_id
        ))
        .map((membership) => membership.identity_id),
    );
    const rows = [
      ...(identity.humans || []).map((item) => ({
        id: item.id,
        label: item.display_name || item.email || item.id,
        kind: "human",
      })),
      ...(identity.services || []).map((item) => ({
        id: item.id,
        label: item.name || item.id,
        kind: "service",
      })),
    ];
    return rows
      .filter((item) => memberIds.has(item.id))
      .sort((left, right) => left.label.localeCompare(right.label));
  }

  function ownerOptions(selectedId = "") {
    const options = ['<option value="">Unassigned owner</option>'];
    owners.forEach((owner) => {
      const selected = owner.id === selectedId ? " selected" : "";
      options.push(`<option value="${escapeHtml(owner.id)}"${selected}>${escapeHtml(owner.label)} · ${escapeHtml(owner.kind)} · ${escapeHtml(owner.id)}</option>`);
    });
    if (selectedId && !owners.some((owner) => owner.id === selectedId)) {
      options.push(`<option value="${escapeHtml(selectedId)}" selected>Current owner · ${escapeHtml(selectedId)}</option>`);
    }
    return options.join("");
  }

  function option(value, current, label = value) {
    return `<option value="${escapeHtml(value)}"${value === current ? " selected" : ""}>${escapeHtml(label)}</option>`;
  }

  function renderEditor(item) {
    const host = Array.from(document.querySelectorAll("[data-resource-mutation-host]"))
      .find((element) => element.dataset.resourceMutationHost === item.id);
    if (!host) return;
    host.innerHTML = `<details data-resource-editor data-resource-id="${escapeHtml(item.id)}" data-original-lifecycle="${escapeHtml(item.lifecycle)}">
      <summary>Edit canonical resource</summary>
      <div class="route-test">
        <input data-resource-edit-name value="${escapeHtml(item.name)}" aria-label="Resource name" />
        <textarea data-resource-edit-description rows="2" aria-label="Resource description">${escapeHtml(item.description || "")}</textarea>
        <select data-resource-edit-owner aria-label="Resource owner" ${identityError ? "disabled" : ""}>${ownerOptions(item.owner_identity_id || "")}</select>
        ${identityError ? `<small>Identity catalog unavailable: ${escapeHtml(identityError)}. Owner is preserved.</small>` : ""}
        <select data-resource-edit-sensitivity aria-label="Resource sensitivity">
          ${option("public", item.sensitivity, "Public")}
          ${option("internal", item.sensitivity, "Internal")}
          ${option("confidential", item.sensitivity, "Confidential")}
          ${option("restricted", item.sensitivity, "Restricted")}
        </select>
        <select data-resource-edit-risk aria-label="Resource risk">
          ${option("low", item.risk, "Low")}
          ${option("medium", item.risk, "Medium")}
          ${option("high", item.risk, "High")}
          ${option("critical", item.risk, "Critical")}
        </select>
        <select data-resource-edit-lifecycle aria-label="Resource lifecycle">
          ${option("active", item.lifecycle, "Active")}
          ${option("deprecated", item.lifecycle, "Deprecated")}
          ${option("disabled", item.lifecycle, "Disabled")}
          ${option("deleted", item.lifecycle, "Deleted")}
        </select>
        <button type="button" class="ghost-button" data-resource-save>Save resource</button>
      </div>
    </details>`;
  }

  function populateRelationshipControls() {
    const options = resources.map((item) => (
      `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)} · ${escapeHtml(item.resource_type)} · ${escapeHtml(item.id)}</option>`
    )).join("");
    const from = document.getElementById("resource-relationship-from");
    const to = document.getElementById("resource-relationship-to");
    if (from) from.innerHTML = `<option value="">Source resource</option>${options}`;
    if (to) to.innerHTML = `<option value="">Target resource</option>${options}`;
  }

  function populateCreateOwner() {
    const select = document.getElementById("resource-create-owner");
    if (!select) return;
    select.innerHTML = ownerOptions();
    select.disabled = Boolean(identityError);
  }

  async function hydrate(nextResources) {
    const current = ++generation;
    resources = nextResources;
    identityError = null;
    try {
      owners = await loadOwners();
    } catch (error) {
      owners = [];
      identityError = error.message;
    }
    if (current !== generation) return;
    populateCreateOwner();
    populateRelationshipControls();
    resources.forEach(renderEditor);
  }

  async function createResource() {
    const name = document.getElementById("resource-create-name")?.value.trim() || "";
    if (!name) {
      setStatus("Resource name is required.");
      return;
    }
    const payload = {
      resource_type: document.getElementById("resource-create-type")?.value,
      name,
      description: document.getElementById("resource-create-description")?.value.trim() || null,
      owner_identity_id: document.getElementById("resource-create-owner")?.value || null,
      sensitivity: document.getElementById("resource-create-sensitivity")?.value,
      risk: document.getElementById("resource-create-risk")?.value,
      aliases: [],
    };
    if (!window.confirm(`Create canonical ${payload.resource_type} resource "${name}" with ${payload.risk} risk and ${payload.sensitivity} sensitivity?`)) return;
    setStatus(`Creating ${name}...`);
    try {
      await apiRequest("/api/resources", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      setStatus(`Created ${name}.`);
      refreshCatalog();
    } catch (error) {
      setStatus(`Resource creation failed: ${error.message}`);
    }
  }

  async function saveResource(button) {
    const root = button.closest("[data-resource-editor]");
    const resourceId = root?.dataset.resourceId;
    if (!root || !resourceId) return;
    const name = root.querySelector("[data-resource-edit-name]")?.value.trim() || "";
    if (!name) {
      setStatus("Resource name is required.");
      return;
    }
    const lifecycle = root.querySelector("[data-resource-edit-lifecycle]")?.value;
    const originalLifecycle = root.dataset.originalLifecycle;
    const payload = {
      name,
      description: root.querySelector("[data-resource-edit-description]")?.value.trim() || null,
      sensitivity: root.querySelector("[data-resource-edit-sensitivity]")?.value,
      risk: root.querySelector("[data-resource-edit-risk]")?.value,
      lifecycle,
    };
    const owner = root.querySelector("[data-resource-edit-owner]");
    if (owner && !owner.disabled) payload.owner_identity_id = owner.value || null;
    const impact = lifecycle !== originalLifecycle && ["disabled", "deleted"].includes(lifecycle)
      ? " This lifecycle makes the resource unavailable for privileged resolution."
      : "";
    if (!window.confirm(`Update canonical resource ${resourceId}? Target lifecycle: ${lifecycle}; risk: ${payload.risk}; sensitivity: ${payload.sensitivity}.${impact}`)) return;
    button.disabled = true;
    try {
      await apiRequest(`/api/resources/${encodeURIComponent(resourceId)}`, {
        method: "PATCH",
        body: JSON.stringify(payload),
      });
      setStatus(`Updated ${resourceId}.`);
      refreshCatalog();
    } catch (error) {
      button.disabled = false;
      setStatus(`Resource update failed: ${error.message}`);
    }
  }

  async function createRelationship() {
    const fromResourceId = document.getElementById("resource-relationship-from")?.value || "";
    const toResourceId = document.getElementById("resource-relationship-to")?.value || "";
    const relationshipType = document.getElementById("resource-relationship-type")?.value || "";
    if (!fromResourceId || !toResourceId) {
      setStatus("Choose both source and target resources.");
      return;
    }
    if (fromResourceId === toResourceId) {
      setStatus("A resource cannot relate to itself.");
      return;
    }
    if (!window.confirm(`Create canonical relationship ${fromResourceId} —${relationshipType}→ ${toResourceId}?`)) return;
    try {
      await apiRequest("/api/resources/relationships", {
        method: "POST",
        body: JSON.stringify({
          from_resource_id: fromResourceId,
          to_resource_id: toResourceId,
          relationship_type: relationshipType,
        }),
      });
      setStatus("Resource relationship created.");
      refreshCatalog();
    } catch (error) {
      setStatus(`Relationship creation failed: ${error.message}`);
    }
  }

  window.addEventListener("codex:resource-catalog-rendered", (event) => {
    hydrate(event.detail?.resources || []).catch(console.error);
  });

  window.addEventListener("DOMContentLoaded", () => {
    document.getElementById("create-resource")?.addEventListener("click", () => {
      createResource().catch(console.error);
    });
    document.getElementById("create-resource-relationship")?.addEventListener("click", () => {
      createRelationship().catch(console.error);
    });
    document.getElementById("resource-catalog-list")?.addEventListener("click", (event) => {
      const button = event.target.closest?.("[data-resource-save]");
      if (button) saveResource(button).catch(console.error);
    });
  });
})();
