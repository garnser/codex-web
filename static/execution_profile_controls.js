import { request as apiRequest } from "./api_client.js";

let catalog = {
  items: [],
  default_profile_id: "repository-write",
  definition: null,
};

export async function load(projectId) {
  try {
    catalog = await apiRequest(
      `/api/execution-profiles?project_id=${encodeURIComponent(projectId)}`,
    );
  } catch (_error) {
    catalog = {
      items: [],
      default_profile_id: "repository-write",
      definition: null,
    };
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
  selector.innerHTML = profiles.map((item) => (
    `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)}</option>`
  )).join("") || '<option value="repository-write">Repository write</option>';
  selector.value = profileId || defaultId();
  const profile = selected(selector.value);
  const scratch = profile?.workspaceMode === "scratch";
  if (profile) {
    const capabilities = (profile.requiredWorkerCapabilities || []).join(", ") || "none";
    summary.innerHTML = `<strong>${escapeHtml(profile.name)}</strong>: ${escapeHtml(profile.repositoryAccess)} repository access · ${escapeHtml(profile.workspaceMode)} workspace · capabilities ${escapeHtml(capabilities)}.${scratch ? " No mutable Git worktree is created." : ""}`;
  } else {
    summary.textContent = "Execution profile metadata unavailable.";
  }
  const mutable = document.getElementById("repository-target");
  const readOnly = document.getElementById("repository-read-context");
  if (mutable) mutable.disabled = scratch;
  if (readOnly) readOnly.disabled = scratch;
}
