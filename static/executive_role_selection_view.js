import { esc } from './reference_links.js';
import { referenceLink } from './reference_navigation.js';

export function executiveRoleSelectionHtml(context, projectId) {
  if (!context) return '';
  const ref = context.definition;
  const definition = ref
    ? `${referenceLink('definition', ref.record_id, {projectId})} · ${esc(ref.definition_id)}@r${esc(ref.revision)}`
    : 'unavailable';
  return `<small>Executive role selection: ${esc(context.status)} · current published definition ${definition} · maximum selected roles ${esc(context.max_roles_per_activation ?? 'unavailable')}.</small>
    <small>Selection data grants no operation authority; current scope and authority are checked on use.${context.reason ? ` ${esc(context.reason)}` : ''}</small>
    ${Array.isArray(context.roles) ? `<pre>${esc(JSON.stringify(context.roles, null, 2))}</pre>` : ''}`;
}
