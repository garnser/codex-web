import { captureProjectView } from "./project_view_scope.js";

// UI continuations only: the server owns authorization and accepted mutations.
export function captureThreadView(state) {
  const project = captureProjectView();
  const { projectId, threadId, threadLoadGeneration } = state;
  return () => project.current() && state.projectId === projectId
    && state.threadId === threadId && state.threadLoadGeneration === threadLoadGeneration;
}

export async function renameSelectedThread({ state, api, title, patchThread }) {
  if (!state.threadId) return;
  const current = captureThreadView(state);
  const threadId = state.threadId;
  const name = window.prompt("Thread name", title.textContent);
  if (!name?.trim()) return;
  await api(`/api/threads/${threadId}/name`, {
    method: "POST", body: JSON.stringify({ name: name.trim() }),
  });
  if (!current()) return;
  title.textContent = name.trim();
  patchThread(threadId, { name: name.trim(), updatedAt: Date.now() / 1000 });
}

export async function archiveSelectedThread({ state, api, clearSelectedThread, refresh }) {
  if (!state.threadId) return;
  const current = captureThreadView(state);
  await api(`/api/threads/${state.threadId}/archive`, { method: "POST" });
  if (!current()) return;
  clearSelectedThread({ historyMode: "replace" });
  await refresh();
}
