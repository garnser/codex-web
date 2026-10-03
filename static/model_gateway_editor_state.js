import { trackDirtyEditor, confirmDiscard } from './dirty_editor.js';
import { captureProjectView } from './project_view_scope.js';

// Presentation-only field groups. Values stay in page memory, including prompts.
const slots = {
  provider: ['model-provider-id', 'model-provider-existing', 'Model provider'],
  model: ['model-definition-id', 'model-definition-existing', 'Model definition'],
  prompt: ['model-prompt-id', 'model-prompt-source', 'Prompt version'],
  policy: ['model-policy-max-attempts', '', 'Model routing policy'],
};
const editors = new Map();
export function modelView() { return captureProjectView(); }
export function setupModelEditors() {
  for (const [key, [field, sourceId, label]] of Object.entries(slots)) {
    if (editors.has(key)) continue;
    const root = document.getElementById(field)?.closest('.route-test');
    if (!root) continue;
    const source = sourceId ? document.getElementById(sourceId) : null;
    const entry = { root, source, selected: source?.value || '' };
    entry.dirty = trackDirtyEditor(root, { label, ignore: sourceId ? `#${sourceId}` : '',
      onDiscard: () => { if (source) source.value = entry.selected; } });
    const discard = document.createElement('button'); discard.type = 'button';
    discard.className = 'ghost-button'; discard.textContent = 'Discard unsaved changes';
    discard.dataset.modelDiscard = key;
    discard.onclick = () => confirmDiscard(entry.dirty);
    root.appendChild(discard); editors.set(key, entry);
  }
}
export function modelEditsPending() {
  return [...editors.values()].some(entry => entry.dirty.dirty());
}
export function resetModelEditors() {
  setupModelEditors();
  for (const entry of editors.values()) {
    entry.selected = entry.source?.value || ''; entry.dirty.markSaved();
  }
}
export function selectModelSource(key, value, load) {
  const entry = editors.get(key);
  if (entry && !confirmDiscard(entry.dirty)) { entry.source.value = entry.selected; return; }
  load(value);
  if (entry) { entry.source.value = value; entry.selected = value; entry.dirty.markSaved(); }
}
export function modelSubmission(key) {
  const entry = editors.get(key), view = captureProjectView();
  const submitted = entry?.dirty.snapshot();
  return {
    current: () => view.current() && Boolean(entry?.root.isConnected),
    saved() { if (view.current() && entry?.root.isConnected) entry.dirty.markSaved(submitted); },
  };
}
