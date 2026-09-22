const ACTIVE_PROJECT_KEY = "codex-web-active-project";

export function initialProjectId() {
  return (
    new URLSearchParams(window.location.search).get("project")
    || sessionStorage.getItem(ACTIVE_PROJECT_KEY)
    || "home"
  );
}

export function activateProject(state, projectId, { historyMode = "replace" } = {}) {
  const normalized = String(projectId || "").trim();
  if (!normalized) return "";
  state.projectId = normalized;
  sessionStorage.setItem(ACTIVE_PROJECT_KEY, normalized);
  if (document.body) document.body.dataset.projectId = normalized;
  const url = new URL(window.location.href);
  url.searchParams.set("project", normalized);
  if (historyMode === "push") {
    history.pushState({ ...history.state, projectId: normalized }, "", url);
  } else if (historyMode !== "none") {
    history.replaceState({ ...history.state, projectId: normalized }, "", url);
  }
  window.dispatchEvent(
    new CustomEvent("codex:project-changed", {
      detail: { projectId: normalized },
    }),
  );
  return normalized;
}


export function publishProjectsRendered(projects, projectId) {
  window.dispatchEvent(new CustomEvent("codex:projects-rendered", {
    detail: {
      projectId,
      projects: projects.map(({ id, name, path }) => ({ id, name, path })),
    },
  }));
}

export function createProjectNavigator(state, {
  refresh,
  applyRunSettings,
  onError = console.error,
} = {}) {
  const selectProject = async (projectId, { historyMode = "push" } = {}) => {
    const normalized = String(projectId || "").trim();
    if (!normalized || normalized === state.projectId) return;
    activateProject(state, normalized, { historyMode });
    state.threadId = null;
    state.activeAgentMessage = null;
    applyRunSettings?.();
    await refresh?.();
  };
  const run = (projectId, options) => {
    selectProject(projectId, options).catch(onError);
  };
  window.addEventListener("codex:project-select", (event) => {
    run(event.detail?.projectId);
  });
  window.addEventListener("popstate", () => {
    const projectId = new URLSearchParams(window.location.search).get("project");
    if (projectId && projectId !== state.projectId) {
      run(projectId, { historyMode: "none" });
    }
  });
  return selectProject;
}
