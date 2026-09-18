(async () => {
  const BASE = window.location.pathname.startsWith("/codex") ? "/codex" : "";
  const { request: apiRequest } = await import(`${BASE}/static/api_client.js`);
  const MAX_AUDIT_ROWS = 100;
  let resources = [];

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
    const element = document.getElementById("crypto-key-status");
    if (element) {
      element.hidden = false;
      element.textContent = message;
    }
  }

  function resourceText(id) {
    if (!id) return "none";
    const item = resources.find((resource) => resource.id === id);
    return item ? `${item.name} (${id})` : id;
  }

  function renderBackendHealth(health) {
    const host = document.getElementById("crypto-backend-health");
    const select = document.getElementById("crypto-key-backend");
    if (!host || !select) return;
    const entries = Object.entries(health || {});
    host.innerHTML = entries.map(([name, healthy]) => `<div class="comm-entry">
      <strong>${escapeHtml(name)}</strong>
      <small>Status: ${healthy ? "healthy" : "unhealthy"}. Key material remains backend-owned and is never exposed here.</small>
    </div>`).join("") || '<div class="comm-entry"><strong>No key backends reported.</strong></div>';
    select.innerHTML = entries.map(([name, healthy]) => (
      `<option value="${escapeHtml(name)}" ${healthy ? "" : "disabled"}>${escapeHtml(name)} · ${healthy ? "healthy" : "unhealthy"}</option>`
    )).join("");
  }

  function renderResourceOptions() {
    const select = document.getElementById("crypto-key-resource-id");
    if (!select) return;
    select.innerHTML = '<option value="">No resource scope</option>' + resources.map((item) => (
      `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)} · ${escapeHtml(item.resource_type)} · ${escapeHtml(item.id)}</option>`
    )).join("");
  }

  function versionRows(key) {
    return (key.versions || []).map((version) => {
      const canRevoke = key.status === "active"
        && version.status !== "revoked"
        && version.version !== key.current_version;
      return `<div class="comm-entry">
        <strong>Version ${escapeHtml(version.version)} · ${escapeHtml(version.status)}</strong>
        <small>Algorithm: ${escapeHtml(version.algorithm)} · Backend reference: ${escapeHtml(version.backend_ref)}</small>
        <small>Created: ${timeText(version.created_at)} · Rotated: ${timeText(version.rotated_at)} · Revoked: ${timeText(version.revoked_at)}</small>
        ${version.revoke_reason ? `<small>Revoke reason: ${escapeHtml(version.revoke_reason)}</small>` : ""}
        ${canRevoke ? `<button type="button" class="ghost-button" data-crypto-revoke-version="${escapeHtml(version.version)}">Revoke old version</button>` : ""}
      </div>`;
    }).join("");
  }

  function renderKeys(items) {
    const list = document.getElementById("crypto-key-list");
    if (!list) return;
    list.innerHTML = items.map((key) => `<div class="comm-entry" data-crypto-key-row="${escapeHtml(key.id)}">
      <strong>${escapeHtml(key.id)} · ${escapeHtml(key.purpose)} · ${escapeHtml(key.status)}</strong>
      <small>Backend: ${escapeHtml(key.backend_type)} · Current version: ${escapeHtml(key.current_version)} · Created by: ${escapeHtml(key.created_by)}</small>
      <small>Scope: org ${escapeHtml(key.scope?.organization_id)} / workspace ${escapeHtml(key.scope?.workspace_id)} · Project: ${escapeHtml(key.scope?.project_id || "none")} · Resource: ${escapeHtml(resourceText(key.scope?.resource_id))}</small>
      <small>Created: ${timeText(key.created_at)} · Updated: ${timeText(key.updated_at)}</small>
      ${versionRows(key)}
      ${key.status === "active" ? `<div class="developer-toolbar">
        <button type="button" class="ghost-button" data-crypto-rotate>Rotate key</button>
        <button type="button" class="ghost-button" data-crypto-revoke>Revoke key</button>
      </div>` : ""}
    </div>`).join("") || '<div class="comm-entry"><strong>No managed keys in this tenant.</strong></div>';
  }

  function renderManifest(items) {
    const host = document.getElementById("crypto-key-manifest");
    if (!host) return;
    host.innerHTML = (items || []).map((item) => `<div class="comm-entry">
      <strong>${escapeHtml(item.key_id)}</strong>
      <small>Required versions: ${(item.versions || []).map(escapeHtml).join(", ") || "none"}</small>
    </div>`).join("") || '<div class="comm-entry"><strong>No key manifest entries.</strong></div>';
  }

  function renderAudit(items) {
    const host = document.getElementById("crypto-key-audit");
    if (!host) return;
    const rows = (items || []).slice(0, MAX_AUDIT_ROWS);
    host.innerHTML = rows.map((event) => `<div class="comm-entry">
      <strong>${escapeHtml(event.operation)} · ${escapeHtml(event.outcome)}</strong>
      <small>${timeText(event.occurred_at)} · actor ${escapeHtml(event.actor_id)} · key ${escapeHtml(event.key_id || "none")}${event.key_version ? ` v${escapeHtml(event.key_version)}` : ""}</small>
      ${event.object_type || event.object_id ? `<small>Object: ${escapeHtml(event.object_type || "unknown")} / ${escapeHtml(event.object_id || "unknown")}</small>` : ""}
      ${event.reason_code ? `<small>Reason: ${escapeHtml(event.reason_code)}</small>` : ""}
    </div>`).join("") || '<div class="comm-entry"><strong>No key audit events.</strong></div>';
  }

  async function refresh() {
    setStatus("Loading managed key metadata...");
    let resourceError = null;
    const resourceLoad = apiRequest("/api/resources")
      .then((value) => { resources = value.items || []; })
      .catch((error) => { resources = []; resourceError = error.message; });
    try {
      const [keys, health, manifest, events] = await Promise.all([
        apiRequest("/api/crypto/keys"),
        apiRequest("/api/crypto/backend-health"),
        apiRequest("/api/crypto/manifest"),
        apiRequest("/api/crypto/events"),
        resourceLoad,
      ]);
      renderResourceOptions();
      renderBackendHealth(health);
      renderKeys(keys.items || []);
      renderManifest(manifest.items || []);
      renderAudit(events.items || []);
      const unhealthy = Object.entries(health || {}).filter(([, ok]) => !ok).map(([name]) => name);
      setStatus(`${keys.items?.length || 0} managed key(s). Backend health: ${unhealthy.length ? `degraded (${unhealthy.join(", ")})` : "healthy"}.${resourceError ? ` Resource scope labels unavailable: ${resourceError}.` : ""} Key material is never exposed.`);
    } catch (error) {
      setStatus(`Key administration unavailable: ${error.message}`);
    }
  }

  async function createKey() {
    const backendType = document.getElementById("crypto-key-backend")?.value || "";
    const purpose = document.getElementById("crypto-key-purpose")?.value || "application_data";
    if (!backendType) {
      setStatus("Choose a healthy key backend.");
      return;
    }
    const projectId = document.getElementById("crypto-key-project-id")?.value.trim() || null;
    const resourceId = document.getElementById("crypto-key-resource-id")?.value || null;
    const scope = [projectId ? `project ${projectId}` : null, resourceId ? `resource ${resourceId}` : null].filter(Boolean).join(", ") || "workspace";
    if (!window.confirm(`Create a ${purpose} key reference using backend ${backendType} scoped to ${scope}? Cryptographic material is generated and retained only by the configured key backend.`)) return;
    try {
      await apiRequest("/api/crypto/keys", {
        method: "POST",
        body: JSON.stringify({
          project_id: projectId,
          resource_id: resourceId,
          purpose,
          backend_type: backendType,
        }),
      });
      setStatus("Managed key created.");
      await refresh();
    } catch (error) {
      setStatus(`Key creation failed: ${error.message}`);
    }
  }

  async function rotateKey(button) {
    const row = button.closest("[data-crypto-key-row]");
    const keyId = row?.dataset.cryptoKeyRow;
    if (!keyId) return;
    if (!window.confirm(`Rotate managed key ${keyId}? The existing current version becomes decrypt-only and a new active version is generated by the backend. Existing ciphertext is not silently rewritten by this operation.`)) return;
    button.disabled = true;
    try {
      const result = await apiRequest(`/api/crypto/keys/${encodeURIComponent(keyId)}/rotate`, { method: "POST" });
      setStatus(`Rotated ${keyId}: v${result.previous_version} → v${result.current_version}.`);
      await refresh();
    } catch (error) {
      button.disabled = false;
      setStatus(`Key rotation failed: ${error.message}`);
    }
  }

  async function revokeVersion(button) {
    const row = button.closest("[data-crypto-key-row]");
    const keyId = row?.dataset.cryptoKeyRow;
    const version = button.dataset.cryptoRevokeVersion;
    if (!keyId || !version) return;
    const reason = window.prompt(`Reason for revoking ${keyId}:v${version}:`, "");
    if (reason === null) return;
    if (!window.confirm(`Revoke old key version ${keyId}:v${version}? Data still encrypted with this version may become undecryptable. The active current version cannot be revoked through this action.`)) return;
    button.disabled = true;
    try {
      await apiRequest(
        `/api/crypto/keys/${encodeURIComponent(keyId)}/versions/${encodeURIComponent(version)}/revoke`,
        { method: "POST", body: JSON.stringify({ reason: reason.trim() || "operator revoked" }) },
      );
      await refresh();
    } catch (error) {
      button.disabled = false;
      setStatus(`Key-version revocation failed: ${error.message}`);
    }
  }

  async function revokeKey(button) {
    const row = button.closest("[data-crypto-key-row]");
    const keyId = row?.dataset.cryptoKeyRow;
    if (!keyId) return;
    const reason = window.prompt(`Reason for revoking managed key ${keyId}:`, "");
    if (reason === null) return;
    if (!window.confirm(`Revoke the entire managed key ${keyId}? All key versions become revoked and dependent encrypted data may become unreadable. This operation does not expose key material.`)) return;
    button.disabled = true;
    try {
      await apiRequest(`/api/crypto/keys/${encodeURIComponent(keyId)}/revoke`, {
        method: "POST",
        body: JSON.stringify({ reason: reason.trim() || "operator revoked" }),
      });
      await refresh();
    } catch (error) {
      button.disabled = false;
      setStatus(`Key revocation failed: ${error.message}`);
    }
  }

  function bind() {
    const panel = document.getElementById("developer-panel");
    document.getElementById("refresh-crypto-keys")?.addEventListener("click", refresh);
    document.getElementById("refresh-developer")?.addEventListener("click", refresh);
    document.getElementById("create-crypto-key")?.addEventListener("click", () => createKey().catch(console.error));
    document.getElementById("crypto-key-list")?.addEventListener("click", (event) => {
      const rotate = event.target.closest?.("[data-crypto-rotate]");
      if (rotate) return void rotateKey(rotate).catch(console.error);
      const version = event.target.closest?.("[data-crypto-revoke-version]");
      if (version) return void revokeVersion(version).catch(console.error);
      const revoke = event.target.closest?.("[data-crypto-revoke]");
      if (revoke) revokeKey(revoke).catch(console.error);
    });
    panel?.addEventListener("toggle", () => {
      if (panel.open) refresh().catch(console.error);
    });
    if (panel?.open) refresh().catch(console.error);
  }

  window.addEventListener("DOMContentLoaded", bind);
})();
