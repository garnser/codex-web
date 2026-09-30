import { request as apiRequest } from "./api_client.js";

let activeProjectId = "";
let generation = 0;
let pending = null;
let catalog = {
  items: [],
  default_profile_id: "repository-write",
  definition: null,
};

function clearControls(message) {
  const selector = document.getElementById('execution-profile');
  if (selector) {
    selector.replaceChildren(new Option(message, ''));
    selector.disabled = true;
  }
  const summary = document.getElementById('execution-profile-summary');
  if (summary) summary.textContent = message;
  for (const id of ['repository-target', 'repository-write-targets', 'repository-read-context']) {
    const control = document.getElementById(id);
    if (control) control.disabled = true;
  }
}

export function setCatalog(value, projectId = activeProjectId) {
  generation += 1;
  pending?.abort();
  pending = null;
  activeProjectId = String(projectId || '').trim();
  catalog = value && typeof value === "object"
    ? value
    : {
        items: [],
        default_profile_id: "repository-write",
        definition: null,
      };
  if (!value) clearControls(activeProjectId ? 'Loading execution profiles…' : 'Select a Project to view execution profiles.');
  return catalog;
}

export async function load(projectId) {
  const id = String(projectId || '').trim();
  setCatalog(null, id);
  if (!id) return catalog;
  const requestGeneration = generation;
  const controller = new AbortController();
  pending = controller;
  try {
    const response = await apiRequest(
      `/api/execution-profiles?project_id=${encodeURIComponent(id)}`,
      { signal: controller.signal },
    );
    if (requestGeneration === generation && id === activeProjectId) setCatalog(response, id);
  } catch (_error) {
    if (requestGeneration === generation) clearControls('Execution profiles unavailable. Refresh this Project to retry.');
  } finally {
    if (pending === controller) pending = null;
  }
  return catalog;
}

export function defaultId() {
  return catalog.default_profile_id || "repository-write";
}

export function selected(profileId) {
  return (catalog.items || []).find((item) => item.id === profileId) || null;
}

export function isScratch(profileId) {
  return selected(profileId)?.workspaceMode === "scratch";
}

export function applyThreadQuery(params, settings) {
  params.set("execution_profile_id", settings.profileId || defaultId());
  if (settings.repositoryResourceId && !isScratch(settings.profileId)) {
    params.set("repository_resource_id", settings.repositoryResourceId);
  }
}

export function render(profileId, escapeHtml) {
  const selector = document.getElementById("execution-profile");
  const summary = document.getElementById("execution-profile-summary");
  if (!selector || !summary) return;
  const profiles = catalog.items || [];
  selector.disabled = !profiles.length;
  selector.innerHTML = profiles.map((item) => (
    `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)}</option>`
  )).join("") || '<option value="">Execution profiles unavailable</option>';
  selector.value = profileId || defaultId();
  const profile = selected(selector.value);
  const scratch = profile?.workspaceMode === "scratch";
  if (profile) {
    const capabilities = (profile.requiredWorkerCapabilities || []).join(", ") || "none";
    const prefix = window.location.pathname.startsWith("/codex") ? "/codex" : "";
    const definitionsPath = `${prefix}/projects/${encodeURIComponent(activeProjectId || "home")}/definitions?execution_profile=${encodeURIComponent(profile.id)}`;
    const definition = catalog.definition;
    const provenance = definition
      ? ` Canonical definition: ${escapeHtml(definition.definition_id)}@r${escapeHtml(definition.revision)}.`
      : "";
    summary.innerHTML = `<strong>${escapeHtml(profile.name)}</strong>: ${escapeHtml(profile.repositoryAccess)} repository access · ${escapeHtml(profile.workspaceMode)} workspace · capabilities ${escapeHtml(capabilities)}.${scratch ? " No mutable Git worktree is created." : ""}${provenance} <a id="manage-execution-profiles" class="ghost-button" href="${escapeHtml(definitionsPath)}">Manage profiles in Definitions</a>`;
  } else {
    summary.textContent = "Execution profile metadata unavailable.";
  }
  const mutable = document.getElementById("repository-target");
  const writable = document.getElementById("repository-write-targets");
  const readOnly = document.getElementById("repository-read-context");
  if (mutable) mutable.disabled = !profile || scratch;
  if (writable) writable.disabled = !profile || scratch;
  if (readOnly) readOnly.disabled = !profile || scratch;
}

window.addEventListener('codex:project-changed', event => {
  const id = String(event.detail?.projectId || '').trim();
  if (id !== activeProjectId) setCatalog(null, id);
});
