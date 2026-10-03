import { confirmAction } from './action_confirmation.js';
import './page_editor.js';
import { esc } from './secret_reference_ui.js';
import { trackDirtyEditor, confirmDiscard } from './dirty_editor.js';
import { formValidation } from './form_validation.js';

const style = document.createElement('link'); style.rel = 'stylesheet'; style.href = new URL('./entitlement_editor.css', import.meta.url).href; document.head.appendChild(style);

let editor = null;
export function discardManagement() { return confirmDiscard(editor); }
export function disposeManagement() { editor?.dispose(); editor = null; }
const options = (values, value) => values.map(item => `<option value="${esc(item)}" ${item === value ? 'selected' : ''}>${esc(item)}</option>`).join('');
const field = (name, label, value = '', extra = '') => `<label>${label}<input name="${name}" value="${esc(value ?? '')}" ${extra}></label>`;
export function renderManagement(host, snapshot, op, refresh) {
  if (!host) return;
  host.closest('.developer-card')?.classList.add('entitlement-management-card');
  const control = snapshot.control;
  host.innerHTML = `<p>Shared workspace ${esc(snapshot.organization_id)}/${esc(snapshot.workspace_id)}. ${esc(snapshot.inheritance)}</p><p>Control: <strong>${esc(control.kind)}</strong> · ${esc(control.controller_identity_id || 'workspace administrators')}. ${esc(control.guidance)}</p><p>Mode source: ${esc(snapshot.mode_source)}. Record source labels describe provenance and cannot grant control.</p>`;
  if (!snapshot.can_manage) { host.insertAdjacentHTML('beforeend', `<p>Read-only: ${esc(snapshot.denial_reason || 'Administration with sufficient assurance is required.')} Contact the registered controller for external changes.</p>`); return; }
  const toolbar = document.createElement('div'); toolbar.className = 'route-test'; host.append(toolbar);
  const surface = document.createElement('div'); host.append(surface);
  function button(label, kind, record = null) { const node = document.createElement('button'); node.type = 'button'; node.textContent = label; node.onclick = () => open(kind, record); toolbar.append(node); }
  button('Edit tenant mode', 'mode'); button('Add capability', 'capability'); button('Add quota', 'quota');
  for (const item of snapshot.capabilities) button(`Edit capability ${item.capability}`, 'capability', item);
  for (const item of snapshot.quotas) { button(`Edit quota ${item.metric}`, 'quota', item); button(`Retire quota ${item.metric}`, 'retire_quota', item); }
  function open(kind, record) {
    if (!discardManagement()) return;
    disposeManagement();
    const key = record?.capability || record?.metric || '';
    let fields = '';
    if (kind === 'mode') fields = `<label>Tenant mode<select name="mode">${options(['self_hosted_unlimited', 'enforced'], snapshot.mode)}</select></label>`;
    else {
      fields = field('key', kind === 'capability' ? 'Capability key' : 'Metric key', key, `required ${record ? 'readonly' : ''}`);
      if (kind !== 'retire_quota') fields += field('source', 'Provenance label', record?.source || 'manual', 'required');
      if (kind === 'capability') fields += `<label>Grant state<select name="enabled">${options(['enabled', 'disabled'], record?.enabled === false ? 'disabled' : 'enabled')}</select></label>` + field('starts_at', 'Starts at (Unix seconds, optional)', record?.starts_at, 'type="number" step="any"') + field('expires_at', 'Expires at (Unix seconds, optional)', record?.expires_at, 'type="number" step="any"');
      if (kind === 'quota') fields += field('limit', 'Limit', record?.limit ?? 0, 'type="number" min="0" step="any" required') + `<label>Aggregation window<select name="window">${options(snapshot.quota_schema.$defs.QuotaWindow.enum, record?.window || 'month')}</select></label><label>Exceeded behavior<select name="behavior">${options(snapshot.quota_schema.$defs.QuotaBehavior.enum, record?.behavior || 'hard_stop')}</select></label>` + field('warning_fraction', 'Warning fraction (0–1)', record?.warning_fraction ?? 0.8, 'type="number" min="0" max="1" step="any" required');
    }
    surface.innerHTML = `<form class="page-inline-editor entitlement-management-form" data-entitlement-form novalidate><h3>${esc(kind.replaceAll('_', ' '))} · ${esc(key || 'workspace')}</h3><p>Changes affect every Project inheriting ${esc(snapshot.workspace_id)}. Disable a capability to retire its grant. Retiring a quota removes its limit and preserves usage.</p><div class="page-editor-actions"><button type="button" data-entitlement-preview>Preview impact</button><button type="submit" data-entitlement-save disabled>Apply change</button><button type="button" data-entitlement-discard>Discard edits</button></div><fieldset><legend>Configuration</legend><div class="route-test">${fields}</div></fieldset><div data-entitlement-impact role="status" aria-live="polite"></div></form>`;
    const form = surface.querySelector('form'); const validation = formValidation(form);
    editor = trackDirtyEditor(form, { label: 'Entitlement configuration' }); const owned = editor;
    const save = form.querySelector('[data-entitlement-save]'); let reviewed = null; let busy = false;
    const value = name => form.elements.namedItem(name).value.trim();
    function payload() {
      const result = { kind, key: kind === 'mode' ? '' : value('key') };
      if (kind === 'mode') result.mode = { mode: value('mode') };
      if (kind === 'capability') result.capability = { enabled: value('enabled') === 'enabled', source: value('source'), starts_at: value('starts_at') ? Number(value('starts_at')) : null, expires_at: value('expires_at') ? Number(value('expires_at')) : null };
      if (kind === 'quota') result.quota = { source: value('source'), limit: Number(value('limit')), window: value('window'), behavior: value('behavior'), warning_fraction: Number(value('warning_fraction')) };
      return result;
    }
    const current = () => op.current() && editor === owned;
    form.addEventListener('input', () => { reviewed = null; save.disabled = true; });
    form.querySelector('[data-entitlement-discard]').onclick = () => { if (confirmDiscard(owned)) { disposeManagement(); surface.replaceChildren(); } };
    form.querySelector('[data-entitlement-preview]').onclick = async () => {
      if (busy || !validation.validate()) return;
      busy = true; reviewed = null; save.disabled = true; const proposed = payload(); const text = JSON.stringify(proposed);
      try {
        const impact = await op.request('/api/entitlements/administration/preview', { method: 'POST', body: text });
        if (!current() || JSON.stringify(payload()) !== text) return;
        if (impact.available !== true) throw Object.assign(new Error('Impact unavailable'), { detail: 'Impact unavailable; no change can be applied.' });
        if (impact.expected_revision !== snapshot.revision) throw Object.assign(new Error('Configuration changed'), { detail: 'Configuration changed; reload before reviewing edits.' });
        form.querySelector('[data-entitlement-impact]').innerHTML = `<p>Target ${esc(impact.kind)} ${esc(impact.key)} · workspace ${esc(impact.organization_id)}/${esc(impact.workspace_id)}</p><p>${esc(impact.effect)}</p><p>Current usage in proposed window: ${esc(impact.current_usage ?? 'not applicable')}. ${esc(impact.usage_caveat)}</p>`;
        reviewed = { impact, proposed, text }; save.disabled = false;
      } catch (error) { if (current()) validation.server(error); }
      finally { busy = false; }
    };
    form.onsubmit = async event => {
      event.preventDefault(); if (busy || !reviewed || !validation.validate() || JSON.stringify(payload()) !== reviewed.text) return;
      if (!await confirmAction({ action: 'Apply entitlement change', target: `${kind} ${reviewed.proposed.key || 'mode'} · ${snapshot.organization_id}/${snapshot.workspace_id}`, risk: 'high', consequence: reviewed.impact.effect, impact: form.querySelector('[data-entitlement-impact]').textContent, recovery: 'Usage history is retained. To change local controls again, reload and preview a new edit; external control remains with its owner.', current: () => current() && reviewed && JSON.stringify(payload()) === reviewed.text, trigger: save })) return;
      busy = true; save.disabled = true; const submitted = owned.snapshot(); const proposed = reviewed.proposed;
      const path = kind === 'mode' ? '/mode' : `/${kind === 'capability' ? 'capabilities' : 'quotas'}/${encodeURIComponent(proposed.key)}`;
      try {
        await op.request('/api/entitlements' + path + '?expected_revision=' + encodeURIComponent(reviewed.impact.expected_revision), { method: kind === 'retire_quota' ? 'DELETE' : 'PUT', ...(kind === 'retire_quota' ? {} : { body: JSON.stringify(proposed[kind]) }) });
        owned.markSaved(submitted);
        if (current() && !owned.dirty()) await refresh();
        else if (current()) validation.show([{ message: 'Saved. Newer edits remain; reload current configuration before applying them.' }]);
      } catch (error) { if (current()) validation.server(error); }
      finally { busy = false; reviewed = null; }
    };
    form.querySelector('input, select')?.focus();
  }
}
