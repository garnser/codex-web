import { showPageEditor } from './page_editor.js';

export function showBotEditor(dialog) {
  if (window.CodexProductUI?.openWorkspace?.('integrations') === false) return false;
  const close = dialog.querySelector('#close-bot-dialog');
  const save = dialog.querySelector('#save-bot-integration');
  close.dataset.close = ''; save.dataset.save = '';
  // Write-only inputs must never enter dirty-editor snapshots or browser storage.
  dialog.addEventListener('close', () => {
    dialog.querySelectorAll('input[type="password"]').forEach(input => { input.value = ''; });
  }, { once: true });
  showPageEditor(dialog);
  return true;
}
