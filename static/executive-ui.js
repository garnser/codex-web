const EXEC_BASE = window.location.pathname.startsWith('/codex') ? '/codex' : '';
const EXEC_SESSION_KEY = 'codex-web-executive-session';
let executiveApiModule = null;

const executiveState = {
  agents: [],
  executionRoles: [],
  activeAgent: 'chief-of-staff',
  sessionId: localStorage.getItem(EXEC_SESSION_KEY) || crypto.randomUUID(),
  lastQuestion: '',
  lastReply: '',
  lastAgent: 'chief-of-staff',
  provider: null,
};
localStorage.setItem(EXEC_SESSION_KEY, executiveState.sessionId);

async function execApi(path, options = {}) {
  executiveApiModule ||= import(`${EXEC_BASE}/static/api_client.js`);
  const { request } = await executiveApiModule;
  return request(path, options);
}

function injectExecutiveStyles() {
  if (document.getElementById('executive-ui-styles')) return;
  const style = document.createElement('style');
  style.id = 'executive-ui-styles';
  style.textContent = `
    .executive-launch { white-space: nowrap; }
    .executive-drawer { position: fixed; top: 0; right: 0; bottom: 0; width: min(720px, 94vw); z-index: 10000; background: var(--surface, #11151d); color: var(--text, #eef2f7); border-left: 1px solid var(--border, #313744); box-shadow: -20px 0 60px rgba(0,0,0,.28); transform: translateX(102%); transition: transform .18s ease; display: grid; grid-template-rows: auto auto 1fr auto; }
    .executive-drawer.open { transform: translateX(0); }
    .executive-backdrop { position: fixed; inset: 0; z-index: 9999; background: rgba(0,0,0,.38); opacity: 0; pointer-events: none; transition: opacity .18s ease; }
    .executive-backdrop.open { opacity: 1; pointer-events: auto; }
    .executive-head { display:flex; align-items:flex-start; justify-content:space-between; gap:14px; padding:16px 18px; border-bottom:1px solid var(--border, #313744); }
    .executive-head strong { display:block; font-size:16px; }
    .executive-head small { display:block; color:var(--muted, #9aa4b5); margin-top:3px; }
    .executive-toolbar { display:grid; grid-template-columns: 1fr 1fr; gap:9px; padding:12px 18px; border-bottom:1px solid var(--border, #313744); }
    .executive-toolbar select, .executive-toolbar input, .executive-context input, .executive-context textarea, .executive-composer textarea { width:100%; border:1px solid var(--border, #313744); border-radius:8px; background:var(--input-bg, #0f131b); color:inherit; padding:8px 9px; font:inherit; }
    .executive-toolbar label, .executive-context label { color:var(--muted, #9aa4b5); font-size:11px; display:grid; gap:4px; }
    .executive-toolbar .wide { grid-column:1/-1; display:flex; align-items:center; gap:8px; }
    .executive-toolbar .wide input { width:auto; }
    .executive-provider { grid-column:1/-1; font-size:11px; color:var(--muted, #9aa4b5); display:flex; gap:8px; flex-wrap:wrap; }
    .executive-provider span { padding:3px 7px; border:1px solid var(--border, #313744); border-radius:999px; }
    .executive-context { grid-column:1/-1; border:1px solid var(--border, #313744); border-radius:8px; overflow:hidden; }
    .executive-context summary { cursor:pointer; padding:8px 10px; font-size:12px; color:var(--muted, #9aa4b5); }
    .executive-context-grid { padding:0 10px 10px; display:grid; grid-template-columns:1fr 1fr; gap:7px; }
    .executive-context-grid .full { grid-column:1/-1; }
    .executive-context-actions { grid-column:1/-1; display:flex; justify-content:flex-end; }
    .executive-messages { overflow:auto; padding:16px 18px; }
    .executive-message { border:1px solid var(--border, #313744); border-radius:10px; padding:12px 13px; margin-bottom:11px; background:rgba(127,127,127,.04); }
    .executive-message.user { background:rgba(100,140,220,.08); }
    .executive-message-meta { font-size:11px; color:var(--muted, #9aa4b5); margin-bottom:7px; }
    .executive-message-body { white-space:pre-wrap; line-height:1.45; font-size:13px; }
    .executive-chips { display:flex; gap:5px; flex-wrap:wrap; margin-top:9px; }
    .executive-chip { font-size:10px; color:var(--muted, #9aa4b5); padding:3px 6px; border:1px solid var(--border, #313744); border-radius:999px; }
    .executive-message-actions { margin-top:10px; display:flex; gap:7px; }
    .executive-composer { border-top:1px solid var(--border, #313744); padding:12px 18px 15px; display:grid; grid-template-columns:1fr auto; gap:9px; align-items:end; }
    .executive-composer textarea { min-height:76px; resize:vertical; }
    .executive-status { grid-column:1/-1; min-height:16px; color:var(--muted, #9aa4b5); font-size:11px; }
    .executive-status.error { color:#e78585; }
    .executive-status.ok { color:#78c99b; }
    .executive-note { padding:12px 13px; border:1px dashed var(--border, #313744); border-radius:10px; color:var(--muted, #9aa4b5); font-size:12px; line-height:1.45; }
    @media (max-width: 640px) { .executive-toolbar { grid-template-columns:1fr; } .executive-toolbar .wide, .executive-provider, .executive-context { grid-column:1; } .executive-context-grid { grid-template-columns:1fr; } .executive-context-grid .full { grid-column:1; } }
  `;
  document.head.appendChild(style);
}

function button(label, className = 'ghost-button') {
  const el = document.createElement('button');
  el.type = 'button';
  el.className = className;
  el.textContent = label;
  return el;
}

function currentCodexProjectId(projects) {
  const activeProjectId = document.body.dataset.activeProject;
  return projects.find((project) => project.id === activeProjectId)?.id || projects[0]?.id || 'home';
}

function collectExecutiveContext(drawer) {
  const out = {};
  drawer.querySelectorAll('[data-exec-context]').forEach((field) => { out[field.dataset.execContext] = field.value; });
  return out;
}

function setExecutiveStatus(drawer, text, className = '') {
  const status = drawer.querySelector('.executive-status');
  status.textContent = text;
  status.className = `executive-status ${className}`;
}

function appendExecutiveMessage(drawer, kind, meta, body, consulted = []) {
  const messages = drawer.querySelector('.executive-messages');
  const card = document.createElement('div');
  card.className = `executive-message ${kind}`;
  const metaEl = document.createElement('div');
  metaEl.className = 'executive-message-meta';
  metaEl.textContent = meta;
  const bodyEl = document.createElement('div');
  bodyEl.className = 'executive-message-body';
  bodyEl.textContent = body;
  card.append(metaEl, bodyEl);
  if (consulted.length) {
    const chips = document.createElement('div');
    chips.className = 'executive-chips';
    consulted.forEach((item) => {
      const chip = document.createElement('span');
      chip.className = 'executive-chip';
      chip.textContent = item.title;
      chips.appendChild(chip);
    });
    card.appendChild(chips);
  }
  if (kind === 'assistant') {
    const actions = document.createElement('div');
    actions.className = 'executive-message-actions';
    const delegate = button('Delegate to Codex');
    delegate.addEventListener('click', () => delegateToCodex(drawer));
    actions.appendChild(delegate);
    card.appendChild(actions);
  }
  messages.appendChild(card);
  messages.scrollTop = messages.scrollHeight;
}

async function delegateToCodex(drawer) {
  if (!executiveState.lastQuestion || !executiveState.lastReply) return;
  const projectId = drawer.querySelector('#executive-project').value || 'home';
  const sandbox = document.getElementById('sandbox')?.value || null;
  const approvalPolicy = document.getElementById('approval-policy')?.value || null;
  const executionRoleId = drawer.querySelector('#executive-execution-role')?.value || null;
  const workItemRef = drawer.querySelector('#executive-work-item')?.value || null;
  const changeClassification = drawer.querySelector('#executive-change-classification')?.value || null;
  setExecutiveStatus(drawer, 'Delegating to Codex…');
  try {
    const result = await execApi('/api/executive/delegate', {
      method: 'POST',
      body: JSON.stringify({
        task: executiveState.lastQuestion,
        executive_reply: executiveState.lastReply,
        agent_id: executiveState.lastAgent,
        execution_role_id: executionRoleId,
        work_item_ref: workItemRef,
        change_classification: changeClassification,
        project_id: projectId,
        sandbox,
        approval_policy: approvalPolicy,
      }),
    });
    const roleName = result.executionRole?.name || 'auto-routed role';
    setExecutiveStatus(drawer, `Delegated to ${roleName} · thread ${result.threadId} · ${result.sandbox} · ${result.approvalPolicy}`, 'ok');
  } catch (error) {
    setExecutiveStatus(drawer, error.message, 'error');
  }
}

async function loadExecutiveData(drawer) {
  const [agentData, contextData, projects, workItemData] = await Promise.all([
    execApi('/api/executive/agents'),
    execApi('/api/executive/context'),
    execApi('/api/projects'),
    execApi('/api/work-items'),
  ]);
  executiveState.agents = agentData.agents || [];
  executiveState.executionRoles = agentData.executionRoles || [];
  executiveState.provider = agentData;

  const agentSelect = drawer.querySelector('#executive-agent');
  agentSelect.innerHTML = '';
  executiveState.agents.forEach((agent) => {
    const option = document.createElement('option');
    option.value = agent.id;
    option.textContent = `${agent.title} · ${agent.name}`;
    if (agent.id === executiveState.activeAgent) option.selected = true;
    agentSelect.appendChild(option);
  });

  const executionRoleSelect = drawer.querySelector('#executive-execution-role');
  executionRoleSelect.innerHTML = '<option value="">Auto-route from objective</option>';
  executiveState.executionRoles.forEach((role) => {
    const option = document.createElement('option');
    option.value = role.id;
    option.textContent = `${role.name} · ${role.lane}`;
    option.title = role.description || '';
    executionRoleSelect.appendChild(option);
  });

  const projectSelect = drawer.querySelector('#executive-project');
  const selectedProject = currentCodexProjectId(projects);
  projectSelect.innerHTML = '';
  projects.forEach((project) => {
    const option = document.createElement('option');
    option.value = project.id;
    option.textContent = project.name;
    if (project.id === selectedProject) option.selected = true;
    projectSelect.appendChild(option);
  });

  const workItemSelect = drawer.querySelector('#executive-work-item');
  workItemSelect.innerHTML = '<option value="">No canonical work item</option>';
  (workItemData.items || []).filter((item) => !item.closed_at).forEach((item) => {
    const option = document.createElement('option');
    option.value = item.ref;
    option.dataset.projectId = item.project_id || '';
    const owner = item.handoff?.status === 'pending' ? item.handoff.to_agent : (item.current_owner || item.next_owner || 'unowned');
    option.textContent = `${item.ref} · ${item.current_stage} · ${owner}${item.title ? ` · ${item.title}` : ''}`;
    workItemSelect.appendChild(option);
  });

  Object.entries(contextData.company || {}).forEach(([key, value]) => {
    const field = drawer.querySelector(`[data-exec-context="${key}"]`);
    if (field) field.value = value || '';
  });

  const provider = drawer.querySelector('.executive-provider');
  provider.innerHTML = '';
  const values = [
    `Provider: ${agentData.provider || 'openai'}`,
    `Model: ${agentData.model || 'default'}`,
    agentData.baseUrl ? `Endpoint: ${agentData.baseUrl}` : 'Endpoint: OpenAI',
  ];
  values.forEach((value) => {
    const chip = document.createElement('span');
    chip.textContent = value;
    provider.appendChild(chip);
  });
}

function buildExecutiveDrawer() {
  injectExecutiveStyles();
  const backdrop = document.createElement('div');
  backdrop.className = 'executive-backdrop';
  const drawer = document.createElement('section');
  drawer.className = 'executive-drawer';
  drawer.setAttribute('aria-hidden', 'true');
  drawer.innerHTML = `
    <div class="executive-head">
      <div><strong>Executive Control Plane</strong><small>SaaS leadership reasoning with Codex execution</small></div>
      <button type="button" class="icon-button" id="executive-close" aria-label="Close executive workspace">×</button>
    </div>
    <div class="executive-toolbar">
      <label>Mode<select id="executive-mode"><option value="advisor">Executive advisor</option><option value="board">Board review</option></select></label>
      <label>Executive<select id="executive-agent"></select></label>
      <label>Codex project<select id="executive-project"></select></label>
      <label>Canonical work item<select id="executive-work-item"><option value="">No canonical work item</option></select></label>
      <label>Execution role<select id="executive-execution-role"><option value="">Auto-route from objective</option></select></label>
      <label>Change class<select id="executive-change-classification"><option value="">Agent classifies before work</option><option value="cosmetic-only">Cosmetic only</option><option value="localized functional">Localized functional</option><option value="shared-surface">Shared surface</option><option value="release/security-sensitive">Release / security sensitive</option></select></label>
      <label class="wide"><input type="checkbox" id="executive-runtime-context"> Include Codex operational context in LLM request</label>
      <div class="executive-provider"></div>
      <details class="executive-context">
        <summary>Company context</summary>
        <div class="executive-context-grid">
          <label>Company<input data-exec-context="company_name"></label>
          <label>Stage<input data-exec-context="stage"></label>
          <label class="full">Product<input data-exec-context="product"></label>
          <label class="full">ICP<input data-exec-context="icp"></label>
          <label>ARR<input data-exec-context="arr"></label>
          <label>MRR<input data-exec-context="mrr"></label>
          <label>Team size<input data-exec-context="team_size"></label>
          <label class="full">Stack<textarea rows="2" data-exec-context="stack"></textarea></label>
          <label class="full">Priorities<textarea rows="2" data-exec-context="priorities"></textarea></label>
          <label class="full">Constraints<textarea rows="2" data-exec-context="constraints"></textarea></label>
          <div class="executive-context-actions"><button type="button" class="ghost-button" id="executive-save-context">Save context</button></div>
        </div>
      </details>
    </div>
    <div class="executive-messages">
      <div class="executive-note">Use <strong>Executive advisor</strong> for one functional leader or <strong>Board review</strong> for a multi-function decision. “Delegate to Codex” auto-routes or explicitly selects an operational execution contract (James, Dana, Quinn, Release Manager, etc.), preserves sandbox/approval policy, and keeps advisory personas separate from execution ownership.</div>
    </div>
    <div class="executive-composer">
      <textarea id="executive-prompt" placeholder="Ask about architecture, roadmap, delivery, pricing, growth, runway, churn, security…"></textarea>
      <button type="button" class="primary-button" id="executive-send">Ask</button>
      <div class="executive-status"></div>
    </div>
  `;
  document.body.append(backdrop, drawer);

  const close = () => { drawer.classList.remove('open'); backdrop.classList.remove('open'); drawer.setAttribute('aria-hidden', 'true'); };
  const open = async () => {
    drawer.classList.add('open'); backdrop.classList.add('open'); drawer.setAttribute('aria-hidden', 'false');
    try { await loadExecutiveData(drawer); setExecutiveStatus(drawer, 'Ready.'); } catch (error) { setExecutiveStatus(drawer, error.message, 'error'); }
  };
  drawer.querySelector('#executive-close').addEventListener('click', close);
  backdrop.addEventListener('click', close);
  document.addEventListener('keydown', (event) => { if (event.key === 'Escape' && drawer.classList.contains('open')) close(); });

  drawer.querySelector('#executive-agent').addEventListener('change', (event) => { executiveState.activeAgent = event.target.value; });
  drawer.querySelector('#executive-work-item').addEventListener('change', (event) => {
    const roleSelect = drawer.querySelector('#executive-execution-role');
    const projectSelect = drawer.querySelector('#executive-project');
    const selected = event.target.selectedOptions[0];
    if (event.target.value) {
      roleSelect.value = '';
      roleSelect.disabled = true;
      if (selected?.dataset.projectId) projectSelect.value = selected.dataset.projectId;
      setExecutiveStatus(drawer, 'Execution role will follow canonical work-item owner/stage.');
    } else {
      roleSelect.disabled = false;
    }
  });
  drawer.querySelector('#executive-mode').addEventListener('change', (event) => {
    drawer.querySelector('#executive-agent').disabled = event.target.value === 'board';
  });
  drawer.querySelector('#executive-save-context').addEventListener('click', async () => {
    try {
      await execApi('/api/executive/context', { method: 'POST', body: JSON.stringify({ company: collectExecutiveContext(drawer) }) });
      setExecutiveStatus(drawer, 'Company context saved.', 'ok');
    } catch (error) { setExecutiveStatus(drawer, error.message, 'error'); }
  });

  const send = async () => {
    const prompt = drawer.querySelector('#executive-prompt');
    const message = prompt.value.trim();
    if (!message) return;
    const mode = drawer.querySelector('#executive-mode').value;
    const selectedAgent = drawer.querySelector('#executive-agent').value || 'chief-of-staff';
    executiveState.lastQuestion = message;
    executiveState.lastAgent = selectedAgent;
    appendExecutiveMessage(drawer, 'user', 'You', message);
    prompt.value = '';
    setExecutiveStatus(drawer, mode === 'board' ? 'Board is reviewing…' : 'Executive is thinking…');
    drawer.querySelector('#executive-send').disabled = true;
    try {
      const result = await execApi('/api/executive/chat', {
        method: 'POST',
        body: JSON.stringify({
          message,
          session_id: executiveState.sessionId,
          agent_id: mode === 'advisor' ? selectedAgent : null,
          mode,
          company: collectExecutiveContext(drawer),
          include_runtime_context: drawer.querySelector('#executive-runtime-context').checked,
        }),
      });
      executiveState.lastReply = result.reply;
      executiveState.lastAgent = result.agent_id === 'chief-of-staff' && mode === 'board' ? selectedAgent : result.agent_id;
      appendExecutiveMessage(drawer, 'assistant', `${result.agent_name} · ${result.agent_title} · ${result.model}`, result.reply, result.consulted || []);
      setExecutiveStatus(drawer, 'Ready.', 'ok');
    } catch (error) {
      appendExecutiveMessage(drawer, 'assistant', 'Executive request failed', error.message);
      setExecutiveStatus(drawer, error.message, 'error');
    } finally {
      drawer.querySelector('#executive-send').disabled = false;
    }
  };
  drawer.querySelector('#executive-send').addEventListener('click', send);
  drawer.querySelector('#executive-prompt').addEventListener('keydown', (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') send();
  });

  return { drawer, open };
}

function installExecutiveUI() {
  const controls = document.querySelector('.topbar .controls');
  if (!controls || document.getElementById('executive-launch')) return;
  const { open } = buildExecutiveDrawer();
  const launch = button('Executive', 'ghost-button executive-launch');
  launch.id = 'executive-launch';
  launch.title = 'Open SaaS executive and Board workspace';
  launch.addEventListener('click', open);
  controls.insertBefore(launch, controls.firstChild);
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', installExecutiveUI);
else installExecutiveUI();