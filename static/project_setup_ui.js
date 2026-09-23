const BASE = window.location.pathname.startsWith("/codex") ? "/codex" : "";
const state = { projectId: "", project: null, resources: [], readiness: null, bootstrap: null, plan: null, error: null };

function esc(value) {
  return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
}
async function api(path, options = {}) {
  const response = await fetch(BASE + path, { headers: { "Content-Type": "application/json", ...(options.headers || {}) }, ...options });
  const text = await response.text();
  let payload = null;
  try { payload = text ? JSON.parse(text) : null; } catch { payload = text; }
  if (!response.ok) {
    const detail = payload?.detail;
    const error = new Error(typeof detail === "string" ? detail : detail?.message || payload?.message || ("HTTP " + response.status));
    error.status = response.status;
    throw error;
  }
  return payload;
}
function activeProjectId() {
  return document.body?.dataset.projectId || new URLSearchParams(location.search).get("project") || sessionStorage.getItem("codex-web-active-project") || "home";
}
function blocked() { return Boolean(state.readiness && state.readiness.execution_ready === false); }
function badge(status) {
  const value = String(status || "unknown").toLowerCase();
  const family = ["ready","active","applied","not_applicable"].includes(value) ? "positive" : ["warning","partial","planned","applying"].includes(value) ? "warning" : ["blocked","failed","unavailable"].includes(value) ? "negative" : "neutral";
  return '<span class="project-setup-badge project-setup-' + family + '">' + esc(value) + '</span>';
}
function inferredManifest() {
  if (!state.project) return null;
  return {
    apiVersion: "codex-web/v1",
    kind: "ProjectBootstrap",
    project: {
      name: state.project.name,
      organization: state.project.organization_id || "local",
      workspace: state.project.workspace_id || "default"
    },
    repositories: (state.resources || []).map((resource, index) => {
      const filesystem = (resource.aliases || []).find((alias) => alias.namespace === "filesystem");
      return {
        id: resource.id || ("repository-" + (index + 1)),
        path: filesystem?.value || resource.path || "",
        default: (state.resources || []).length === 1
      };
    }).filter((item) => item.path),
    execution: {
      repositorySelection: state.project.repository_selection_policy === "coordinated"
        ? "coordinated"
        : ((state.resources || []).length === 1 ? "single" : "explicit"),
      requiredCapabilities: ["command_execution"],
      sandbox: state.project.sandbox || "workspace-write"
    }
  };
}
function gateExecution() {
  const value = blocked();
  for (const id of ["new-thread", "send", "prompt"]) {
    const element = document.getElementById(id);
    if (!element) continue;
    element.disabled = value;
    element.setAttribute("aria-disabled", String(value));
    if (value) element.title = "Project readiness is blocked. Open Project Setup for remediation.";
  }
}
function setPanel(name) {
  document.querySelectorAll("[data-setup-panel]").forEach((node) => { node.hidden = node.dataset.setupPanel !== name; });
  document.querySelectorAll("[data-setup-tab]").forEach((node) => node.classList.toggle("active", node.dataset.setupTab === name));
}
function manifestInput() {
  const raw = document.querySelector("[data-project-setup-manifest]")?.value?.trim();
  if (!raw) return inferredManifest();
  try { return JSON.parse(raw); } catch { throw new Error("Manifest editor accepts normalized JSON; YAML remains supported by the CLI."); }
}
function remediation(route) {
  if (!route) return;
  if (route.startsWith("/api/secrets")) return window.CodexProductUI?.openWorkspace?.("settings");
  if (route.includes("/resources")) return window.CodexProductUI?.openWorkspace?.("resources");
  if (route.includes("execution-workers")) return window.CodexProductUI?.openWorkspace?.("workers");
  if (route.includes("/bootstrap/")) return setPanel("plan");
  location.hash = "#setup/route/" + encodeURIComponent(route);
}
function checkHtml(check) {
  const button = check.remediation_route ? '<button type="button" class="ghost-button" data-setup-route="' + esc(check.remediation_route) + '">Open remediation</button>' : "";
  return '<article class="project-setup-check" data-check-id="' + esc(check.id) + '"><header><div><strong>' + esc(check.domain || check.id) + '</strong><code>' + esc(check.code || "") + '</code></div>' + badge(check.status) + '</header><p>' + esc(check.message || "") + '</p>' + (check.remediation ? '<small>' + esc(check.remediation) + '</small>' : "") + button + '</article>';
}
function operationHtml(op) {
  return '<article class="project-setup-operation"><header><strong>' + esc(op.domain || "operation") + '</strong>' + badge(op.disposition) + '</header><code>' + esc(op.reason_code || "") + '</code><p>' + esc(op.message || "") + '</p>' + (op.operator_action_required ? "<small>Operator approval required</small>" : "") + '</article>';
}
function render() {
  const body = document.querySelector("[data-project-setup-body]");
  if (!body) return;
  const checks = Array.isArray(state.readiness?.checks) ? [...state.readiness.checks] : [];
  const order = { blocked: 0, warning: 1, ready: 2, not_applicable: 3 };
  checks.sort((a,b) => (order[a.status] ?? 9) - (order[b.status] ?? 9));
  const latest = state.bootstrap?.items?.[0] || null;
  const plan = state.plan?.plan || null;
  const blockers = checks.filter((item) => item.status === "blocked");
  const targetRequiredPerTurn = checks.some((item) => (
    item.code === "repository_target_required_per_turn"
  ));
  const repositoryPolicy = state.project?.repository_selection_policy || "deterministic";
  const readinessSummary = state.readiness?.execution_ready
    ? (
        targetRequiredPerTurn
          ? "Project is ready. Repository target required per turn."
          : "Canonical topology and execution prerequisites are ready."
      )
    : "Execution remains gated until required readiness checks pass.";
  const latestOps = Array.isArray(latest?.plan?.operations) ? latest.plan.operations : [];
  const planOps = Array.isArray(plan?.operations) ? plan.operations : [];
  body.innerHTML =
    '<section class="project-setup-summary"><div class="project-setup-hero"><div><small>Project readiness</small><h3>' + esc(state.project?.name || state.projectId) + '</h3><p>' +
    esc(readinessSummary) +
    '</p></div>' + badge(state.readiness?.status) + '</div><div class="project-setup-metrics"><div><span>Semantic</span><strong>' + (state.readiness?.semantic_ready ? "Ready" : "Blocked") +
    '</strong></div><div><span>Execution</span><strong>' + (state.readiness?.execution_ready ? "Ready" : "Blocked") +
    '</strong></div><div><span>Repository policy</span><strong>' + esc(repositoryPolicy === "explicit" ? "Explicit per turn" : repositoryPolicy === "coordinated" ? "Coordinated Project set" : "Deterministic") +
    '</strong></div><div><span>Blockers</span><strong>' + blockers.length + '</strong></div></div>' +
    (state.error ? '<div class="project-setup-error" role="alert">' + esc(state.error) + '</div>' : "") +
    (blocked() ? '<button type="button" class="ghost-button" data-setup-fresh>Retry safe Project setup</button>' : "") + '</section>' +
    '<nav class="project-setup-tabs"><button type="button" class="active" data-setup-tab="readiness">Readiness</button><button type="button" data-setup-tab="bootstrap">Bootstrap history</button><button type="button" data-setup-tab="plan">Plan / apply</button></nav>' +
    '<section data-setup-panel="readiness"><div class="project-setup-checks">' + (checks.length ? checks.map(checkHtml).join("") : '<div class="project-setup-empty">Readiness checks unavailable.</div>') + '</div></section>' +
    '<section data-setup-panel="bootstrap" hidden>' + (latest ? '<div class="project-setup-bootstrap-meta"><div><span>Execution</span><code>' + esc(latest.id) + '</code></div><div><span>Plan</span><code>' + esc(latest.plan_id || latest.plan?.id || "") + '</code></div><div><span>Status</span>' + badge(latest.status) + '</div></div><div class="project-setup-operations">' + latestOps.map(operationHtml).join("") + '</div>' : '<div class="project-setup-empty">No durable ProjectBootstrap execution is recorded.</div>') + '</section>' +
    '<section data-setup-panel="plan" hidden><div class="project-setup-toolbar"><button type="button" class="ghost-button" data-setup-preflight>Run preflight</button><button type="button" class="ghost-button" data-setup-plan>Build plan</button><label><input type="checkbox" data-setup-approve> Approve material authority changes</label><button type="button" class="primary-button" data-setup-apply ' + (!plan || state.plan?.blocked ? "disabled" : "") + '>Apply reviewed plan</button></div>' +
    (plan ? '<div class="project-setup-plan-summary"><code>' + esc(plan.id) + '</code></div><div class="project-setup-operations">' + planOps.map(operationHtml).join("") + '</div>' : '<div class="project-setup-empty">Build a deterministic plan before applying changes.</div>') +
    '<details class="project-setup-manifest-panel"><summary>Advanced: normalized desired state</summary><p>References only. Never place raw credentials here.</p><textarea data-project-setup-manifest rows="16" spellcheck="false"></textarea><button type="button" class="ghost-button" data-setup-reset>Reset from Project</button></details></section>';
  const textarea = body.querySelector("[data-project-setup-manifest]");
  if (textarea && inferredManifest()) textarea.value = JSON.stringify(inferredManifest(), null, 2);
  wire(body);
  gateExecution();
  const launch = document.getElementById("project-setup-launch");
  if (launch) {
    launch.textContent = state.readiness?.execution_ready ? "Project ready" : "Project setup";
    launch.classList.toggle("project-setup-blocked", blocked());
    launch.dataset.status = state.readiness?.status || "unknown";
  }
}
async function planning(kind) {
  try {
    state.error = null;
    const result = await api("/api/projects/" + encodeURIComponent(state.projectId) + "/bootstrap/" + kind, { method: "POST", body: JSON.stringify({ manifest: manifestInput(), migrate_legacy: true }) });
    state.plan = kind === "plan" ? result : { preflight: result.preflight, blocked: result.blocked, plan: null };
  } catch (error) { state.error = error.message; }
  render(); setPanel("plan");
}
async function applyPlan() {
  const plan = state.plan?.plan;
  if (!plan?.id) return;
  try {
    state.error = null;
    await api("/api/projects/" + encodeURIComponent(state.projectId) + "/bootstrap/apply", { method: "POST", body: JSON.stringify({ manifest: manifestInput(), migrate_legacy: true, expected_plan_id: plan.id, approve_authority_changes: Boolean(document.querySelector("[data-setup-approve]")?.checked) }) });
    state.plan = null;
    await refresh();
    setPanel("readiness");
  } catch (error) { state.error = error.message; render(); setPanel("plan"); }
}
function wire(body) {
  body.querySelectorAll("[data-setup-tab]").forEach((button) => button.addEventListener("click", () => setPanel(button.dataset.setupTab)));
  body.querySelectorAll("[data-setup-route]").forEach((button) => button.addEventListener("click", () => remediation(button.dataset.setupRoute)));
  body.querySelector("[data-setup-preflight]")?.addEventListener("click", () => planning("preflight"));
  body.querySelector("[data-setup-plan]")?.addEventListener("click", () => planning("plan"));
  body.querySelector("[data-setup-apply]")?.addEventListener("click", applyPlan);
  body.querySelector("[data-setup-reset]")?.addEventListener("click", () => { const textarea = body.querySelector("[data-project-setup-manifest]"); if (textarea) textarea.value = JSON.stringify(inferredManifest(), null, 2); });
  body.querySelector("[data-setup-fresh]")?.addEventListener("click", async () => { try { await api("/api/projects/" + encodeURIComponent(state.projectId) + "/fresh-bootstrap", { method: "POST" }); await refresh(); } catch (error) { state.error = error.message; render(); } });
}
async function refresh() {
  state.projectId = activeProjectId();
  state.error = null;
  try {
    const results = await Promise.all([
      api("/api/projects"),
      api("/api/projects/" + encodeURIComponent(state.projectId) + "/readiness"),
      api("/api/projects/" + encodeURIComponent(state.projectId) + "/bootstrap/status").catch((error) => error.status === 404 ? { items: [] } : Promise.reject(error)),
      api("/api/projects/" + encodeURIComponent(state.projectId) + "/ui-state?thread_limit=1&include_static=true")
    ]);
    state.project = (results[0] || []).find((project) => project.id === state.projectId) || results[3]?.project || null;
    state.readiness = results[1];
    state.bootstrap = results[2] || { items: [] };
    state.resources = results[3]?.resources?.items || [];
  } catch (error) { state.error = error.message; }
  render();
  return state;
}
function open() {
  const dialog = document.getElementById("project-setup-dialog");
  if (dialog && !dialog.open) dialog.showModal();
  refresh();
}
function build() {
  const controls = document.querySelector(".topbar .controls");
  if (controls && !document.getElementById("project-setup-launch")) {
    const button = document.createElement("button");
    button.type = "button"; button.id = "project-setup-launch"; button.className = "ghost-button project-setup-launch"; button.textContent = "Project setup"; button.addEventListener("click", open); controls.prepend(button);
  }
  if (!document.getElementById("project-setup-dialog")) {
    const dialog = document.createElement("dialog");
    dialog.id = "project-setup-dialog"; dialog.className = "project-setup-dialog";
    dialog.innerHTML = '<div class="project-setup-shell"><header class="project-setup-header"><div><small>Project lifecycle</small><h2>Setup, migration & readiness</h2><p>Canonical readiness and deterministic remediation.</p></div><button type="button" class="icon-button" data-project-setup-close>×</button></header><div class="project-setup-body" data-project-setup-body></div></div>';
    document.body.appendChild(dialog);
    dialog.querySelector("[data-project-setup-close]").addEventListener("click", () => dialog.close());
  }
  const grid = document.querySelector("#developer-panel .developer-grid");
  if (grid && !document.getElementById("project-setup-workspace-card")) {
    const card = document.createElement("div");
    card.className = "developer-card"; card.id = "project-setup-workspace-card";
    card.innerHTML = '<div class="section-title"><h2>Project Setup & Readiness</h2><button type="button" class="ghost-button" data-project-setup-open>Open setup</button></div><div class="form-result">Canonical bootstrap/readiness state.</div>';
    card.querySelector("[data-project-setup-open]").addEventListener("click", open);
    grid.appendChild(card);
  }
}
function install() {
  build();
  window.CodexProjectSetup = Object.freeze({ open, refresh, getState: () => ({ ...state }) });
  window.addEventListener("codex:project-changed", () => { state.plan = null; refresh(); });
  window.addEventListener("codex:project-created", () => { state.plan = null; open(); });
  window.addEventListener("hashchange", () => { if (location.hash.startsWith("#setup/")) open(); });
  refresh().then(() => { if (location.hash.startsWith("#setup/")) open(); });
}
if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", install, { once: true }); else install();
