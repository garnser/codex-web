(async () => {
  const BASE = window.location.pathname.startsWith("/codex") ? "/codex" : "";
  let latestDetail = null, ready = false;
  window.addEventListener("codex:identity-state-rendered", event => {
    latestDetail = event.detail || {}; if (ready) hydrate(latestDetail);
  });
  const { confirmAction } = await import(`${BASE}/static/action_confirmation.js`);
  const { request: apiRequest } = await import(`${BASE}/static/api_client.js`);
  const { hydrateIdentityOptions, populateWorkspaces, populateTeams, populateTokenServices } = await import(`${BASE}/static/identity_authority_options.js`);
  const { setupIdentityEditors, resetIdentityEditors, identityEditsPending, runIdentityMutation } = await import(`${BASE}/static/identity_editor_state.js`);
  const { bindWhenReady } = await import(`${BASE}/static/reference_links.js`);

  function setStatus(message) {
    const element = document.getElementById("identity-authority-status");
    if (element) {
      element.hidden = false;
      element.textContent = message;
    }
  }

  function refreshIdentity() {
    document.getElementById("refresh-identity-admin")?.click();
  }

  function hydrate(detail) {
    const requirement = document.getElementById("identity-authority-requirement");
    if (requirement) {
      requirement.textContent = `Sensitive identity administration requires canonical admin authority and MFA/step-up assurance. Current assurance: ${detail.actor?.assurance || "unknown"}.`;
    }
    if (identityEditsPending()) { setStatus("Refresh deferred. Save or discard unsaved identity edits first."); return; }
    hydrateIdentityOptions(detail); resetIdentityEditors();
  }

  async function post(path, payload, success, ticket) {
    if (!ticket.current()) return;
    ticket.submitting();
    try {
      await apiRequest(path, {
        method: "POST",
        body: JSON.stringify(payload),
      });
      if (!ticket.current()) return;
      ticket.saved();
      setStatus(success);
      refreshIdentity();
    } catch (error) {
      if (!ticket.current()) return;
      setStatus(`Identity mutation failed: ${error.message}`);
    }
  }

  async function createOrganization(ticket) {
    const name = document.getElementById("identity-create-org-name")?.value.trim() || "";
    const id = document.getElementById("identity-create-org-id")?.value.trim() || null;
    if (!name) return setStatus("Organization name is required.");
    if (!await confirmAction({ action: 'Create organization', target: name, risk: 'bounded', consequence: `Create organization "${name}"? This is sensitive tenant administration and the server requires admin+MFA assurance.`, recovery: 'Creation alone does not grant membership or authority.' })) return;
    await post("/api/identity/organizations", { id, name }, `Created organization ${name}.`, ticket);
  }

  async function createWorkspace(ticket) {
    const organizationId = document.getElementById("identity-create-workspace-org")?.value || "";
    const name = document.getElementById("identity-create-workspace-name")?.value.trim() || "";
    const id = document.getElementById("identity-create-workspace-id")?.value.trim() || null;
    if (!organizationId || !name) return setStatus("Organization and workspace name are required.");
    if (!await confirmAction({ action: 'Create workspace', target: `${name} in ${organizationId}`, risk: 'bounded', consequence: `Create workspace "${name}" in organization ${organizationId}? Server-side admin+MFA is required.`, recovery: 'Creation alone does not grant membership or authority.' })) return;
    await post("/api/identity/workspaces", {
      id,
      organization_id: organizationId,
      name,
    }, `Created workspace ${name}.`, ticket);
  }

  async function createHuman(ticket) {
    const displayName = document.getElementById("identity-create-human-name")?.value.trim() || "";
    const email = document.getElementById("identity-create-human-email")?.value.trim() || null;
    const id = document.getElementById("identity-create-human-id")?.value.trim() || null;
    if (!displayName) return setStatus("Human display name is required.");
    if (!await confirmAction({ action: 'Create human identity', target: displayName, risk: 'bounded', consequence: `Create human identity "${displayName}"? Creation does not grant membership or authority.`, recovery: 'Membership and authority require separate authorized actions.' })) return;
    await post("/api/identity/humans", { id, display_name: displayName, email }, `Created human identity ${displayName}.`, ticket);
  }

  async function createService(ticket) {
    const name = document.getElementById("identity-create-service-name")?.value.trim() || "";
    const description = document.getElementById("identity-create-service-description")?.value.trim() || null;
    if (!name) return setStatus("Service identity name is required.");
    if (!await confirmAction({ action: 'Create service identity', target: name, risk: 'bounded', consequence: `Create service identity "${name}"? Creation does not grant tenant membership, scopes, or a token.`, recovery: 'Membership, authority and token creation are separate actions.' })) return;
    await post("/api/identity/services", { name, description }, `Created service identity ${name}.`, ticket);
  }

  async function createMembership(ticket) {
    const identity = document.getElementById("identity-membership-identity");
    const identityId = identity?.value || "";
    const principalKind = identity?.selectedOptions?.[0]?.dataset.kind || "";
    const organizationId = document.getElementById("identity-membership-org")?.value || "";
    const workspaceId = document.getElementById("identity-membership-workspace")?.value || null;
    const roles = Array.from(document.getElementById("identity-membership-roles")?.selectedOptions || []).map((item) => item.value);
    const teamIds = Array.from(document.getElementById("identity-membership-teams")?.selectedOptions || []).map((item) => item.value);
    if (!identityId || !principalKind || !organizationId || !roles.length) {
      return setStatus("Identity, tenant and at least one role are required.");
    }
    const target = workspaceId ? `${organizationId}/${workspaceId}` : organizationId;
    if (!await confirmAction({ action: 'Grant tenant membership', target: `${identityId} in ${target}`, risk: 'high', consequence: `Grant ${roles.join(", ")} membership to ${identityId} in ${target}? This expands canonical human/service authority and requires admin+MFA.`, recovery: 'An authorized administrator can revoke membership; prior actions are not undone.' })) return;
    await post("/api/identity/memberships", {
      identity_id: identityId,
      principal_kind: principalKind,
      organization_id: organizationId,
      workspace_id: workspaceId,
      roles,
      team_ids: teamIds,
    }, `Created membership for ${identityId}.`, ticket);
  }

  function tokenExpiry() {
    const value = document.getElementById("identity-token-expires")?.value || "";
    if (!value) return null;
    const timestamp = new Date(value).getTime();
    return Number.isNaN(timestamp) ? null : timestamp / 1000;
  }

  function clearCreatedToken() {
    const result = document.getElementById("identity-created-token-result");
    const token = document.getElementById("identity-created-token");
    if (token) token.textContent = "";
    if (result) result.hidden = true;
  }

  async function createToken(ticket) {
    clearCreatedToken();
    const serviceIdentityId = document.getElementById("identity-token-service")?.value || "";
    const organizationId = document.getElementById("identity-token-org")?.value || "";
    const workspaceId = document.getElementById("identity-token-workspace")?.value || "";
    const scopes = (document.getElementById("identity-token-scopes")?.value || "")
      .split(",").map((item) => item.trim()).filter(Boolean);
    if (!serviceIdentityId || !organizationId || !workspaceId) {
      return setStatus("Service identity, organization and workspace are required.");
    }
    if (!await confirmAction({ action: 'Create service token', target: `${serviceIdentityId} in ${organizationId}/${workspaceId}`, risk: 'high', consequence: `Create a service token for ${serviceIdentityId} in ${organizationId}/${workspaceId} with scopes [${scopes.join(", ") || "none"}]? The raw token will be shown once and is not persisted by this UI.`, recovery: 'The token can be revoked through canonical identity management; raw material is shown once.' })) return;
    if (!ticket.current()) return;
    const expiresAt = tokenExpiry();
    ticket.submitting();
    try {
      const credentials = await apiRequest("/api/identity/service-tokens", {
        method: "POST",
        body: JSON.stringify({
          service_identity_id: serviceIdentityId,
          organization_id: organizationId,
          workspace_id: workspaceId,
          scopes,
          expires_at: expiresAt,
        }),
      });
      if (!ticket.current()) return;
      ticket.saved();
      const result = document.getElementById("identity-created-token-result");
      const token = document.getElementById("identity-created-token");
      if (token) token.textContent = credentials.token || "";
      if (result) result.hidden = false;
      setStatus(`Created service token ${credentials.token_id}. Copy it now; it will not appear in list APIs.`);
      refreshIdentity();
    } catch (error) {
      if (!ticket.current()) return;
      setStatus(`Service-token creation failed: ${error.message}`);
    }
  }

  function bind() {
    setupIdentityEditors();
    ready = true; if (latestDetail) hydrate(latestDetail);
    document.getElementById("identity-membership-org")?.addEventListener("change", populateWorkspaces);
    document.getElementById("identity-membership-workspace")?.addEventListener("change", populateTeams);
    document.getElementById("identity-token-org")?.addEventListener("change", populateWorkspaces);
    document.getElementById("identity-token-workspace")?.addEventListener("change", populateTokenServices);
    document.getElementById("identity-create-org")?.addEventListener("click", () => runIdentityMutation("org", createOrganization).catch(console.error));
    document.getElementById("identity-create-workspace")?.addEventListener("click", () => runIdentityMutation("workspace", createWorkspace).catch(console.error));
    document.getElementById("identity-create-human")?.addEventListener("click", () => runIdentityMutation("human", createHuman).catch(console.error));
    document.getElementById("identity-create-service")?.addEventListener("click", () => runIdentityMutation("service", createService).catch(console.error));
    document.getElementById("identity-create-membership")?.addEventListener("click", () => runIdentityMutation("membership", createMembership).catch(console.error));
    document.getElementById("identity-create-token")?.addEventListener("click", () => runIdentityMutation("token", createToken).catch(console.error));
    document.getElementById("identity-clear-created-token")?.addEventListener("click", clearCreatedToken);
  }

  bindWhenReady(bind);
})();
