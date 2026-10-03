import { trackDirtyEditor, confirmDiscard } from './dirty_editor.js';
import { captureProjectView } from './project_view_scope.js';

const editors = new Map();
const buttons = { org: 'identity-create-org', workspace: 'identity-create-workspace', human: 'identity-create-human', service: 'identity-create-service', membership: 'identity-create-membership', token: 'identity-create-token' };
const selectState = root => [...root.querySelectorAll('select')].map(node => ({ node, html: node.innerHTML, values: [...node.selectedOptions].map(option => option.value) }));
export function setupIdentityEditors() {
  for (const [key, id] of Object.entries(buttons)) {
    if (editors.has(key)) continue;
    const button = document.getElementById(id), root = button?.closest('.route-test');
    if (!root) continue;
    const entry = { root, button, options: selectState(root) };
    entry.editor = trackDirtyEditor(root, { label: `Identity ${key}`, onDiscard() {
      // Restore dependent option catalogs before selecting the saved values.
      for (const { node, html, values } of entry.options) {
        node.innerHTML = html;
        for (const option of node.options) option.selected = values.includes(option.value);
      }
      root.dispatchEvent(new Event('input', { bubbles: true }));
    } });
    const discard = document.createElement('button'); discard.type = 'button';
    discard.className = 'ghost-button'; discard.dataset.identityDiscard = key;
    discard.textContent = 'Discard unsaved changes'; discard.onclick = () => confirmDiscard(entry.editor);
    root.appendChild(discard); editors.set(key, entry);
  }
}
export function identityEditsPending() { return [...editors.values()].some(entry => entry.editor.dirty()); }
export function resetIdentityEditors() {
  setupIdentityEditors();
  for (const entry of editors.values()) { entry.options = selectState(entry.root); entry.editor.markSaved(); }
}
export async function runIdentityMutation(key, action) {
  const entry = editors.get(key), view = captureProjectView();
  if (!entry || entry.button.disabled || entry.pending) return;
  const snapshot = entry.editor.snapshot(), options = selectState(entry.root);
  const current = () => view.current() && entry.root.isConnected;
  entry.pending = true;
  try { await action({ current, submitting() { if (current()) entry.button.disabled = true; }, saved() {
    if (current()) { entry.options = options; entry.editor.markSaved(snapshot); }
  } }); } finally {
    entry.pending = false;
    if (entry.root.isConnected) entry.button.disabled = false;
    if (current() && document.activeElement === document.body) entry.button.focus();
  }
}
