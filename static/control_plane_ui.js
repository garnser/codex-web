(async () => {
const CONTROL_BASE = window.location.pathname.startsWith('/codex') ? '/codex' : '';
const { request: apiRequest } = await import(`${CONTROL_BASE}/static/api_client.js`);

function formatAge(seconds) {
  const value = Math.max(0, Number(seconds) || 0);
  if (value < 60) return `${Math.round(value)}s`;
  if (value < 3600) return `${Math.round(value / 60)}m`;
  return `${(value / 3600).toFixed(value >= 36000 ? 0 : 1)}h`;
}

function addStyles() {
  if (document.getElementById('control-plane-ui-styles')) return;
  const style = document.createElement('style');
  style.id = 'control-plane-ui-styles';
  style.textContent = `
    .operations-summary { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:8px; margin:8px 0 12px; }
    .operations-metric { border:1px solid var(--border,#313744); border-radius:8px; padding:8px; background:rgba(127,127,127,.04); }
    .operations-metric span { display:block; color:var(--muted,#9aa4b5); font-size:10px; text-transform:uppercase; letter-spacing:.04em; }
    .operations-metric strong { display:block; margin-top:4px; font-size:16px; }
    .operations-sections { display:grid; gap:7px; font-size:11px; }
    .operations-line { display:flex; justify-content:space-between; gap:10px; border-bottom:1px solid var(--border,#313744); padding-bottom:5px; }
    .operations-line:last-child { border-bottom:0; }
    .operations-health-bad { color:#e78585; }
    .operations-health-good { color:#78c99b; }
    .operations-toolbar { display:flex; align-items:center; justify-content:space-between; gap:8px; }
    .operations-toolbar small { color:var(--muted,#9aa4b5); }
    .executive-knowledge { grid-column:1/-1; border:1px solid var(--border,#313744); border-radius:8px; overflow:hidden; }
    .executive-knowledge summary { cursor:pointer; padding:8px 10px; font-size:12px; color:var(--muted,#9aa4b5); }
    .knowledge-body { padding:0 10px 10px; display:grid; gap:8px; }
    .knowledge-toolbar { display:grid; grid-template-columns:1fr 1fr auto; gap:7px; align-items:end; }
    .knowledge-editor { display:grid; grid-template-columns:2fr 1fr 1fr 90px; gap:7px; }
    .knowledge-editor .full { grid-column:1/-1; }
    .knowledge-editor textarea { min-height:72px; resize:vertical; }
    .knowledge-actions { display:flex; gap:7px; justify-content:flex-end; }
    .knowledge-list { display:grid; gap:6px; max-height:240px; overflow:auto; }
    .knowledge-item { border:1px solid var(--border,#313744); border-radius:7px; padding:8px; display:grid; gap:5px; background:rgba(127,127,127,.03); }
    .knowledge-item-head { display:flex; align-items:flex-start; justify-content:space-between; gap:8px; }
    .knowledge-item-title { font-size:12px; font-weight:600; }
    .knowledge-item-meta { color:var(--muted,#9aa4b5); font-size:10px; }
    .knowledge-item-content { font-size:11px; white-space:pre-wrap; max-height:72px; overflow:hidden; }
    .knowledge-item-buttons { display:flex; gap:5px; }
    .knowledge-status { min-height:14px; color:var(--muted,#9aa4b5); font-size:10px; }
    .knowledge-status.error { color:#e78585; }
    @media (max-width:720px) { .operations-summary { grid-template-columns:1fr 1fr; } .knowledge-toolbar,.knowledge-editor { grid-template-columns:1fr; } .knowledge-editor .full { grid-column:1; } }
  `;
  document.head.appendChild(style);
}

function metric(label, value) {
  const el = document.createElement('div');
  el.className = 'operations-metric';
  const caption = document.createElement('span');
  caption.textContent = label;
  const strong = document.createElement('strong');
  strong.textContent = String(value);
  el.append(caption, strong);
  return el;
}

function line(label, value) {
  const el = document.createElement('div');
  el.className = 'operations-line';
  const left = document.createElement('span');
  left.textContent = label;
  const right = document.createElement('strong');
  right.textContent = String(value);
  el.append(left, right);
  return el;
}

function renderOperations(card, data) {
  const runtime = data.runtime || {};
  const work = data.workItems || {};
  const providers = data.providers || {};
  const activity = data.activity || {};
  const summary = card.querySelector('.operations-summary');
  summary.innerHTML = '';
  summary.append(
    metric('Active', runtime.activeTurns || 0),
    metric('Queued', runtime.queuedTurns || 0),
    metric('Open items', work.open || 0),
    metric('Handoffs', work.pendingHandoffs || 0),
    metric('Blocked', work.blocked || 0),
    metric('Split-brain', work.splitBrain || 0),
  );
  const sections = card.querySelector('.operations-sections');
  sections.innerHTML = '';
  const health = runtime.healthy ? 'Healthy' : `Unhealthy · ${(runtime.healthProblems || []).join('; ') || 'unknown problem'}`;
  const healthLine = line('Runtime', health);
  healthLine.querySelector('strong').className = runtime.healthy ? 'operations-health-good' : 'operations-health-bad';
  sections.append(
    healthLine,
    line('Oldest queued turn', formatAge(runtime.oldestQueueAgeSeconds)),
    line('Oldest pending handoff', formatAge(work.oldestPendingHandoffAgeSeconds)),
    line('Release gates', work.releaseGates || 0),
    line('Runtime connections', providers.runtimeConnections || 0),
    line('Recent delivery failures', providers.recentDeliveryFailures || 0),
    line('GitLab sync failures', providers.gitlabSyncConsecutiveFailures || 0),
    line('Recovery activity', activity.recoveryEvents || 0),
    line('Executive activity', activity.executiveEvents || 0),
  );
  const stamp = card.querySelector('.operations-updated');
  stamp.textContent = data.generatedAt ? `Updated ${new Date(data.generatedAt * 1000).toLocaleTimeString()}` : 'Updated';
}

async function refreshOperations(card) {
  const button = card.querySelector('.operations-refresh');
  if (button) button.disabled = true;
  try {
    const data = await apiRequest('/api/operations?window_seconds=900');
    renderOperations(card, data);
  } catch (error) {
    const sections = card.querySelector('.operations-sections');
    sections.textContent = `Operations unavailable: ${error.message}`;
  } finally {
    if (button) button.disabled = false;
  }
}

function installOperations() {
  const grid = document.querySelector('#developer-panel .developer-grid');
  if (!grid || document.getElementById('operations-card')) return;
  const card = document.createElement('div');
  card.id = 'operations-card';
  card.className = 'developer-card';
  card.innerHTML = `
    <div class="operations-toolbar"><h2>Operations</h2><div><small class="operations-updated"></small> <button type="button" class="ghost-button operations-refresh">Refresh</button></div></div>
    <div class="operations-summary"></div>
    <div class="operations-sections">Open Developer to load operations.</div>
  `;
  grid.prepend(card);
  card.querySelector('.operations-refresh').addEventListener('click', () => refreshOperations(card));
  const panel = document.getElementById('developer-panel');
  panel?.addEventListener('toggle', () => { if (panel.open) refreshOperations(card); });
  if (panel?.open) refreshOperations(card);
}

function knowledgeProjectId(drawer) {
  return drawer.querySelector('#executive-project')?.value || null;
}

function setKnowledgeStatus(root, text, error = false) {
  const status = root.querySelector('.knowledge-status');
  if (!status) return;
  status.textContent = text;
  status.className = `knowledge-status${error ? ' error' : ''}`;
}

function knowledgeQuery(root, drawer) {
  const scope = root.querySelector('.knowledge-scope').value;
  const params = new URLSearchParams({ scope, limit: '100' });
  if (scope === 'project') {
    const projectId = knowledgeProjectId(drawer);
    if (projectId) params.set('project_id', projectId);
  }
  return params;
}

function resetKnowledgeEditor(root) {
  root.querySelector('.knowledge-id').value = '';
  root.querySelector('.knowledge-title').value = '';
  root.querySelector('.knowledge-tags').value = '';
  root.querySelector('.knowledge-source').value = 'manual';
  root.querySelector('.knowledge-priority').value = '50';
  root.querySelector('.knowledge-content').value = '';
  root.querySelector('.knowledge-save').textContent = 'Add knowledge';
}

function fillKnowledgeEditor(root, item) {
  root.querySelector('.knowledge-id').value = item.id || '';
  root.querySelector('.knowledge-title').value = item.title || '';
  root.querySelector('.knowledge-tags').value = (item.tags || []).join(', ');
  root.querySelector('.knowledge-source').value = item.source || 'manual';
  root.querySelector('.knowledge-priority').value = String(item.priority ?? 50);
  root.querySelector('.knowledge-content').value = item.content || '';
  root.querySelector('.knowledge-save').textContent = 'Update knowledge';
}

function renderKnowledge(root, drawer, items) {
  const list = root.querySelector('.knowledge-list');
  list.innerHTML = '';
  if (!items.length) {
    list.textContent = 'No knowledge entries in this scope.';
    return;
  }
  items.forEach((item) => {
    const card = document.createElement('div');
    card.className = 'knowledge-item';
    const head = document.createElement('div');
    head.className = 'knowledge-item-head';
    const heading = document.createElement('div');
    const title = document.createElement('div');
    title.className = 'knowledge-item-title';
    title.textContent = item.title;
    const meta = document.createElement('div');
    meta.className = 'knowledge-item-meta';
    meta.textContent = `${item.scope}${item.project_id ? ` · ${item.project_id}` : ''} · ${item.source || 'manual'} · priority ${item.priority ?? 50}${item.tags?.length ? ` · ${item.tags.join(', ')}` : ''}`;
    heading.append(title, meta);
    const buttons = document.createElement('div');
    buttons.className = 'knowledge-item-buttons';
    const edit = document.createElement('button');
    edit.type = 'button';
    edit.className = 'ghost-button';
    edit.textContent = 'Edit';
    edit.addEventListener('click', () => fillKnowledgeEditor(root, item));
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'ghost-button';
    remove.textContent = 'Delete';
    remove.addEventListener('click', async () => {
      remove.disabled = true;
      try {
        await apiRequest(`/api/executive/knowledge/${encodeURIComponent(item.id)}`, { method: 'DELETE' });
        setKnowledgeStatus(root, `Deleted ${item.title}`);
        await refreshKnowledge(root, drawer);
      } catch (error) {
        setKnowledgeStatus(root, error.message, true);
        remove.disabled = false;
      }
    });
    buttons.append(edit, remove);
    head.append(heading, buttons);
    const content = document.createElement('div');
    content.className = 'knowledge-item-content';
    content.textContent = item.content || '';
    card.append(head, content);
    list.appendChild(card);
  });
}

async function refreshKnowledge(root, drawer) {
  setKnowledgeStatus(root, 'Loading knowledge…');
  try {
    const payload = await apiRequest(`/api/executive/knowledge?${knowledgeQuery(root, drawer)}`);
    renderKnowledge(root, drawer, payload.items || []);
    setKnowledgeStatus(root, `${(payload.items || []).length} knowledge entr${(payload.items || []).length === 1 ? 'y' : 'ies'}`);
  } catch (error) {
    setKnowledgeStatus(root, `Knowledge unavailable: ${error.message}`, true);
  }
}

async function saveKnowledge(root, drawer) {
  const scope = root.querySelector('.knowledge-scope').value;
  const projectId = scope === 'project' ? knowledgeProjectId(drawer) : null;
  if (scope === 'project' && !projectId) {
    setKnowledgeStatus(root, 'Select a Codex project for project-scoped knowledge.', true);
    return;
  }
  const title = root.querySelector('.knowledge-title').value.trim();
  const content = root.querySelector('.knowledge-content').value.trim();
  if (!title || !content) {
    setKnowledgeStatus(root, 'Title and content are required.', true);
    return;
  }
  const button = root.querySelector('.knowledge-save');
  button.disabled = true;
  const id = root.querySelector('.knowledge-id').value || null;
  try {
    await apiRequest('/api/executive/knowledge', {
      method: 'POST',
      body: JSON.stringify({
        id,
        scope,
        project_id: projectId,
        title,
        content,
        tags: root.querySelector('.knowledge-tags').value.split(',').map((value) => value.trim()).filter(Boolean),
        source: root.querySelector('.knowledge-source').value.trim() || 'manual',
        priority: Number(root.querySelector('.knowledge-priority').value) || 50,
      }),
    });
    setKnowledgeStatus(root, id ? 'Knowledge updated.' : 'Knowledge added.');
    resetKnowledgeEditor(root);
    await refreshKnowledge(root, drawer);
  } catch (error) {
    setKnowledgeStatus(root, error.message, true);
  } finally {
    button.disabled = false;
  }
}

function installKnowledge(drawer) {
  const toolbar = drawer.querySelector('.executive-toolbar');
  if (!toolbar || drawer.querySelector('.executive-knowledge')) return;
  const root = document.createElement('details');
  root.className = 'executive-knowledge';
  root.innerHTML = `
    <summary>Durable company / project knowledge</summary>
    <div class="knowledge-body">
      <div class="knowledge-toolbar">
        <label>Scope<select class="knowledge-scope"><option value="company">Company</option><option value="project">Current project</option></select></label>
        <div class="knowledge-status"></div>
        <button type="button" class="ghost-button knowledge-refresh">Refresh</button>
      </div>
      <div class="knowledge-editor">
        <input type="hidden" class="knowledge-id">
        <label>Title<input class="knowledge-title" maxlength="300"></label>
        <label>Tags<input class="knowledge-tags" placeholder="security, billing"></label>
        <label>Source<input class="knowledge-source" value="manual"></label>
        <label>Priority<input class="knowledge-priority" type="number" min="0" max="100" value="50"></label>
        <label class="full">Content<textarea class="knowledge-content" maxlength="20000"></textarea></label>
      </div>
      <div class="knowledge-actions"><button type="button" class="ghost-button knowledge-clear">Clear</button><button type="button" class="ghost-button knowledge-save">Add knowledge</button></div>
      <div class="knowledge-list"></div>
    </div>
  `;
  const context = toolbar.querySelector('.executive-context');
  toolbar.insertBefore(root, context || null);
  root.querySelector('.knowledge-refresh').addEventListener('click', () => refreshKnowledge(root, drawer));
  root.querySelector('.knowledge-clear').addEventListener('click', () => resetKnowledgeEditor(root));
  root.querySelector('.knowledge-save').addEventListener('click', () => saveKnowledge(root, drawer));
  root.querySelector('.knowledge-scope').addEventListener('change', () => refreshKnowledge(root, drawer));
  drawer.querySelector('#executive-project')?.addEventListener('change', () => {
    if (root.querySelector('.knowledge-scope').value === 'project') refreshKnowledge(root, drawer);
  });
  root.addEventListener('toggle', () => { if (root.open) refreshKnowledge(root, drawer); });
}

function watchForExecutiveDrawer() {
  const install = () => {
    const drawer = document.querySelector('.executive-drawer');
    if (drawer) installKnowledge(drawer);
  };
  install();
  const observer = new MutationObserver(install);
  observer.observe(document.body, { childList: true, subtree: true });
}

addStyles();
window.addEventListener('DOMContentLoaded', () => {
  installOperations();
  watchForExecutiveDrawer();
});
})();
