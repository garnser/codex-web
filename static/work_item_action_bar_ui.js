import { request } from './api_client.js';

// Existing operator actions and explicit routing inspection share one action bar.
export function workItemActionBarHtml(item, actions) {
  return `<div class="work-item-action-bar">
        <button type="button" class="ghost-button work-item-retry" ${actions.retry?.allowed ? '' : 'disabled'}>Retry</button>
        <button type="button" class="ghost-button work-item-reconcile" ${actions.reconcile?.allowed ? '' : 'disabled'}>Reconcile</button>
        ${item.handoff?.status === 'pending' ? '<button type="button" class="ghost-button work-handoff-inspect">Inspect handoff routing</button>' : ''}
      </div>`;
}

export function installWorkItemActionBar(detail, item, { runItemAction, captureScope, scopedPath, pathRef }) {
  detail.querySelector('.work-item-retry')?.addEventListener('click', () => runItemAction('retry'));
  detail.querySelector('.work-item-reconcile')?.addEventListener('click', () => runItemAction('reconcile'));
  detail.querySelector('.work-handoff-inspect')?.addEventListener('click', async event => {
    const isCurrent = captureScope();
    const output = detail.querySelector('.work-handoff-inspection');
    event.target.disabled = true;
    output.textContent = 'Inspecting current handoff routing…';
    try {
      const query = new URLSearchParams({ expected_requested_at: String(item.handoff.requested_at), expected_recipient: item.handoff.to_agent });
      const result = await request(scopedPath(`/api/work-items/${pathRef(item.ref)}/operator/handoff-eligibility?${query}`));
      if (!isCurrent()) return;
      output.textContent = `Routing inspection: ${result.status}${result.blocked_reason ? ` · ${result.blocked_reason}` : ''}${result.current_thread_id ? ` · recipient ${result.current_thread_id}` : ''}. Inspection only; no input dispatched or admission granted.`;
    } catch (error) {
      if (isCurrent()) output.textContent = error.message || 'Handoff routing inspection unavailable';
    } finally {
      if (isCurrent()) event.target.disabled = false;
    }
  });
}
