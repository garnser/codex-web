import { esc, bindSecretLink } from './secret_reference_ui.js';

export function renderSecretOptions(select, secrets, selectedId = '', projectId) {
  if (!select) return;
  const options = secrets.map((item) => ({
    id: String(item.id || ''),
    label: item.name || item.label || item.id,
    status: item.status || '',
  })).filter((item) => item.id);
  if (selectedId && !options.some((item) => item.id === selectedId)) {
    options.unshift({ id: selectedId, label: selectedId, status: 'not listed' });
  }
  select.innerHTML = [
    '<option value="">Select a canonical secret…</option>',
    ...options.map((item) => `<option value="${esc(item.id)}" ${item.id === selectedId ? 'selected' : ''}>${esc(item.label)}${item.status ? ` (${esc(item.status)})` : ''}</option>`),
  ].join('');
  bindSecretLink(select, selectedId, projectId);
}
