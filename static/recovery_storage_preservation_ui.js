import { esc } from './secret_reference_ui.js';
import { referenceLink } from './reference_navigation.js';
import { confirmAction } from './action_confirmation.js';
import { trackDirtyEditor } from './dirty_editor.js';

// Existing Recovery page, canonical APIs and confirmation; no browser policy or
// backup truth. An uncertain transport is observe-only, never an execute retry.
export function bindStoragePreservation(root, snapshot, op, status) {
  const section = document.createElement('section'); root.append(section);
  section.innerHTML = '<h3>Opaque storage preservation</h3><p>Preserves exact physical rows, including malformed payloads. Verified storage is not semantic Recovery health, a repair, or a live restore. No Recovery state-version activation.</p><p data-availability role="status">Reading current canonical availability…</p>';
  let editor = null; let disposed = false;
  const current = () => !disposed && op.current();
  const wrapper = { label: 'Storage preservation', dirty: () => editor?.dirty() || false, dispose() { disposed = true; editor?.dispose(); } };
  const availability = section.querySelector('[data-availability]');
  if (!snapshot.policy_control?.can_configure) {
    availability.textContent = 'Detailed storage diagnostics and actions require current Recovery administration and MFA. Ordinary history shows incomplete totals as unknown.';
    return wrapper;
  }
  void (async () => {
    try {
      const state = await op.request('/api/recovery/storage-preservation/status');
      if (!current()) return;
      availability.textContent = state.blockers?.length ? state.blockers.join('; ') : (state.boundary || 'Preservation availability is unqualified; no operation controls admitted.');
      if (state.schema_version !== '1.0' || !state.registered || state.blockers?.length || !state.bindings?.length || !state.configuration?.resource_id) return;
      section.insertAdjacentHTML('beforeend', `<form class="recovery-policy-form recovery-reconciliation-form" aria-label="Opaque storage preservation">
        <p>Whole-StateStore Resource <strong>${esc(state.configuration.resource_id)}</strong>. Effective budget ${esc(state.configuration.memory_bytes)} memory bytes / ${esc(state.configuration.disk_bytes)} disk bytes / ${esc(state.configuration.wall_seconds)} seconds. Actual enforcement requires independent current capacity Evidence.</p>
        <label>Operation<select name="action"><option value="recovery.storage-preservation.capture">Capture encrypted storage</option><option value="recovery.storage-preservation.verify">Verify in a fresh isolated same-backend target</option></select></label>
        <label>Existing binding<select name="binding">${state.bindings.map(b => `<option value="${esc(b.id)}">${esc(b.id)}</option>`).join('')}</select></label>
        <label>New operation ID<input name="operation" required maxlength="100" pattern="[a-zA-Z0-9_-]+"></label>
        <label>Stable idempotency key<input name="key" required maxlength="200"></label>
        <label>Independent capacity Evidence ID<input name="evidence" required maxlength="200"></label>
        <label>Independent verification ID<input name="verification" required maxlength="200"></label>
        <label>Preservation Artifact ID (required for isolated verification)<input name="artifact" maxlength="200"></label>
        <button type="button" data-storage-preview>Review current scope and budget</button><pre data-storage-impact aria-live="polite"></pre>
        <button type="submit" disabled>Submit once through canonical ActionIntent</button>
        <label>Existing ActionIntent ID<input name="intent" maxlength="200"></label>
        <button type="button" data-storage-observe>Observe existing intent without replay</button>
        <pre data-storage-result role="status"></pre><p data-storage-links></p></form>`);
      const form = section.querySelector('form'); editor = trackDirtyEditor(form, { label: 'Storage preservation' });
      const field = name => form.elements.namedItem(name);
      const submit = form.querySelector('[type="submit"]');
      const result = form.querySelector('[data-storage-result]');
      let reviewed = null; let busy = false; let attempted = false;
      const payload = () => ({ binding_id: field('binding').value, action: field('action').value, request: {
        operation_id: field('operation').value, resource_id: state.configuration.resource_id,
        expected_policy_fingerprint: snapshot.policy_control.expected_fingerprint,
        capacity_evidence_id: field('evidence').value, capacity_verification_id: field('verification').value,
        artifact_id: field('artifact').value || null, idempotency_key: field('key').value,
        confirm_opaque_preservation: true,
      } });
      const show = async value => {
        const item = value.item; field('intent').value = item.id;
        result.textContent = `ActionIntent ${item.id}: ${item.status}. Receipt ${item.last_receipt_id || 'unobserved'}; verification ${item.last_verification_id || 'unobserved'}. Storage integrity and semantic Recovery are separate.`;
        form.querySelector('[data-storage-links]').innerHTML = referenceLink('action_intent', item.id) + ' · ' + referenceLink('verification', item.last_verification_id);
        if (item.status !== 'succeeded') return;
        const receipt = await op.request('/api/recovery/storage-preservation/receipts/' + encodeURIComponent(item.id));
        if (!current()) return;
        result.textContent += '\n' + JSON.stringify(receipt, null, 2);
        form.querySelector('[data-storage-links]').insertAdjacentHTML('beforeend', (receipt.artifact_ids || []).map(id => ' · ' + referenceLink('artifact', id)).join('') + ' · ' + referenceLink('evidence', receipt.evidence_id));
      };
      form.addEventListener('input', () => { reviewed = null; submit.disabled = true; });
      form.querySelector('[data-storage-preview]').onclick = async () => {
        if (busy || attempted || !form.reportValidity()) return;
        busy = true; submit.disabled = true;
        const frozen = JSON.stringify(payload());
        try {
          const value = await op.request('/api/recovery/storage-preservation/preview', { method: 'POST', body: frozen });
          if (!current() || frozen !== JSON.stringify(payload())) return;
          form.querySelector('[data-storage-impact]').textContent = JSON.stringify(value, null, 2);
          reviewed = frozen; submit.disabled = false;
        } catch (error) { if (current()) result.textContent = error.message; }
        finally { busy = false; }
      };
      form.onsubmit = async event => {
        event.preventDefault();
        if (busy || attempted || !reviewed || reviewed !== JSON.stringify(payload()) || !form.reportValidity()) return;
        const frozen = reviewed;
        if (!await confirmAction({ action: 'Preserve or verify opaque storage', target: state.configuration.resource_id, risk: 'high',
          consequence: 'Reads every physical StateStore row into restricted encrypted Artifact content, or writes only a fresh isolated verification target. Malformed bytes remain unchanged.',
          impact: form.querySelector('[data-storage-impact]').textContent,
          recovery: 'No source repair, semantic Restore PASS, state1.3 activation or release/upgrade qualification. Unknown outcomes must be observed without recapture.',
          current: () => current() && !attempted && frozen === JSON.stringify(payload()), trigger: submit })) return;
        if (!current() || attempted || frozen !== JSON.stringify(payload())) return;
        busy = attempted = true; submit.disabled = true;
        try { const value = await op.request('/api/recovery/storage-preservation/operations', { method: 'POST', body: frozen }); if (current()) await show(value); }
        catch (error) { if (current()) result.textContent = 'Outcome unknown or denied: ' + error.message + '. Inspect existing ActionIntents; do not submit another capture.'; }
        finally { busy = false; if (current()) status('Storage operation attempted; inspect canonical receipt and semantic status separately.'); }
      };
      form.querySelector('[data-storage-observe]').onclick = async () => {
        if (busy || !field('intent').value || !field('operation').value) return;
        busy = true;
        try { const value = await op.request(`/api/recovery/storage-preservation/operations/${encodeURIComponent(field('operation').value)}/intents/${encodeURIComponent(field('intent').value)}/observe`, { method: 'POST' }); if (current()) await show(value); }
        catch (error) { if (current()) result.textContent = error.message; }
        finally { busy = false; }
      };
    } catch (error) { if (current()) availability.textContent = 'Storage preservation unavailable or denied: ' + error.message; }
  })();
  return wrapper;
}
