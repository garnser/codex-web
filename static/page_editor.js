import { confirmDiscard } from './dirty_editor.js';

if (!document.querySelector('[data-page-editor-style]')) {
  const link = document.createElement('link'); link.rel = 'stylesheet';
  link.href = new URL('./page_editor.css', import.meta.url).href;
  link.dataset.pageEditorStyle = ''; document.head.appendChild(link);
}

// Presentation only: move the live editor, retaining canonical API handlers.
export function showPageEditor(dialog, { workspace } = {}) {
  if (dialog.open) return;
  if (workspace && window.CodexProductUI?.openWorkspace?.(workspace) === false) return false;
  const opener = document.activeElement;
  const origin = document.createComment('editor origin'); dialog.before(origin);
  const host = [...document.querySelectorAll('[data-product-workspace-host]')]
    .find(item => item.closest('[data-product-workspace-panel]')?.hidden === false
      && item.closest('#product-workspace-page')?.hidden === false);
  const siblings = host ? [...host.children].map(node => [node, node.hidden]) : [];
  const scroller = host?.closest('.product-workspace-panels');
  const scrollTop = scroller?.scrollTop || 0;
  const header = dialog.querySelector('header');
  const close = dialog.querySelector('[data-close]') || header?.querySelector('button');
  const actions = document.createElement('div'); actions.className = 'page-editor-actions';
  const moved = [];
  const move = node => { const marker = document.createComment('action origin'); node.before(marker); moved.push([node, marker]); actions.appendChild(node); };
  if (close) { close.textContent = 'Back'; move(close); }
  dialog.querySelectorAll('[data-save]').forEach(move);
  header?.appendChild(actions);
  dialog.classList.add('page-editor');
  dialog.setAttribute('aria-label', dialog.querySelector('h2')?.textContent || 'Editor');
  siblings.forEach(([node]) => { node.hidden = true; });
  (host || document.body).appendChild(dialog);
  if (!host) dialog.classList.add('page-editor-standalone');
  dialog.show();
  if (scroller) scroller.scrollTop = 0;
  const heading = dialog.querySelector('h2');
  if (heading) { heading.tabIndex = -1; heading.focus({ preventScroll: true }); }
  const escape = event => {
    if (event.key !== 'Escape' || document.querySelector('dialog:modal')) return;
    event.preventDefault(); close?.click();
  };
  // Navigation has already passed the shell's shared dirty-editor guard.
  const leave = () => dialog.close();
  window.addEventListener('keydown', escape);
  window.addEventListener('codex:project-workspace-page', leave);
  window.addEventListener('codex:project-changed', leave);
  dialog.addEventListener('close', () => {
    window.removeEventListener('keydown', escape);
    window.removeEventListener('codex:project-workspace-page', leave);
    window.removeEventListener('codex:project-changed', leave);
    siblings.forEach(([node, hidden]) => { if (node.isConnected) node.hidden = hidden; });
    moved.forEach(([node, marker]) => marker.replaceWith(node)); actions.remove();
    if (dialog.isConnected) origin.replaceWith(dialog); else origin.remove();
    dialog.classList.remove('page-editor', 'page-editor-standalone');
    if (scroller) scroller.scrollTop = scrollTop;
    if (opener?.isConnected) opener.focus({ preventScroll: true });
  }, { once: true });
}

export function inlineEditorActions(root, editor, { selector, backText = 'Back' } = {}) {
  root.classList.add('page-inline-editor');
  const actions = document.createElement('div'); actions.className = 'page-editor-actions';
  const back = document.createElement('button'); back.type = 'button'; back.textContent = backText;
  back.addEventListener('click', () => {
    if (!confirmDiscard(editor)) return;
    root.open = false; root.querySelector('summary')?.focus();
  });
  actions.appendChild(back);
  root.querySelectorAll(selector)
    .forEach(button => actions.appendChild(button));
  root.insertBefore(actions, root.querySelector('summary')?.nextSibling || root.firstChild);
}

export function resourceEditorActions(root, editor) {
  inlineEditorActions(root, editor, { backText: 'Back to resources',
    selector: '[data-resource-save],[data-resource-discard],#create-resource,#create-resource-relationship' });
}
