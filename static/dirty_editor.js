// Ephemeral editor state only. Never persist field values to browser storage.
const editors = new Set();

function fields(root) {
  return [...root.querySelectorAll('input,select,textarea')]
    .filter(field => !['submit', 'button', 'reset'].includes(field.type));
}

function snapshot(root) {
  return JSON.stringify(fields(root).map(field => {
    if (field.type === 'checkbox' || field.type === 'radio') return field.checked;
    if (field.multiple) return [...field.selectedOptions].map(option => option.value);
    return field.value;
  }));
}

export function trackDirtyEditor(root, { label, onDiscard } = {}) {
  let saved = snapshot(root);
  const notice = document.createElement('p');
  notice.dataset.dirtyEditorStatus = '';
  notice.setAttribute('role', 'status');
  root.appendChild(notice);
  const editor = {
    label: label || 'Editor',
    location: window.location.href,
    historyState: history.state,
    dirty: () => root.isConnected && snapshot(root) !== saved,
    snapshot: () => snapshot(root),
    markSaved(value = snapshot(root)) { saved = value; update(); },
    discard() {
      const values = JSON.parse(saved);
      fields(root).forEach((field, index) => {
        const value = values[index];
        if (field.type === 'checkbox' || field.type === 'radio') field.checked = value;
        else if (field.multiple) [...field.options].forEach(option => { option.selected = value.includes(option.value); });
        else field.value = value;
      });
      update();
      onDiscard?.();
    },
    dispose() {
      editors.delete(editor);
      root.removeEventListener('input', update);
      root.removeEventListener('change', update);
      notice.remove();
      saved = '[]';
    },
  };
  function update() {
    notice.textContent = editor.dirty() ? 'Unsaved changes' : 'No unsaved changes';
  }
  root.addEventListener('input', update);
  root.addEventListener('change', update);
  editors.add(editor);
  update();
  return editor;
}

export function confirmDiscard(editor = null) {
  const pending = [...(editor ? [editor] : editors)].filter(item => item.dirty());
  if (!pending.length) return true;
  if (!window.confirm(`Discard unsaved changes in ${pending.map(item => item.label).join(', ')}? Choose Cancel to keep editing.`)) return false;
  pending.forEach(item => item.discard());
  return true;
}

window.addEventListener('beforeunload', event => {
  if (![...editors].some(editor => editor.dirty())) return;
  event.preventDefault();
  event.returnValue = '';
});

// Capture history navigation before the application changes Project or surface.
// Rejected navigation restores the editor's URL and stops downstream loaders.
function guardHistory(event) {
  const pending = [...editors].find(editor => editor.dirty() && editor.location !== location.href);
  if (!pending || confirmDiscard()) return;
  history.pushState(pending.historyState, '', pending.location);
  event.stopImmediatePropagation();
}
window.addEventListener('popstate', guardHistory, { capture: true });
window.addEventListener('hashchange', guardHistory, { capture: true });
