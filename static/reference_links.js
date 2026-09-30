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
  if (!host) return;
  const query = new URLSearchParams(location.search);
  const kind = query.get('consumer_type'); const id = query.get('consumer_id');
  const family = query.get("definition_id"); const key = query.get("configuration_key");
  const revision = query.get("reference_revision");
  const logical = [...host.querySelectorAll("[data-definition-id],[data-configuration-key]")].find(node =>
    ((family && node.dataset.definitionId === family) || (key && node.dataset.configurationKey === key))
    && (!revision || node.dataset.referenceRevision === revision));
  const row = logical || [...host.querySelectorAll('[data-reference-kind]')].find(node => node.dataset.referenceKind === kind && node.dataset.referenceId === id);
  if (row) {
    for (let parent = row; parent && parent !== host; parent = parent.parentElement) if (parent.tagName === 'DETAILS') parent.open = true;
    row.tabIndex = -1; row.focus(); row.scrollIntoView({ block: 'nearest' });
  }
}

export function bindWhenReady(bind) {
  if (document.readyState === 'loading') window.addEventListener('DOMContentLoaded', bind, { once: true });
  else bind();
}
