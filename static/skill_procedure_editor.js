import { request } from './api_client.js';
import { esc } from './definition_typed_editor_shared.js';

// The server schema owns fields, enums and bounds. This is an ephemeral form,
// never a second procedure validator, definition catalog or execution store.
export async function installProcedureEditor(host, value, current) {
  host.textContent = 'Loading canonical playbook schema…';
  const result = await request('/api/skills/procedure-schema');
  if (!current()) return null;
  const schema = result.schema;
  const resolve = node => node.$ref ? resolve(schema.$defs[node.$ref.split('/').pop()]) : node;
  const label = path => path.join('.');
  function refreshDisabled() {
    const enabled = host.querySelector('[data-procedure-enabled]')?.checked;
    for (const field of host.querySelectorAll('[data-procedure-value]')) {
      let included = enabled;
      for (let parent = field.parentElement; parent && parent !== host; parent = parent.parentElement) {
        if (!parent.matches('[data-procedure-path]')) continue;
        const optional = parent.querySelector(':scope > label > [data-procedure-optional]');
        if (optional && !optional.checked) included = false;
      }
      field.disabled = !included;
    }
  }
  function nodeFor(raw) {
    let node = resolve(raw);
    if (node.anyOf) node = resolve(node.anyOf.find(item => item.type !== 'null'));
    return node;
  }
  function render(raw, value, path, required = true) {
    const node = nodeFor(raw), name = label(path), title = path.at(-1) || 'Playbook';
    const wrap = document.createElement('div');
    wrap.dataset.procedurePath = name;
    wrap.className = 'procedure-field';
    const optional = !required && value == null;
    if (!required) {
      const enable = document.createElement('label');
      enable.innerHTML = `<input type="checkbox" data-procedure-optional ${optional ? '' : 'checked'}> Include ${esc(title)}`;
      wrap.append(enable);
    }
    const content = document.createElement('div');
    content.hidden = optional;
    wrap.append(content);
    wrap.querySelector('[data-procedure-optional]')?.addEventListener('change', event => {
      content.hidden = !event.target.checked;
      refreshDisabled();
    });
    if (node.type === 'object') {
      const group = document.createElement('fieldset');
      group.innerHTML = `<legend>${esc(title)}</legend>`;
      for (const [key, child] of Object.entries(node.properties || {})) {
        group.append(render(child, value?.[key] ?? child.default, [...path, key], (node.required || []).includes(key)));
      }
      content.append(group);
    } else if (node.type === 'array') {
      const group = document.createElement('fieldset');
      group.innerHTML = `<legend>${esc(title)} (${node.minItems || 0}–${node.maxItems || 'bounded'})</legend>`;
      const rows = document.createElement('div');
      group.append(rows);
      function add(item) {
        if (rows.children.length >= (node.maxItems || 100)) return;
        const row = document.createElement('div');
        row.className = 'procedure-array-row';
        row.append(render(node.items, item, [...path, rows.children.length]));
        for (const [text, move] of [['Move up', -1], ['Move down', 1], ['Remove', 0]]) {
          const button = document.createElement('button');
          button.type = 'button'; button.textContent = text;
          button.setAttribute('aria-label', `${text} ${name} item`);
          button.addEventListener('click', () => {
            if (!move) row.remove();
            else if (move < 0 && row.previousElementSibling) rows.insertBefore(row, row.previousElementSibling);
            else if (move > 0 && row.nextElementSibling) rows.insertBefore(row.nextElementSibling, row);
            host.dispatchEvent(new Event('input', { bubbles: true }));
          });
          row.append(button);
        }
        rows.append(row);
      }
      for (const item of value || []) add(item);
      const button = document.createElement('button');
      button.type = 'button'; button.textContent = title === 'steps' ? 'Add step' : `Add ${title} item`;
      if (title === 'steps') button.dataset.procedureAddStep = '';
      button.addEventListener('click', () => { add(undefined); refreshDisabled(); host.dispatchEvent(new Event('input', { bubbles: true })); });
      group.append(button); content.append(group);
    } else {
      const field = document.createElement('label');
      const allowed = node.enum || (node.const !== undefined ? [node.const] : null);
      if (allowed) field.innerHTML = `${esc(name)} <select>${allowed.map(v => `<option value="${esc(v)}">${esc(v)}</option>`).join('')}</select>`;
      else field.innerHTML = `${esc(name)} <input type="${node.type === 'boolean' ? 'checkbox' : ['number', 'integer'].includes(node.type) ? 'number' : 'text'}">`;
      const input = field.querySelector('input,select');
      input.dataset.procedureValue = '';
      input.setAttribute('aria-label', name);
      if (node.type === 'boolean') input.checked = value ?? node.default ?? false;
      else input.value = value ?? node.default ?? node.const ?? allowed?.[0] ?? '';
      if (node.minimum !== undefined) input.min = node.minimum;
      if (node.maximum !== undefined) input.max = node.maximum;
      if (node.maxLength) input.maxLength = node.maxLength;
      if (node.pattern) input.pattern = node.pattern;
      input.step = node.type === 'integer' ? '1' : 'any';
      content.append(field);
    }
    wrap.readProcedureValue = () => {
      if (!required && !wrap.querySelector(':scope > label > [data-procedure-optional]').checked) return undefined;
      if (node.type === 'object') {
        const result = {};
        for (const child of content.querySelector('fieldset').children) {
          if (!child.readProcedureValue) continue;
          const v = child.readProcedureValue();
          if (v !== undefined) result[child.dataset.procedurePath.split('.').pop()] = v;
        }
        return result;
      }
      if (node.type === 'array') return [...content.querySelector('fieldset > div').children].map(row => row.firstElementChild.readProcedureValue());
      const input = content.querySelector('[data-procedure-value]');
      if (node.type === 'boolean') return input.checked;
      if (['integer', 'number'].includes(node.type)) {
        if (!input.value.trim() || !Number.isFinite(Number(input.value))) throw new Error(`${name} requires a number.`);
        return Number(input.value);
      }
      return input.value;
    };
    return wrap;
  }
  host.innerHTML = `<label><input type="checkbox" data-procedure-enabled ${value ? 'checked' : ''}> Structured operational playbook</label><p>A playbook is a Skill Definition with bounded steps. Instructions describe reusable behavior; Resource facts describe current conditions; policy and approvals independently control authority. A run records what happened. Drafts are inactive.</p>`;
  const tree = render(schema, value, []);
  tree.hidden = !value; host.append(tree);
  host.querySelector('[data-procedure-enabled]').addEventListener('change', event => { tree.hidden = !event.target.checked; refreshDisabled(); });
  refreshDisabled();
  return { read: () => host.querySelector('[data-procedure-enabled]').checked ? tree.readProcedureValue() : null };
}
