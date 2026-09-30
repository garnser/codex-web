import { showPageEditor } from './page_editor.js';

export function installWorkItemsMount(dialog, onOpen, canClose = () => true) {
  const shell = dialog.querySelector('.work-items-shell');
  const closeButton = dialog.querySelector('.work-items-close');
  let pendingDetail = null;

  window.addEventListener('codex:open-work-items', async (event) => {
    const host = event.detail?.mode === 'inline' && event.detail?.host instanceof HTMLElement
      ? event.detail.host
      : null;
    if (!host && document.getElementById('product-workspace-page') && window.CodexProductUI?.openWorkspace) {
      pendingDetail = event.detail;
      try { window.CodexProductUI.openWorkspace('work', { page: 'work-items' }); }
      finally { pendingDetail = null; }
      return;
    }
    const detail = { ...pendingDetail, ...event.detail };
    if (host) {
      if (dialog.open) dialog.close();
      if (shell.parentElement !== host) host.append(shell);
      shell.dataset.workItemsInline = 'true';
      closeButton.hidden = true;
    } else {
      if (shell.parentElement !== dialog) dialog.append(shell);
      delete shell.dataset.workItemsInline;
      closeButton.hidden = false;
      if (!dialog.open) showPageEditor(dialog, { closeOnNavigation: false });
    }
    shell.hidden = false;
    await onOpen(detail);
  });

  closeButton.addEventListener('click', () => { if (canClose()) dialog.close(); });
  dialog.addEventListener('cancel', event => { if (!canClose()) event.preventDefault(); });
}

export function workItemsSurfaceActive() {
  if (document.querySelector('#work-items-dialog')?.open) return true;
  const shell = document.querySelector('.work-items-shell[data-work-items-inline="true"]');
  return Boolean(shell && !shell.hidden);
}
