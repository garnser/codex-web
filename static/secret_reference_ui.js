import { request } from './api_client.js';
import { captureProjectView, currentProjectId } from './project_view_scope.js';
import { projectPath } from './reference_navigation.js';
export { projectPath };

export const esc = value => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;');
export function secretPath(secretId = '', projectId = currentProjectId()) { return projectPath('secrets', { secret_id: secretId }, projectId); }
export function secretOperation(report) {
  const view = captureProjectView(); const project = currentProjectId();
  const assertCurrent = () => { if (!view.current()) throw new DOMException('Project changed', 'AbortError'); };
  return {
    current: view.current, status(message) { if (view.current()) report(message); },
    async request(path, options) {
      assertCurrent();
      if (path.startsWith('/api/secrets')) {
        if (!project) throw new Error('Select a Project to manage shared secret references');
        const url = new URL(path, location.origin); url.searchParams.set('project_id', project);
        path = url.pathname + url.search;
      }
      const response = await request(path, options); assertCurrent(); return response;
    },
  };
}
export function canManage(actor) {
  return ['mfa', 'local_trusted'].includes(actor?.assurance)
    && (actor.principal_kind === 'service'
      ? ['identity:admin', 'secret:admin'].every(scope => (actor.service_scopes || []).includes(scope))
      : (actor.roles || []).some(role => ['owner', 'admin'].includes(role)));
}
export function renderReferences(items, { manageable, identityText, timeText }) {
  return items.map(item => `<div class="comm-entry" data-secret-row="${esc(item.id)}">
    <strong>${esc(item.name)} · ${esc(item.status)}</strong>
    <small>ID: ${esc(item.id)} · Backend: ${esc(item.backend)} · Workspace owner: ${esc(item.organization_id)}/${esc(item.workspace_id)}</small>
    <small>Shared reference available in this Project; this is not a Project-owned copy. Project references: ${esc(item.project_reference_count ?? 'unknown')} · Shared references: ${esc(item.shared_reference_count ?? 'unknown')}.</small>
    <small>Provider: ${esc(item.provider || 'none')} · Purpose: ${esc(item.purpose || 'none')} · Owner: ${esc(identityText(item.owner_identity_id))}</small>
    <small>Use permission: ${item.use_allowed ? 'allowed for current actor' : 'not granted to current actor'} · ACL: ${esc((item.allowed_identity_ids || []).map(identityText).join(', ') || 'none')}</small>
    <small>Reveal permission ACL: ${esc((item.reveal_identity_ids || []).map(identityText).join(', ') || 'none')}. This API and UI never reveal stored values.</small>
    <small>Created: ${timeText(item.created_at)} · Expires: ${timeText(item.expires_at)} · Rotation: ${esc(item.rotation)} · Rotated: ${timeText(item.rotated_at)}</small>
    <div class="developer-toolbar"><button type="button" data-secret-usage="${esc(item.id)}">Inspect consumers</button>
      <a href="${esc(projectPath('work-items'))}">Bind / unbind TaskSource credential</a>
      <a href="${esc(projectPath('configuration'))}">Manage configuration references</a>
      <a href="${esc(secretPath(item.id))}">Link to this reference</a></div>
    <div data-secret-usage-result></div>
    ${item.status === 'active' && manageable ? `<div class="route-test">
      <label>Replacement value for ${esc(item.name)} <input type="password" autocomplete="new-password" data-secret-rotate-value></label>
      <button type="button" data-secret-rotate="${esc(item.id)}">Rotate</button>
      <button type="button" data-secret-revoke="${esc(item.id)}">Revoke</button></div>` : ''}
    ${item.revoked_at ? '<small>Revoked references remain visible for audit; create a replacement and update the consumer binding. Independent restore/delete is not supported.</small>' : ''}
  </div>`).join('') || '<p>No secret references visible in this workspace. An administrator can create a shared reference; existing values are never returned.</p>';
}
export function renderImpact(usage) {
  const pages = new Set(['configuration', 'work-items', 'integrations', 'agents', 'operations', 'runs', 'automations']);
  return `<p>${esc(usage.count)} reference(s), including ${esc(usage.outside_view_count)} outside this Project view. Rotation/revocation affects the shared reference across Projects.${usage.truncated ? ' Display is limited; totals include omitted references.' : ''}</p>
    ${(usage.items || []).map(item => `<div class="comm-entry"><strong>${esc(item.label)}</strong><small>${esc(item.object_id)} · ${esc(item.scope)} · ${esc(item.state || '')}${item.revision ? ` · r${esc(item.revision)}` : ''}</small>${pages.has(item.page) ? `<a href="${esc(projectPath(item.page, { consumer_type: item.object_type || '', consumer_id: item.object_id }))}">Manage consumer</a>` : ''}</div>`).join('')}
    <p>Coverage: ${esc((usage.coverage || []).join(', '))}.</p><p>${esc((usage.limitations || []).join(' '))}</p>`;
}

export function bindSecretLink(select, selectedId, projectId = currentProjectId()) {
  let manage = select.parentElement.querySelector('[data-manage-source-secret]');
  if (!manage) {
    manage = document.createElement('a'); manage.dataset.manageSourceSecret = '';
    manage.textContent = 'Manage Project Secrets'; select.parentElement.appendChild(manage);
  }
  manage.href = secretPath(selectedId, projectId);
  select.onchange = () => { manage.href = secretPath(select.value, projectId); };
}
