(async () => {
  const BASE = window.location.pathname.startsWith("/codex") ? "/codex" : "";
  const { request: apiRequest } = await import(`${BASE}/static/api_client.js`);
  let actor = null;
  let state = null;

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");
  }

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

  function options(items, label, selected = "") {
    return items.map((item) => (
      `<option value="${escapeHtml(item.id)}"${item.id === selected ? " selected" : ""}>${escapeHtml(label(item))} · ${escapeHtml(item.id)}</option>`
    )).join("");
  }

  function workspaceRows(organizationId) {
    return (state?.workspaces || []).filter((item) => item.organization_id === organizationId);
  }

  function populateOrganizations() {
    const organizations = state?.organizations || [];
    const html = options(organizations, (item) => item.name);
    ["identity-create-workspace-org", "identity-membership-org", "identity-token-org"].forEach((id) => {
      const select = document.getElementById(id);
      const previous = select?.value || actor?.organization_id || "";
      if (!select) return;
      select.innerHTML = html;
      if (organizations.some((item) => item.id === previous)) select.value = previous;
    });
  }

  function populateWorkspaces() {
    const membershipOrg = document.getElementById("identity-membership-org")?.value || "";
    const membership = document.getElementById("identity-membership-workspace");
    if (membership) {
      const previous = membership.value;
      membership.innerHTML = '<option value="">Organization-wide membership</option>'
        + options(workspaceRows(membershipOrg), (item) => item.name);
      if ([...membership.options].some((item) => item.value === previous)) membership.value = previous;
    }

    const tokenOrg = document.getElementById("identity-token-org")?.value || "";
    const tokenWorkspace = document.getElementById("identity-token-workspace");
    if (tokenWorkspace) {
      const rows = workspaceRows(tokenOrg);
      const previous = tokenWorkspace.value || actor?.workspace_id || "";
      tokenWorkspace.innerHTML = options(rows, (item) => item.name);
      if (rows.some((item) => item.id === previous)) tokenWorkspace.value = previous;
    }
    populateTeams();
    populateTokenServices();
  }

  function populatePrincipals() {
    const rows = [
      ...(state?.humans || []).map((item) => ({
        id: item.id,
        kind: "human",
        label: item.display_name || item.email || item.id,
      })),
      ...(state?.services || []).map((item) => ({
        id: item.id,
        kind: "service",
        label: item.name || item.id,
      })),
    ];
    const select = document.getElementById("identity-membership-identity");
    if (!select) return;
    const previous = select.value;
    select.innerHTML = rows.map((item) => (
      `<option value="${escapeHtml(item.id)}" data-kind="${item.kind}">${escapeHtml(item.label)} · ${item.kind} · ${escapeHtml(item.id)}</option>`
    )).join("");
    if (rows.some((item) => item.id === previous)) select.value = previous;
  }

  function populateTeams() {
    const org = document.getElementById("identity-membership-org")?.value || "";
    const workspace = document.getElementById("identity-membership-workspace")?.value || "";
    const teams = (state?.teams || []).filter((item) => (
      item.organization_id === org
      && (!item.workspace_id || !workspace || item.workspace_id === workspace)
    ));
    const select = document.getElementById("identity-membership-teams");
    if (select) select.innerHTML = options(teams, (item) => item.name);
  }

  function serviceEligible(serviceId, org, workspace) {
    return (state?.memberships || []).some((membership) => (
      membership.identity_id === serviceId
      && membership.principal_kind === "service"
      && !membership.revoked_at
      && membership.organization_id === org
      && (!membership.workspace_id || membership.workspace_id === workspace)
    ));
  }

  function populateTokenServices() {
    const org = document.getElementById("identity-token-org")?.value || "";
    const workspace = document.getElementById("identity-token-workspace")?.value || "";
    const services = (state?.services || []).filter((item) => (
      !item.disabled_at && serviceEligible(item.id, org, workspace)
    ));
    const select = document.getElementById("identity-token-service");
    if (!select) return;
    const previous = select.value;
    select.innerHTML = options(services, (item) => item.name);
    if (services.some((item) => item.id === previous)) select.value = previous;
  }

  function hydrate(detail) {
    actor = detail.actor;
    state = detail.state;
    const requirement = document.getElementById("identity-authority-requirement");
    if (requirement) {
      requirement.textContent = `Sensitive identity administration requires canonical admin authority and MFA/step-up assurance. Current assurance: ${actor?.assurance || "unknown"}.`;
    }
    populateOrganizations();
    populateWorkspaces();
    populatePrincipals();
  }

  async function post(path, payload, success) {
    try {
      await apiRequest(path, {
        method: "POST",
        body: JSON.stringify(payload),
      });
      setStatus(success);
      refreshIdentity();
    } catch (error) {
      setStatus(`Identity mutation failed: ${error.message}`);
    }
  }

  async function createOrganization() {
    const name = document.getElementById("identity-create-org-name")?.value.trim() || "";
    const id = document.getElementById("identity-create-org-id")?.value.trim() || null;
    if (!name) return setStatus("Organization name is required.");
    if (!window.confirm(`Create organization "${name}"? This is sensitive tenant administration and the server requires admin+MFA assurance.`)) return;
    await post("/api/identity/organizations", { id, name }, `Created organization ${name}.`);
  }

  async function createWorkspace() {
    const organizationId = document.getElementById("identity-create-workspace-org")?.value || "";
    const name = document.getElementById("identity-create-workspace-name")?.value.trim() || "";
    const id = document.getElementById("identity-create-workspace-id")?.value.trim() || null;
    if (!organizationId || !name) return setStatus("Organization and workspace name are required.");
    if (!window.confirm(`Create workspace "${name}" in organization ${organizationId}? Server-side admin+MFA is required.`)) return;
    await post("/api/identity/workspaces", {
      id,
      organization_id: organizationId,
      name,
    }, `Created workspace ${name}.`);
  }

  async function createHuman() {
    const displayName = document.getElementById("identity-create-human-name")?.value.trim() || "";
    const email = document.getElementById("identity-create-human-email")?.value.trim() || null;
    const id = document.getElementById("identity-create-human-id")?.value.trim() || null;
    if (!displayName) return setStatus("Human display name is required.");
    if (!window.confirm(`Create human identity "${displayName}"? Creation does not grant membership or authority.`)) return;
    await post("/api/identity/humans", { id, display_name: displayName, email }, `Created human identity ${displayName}.`);
  }

  async function createService() {
    const name = document.getElementById("identity-create-service-name")?.value.trim() || "";
    const description = document.getElementById("identity-create-service-description")?.value.trim() || null;
    if (!name) return setStatus("Service identity name is required.");
    if (!window.confirm(`Create service identity "${name}"? Creation does not grant tenant membership, scopes, or a token.`)) return;
    await post("/api/identity/services", { name, description }, `Created service identity ${name}.`);
  }

  async function createMembership() {
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
    if (!window.confirm(`Grant ${roles.join(", ")} membership to ${identityId} in ${target}? This expands canonical human/service authority and requires admin+MFA.`)) return;
    await post("/api/identity/memberships", {
      identity_id: identityId,
      principal_kind: principalKind,
      organization_id: organizationId,
      workspace_id: workspaceId,
      roles,
      team_ids: teamIds,
    }, `Created membership for ${identityId}.`);
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

  async function createToken() {
    clearCreatedToken();
    const serviceIdentityId = document.getElementById("identity-token-service")?.value || "";
    const organizationId = document.getElementById("identity-token-org")?.value || "";
    const workspaceId = document.getElementById("identity-token-workspace")?.value || "";
    const scopes = (document.getElementById("identity-token-scopes")?.value || "")
      .split(",").map((item) => item.trim()).filter(Boolean);
    if (!serviceIdentityId || !organizationId || !workspaceId) {
      return setStatus("Service identity, organization and workspace are required.");
    }
    if (!window.confirm(`Create a service token for ${serviceIdentityId} in ${organizationId}/${workspaceId} with scopes [${scopes.join(", ") || "none"}]? The raw token will be shown once and is not persisted by this UI.`)) return;
    try {
      const credentials = await apiRequest("/api/identity/service-tokens", {
        method: "POST",
        body: JSON.stringify({
          service_identity_id: serviceIdentityId,
          organization_id: organizationId,
          workspace_id: workspaceId,
          scopes,
          expires_at: tokenExpiry(),
        }),
      });
      const result = document.getElementById("identity-created-token-result");
      const token = document.getElementById("identity-created-token");
      if (token) token.textContent = credentials.token || "";
      if (result) result.hidden = false;
      setStatus(`Created service token ${credentials.token_id}. Copy it now; it will not appear in list APIs.`);
      refreshIdentity();
    } catch (error) {
      setStatus(`Service-token creation failed: ${error.message}`);
    }
  }

  function bind() {
    document.getElementById("identity-membership-org")?.addEventListener("change", populateWorkspaces);
    document.getElementById("identity-membership-workspace")?.addEventListener("change", populateTeams);
    document.getElementById("identity-token-org")?.addEventListener("change", populateWorkspaces);
    document.getElementById("identity-token-workspace")?.addEventListener("change", populateTokenServices);
    document.getElementById("identity-create-org")?.addEventListener("click", () => createOrganization().catch(console.error));
    document.getElementById("identity-create-workspace")?.addEventListener("click", () => createWorkspace().catch(console.error));
    document.getElementById("identity-create-human")?.addEventListener("click", () => createHuman().catch(console.error));
    document.getElementById("identity-create-service")?.addEventListener("click", () => createService().catch(console.error));
    document.getElementById("identity-create-membership")?.addEventListener("click", () => createMembership().catch(console.error));
    document.getElementById("identity-create-token")?.addEventListener("click", () => createToken().catch(console.error));
    document.getElementById("identity-clear-created-token")?.addEventListener("click", clearCreatedToken);
  }

  window.addEventListener("codex:identity-state-rendered", (event) => hydrate(event.detail || {}));
  window.addEventListener("DOMContentLoaded", bind);
})();
