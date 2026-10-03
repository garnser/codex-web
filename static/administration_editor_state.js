import { trackDirtyEditor, confirmDiscard } from './dirty_editor.js';
import { captureProjectView } from './project_view_scope.js';

// Transient metadata only. One registration per connected Administration editor.
const entries = new Map();
let generation = 0;
for (const event of ['popstate', 'hashchange', 'codex:project-workspace-page', 'codex:project-changed']) {
  window.addEventListener(event, () => { generation += 1; });
}
function prune(container = null) {
  for (const [root, editor] of entries) {
    if (!root.isConnected || container?.contains(root)) {
      editor.dispose(); entries.delete(root);
    }
  }
}
export function administrationEditsPending(container) {
  prune();
  return [...entries].some(([root, editor]) => container.contains(root) && editor.dirty());
}
export function clearAdministrationEditors(container) { prune(container); }
export function trackAdministrationEditor(root, label) {
  prune();
  const editor = trackDirtyEditor(root, { label });
  entries.set(root, editor);
  const discard = document.createElement('button');
  discard.type = 'button'; discard.textContent = 'Discard edits';
  discard.dataset.administrationDiscard = '';
  discard.onclick = () => confirmDiscard(editor);
  root.appendChild(discard);
  return {
    dirty: editor.dirty,
    discard: () => confirmDiscard(editor),
    reset: () => editor.markSaved(),
    submission() {
      const snapshot = editor.snapshot(), view = captureProjectView();
      const origin = location.href, version = generation;
      const current = () => root.isConnected && entries.get(root) === editor
        && origin === location.href && version === generation && view.current();
      return { current, saved() { if (current()) editor.markSaved(snapshot); } };
    },
  };
}
