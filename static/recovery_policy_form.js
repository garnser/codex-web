import { esc } from './secret_reference_ui.js';

const fields = ['version', 'deployment_mode', 'backup_key_id', 'destination_id', 'backup_interval_seconds', 'restore_verification_interval_seconds', 'retention_count', 'require_audit_integrity'];
const labels = { version: 'Policy version label', deployment_mode: 'Declared deployment mode', backup_key_id: 'Workspace backup key', destination_id: 'Registered backup destination', backup_interval_seconds: 'Backup interval (seconds)', restore_verification_interval_seconds: 'Restore verification interval (seconds)', retention_count: 'Retained backup count', require_audit_integrity: 'Require audit continuity during restore verification' };
export function renderPolicyForm(snapshot, keys) {
  const control = snapshot.policy_control; const schema = control.schema;
  const initial = snapshot.policy || Object.fromEntries(Object.entries(schema.properties).filter(([, spec]) => 'default' in spec).map(([name, spec]) => [name, spec.default]));
  const options = (items, selected) => items.map(value => `<option value="${esc(value)}" ${value === selected ? 'selected' : ''}>${esc(value)}</option>`).join('');
  return `<form data-recovery-form class="page-inline-editor recovery-policy-form" novalidate><h3>Editable policy</h3><p>Canonical RecoveryState · shared workspace ${esc(control.organization_id)}/${esc(control.workspace_id)}. Policy changes affect subsequent recovery operations; existing backup evidence remains read-only.</p>
    <p>Destination registration, backend filesystem configuration and worker topology are deployment settings; this form selects existing registrations and does not change them.</p>
    <div class="page-editor-actions"><button type="button" data-recovery-preview ${control.can_configure ? '' : 'disabled'}>Preview impact</button><button type="submit" data-recovery-save disabled>Publish policy</button><button type="button" data-recovery-discard>Discard edits</button></div>
    <fieldset ${control.can_configure ? '' : 'disabled'}><legend>Policy settings</legend><div class="route-test">
    ${fields.map(name => {
      const spec = schema.properties[name]; const value = initial[name]; let input;
      if (spec.type === 'boolean') input = `<input name="${name}" type="checkbox" ${value ? 'checked' : ''}>`;
      else if (name === 'backup_key_id' || name === 'destination_id' || name === 'deployment_mode') {
        let values = name === 'backup_key_id' ? keys.filter(key => key.status === 'active' && key.purpose === 'backup' && !key.scope?.project_id && !key.scope?.resource_id).map(key => key.id) : name === 'destination_id' ? control.destinations.map(item => item.id) : schema.$defs.RecoveryDeploymentMode.enum;
        if (value && !values.includes(value)) values = [value, ...values];
        input = `<select name="${name}" required>${options(values, value)}</select>`;
      } else input = `<input name="${name}" type="${spec.type === 'integer' ? 'number' : 'text'}" value="${esc(value)}" required ${spec.minimum !== undefined ? `min="${spec.minimum}" step="1"` : ''} ${spec.maximum !== undefined ? `max="${spec.maximum}"` : ''}>`;
      return `<label>${labels[name]} ${input}</label>`;
    }).join('')}</div><h4>Recovery objectives</h4><p>Enable each data class to define its recovery point and recovery time objectives. These targets do not assert that recovery is currently qualified.</p>
    ${schema.$defs.RecoveryDataClass.enum.map(kind => {
      const objective = (initial.objectives || []).find(item => item.data_class === kind);
      return `<div data-objective="${esc(kind)}" class="route-test"><label><input type="checkbox" data-objective-enabled ${objective ? 'checked' : ''}> ${esc(kind.replaceAll('_', ' '))}</label><label>RPO seconds<input type="number" data-rpo min="0" max="31536000" step="1" value="${objective?.rpo_seconds ?? 3600}" required></label><label>RTO seconds<input type="number" data-rto min="1" max="31536000" step="1" value="${objective?.rto_seconds ?? 14400}" required></label></div>`;
    }).join('')}
    <p>Key-manifest validation remains enforced by the recovery engine. Point-in-time recovery is not implemented by this editor. Historical policy values for these capabilities are preserved.</p>
    </fieldset><div data-recovery-impact role="status" aria-live="polite"></div></form>`;
}
export function readPolicyForm(form, snapshot) {
  const schema = snapshot.policy_control.schema;
  const payload = snapshot.policy ? structuredClone(snapshot.policy) : Object.fromEntries(Object.entries(schema.properties).filter(([, spec]) => 'default' in spec).map(([name, spec]) => [name, spec.default]));
  for (const name of fields) {
    const node = form.elements.namedItem(name); const spec = schema.properties[name];
    payload[name] = spec.type === 'boolean' ? node.checked : spec.type === 'integer' ? Number(node.value) : node.value.trim();
  }
  payload.objectives = [...form.querySelectorAll('[data-objective]')].filter(row => row.querySelector('[data-objective-enabled]').checked).map(row => ({ data_class: row.dataset.objective, rpo_seconds: Number(row.querySelector('[data-rpo]').value), rto_seconds: Number(row.querySelector('[data-rto]').value) }));
  return payload;
}
