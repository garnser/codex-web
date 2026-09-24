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


export const ADMINISTRATION_NAVIGATION = Object.freeze([
  { page: "overview", label: "Overview", description: "Administrative scope, identity and security status." },
  { page: "users", label: "Users", description: "Human identities in the active organization/workspace." },
  { page: "memberships", label: "Memberships & Roles", description: "Direct memberships and scoped canonical roles." },
  { page: "access", label: "Access", description: "Project and repository access, including inheritance and effective scope." },
  { page: "authentication", label: "Authentication", description: "Sessions, service tokens and supported authentication policy." },
  { page: "organization-settings", label: "Organization Settings", description: "Supported organization/workspace settings." },
]);

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

export function administrationPresentation(page = "overview") {
  return ADMINISTRATION_PAGES[page] || ADMINISTRATION_PAGES.overview;
}

function administrationPageContent(page, context, presentation) {
  const identity = context?.identity || {};
  if (page === "overview") {
    const organization = (identity.organizations || []).find(
      (item) => item.id === context?.organizationId,
    );
    const workspace = (identity.workspaces || []).find(
      (item) => item.id === context?.workspaceId,
    );
    const activeHumans = (identity.humans || []).filter((item) => !item.disabled_at);
    const activeMemberships = (identity.memberships || []).filter((item) => !item.revoked_at);
    const activeSessions = (identity.sessions || []).filter((item) => !item.revoked_at);
    const activeTokens = (identity.service_tokens || []).filter((item) => !item.revoked_at);
    return `
      <section class="product-administration-overview" data-administration-overview>
        <h3>${escapeHtml(organization?.name || context?.organizationId || "Organization")}</h3>
        <p>${escapeHtml(workspace?.name || context?.workspaceId || "Workspace")} · authenticated as ${escapeHtml(context?.actor?.identity_id || "unknown identity")}</p>
        <div class="product-administration-summary">
          <div><strong>${activeHumans.length}</strong><span>active users</span></div>
          <div><strong>${activeMemberships.length}</strong><span>active memberships</span></div>
          <div><strong>${activeSessions.length}</strong><span>active sessions</span></div>
          <div><strong>${activeTokens.length}</strong><span>active service tokens</span></div>
        </div>
        <p class="product-field-help">Administration uses canonical Organization/Workspace identity and authorization state. Project selection does not change this administrative scope.</p>
      </section>
    `;
  }
  const guidance = {
    users: "Human identities and membership status come from canonical identity state. Sensitive authentication material is never displayed.",
    memberships: "Direct memberships and scoped roles remain canonical authorization state. High-impact mutations may require stronger authentication.",
    access: "Project and repository authorization is distinct from repository registration. Direct, inherited and effective access will be shown separately.",
    authentication: "Sessions and service-token metadata may be administered here. Stored token or credential values are never retrievable.",
    "organization-settings": "Only settings backed by canonical Organization/Workspace APIs belong here; unsupported controls are not invented in the client.",
  };
  return `
    <section class="product-administration-route-state" data-administration-page-state>
      <h3>${escapeHtml(presentation.title)}</h3>
      <p>${escapeHtml(guidance[page] || presentation.purpose)}</p>
    </section>
  `;
}

export function renderAdministrationNavigation(container, {
  page = "overview",
  context = null,
  onNavigate = null,
} = {}) {
  if (!container) return;
  const presentation = administrationPresentation(page);
  const organizationId = context?.organizationId || "";
  const workspaceId = context?.workspaceId || "";
  const denied = Boolean(context?.denied);
  container.innerHTML = `
    <section class="product-administration-shell" data-administration-shell>
      <header class="product-administration-header">
        <div>
          <small data-administration-scope>Organization / Workspace</small>
          <h2 data-administration-title>${escapeHtml(presentation.title)}</h2>
          <p data-administration-purpose>${escapeHtml(presentation.purpose)}</p>
        </div>
        <div class="product-administration-scope" aria-label="Administrative scope">
          <strong>${escapeHtml(organizationId || "Unknown organization")}</strong>
          <span>${escapeHtml(workspaceId || "Unknown workspace")}</span>
        </div>
      </header>
      <div class="product-administration-layout">
        <nav class="product-administration-nav" aria-label="Administration navigation">
          ${ADMINISTRATION_NAVIGATION.map((item) => `
            <button
              type="button"
              data-administration-page="${escapeHtml(item.page)}"
              aria-current="${item.page === page ? "page" : "false"}"
              ${denied ? "disabled" : ""}
            >
              <strong>${escapeHtml(item.label)}</strong>
              <small>${escapeHtml(item.description)}</small>
            </button>
          `).join("")}
        </nav>
        <main class="product-administration-content" data-administration-content>
          ${denied ? `
            <div class="workspace-state workspace-state-denied" role="alert" data-administration-denied>
              <strong>Administration access denied</strong>
              <p>${escapeHtml(context?.reason || "The canonical identity service denied administrative access.")}</p>
            </div>
          ` : administrationPageContent(page, context, presentation)}
        </main>
      </div>
    </section>
  `;
  if (denied || typeof onNavigate !== "function") return;
  container.querySelectorAll("[data-administration-page]").forEach((button) => {
    button.addEventListener("click", () => onNavigate(button.dataset.administrationPage));
  });
}
