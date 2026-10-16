import { request as apiRequest } from './api_client.js';
import { currentProjectId } from './project_view_scope.js';
import { mergeRequirementsHtml } from './merge_requirements_view.js';

export function mountMergeRequirements({panel, item, projectId, record, panels, pending, viewGeneration, generation}) {
  const form = panel.querySelector('[data-merge-requirements]');
  if (!form) return;
  const results = form.querySelector('[data-merge-results]');
  const status = form.querySelector('[data-merge-status]');
  let expiryTimer;
  form.addEventListener('input', () => {
    clearTimeout(expiryTimer); record.controller?.abort(); record.controller = null; results.replaceChildren();
    form.querySelector('button').disabled = false; results.removeAttribute('aria-busy');
    status.textContent = 'Selection changed. Inspect current requirements for this scope.';
  });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    record.controller?.abort();
    clearTimeout(expiryTimer);
    const controller = new AbortController();
    record.controller = controller; pending.add(controller);
    const repository = form.elements.repository.value;
    const externalId = form.elements.pull_request.value;
    const owned = () => generation() === viewGeneration && projectId === currentProjectId()
      && panels.get(item.id) === record && record.controller === controller && panel.isConnected;
    const button = form.querySelector('button');
    button.disabled = true; results.replaceChildren(); results.setAttribute('aria-busy', 'true');
    status.textContent = 'Reading current merge requirements…';
    try {
      const query = new URLSearchParams({assignment_id:item.id, project_id:projectId,
        repository_resource_id:repository, external_id:externalId});
      const response = await apiRequest(`/api/control-plane-broker/merge-requirements?${query}`, {signal:controller.signal});
      if (!owned()) return;
      if (response.scope?.project_id !== projectId || response.scope?.assignment_id !== item.id
          || response.scope?.repository_id !== repository || response.item?.resource_id !== repository
          || response.item?.external_id !== externalId || response.scope?.inspection_kind !== 'operator') {
        throw new Error('Mismatched requirements scope');
      }
      results.innerHTML = mergeRequirementsHtml(response, projectId);
      status.textContent = 'Read complete. Inspect source coverage, freshness and provider gates below.';
      expiryTimer = setTimeout(() => {
        if (generation() !== viewGeneration || projectId !== currentProjectId()
            || panels.get(item.id) !== record || !panel.isConnected) return;
        results.innerHTML = mergeRequirementsHtml(response, projectId);
        status.textContent = 'Merge requirements are stale. Refresh this read before deciding.';
      }, Math.max(0, Math.min(30001, response.item.expires_at * 1000 - Date.now() + 1)));
    } catch (error) {
      if (owned()) {
        results.replaceChildren();
        status.textContent = `${[401,403].includes(error.status) ? 'Merge requirements access denied' : 'Merge requirements unavailable'}: ${error.message}. Refresh the current assignment or request authorized review. No merge permission inferred.`;
      }
    } finally {
      pending.delete(controller);
      if (record.controller === controller) { button.disabled = false; results.removeAttribute('aria-busy'); }
    }
  });
}
