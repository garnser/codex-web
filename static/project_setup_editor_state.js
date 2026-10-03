import { trackDirtyEditor, confirmDiscard } from './dirty_editor.js';

// Unsaved normalized manifests stay in this page only, scoped to one Project.
let projectId = '', editor = null, root = null, baseline = '', pending = null;
export function preserveSetupEditor(nextProject) {
  if (nextProject === projectId && root?.isConnected) {
    pending = {
      raw: editor.dirty() ? root.querySelector('textarea').value : null,
      baseline,
      expanded: root.open,
      panel: [...document.querySelectorAll('[data-setup-panel]')].find(node => !node.hidden)?.dataset.setupPanel,
    };
  } else pending = null;
  editor?.dispose(); editor = null; root = null; projectId = nextProject;
}
export function mountSetupEditor(body, initial) {
  root = body.querySelector('.project-setup-manifest-panel');
  if (!root) return;
  const input = root.querySelector('textarea');
  input.value = pending?.raw ?? initial;
  baseline = pending?.raw == null ? initial : pending.baseline;
  root.open = pending?.expanded || false;
  editor = trackDirtyEditor(root, { label: `Project ${projectId} setup manifest` });
  editor.markSaved(JSON.stringify([baseline]));
  const panel = pending?.panel; pending = null; return panel;
}
export function discardSetupEditor(value) {
  if (!confirmDiscard(editor)) return false;
  if (value !== undefined && root) {
    root.querySelector('textarea').value = value; baseline = value; editor.markSaved();
  }
  return true;
}
export function setupSubmission() {
  const raw = root?.querySelector('textarea').value;
  return { saved() {
    if (editor && raw !== undefined) { baseline = raw; editor.markSaved(JSON.stringify([raw])); }
  } };
}
