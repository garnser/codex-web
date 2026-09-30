import { currentProjectId } from './project_view_scope.js';
export function bindingPath(id, kind = 'agent') {
  const project = currentProjectId();
  return project ? `/projects/${encodeURIComponent(project)}/operations?${kind === 'model' ? 'model_provider_id' : 'provider_id'}=${encodeURIComponent(id)}` : null;
}
export function updateRuntimeBindingLinks(card) {
  for (const select of card.querySelectorAll('.agent-new-runtime,.agent-preference-runtime')) {
    const link = select.parentElement.querySelector('[data-runtime-binding-link]'); if (!link) continue;
    const provider = select.value.split('/')[0]; const path = provider && bindingPath(provider);
    link.hidden = !path; if (path) link.href = path;
  }
}
