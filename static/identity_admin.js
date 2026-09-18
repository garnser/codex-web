(async () => {
  const BASE = window.location.pathname.startsWith("/codex") ? "/codex" : "";
  const { request: apiRequest } = await import(`${BASE}/static/api_client.js`);
  let actor = null;

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

  function listText(values) {
    return values?.length ? values.map(escapeHtml).join(", ") : "none";
  }

  function setHtml(id, html) {
    const element = document.getElementById(id);
    if (element) element.innerHTML = html;
  }

  function identityName(id, state) {
    const human = (state.humans || []).find((item) => item.id === id);
    if (human) return human.display_name || human.email || id;
    const service = (state.services || []).find((item) => item.id === id);
    return service?.name || id;
  }

  function renderCurrent(state) {
    if (!actor) return;
    const org = (state.organizations || []).find((item) => item.id === actor.organization_id);
    const workspace = (state.workspaces || []).find((item) => item.id === actor.workspace_id);
    setHtml("identity-current", `<div class="comm-entry">
      <strong>${escapeHtml(identityName(actor.identity_id, state))} · ${escapeHtml(actor.principal_kind)}</strong>
      <small>Identity: ${escapeHtml(actor.identity_id)} · Organization: ${escapeHtml(org?.name || actor.organization_id)} (${escapeHtml(actor.organization_id)}) · Workspace: ${escapeHtml(workspace?.name || actor.workspace_id)} (${escapeHtml(actor.workspace_id)})</small>
      <small>Roles: ${listText(actor.roles)} · Teams: ${listText(actor.team_ids)} · Authentication assurance: ${escapeHtml(actor.assurance)}</small>
      <small>Session: ${escapeHtml(actor.session_id || "none")} · Service token: ${escapeHtml(actor.service_token_id || "none")} · Service scopes: ${listText(actor.service_scopes)}</small>
    </div>`);
  }

  function renderTenants(state) {
    const workspaces = state.workspaces || [];
    setHtml("identity-tenants", (state.organizations || []).map((organization) => {
      const scoped = workspaces.filter((workspace) => workspace.organization_id === organization.id);
      return `<div class="comm-entry">
        <strong>${escapeHtml(organization.name)} · ${escapeHtml(organization.id)}</strong>
        <small>Status: ${organization.disabled_at ? `disabled ${timeText(organization.disabled_at)}` : "active"} · Created: ${timeText(organization.created_at)}</small>
        <small>Workspaces: ${scoped.length ? scoped.map((workspace) => `${escapeHtml(workspace.name)} (${escapeHtml(workspace.id)})${workspace.disabled_at ? " [disabled]" : ""}`).join(", ") : "none"}</small>
      </div>`;
    }).join("") || '<div class="comm-entry"><strong>No organizations.</strong></div>');
  }

  function externalLinks(human) {
    if (!human.external_links?.length) return "External authentication links: none";
    return human.external_links.map((link) => {
      const groups = link.groups_snapshot?.length ? ` · groups: ${link.groups_snapshot.join(", ")}` : "";
      return `${link.provider} · issuer: ${link.issuer} · subject: ${link.subject}${link.email ? ` · ${link.email}` : ""}${groups} · last seen: ${timeText(link.last_seen_at)}`;
    }).join(" | ");
  }

  function renderPrincipals(state) {
    const humans = (state.humans || []).map((item) => `<div class="comm-entry">
      <strong>${escapeHtml(item.display_name)} · human</strong>
      <small>ID: ${escapeHtml(item.id)} · Email: ${escapeHtml(item.email || "none")} · Status: ${item.disabled_at ? "disabled" : "active"}</small>
      <small>${escapeHtml(externalLinks(item))}</small>
    </div>`);
    const services = (state.services || []).map((item) => `<div class="comm-entry">
      <strong>${escapeHtml(item.name)} · service</strong>
      <small>ID: ${escapeHtml(item.id)} · Status: ${item.disabled_at ? "disabled" : "active"}${item.description ? ` · ${escapeHtml(item.description)}` : ""}</small>
    </div>`);
    setHtml("identity-principals", [...humans, ...services].join("") || '<div class="comm-entry"><strong>No identities.</strong></div>');
  }

  function renderMemberships(state) {
    const memberships = (state.memberships || []).map((item) => `<div class="comm-entry">
      <strong>${escapeHtml(identityName(item.identity_id, state))} · ${escapeHtml(item.principal_kind)}</strong>
      <small>Membership: ${escapeHtml(item.id)} · Organization: ${escapeHtml(item.organization_id)} · Workspace: ${escapeHtml(item.workspace_id || "organization-wide")}</small>
      <small>Roles: ${listText(item.roles)} · Teams: ${listText(item.team_ids)} · Status: ${item.revoked_at ? `revoked ${timeText(item.revoked_at)}` : "active"}</small>
    </div>`);
    const teams = (state.teams || []).map((item) => `<div class="comm-entry">
      <strong>Team: ${escapeHtml(item.name)} · ${escapeHtml(item.id)}</strong>
      <small>Organization: ${escapeHtml(item.organization_id)} · Workspace: ${escapeHtml(item.workspace_id || "organization-wide")} · Members: ${listText(item.member_identity_ids)}</small>
    </div>`);
    setHtml("identity-memberships", [...memberships, ...teams].join("") || '<div class="comm-entry"><strong>No memberships or teams.</strong></div>');
  }

  function renderSessions(state) {
    const rows = (state.sessions || []).map((item) => {
      const current = item.id === actor?.session_id;
      return `<div class="comm-entry">
        <strong>${escapeHtml(identityName(item.identity_id, state))} · session${current ? " · current" : ""}</strong>
        <small>ID: ${escapeHtml(item.id)} · Tenant: ${escapeHtml(item.organization_id)}/${escapeHtml(item.workspace_id)} · Assurance: ${escapeHtml(item.assurance)} · Rotation: ${escapeHtml(item.rotation)}</small>
        <small>Created: ${timeText(item.created_at)} · Last seen: ${timeText(item.last_seen_at)} · Idle expiry: ${timeText(item.idle_expires_at)} · Absolute expiry: ${timeText(item.absolute_expires_at)}</small>
        <small>Step-up until: ${timeText(item.step_up_until)} · Status: ${item.revoked_at ? `revoked ${timeText(item.revoked_at)}` : "active"}</small>
        ${item.revoked_at ? "" : `<button type="button" class="ghost-button" data-identity-revoke-session="${escapeHtml(item.id)}" data-current="${current ? "true" : "false"}">Revoke session</button>`}
      </div>`;
    });
    const revokeOthers = actor?.principal_kind === "human"
      ? '<button id="identity-revoke-other-sessions" type="button" class="ghost-button">Revoke my other sessions</button>'
      : "";
    setHtml("identity-sessions", `${revokeOthers}${rows.join("") || '<div class="comm-entry"><strong>No sessions.</strong></div>'}`);
  }

  function renderTokens(state) {
    setHtml("identity-service-tokens", (state.service_tokens || []).map((item) => `<div class="comm-entry">
      <strong>${escapeHtml(identityName(item.service_identity_id, state))} · service token</strong>
      <small>ID: ${escapeHtml(item.id)} · Tenant: ${escapeHtml(item.organization_id)}/${escapeHtml(item.workspace_id)} · Scopes: ${listText(item.scopes)}</small>
      <small>Created: ${timeText(item.created_at)} · Expires: ${timeText(item.expires_at)} · Last used: ${timeText(item.last_used_at)} · Rotation: ${escapeHtml(item.rotation)}</small>
      <small>Status: ${item.revoked_at ? `revoked ${timeText(item.revoked_at)} · ${escapeHtml(item.revoke_reason || "no reason")}` : "active"}</small>
      ${item.revoked_at ? "" : `<button type="button" class="ghost-button" data-identity-revoke-token="${escapeHtml(item.id)}">Revoke token</button>`}
    </div>`).join("") || '<div class="comm-entry"><strong>No service tokens.</strong><small>Raw service-token material is never returned by this listing.</small></div>');
  }

  function renderRecovery(state) {
    setHtml("identity-recovery", (state.recovery_factors || []).map((item) => `<div class="comm-entry">
      <strong>${escapeHtml(identityName(item.identity_id, state))} · ${escapeHtml(item.provider)}</strong>
      <small>Factor: ${escapeHtml(item.id)} · Label: ${escapeHtml(item.label)} · Created: ${timeText(item.created_at)} · Status: ${item.disabled_at ? "disabled" : "active"}</small>
    </div>`).join("") || '<div class="comm-entry"><strong>No recovery factors.</strong></div>');
  }

  async function refresh() {
    const status = document.getElementById("identity-admin-status");
    if (status) status.textContent = "Loading canonical identity state...";
    try {
      const [currentActor, state] = await Promise.all([
        apiRequest("/api/identity/me"),
        apiRequest("/api/identity"),
      ]);
      actor = currentActor;
      renderCurrent(state);
      renderTenants(state);
      renderPrincipals(state);
      renderMemberships(state);
      renderSessions(state);
      renderTokens(state);
      renderRecovery(state);
      if (status) status.textContent = `Identity state loaded · current assurance: ${actor.assurance}`;
    } catch (error) {
      if (status) status.textContent = `Identity administration unavailable: ${error.message}`;
    }
  }

  async function revokeSession(button) {
    const sessionId = button.dataset.identityRevokeSession;
    const current = button.dataset.current === "true";
    if (!sessionId) return;
    const warning = current
      ? " This is the current session and successful revocation will invalidate it."
      : " The server requires appropriate administrative authority and MFA for another session.";
    if (!window.confirm(`Revoke browser session ${sessionId}?${warning}`)) return;
    button.disabled = true;
    try {
      await apiRequest(`/api/identity/sessions/${encodeURIComponent(sessionId)}`, { method: "DELETE" });
      await refresh();
    } catch (error) {
      button.disabled = false;
      document.getElementById("identity-admin-status").textContent = `Session revocation failed: ${error.message}`;
    }
  }

  async function revokeOtherSessions() {
    if (!window.confirm("Revoke all other browser sessions for the current human identity?")) return;
    try {
      const result = await apiRequest("/api/identity/sessions/revoke-others", { method: "POST" });
      document.getElementById("identity-admin-status").textContent = `Revoked ${result.revoked ?? 0} other session(s).`;
      await refresh();
    } catch (error) {
      document.getElementById("identity-admin-status").textContent = `Session revocation failed: ${error.message}`;
    }
  }

  async function revokeToken(button) {
    const tokenId = button.dataset.identityRevokeToken;
    if (!tokenId) return;
    if (!window.confirm(`Revoke service token ${tokenId}? The server requires administrative authority and MFA; this cannot reveal or recover the token material.`)) return;
    button.disabled = true;
    try {
      await apiRequest(`/api/identity/service-tokens/${encodeURIComponent(tokenId)}`, { method: "DELETE" });
      await refresh();
    } catch (error) {
      button.disabled = false;
      document.getElementById("identity-admin-status").textContent = `Service-token revocation failed: ${error.message}`;
    }
  }

  function bind() {
    const panel = document.getElementById("developer-panel");
    document.getElementById("refresh-identity-admin")?.addEventListener("click", refresh);
    document.getElementById("refresh-developer")?.addEventListener("click", refresh);
    document.getElementById("identity-sessions")?.addEventListener("click", (event) => {
      const sessionButton = event.target.closest?.("[data-identity-revoke-session]");
      if (sessionButton) {
        revokeSession(sessionButton).catch(console.error);
        return;
      }
      if (event.target.closest?.("#identity-revoke-other-sessions")) {
        revokeOtherSessions().catch(console.error);
      }
    });
    document.getElementById("identity-service-tokens")?.addEventListener("click", (event) => {
      const button = event.target.closest?.("[data-identity-revoke-token]");
      if (button) revokeToken(button).catch(console.error);
    });
    panel?.addEventListener("toggle", () => {
      if (panel.open) refresh().catch(console.error);
    });
    if (panel?.open) refresh().catch(console.error);
  }

  window.addEventListener("DOMContentLoaded", bind);
})();
