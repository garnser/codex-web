const writableSelections = new Map();
const WRITABLE_SELECTIONS_KEY = "codex-web:writable-repository-selections:v1";
const PROJECT_SETTINGS_KEY = "codex-web-project-settings";

function persistedWritableSelections() {
  try {
    const value = JSON.parse(localStorage.getItem(WRITABLE_SELECTIONS_KEY) || "{}");
    return value && typeof value === "object" && !Array.isArray(value) ? value : {};
  } catch {
    return {};
  }
}

function persistWritableIds(projectId, ids) {
  if (!projectId) return;
  try {
    const selections = persistedWritableSelections();
    selections[projectId] = ids;
    localStorage.setItem(WRITABLE_SELECTIONS_KEY, JSON.stringify(selections));

    const projectSettings = JSON.parse(localStorage.getItem(PROJECT_SETTINGS_KEY) || "{}");
    const current = projectSettings[projectId] || {};
    projectSettings[projectId] = { ...current, writableRepositoryResourceIds: ids };
    localStorage.setItem(PROJECT_SETTINGS_KEY, JSON.stringify(projectSettings));
  } catch {
    // Browser storage is optional; in-memory selection remains authoritative for this page.
  }
}

function storedWritableIds(project, settings = {}) {
  const explicit = Array.isArray(settings.writableRepositoryResourceIds)
    ? settings.writableRepositoryResourceIds.filter(Boolean)
    : [];
  const key = project?.id || "";
  if (explicit.length) {
    const normalized = Array.from(new Set(explicit));
    writableSelections.set(key, normalized);
    persistWritableIds(key, normalized);
    return normalized;
  }
  if (writableSelections.has(key)) return writableSelections.get(key) || [];
  const persisted = persistedWritableSelections()[key];
  let projectPersisted = [];
  try {
    const projectSettings = JSON.parse(localStorage.getItem(PROJECT_SETTINGS_KEY) || "{}");
    projectPersisted = projectSettings[key]?.writableRepositoryResourceIds || [];
  } catch {
    projectPersisted = [];
  }
  const source = Array.isArray(persisted) && persisted.length ? persisted : projectPersisted;
  const normalized = Array.isArray(source)
    ? Array.from(new Set(source.filter(Boolean)))
    : [];
  writableSelections.set(key, normalized);
  return normalized;
}

export function selectedWritableRepositoryIds({ project, settings = {} } = {}) {
  const ids = storedWritableIds(project, settings);
  const primary = settings.repositoryResourceId || "";
  return primary && ids.includes(primary)
    ? [primary, ...ids.filter((id) => id !== primary)]
    : [...ids];
}

export function scope() {
  const writable = document.getElementById("repository-write-targets");
  const primary = document.getElementById("repository-target")?.value || "";
  const ids = Array.from(writable?.selectedOptions || [], (option) => option.value);
  const ordered = primary && ids.includes(primary)
    ? [primary, ...ids.filter((id) => id !== primary)]
    : ids;
  return { writable_repository_resource_ids: ordered };
}

export function activeRepositories(resources = []) {
  return resources.filter((item) => (
    item.resource_type === "repository" && item.lifecycle === "active"
  ));
}

export function targetState({
  policy,
  project,
  resources = [],
  repositories,
  settings = {},
  threadSettings = {},
  selectedId,
  boundId,
} = {}) {
  policy = policy || project?.repository_selection_policy || "deterministic";
  repositories = repositories || activeRepositories(resources);
  selectedId = selectedId ?? settings.repositoryResourceId ?? "";
  boundId = boundId ?? threadSettings.repository_resource_id ?? "";
  const storedIds = storedWritableIds(project, settings);
  const writableControl = typeof document === "undefined"
    ? null
    : document.getElementById("repository-write-targets");
  const controlIds = Array.from(
    writableControl?.selectedOptions || [],
    (option) => option.value,
  );
  const writableIds = controlIds.length ? controlIds : storedIds;
  const byId = new Map(repositories.map((item) => [item.id, item]));
  const requestedIds = writableIds.length ? writableIds : (selectedId ? [selectedId] : []);

  if (selectedId && writableIds.length && !writableIds.includes(selectedId)) {
    return {
      status: "conflict",
      blocked: true,
      code: "repository_target_conflict",
      message: `Repository conflict: ${selectedId} is selected as the primary target but is not in the writable set.`,
      id: selectedId,
      provenance: "explicit primary selection conflicts with coordinated writable set",
    };
  }

  if (boundId && requestedIds.length && !requestedIds.includes(boundId)) {
    return {
      status: "conflict",
      blocked: true,
      code: "repository_target_conflict",
      message: `Repository conflict: this thread is bound to ${boundId}, but the writable selection does not include it.`,
      id: selectedId || writableIds[0] || "",
      provenance: "explicit selection conflicts with thread binding",
    };
  }

  const missingIds = requestedIds.filter((id) => !byId.has(id));
  if (missingIds.length) {
    return {
      status: "stale",
      blocked: false,
      code: "repository_target_stale",
      message: `Repository ${missingIds[0]} is no longer an active Project-bound option; canonical preflight will validate it.`,
      id: missingIds[0],
      provenance: "explicit coordinated selection",
    };
  }

  if (writableIds.length > 1) {
    const labels = writableIds.map((id) => byId.get(id)?.name || id);
    return {
      status: "selected",
      blocked: false,
      code: null,
      message: `${writableIds.length} writable repositories · ${labels.join(", ")} · explicit coordinated selection`,
      id: selectedId || writableIds[0],
      repositoryIds: writableIds,
      provenance: "explicit coordinated selection",
    };
  }

  const id = boundId || writableIds[0] || selectedId;
  if (id) {
    const repository = byId.get(id);
    if (!repository) {
      return {
        status: "stale",
        blocked: false,
        code: "repository_target_stale",
        message: `Repository ${id} is no longer an active Project-bound option; canonical preflight will validate it.`,
        id,
        provenance: boundId ? "thread/profile binding" : "explicit selection",
      };
    }
    return {
      status: "selected",
      blocked: false,
      code: null,
      message: `${repository.name || repository.id} · ${boundId ? "thread/profile binding" : "explicit selection"}`,
      id,
      repository,
      provenance: boundId ? "thread/profile binding" : "explicit selection",
    };
  }

  if (policy === "explicit") {
    return {
      status: "required",
      blocked: true,
      code: "repository_target_missing",
      message: "Repository target required per turn",
      id: "",
      provenance: "explicit Project policy",
    };
  }

  return {
    status: "canonical",
    blocked: false,
    code: null,
    message: "Repository target resolved by canonical Work Item/routing/default context",
    id: "",
    provenance: "canonical preflight",
  };
}

export function renderControls({
  project,
  resources = [],
  settings = {},
  threadSettings = {},
  escapeHtml,
}) {
  const mutable = document.getElementById("repository-target");
  const writable = document.getElementById("repository-write-targets");
  const readOnly = document.getElementById("repository-read-context");
  const status = document.getElementById("repository-target-status");
  if (!mutable || !writable || !readOnly) return;

  const policy = project?.repository_selection_policy || "deterministic";
  const repositories = activeRepositories(resources);
  const boundId = threadSettings.repository_resource_id || "";
  const explicit = policy === "explicit";

  mutable.innerHTML = [
    `<option value="">${explicit ? "Select repository…" : "Auto"}</option>`,
    ...repositories.map((item) => (
      `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)} · ${escapeHtml(item.id)}</option>`
    )),
  ].join("");
  mutable.value = settings.repositoryResourceId || "";

  const selectedWritable = new Set(storedWritableIds(project, settings));
  writable.innerHTML = repositories
    .map((item) => (
      `<option value="${escapeHtml(item.id)}" ${selectedWritable.has(item.id) ? "selected" : ""}>${escapeHtml(item.name)}</option>`
    ))
    .join("");
  writable.onchange = () => {
    const ids = Array.from(writable.selectedOptions, (option) => option.value);
    const projectId = project?.id || "";
    writableSelections.set(projectId, ids);
    persistWritableIds(projectId, ids);
    renderControls({
      project,
      resources,
      settings: { ...settings, writableRepositoryResourceIds: ids },
      threadSettings,
      escapeHtml,
    });
  };

  const selected = new Set(settings.readOnlyRepositoryResourceIds || []);
  readOnly.innerHTML = repositories
    .filter((item) => (
      item.id !== settings.repositoryResourceId
      && !selectedWritable.has(item.id)
    ))
    .map((item) => (
      `<option value="${escapeHtml(item.id)}" ${selected.has(item.id) ? "selected" : ""}>${escapeHtml(item.name)}</option>`
    ))
    .join("");

  const target = targetState({ policy, repositories, settings, threadSettings });
  if (status) {
    status.textContent = target.message;
    status.dataset.state = target.status;
  }
  mutable.setAttribute("aria-invalid", String(target.blocked));
  mutable.required = explicit && !boundId;
}

export function renderStatus({
  project,
  resources = [],
  settings = {},
  threadSettings = {},
}) {
  const status = document.getElementById("repository-target-status");
  const mutable = document.getElementById("repository-target");
  if (!status || !mutable) return;
  const target = targetState({ project, resources, settings, threadSettings });
  status.textContent = target.message;
  status.dataset.state = target.status;
  mutable.setAttribute("aria-invalid", String(target.blocked));
  mutable.required = (project?.repository_selection_policy === "explicit")
    && !threadSettings.repository_resource_id;
}
