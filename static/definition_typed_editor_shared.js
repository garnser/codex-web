export function esc(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

export function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

export function csv(value) {
  return String(value ?? '').split(',').map((item) => item.trim()).filter(Boolean);
}

export function lines(value) {
  return String(value ?? '').split('\n').map((item) => item.trim()).filter(Boolean);
}

export function pairs(value) {
  const result = {};
  for (const row of lines(value)) {
    const split = row.indexOf('=');
    if (split < 1) continue;
    result[row.slice(0, split).trim()] = row.slice(split + 1).trim();
  }
  return result;
}

export function pairText(value) {
  return Object.entries(value || {}).map(([key, item]) => key + '=' + item).join('\n');
}

export function numberOrNull(value) {
  const raw = String(value ?? '').trim();
  if (!raw) return null;
  const parsed = Number(raw);
  return Number.isFinite(parsed) ? parsed : null;
}

export function field(label, html) {
  return '<label><span>' + esc(label) + '</span>' + html + '</label>';
}

export function input(cls, value, attrs = '') {
  return '<input class="' + cls + '" value="' + esc(value ?? '') + '" ' + attrs + ' />';
}

export function textarea(cls, value, rows = 3) {
  return '<textarea class="' + cls + '" rows="' + rows + '">' + esc(value ?? '') + '</textarea>';
}

export function lifecycleSelect(
  value,
  cls = 'typed-role-lifecycle',
  disabledValues = [],
) {
  const blocked = new Set(disabledValues);
  return '<select class="' + cls + '">'
    + ['active', 'deprecated', 'disabled'].map((item) => (
      '<option value="' + item + '"'
      + (item === value ? ' selected' : '')
      + (blocked.has(item) ? ' disabled' : '')
      + '>' + item + '</option>'
    )).join('')
    + '</select>';
}

export function dateTimeValue(seconds) {
  if (!seconds) return '';
  const value = new Date(Number(seconds) * 1000);
  if (Number.isNaN(value.getTime())) return '';
  const local = new Date(value.getTime() - value.getTimezoneOffset() * 60000);
  return local.toISOString().slice(0, 16);
}
