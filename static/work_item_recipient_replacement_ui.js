import { confirmAction } from './action_confirmation.js';

const pending = new Set();

// The Work selector comes from the current inspector. Only the canonical
// eligibility and replacement owners authorize changing recipient custody.
export function installAcceptedRecipientReplacement(detail, item, {
  request, scopedPath, captureScope, reload, projectId, ownerDispatch,
}) {
  const handoff = item.handoff;
  const currentWork = !handoff && ownerDispatch?.recipient_replacement;
  const threadId = currentWork?.thread_id || handoff?.delivery?.thread_id;
  if ((!currentWork && handoff?.status !== 'accepted') || !item.ref || !threadId) return;
  const anchor = detail.querySelector('.work-handoff-inspection');
  if (!anchor) return;
  const host = document.createElement('div');
  host.className = 'work-accepted-recipient-replacement';
  anchor.after(host);
  const key = JSON.stringify([projectId, item.ref, handoff?.requested_at, threadId]);
  const action = currentWork ? 'Replace current Work recipient context' : 'Replace accepted recipient context';
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'ghost-button';
  button.textContent = action;
  button.disabled = pending.has(key);
  const output = document.createElement('span');
  output.setAttribute('role', 'status');
  output.setAttribute('aria-live', 'polite');
  host.append(button, output);
  button.addEventListener('click', async () => {
    if (pending.has(key)) return;
    const currentScope = captureScope();
    const current = () => currentScope() && host.isConnected;
    if (!current()) return;
    // Capture both Project-scoped URLs before awaiting any read or confirmation.
    const query = new URLSearchParams({ work_item_ref: item.ref });
    const root = `/api/threads/${encodeURIComponent(threadId)}`;
    const inspectPath = scopedPath(`${root}/replacement-eligibility?${query}`);
    const replacePath = scopedPath(`${root}/replace?${query}`);
    pending.add(key);
    button.disabled = true;
    output.textContent = 'Checking canonical recipient replacement eligibility…';
    let attempted = false;
    try {
      const proof = await request(inspectPath);
      if (!current()) return;
      if (proof.eligible !== true || proof.work_item_ref !== item.ref
          || proof.threadId !== threadId || !proof.currentThreadId) {
        throw new Error(proof.code || 'Recipient replacement is not available');
      }
      const confirmed = await confirmAction({
        action,
        target: `${item.ref} · ${handoff?.to_agent || item.current_owner || ''} · ${proof.currentThreadId}`,
        consequence: currentWork
          ? 'Join the exact owned predecessor and create one replacement context. Retained inputs and unknown outcomes remain recorded; this does not replay them.'
          : 'Create a replacement native context through canonical custody guards. The accepted handoff and retained history remain recorded.',
        current, trigger: button,
      });
      if (!confirmed || !current()) return;
      attempted = true;
      output.textContent = 'Requesting recipient replacement…';
      const result = await request(replacePath, { method: 'POST' });
      if (!current()) return;
      if (result.ok !== true || result.work_item_ref !== item.ref
          || !result.oldThreadId || !result.newThreadId) {
        throw new Error('Replacement response is incomplete; inspect canonical custody before another request');
      }
      // Unbound recipients have no BotBinding. Refresh Work instead of assuming
      // a binding exists or changing the separately selected Thread view.
      output.textContent = `Recipient context: ${result.oldThreadId} → ${result.newThreadId}. Refreshing Work…`;
      await reload(item.ref);
    } catch (error) {
      if (current()) {
        const code = error.detail?.code;
        output.textContent = `${code ? `${code}: ` : ''}${error.message || 'Replacement unavailable'}`
          + (attempted ? ' No automatic retry. Inspect current Work and recipient custody before another request.' : '');
      }
    } finally {
      pending.delete(key);
      // A POST is never retried from this rendered action, including unknown
      // outcomes. An operator may inspect fresh canonical state separately.
      if (current() && !attempted) button.disabled = false;
    }
  });
}
