import { esc as escapeHtml } from './reference_links.js';

function option(value, current, label = value) {
  return `<option value="${escapeHtml(value)}"${value === current ? ' selected' : ''}>${escapeHtml(label)}</option>`;
}
export function operationalContextFields(item) {
  return `        <fieldset data-resource-operational-context data-known="${item.operational_context != null}"><legend>Current operational facts for playbook selection</legend>
          <label>Environment<select data-resource-environment>${option('', item.operational_context?.environment || '', 'Unknown / unset')}${['development','test','staging','production'].map(value => option(value, item.operational_context?.environment)).join('')}</select></label>
          <label>Operational tags (comma separated)<input data-resource-operational-tags value="${escapeHtml((item.operational_context?.tags || []).join(', '))}"></label>
          <label>Service versions (one service=version per line)<textarea data-resource-service-versions>${escapeHtml(Object.entries(item.operational_context?.service_versions || {}).map(([name,version]) => `${name}=${version}`).join('\n'))}</textarea></label>
          <small>These are current facts, distinct from playbook instructions and authority. Changes invalidate applicability context and require fresh admission.</small>
        </fieldset>`;
}
export function readOperationalContext(root) {
  const versions = {};
  for (const line of root.querySelector('[data-resource-service-versions]').value.split('\n').map(v => v.trim()).filter(Boolean)) {
    const parts = line.split('=');
    if (parts.length !== 2 || !parts[0].trim() || !/^\d{1,6}\.\d{1,6}\.\d{1,6}$/.test(parts[1].trim()) || Object.hasOwn(versions, parts[0].trim())) throw new Error('Service versions require unique service=major.minor.patch lines.');
    versions[parts[0].trim()] = parts[1].trim();
  }
  const facts = {
    environment: root.querySelector('[data-resource-environment]').value || null,
    tags: root.querySelector('[data-resource-operational-tags]').value.split(',').map(v => v.trim()).filter(Boolean),
    service_versions: versions,
  };
  const known = root.querySelector('[data-resource-operational-context]').dataset.known === 'true';
  return known || facts.environment || facts.tags.length || Object.keys(versions).length ? facts : null;
}
