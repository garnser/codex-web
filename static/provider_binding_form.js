import { esc } from './secret_reference_ui.js';

const labels = { id: 'Stable binding ID', display_name: 'Display name', declared_capabilities: 'Declared capabilities', granted_capabilities: 'Granted capabilities', model_provider_ids: 'Linked ModelGateway provider IDs', extension_installation_id: 'Extension installation ID', configuration_refs: 'Configuration references', credential_refs: 'SecretReference IDs', residency_tags: 'Residency tags', compliance_tags: 'Compliance tags', lifecycle: 'Binding lifecycle', health: 'Declared binding health', compatibility: 'Binding compatibility', status: 'ModelGateway binding status', adapter_type: 'Registered adapter type', base_url: 'Provider API base URL (optional)', credential_ref: 'SecretReference ID (optional)', credential_required: 'Require credential reference' };
export function bindingForm(provider, schema, canManage) {
  return `<form class="page-inline-editor provider-binding-form" data-binding-form novalidate><h3>Edit ${esc(provider.display_name)}</h3><p>Edit the local binding and its routing eligibility. Runtime registration requires a deployment change. Archive, delete and remote account retirement are unavailable here. Binding health metadata and runtime-reported health are separate.</p><div class="page-editor-actions"><button type="button" data-binding-preview ${canManage ? '' : 'disabled'}>Preview impact</button><button type="submit" data-binding-save disabled>Apply binding change</button><button type="button" data-binding-discard>Discard edits</button></div><fieldset ${canManage ? '' : 'disabled'}><legend>Canonical provider binding</legend><div class="provider-binding-fields">${Object.entries(schema.properties).map(([name, spec]) => {
    const value = provider[name]; const definition = spec.$ref ? schema.$defs[spec.$ref.split('/').pop()] : spec;
    let control;
    if (spec.type === 'boolean') control = `<input name="${name}" type="checkbox" ${value ? 'checked' : ''}>`;
    else if (definition.enum) control = `<select name="${name}">${definition.enum.map(item => `<option value="${esc(item)}" ${value === item ? 'selected' : ''}>${esc(item)}</option>`).join('')}</select>`;
    else if (spec.type === 'array' && spec.items?.$ref) {
      const values = schema.$defs[spec.items.$ref.split('/').pop()].enum;
      control = `<select name="${name}" multiple size="6">${values.map(item => `<option value="${esc(item)}" ${(value || []).includes(item) ? 'selected' : ''}>${esc(item)}</option>`).join('')}</select><small>Use Ctrl/Cmd to select multiple capabilities. Grants cannot exceed declarations.</small>`;
    } else control = `<input type="text" name="${name}" value="${esc(Array.isArray(value) ? value.join(', ') : value || '')}" ${name === 'id' ? 'readonly' : ''} ${(schema.required || []).includes(name) ? 'required' : ''} ${spec.maxLength ? `maxlength="${spec.maxLength}"` : ''}>${spec.type === 'array' ? '<small>Separate references or tags with commas.</small>' : ''}`;
    return `<label>${esc(labels[name] || name)}${control}</label>`;
  }).join('')}</div></fieldset><div data-binding-impact role="status" aria-live="polite"></div></form>`;
}
export function readBinding(form, schema) {
  return Object.fromEntries(Object.entries(schema.properties).map(([name, spec]) => {
    const input = form.elements.namedItem(name);
    const value = spec.type === 'boolean' ? input.checked : spec.type === 'array' ? input.multiple ? [...input.selectedOptions].map(option => option.value) : input.value.split(',').map(value => value.trim()).filter(Boolean) : input.value.trim();
    return [name, ['extension_installation_id', 'base_url', 'credential_ref'].includes(name) ? value || null : value];
  }));
}
