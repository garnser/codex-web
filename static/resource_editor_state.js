import { trackDirtyEditor, confirmDiscard } from './dirty_editor.js';

// Page-memory editor guards, not a second resource cache or persistence layer.
const editors = new Map();
const rows = new Set();

export function resourceEditor(root, { row = false, label = 'Resource', onDiscard } = {}) {
  if (!root) return null;
  if (editors.has(root)) return editors.get(root);
  const editor = trackDirtyEditor(root, { label });
  editors.set(root, editor);
  if (row) rows.add(root);
  const discard = document.createElement('button');
  discard.type = 'button';
  discard.className = 'ghost-button';
  discard.dataset.resourceDiscard = '';
  discard.textContent = 'Discard changes';
  discard.addEventListener('click', () => {
    if (confirmDiscard(editor)) onDiscard?.();
  });
  root.appendChild(discard);
  return editor;
}

export function resourceRowsCanRender() {
  if ([...rows].some(root => editors.get(root)?.dirty())) return false;
  for (const root of rows) {
    editors.get(root)?.dispose();
    editors.delete(root);
  }
  rows.clear();
  return true;
}
