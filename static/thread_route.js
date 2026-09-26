export function createThreadRoute({ state, api, clearSelection, loadThread, addMessage }) {
  const idFromLocation = () => new URLSearchParams(window.location.search).get("thread") || "";

  function updateLocation(threadId, { historyMode = "push" } = {}) {
    if (!window.location.pathname.includes("/projects/")) return;
    const url = new URL(window.location.href);
    if (threadId) url.searchParams.set("thread", threadId);
    else url.searchParams.delete("thread");
    const nextState = { ...history.state, projectId: state.projectId, threadId: threadId || null };
    if (historyMode === "push") history.pushState(nextState, "", url);
    else if (historyMode === "replace") history.replaceState(nextState, "", url);
  }

  async function find(threadId) {
    const projectId = state.projectId;
    const generation = state.threadLoadGeneration;
    const listed = state.threads?.data || state.threads?.threads || state.threads || [];
    let thread = listed.find((item) => String(item.id) === threadId);
    if (!thread) {
      const query = new URLSearchParams({ project_id: projectId, search: threadId, limit: "100" });
      const result = await api(`/api/threads?${query}`);
      if (projectId !== state.projectId || generation !== state.threadLoadGeneration) return null;
      thread = (result.data || result.threads || []).find((item) => String(item.id) === threadId);
    }
    if (!thread || (thread.projectId && thread.projectId !== projectId)) {
      throw new Error("This Thread is unavailable in the active Project.");
    }
    return thread;
  }

  async function restore() {
    const threadId = idFromLocation();
    if (!threadId) {
      if (state.threadId) clearSelection();
      return;
    }
    if (state.threadId === threadId) return;
    try {
      await loadThread(threadId, { historyMode: "none" });
    } catch (error) {
      clearSelection({ historyMode: "replace" });
      addMessage("Thread unavailable", error.message, "tool", new Date());
    }
  }

  return { find, idFromLocation, restore, updateLocation };
}
