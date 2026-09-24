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

  let workspace;
  let modelList;
  try {
    [workspace, modelList] = await Promise.all([
      api(
        `/api/projects/${encodeURIComponent(projectId)}/ui-state?${query}`,
        { signal },
      ),
      models.length
        ? Promise.resolve(models)
        : api("/api/models", { signal })
          .then((response) => (
            Array.isArray(response.data) ? response.data : []
          ))
          .catch((error) => {
            if (error?.name === "AbortError") throw error;
            onModelError(error);
            return [];
          }),
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
