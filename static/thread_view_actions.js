import { captureProjectView } from "./project_view_scope.js";

let disposeArchiveRecovery = () => {};

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
  const project = captureProjectView(), threadId = state.threadId;
  await api(`/api/threads/${threadId}/archive`, { method: "POST" });
  if (!current()) return;
  clearSelectedThread({ historyMode: "replace" });
  await refresh();
  if (!project.current()) return;
  disposeArchiveRecovery();
  const recovery = document.createElement('div');
  recovery.dataset.threadArchiveRecovery = ''; recovery.className = 'form-result';
  recovery.setAttribute('role', 'status');
  const message = document.createElement('span');
  message.textContent = `Archived Thread ${threadId}. `;
  const restore = document.createElement('button'); restore.type = 'button';
  restore.textContent = 'Unarchive Thread';
  restore.onclick = async () => {
    if (!project.current()) { recovery.remove(); return; }
    restore.disabled = true;
    try {
      await api(`/api/threads/${threadId}/unarchive`, { method: 'POST' });
      if (!project.current()) return;
      message.textContent = `Restored Thread ${threadId}. Select it from the Thread list. `;
      restore.remove(); await refresh();
    } catch (error) {
      if (project.current()) { message.textContent = `Unarchive failed: ${error.message}. `; restore.disabled = false; }
    }
  };
  recovery.append(message, restore);
  (document.getElementById('threads')?.parentElement || document.body).appendChild(recovery);
  const expire = () => { recovery.remove(); window.removeEventListener('codex:project-changed', expire); };
  disposeArchiveRecovery = expire;
  window.addEventListener('codex:project-changed', expire, { once: true });
}
