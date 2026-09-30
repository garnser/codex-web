import { currentProjectId } from './project_view_scope.js';

const escape = value => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;');
// Structural UI routes, not an authority or mutable domain-definition catalog.
const targets = {
  secret: ['secrets', 'secret_id'], key: ['configuration', 'key_id'],
  execution_profile: ['definitions', 'execution_profile'], definition: ['definitions', 'definition_record'],
  agent_provider: ['operations', 'provider_id'], model_provider: ['operations', 'model_provider_id'],
  identity: ['organization'], session: ['operations'], model: ['agents'], invocation: ['agents'],
  goal: ['goals'], decision: ['decisions'], work_item: ['work-items'],
  agent_profile: ['agent-profiles'], team: ['teams'], skill: ['skills', 'skill_id'],
  configuration: ['configuration'], configuration_key: ['configuration', 'configuration_key'], definition_family: ['definitions', 'definition_id'], worker: ['workers'], assignment: ['workers'],
  resource: ['resources'], extension: ['integrations'], action_provider: ['integrations'],
  recovery_policy: ['operations'], backup: ['operations'],
};

export function projectPath(page, query = {}, projectId = currentProjectId()) {
  if (!projectId || !/^[a-z][a-z-]*$/.test(page)) return null;
  const prefix = location.pathname.startsWith('/codex/') ? '/codex' : '';
  return `${prefix}/projects/${encodeURIComponent(projectId)}/${page}?${new URLSearchParams(query)}`;
}

export function referencePath(kind, id, { projectId = currentProjectId(), revision } = {}) {
  const target = Object.hasOwn(targets, kind) && targets[kind];
  if (!target || !id) return null;
  const [page, key] = target;
  const query = key ? { [key]: id } : { consumer_type: kind, consumer_id: id };
  if (Number.isInteger(revision) && revision > 0) {
    if (kind === 'skill') query.skill_revision = revision;
    if (['configuration_key', 'definition_family'].includes(kind)) query.reference_revision = revision;
  }
  return projectPath(page, query, projectId);
}

export function referenceLink(kind, id, { label = id, readOnlyReason = '', ...options } = {}) {
  if (!id) return '—';
  const path = referencePath(kind, id, options);
  const reason = readOnlyReason || (!path ? 'Inspection only: no object editor is available in this Project context.' : '');
  return `${path ? `<a data-managed-reference href="${escape(path)}" aria-label="View ${escape(kind.replaceAll('_', ' '))}: ${escape(label)}">${escape(label)}</a>` : escape(label)}${reason ? ` <small>${escape(reason)}</small>` : ''}`;
}

export function requestedReference(kind, projectId = currentProjectId()) {
  const route = location.pathname.match(/\/projects\/([^/]+)(?:\/|$)/);
  if (route) { try { if (decodeURIComponent(route[1]) !== projectId) return ''; } catch { return ''; } }
  const query = new URLSearchParams(location.search);
  return query.get('consumer_type') === kind ? query.get('consumer_id') || '' : '';
}

export function rememberReference(kind, id) {
  const url = new URL(location.href);
  url.searchParams.set('consumer_type', kind); url.searchParams.set('consumer_id', id);
  history.replaceState(history.state, '', url);
}

export function referenceNode(kind, id, options = {}) {
  const node = document.createElement('span'); node.innerHTML = referenceLink(kind, id, options); return node;
}

// Preserve only typed object identifiers, never editor values or arbitrary return URLs.
// Normal anchors retain browser Back behavior and the shared dirty/unload guard.
document.addEventListener('click', event => {
  const link = event.target.closest?.('a[data-managed-reference]');
  const source = link?.closest('[data-reference-kind][data-reference-id]');
  if (!source || event.defaultPrevented) return;
  rememberReference(source.dataset.referenceKind, source.dataset.referenceId);
});
