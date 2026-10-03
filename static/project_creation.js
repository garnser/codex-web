import { formDraft } from './form_draft.js';

export function projectCreationEditor(dialog) {
  const form = dialog.querySelector('form');
  const save = dialog.querySelector('#save-project');
  const cancel = dialog.querySelector('#cancel-project');
  const result = dialog.querySelector('#project-result');
  const draft = formDraft('Project creation');
  let busy = false;
  draft.mount(form);

  const discard = document.createElement('button');
  discard.type = 'button'; discard.textContent = 'Discard project edits';
  discard.dataset.projectDiscard = '';
  discard.addEventListener('click', () => draft.discard());
  form.querySelector('menu').before(discard);

  const guard = event => {
    if (draft.discard()) return;
    event.preventDefault(); event.stopImmediatePropagation();
  };
  cancel.addEventListener('click', event => {
    event.preventDefault(); event.stopImmediatePropagation();
    if (draft.discard()) dialog.close('cancel');
  }, { capture: true });
  dialog.addEventListener('cancel', guard, { capture: true });

  function clear() {
    form.reset(); draft.reset(); result.hidden = true; result.textContent = '';
  }

  return {
    open() { if (!dialog.open) dialog.showModal(); },
    async save(event, { api, accept, refresh }) {
      event.preventDefault();
      if (busy || !form.reportValidity()) return;
      const ticket = draft.submission();
      const payload = {
        name: dialog.querySelector('#project-name').value,
        path: dialog.querySelector('#project-path').value,
        model: dialog.querySelector('#project-model').value || null,
      };
      busy = true; save.disabled = true;
      result.hidden = false; result.textContent = 'Saving...';
      try {
        const project = await api('/api/projects', { method: 'POST', body: JSON.stringify(payload) });
        if (!ticket.current()) return;
        ticket.saved();
        const hasNewerEdits = draft.dirty();
        accept(project, { activate: !hasNewerEdits });
        if (hasNewerEdits) {
          result.textContent = `Created ${project.name}; newer project edits remain unsaved.`;
        } else {
          dialog.close(); clear();
        }
        await refresh();
      } catch (error) {
        if (ticket.current()) result.textContent = error.message;
      } finally {
        busy = false;
        if (save.isConnected) save.disabled = false;
      }
    },
  };
}
