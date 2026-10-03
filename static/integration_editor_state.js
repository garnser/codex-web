import { formDraft } from './form_draft.js';

export function integrationEditors() {
  const entries = {
    gitlab: { draft: formDraft('GitLab routing'), selector: '#gitlab-enabled' },
    presence: { draft: formDraft('Agent channel presence'), selector: '#agent-channel-presence' },
  };
  for (const [key, entry] of Object.entries(entries)) {
    const anchor = document.querySelector(entry.selector), root = anchor?.closest('.gitlab-routing');
    if (!root) continue;
    entry.draft.mount(root);
    const discard = document.createElement('button'); discard.type = 'button'; discard.textContent = 'Discard edits';
    discard.dataset.integrationDiscard = key; discard.onclick = () => entry.draft.discard(); root.appendChild(discard);
  }
  return {
    dirty: key => entries[key].draft.dirty(),
    reset: key => entries[key].draft.reset(),
    view: key => entries[key].draft.view(),
    submission: key => entries[key].draft.submission(),
  };
}
