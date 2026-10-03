import { confirmAction } from './action_confirmation.js';
import './page_editor.js';
import { request } from './api_client.js';
import { captureProjectView, currentProjectId } from './project_view_scope.js';
import { esc } from './secret_reference_ui.js';
import { keyLink } from './key_usage_ui.js';
import { referenceAttributes, focusReference, bindWhenReady } from './reference_links.js';
import { trackDirtyEditor, confirmDiscard } from './dirty_editor.js';
import { formValidation } from './form_validation.js';
import { renderPolicyForm, readPolicyForm } from './recovery_policy_form.js';

const style = document.createElement('link'); style.rel = 'stylesheet'; style.href = new URL('./recovery_policy.css', import.meta.url).href; document.head.appendChild(style);

let editor = null; let generation = 0;
function operation() {
  const view = captureProjectView(); const project = currentProjectId(); const opened = generation;
  const current = () => view.current() && opened === generation;
  return { current, async request(path, options) {
    if (!current()) throw new DOMException('View changed', 'AbortError');
    const url = new URL(path, location.origin); if (project) url.searchParams.set('project_id', project);
    const result = await request(url.pathname + url.search, options);
    if (!current()) throw new DOMException('View changed', 'AbortError'); return result;
  } };
}
function status(text) { const host = document.getElementById('recovery-policy-status'); if (host) host.textContent = text; }
function impactText(impact) {
  return `<p>Changed fields: ${esc(impact.changed_fields.join(', ') || 'none')}. Shared workspace: ${esc(impact.organization_id)}/${esc(impact.workspace_id)}.</p><p>${esc(impact.retained_backup_count)} retained backups; ${esc(impact.backups_over_new_retention)} currently exceed the proposed retention count. Retention is enforced by subsequent backups.</p><p>${esc(impact.schedule_effect)}</p><p>${esc(impact.application)}</p><p>${esc(impact.rollback_limit)}</p>`;
}
function evidence(snapshot) {
  const policy = snapshot.policy; const health = snapshot.health || {};
  return `<section><h3>Runtime evidence — read-only</h3><p>Recovery qualified: ${health.recovery_qualified ? 'yes' : 'no'}. ${esc((health.blockers || []).join(' · '))}</p>
    <p>Policy fingerprint: ${esc(snapshot.policy_control.expected_fingerprint)}. Automatic scheduler: ${snapshot.policy_control.scheduler_available ? 'attached' : 'unavailable'}.</p>
    ${(snapshot.policy_control.schedules || []).map(item => `<p>Schedule ${esc(item.kind)}: ${esc(item.status)} · ${esc(item.id || 'none')} · revision ${esc(item.revision || 'none')} · interval ${esc(item.interval_seconds ?? 'unknown')} seconds · next run ${item.next_run_at ? esc(new Date(item.next_run_at * 1000).toISOString()) : 'not scheduled'}</p>`).join('')}
    ${policy ? `<div ${referenceAttributes('recovery_policy', policy.id)}><strong>${esc(policy.id)} · version ${esc(policy.version)}</strong><p>Backup key: ${keyLink(policy.backup_key_id)}</p></div>` : '<p>No policy has been configured.</p>'}
    ${(snapshot.backups || []).slice(0, 100).map(item => `<div class="comm-entry" ${referenceAttributes('backup', item.id)}><strong>${esc(item.id)} · immutable backup metadata</strong><small>Policy ${esc(item.policy_id)} / ${esc(item.policy_version)} · ${esc(item.policy_fingerprint)}</small><small>Encrypted by ${keyLink(item.key_id, item.key_version)}</small><small>Frozen restore requirements: ${(item.key_manifest || []).flatMap(entry => entry.versions.map(version => keyLink(entry.key_id, version))).join(', ') || 'none'}</small></div>`).join('') || '<p>No retained backups in this tenant.</p>'}
    ${(snapshot.verifications || []).slice(0, 100).map(item => `<div class="comm-entry"><strong>Restore verification ${esc(item.status)} · ${esc(item.backup_id)}</strong><small>Evidence: ${esc(item.evidence_id || 'none')} · ${esc((item.blockers || []).join(' · '))}</small><small>Missing key refs: ${esc((item.key_missing_refs || []).join(', ') || 'none')} · Revoked key refs: ${esc((item.key_revoked_refs || []).join(', ') || 'none')}</small></div>`).join('')}
    <p>Only the first 100 backups/verifications are displayed. Destination paths, encrypted envelopes and key material are never rendered.</p></section>`;
}
async function load(force = false) {
  const root = document.getElementById('recovery-policy-content'); if (!root) return;
  if (!force && !confirmDiscard(editor)) return;
  generation++; editor?.dispose(); editor = null; root.replaceChildren(); status('Loading canonical recovery policy…');
  const op = operation();
  try {
    const snapshot = await op.request('/api/recovery/status');
    let keys = [];
    if (snapshot.policy_control.can_configure) keys = (await op.request('/api/crypto/keys')).items || [];
    if (!op.current()) return;
    root.innerHTML = renderPolicyForm(snapshot, keys) + evidence(snapshot) + '<section data-recovery-history><h3>Policy change history</h3></section>';
    const form = root.querySelector('form'); const validation = formValidation(form);
    editor = trackDirtyEditor(form, { label: 'Recovery policy' }); const ownedEditor = editor;
    const preview = form.querySelector('[data-recovery-impact]'); const save = form.querySelector('[data-recovery-save]');
    let reviewed = null; let busy = false;
    form.addEventListener('input', () => { reviewed = null; save.disabled = true; });
    form.querySelector('[data-recovery-discard]').onclick = () => { if (confirmDiscard(ownedEditor)) void load(true); };
    form.querySelector('[data-recovery-preview]').onclick = async () => {
      if (busy || !validation.validate()) return;
      busy = true; reviewed = null; save.disabled = true;
      const payload = readPolicyForm(form, snapshot); const submitted = JSON.stringify(payload);
      try {
        const impact = await op.request('/api/recovery/policy/preview', { method: 'POST', body: submitted });
        if (!op.current() || JSON.stringify(readPolicyForm(form, snapshot)) !== submitted) return;
        if (impact.available !== true) throw new Error('Dependency impact unavailable');
        preview.innerHTML = impactText(impact); reviewed = { impact, submitted }; save.disabled = false;
      } catch (error) { if (op.current()) { validation.server(error); status('Impact unavailable; values retained.'); } }
      finally { busy = false; }
    };
    form.onsubmit = async event => {
      event.preventDefault();
      if (busy || !reviewed || !validation.validate()) return;
      if (JSON.stringify(readPolicyForm(form, snapshot)) !== reviewed.submitted) { save.disabled = true; return; }
      if (!await confirmAction({ action: 'Publish recovery policy', target: `${snapshot.policy?.id || 'recovery-policy-default'} · ${snapshot.policy_control.organization_id}/${snapshot.policy_control.workspace_id}`, risk: 'high',
        consequence: 'Subsequent backup, retention and verification behavior changes.', impact: preview.textContent,
        recovery: 'Policy history supports rollback after a fresh impact preview. Rollback cannot restore deleted backup bytes or revoked keys.', current: () => op.current() && reviewed && JSON.stringify(readPolicyForm(form, snapshot)) === reviewed.submitted, trigger: save })) return;
      busy = true; save.disabled = true; const submitted = ownedEditor.snapshot();
      try {
        await op.request('/api/recovery/policy?expected_fingerprint=' + encodeURIComponent(reviewed.impact.expected_fingerprint), { method: 'PUT', body: reviewed.submitted });
        ownedEditor.markSaved(submitted);
        if (op.current() && !ownedEditor.dirty()) await load(true);
        else if (op.current()) status('Policy published; newer unsaved edits are retained. Preview newer edits before publishing again.');
      } catch (error) { if (op.current()) { validation.server(error); status('Publication failed or conflicted; values retained. Reload current state before retrying.'); } }
      finally { busy = false; reviewed = null; }
    };
    const history = root.querySelector('[data-recovery-history]');
    for (const revision of snapshot.policy_control.history || []) {
      const row = document.createElement('div'); row.className = 'comm-entry';
      row.innerHTML = `<strong>${esc(revision.id)}</strong><small>Actor ${esc(revision.actor_id)} · version ${esc(revision.policy.version)} · ${esc(new Date(revision.occurred_at * 1000).toISOString())}</small><small>${revision.restored_from_id ? `Restored from ${esc(revision.restored_from_id)}` : 'Published policy revision'}</small>`;
      if (snapshot.policy_control.can_configure) {
        const button = document.createElement('button'); button.type = 'button'; button.textContent = 'Review rollback'; row.append(button);
        button.onclick = async () => {
          if (busy || !confirmDiscard(ownedEditor)) return;
          busy = true; button.disabled = true;
          try {
            const impact = await op.request('/api/recovery/policy/preview', { method: 'POST', body: JSON.stringify(revision.policy) });
            if (!op.current()) return;
            if (impact.available !== true) throw new Error('Dependency impact unavailable');
            preview.innerHTML = impactText(impact);
            button.textContent = 'Confirm policy rollback'; button.disabled = false;
            button.onclick = async () => {
              if (busy || !op.current() || !confirmDiscard(ownedEditor)) return;
              if (!await confirmAction({ action: 'Restore policy revision', target: revision.id, risk: 'high', consequence: 'A new policy revision is published from this historical policy.', impact: preview.textContent, recovery: 'This cannot recover deleted backups, undo completed operations or restore revoked keys.', current: op.current, trigger: button })) return;
              busy = true; button.disabled = true;
              try { await op.request('/api/recovery/policy/rollback/' + encodeURIComponent(revision.id), { method: 'POST', body: JSON.stringify({ expected_fingerprint: impact.expected_fingerprint }) }); if (op.current()) await load(true); }
              catch (error) { if (op.current()) { validation.server(error); status('Rollback blocked or conflicted. Refresh current policy and dependencies.'); } }
              finally { busy = false; }
            };
          } catch (error) { if (op.current()) { validation.server(error); status('Rollback impact unavailable; no policy changed.'); button.disabled = false; } }
          finally { busy = false; }
        };
      }
      history.append(row);
    }
    focusReference(root); status(snapshot.policy_control.can_configure ? 'Canonical policy loaded. Preview changes before publishing.' : 'Read-only policy: recovery administration with sufficient assurance is required.');
  } catch (error) { if (op.current()) status(`Recovery policy unavailable: ${error.message}`); }
}
bindWhenReady(() => {
  document.getElementById('refresh-recovery-policy')?.addEventListener('click', () => void load());
  window.addEventListener('codex:project-changed', () => void load(true));
  window.addEventListener('codex:project-workspace-page', event => { if (event.detail?.workspace === 'operations' && !editor?.dirty()) void load(true); });
});
