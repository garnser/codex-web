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
  if (historyMode !== "none") {
    const method = historyMode === "push" ? "pushState" : "replaceState";
    history[method](
      { ...history.state, projectId: normalized },
      "",
      url,
    );
  }
  window.dispatchEvent(
    new CustomEvent("codex:project-changed", {
      detail: { projectId: normalized },
    }),
  );
  return normalized;
}
