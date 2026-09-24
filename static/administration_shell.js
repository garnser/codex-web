export const ADMINISTRATION_PAGES = Object.freeze({
  overview: {
    title: "Administration",
    purpose: "Manage organization-wide identity, access, authentication and security settings.",
  },
  users: {
    title: "Users",
    purpose: "Manage human identities and their current organization/workspace status.",
  },
  memberships: {
    title: "Memberships & Roles",
    purpose: "Inspect and manage canonical memberships and scoped roles.",
  },
  access: {
    title: "Access",
    purpose: "Inspect direct, inherited and effective access across Projects and repositories.",
  },
  authentication: {
    title: "Authentication",
    purpose: "Manage supported sessions, service tokens and authentication policy without exposing stored secrets.",
  },
  "organization-settings": {
    title: "Organization Settings",
    purpose: "Manage supported organization/workspace settings in the active administrative scope.",
  },
});

export function currentAdministrationRoute(pathname = window.location.pathname) {
  const match = String(pathname || "").match(/^(.*)\/administration(?:\/([^/]+))?\/?$/);
  if (!match) return null;
  return {
    prefix: match[1] || "",
    page: match[2] || "overview",
  };
}

export function administrationPath(page = "overview", { prefix = "" } = {}) {
  const normalized = ADMINISTRATION_PAGES[page] ? page : "overview";
  const root = String(prefix || "").replace(/\/$/, "");
  return `${root}/administration/${normalized}`;
}

export async function loadAdministrationContext(api) {
  const actor = await api("/api/identity/me");
  try {
    const identity = await api("/api/identity");
    return {
      allowed: true,
      denied: false,
      actor,
      identity,
      organizationId: actor.organization_id || "",
      workspaceId: actor.workspace_id || "",
    };
  } catch (error) {
    if (error?.status !== 403) throw error;
    return {
      allowed: false,
      denied: true,
      actor,
      identity: null,
      organizationId: actor.organization_id || "",
      workspaceId: actor.workspace_id || "",
      reason: error.message || "administration access denied",
    };
  }
}
