import { currentProjectId } from './project_view_scope.js';
import { esc, secretPath } from './secret_reference_ui.js';

export { esc };

export function secretLinks(references = []) {
  return references.filter(Boolean).map(id => !currentProjectId() ? esc(id) : `<a href="${esc(secretPath(id))}">${esc(id)}</a>`).join(', ') || 'none';
}
export function referenceAttributes(kind, id) {
  return `data-reference-kind="${esc(kind)}" data-reference-id="${esc(id)}"`;
}
export function focusReference(host) {
  const query = new URLSearchParams(location.search);
  const kind = query.get('consumer_type'); const id = query.get('consumer_id');
  if (!kind || !id) return;
  const row = [...host.querySelectorAll('[data-reference-kind]')].find(node => node.dataset.referenceKind === kind && node.dataset.referenceId === id);
  if (row) { row.tabIndex = -1; row.focus(); row.scrollIntoView({ block: 'nearest' }); }
}

export function bindWhenReady(bind) {
  if (document.readyState === 'loading') window.addEventListener('DOMContentLoaded', bind, { once: true });
  else bind();
}
