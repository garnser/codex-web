import { showPageEditor } from './page_editor.js';
import { trackDirtyEditor, confirmDiscard } from './dirty_editor.js';

let entry = null;

export function botEditorSubmission() {
  const current = entry, snapshot = current?.editor.snapshot();
  return {
    current: () => current === entry && Boolean(current?.form.isConnected),
    saved() { if (current === entry) current.editor.markSaved(snapshot); },
    dirty: () => Boolean(current?.editor.dirty()),
  };
}

export function showBotEditor(dialog) {
  if (window.CodexProductUI?.openWorkspace?.('integrations') === false) return false;
  const close = dialog.querySelector('#close-bot-dialog');
  const save = dialog.querySelector('#save-bot-integration');
  const form = dialog.querySelector('#bot-form');
  close.dataset.close = ''; save.dataset.save = '';
  entry?.editor.dispose();
  entry = { form, editor: trackDirtyEditor(form, { label: 'Bot Integration', ignore: 'input[type="password"]' }) };
  const discard = document.createElement('button'); discard.type = 'button'; discard.textContent = 'Discard metadata edits';
  discard.dataset.botDiscard = ''; discard.onclick = () => confirmDiscard(entry.editor); form.appendChild(discard);
  const guard = event => {
    if (confirmDiscard(entry.editor)) return;
    event.preventDefault(); event.stopImmediatePropagation();
  };
  close.addEventListener('click', guard, { capture: true });
  dialog.querySelector('#cancel-bot-integration')?.addEventListener('click', guard, { capture: true });
  dialog.addEventListener('cancel', guard, { capture: true });
  // Write-only inputs must never enter dirty-editor snapshots or browser storage.
  dialog.addEventListener('close', () => {
    dialog.querySelectorAll('input[type="password"]').forEach(input => { input.value = ''; });
    entry?.editor.dispose(); entry = null; discard.remove();
  }, { once: true });
  showPageEditor(dialog);
  return true;
}
