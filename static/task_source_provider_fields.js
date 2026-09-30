import { esc } from './secret_reference_ui.js';

// The API supplies code-owned Pydantic schemas, not a second provider catalog.
export function renderProviderDetails(root, schema, sourceType, settings = {}) {
  let host = root.querySelector('[data-source-provider-details]');
  if (!host) {
    host = document.createElement('details'); host.dataset.sourceProviderDetails = '';
    root.querySelector('.work-source-provider-fields').appendChild(host);
  }
  host.hidden = sourceType.toLowerCase() !== 'servicenow';
  const fields = Object.entries(schema?.$defs?.ServiceNowFieldSettings?.properties || {}).filter(([, value]) => value.type === 'string').slice(0, 20);
  const states = (schema?.$defs?.ServiceNowTaskSourceSettings?.properties?.canonical_state_values?.propertyNames?.enum || []).filter(value => !['implementation_active', 'closed'].includes(value)).slice(0, 20);
  const fingerprint = JSON.stringify([fields, states]);
  if (host.dataset.schema !== fingerprint) {
    host.dataset.schema = fingerprint;
    host.innerHTML = '<summary>ServiceNow field names and additional state mappings</summary>'
      + (fields.length ? fields.map(([name, spec]) => `<label>${esc(spec.title || name)} field name <input data-source-provider-field="${esc(name)}" placeholder="${esc(spec.default || '')}"></label>`).join('') : '<p>Advanced schema unavailable. Existing mappings are preserved; refresh to edit these fields.</p>')
      + states.map(stage => `<label>${esc(stage)} state value <input data-source-provider-state="${esc(stage)}"></label>`).join('');
  }
  for (const [name, spec] of fields) {
    const input = [...host.querySelectorAll('[data-source-provider-field]')].find(item => item.dataset.sourceProviderField === name);
    input.value = settings.fields?.[name] ?? spec.default ?? '';
  }
  host.querySelectorAll('[data-source-provider-state]').forEach(input => { input.value = settings.canonical_state_values?.[input.dataset.sourceProviderState] || ''; });
}

export function captureProviderDetails(root, previous = {}) {
  const fields = { ...(previous.fields || {}) };
  const canonical_state_values = { ...(previous.canonical_state_values || {}) };
  root.querySelectorAll('[data-source-provider-field]').forEach(input => { fields[input.dataset.sourceProviderField] = input.value.trim(); });
  root.querySelectorAll('[data-source-provider-state]').forEach(input => {
    const stage = input.dataset.sourceProviderState, value = input.value.trim();
    if (value) canonical_state_values[stage] = value; else delete canonical_state_values[stage];
  });
  return { ...(Object.keys(fields).length ? { fields } : {}), canonical_state_values };
}
