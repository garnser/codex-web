import { statusBadge as sharedStatusBadge, statusFamily as sharedStatusFamily } from "./workspace_components.js";
import { renderHomeOverview } from "./home_overview.js";
import {
  administrationPath,
  administrationPresentation,
  currentAdministrationRoute,
  loadAdministrationContext,
  renderAdministrationNavigation,
} from "./administration_shell.js";

const WORKSPACES = [
  { id: "overview", label: "Home", group: "Home", kind: "embedded", description: "Current workspace orientation, status vocabulary, explainability and shortcuts." },
  { id: "inbox", label: "Attention", group: "Home", kind: "launcher", selector: "[data-attention-launch]", description: "Canonical human-intervention queue." },
  { id: "projects", label: "Projects", group: "Home", kind: "focus", selector: "#projects", description: "Project selection and creation." },
  { id: "setup", label: "Project Setup / Readiness", group: "Home", kind: "launcher", selector: "#project-setup-launch", description: "Bootstrap, migration planning, readiness blockers and guided remediation." },
  { id: "threads", label: "Threads", group: "Work", kind: "focus", selector: "#thread-search", description: "Fast conversational work remains directly accessible." },
  { id: "work", label: "Work", group: "Work", kind: "embedded", description: "Canonical work graph and execution continuity." },
  { id: "goals", label: "Goals", group: "Work", kind: "launcher", selector: "#goals-button", description: "Outcome definitions and progress." },
  { id: "decisions", label: "Decisions", group: "Work", kind: "launcher", selector: "#decisions-button", description: "Canonical decisions and provenance." },
  { id: "metrics", label: "Metrics / KPIs", group: "Work", kind: "launcher", selector: "#metrics-button", description: "Versioned measurements, observations and snapshots." },
  { id: "company", label: "Company Operations", group: "Work", kind: "launcher", selector: "#company-operations-button", description: "Governed business entities, facts and operating KPIs." },
  { id: "agents", label: "Team / Agents", group: "Team", kind: "embedded", description: "Agent identities, provider/runtime/session state and team execution context." },
  { id: "skills", label: "Skills", group: "Team", kind: "embedded", description: "Reusable versioned procedures, exact Agent Profile pins and execution provenance." },
  { id: "autonomy", label: "Automation / Autonomy", group: "Automation", kind: "embedded", description: "Autonomy controls, orchestration, ActionIntents and explainability." },
  { id: "integrations", label: "Integrations / Extensions", group: "Automation", kind: "embedded", description: "Extensions, ActionProviders and input/source integrations." },
  { id: "operations", label: "Operations / Observability", group: "Operations", kind: "embedded", description: "Logs, evidence, health, incidents, releases and recovery state." },
  { id: "workers", label: "Workers / Execution", group: "Operations", kind: "embedded", description: "Execution workers, workspaces, leases and capability state." },
  { id: "organization", label: "Organization / Roles", group: "Organization", kind: "embedded", description: "Organizations, workspaces, identities, sessions and Executive roles." },
  { id: "resources", label: "Resources", group: "Organization", kind: "embedded", description: "Canonical resources and relationships." },
  { id: "definitions", label: "Definitions / Contracts", group: "Organization", kind: "embedded", description: "Definition lifecycle, exact revisions, compatibility and usage." },
  { id: "settings", label: "Settings / Security", group: "Organization", kind: "embedded", description: "Configuration, entitlements, secret references, keys and trust diagnostics." },
  { id: "memory", label: "Memory", group: "Organization", kind: "launcher", selector: "#memory-button", description: "Governed organizational memory and retrieval." },
];


const PROJECT_NAVIGATION_TREE = [
  { id: "overview", label: "Overview", workspace: "overview", page: "overview", description: "Status and next actions for this Project." },
  {
    id: "work-group",
    label: "Work",
    children: [
      { id: "work-items", label: "Work Items", workspace: "work", page: "work-items", description: "Canonical tasks, blockers and ownership." },
      { id: "runs", label: "Runs / Execution", workspace: "work", page: "runs", description: "What executed, where, and with what result." },
      { id: "chat", label: "Chat / Threads", workspace: "threads", page: "chat", description: "Interactive agent work in this Project." },
      { id: "goals", label: "Goals", workspace: "goals" },
      { id: "decisions", label: "Decisions", workspace: "decisions" },
    ],
  },
  {
    id: "agents-group",
    label: "Agents",
    children: [
      { id: "agent-profiles", label: "Agent Profiles", workspace: "agents", page: "agents", description: "Stable agent identity and execution preferences." },
      { id: "teams", label: "Teams / Squads", workspace: "agents", page: "agents", description: "Bounded delegation between agent profiles." },
      { id: "skills", label: "Skills", workspace: "skills", description: "Versioned reusable procedures pinned to runs." },
    ],
  },
  {
    id: "automation-group",
    label: "Automation",
    children: [
      { id: "automations", label: "Automations", workspace: "autonomy", page: "automations", description: "Scheduled and event-driven governed work." },
      { id: "integrations", label: "Integrations / Extensions", workspace: "integrations", description: "External inputs, actions and extension capabilities." },
    ],
  },
  { id: "attention", label: "Attention", workspace: "inbox", page: "attention", description: "Human decisions and remediation only." },
  {
    id: "operations-group",
    label: "Operations",
    children: [
      { id: "runtime", label: "Runtimes / Workers", workspace: "workers", description: "Where executions run and their capabilities." },
      { id: "providers", label: "Providers", workspace: "operations", page: "operations", description: "Model/action provider availability and health." },
      { id: "incidents", label: "Incidents / Failures", workspace: "operations", page: "operations", description: "Operational failures and canonical remediation." },
    ],
  },
  { id: "project-settings", label: "Project Settings", workspace: "setup", page: "project-settings", description: "Topology, readiness and execution defaults." },
];

const GLOBAL_NAVIGATION = [
  {
    id: "administration",
    label: "Administration",
    administrationPage: "overview",
    description: "Organization-wide users, authentication, access and security administration.",
  },
];

const PROJECT_PAGE_PRESENTATION = Object.freeze({
  overview: {
    title: "Overview",
    purpose: "See the selected Project's current work, attention needs and execution health before choosing where to act.",
    scope: "Project",
  },
  "work-items": {
    title: "Work Items",
    purpose: "Plan, inspect and advance canonical units of work; execution history and blockers remain attached to each item.",
    scope: "Project",
  },
  runs: {
    title: "Runs / Execution",
    purpose: "Inspect what agents and workers actually executed, including routing, repository scope, evidence and outcomes.",
    scope: "Project execution",
  },
  chat: {
    title: "Chat / Threads",
    purpose: "Work interactively with an agent inside the selected Project; repository and execution policy still apply to every turn.",
    scope: "Project",
  },
  agents: {
    title: "Agents",
    purpose: "Manage reusable agent identities, teams and skills independently from the provider or runtime that executes them.",
    scope: "Project",
  },
  automations: {
    title: "Automations",
    purpose: "Define governed scheduled or event-driven work with explicit authority, budgets, retries and execution targets.",
    scope: "Project",
  },
  attention: {
    title: "Attention",
    purpose: "Handle the small set of approvals, decisions and remediations that genuinely require a person.",
    scope: "Project and assigned work",
  },
  operations: {
    title: "Operations",
    purpose: "Understand runtime, worker and provider health, failures and remediation without reading raw operational state.",
    scope: "Organization / workspace runtime",
  },
  "project-settings": {
    title: "Project Settings",
    purpose: "Configure Project topology, readiness and execution defaults; changes here can affect every future run in this Project.",
    scope: "Project",
  },
});

const PROJECT_PAGE_WORKSPACES = Object.freeze({
  overview: "overview",
  "work-items": "work",
  runs: "work",
  chat: "threads",
  agents: "agents",
  automations: "autonomy",
  attention: "inbox",
  operations: "operations",
  "project-settings": "setup",
});

const WORKSPACE_DEFAULT_PAGE = Object.freeze({
  overview: "overview",
  work: "work-items",
  threads: "chat",
  agents: "agents",
  autonomy: "automations",
  inbox: "attention",
  operations: "operations",
  workers: "operations",
  setup: "project-settings",
});

const CARD_RULES = [
  [/^Project Setup & Readiness$/i, "setup"],
  [/^Work Graph$/i, "work"],
  [/^Identity & Sessions$/i, "organization"],
  [/^Resource Catalog$/i, "resources"],
  [/^Definition Registry$/i, "definitions"],
  [/^Extensions$/i, "integrations"],
  [/^Input Plugin Pipeline$/i, "integrations"],
  [/^Action Providers$/i, "integrations"],
  [/^GitLab Routing$/i, "integrations"],
  [/^Agent Channel Presence$/i, "integrations"],
  [/^Model Gateway$/i, "agents"],
  [/^Agent Providers & Sessions$/i, "operations"],
  [/^Agent Profiles & Teams$/i, "agents"],
  [/^Skills$/i, "skills"],
  [/^Execution Workers$/i, "workers"],
  [/^Execution Workspaces & Leases$/i, "workers"],
  [/^Artifacts, Evidence & Verification$/i, "operations"],
  [/^Operations & Observability$/i, "operations"],
  [/^Structured Logs$/i, "operations"],
  [/^Autonomy Control Center$/i, "autonomy"],
  [/^Orchestration Inspector$/i, "autonomy"],
  [/^ActionIntents & Side Effects$/i, "autonomy"],
  [/^Entitlements & Usage$/i, "settings"],
  [/^Configuration & Features$/i, "settings"],
  [/^Secrets & Credentials$/i, "settings"],
  [/^Encryption Keys$/i, "settings"],
  [/^Security & Trust Diagnostics$/i, "settings"],
];

const DIRECT_ACTIONS = {
  work: [
    { label: "Work Items", event: "codex:open-work-items" },
  ],
  organization: [
    { label: "Executive roles", selector: "#executive-management-button" },
  ],
  integrations: [
    { label: "API browser", selector: "#swagger-browser-launch" },
  ],
};

const CONCEPTS = [
  ["definition", "Definition", "What behavior/contract revision exists."],
  ["policy", "Authorization / policy", "Whether this identity/role may act."],
  ["configuration", "Configuration / rollout", "Whether behavior is configured or enabled here."],
  ["entitlement", "Entitlement", "Whether the tenant is allowed to use the capability."],
  ["quota", "Budget / quota", "Whether bounded usage remains available."],
  ["compatibility", "Compatibility / version skew", "Whether versions/contracts can interoperate."],
  ["health", "Health / capability", "Whether the provider, worker or extension can serve the request."],
  ["evidence", "Evidence / verification", "Whether required proof and verification exist."],
];

const EXPLAIN_STAGES = [
  "Trigger / source",
  "Identity / assurance",
  "Owner / role",
  "Definition revision",
  "Model / agent routing",
  "Policy / configuration / entitlement",
  "Target resource",
  "Execution contract / worker",
  "Approval / quorum",
  "ActionIntent / provider receipt",
  "Evidence / verification",
  "Result",
];

let activeWorkspace = "overview";
let activePage = "overview";

function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function conceptBadge(kind, label = null) {
  const match = CONCEPTS.find(([key]) => key === kind);
  const text = label || match?.[1] || kind;
  const span = document.createElement("span");
  span.className = `product-concept-badge concept-${kind}`;
  span.dataset.concept = kind;
  span.textContent = text;
  return span;
}

function statusBadge(status, label = null) {
  const span = sharedStatusBadge(status, label, { className: "product-status-badge" });
  span.classList.add(`status-${sharedStatusFamily(status)}`);
  return span;
}

function provenanceTrail(stages) {
  const root = document.createElement("ol");
  root.className = "product-provenance-trail";
  for (const stage of stages || []) {
    const item = document.createElement("li");
    const strong = document.createElement("strong");
    strong.textContent = stage.label || stage.stage || "Stage";
    item.appendChild(strong);
    if (stage.detail) {
      const small = document.createElement("small");
      small.textContent = stage.detail;
      item.appendChild(small);
    }
    root.appendChild(item);
  }
  return root;
}

function workspaceById(id) {
  return WORKSPACES.find((item) => item.id === id) || WORKSPACES[0];
}

function setHash(id) {
  const desired = `#workspace/${id}`;
  if (window.location.hash !== desired) window.location.hash = desired;
}

function currentProjectRoute() {
  const match = window.location.pathname.match(/^(.*)\/projects\/([^/]+)\/([^/]+)\/?$/);
  if (!match) return null;
  let projectId = match[2];
  try {
    projectId = decodeURIComponent(projectId);
  } catch {
    // Keep the raw route segment for deterministic recovery.
  }
  return {
    prefix: match[1] || "",
    projectId,
    page: match[3],
  };
}

async function administrationApi(path) {
  const route = currentAdministrationRoute();
  const prefix = route?.prefix
    ?? currentProjectRoute()?.prefix
    ?? (window.location.pathname.startsWith("/codex") ? "/codex" : "");
  const response = await fetch(`${prefix}${path}`, {
    headers: { Accept: "application/json" },
  });
  if (!response.ok) {
    let detail = null;
    try {
      const payload = await response.json();
      detail = payload?.detail || payload?.message || null;
    } catch {
      detail = null;
    }
    const error = new Error(detail || `Request failed (${response.status})`);
    error.status = response.status;
    throw error;
  }
  if (response.status === 204) return null;
  return response.json();
}

function administrationRoot() {
  return document.getElementById("product-administration-root");
}

function setAdministrationLocation(page = "overview", { replace = false } = {}) {
  const current = currentAdministrationRoute();
  const prefix = current?.prefix
    ?? currentProjectRoute()?.prefix
    ?? (window.location.pathname.startsWith("/codex") ? "/codex" : "");
  const url = new URL(window.location.href);
  url.pathname = administrationPath(page, { prefix });
  url.search = "";
  url.hash = "";
  const state = { ...history.state, administrationPage: page };
  if (replace) history.replaceState(state, "", url);
  else history.pushState(state, "", url);
  void renderAdministrationRoute(page);
}

async function renderAdministrationRoute(page = "overview") {
  const root = administrationRoot();
  if (!root) return;
  document.body.classList.add("product-administration-page");
  document.body.classList.remove("product-routed-project-page", "product-chat-page");
  document.body.dataset.administrationPage = page;
  delete document.body.dataset.projectPage;
  root.hidden = false;
  const main = document.querySelector(":scope > .main") || document.querySelector(".main");
  if (main) main.setAttribute("aria-hidden", "true");
  const dialog = document.getElementById("product-workspace-dialog");
  if (dialog?.open) dialog.close();

  document.querySelectorAll("[data-project-nav-node]").forEach((button) => {
    button.setAttribute(
      "aria-current",
      button.dataset.projectNavNode === "administration" ? "page" : "false",
    );
  });

  root.innerHTML = `
    <div class="workspace-state workspace-state-loading" role="status">
      Loading Administration…
    </div>
  `;
  try {
    const context = await loadAdministrationContext(administrationApi);
    renderAdministrationNavigation(root, {
      page,
      context,
      onNavigate: (nextPage) => setAdministrationLocation(nextPage),
    });
    if (context.allowed) {
      const presentation = administrationPresentation(page);
      const stateHost = root.querySelector("[data-administration-page-state]");
      if (stateHost) {
        stateHost.className = "workspace-state product-administration-page-placeholder";
        stateHost.innerHTML = `
          <strong>${esc(presentation.title)}</strong>
          <p>${esc(presentation.purpose)}</p>
          <small>Administration navigation and canonical scope are active. Domain-specific management is provided by the corresponding Administration child surface.</small>
        `;
      }
    }
  } catch (error) {
    root.innerHTML = `
      <section class="product-administration-shell">
        <div class="workspace-state workspace-state-error" role="alert">
          <strong>Administration unavailable</strong>
          <p>${esc(error?.message || "Unable to load canonical Administration context.")}</p>
        </div>
      </section>
    `;
  }
}

function leaveAdministrationMode() {
  document.body.classList.remove("product-administration-page");
  delete document.body.dataset.administrationPage;
  const root = administrationRoot();
  if (root) {
    root.hidden = true;
    root.innerHTML = "";
  }
}

function legacyStaticRoutingContext() {
  return (
    window.location.pathname.includes("/tests/")
    || window.location.pathname.endsWith("/static/index.html")
  );
}

function navigationProjectId() {
  return (
    currentProjectRoute()?.projectId
    || document.body?.dataset.activeProject
    || document.getElementById("product-project-switcher")?.value
    || new URLSearchParams(window.location.search).get("project")
    || "home"
  );
}

function setWorkspaceLocation(id, page = null, { replace = false } = {}) {
  const routePage = page || WORKSPACE_DEFAULT_PAGE[id];
  if (!routePage || legacyStaticRoutingContext()) {
    setHash(id);
    return;
  }
  const current = currentProjectRoute();
  const prefix = current?.prefix ?? (window.location.pathname.startsWith("/codex") ? "/codex" : "");
  const projectId = navigationProjectId();
  const url = new URL(window.location.href);
  url.pathname = `${prefix}/projects/${encodeURIComponent(projectId)}/${routePage}`;
  url.searchParams.delete("project");
  url.hash = "";
  const state = { ...history.state, projectId, projectPage: routePage };
  if (replace) history.replaceState(state, "", url);
  else history.pushState(state, "", url);
  if (document.body) document.body.dataset.projectPage = routePage;
  applyRoutedShellMode(routePage);
}

function applyRoutedShellMode(page) {
  if (!document.body || !page) return;
  leaveAdministrationMode();
  document.body.dataset.projectPage = page;
  document.body.classList.add("product-routed-project-page");
  document.body.classList.toggle("product-chat-page", page === "chat");
  const main = document.querySelector(":scope > .main") || document.querySelector(".main");
  if (main) main.setAttribute("aria-hidden", page === "chat" ? "false" : "true");
  const developer = document.getElementById("developer-panel");
  if (developer) developer.hidden = true;
}

function migrateLegacyEntry() {
  if (legacyStaticRoutingContext() || currentProjectRoute()) return false;
  const path = window.location.pathname.replace(/\/+$/, "") || "/";
  const root = path === "/" || path === "/codex";
  if (!root) return false;

  const legacy = window.location.hash.match(/^#workspace\/([a-z0-9-]+)$/);
  const workspaceId = legacy?.[1] || "overview";
  const page = WORKSPACE_DEFAULT_PAGE[workspaceId] || "overview";
  const projectId = navigationProjectId();
  if (!projectId) return false;

  setWorkspaceLocation(workspaceId, page, { replace: true });
  openWorkspace(workspaceId, { page, updateLocation: false });
  applyRoutedShellMode(page);
  return true;
}

function closeSwitcher() {
  const dialog = document.getElementById("product-workspace-switcher");
  if (dialog?.open) dialog.close();
}

function focusSidebar(selector) {
  const target = document.querySelector(selector);
  if (!target) return false;
  if (document.body.classList.contains("sidebar-collapsed")) {
    document.getElementById("sidebar-toggle")?.click();
  }
  closeSwitcher();
  target.scrollIntoView({ block: "center", behavior: "smooth" });
  if (typeof target.focus === "function") target.focus({ preventScroll: true });
  return true;
}

function launchExisting(selector) {
  const target = document.querySelector(selector);
  if (!target) return false;
  closeSwitcher();
  target.click();
  return true;
}

function workspaceHost(id) {
  return document.querySelector(`[data-product-workspace-host="${CSS.escape(id)}"]`);
}

function cardHeading(card) {
  const heading = card.querySelector("h2");
  if (!heading) return "";
  const clone = heading.cloneNode(true);
  clone.querySelectorAll(".context-help-link").forEach((node) => node.remove());
  return (clone.textContent || "").trim();
}

function adoptCard(card) {
  if (!(card instanceof HTMLElement)) return false;
  if (!card.classList.contains("developer-card")) return false;
  if (card.dataset.productWorkspace) return false;
  const heading = cardHeading(card);
  const rule = CARD_RULES.find(([pattern]) => pattern.test(heading));
  if (!rule) return false;
  const id = rule[1];
  const host = workspaceHost(id);
  if (!host) return false;
  card.dataset.productWorkspace = id;
  card.classList.add("product-adopted-card");
  host.appendChild(card);
  return true;
}

function relocateLegacyControlSource() {
  const developer = document.getElementById("developer-panel");
  if (!developer || developer.dataset.productCompatibilitySource === "true") return;
  developer.dataset.productCompatibilitySource = "true";
  developer.hidden = true;
  developer.setAttribute("aria-hidden", "true");
  document.body.appendChild(developer);
}

function adoptAll(root = document) {
  root.querySelectorAll?.(".developer-card").forEach(adoptCard);
}

function refreshWorkspaceCards(id) {
  const host = workspaceHost(id);
  if (!host) return;
  const selectors = [
    "button[id^='refresh-']",
    "button[data-acc-refresh]",
    "button[data-orchestration-refresh]",
    "button[data-refresh]",
  ];
  const clicked = new Set();
  host.querySelectorAll(selectors.join(",")).forEach((button) => {
    if (!(button instanceof HTMLButtonElement) || button.disabled || clicked.has(button)) return;
    clicked.add(button);
    button.click();
  });
}

function syncNavigationState(id, page = null) {
  document.querySelectorAll("[data-product-workspace-nav]").forEach((button) => {
    const buttonPage = button.dataset.projectPage || "";
    const selected = button.dataset.productWorkspaceNav === id
      && (!page || !buttonPage || buttonPage === page);
    button.classList.toggle("active", selected);
    button.setAttribute("aria-current", selected ? "page" : "false");
    if (selected) button.closest("details[data-project-nav-group]")?.setAttribute("open", "");
  });
}

function setActiveInternal(id, { updateLocation = true, page = null } = {}) {
  activeWorkspace = id;
  activePage = page || WORKSPACE_DEFAULT_PAGE[id] || id;
  document.querySelectorAll("[data-product-workspace-panel]").forEach((panel) => {
    panel.hidden = panel.dataset.productWorkspacePanel !== id;
  });
  syncNavigationState(id, activePage);
  const item = workspaceById(id);
  const presentation = PROJECT_PAGE_PRESENTATION[activePage];
  const title = document.querySelector("[data-product-workspace-title]");
  const description = document.querySelector("[data-product-workspace-description]");
  const scope = document.querySelector("[data-project-page-scope]");
  if (title) title.textContent = presentation?.title || item.label;
  if (description) description.textContent = presentation?.purpose || item.description;
  if (scope) scope.textContent = presentation?.scope ? `Scope: ${presentation.scope}` : "";
  renderWorkspaceActions(id);
  refreshWorkspaceCards(id);
  if (id === "overview") {
    const host = document.querySelector("[data-home-overview]");
    if (host) void renderHomeOverview(host);
  }
  if (updateLocation) setWorkspaceLocation(id, activePage);
}

function renderWorkspaceActions(id) {
  const host = document.querySelector("[data-product-workspace-actions]");
  if (!host) return;
  host.innerHTML = "";
  for (const action of DIRECT_ACTIONS[id] || []) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "ghost-button";
    button.textContent = action.label;
    button.addEventListener("click", () => {
      if (action.event) {
        window.dispatchEvent(new CustomEvent(action.event));
        return;
      }
      launchExisting(action.selector);
    });
    host.appendChild(button);
  }
  if (id === "autonomy") {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "ghost-button";
    button.textContent = "Explain an action";
    button.addEventListener("click", () => {
      const input = document.querySelector("[data-acc-intent]");
      input?.focus();
      input?.scrollIntoView({ block: "center", behavior: "smooth" });
    });
    host.appendChild(button);
  }
}

function openInternalWorkspace(id, { page = null, updateLocation = true } = {}) {
  const dialog = document.getElementById("product-workspace-dialog");
  if (!dialog) return false;
  closeSwitcher();
  setActiveInternal(id, { page, updateLocation });
  if (!dialog.open) {
    if (currentProjectRoute()) dialog.show();
    else dialog.showModal();
  }
  return true;
}

function closeInternalWorkspace() {
  const dialog = document.getElementById("product-workspace-dialog");
  if (dialog?.open) dialog.close();
}

function openWorkspace(id, { page = null, updateLocation = true } = {}) {
  const item = workspaceById(id);
  const resolvedPage = page || WORKSPACE_DEFAULT_PAGE[item.id] || item.id;
  activePage = resolvedPage;
  if (document.body) document.body.dataset.projectPage = resolvedPage;
  if (currentProjectRoute() || !legacyStaticRoutingContext()) {
    applyRoutedShellMode(resolvedPage);
  }
  if (item.kind === "launcher") {
    closeInternalWorkspace();
    activeWorkspace = item.id;
    syncNavigationState(item.id, resolvedPage);
    if (updateLocation) setWorkspaceLocation(item.id, resolvedPage);
    return launchExisting(item.selector);
  }
  if (item.kind === "focus") {
    closeInternalWorkspace();
    activeWorkspace = item.id;
    syncNavigationState(item.id, resolvedPage);
    if (updateLocation) setWorkspaceLocation(item.id, resolvedPage);
    return focusSidebar(item.selector);
  }
  return openInternalWorkspace(item.id, { page: resolvedPage, updateLocation });
}

function overviewMarkup() {
  return `
    <div class="home-overview-shell" data-home-overview>
      <div class="workspace-state workspace-state-loading" role="status">Loading current workspace…</div>
    </div>
    <details class="product-overview-reference">
      <summary>UI vocabulary and explainability reference</summary>
      <div class="product-overview-grid">
      <section class="product-overview-card">
        <h3>Canonical UI vocabulary</h3>
        <p>Unavailable actions should say <em>why</em>. These concepts remain distinct rather than collapsing into generic settings or “not allowed”.</p>
        <div class="product-concept-grid">
          ${CONCEPTS.map(([kind, label, detail]) => `
            <div class="product-concept-row" data-concept-row="${esc(kind)}">
              <span class="product-concept-badge concept-${esc(kind)}" data-concept="${esc(kind)}">${esc(label)}</span>
              <small>${esc(detail)}</small>
            </div>`).join("")}
        </div>
      </section>
      <section class="product-overview-card">
        <h3>Shared explainability path</h3>
        <p>Canonical action explanations should trace stored state end-to-end; refresh and navigation do not require a model call.</p>
        <ol class="product-provenance-trail">
          ${EXPLAIN_STAGES.map((stage) => `<li><strong>${esc(stage)}</strong></li>`).join("")}
        </ol>
        <div class="product-explain-shortcut">
          <input data-product-explain-id placeholder="action-intent-…" aria-label="ActionIntent ID">
          <button type="button" class="ghost-button" data-product-explain>Open Explain Action</button>
        </div>
      </section>
      <section class="product-overview-card product-overview-wide">
        <h3>Scope & sensitive references</h3>
        <p>Tenant/workspace scope, identity assurance and canonical references stay visible. Secret and cryptographic key material must never be rendered; UI surfaces only references and metadata.</p>
        <div class="product-status-examples">
          <span class="product-status-badge status-positive" data-status="current">current</span>
          <span class="product-status-badge status-warning" data-status="partial">partial</span>
          <span class="product-status-badge status-negative" data-status="blocked">blocked</span>
          <span class="product-status-badge status-neutral" data-status="unknown">unknown</span>
        </div>
      </section>
      </div>
    </details>`;
}

function buildPanels() {
  const root = document.querySelector("[data-product-workspace-panels]");
  if (!root) return;
  for (const workspace of WORKSPACES.filter((item) => item.kind === "embedded")) {
    const panel = document.createElement("section");
    panel.className = "product-workspace-panel";
    panel.dataset.productWorkspacePanel = workspace.id;
    panel.hidden = workspace.id !== "overview";
    if (workspace.id === "overview") {
      panel.innerHTML = overviewMarkup();
    } else {
      panel.innerHTML = `
        <div class="product-workspace-host" data-product-workspace-host="${esc(workspace.id)}">
          <div class="product-workspace-empty" data-product-workspace-empty>
            Canonical ${esc(workspace.label)} surfaces will appear here as their modules load.
          </div>
        </div>`;
    }
    root.appendChild(panel);
  }
  root.querySelector("[data-product-explain]")?.addEventListener("click", () => {
    const value = root.querySelector("[data-product-explain-id]")?.value?.trim() || "";
    openInternalWorkspace("autonomy");
    queueMicrotask(() => {
      const input = document.querySelector("[data-acc-intent]");
      if (input && value) {
        input.value = value;
        input.dispatchEvent(new Event("input", { bubbles: true }));
      }
      input?.focus();
      if (value) document.querySelector("[data-acc-explain]")?.click();
    });
  });
}

function updateEmptyStates() {
  document.querySelectorAll(".product-workspace-host").forEach((host) => {
    const empty = host.querySelector(":scope > .product-workspace-empty");
    const hasCard = Boolean(host.querySelector(":scope > .developer-card"));
    if (empty) empty.hidden = hasCard;
  });
}


function navigationLeaf(item) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "product-project-nav-leaf";
  button.dataset.projectNavNode = item.id;
  if (item.workspace) button.dataset.productWorkspaceNav = item.workspace;
  if (item.page) button.dataset.projectPage = item.page;
  if (item.administrationPage) button.dataset.administrationPage = item.administrationPage;
  const label = document.createElement("strong");
  label.textContent = item.label;
  button.appendChild(label);
  if (item.description) {
    const help = document.createElement("small");
    help.className = "product-project-nav-help";
    help.textContent = item.description;
    button.appendChild(help);
  }
  button.title = item.description || item.label;
  if (item.administrationPage) {
    button.addEventListener("click", () => setAdministrationLocation(item.administrationPage));
  } else {
    button.addEventListener("click", () => openWorkspace(item.workspace, { page: item.page || null }));
  }
  return button;
}

function buildProjectNavigation(container) {
  container.innerHTML = "";
  const projectTree = document.createElement("nav");
  projectTree.className = "product-project-nav-tree";
  projectTree.setAttribute("aria-label", "Project navigation");

  for (const item of PROJECT_NAVIGATION_TREE) {
    if (!item.children) {
      projectTree.appendChild(navigationLeaf(item));
      continue;
    }
    const group = document.createElement("details");
    group.dataset.projectNavGroup = item.id;
    group.className = "product-project-nav-group";
    group.open = ["work-group", "agents-group"].includes(item.id);
    const summary = document.createElement("summary");
    summary.textContent = item.label;
    summary.setAttribute("aria-label", item.label);
    group.appendChild(summary);
    const children = document.createElement("div");
    children.className = "product-project-nav-children";
    item.children.forEach((child) => children.appendChild(navigationLeaf(child)));
    group.appendChild(children);
    projectTree.appendChild(group);
  }
  container.appendChild(projectTree);

  const global = document.createElement("nav");
  global.className = "product-global-nav";
  global.setAttribute("aria-label", "Global navigation");
  const heading = document.createElement("span");
  heading.className = "product-global-nav-label";
  heading.textContent = "Global";
  global.appendChild(heading);
  GLOBAL_NAVIGATION.forEach((item) => {
    const button = navigationLeaf(item);
    button.classList.add("product-global-nav-leaf");
    global.appendChild(button);
  });
  container.appendChild(global);
}

function buildSwitcherNav(container) {
  const groups = [...new Set(WORKSPACES.map((item) => item.group))];
  for (const group of groups) {
    const section = document.createElement("section");
    section.className = "product-workspace-nav-group";
    const heading = document.createElement("h3");
    heading.textContent = group;
    section.appendChild(heading);
    const grid = document.createElement("div");
    grid.className = "product-workspace-nav-grid";
    for (const workspace of WORKSPACES.filter((item) => item.group === group)) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "product-workspace-nav-item";
      button.dataset.productWorkspaceNav = workspace.id;
      button.innerHTML = `<strong>${esc(workspace.label)}</strong><small>${esc(workspace.description)}</small>`;
      button.dataset.workspaceSearch = `${workspace.label} ${workspace.group} ${workspace.description}`.toLowerCase();
      button.addEventListener("click", () => openWorkspace(workspace.id));
      grid.appendChild(button);
    }
    section.appendChild(grid);
    container.appendChild(section);
  }
}

function buildShell() {
  if (document.getElementById("product-workspace-dialog")) return;

  const administration = document.createElement("main");
  administration.id = "product-administration-root";
  administration.className = "product-administration-root";
  administration.hidden = true;
  administration.setAttribute("aria-label", "Administration");
  document.body.appendChild(administration);

  const sidebar = document.querySelector(".sidebar");
  const brand = sidebar?.querySelector(".brand");
  if (sidebar && brand) {
    const projectContext = document.createElement("div");
    projectContext.className = "product-project-context";
    projectContext.innerHTML = `
      <label for="product-project-switcher">Current Project</label>
      <select id="product-project-switcher" aria-label="Current Project">
        <option value="">Loading projects…</option>
      </select>
      <small class="product-field-help">Sets the Project scope for navigation, work, repository targets and new executions.</small>
      <div class="product-project-context-state" data-project-context-state role="status" hidden></div>
    `;
    brand.insertAdjacentElement("afterend", projectContext);

    const navigation = document.createElement("div");
    navigation.className = "product-project-navigation";
    buildProjectNavigation(navigation);
    const all = document.createElement("button");
    all.type = "button";
    all.className = "ghost-button product-all-workspaces";
    all.dataset.workspaceSwitcherLaunch = "true";
    all.textContent = "All workspaces";
    navigation.appendChild(all);
    projectContext.insertAdjacentElement("afterend", navigation);
  }

  const switcher = document.createElement("dialog");
  switcher.id = "product-workspace-switcher";
  switcher.className = "product-workspace-switcher";
  switcher.setAttribute("aria-labelledby", "product-workspace-switcher-title");
  switcher.innerHTML = `
    <div class="product-switcher-shell">
      <header>
        <div><h2 id="product-workspace-switcher-title">Navigate</h2><p>Search workflows and canonical workspaces without leaving the active Project.</p></div>
        <button type="button" class="icon-button" data-workspace-switcher-close aria-label="Close navigation">×</button>
      </header>
      <div class="product-command-search">
        <label for="product-workspace-search">Search workspaces</label>
        <input id="product-workspace-search" type="search" autocomplete="off" placeholder="Work, Team, Automation, Operations…" />
      </div>
      <div class="product-workspace-nav-groups" data-product-workspace-nav-groups></div>
      <footer><small>Shortcut: Ctrl/⌘ K · navigation and Project switching do not invoke a model.</small></footer>
    </div>`;
  document.body.appendChild(switcher);
  buildSwitcherNav(switcher.querySelector("[data-product-workspace-nav-groups]"));
  switcher.querySelector("[data-workspace-switcher-close]").addEventListener("click", () => switcher.close());
  const workspaceSearch = switcher.querySelector("#product-workspace-search");
  workspaceSearch?.addEventListener("input", () => {
    const needle = workspaceSearch.value.trim().toLowerCase();
    switcher.querySelectorAll(".product-workspace-nav-item").forEach((button) => {
      button.hidden = Boolean(needle) && !button.dataset.workspaceSearch.includes(needle);
    });
    switcher.querySelectorAll(".product-workspace-nav-group").forEach((group) => {
      group.hidden = !group.querySelector(".product-workspace-nav-item:not([hidden])");
    });
  });

  const dialog = document.createElement("dialog");
  dialog.id = "product-workspace-dialog";
  dialog.className = "product-workspace-dialog";
  dialog.setAttribute("aria-labelledby", "product-workspace-dialog-title");
  dialog.innerHTML = `
    <div class="product-workspace-shell">
      <header class="product-workspace-header">
        <div>
          <small data-project-page-scope>Scope: Project</small>
          <h2 id="product-workspace-dialog-title" data-product-workspace-title>Overview</h2>
          <p data-product-workspace-description></p>
        </div>
        <div class="product-workspace-header-actions">
          <div data-product-workspace-actions></div>
          <button type="button" class="ghost-button" data-open-workspace-switcher>Switch workspace</button>
          <button type="button" class="icon-button" data-product-workspace-close aria-label="Close workspace">×</button>
        </div>
      </header>
      <div class="product-workspace-panels" data-product-workspace-panels></div>
    </div>`;
  document.body.appendChild(dialog);
  buildPanels();
  dialog.querySelector("[data-product-workspace-close]").addEventListener("click", () => dialog.close());
  dialog.querySelector("[data-open-workspace-switcher]").addEventListener("click", () => {
    if (!switcher.open) switcher.showModal();
  });

  document.querySelectorAll("[data-workspace-switcher-launch]").forEach((button) => {
    button.addEventListener("click", () => {
      if (!switcher.open) switcher.showModal();
    });
  });

  const topbar = document.querySelector(".topbar .controls");
  if (topbar) {
    const projectIndicator = document.createElement("button");
    projectIndicator.type = "button";
    projectIndicator.className = "ghost-button product-project-indicator";
    projectIndicator.dataset.projectIndicator = "true";
    projectIndicator.textContent = "Project: loading…";
    projectIndicator.setAttribute("aria-label", "Current Project");
    projectIndicator.addEventListener("click", () => {
      const select = document.getElementById("product-project-switcher");
      if (document.body.classList.contains("sidebar-collapsed")) {
        document.getElementById("sidebar-toggle")?.click();
      }
      select?.focus();
    });
    topbar.prepend(projectIndicator);

    const launch = document.createElement("button");
    launch.type = "button";
    launch.className = "ghost-button product-workspaces-launch";
    launch.dataset.workspaceSwitcherLaunch = "topbar";
    launch.textContent = "Workspaces";
    launch.title = "Open product workspaces (Ctrl/⌘ K)";
    launch.addEventListener("click", () => {
      if (!switcher.open) switcher.showModal();
    });
    topbar.prepend(launch);
  }

  installProjectContext();
  setActiveInternal("overview", { updateLocation: false, page: "overview" });
}

function projectLabel(project) {
  return project?.name || project?.path || project?.id || "Unknown Project";
}

function renderProjectContext({ projects = [], projectId = "" } = {}) {
  const select = document.getElementById("product-project-switcher");
  const indicator = document.querySelector("[data-project-indicator]");
  if (!select) return;
  const current = projectId || select.value || new URLSearchParams(window.location.search).get("project") || "";
  select.innerHTML = "";
  for (const project of projects) {
    const option = document.createElement("option");
    option.value = project.id;
    option.textContent = projectLabel(project);
    option.dataset.path = project.path || "";
    select.appendChild(option);
  }
  if (current && !projects.some((project) => project.id === current)) {
    const option = document.createElement("option");
    option.value = current;
    option.textContent = current;
    select.appendChild(option);
  }
  if (current) select.value = current;
  const active = projects.find((project) => project.id === select.value);
  const label = active ? projectLabel(active) : (select.value || "No Project");
  if (indicator) indicator.textContent = `Project: ${label}`;
  document.body.dataset.activeProject = select.value || "";
}

function setProjectContextUnavailable({ projectId = "", projects = [] } = {}) {
  const select = document.getElementById("product-project-switcher");
  const indicator = document.querySelector("[data-project-indicator]");
  const status = document.querySelector("[data-project-context-state]");
  if (select) {
    select.innerHTML = "";
    for (const project of projects) {
      const option = document.createElement("option");
      option.value = project.id;
      option.textContent = projectLabel(project);
      select.appendChild(option);
    }
    select.selectedIndex = -1;
  }
  if (indicator) indicator.textContent = `Project unavailable: ${projectId || "unknown"}`;
  if (status) {
    status.hidden = false;
    status.textContent = projects.length
      ? "This Project is unavailable or you no longer have access. Choose an available Project above to recover."
      : "This Project is unavailable or you no longer have access. No alternative Project is currently available.";
  }
  document.body.classList.add("project-context-unavailable");
  document.body.dataset.activeProject = "";
  document.querySelectorAll(".product-project-nav-tree [data-project-nav-node]").forEach((button) => {
    button.disabled = true;
  });
}

function clearProjectContextUnavailable() {
  document.body.classList.remove("project-context-unavailable");
  const status = document.querySelector("[data-project-context-state]");
  if (status) {
    status.hidden = true;
    status.textContent = "";
  }
  document.querySelectorAll(".product-project-nav-tree [data-project-nav-node]").forEach((button) => {
    button.disabled = false;
  });
}

function installProjectContext() {
  const select = document.getElementById("product-project-switcher");
  if (!select) return;
  select.addEventListener("change", () => {
    if (!select.value) return;
    window.dispatchEvent(new CustomEvent("codex:project-select", {
      detail: { projectId: select.value },
    }));
  });
  window.addEventListener("codex:projects-rendered", (event) => {
    const detail = event.detail || {};
    const routedProjectId = currentProjectRoute()?.projectId || "";
    const effectiveProjectId = routedProjectId || detail.projectId || "";
    renderProjectContext({ ...detail, projectId: effectiveProjectId });
    if (routedProjectId && detail.projectId && routedProjectId !== detail.projectId) {
      window.dispatchEvent(new CustomEvent("codex:project-select", {
        detail: { projectId: routedProjectId },
      }));
    }
    migrateLegacyEntry();
  });
  window.addEventListener("codex:project-context-unavailable", (event) => {
    setProjectContextUnavailable(event.detail || {});
  });
  window.addEventListener("codex:project-changed", (event) => {
    const projectId = event.detail?.projectId || "";
    if (projectId) {
      clearProjectContextUnavailable();
      select.value = projectId;
      document.body.dataset.activeProject = projectId;
      const option = select.selectedOptions[0];
      const indicator = document.querySelector("[data-project-indicator]");
      if (indicator) indicator.textContent = `Project: ${option?.textContent || projectId}`;
      const route = currentProjectRoute();
      if (route && route.projectId !== projectId) {
        const url = new URL(window.location.href);
        url.pathname = `${route.prefix}/projects/${encodeURIComponent(projectId)}/${route.page}`;
        url.searchParams.delete("project");
        history.pushState(
          { ...history.state, projectId, projectPage: route.page },
          "",
          url,
        );
      }
      if (activeWorkspace === "overview") {
        const host = document.querySelector("[data-home-overview]");
        if (host) void renderHomeOverview(host, projectId);
      }
    }
  });
}

function installObservers() {
  const observer = new MutationObserver((records) => {
    let changed = false;
    for (const record of records) {
      for (const node of record.addedNodes) {
        if (!(node instanceof Element)) continue;
        if (node.matches?.(".developer-card")) changed = adoptCard(node) || changed;
        node.querySelectorAll?.(".developer-card").forEach((card) => {
          changed = adoptCard(card) || changed;
        });
      }
    }
    if (changed) updateEmptyStates();
  });
  observer.observe(document.body, { childList: true, subtree: true });
}

function installKeyboard() {
  document.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      const switcher = document.getElementById("product-workspace-switcher");
      if (switcher && !switcher.open) {
        switcher.showModal();
        const search = switcher.querySelector("#product-workspace-search");
        if (search) {
          search.value = "";
          search.dispatchEvent(new Event("input", { bubbles: true }));
          search.focus();
        } else {
          switcher.querySelector(".product-workspace-nav-item")?.focus();
        }
      }
    }
  });
}

async function syncAdministrationEntryVisibility() {
  if (legacyStaticRoutingContext()) return;
  const entry = document.querySelector('[data-project-nav-node="administration"]');
  if (!entry) return;
  try {
    const context = await loadAdministrationContext(administrationApi);
    entry.hidden = !context.allowed;
    entry.disabled = !context.allowed;
  } catch {
    entry.hidden = true;
    entry.disabled = true;
  }
}

function installRouting() {
  const route = () => {
    const administrationRoute = currentAdministrationRoute();
    if (administrationRoute) {
      void renderAdministrationRoute(administrationRoute.page);
      return;
    }

    const projectRoute = currentProjectRoute();
    if (projectRoute) {
      const workspaceId = PROJECT_PAGE_WORKSPACES[projectRoute.page];
      if (!workspaceId) return;
      if (document.body) document.body.dataset.projectPage = projectRoute.page;
      applyRoutedShellMode(projectRoute.page);
      if (workspaceId !== activeWorkspace || projectRoute.page !== activePage) {
        openWorkspace(workspaceId, {
          page: projectRoute.page,
          updateLocation: false,
        });
      } else {
        syncNavigationState(workspaceId, projectRoute.page);
      }
      return;
    }

    const match = window.location.hash.match(/^#workspace\/([a-z0-9-]+)$/);
    if (!match) return;
    const item = WORKSPACES.find((workspace) => workspace.id === match[1]);
    if (item && item.id !== activeWorkspace) {
      openWorkspace(item.id, { updateLocation: false });
    }
  };
  window.addEventListener("hashchange", route);
  window.addEventListener("popstate", route);
  route();
}

function install() {
  buildShell();
  relocateLegacyControlSource();
  adoptAll(document);
  updateEmptyStates();
  installObservers();
  installKeyboard();
  installRouting();
  void syncAdministrationEntryVisibility();

  window.CodexProductUI = Object.freeze({
    openWorkspace,
    conceptBadge,
    statusBadge,
    provenanceTrail,
    concepts: Object.freeze(CONCEPTS.map(([key, label]) => ({ key, label }))),
    explainStages: Object.freeze([...EXPLAIN_STAGES]),
  });
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", install, { once: true });
} else {
  install();
}
