function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function fmtTime(value) {
  if (!value) return "Never";
  try {
    return new Date(Number(value) * 1000).toLocaleString();
  } catch {
    return String(value);
  }
}

function humanName(context, identityId) {
  const human = (context?.identity?.humans || []).find((item) => item.id === identityId);
  return human?.display_name || identityId;
}

function serviceName(context, serviceId) {
  const service = (context?.identity?.services || []).find((item) => item.id === serviceId);
  return service?.name || serviceId;
}

function sessionRows(context) {
  const items = (context?.identity?.sessions || [])
    .filter((item) => (
      item.organization_id === context.organizationId
      && item.workspace_id === context.workspaceId
      && item.revoked_at == null
    ))
    .sort((a, b) => Number(b.last_seen_at || b.created_at || 0) - Number(a.last_seen_at || a.created_at || 0));
  if (!items.length) return '<div class="workspace-state">No active sessions in this Workspace.</div>';
  return items.map((item) => {
    const current = item.id === context?.actor?.session_id;
    return `
      <article class="administration-user-card" data-auth-session="${esc(item.id)}">
        <header>
          <div>
            <h3>${esc(humanName(context, item.identity_id))}</h3>
            <p>${esc(item.identity_id)} · ${esc(item.assurance || "unknown assurance")}${current ? " · current session" : ""}</p>
          </div>
          <span class="product-status-badge ${current ? "status-positive" : ""}">${current ? "Current" : "Active"}</span>
        </header>
        <p>Created ${esc(fmtTime(item.created_at))} · last seen ${esc(fmtTime(item.last_seen_at))}</p>
        <small>Idle expiry ${esc(fmtTime(item.idle_expires_at))} · absolute expiry ${esc(fmtTime(item.absolute_expires_at))} · step-up until ${esc(fmtTime(item.step_up_until))}</small>
        ${current ? "" : `<button type="button" data-auth-revoke-session="${esc(item.id)}">Revoke session</button>`}
      </article>
    `;
  }).join("");
}

function authenticationStatusView(status) {
  const external = status?.external_identity || {};
  const recovery = status?.recovery || {};
  const providers = (external.providers || [])
    .map((item) => `${item.provider} (${Number(item.linked_identity_count || 0)} linked)`)
    .join(", ");
  const recoveryProviders = (recovery.providers || [])
    .map((item) => `${item.provider} (${Number(item.active_factor_count || 0)} active)`)
    .join(", ");

  return `
    <div class="administration-user-card" data-auth-status-loaded>
      <header>
        <div>
          <h3>${esc(status?.identity_mode || "unknown")} identity mode</h3>
          <p>Current assurance: ${esc(status?.current_assurance || "unknown")} · step-up ${status?.step_up_active ? "active" : "not active"}</p>
        </div>
        <span class="product-status-badge ${status?.step_up_active ? "status-positive" : ""}">${status?.step_up_active ? "Step-up active" : "Step-up required for sensitive changes"}</span>
      </header>
      <p>Session authentication: ${status?.session_authentication_supported ? "supported" : "unavailable"} · service-token authentication: ${status?.service_token_authentication_supported ? "supported" : "unavailable"}.</p>
      <p>External identity mapping: ${external.configured ? esc(providers || "configured") : "not configured"}.</p>
      <small>Linked identities: ${Number(external.linked_identity_count || 0)}. External claims grant authority: ${external.claims_grant_authority ? "yes" : "no — codex-web authorization remains canonical and separately scoped"}.</small>
      <p>Recovery factors: ${recovery.configured ? esc(recoveryProviders || `${Number(recovery.active_factor_count || 0)} active`) : "none configured"}.</p>
      <small>Active Workspace sessions: ${Number(status?.active_session_count || 0)} · active service tokens: ${Number(status?.active_service_token_count || 0)}.</small>
    </div>
  `;
}

function tokenRows(context) {
  const items = (context?.identity?.service_tokens || [])
    .filter((item) => (
      item.organization_id === context.organizationId
      && item.workspace_id === context.workspaceId
    ))
    .sort((a, b) => Number(b.created_at || 0) - Number(a.created_at || 0));
  if (!items.length) return '<div class="workspace-state">No service tokens have been created in this Workspace.</div>';
  return items.map((item) => {
    const revoked = item.revoked_at != null;
    const expired = item.expires_at != null && Number(item.expires_at) <= Date.now() / 1000;
    const status = revoked ? "Revoked" : expired ? "Expired" : "Active";
    return `
      <article class="administration-user-card" data-auth-token="${esc(item.id)}">
        <header>
          <div>
            <h3>${esc(serviceName(context, item.service_identity_id))}</h3>
            <p>${esc(item.service_identity_id)} · token metadata ${esc(item.id)}</p>
          </div>
          <span class="product-status-badge ${!revoked && !expired ? "status-positive" : ""}">${esc(status)}</span>
        </header>
        <p>Scopes: ${esc((item.scopes || []).join(", ") || "No scopes")}</p>
        <small>Created ${esc(fmtTime(item.created_at))} · expires ${esc(fmtTime(item.expires_at))} · last used ${esc(fmtTime(item.last_used_at))}</small>
        ${revoked ? `<small>Revoked ${esc(fmtTime(item.revoked_at))} · ${esc(item.revoke_reason || "no reason recorded")}</small>` : `<button type="button" data-auth-revoke-token="${esc(item.id)}">Revoke token</button>`}
      </article>
    `;
  }).join("");
}

export function renderAdministrationAuthentication(container, {
  context,
  api,
  onChanged = null,
} = {}) {
  if (!container) return;
  if (!context?.allowed) {
    container.innerHTML = '<div class="workspace-state workspace-state-error">Administration access is required.</div>';
    return;
  }

  const services = (context.identity?.services || [])
    .filter((item) => item.disabled_at == null)
    .sort((a, b) => String(a.name || a.id).localeCompare(String(b.name || b.id)));

  container.className = "administration-authentication-surface";
  container.innerHTML = `
    <section class="administration-users-toolbar">
      <div>
        <strong>Authentication & sessions</strong>
        <p>Authentication establishes identity. Operational authorization remains separate and is managed through Memberships and Access.</p>
      </div>
      <div class="administration-membership-actions">
        <button type="button" data-auth-revoke-others>Revoke my other sessions</button>
      </div>
    </section>
    <div class="administration-users-message" data-auth-message hidden></div>

    <section>
      <h3>Active sessions</h3>
      <p>Session revocation is canonical. Revoking another user's session requires administrator MFA/step-up at the API boundary.</p>
      <div data-auth-sessions>${sessionRows(context)}</div>
    </section>

    <section class="administration-user-create">
      <h3>Create service token</h3>
      <p>Service tokens represent non-human identities. The raw token is shown once at creation and is never retrievable from stored token metadata.</p>
      <div class="administration-user-create-fields">
        <label><span>Service identity</span>
          <select data-auth-service>
            <option value="">Select service identity…</option>
            ${services.map((item) => `<option value="${esc(item.id)}">${esc(item.name || item.id)} · ${esc(item.id)}</option>`).join("")}
          </select>
        </label>
        <label><span>Scopes</span>
          <input data-auth-scopes type="text" placeholder="scope.one, scope.two" />
        </label>
        <label><span>Expiry</span>
          <input data-auth-expiry type="datetime-local" />
        </label>
      </div>
      <div class="administration-membership-actions">
        <button type="button" data-auth-create-token>Create service token</button>
      </div>
      <div data-auth-created-secret hidden></div>
    </section>

    <section>
      <h3>Service token metadata</h3>
      <div data-auth-tokens>${tokenRows(context)}</div>
    </section>

    <section>
      <h3>Authentication mechanisms</h3>
      <p>Configuration and status below come from the canonical identity boundary; external identity claims do not grant codex-web authorization by themselves. SSO/OIDC claims establish identity only and authorization remains separately canonical and scoped.</p>
      <div class="workspace-state workspace-state-loading" data-auth-status role="status">
        Loading canonical authentication status…
      </div>
    </section>
  `;

  const message = container.querySelector("[data-auth-message]");
  const sessions = container.querySelector("[data-auth-sessions]");
  const tokens = container.querySelector("[data-auth-tokens]");
  const service = container.querySelector("[data-auth-service]");
  const scopes = container.querySelector("[data-auth-scopes]");
  const expiry = container.querySelector("[data-auth-expiry]");
  const createButton = container.querySelector("[data-auth-create-token]");
  const revokeOthers = container.querySelector("[data-auth-revoke-others]");
  const createdSecret = container.querySelector("[data-auth-created-secret]");
  const authStatus = container.querySelector("[data-auth-status]");

  const loadAuthenticationStatus = async () => {
    try {
      const status = await api("/api/identity/authentication-status");
      authStatus.className = "";
      authStatus.innerHTML = authenticationStatusView(status);
    } catch (error) {
      authStatus.className = "workspace-state workspace-state-denied";
      authStatus.innerHTML = `
        <strong>Authentication status requires administrator step-up</strong>
        <p>${esc(error?.message || "Canonical authentication status is unavailable.")}</p>
        <small>No authentication configuration is inferred from browser state when the canonical status projection is unavailable.</small>
      `;
    }
  };

  queueMicrotask(() => { void loadAuthenticationStatus(); });

  const setMessage = (value, kind = "info") => {
    message.hidden = !value;
    message.dataset.kind = kind;
    message.textContent = value || "";
  };

  revokeOthers.addEventListener("click", async () => {
    if (!window.confirm("Revoke all of your other active sessions?")) return;
    revokeOthers.disabled = true;
    try {
      const result = await api("/api/identity/sessions/revoke-others", { method: "POST" });
      context.identity.sessions = (context.identity.sessions || []).map((item) => (
        item.identity_id === context.actor?.identity_id
        && item.id !== context.actor?.session_id
        && item.revoked_at == null
          ? { ...item, revoked_at: Date.now() / 1000, revoke_reason: "revoked-other-sessions" }
          : item
      ));
      sessions.innerHTML = sessionRows(context);
      setMessage(`Revoked ${Number(result?.revoked || 0)} other session(s).`);
    } catch (error) {
      setMessage(error?.message || "Unable to revoke other sessions.", "error");
      revokeOthers.disabled = false;
    }
  });

  sessions.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-auth-revoke-session]");
    if (!button) return;
    const sessionId = button.dataset.authRevokeSession;
    if (!window.confirm(`Revoke session ${sessionId}?`)) return;
    button.disabled = true;
    try {
      await api(`/api/identity/sessions/${encodeURIComponent(sessionId)}`, { method: "DELETE" });
      context.identity.sessions = (context.identity.sessions || []).map((item) => (
        item.id === sessionId
          ? { ...item, revoked_at: Date.now() / 1000, revoke_reason: `revoked-by:${context.actor?.identity_id || "administrator"}` }
          : item
      ));
      sessions.innerHTML = sessionRows(context);
      setMessage("Session revoked.");
    } catch (error) {
      setMessage(error?.message || "Unable to revoke session.", "error");
      button.disabled = false;
    }
  });

  createButton.addEventListener("click", async () => {
    if (!service.value) {
      setMessage("Select a service identity before creating a token.", "error");
      return;
    }
    const scopeValues = String(scopes.value || "")
      .split(",")
      .map((value) => value.trim())
      .filter(Boolean);
    const expiresAt = expiry.value ? new Date(expiry.value).getTime() / 1000 : null;
    if (expiry.value && !Number.isFinite(expiresAt)) {
      setMessage("Token expiry is invalid.", "error");
      return;
    }
    createButton.disabled = true;
    createdSecret.hidden = true;
    createdSecret.textContent = "";
    try {
      const result = await api("/api/identity/service-tokens", {
        method: "POST",
        body: JSON.stringify({
          service_identity_id: service.value,
          organization_id: context.organizationId,
          workspace_id: context.workspaceId,
          scopes: scopeValues,
          expires_at: expiresAt,
        }),
      });
      createdSecret.hidden = false;
      createdSecret.innerHTML = `
        <div class="workspace-state workspace-state-warning" role="status">
          <strong>Copy this token now</strong>
          <p data-auth-created-token>${esc(result.token || "")}</p>
          <small>This secret is returned once. Stored Administration state contains metadata only.</small>
        </div>
      `;
      context.identity.service_tokens = [
        {
          id: result.token_id,
          service_identity_id: service.value,
          organization_id: context.organizationId,
          workspace_id: context.workspaceId,
          scopes: scopeValues,
          created_at: Date.now() / 1000,
          expires_at: result.expires_at ?? expiresAt,
          last_used_at: null,
          revoked_at: null,
        },
        ...(context.identity.service_tokens || []),
      ];
      tokens.innerHTML = tokenRows(context);
      setMessage("Service token created.");
      createButton.disabled = false;
    } catch (error) {
      setMessage(error?.message || "Unable to create service token.", "error");
      createButton.disabled = false;
    }
  });

  tokens.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-auth-revoke-token]");
    if (!button) return;
    const tokenId = button.dataset.authRevokeToken;
    if (!window.confirm(`Revoke service token ${tokenId}?`)) return;
    button.disabled = true;
    try {
      await api(`/api/identity/service-tokens/${encodeURIComponent(tokenId)}`, { method: "DELETE" });
      context.identity.service_tokens = (context.identity.service_tokens || []).map((item) => (
        item.id === tokenId
          ? { ...item, revoked_at: Date.now() / 1000, revoke_reason: `revoked-by:${context.actor?.identity_id || "administrator"}` }
          : item
      ));
      tokens.innerHTML = tokenRows(context);
      setMessage("Service token revoked.");
    } catch (error) {
      setMessage(error?.message || "Unable to revoke service token.", "error");
      button.disabled = false;
    }
  });
}
