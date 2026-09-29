// UI command sources project existing canonical controls/data. They grant no authority.
const sources = new Map();
const recent = [];
export function registerCommandSource(id, commands) {
  if (!id || typeof commands !== 'function') throw new TypeError('Command source requires an id and a function');
  sources.set(id, commands);
  window.dispatchEvent(new Event('codex:commands-changed'));
  return () => sources.delete(id);
}

export function installCommandPalette(dialog, getContext) {
  const search = dialog.querySelector('#product-workspace-search');
  const results = dialog.querySelector('[data-product-workspace-nav-groups]');
  const status = dialog.querySelector('[data-command-status]');
  let commands = [], selected = 0, returnFocus = null, executing = false;
  function availableCommands() {
    const context = getContext();
    const commands = [];
    for (const [source, provide] of sources) {
      for (const command of provide(context)) {
        if (!command.id || !command.label || typeof command.run !== 'function') continue;
        if (command.projectId && command.projectId !== context.projectId) continue;
        if (command.available && !command.available()) continue;
        commands.push({ ...command, key: `${source}:${command.id}` });
      }
    }
    return commands;
  }
  function select(index) {
    selected = Math.max(0, Math.min(index, commands.length - 1));
    results.querySelectorAll('[data-command-key]').forEach((button, i) => {
      button.classList.toggle('active', i === selected);
      button.tabIndex = i === selected ? 0 : -1;
      button.setAttribute('aria-selected', String(i === selected));
    });
    const current = results.querySelectorAll('[data-command-key]')[selected];
    if (current) {
      search.setAttribute('aria-activedescendant', current.id);
      current.scrollIntoView({ block: 'nearest' });
    } else search.removeAttribute('aria-activedescendant');
  }
  function render() {
    const needle = search.value.trim().toLowerCase();
    commands = availableCommands().filter((item) =>
      `${item.label} ${item.description || ''} ${item.scopeLabel || ''}`.toLowerCase().includes(needle));
    commands.sort((a, b) => {
      const rank = (item) => { const i = recent.indexOf(item.key); return i < 0 ? 999 : i; };
      return rank(a) - rank(b);
    });
    results.replaceChildren();
    commands.forEach((item, index) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'product-workspace-nav-item';
      button.dataset.commandKey = item.key;
      if (item.workspaceId) button.dataset.productWorkspaceNav = item.workspaceId;
      button.id = `product-command-${index}`;
      button.setAttribute('role', 'option');
      button.tabIndex = -1;
      const label = document.createElement('strong');
      label.textContent = item.label;
      const detail = document.createElement('small');
      detail.textContent = [item.scopeLabel, item.description, recent.includes(item.key) ? 'Recent' : ''].filter(Boolean).join(' · ');
      button.append(label, detail);
      button.addEventListener('click', () => execute(item.key));
      results.append(button);
    });
    status.textContent = commands.length ? `${commands.length} results. Use ↑ / ↓ and Enter.` : 'No matching commands. Try a destination or Project name.';
    select(0);
  }
  async function execute(key) {
    // Re-resolve scope and availability at execution, never trust a rendered result.
    const command = availableCommands().find((item) => item.key === key);
    if (!command || executing) { render(); return; }
    executing = true;
    recent.splice(0, recent.length, key, ...recent.filter((item) => item !== key).slice(0, 7));
    dialog.close();
    try { await command.run(); }
    catch {
      dialog.showModal();
      render();
      status.textContent = 'Could not open this destination. Refresh its canonical view and try again.';
      search.focus();
    }
    finally { executing = false; }
  }
  function open() {
    if (dialog.open) { search.focus(); return; }
    // Do not bypass another modal's focus/confirmation boundary.
    if (document.querySelector('dialog:modal')) return;
    returnFocus = document.activeElement;
    search.value = '';
    dialog.showModal();
    render();
    search.focus();
  }
  search.addEventListener('input', render);
  search.addEventListener('keydown', (event) => {
    if (event.isComposing) return;
    if (['ArrowDown', 'ArrowUp', 'Enter'].includes(event.key)) event.preventDefault();
    if (event.key === 'ArrowDown') select((selected + 1) % Math.max(1, commands.length));
    if (event.key === 'ArrowUp') select((selected - 1 + commands.length) % Math.max(1, commands.length));
    if (event.key === 'Enter' && commands[selected]) void execute(commands[selected].key);
  });
  results.addEventListener('keydown', (event) => {
    if (!['ArrowDown', 'ArrowUp'].includes(event.key)) return;
    event.preventDefault();
    select((selected + (event.key === 'ArrowDown' ? 1 : -1) + commands.length) % Math.max(1, commands.length));
    results.querySelectorAll('[data-command-key]')[selected]?.focus();
  });
  dialog.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') { event.preventDefault(); dialog.close(); }
  }, true);
  dialog.addEventListener('close', () => {
    // Navigation may already have moved focus into the selected destination.
    if (dialog.contains(document.activeElement) || document.activeElement === document.body) {
      if (returnFocus?.isConnected && returnFocus.getClientRects().length) returnFocus.focus();
      else document.querySelector('[data-workspace-switcher-launch]')?.focus();
    }
  });
  for (const event of ['codex:project-changed', 'codex:projects-rendered', 'codex:project-context-unavailable', 'codex:commands-changed']) {
    window.addEventListener(event, () => { if (dialog.open) render(); });
  }
  document.addEventListener('keydown', (event) => {
    if ((event.ctrlKey || event.metaKey) && !event.altKey && event.key.toLowerCase() === 'k') {
      event.preventDefault();
      open();
    }
  });
  return { open };
}
