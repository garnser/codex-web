(async () => {
  const BASE = window.location.pathname.startsWith("/codex") ? "/codex" : "";
  const { request: apiRequest } = await import(`${BASE}/static/api_client.js`);
  const MAX_AUDIT_ROWS = 100;
  let identityNames = new Map();

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

  function identityText(id) {
    if (!id) return "none";
    return identityNames.has(id) ? `${identityNames.get(id)} (${id})` : id;
  }

  function identityList(ids) {
    return ids?.length ? ids.map(identityText).join(", ") : "none";
  }

  function setStatus(message) {
    const status = document.getElementById("secret-admin-status");
    if (status) {
      status.hidden = false;
      status.textContent = message;
    }
  }

  function expiryValue() {
    const value = document.getElementById("secret-create-expires")?.value || "";
    if (!value) return null;
    const timestamp = new Date(value).getTime();
    return Number.isNaN(timestamp) ? null : timestamp / 1000;
  }

  function renderIdentityChoices(state, actor) {
    const memberIds = new Set(
      (state?.memberships || [])
        .filter((membership) => (
          membership.organization_id === actor?.organization_id
          && membership.workspace_id === actor?.workspace_id
          && !membership.revoked_at
        ))
        .map((membership) => membership.identity_id),
    );
    const rows = [
      ...(state?.humans || []).map((item) => ({
        id: item.id,
        label: item.display_name || item.email || item.id,
        kind: "human",
      })),
      ...(state?.services || []).map((item) => ({
        id: item.id,
        label: item.name || item.id,
        kind: "service",
      })),
    ].filter((item) => memberIds.has(item.id));
    identityNames = new Map(rows.map((item) => [item.id, item.label]));
    const html = rows.map((item) => (
      `<option value="${escapeHtml(item.id)}">${escapeHtml(item.label)} · ${item.kind} · ${escapeHtml(item.id)}</option>`
    )).join("");
    for (const id of ["secret-create-use-identities", "secret-create-reveal-identities"]) {
      const select = document.getElementById(id);
      if (select) {
        select.innerHTML = html;
        select.disabled = false;
      }
    }
  }

  function disableIdentityChoices(message) {
    identityNames = new Map();
    for (const id of ["secret-create-use-identities", "secret-create-reveal-identities"]) {
      const select = document.getElementById(id);
      if (select) {
        select.innerHTML = `<option disabled>${escapeHtml(message)}</option>`;
        select.disabled = true;
      }
    }
  }

  function renderSecrets(items) {
    const list = document.getElementById("secret-admin-list");
    if (!list) return;
    list.innerHTML = items.map((item) => {
      const active = item.status === "active";
      return `<div class="comm-entry" data-secret-row="${escapeHtml(item.id)}">
        <strong>${escapeHtml(item.name)} · ${escapeHtml(item.status)}</strong>
        <small>ID: ${escapeHtml(item.id)} · Backend: ${escapeHtml(item.backend)} · Tenant: ${escapeHtml(item.organization_id)}/${escapeHtml(item.workspace_id)}</small>
        <small>Provider: ${escapeHtml(item.provider || "none")} · Purpose: ${escapeHtml(item.purpose || "none")} · Owner: ${escapeHtml(identityText(item.owner_identity_id))}</small>
        <small>Use permission: ${escapeHtml(identityList(item.allowed_identity_ids))}</small>
        <small>Reveal permission: ${escapeHtml(identityList(item.reveal_identity_ids))}</small>
        <small>Created: ${timeText(item.created_at)} · Expires: ${timeText(item.expires_at)} · Rotated: ${timeText(item.rotated_at)} · Rotation: ${escapeHtml(item.rotation)}</small>
        ${item.revoked_at ? `<small>Revoked: ${timeText(item.revoked_at)} · ${escapeHtml(item.revoke_reason || "no reason")}</small>` : ""}
        ${active ? `<div class="route-test">
          <input type="password" autocomplete="new-password" data-secret-rotate-value placeholder="New secret value (cleared after submission)" />
          <button type="button" class="ghost-button" data-secret-rotate="${escapeHtml(item.id)}">Rotate</button>
          <button type="button" class="ghost-button" data-secret-revoke="${escapeHtml(item.id)}">Revoke</button>
        </div>` : ""}
      </div>`;
    }).join("") || '<div class="comm-entry"><strong>No secret references visible in the current tenant.</strong><small>Raw secret values are never returned by this API.</small></div>';
  }

  function renderAudit(items, error) {
    const status = document.getElementById("secret-audit-status");
    const list = document.getElementById("secret-audit-list");
    if (!status || !list) return;
    if (error) {
      status.textContent = `Secret audit unavailable: ${error}. Sensitive audit inspection requires canonical admin+MFA assurance.`;
      list.innerHTML = "";
      return;
    }
    const rows = (items || []).slice(-MAX_AUDIT_ROWS).reverse();
    status.textContent = `${items.length} audit event(s) · showing ${rows.length} most recent`;
    list.innerHTML = rows.map((event) => {
      const context = Object.entries(event.context || {})
        .map(([key, value]) => `${escapeHtml(key)}=${escapeHtml(value)}`)
        .join(" · ");
      return `<div class="comm-entry">
        <strong>${escapeHtml(event.action)} · ${escapeHtml(event.outcome)} · ${escapeHtml(event.secret_id)}</strong>
        <small>${timeText(event.created_at)} · actor ${escapeHtml(identityText(event.actor_identity_id))} · ${escapeHtml(event.actor_kind)}</small>
        ${event.reason ? `<small>Reason: ${escapeHtml(event.reason)}</small>` : ""}
        ${context ? `<small>${context}</small>` : ""}
      </div>`;
    }).join("") || '<div class="comm-entry"><strong>No secret audit events.</strong></div>';
  }

  async function refresh() {
    setStatus("Loading secret metadata...");
    let identityState = null;
    let actor = null;
    let audit = [];
    let auditError = null;
    const identityLoad = Promise.all([
      apiRequest("/api/identity"),
      apiRequest("/api/identity/me"),
    ]).then(([state, current]) => {
      identityState = state;
      actor = current;
    }).catch((error) => {
      disableIdentityChoices(`Identity metadata unavailable: ${error.message}`);
    });
    const auditLoad = apiRequest("/api/secrets/audit")
      .then((value) => { audit = value.items || []; })
      .catch((error) => { auditError = error.message; });
    try {
      const secrets = await apiRequest("/api/secrets");
      await Promise.all([identityLoad, auditLoad]);
      if (identityState && actor) renderIdentityChoices(identityState, actor);
      renderSecrets(secrets.items || []);
      renderAudit(audit, auditError);
      setStatus(`${secrets.items?.length || 0} secret reference(s) visible. Raw values are never returned; sensitive mutations require server-side admin+MFA.`);
    } catch (error) {
      setStatus(`Secret metadata unavailable: ${error.message}`);
    }
  }

  async function createSecret() {
    const name = document.getElementById("secret-create-name")?.value.trim() || "";
    const valueInput = document.getElementById("secret-create-value");
    const value = valueInput?.value || "";
    if (!name || !value) {
      setStatus("Secret reference name and value are required.");
      return;
    }
    const allowedIdentityIds = Array.from(document.getElementById("secret-create-use-identities")?.selectedOptions || []).map((item) => item.value);
    const revealIdentityIds = Array.from(document.getElementById("secret-create-reveal-identities")?.selectedOptions || []).map((item) => item.value);
    if (!window.confirm(`Create secret reference "${name}"? The value will be sent directly to the SecretBroker, never rendered back, and cleared from this browser field immediately.`)) return;
    if (valueInput) valueInput.value = "";
    try {
      await apiRequest("/api/secrets", {
        method: "POST",
        body: JSON.stringify({
          name,
          value,
          provider: document.getElementById("secret-create-provider")?.value.trim() || null,
          purpose: document.getElementById("secret-create-purpose")?.value.trim() || null,
          allowed_identity_ids: allowedIdentityIds,
          reveal_identity_ids: revealIdentityIds,
          expires_at: expiryValue(),
        }),
      });
      setStatus(`Created secret reference ${name}.`);
      await refresh();
    } catch (error) {
      setStatus(`Secret creation failed: ${error.message}. Re-enter the value to retry.`);
    }
  }

  async function rotateSecret(button) {
    const secretId = button.dataset.secretRotate;
    const row = button.closest("[data-secret-row]");
    const valueInput = row?.querySelector("[data-secret-rotate-value]");
    const value = valueInput?.value || "";
    if (!secretId || !value) {
      setStatus("Enter a new secret value before rotation.");
      return;
    }
    if (!window.confirm(`Rotate secret ${secretId}? The new value is sent directly to the SecretBroker and cleared from the browser immediately.`)) return;
    if (valueInput) valueInput.value = "";
    button.disabled = true;
    try {
      await apiRequest(`/api/secrets/${encodeURIComponent(secretId)}/rotate`, {
        method: "POST",
        body: JSON.stringify({ value }),
      });
      setStatus(`Rotated ${secretId}.`);
      await refresh();
    } catch (error) {
      button.disabled = false;
      setStatus(`Secret rotation failed: ${error.message}. Re-enter the value to retry.`);
    }
  }

  async function revokeSecret(button) {
    const secretId = button.dataset.secretRevoke;
    if (!secretId) return;
    if (!window.confirm(`Revoke secret ${secretId}? Existing consumers will no longer be allowed to use it; the raw value is not exposed by this operation.`)) return;
    button.disabled = true;
    try {
      await apiRequest(`/api/secrets/${encodeURIComponent(secretId)}`, { method: "DELETE" });
      setStatus(`Revoked ${secretId}.`);
      await refresh();
    } catch (error) {
      button.disabled = false;
      setStatus(`Secret revocation failed: ${error.message}`);
    }
  }

  function bind() {
    const panel = document.getElementById("developer-panel");
    document.getElementById("refresh-secrets")?.addEventListener("click", refresh);
    document.getElementById("refresh-developer")?.addEventListener("click", refresh);
    document.getElementById("create-secret")?.addEventListener("click", () => createSecret().catch(console.error));
    document.getElementById("secret-admin-list")?.addEventListener("click", (event) => {
      const rotate = event.target.closest?.("[data-secret-rotate]");
      if (rotate) {
        rotateSecret(rotate).catch(console.error);
        return;
      }
      const revoke = event.target.closest?.("[data-secret-revoke]");
      if (revoke) revokeSecret(revoke).catch(console.error);
    });
    panel?.addEventListener("toggle", () => {
      if (panel.open) refresh().catch(console.error);
    });
    if (panel?.open) refresh().catch(console.error);
  }

  window.addEventListener("DOMContentLoaded", bind);
})();
