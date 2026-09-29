// Presentation only: canonical server validation remains authoritative.
let sequence = 0;

export function formValidation(root) {
  const prefix = `form-validation-${++sequence}`;
  let summary = null;
  const touched = new Map();
  const inline = [];
  const control = name => typeof name === 'string'
    ? [...root.querySelectorAll('input,select,textarea')].find(field => field.name === name || field.id === name)
    : name instanceof HTMLElement && root.contains(name) ? name : null;

  function focusControl(field) {
    for (let parent = field.parentElement; parent; parent = parent.parentElement) {
      if (parent instanceof HTMLDetailsElement) parent.open = true;
    }
    field.focus();
  }

  function clear() {
    summary?.remove();
    summary = null;
    inline.splice(0).forEach(node => node.remove());
    for (const [field, previous] of touched) {
      for (const [name, value] of Object.entries(previous)) {
        if (value === null) field.removeAttribute(name);
        else field.setAttribute(name, value);
      }
    }
    touched.clear();
  }

  function show(errors, { focus = true } = {}) {
    clear();
    if (!errors.length) return true;
    summary = document.createElement('section');
    summary.dataset.validationSummary = '';
    summary.setAttribute('role', 'alert');
    summary.tabIndex = -1;
    const title = document.createElement('h3');
    title.textContent = 'Check the form before saving';
    summary.append(title);
    const list = document.createElement('ul');
    summary.append(list);
    let first = null;
    errors.slice(0, 50).forEach((error, index) => {
      const field = control(error.field);
      const message = String(error.message || 'Review this value and try again.');
      const row = document.createElement('li');
      if (field) {
        if (!touched.has(field)) touched.set(field, {
          'aria-invalid': field.getAttribute('aria-invalid'),
          'aria-describedby': field.getAttribute('aria-describedby'),
        });
        const notice = document.createElement('span');
        notice.id = `${prefix}-${index}`;
        notice.dataset.fieldError = '';
        notice.style.display = 'block';
        notice.textContent = message;
        field.insertAdjacentElement('afterend', notice);
        inline.push(notice);
        field.setAttribute('aria-invalid', 'true');
        field.setAttribute('aria-describedby', [field.getAttribute('aria-describedby'), notice.id].filter(Boolean).join(' '));
        const link = document.createElement('button');
        link.type = 'button';
        link.className = 'ghost-button';
        const label = field.labels?.[0]?.childNodes?.[0]?.textContent?.trim() || field.getAttribute('aria-label') || field.name || 'Field';
        link.textContent = `${label}: ${message}`;
        link.addEventListener('click', () => focusControl(field));
        row.append(link);
        if (!field.disabled) first ||= field;
      } else row.textContent = message;
      list.append(row);
    });
    root.prepend(summary);
    if (focus) focusControl(first || summary);
    return false;
  }

  function validate(extra = []) {
    const errors = [...root.querySelectorAll('input,select,textarea')]
      .filter(field => field.willValidate && (!field.validity.valid || (field.required && !field.value.trim())))
      .map(field => ({ field, message: field.validationMessage || 'Enter a value before saving.' }));
    return show([...errors, ...extra]);
  }

  function server(error, fields = {}) {
    const detail = error?.detail;
    const issues = Array.isArray(detail) ? detail : Array.isArray(detail?.errors) ? detail.errors : [];
    if (issues.length) return show(issues.map(issue => {
      const path = (Array.isArray(issue.loc) ? issue.loc : []).filter(part => !['body', 'query'].includes(part)).join('.');
      return { field: fields[path] || path, message: issue.msg || 'Review this value and try again.' };
    }));
    const message = typeof detail === 'string' ? detail : detail?.message;
    return show([{ message: message || (error?.status === 403
      ? 'You do not have permission to save this change. Check your access and try again.'
      : 'The server could not save these values. Review the form and try again; your entries are preserved.') }]);
  }
  return { clear, show, validate, server };
}
