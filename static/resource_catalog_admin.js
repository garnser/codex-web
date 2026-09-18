(async () => {
  const BASE = window.location.pathname.startsWith("/codex") ? "/codex" : "";
  const { request: apiRequest } = await import(`${BASE}/static/api_client.js`);
  let resources = [];
  const relationshipCache = new Map();

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

  function aliasesText(aliases) {
    if (!aliases?.length) return "none";
    return aliases.map((alias) => {
      const provider = alias.provider ? `/${alias.provider}` : "";
      return `${alias.namespace}${provider}:${alias.value}`;
    }).join(", ");
  }

  function provenanceText(provenance) {
    if (!provenance) return "none";
    const parts = [
      provenance.provider,
      provenance.provider_instance,
      provenance.external_id,
    ].filter(Boolean);
    return parts.length ? parts.join(" · ") : "metadata present";
  }

  function resourceMatches(item) {
    const search = (document.getElementById("resource-search")?.value || "").trim().toLowerCase();
    const type = document.getElementById("resource-type-filter")?.value || "";
    const lifecycle = document.getElementById("resource-lifecycle-filter")?.value || "";
    if (type && item.resource_type !== type) return false;
    if (lifecycle && item.lifecycle !== lifecycle) return false;
    if (!search) return true;
    const aliasText = (item.aliases || []).map((alias) => (
      `${alias.namespace} ${alias.provider || ""} ${alias.value}`
    )).join(" ");
    const haystack = [
      item.id,
      item.name,
      item.description,
      item.owner_identity_id,
      item.resource_type,
      item.lifecycle,
      aliasText,
    ].filter(Boolean).join(" ").toLowerCase();
    return haystack.includes(search);
  }

  function relationshipName(id) {
    const item = resources.find((resource) => resource.id === id);
    return item ? `${item.name} (${id})` : id;
  }

  function renderRelationships(resourceId, relationships) {
    const host = document.querySelector(`[data-resource-relationship-host="${CSS.escape(resourceId)}"]`);
    if (!host) return;
    host.innerHTML = relationships.map((relationship) => {
      const incoming = relationship.to_resource_id === resourceId;
      const peer = incoming ? relationship.from_resource_id : relationship.to_resource_id;
      const direction = incoming ? "from" : "to";
      return `<div class="comm-entry">
        <small>${escapeHtml(relationship.relationship_type)} · ${direction} ${escapeHtml(relationshipName(peer))}</small>
      </div>`;
    }).join("") || "<small>No canonical relationships.</small>";
  }

  function render() {
    const list = document.getElementById("resource-catalog-list");
    const status = document.getElementById("resource-catalog-status");
    if (!list || !status) return;
    const visible = resources.filter(resourceMatches);
    status.hidden = false;
    status.textContent = `${visible.length} of ${resources.length} canonical resource(s)`;
    list.innerHTML = visible.map((item) => {
      const provenance = item.provenance || null;
      return `<div class="comm-entry" data-resource-row="${escapeHtml(item.id)}">
        <strong>${escapeHtml(item.name)} · ${escapeHtml(item.resource_type)}</strong>
        <small>ID: ${escapeHtml(item.id)} · Lifecycle: ${escapeHtml(item.lifecycle)}</small>
        <small>Owner: ${escapeHtml(item.owner_identity_id || "unassigned")} · Risk: ${escapeHtml(item.risk)} · Sensitivity: ${escapeHtml(item.sensitivity)}</small>
        ${item.description ? `<small>${escapeHtml(item.description)}</small>` : ""}
        <small>Aliases: ${escapeHtml(aliasesText(item.aliases))}</small>
        <small>Provenance: ${escapeHtml(provenanceText(provenance))}${provenance?.external_url ? ` · URL: ${escapeHtml(provenance.external_url)}` : ""}</small>
        <small>Discovered: ${timeText(provenance?.discovered_at)} · Last seen: ${timeText(provenance?.last_seen_at)} · Updated: ${timeText(item.updated_at)}</small>
        <button type="button" class="ghost-button" data-resource-relationships="${escapeHtml(item.id)}">Relationships</button>
        <div data-resource-relationship-host="${escapeHtml(item.id)}"></div>
      </div>`;
    }).join("") || '<div class="comm-entry"><strong>No resources match the current filters.</strong></div>';
    relationshipCache.forEach((relationships, resourceId) => {
      renderRelationships(resourceId, relationships);
    });
  }

  async function refreshResources() {
    const status = document.getElementById("resource-catalog-status");
    if (status) {
      status.hidden = false;
      status.textContent = "Loading canonical resources...";
    }
    try {
      const response = await apiRequest("/api/resources");
      resources = response.items || [];
      relationshipCache.clear();
      render();
    } catch (error) {
      resources = [];
      if (status) status.textContent = `Resource Catalog unavailable: ${error.message}`;
      const list = document.getElementById("resource-catalog-list");
      if (list) list.innerHTML = "";
    }
  }

  async function loadRelationships(button) {
    const resourceId = button.dataset.resourceRelationships;
    if (!resourceId) return;
    if (relationshipCache.has(resourceId)) {
      renderRelationships(resourceId, relationshipCache.get(resourceId));
      return;
    }
    button.disabled = true;
    try {
      const response = await apiRequest(
        `/api/resources/${encodeURIComponent(resourceId)}/relationships?direction=both`,
      );
      relationshipCache.set(resourceId, response.items || []);
      renderRelationships(resourceId, response.items || []);
    } catch (error) {
      const host = document.querySelector(`[data-resource-relationship-host="${CSS.escape(resourceId)}"]`);
      if (host) host.innerHTML = `<small>Relationships unavailable: ${escapeHtml(error.message)}</small>`;
    } finally {
      button.disabled = false;
    }
  }

  function bind() {
    const panel = document.getElementById("developer-panel");
    document.getElementById("refresh-resources")?.addEventListener("click", refreshResources);
    document.getElementById("refresh-developer")?.addEventListener("click", refreshResources);
    document.getElementById("resource-search")?.addEventListener("input", render);
    document.getElementById("resource-type-filter")?.addEventListener("change", render);
    document.getElementById("resource-lifecycle-filter")?.addEventListener("change", render);
    document.getElementById("resource-catalog-list")?.addEventListener("click", (event) => {
      const button = event.target.closest?.("[data-resource-relationships]");
      if (button) loadRelationships(button).catch(console.error);
    });
    panel?.addEventListener("toggle", () => {
      if (panel.open) refreshResources().catch(console.error);
    });
    if (panel?.open) refreshResources().catch(console.error);
  }

  window.addEventListener("DOMContentLoaded", bind);
})();
