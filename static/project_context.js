const ACTIVE_PROJECT_KEY = "codex-web-active-project";

function projectIdFromPath(pathname = window.location.pathname) {
  const match = String(pathname || "").match(/\/projects\/([^/]+)(?:\/|$)/);
  if (!match) return "";
  try {
    return decodeURIComponent(match[1]);
  } catch {
    return match[1];
  }
}

export function initialProjectId() {
  return (
    projectIdFromPath()
    || new URLSearchParams(window.location.search).get("project")
    || sessionStorage.getItem(ACTIVE_PROJECT_KEY)
    || "home"
  );
}

export function activateProject(state, projectId, { historyMode = "replace" } = {}) {
  const normalized = String(projectId || "").trim();
  if (!normalized) return "";
  const projectChanged = state.projectId !== normalized;
  state.projectId = normalized;
  sessionStorage.setItem(ACTIVE_PROJECT_KEY, normalized);
  if (document.body) document.body.dataset.projectId = normalized;
  const url = new URL(window.location.href);
  const routed = url.pathname.match(/^(.*\/projects\/)[^/]+(\/[^/]+\/?)$/);
  if (routed) {
    url.pathname = `${routed[1]}${encodeURIComponent(normalized)}${routed[2]}`;
    url.searchParams.delete("project");
  } else {
    url.searchParams.set("project", normalized);
  }
  if (projectChanged && historyMode !== "none") url.searchParams.delete("thread");
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
  onThreadCleared,
  onLocationChanged,
  onError = console.error,
} = {}) {
  const selectProject = async (projectId, { historyMode = "push" } = {}) => {
    const normalized = String(projectId || "").trim();
    if (!normalized || normalized === state.projectId) return;
    activateProject(state, normalized, { historyMode });
    state.threadId = null;
    state.activeAgentMessage = null;
    onThreadCleared?.();
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
    const projectId = (
      projectIdFromPath()
      || new URLSearchParams(window.location.search).get("project")
    );
    if (projectId && projectId !== state.projectId) {
      selectProject(projectId, { historyMode: "none" })
        .then(() => onLocationChanged?.())
        .catch(onError);
      return;
    }
    onLocationChanged?.();
  });
  return selectProject;
}
