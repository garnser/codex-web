export function clearProjectProjection(state) {
  // Drop the previous project's projection before publishing the new scope.
  // A slow or failed refresh must never leave its threads available here.
  state.refreshController?.abort();
  state.refreshGeneration = (state.refreshGeneration || 0) + 1;
  state.threads = { data: [] };
  state.threadIndexProject = "";
  state.projectResources = [];
  state.botBindings = [];
  state.threadSettings = {};
  state.botChannels = [];
}

export class ProjectContextUnavailableError extends Error {
  constructor(projectId, projects = [], cause = null) {
    super(`Project ${projectId} is unavailable or inaccessible`);
    this.name = "ProjectContextUnavailableError";
    this.projectId = projectId;
    this.projects = projects;
    this.cause = cause;
  }
}

export async function loadProjectUiState({
  api,
  projectId,
  search = "",
  projects = [],
  reloadProjects = false,
  models = [],
  cachedStatic = null,
  onModelError = () => {},
  signal = null,
  deferModels = false,
}) {
  const query = new URLSearchParams({
    thread_limit: "50",
    include_static: cachedStatic ? "false" : "true",
  });
  if (search) query.set("search", search);

  const projectList = (!reloadProjects && projects.length)
    ? projects
    : await api("/api/projects", { signal });

  if (!projectList.some((project) => project.id === projectId)) {
    throw new ProjectContextUnavailableError(projectId, projectList);
  }

  // Models are independently useful metadata. A ready workspace may hydrate
  // its execution context and selected conversation while discovery continues.
  // The caller still owns/join this promise under its existing refresh signal.
  const modelsReady = models.length
    ? Promise.resolve(models)
    : api("/api/models", { signal })
      .then((response) => Array.isArray(response.data) ? response.data : [])
      .catch((error) => {
        if (error?.name === "AbortError") {
          if (deferModels) return null;
          throw error;
        }
        onModelError(error);
        return [];
      });
  let workspace;
  let modelList;
  try {
    [workspace, modelList] = await Promise.all([
      api(
        `/api/projects/${encodeURIComponent(projectId)}/ui-state?${query}`,
        { signal },
      ),
      deferModels ? Promise.resolve(models) : modelsReady,
    ]);
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    if (error?.status === 403 || error?.status === 404) {
      throw new ProjectContextUnavailableError(projectId, projectList, error);
    }
    throw error;
  }

  let nextProjects = projectList;
  if (workspace.project) {
    const index = nextProjects.findIndex(
      (project) => project.id === workspace.project.id,
    );
    if (index >= 0) {
      nextProjects = [...nextProjects];
      nextProjects[index] = workspace.project;
    } else {
      nextProjects = [...nextProjects, workspace.project];
    }
  }

  const staticState = workspace.project || workspace.executionProfiles
    ? {
        project: workspace.project || cachedStatic?.project || null,
        executionProfiles: (
          workspace.executionProfiles
          || cachedStatic?.executionProfiles
          || null
        ),
      }
    : cachedStatic;

  return {
    projects: nextProjects,
    models: modelList,
    ...(deferModels ? { modelsReady } : {}),
    staticState,
    resources: workspace.resources?.items || [],
    bindings: workspace.bindings?.items || [],
    threadSettings: workspace.threadSettings || {},
    channels: workspace.channels?.items || [],
    threads: workspace.threads || { data: [] },
  };
}


export async function loadProjectUiStateForRefresh({
  api,
  projectId,
  search,
  state,
  reloadProjects,
  controller,
  generation,
  renderProjects,
  renderThreads,
  clearMessages,
  logEvent,
  deferModels = false,
}) {
  try {
    return await loadProjectUiState({
      api,
      projectId,
      search,
      projects: state.projects,
      reloadProjects,
      models: state.models,
      cachedStatic: state.projectUiStatic[projectId] || null,
      signal: controller.signal,
      deferModels,
      onModelError: (error) => {
        logEvent("models.error", { message: error.message });
      },
    });
  } catch (error) {
    if (!(error instanceof ProjectContextUnavailableError)) throw error;
    if (
      controller.signal.aborted
      || generation !== state.refreshGeneration
      || projectId !== state.projectId
    ) return null;
    state.projects = error.projects || [];
    state.projectResources = [];
    state.botBindings = [];
    state.threadSettings = {};
    state.botChannels = [];
    state.threads = { data: [] };
    state.threadId = null;
    renderProjects();
    renderThreads();
    clearMessages();
    window.dispatchEvent(new CustomEvent("codex:project-context-unavailable", {
      detail: { projectId, projects: state.projects },
    }));
    logEvent("project.context_unavailable", { projectId });
    return null;
  }
}

export function refreshBindingQueueDepth(depthByThread, bindings) {
  for (const binding of bindings || []) {
    if (binding.thread_id && Number.isFinite(binding.queueDepth)) {
      depthByThread.set(binding.thread_id, Math.max(0, binding.queueDepth));
    }
  }
}

// Reuse the refresh owner and freshness token; no independent loader/controller.
// The caller invokes this only after publishing its canonical workspace and
// restoring the selected conversation, then joins the original optional read.
export async function applyProjectModels(snapshot, {
  state, controller, generation, projectId, search,
  getSearch, getControl, renderModelOptions,
}) {
  if (!snapshot.modelsReady) return;
  const models = await snapshot.modelsReady;
  if (!models || controller.signal.aborted
      || generation !== state.refreshGeneration || projectId !== state.projectId
      || search !== getSearch()) return;
  state.models = models;
  const control = getControl();
  if (control) control.innerHTML = renderModelOptions(control.value || "");
}
