export async function loadProjectUiState({
  api,
  projectId,
  search = "",
  projects = [],
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

  const [projectList, workspace, modelList] = await Promise.all([
    projects.length ? Promise.resolve(projects) : api("/api/projects", { signal }),
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
