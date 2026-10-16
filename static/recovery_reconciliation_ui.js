import { referenceLink } from './reference_navigation.js';
import { esc } from './secret_reference_ui.js';
import { confirmAction } from './action_confirmation.js';
import { trackDirtyEditor } from './dirty_editor.js';

export function bindRecoveryReconciliation(root, snapshot, op, status) {
  const control = snapshot.scheduled_work?.operator_control;
  const bindings = (control?.bindings || []).filter(item => item.enabled === true);
  const firings = (snapshot.scheduled_work?.firings || []).filter(item => ['running', 'unknown'].includes(item.status));
  const section = document.createElement('section');
  root.append(section);
  section.innerHTML = '<h3>Scheduled owner reconciliation</h3><p>No effect replay. UNKNOWN remains UNKNOWN. Releasing custody is not fresh Recovery qualification.</p>';
  if (control?.blockers?.length) {
    const note = document.createElement('p'); note.textContent = control.blockers.join('; '); section.append(note);
  }
  if (!snapshot.policy_control?.can_configure || control?.registered !== true || !bindings.length || !firings.length) {
    const note = document.createElement('p');
    note.textContent = 'Requires an authenticated Recovery administrator, an explicitly configured/enabled canonical recovery provider binding, and a scoped unresolved firing. No binding, authority or activation is created here.';
    section.append(note);
    return null;
  }
  section.insertAdjacentHTML('beforeend', `<form class="recovery-policy-form recovery-reconciliation-form" aria-label="Evidence-bound Recovery reconciliation">
    <label>Original firing<select name="event">${firings.map(item => `<option value="${esc(item.event_id)}">${esc(item.event_id)} · ${esc(item.status)} · ${esc(item.owner_id)}</option>`).join('')}</select></label>
    <label>Existing provider binding<select name="binding">${bindings.map(item => `<option value="${esc(item.id)}">${esc(item.id)}</option>`).join('')}</select></label>
    <label>Independent disposition Evidence reference<input name="evidence" required maxlength="200"></label>
    <label>Independent verification reference<input name="verification" required maxlength="200"></label>
    <label>Stable idempotency key<input name="key" required maxlength="200"></label>
    <label><input type="checkbox" name="activate"> Explicitly activate persisted Recovery1.3, only with qualified backup/restore and complete original-writer fencing. Older1.2 writers will refuse; no source-only rollback.</label>
    <button type="button" data-preview>Read current canonical snapshot</button>
    <pre data-snapshot aria-live="polite"></pre>
    <button type="submit" disabled>Submit guarded reconciliation</button>
    <label>Existing ActionIntent ID for read-only outcome reconciliation<input name="intent" maxlength="200"></label>
    <button type="button" data-observe>Observe existing intent without replay</button>
    <pre data-result role="status"></pre><p data-result-links></p></form>`);
  const form = section.querySelector('form');
  const field = name => form.elements.namedItem(name);
  const editor = trackDirtyEditor(form, { label: 'Recovery reconciliation' });
  const submit = form.querySelector('[type="submit"]');
  const result = form.querySelector('[data-result]');
  let preview = null;
  let busy = false;
  let attempted = false;
  const url = () => `/api/recovery/scheduled-firings/${encodeURIComponent(field('event').value)}`;
  form.addEventListener('input', () => { submit.disabled = attempted || busy || !preview; });
  field('event').addEventListener('change', () => { preview = null; submit.disabled = true; });
  form.querySelector('[data-preview]').onclick = async () => {
    if (busy || attempted) return;
    busy = true; submit.disabled = true;
    try {
      const firing = field('event').value;
      const value = await op.request(url() + '/reconciliation-preview');
      if (!op.current() || field('event').value !== firing || value.event_id !== firing) return;
      preview = value;
      form.querySelector('[data-snapshot]').textContent = JSON.stringify(value, null, 2);
    } catch (error) { preview = null; if (op.current()) result.textContent = error.message; }
    finally { busy = false; if (op.current()) submit.disabled = attempted || !preview; }
  };
  const show = value => {
    const item = value.item;
    field('intent').value = item.id;
    result.textContent = `ActionIntent ${item.id}: ${item.status}\nReceipt ${item.last_receipt_id || 'unobserved'} · verification ${item.last_verification_id || 'unobserved'}\nThis is custody disposition, not effect success or Recovery qualification. UNKNOWN outcomes require observation; do not retry effects.`;
    const history = referenceLink('action_intent', item.id, { label: `ActionIntent ${item.id} and receipt ${item.last_receipt_id || 'unobserved'} history` });
    form.querySelector('[data-result-links]').innerHTML = history + ' · ' + referenceLink('verification', item.last_verification_id);
    status('Canonical Recovery intent observed; original effect/health must be inspected separately.');
  };
  form.onsubmit = async event => {
    event.preventDefault();
    if (busy || attempted || !preview || !form.reportValidity()) return;
    if (preview.activation_required && !field('activate').checked) {
      result.textContent = 'Explicit qualified Recovery1.3 activation is required; no mutation submitted.';
      return;
    }
    const payload = { binding_id: field('binding').value, request: {
      event_id: preview.event_id, expected_state_sha256: preview.expected_state_sha256,
      expected_firing_sha256: preview.expected_firing_sha256, expected_schedule_sha256: preview.expected_schedule_sha256,
      expected_policy_fingerprint: preview.expected_policy_fingerprint,
      evidence_id: field('evidence').value, verification_id: field('verification').value,
      idempotency_key: field('key').value, activate_state_1_3: field('activate').checked,
    } };
    const frozen = JSON.stringify(payload);
    if (!await confirmAction({ action: 'Reconcile scheduled Recovery custody', target: preview.event_id,
      risk: 'high', consequence: 'Evidence/CAS-bound custody release; original UNKNOWN is preserved. First persisted1.3 write is one-way.',
      recovery: 'No effect replay or old1.2 source-only rollback. Fresh backup and isolated restore still required.',
      current: () => op.current() && field('event').value === payload.request.event_id && field('binding').value === payload.binding_id
        && field('evidence').value === payload.request.evidence_id && field('verification').value === payload.request.verification_id
        && field('key').value === payload.request.idempotency_key && field('activate').checked === payload.request.activate_state_1_3,
      trigger: submit })) return;
    if (busy || attempted) return;
    attempted = true; busy = true; submit.disabled = true;
    try { const value = await op.request(url() + '/reconcile', { method: 'POST', body: frozen }); if (op.current()) show(value); }
    catch (error) { if (op.current()) result.textContent = `${error.message}\nIf response was lost, inspect the SAME idempotency key/intent. Outcome is unknown; do not mint a replacement key or replay.`; }
    finally { busy = false; /* Keep submit disabled after an attempted mutation; use explicit observation. */ }
  };
  form.querySelector('[data-observe]').onclick = async () => {
    if (busy || !field('intent').value) return;
    busy = true;
    try {
      const value = await op.request(url() + `/reconciliation-intents/${encodeURIComponent(field('intent').value)}/observe`, { method: 'POST', body: '{}' });
      if (op.current()) show(value);
    } catch (error) { if (op.current()) result.textContent = error.message; }
    finally { busy = false; }
  };
  return editor;
}
