import { trackDirtyEditor, confirmDiscard } from './dirty_editor.js';
import { captureProjectView } from './project_view_scope.js';

// Ephemeral reference selections only; canonical extension APIs own grants/config.
const editors = new Map();
function prune() {
  for (const [root, editor] of editors) if (!root.isConnected) { editor.dispose(); editors.delete(root); }
}
export function extensionEditsPending() {
  prune(); return [...editors.values()].some(editor => editor.dirty());
}
export function deferExtensionRefresh() {
  if (extensionEditsPending()) {
    const status = document.getElementById('extension-admin-status');
    if (status) status.textContent = 'Refresh deferred. Save or discard unsaved extension selections first.';
    return true;
  }
  for (const editor of editors.values()) editor.dispose();
  editors.clear(); return false;
}
export function trackExtensionEditor(root, label) {
  prune(); if (!root || editors.has(root)) return;
  const editor = trackDirtyEditor(root, { label }); editors.set(root, editor);
  const discard = document.createElement('button'); discard.type = 'button';
  discard.className = 'ghost-button'; discard.dataset.extensionDiscard = '';
  discard.textContent = 'Discard unsaved changes';
  discard.onclick = () => confirmDiscard(editor); root.appendChild(discard);
}
export function trackExtensionGrants(host) {
  host.querySelectorAll('[data-extension-capability-row]').forEach(row => {
    if (row.querySelector('select')) trackExtensionEditor(row, `Extension ${row.dataset.extensionId} grant ${row.dataset.capability}`);
  });
}
export function extensionSubmission(root) {
  const view = captureProjectView(), editor = editors.get(root), submitted = editor?.snapshot();
  const current = () => view.current() && root.isConnected;
  return { current, saved() { if (current()) editor?.markSaved(submitted); } };
}
