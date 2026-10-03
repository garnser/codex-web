import { esc as escapeHtml } from './reference_links.js';
let actor = null, state = null;
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

export function populateWorkspaces(event) {
  const target = event?.target?.id;
  const membershipOrg = document.getElementById("identity-membership-org")?.value || "";
  const membership = document.getElementById("identity-membership-workspace");
  if (membership && (!target || target === "identity-membership-org")) {
    const previous = membership.value;
    membership.innerHTML = '<option value="">Organization-wide membership</option>'
      + options(workspaceRows(membershipOrg), (item) => item.name);
    if ([...membership.options].some((item) => item.value === previous)) membership.value = previous;
  }

  const tokenOrg = document.getElementById("identity-token-org")?.value || "";
  const tokenWorkspace = document.getElementById("identity-token-workspace");
  if (tokenWorkspace && (!target || target === "identity-token-org")) {
    const rows = workspaceRows(tokenOrg);
    const previous = tokenWorkspace.value || actor?.workspace_id || "";
    tokenWorkspace.innerHTML = options(rows, (item) => item.name);
    if (rows.some((item) => item.id === previous)) tokenWorkspace.value = previous;
  }
  if (!target || target === "identity-membership-org") populateTeams();
  if (!target || target === "identity-token-org") populateTokenServices();
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

export function populateTeams() {
  const org = document.getElementById("identity-membership-org")?.value || "";
  const workspace = document.getElementById("identity-membership-workspace")?.value || "";
  const teams = (state?.teams || []).filter((item) => (
    item.organization_id === org
    && (!item.workspace_id || !workspace || item.workspace_id === workspace)
  ));
  const select = document.getElementById("identity-membership-teams");
  if (select) {
    const selected = new Set([...select.selectedOptions].map(item => item.value));
    select.innerHTML = options(teams, item => item.name);
    for (const option of select.options) option.selected = selected.has(option.value);
  }
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

export function populateTokenServices() {
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


export function hydrateIdentityOptions(detail) {
  actor = detail.actor; state = detail.state;
  populateOrganizations(); populateWorkspaces(); populatePrincipals();
}
