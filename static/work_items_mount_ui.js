export function installWorkItemsMount(dialog, onOpen) {
  const shell = dialog.querySelector('.work-items-shell');
  const closeButton = dialog.querySelector('.work-items-close');

  window.addEventListener('codex:open-work-items', async (event) => {
    const host = event.detail?.mode === 'inline' && event.detail?.host instanceof HTMLElement
      ? event.detail.host
      : null;
    if (host) {
      if (dialog.open) dialog.close();
      if (shell.parentElement !== host) host.append(shell);
      shell.dataset.workItemsInline = 'true';
      closeButton.hidden = true;
    } else {
      if (shell.parentElement !== dialog) dialog.append(shell);
      delete shell.dataset.workItemsInline;
      closeButton.hidden = false;
      if (!dialog.open) dialog.showModal();
    }
    shell.hidden = false;
    await onOpen();
  });

  closeButton.addEventListener('click', () => dialog.close());
}

export function workItemsSurfaceActive() {
  if (document.querySelector('#work-items-dialog')?.open) return true;
  const shell = document.querySelector('.work-items-shell[data-work-items-inline="true"]');
  return Boolean(shell && !shell.hidden);
}
