import { request } from './api_client.js';
import { captureProjectView, currentProjectId } from './project_view_scope.js';
import { esc, projectPath } from './secret_reference_ui.js';

export { esc };

export function keyOperation(report) {
  const view = captureProjectView(); const project = currentProjectId();
  const assertCurrent = () => { if (!view.current()) throw new DOMException('Project changed', 'AbortError'); };
  return { current: view.current, status(message) { if (view.current()) report(message); },
    async request(path, options) {
      assertCurrent();
      if (project) {
        const url = new URL(path, location.origin); url.searchParams.set('project_id', project);
        path = url.pathname + url.search;
      }
      const result = await request(path, options); assertCurrent(); return result;
    },
  };
}
export function keyLink(keyId, version) {
  const query = { key_id: keyId, ...(version ? { key_version: version } : {}) };
  const target = currentProjectId() ? projectPath('configuration', query) : `?${new URLSearchParams(query)}`;
  return `<a href="${esc(target)}" data-key-link="${esc(keyId)}">${esc(keyId)}${version ? ` v${esc(version)}` : ''}</a>`;
}
export function focusKey(host) {
  const query = new URLSearchParams(location.search); const id = query.get('key_id');
  if (!id || !host) return;
  const row = [...host.querySelectorAll('[data-crypto-key-row]')].find(item => item.dataset.cryptoKeyRow === id);
  if (row) { row.tabIndex = -1; row.focus(); row.scrollIntoView({ block: 'nearest' }); }
}
export async function inspectKeyUsage(row, operation, { retire = false, version } = {}) {
  const host = row?.querySelector('[data-key-usage-result]'); const id = row?.dataset.cryptoKeyRow;
  if (!host || !id) return false;
  host.textContent = 'Loading current canonical key dependencies…';
  try {
    const usage = await operation.request(`/api/crypto/keys/${encodeURIComponent(id)}/usage${version ? `?version=${encodeURIComponent(version)}` : ''}`);
    if (!operation.current() || !row.isConnected) return false;
    if (usage.available !== true || !Number.isInteger(usage.blocking_count)) throw new Error('Dependency impact is unavailable');
    host.innerHTML = `<p>${esc(usage.count)} canonical dependency reference(s). ${usage.truncated ? 'Display is limited; totals include omitted references.' : ''}</p>
      ${(usage.items || []).map(item => `<div class="comm-entry" data-key-consumer="${esc(item.object_id)}"><strong>${esc(item.label)} · ${esc(item.object_id)}</strong>
        <small>${esc(item.relationship)} · ${esc(item.scope)} · ${item.broken ? 'Broken: required key/version is unavailable. Repair the owning recovery reference and verify restore.' : 'Required key versions retained'}</small>
        <small>Key metadata: ${(item.versions || []).map(number => keyLink(id, number)).join(', ')}</small></div>`).join('')}
      <p>Coverage: ${esc((usage.coverage || []).join(', '))}</p><p>${esc((usage.limitations || []).join(' '))}</p>`;
    if (retire && usage.blocking_count > 0) {
      operation.status('Revocation blocked: canonical recovery consumers still require this key/version. Retain it or migrate references through recovery management and verify restore. Rotation preserves decrypt-only versions.');
      return false;
    }
    operation.status('Current dependencies loaded. External copies and unregistered encrypted domains are not included.');
    return true;
  } catch (error) {
    if (operation.current() && row.isConnected) { host.textContent = `Dependency impact unavailable: ${error.message}`; operation.status('Key action blocked until dependency impact is available.'); }
    return false;
  }
}

export async function runKeyAction(button, action) {
  const row = button.closest('[data-crypto-key-row]');
  if (!row || row.dataset.keyActionPending) return;
  row.dataset.keyActionPending = 'true';
  const buttons = [...row.querySelectorAll('button')].map(node => [node, node.disabled]);
  buttons.forEach(([node]) => { node.disabled = true; });
  try { await action(); }
  finally { delete row.dataset.keyActionPending; buttons.forEach(([node, disabled]) => { node.disabled = disabled; }); }
}
