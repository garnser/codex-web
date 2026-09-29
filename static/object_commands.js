import { registerCommandSource } from './command_palette.js';

export function installThreadCommands(state, loadThread) {
  registerCommandSource('threads', ({ projectId, projectLabel }) => {
    if (!projectId || state.threadIndexProject !== projectId || state.projectId !== projectId) return [];
    const items = state.threads?.data || state.threads?.threads || state.threads || [];
    return items.filter((item) => !item.projectId || item.projectId === projectId).map((item) => ({
      id: item.id, label: item.name || item.preview || 'Untitled thread', projectId,
      scopeLabel: `Thread · Project: ${projectLabel}`,
      run: () => {
        window.CodexProductUI?.openWorkspace('threads');
        return loadThread(item.id, { historyMode: 'push' });
      },
    }));
  });
  window.addEventListener('codex:project-changed', () => { state.threadIndexProject = ''; });
}

export function installWorkItemCommands(state, loadDetail) {
  registerCommandSource('work-items', ({ projectId, projectLabel }) => {
    if (!projectId || state.projectId !== projectId) return [];
    return state.items.filter((item) => item.project_id === projectId).map((item) => ({
      id: item.ref, label: item.title || item.ref, projectId,
      scopeLabel: `Work Item · Project: ${projectLabel}`, description: item.ref,
      run: () => window.dispatchEvent(new CustomEvent('codex:open-work-items', {
        detail: { commandRef: item.ref, commandProject: projectId },
      })),
    }));
  });
  return async ({ commandRef, commandProject } = {}) => {
    if (!commandRef || commandProject !== state.projectId) return;
    if (!state.items.some((item) => item.ref === commandRef && item.project_id === commandProject)) return;
    state.selectedRef = commandRef;
    await loadDetail(commandRef);
  };
}
