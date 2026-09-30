import { referencePath } from './reference_navigation.js';
export function bindingPath(id, kind = 'agent') {
  return referencePath(kind === 'model' ? 'model_provider' : 'agent_provider', id);
}
export function updateRuntimeBindingLinks(card) {
  for (const select of card.querySelectorAll('.agent-new-runtime,.agent-preference-runtime')) {
    const link = select.parentElement.querySelector('[data-runtime-binding-link]'); if (!link) continue;
    const provider = select.value.split('/')[0]; const path = provider && bindingPath(provider);
    link.hidden = !path; if (path) link.href = path;
  }
}
