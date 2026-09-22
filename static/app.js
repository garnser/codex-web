import*as ep from"./execution_profile_controls.js";
import{loadProjectUiState}from"./project_ui_state.js";
import{connectProjectUiEventStream,createProjectUiEventReconciler}from"./project_ui_events.js";
import{activateProject,createProjectNavigator,initialProjectId,publishProjectsRendered}from"./project_context.js";
import{createLoggedApi}from"./frontend_api.js";
import{markMilestone,observeRender,startLongTaskObserver}from"./frontend_perf.js";
import{createExecutionPreflightUi as createPfUi}from"./execution_preflight_ui.js";
import{coerceMessageDate,formatMessageTimestamp,itemTimestamp}from"./thread_message_time.js";
import*as rtui from"./repository_target_ui.js";

const state={
  projects: [],
  projectResources: [],
  projectUiStatic: {},
  projectId: initialProjectId(),
  threads: [],
  threadId: null,
  activeAgentMessage: null,
  approvals: new Map(),
  activeTurnsByThread: new Map(),
  queuedDepthByThread: new Map(),
  waiting: false,
  eventLog: [],
  tokenUsageByThread: {},
  accountRateLimits: null,
  models: [],
  threadSettings: {},
  botConnections: [],
  botBindings: [],
  botChannels: [],
  botIntegrationTarget: null,
  gitlabIntegration: null,
  agentChannelPresence: null,
  expandedItems: new Set(),
  threadRenderPending: false,
  diagnostics: null,
  refreshTimer: null,
  refreshInFlight: null,
  refreshController: null,
  refreshGeneration: 0,
  searchTimer: null,
  commLogRenderPending: false,
  threadReplacements: new Map(),
};

const $ = (id) => document.getElementById(id);
const BASE = window.location.pathname.startsWith("/codex") ? "/codex" : "";
const THEME_KEY = "codex-web-theme";
const SETTINGS_KEY = "codex-web-project-settings";
const TOKEN_USAGE_KEY = "codex-web-token-usage";
const SIDEBAR_KEY = "codex-web-sidebar";
const GITLAB_AGENT_NAMES = ["carl", "dana", "james", "janice", "larry", "maya", "nora", "quinn", "riley", "sally", "tom"];

const SLACK_ICON_MAP = {
  ":large_blue_circle:": "🔵",
  ":large_green_circle:": "🟢",
  ":large_orange_circle:": "🟠",
  ":large_purple_circle:": "🟣",
  ":large_yellow_circle:": "🟡",
  ":red_circle:": "🔴",
  ":black_circle:": "⚫",
  ":white_circle:": "⚪",
  ":brown_circle:": "🟤",
  ":large_red_square:": "🟥",
  ":large_blue_square:": "🟦",
  ":large_green_square:": "🟩",
  ":large_yellow_square:": "🟨",
  ":large_orange_square:": "🟧",
  ":large_purple_square:": "🟪",
  ":large_brown_square:": "🟫",
  ":black_large_square:": "⬛",
  ":white_large_square:": "⬜",
  ":small_blue_diamond:": "🔹",
  ":small_orange_diamond:": "🔸",
  ":large_blue_diamond:": "🔷",
  ":large_orange_diamond:": "🔶",
  ":small_red_triangle:": "🔺",
  ":small_red_triangle_down:": "🔻",
  ":eight_pointed_black_star:": "✴",
  ":six_pointed_star:": "🔯",
  ":star:": "⭐",
  ":sparkles:": "✨",
  ":zap:": "⚡",
  ":fire:": "🔥",
  ":snowflake:": "❄",
  ":sunny:": "☀",
  ":crescent_moon:": "🌙",
  ":cloud:": "☁",
  ":umbrella:": "☂",
  ":coffee:": "☕",
  ":rocket:": "🚀",
  ":satellite:": "🛰",
  ":gear:": "⚙",
  ":mag:": "🔍",
  ":lock:": "🔒",
  ":key:": "🔑",
  ":bell:": "🔔",
  ":bookmark:": "🔖",
  ":pushpin:": "📌",
  ":paperclip:": "📎",
  ":scissors:": "✂",
  ":hammer:": "🔨",
  ":wrench:": "🔧",
  ":pick:": "⛏",
  ":shield:": "🛡",
  ":link:": "🔗",
  ":package:": "📦",
  ":battery:": "🔋",
  ":bulb:": "💡",
  ":hourglass:": "⌛",
  ":watch:": "⌚",
  ":compass:": "🧭",
  ":anchor:": "⚓",
};
const REASONING_EFFORTS=[["","Default reasoning"],["none","None"],["minimal","Minimal"],["low","Low"],["medium","Medium"],["high","High"],["xhigh","Extra high"]];
function preferredTheme(){return matchMedia("(prefers-color-scheme: dark)").matches?"dark":"light"}
function currentTheme(){return document.documentElement.dataset.theme||localStorage.getItem(THEME_KEY)||preferredTheme()}
function slackIconForThread(threadId){const bindings=state.botBindings.filter(binding=>binding.project_id===state.projectId&&binding.thread_id===threadId&&binding.provider==="slack"&&binding.slack_icon);return(bindings.find(binding=>binding.is_primary_channel)||bindings[0])?.slack_icon||""}

function slackIconGlyph(iconCode){return iconCode?(SLACK_ICON_MAP[iconCode]||iconCode.replaceAll(":","").slice(0,2).toUpperCase()):""}

function applyTheme(theme) {
  const normalized = theme === "dark" ? "dark" : "light";
  document.documentElement.dataset.theme = normalized;
  localStorage.setItem(THEME_KEY, normalized);
  const toggle = $("theme-toggle");
  if (!toggle) return;
  const isDark = normalized === "dark";
  toggle.textContent = isDark ? "☀" : "◐";
  toggle.setAttribute("aria-pressed", String(isDark));
  toggle.title = isDark ? "Switch to light mode" : "Switch to dark mode";
}

function developerPanelOpen(){return Boolean($("developer-panel")?.open)}
function scheduleCommunicationLogRender(){if(!developerPanelOpen()||state.commLogRenderPending)return;state.commLogRenderPending=true;requestAnimationFrame(()=>{state.commLogRenderPending=false;renderCommunicationLog()})}

const api=createLoggedApi(logEvent);
const pfUi=createPfUi({api,addMessage,loadThread,scheduleRefresh,logEvent});

const uiEvents=createProjectUiEventReconciler({state,api,renderThreads,reconcileWorkspace:refresh,logEvent,getSearch:()=>$("thread-search")?.value||""});

function threadHistoryController() {
  return window.codexThreadHistory || null;
}

function scrollMessagesToBottom() {
  const messages = $("messages");
  if (!messages) return;
  const history = threadHistoryController();
  if (history?.requestBottomScroll) {
    history.requestBottomScroll(messages);
    return;
  }
  messages.scrollTo({ top: messages.scrollHeight, behavior: "auto" });
}

function replacementFrom(value) {
  const detail = value?.detail || value;
  if (!detail?.staleThreadReplaced && detail?.code !== "thread_replaced") return null;
  const oldThreadId = detail.oldThreadId;
  const newThreadId = detail.newThreadId || detail.threadId;
  return oldThreadId && newThreadId ? { oldThreadId, newThreadId } : null;
}

async function applyThreadReplacement(oldThreadId, newThreadId) {
  if (!oldThreadId || !newThreadId || oldThreadId === newThreadId) return;
  state.threadReplacements.set(oldThreadId, newThreadId);
  state.activeTurnsByThread.delete(oldThreadId);
  state.queuedDepthByThread.delete(oldThreadId);
  if (state.threadSettings?.[oldThreadId] && !state.threadSettings[newThreadId]) {
    state.threadSettings[newThreadId] = state.threadSettings[oldThreadId];
  }
  if (state.tokenUsageByThread?.[oldThreadId] && !state.tokenUsageByThread[newThreadId]) {
    state.tokenUsageByThread[newThreadId] = state.tokenUsageByThread[oldThreadId];
  }
  if (state.threadId !== oldThreadId) {
    scheduleRefresh(0);
    return;
  }
  state.threadId = newThreadId;
  state.activeAgentMessage = null;
  setWaiting(false);
  await refresh();
  await loadThread(newThreadId);
}

function activeProject(){return state.projects.find(project=>project.id===state.projectId)||state.projects[0]}

function loadProjectSettings(){try{return JSON.parse(localStorage.getItem(SETTINGS_KEY)||"{}")}catch{return{}}}

function savedProjectSettings(projectId=state.projectId){return loadProjectSettings()[projectId]||{}}

function currentRunSettings() {
  const project = activeProject();
  const saved = savedProjectSettings(project?.id || state.projectId);
  return {
    sandbox: saved.sandbox || project?.sandbox || "workspace-write",
    approvalPolicy: saved.approvalPolicy || project?.approval_policy || "on-request",
    profileId:saved.profileId||ep.defaultId(),
    repositoryResourceId: saved.repositoryResourceId || "",
    readOnlyRepositoryResourceIds: Array.isArray(saved.readOnlyRepositoryResourceIds)
      ? saved.readOnlyRepositoryResourceIds
      : [],
  };
}

function repositoryTargetArgs(threadId=state.threadId){return{project:activeProject(),resources:state.projectResources||[],settings:currentRunSettings(),threadSettings:threadId?threadRunSettings(threadId):{}}}
function repositoryTargetState(threadId=state.threadId){return rtui.targetState(repositoryTargetArgs(threadId))}
function renderRepositoryTargetStatus(){rtui.renderStatus(repositoryTargetArgs())}
function renderRepositoryTargets(){rtui.renderControls({...repositoryTargetArgs(),escapeHtml})}

function threadRunSettings(threadId=state.threadId){return state.threadSettings?.[threadId]||{}}

function selectedThreadTurnOptions(threadId=state.threadId){const settings=threadRunSettings(threadId);return{model:settings.model||null,reasoningEffort:settings.reasoning_effort||null}}

function modelOptionLabel(model){return model.displayName||model.display_name||model.model||model.id||"Unnamed model"}

function modelOptionValue(model){return model.model||model.id||""}

function renderModelOptions(selectedModel) {
  const options = [`<option value="">Project/default model</option>`];
  const seen = new Set([""]);
  if (selectedModel && !state.models.some((model) => modelOptionValue(model) === selectedModel)) {
    options.push(`<option value="${escapeHtml(selectedModel)}" selected>${escapeHtml(selectedModel)}</option>`);
    seen.add(selectedModel);
  }
  state.models.forEach((model) => {
    const value = modelOptionValue(model);
    if (!value || seen.has(value)) return;
    seen.add(value);
    options.push(`<option value="${escapeHtml(value)}" ${selectedModel === value ? "selected" : ""}>${escapeHtml(modelOptionLabel(model))}</option>`);
  });
  return options.join("");
}

function renderReasoningOptions(selectedEffort) {
  return REASONING_EFFORTS
    .map(([value, label]) => `<option value="${escapeHtml(value)}" ${selectedEffort === value ? "selected" : ""}>${escapeHtml(label)}</option>`)
    .join("");
}

function applyRunSettings() {
  const settings = currentRunSettings();
  $("sandbox").value = settings.sandbox;
  $("approval-policy").value = settings.approvalPolicy;
  if ($("repository-target")) $("repository-target").value = settings.repositoryResourceId;
  ep.render(settings.profileId, escapeHtml);
}

function persistRunSettings() {
  const project = activeProject();
  if (!project) return;
  const allSettings = loadProjectSettings();
  allSettings[project.id] = {
    sandbox: $("sandbox").value,
    approvalPolicy: $("approval-policy").value,
    profileId:$("execution-profile")?.value||ep.defaultId(),
    repositoryResourceId: $("repository-target")?.value || "",
    readOnlyRepositoryResourceIds: Array.from(
      $("repository-read-context")?.selectedOptions || [],
      (option) => option.value,
    ),
  };
  localStorage.setItem(SETTINGS_KEY, JSON.stringify(allSettings));
}

async function syncThreadRunSettings() {
  if (!state.threadId) return;
  const settings = currentRunSettings();
  const response = await api(`/api/threads/${state.threadId}/settings`, {
    method: "POST",
    body: JSON.stringify({
      sandbox: settings.sandbox,
      approval_policy: settings.approvalPolicy,
    }),
  });
  state.threadSettings[state.threadId] = {
    ...threadRunSettings(state.threadId),
    sandbox: response.sandbox || null,
    approval_policy: response.approval_policy || null,
    model: response.model || null,
    reasoning_effort: response.reasoning_effort || null,
  };
  renderThreads();
}

async function updateThreadRunSettings(threadId, updates) {
  if (!threadId) return;
  const current = threadRunSettings(threadId);
  const response = await api(`/api/threads/${threadId}/settings`, {
    method: "POST",
    body: JSON.stringify({
      sandbox: current.sandbox || null,
      approval_policy: current.approval_policy || null,
      model: current.model || "",
      reasoning_effort: current.reasoning_effort || "",
      ...updates,
    }),
  });
  state.threadSettings[threadId] = {
    ...current,
    sandbox: response.sandbox || current.sandbox || null,
    approval_policy: response.approval_policy || current.approval_policy || null,
    model: response.model || null,
    reasoning_effort: response.reasoning_effort || null,
  };
  renderThreads();
}

function loadTokenUsageCache() {
  try {
    return JSON.parse(localStorage.getItem(TOKEN_USAGE_KEY) || "{}");
  } catch {
    return {};
  }
}

function saveTokenUsageCache() {
  localStorage.setItem(TOKEN_USAGE_KEY, JSON.stringify(state.tokenUsageByThread));
}

function numeric(value, fallback = 0) {
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
}

function formatTokens(value) {
  const tokens = numeric(value, 0);
  if (!tokens) return "--";
  if (tokens >= 1_000_000) return `${(tokens / 1_000_000).toFixed(tokens >= 10_000_000 ? 0 : 1)}M`;
  if (tokens >= 1_000) return `${(tokens / 1_000).toFixed(tokens >= 10_000 ? 0 : 1)}k`;
  return String(tokens);
}

function normalizeBreakdown(raw = {}) {
  return {
    totalTokens: numeric(raw.totalTokens ?? raw.total_tokens),
    inputTokens: numeric(raw.inputTokens ?? raw.input_tokens),
    cachedInputTokens: numeric(raw.cachedInputTokens ?? raw.cached_input_tokens),
    outputTokens: numeric(raw.outputTokens ?? raw.output_tokens),
    reasoningOutputTokens: numeric(raw.reasoningOutputTokens ?? raw.reasoning_output_tokens),
  };
}

function normalizeTokenUsage(raw = {}) {
  return {
    total: normalizeBreakdown(raw.total ?? raw.total_token_usage),
    last: normalizeBreakdown(raw.last ?? raw.last_token_usage),
    modelContextWindow: raw.modelContextWindow ?? raw.model_context_window ?? null,
    updatedAt: Date.now(),
  };
}

function usagePercent(usage) {
  const total = usage?.total?.totalTokens || 0;
  const windowSize = usage?.modelContextWindow || 0;
  if (!total || !windowSize) return null;
  return Math.min(100, Math.round((total / windowSize) * 100));
}

function normalizeRateLimitWindow(window = {}) {
  if (!window) return null;
  return {
    usedPercent: numeric(window.usedPercent ?? window.used_percent),
    windowDurationMins: window.windowDurationMins ?? window.window_minutes ?? null,
    resetsAt: window.resetsAt ?? window.resets_at ?? null,
  };
}

function normalizeRateLimits(raw = {}) {
  const snapshot = raw.rateLimitsByLimitId?.codex || raw.rate_limits_by_limit_id?.codex || raw.rateLimits || raw.rate_limits || raw;
  if (!snapshot) return null;
  return {
    limitId: snapshot.limitId ?? snapshot.limit_id ?? null,
    planType: snapshot.planType ?? snapshot.plan_type ?? null,
    primary: normalizeRateLimitWindow(snapshot.primary),
    secondary: normalizeRateLimitWindow(snapshot.secondary),
    rateLimitReachedType: snapshot.rateLimitReachedType ?? snapshot.rate_limit_reached_type ?? null,
  };
}

function formatLimitReset(window) {
  if (!window?.resetsAt) return "Reset time unavailable";
  return `Resets ${new Date(window.resetsAt * 1000).toLocaleString()}`;
}

function renderLimitWindow(prefix, window) {
  const label = $(`token-limit-${prefix}`);
  const fill = $(`token-limit-${prefix}-fill`);
  const reset = $(`token-limit-${prefix}-reset`);
  if (!label || !fill || !reset) return;
  if (!window) {
    label.textContent = "--";
    fill.style.width = "0%";
    reset.textContent = "Rate limit unavailable";
    return;
  }
  const percent = Math.max(0, Math.min(100, Math.round(window.usedPercent)));
  label.textContent = `${percent}% used`;
  fill.style.width = `${percent}%`;
  reset.textContent = formatLimitReset(window);
}

function renderTokenUsage() {
  const usage = state.threadId ? state.tokenUsageByThread[state.threadId] : null;
  const context = $("token-context");
  const fill = $("token-context-fill");
  const last = $("token-last");
  const total = $("token-total");
  const output = $("token-output");
  if (!context || !fill || !last || !total || !output) return;

  const percent = usagePercent(usage);
  if (usage) {
    const totalTokens = usage.total?.totalTokens || 0;
    const contextWindow = usage.modelContextWindow;
    context.textContent = percent === null
      ? `${formatTokens(totalTokens)} tokens`
      : `${percent}% of ${formatTokens(contextWindow)}`;
    fill.style.width = `${percent ?? 0}%`;
    last.textContent = formatTokens(usage.last?.totalTokens);
    total.textContent = formatTokens(totalTokens);
    output.textContent = formatTokens(usage.total?.outputTokens);
  } else {
    context.textContent = state.threadId ? "No token report yet" : "Select a thread";
    fill.style.width = "0%";
    last.textContent = "--";
    total.textContent = "--";
    output.textContent = "--";
  }

  renderLimitWindow("5h", state.accountRateLimits?.primary);
  renderLimitWindow("weekly", state.accountRateLimits?.secondary);
}

function loadSidebarPreference() {
  try {
    return JSON.parse(localStorage.getItem(SIDEBAR_KEY) || "{}");
  } catch {
    return {};
  }
}

function saveSidebarPreference(value) {
  localStorage.setItem(SIDEBAR_KEY, JSON.stringify(value));
}

function applySidebarPreference(preference = loadSidebarPreference()) {
  const width = numeric(preference.width, 336);
  document.documentElement.style.setProperty("--sidebar-width", `${Math.max(260, Math.min(520, width))}px`);
  document.body.classList.toggle("sidebar-collapsed", Boolean(preference.collapsed));
  const toggle = $("sidebar-toggle");
  if (toggle) {
    const collapsed = Boolean(preference.collapsed);
    toggle.textContent = collapsed ? "›" : "‹";
    toggle.title = collapsed ? "Expand sidebar" : "Collapse sidebar";
    toggle.setAttribute("aria-label", toggle.title);
  }
}

function setupSidebarControls() {
  const toggle = $("sidebar-toggle");
  const resizer = $("sidebar-resizer");
  toggle.addEventListener("click", () => {
    const preference = loadSidebarPreference();
    preference.collapsed = !preference.collapsed;
    saveSidebarPreference(preference);
    applySidebarPreference(preference);
  });

  resizer.addEventListener("pointerdown", (event) => {
    event.preventDefault();
    document.body.classList.add("resizing-sidebar");
    resizer.setPointerCapture(event.pointerId);
    const onMove = (moveEvent) => {
      const width = Math.max(260, Math.min(520, moveEvent.clientX));
      const preference = { ...loadSidebarPreference(), width, collapsed: false };
      saveSidebarPreference(preference);
      applySidebarPreference(preference);
    };
    const onUp = () => {
      document.body.classList.remove("resizing-sidebar");
      resizer.removeEventListener("pointermove", onMove);
      resizer.removeEventListener("pointerup", onUp);
      resizer.removeEventListener("pointercancel", onUp);
    };
    resizer.addEventListener("pointermove", onMove);
    resizer.addEventListener("pointerup", onUp);
    resizer.addEventListener("pointercancel", onUp);
  });
}

function itemExpansionKey(scope, id) {
  return `${scope}:${id}`;
}

function isItemExpanded(scope, id) {
  return state.expandedItems.has(itemExpansionKey(scope, id));
}

function toggleItemExpanded(scope, id) {
  const key = itemExpansionKey(scope, id);
  if (state.expandedItems.has(key)) {
    state.expandedItems.delete(key);
  } else {
    state.expandedItems.add(key);
  }
}

function renderProjects() {
  $("projects").innerHTML = "";
  state.projects.forEach((project)=>{
    const item = document.createElement("div");
    const expanded=isItemExpanded("project",project.id);
    item.className=`item ${project.id === state.projectId ? "active" : ""} ${expanded ? "expanded" : ""}`;
    item.innerHTML = `
      <div class="item-header">
        <div class="item-main">
          <strong>${escapeHtml(project.name)}</strong>
          <span>${escapeHtml(project.path)}</span>
        </div>
        <button type="button" class="item-expand-button" data-action="expand" aria-expanded="${expanded}" title="${expanded ? "Hide actions" : "Show actions"}">Actions</button>
      </div>
      <div class="item-actions" aria-label="Project actions" ${expanded ? "" : "hidden"}>
        <button type="button" class="item-action-button" data-action="bot">Bot Integration</button>
      </div>
    `;
    item.querySelector(".item-main").addEventListener("click",()=>selectProject(project.id));
    item.querySelector('[data-action="expand"]').addEventListener("click", (event) => {
      event.stopPropagation();
      toggleItemExpanded("project", project.id);
      renderProjects();
    });
    item.querySelector('[data-action="bot"]').addEventListener("click", (event) => {
      event.stopPropagation();
      openBotIntegration({
        scope: "project",
        projectId: project.id,
        title: project.name,
      });
    });
    $("projects").appendChild(item);
  });
  publishProjectsRendered(state.projects,state.projectId);
}

function renderThreads() {
  const startedAt=performance.now();
  const container=$("threads");
  container.innerHTML="";
  const rawThreads=state.threads?.data||state.threads?.threads||state.threads||[];
  const bindingsByThread=new Map();
  for(const binding of state.botBindings){
    if(binding.project_id!==state.projectId)continue;
    const values=bindingsByThread.get(binding.thread_id)||[];
    values.push(binding);
    bindingsByThread.set(binding.thread_id,values);
  }
  const primaryThreadIds=new Set(
    [...bindingsByThread.entries()]
      .filter(([,values])=>values.some((binding)=>binding.is_master))
      .map(([threadId])=>threadId)
  );
  const threads=[...rawThreads].sort((left,right)=>{
    const primaryDelta=Number(primaryThreadIds.has(right.id))-Number(primaryThreadIds.has(left.id));
    if(primaryDelta)return primaryDelta;
    return (right.updatedAt||0)-(left.updatedAt||0);
  });

  threads.forEach((thread)=>{
    const item=document.createElement("div");
    const title=thread.name||thread.preview||"Untitled thread";
    const updated=thread.updatedAt?new Date(thread.updatedAt*1000).toLocaleString():"";
    const expanded=isItemExpanded("thread",thread.id);
    const threadBindings=bindingsByThread.get(thread.id)||[];
    const slackIcon=(threadBindings.find((binding)=>binding.provider==="slack")||threadBindings[0])?.slack_icon||"";
    const slackIconMarkup=slackIcon
      ?`<span class="slack-thread-icon" title="${escapeHtml(`Slack icon ${slackIcon}`)}" aria-label="${escapeHtml(`Slack icon ${slackIcon}`)}">${escapeHtml(slackIconGlyph(slackIcon))}</span>`
      :"";
    const waiting=isThreadBusy(thread.id);
    const depth=queuedDepth(thread.id);
    const threadStatus=waiting
      ?`<span class="thread-state waiting"><span class="thread-state-dot"></span>Waiting for Codex</span>`
      :(depth>0?`<span class="thread-state queued">Queued ${depth}</span>`:"");

    let actionMarkup="";
    if(expanded){
      const isPrimary=primaryThreadIds.has(thread.id);
      const settings=threadRunSettings(thread.id);
      const selectedModel=settings.model||"";
      const selectedReasoningEffort=settings.reasoning_effort||"";
      const primaryChannel=threadBindings.find((binding)=>binding.is_primary_channel);
      const channelOptions=state.botChannels
        .map((channel)=>`<option value="${escapeHtml(`${channel.provider}:${channel.id}`)}" ${primaryChannel?.provider===channel.provider&&primaryChannel?.external_conversation_id===channel.id?"selected":""}>${escapeHtml(channel.label||channel.id)}</option>`)
        .join("");
      actionMarkup=`
        <div class="item-actions" aria-label="Thread actions">
          <button type="button" class="item-action-button" data-action="bot">Bot Integration</button>
          <label class="item-action-select">
            <span>Model</span>
            <select data-action="thread-model">${renderModelOptions(selectedModel)}</select>
          </label>
          <label class="item-action-select">
            <span>Reasoning</span>
            <select data-action="thread-reasoning">${renderReasoningOptions(selectedReasoningEffort)}</select>
          </label>
          <label class="item-action-check">
            <input type="checkbox" data-action="primary" ${isPrimary?"checked":""} />
            Primary catch-all
          </label>
          <label class="item-action-select">
            <span>Primary channel</span>
            <select data-action="primary-channel">
              <option value="">Default</option>
              ${channelOptions}
            </select>
          </label>
        </div>`;
    }
    item.className=`item ${thread.id===state.threadId?"active":""} ${expanded?"expanded":""}`;
    item.innerHTML=`
      <div class="item-header">
        <div class="item-main">
          <span class="thread-title-line">${slackIconMarkup}<strong>${escapeHtml(title)}</strong></span>
          <span class="thread-meta-line"><span>${escapeHtml(updated||"No activity yet")}</span>${threadStatus}</span>
        </div>
        <button type="button" class="item-expand-button" data-action="expand" aria-expanded="${expanded}" title="${expanded?"Hide actions":"Show actions"}">Actions</button>
      </div>
      ${actionMarkup}
    `;
    item.querySelector(".item-main").addEventListener("click",()=>loadThread(thread.id));
    item.querySelector('[data-action="expand"]').addEventListener("click",(event)=>{
      event.stopPropagation();
      toggleItemExpanded("thread",thread.id);
      renderThreads();
    });
    item.querySelector('[data-action="bot"]')?.addEventListener("click",(event)=>{
      event.stopPropagation();
      openBotIntegration({scope:"thread",projectId:state.projectId,threadId:thread.id,title});
    });
    item.querySelector('[data-action="thread-model"]')?.addEventListener("change",async(event)=>{
      event.stopPropagation();
      await updateThreadRunSettings(thread.id,{model:event.target.value});
    });
    item.querySelector('[data-action="thread-reasoning"]')?.addEventListener("change",async(event)=>{
      event.stopPropagation();
      await updateThreadRunSettings(thread.id,{reasoning_effort:event.target.value});
    });
    item.querySelector('[data-action="primary"]')?.addEventListener("change",async(event)=>{
      event.stopPropagation();
      const response=await api(`/api/threads/${thread.id}/primary`,{
        method:"POST",
        body:JSON.stringify({primary:event.target.checked,project_id:state.projectId}),
      });
      state.botBindings=response.bindings||state.botBindings;
      await refresh();
    });
    item.querySelector('[data-action="primary-channel"]')?.addEventListener("change",async(event)=>{
      event.stopPropagation();
      const [provider,...channelParts]=event.target.value.split(":");
      const channelId=channelParts.join(":")||null;
      const response=await api(`/api/threads/${thread.id}/primary-channel`,{
        method:"POST",
        body:JSON.stringify({
          project_id:state.projectId,
          provider:provider||"slack",
          external_conversation_id:channelId,
        }),
      });
      state.botBindings=response.bindings||state.botBindings;
      await refresh();
    });
    container.appendChild(item);
  });
  observeRender("threads",startedAt,{
    rows:threads.length,
    nodes:container.childElementCount,
  });
}

function scheduleRenderThreads() {
  if (!$("threads") || state.threadRenderPending) return;
  state.threadRenderPending = true;
  requestAnimationFrame(() => {
    state.threadRenderPending = false;
    renderThreads();
  });
}

function renderApprovals() {
  const container = $("approvals");
  container.innerHTML = "";
  state.approvals.forEach((request, id) => {
    const params = request.params || {};
    const command = params.command || params.reason || request.method;
    const box = document.createElement("div");
    box.className = "approval";
    box.innerHTML = `
      <strong>Approval requested</strong>
      <pre>${escapeHtml(command)}</pre>
      <div class="approval-actions">
        <button data-decision="accept">Approve once</button>
        <button data-decision="acceptForSession">Approve session</button>
        <button class="deny" data-decision="decline">Deny</button>
      </div>
    `;
    box.querySelectorAll("button").forEach((button) => {
      button.addEventListener("click", async () => {
        await api(`/api/approvals/${id}`, {
          method: "POST",
          body: JSON.stringify({ decision: button.dataset.decision }),
        });
        state.approvals.delete(id);
        renderApprovals();
      });
    });
    container.appendChild(box);
  });
}

function setWaiting(waiting, label = "Waiting for Codex", mode = waiting ? "waiting" : "idle") {
  state.waiting = waiting;
  const status = $("codex-status");
  const text = $("codex-status-text");
  if (!status || !text) return;
  status.classList.toggle("waiting", waiting);
  status.classList.toggle("idle", !waiting);
  text.textContent = waiting ? label : "Idle";
}

function queuedDepth(threadId) {
  return Number(state.queuedDepthByThread.get(threadId) || 0);
}

function setThreadQueueDepth(threadId, depth) {
  if (!threadId) return;
  const normalized = Math.max(0, Number(depth || 0));
  if (normalized) {
    state.queuedDepthByThread.set(threadId, normalized);
  } else {
    state.queuedDepthByThread.delete(threadId);
  }
  scheduleRenderThreads();
  updateWaitingFromState();
}

function ensureActiveTurns(threadId) {
  if (!threadId) return null;
  if (!state.activeTurnsByThread.has(threadId)) {
    state.activeTurnsByThread.set(threadId, new Set());
  }
  return state.activeTurnsByThread.get(threadId);
}

function markThreadBusy(threadId, turnId = "__active__") {
  const turns = ensureActiveTurns(threadId);
  if (turns) turns.add(turnId || "__active__");
  scheduleRenderThreads();
  updateWaitingFromState();
}

function clearThreadBusy(threadId, turnId = null) {
  if (!threadId || !state.activeTurnsByThread.has(threadId)) return;
  if (turnId) {
    state.activeTurnsByThread.get(threadId).delete(turnId);
  } else {
    state.activeTurnsByThread.delete(threadId);
    scheduleRenderThreads();
    updateWaitingFromState();
    return;
  }
  if (state.activeTurnsByThread.get(threadId).size === 0) {
    state.activeTurnsByThread.delete(threadId);
  }
  scheduleRenderThreads();
  updateWaitingFromState();
}

function isThreadBusy(threadId) {
  return Boolean(threadId && state.activeTurnsByThread.get(threadId)?.size);
}

function approvalThreadId(request) {
  return request?.params?.threadId || request?.params?.turn?.threadId || null;
}

function activeApprovalForThread(threadId) {
  for (const request of state.approvals.values()) {
    const approvalThread = approvalThreadId(request);
    if (!approvalThread || approvalThread === threadId) return true;
  }
  return false;
}

function updateWaitingFromState() {
  if (activeApprovalForThread(state.threadId)) {
    setWaiting(true, "Waiting for approval");
  } else if (isThreadBusy(state.threadId)) {
    setWaiting(true, "Waiting for Codex");
  } else {
    setWaiting(false);
  }
}

async function refreshQueueStatus(threadId = state.threadId) {
  if (!threadId) return;
  try {
    const status = await api(`/api/threads/${threadId}/queue`);
    setThreadQueueDepth(threadId, status.queueDepth || 0);
    if (status.active) markThreadBusy(threadId);
  } catch (error) {
    logEvent("queue.error", { message: error.message });
  }
}

function hydrateThreadActivity(thread) {
  if (!thread?.id) return;
  clearThreadBusy(thread.id);
  if (thread.status?.type === "active") {
    markThreadBusy(thread.id);
  }
  (thread.turns || []).forEach((turn) => {
    if (turn?.status === "inProgress") markThreadBusy(thread.id, turn.id);
  });
  updateWaitingFromState();
}

function hydrateThreadListActivity(threads) {
  (threads || []).forEach((thread) => {
    if (!thread?.id || !thread.status?.type) return;
    if (thread.status.type === "active") {
      markThreadBusy(thread.id);
    } else if (thread.status.type === "idle" || thread.status.type === "systemError" || thread.status.type === "notLoaded") {
      clearThreadBusy(thread.id);
    }
  });
  updateWaitingFromState();
}

function clearMessages() {
  $("messages").innerHTML = "";
  state.activeAgentMessage = null;
}

function truncateCommandOutput(text, limit = 60000) {
  const value = String(text || "");
  if (value.length <= limit) return value;
  return `${value.slice(0, limit)}\n\n... truncated ${value.length - limit} characters`;
}

function commandPreview(command) {
  const value = String(command || "").trim();
  if (!value) return "Command details";
  return value.replace(/\s+/g, " ");
}

function addMessage(role, text, type = role, timestamp = new Date()) {
  const message = document.createElement("article");
  message.className = `message ${type}`;
  message.innerHTML = `
    <div class="message-header">
      <div class="role">${escapeHtml(role)}</div>
      <time datetime="${timestamp.toISOString()}">${escapeHtml(formatMessageTimestamp(timestamp))}</time>
    </div>
    <div class="body"></div>
  `;
  message.querySelector(".body").textContent = text || "";
  $("messages").appendChild(message);
  scrollMessagesToBottom();
  return message;
}

function resizePromptInput() {
  const prompt = $("prompt");
  if (!prompt) return;
  prompt.style.height = "auto";
  prompt.style.height = `${Math.min(prompt.scrollHeight, 260)}px`;
}

function attachQueuedSteer(message, threadId, queuedId = null) {
  if (!message || !threadId) return;
  message.classList.add("queued-message");
  message.dataset.threadId = threadId;
  if (queuedId) message.dataset.queuedId = queuedId;
  let actions = message.querySelector(".message-actions");
  if (!actions) {
    actions = document.createElement("div");
    actions.className = "message-actions";
    message.appendChild(actions);
  }
  let button = actions.querySelector("[data-action='steer']");
  if (!button) {
    button = document.createElement("button");
    button.type = "button";
    button.className = "message-action";
    button.dataset.action = "steer";
    button.textContent = "Steer now";
    button.addEventListener("click", () => steerQueuedMessage(message).catch((error) => {
      addMessage("Queue", error.message, "tool", new Date());
    }));
    actions.appendChild(button);
  }
  button.disabled = !message.dataset.queuedId;
  button.title = button.disabled ? "Waiting for queue id" : "Interrupt current turn and send this message now";
}

function setQueuedMessageId(message, queuedId) {
  if (!message || !queuedId) return;
  message.dataset.queuedId = queuedId;
  attachQueuedSteer(message, message.dataset.threadId || state.threadId, queuedId);
}

function addFileChangeMessage(changes, timestamp = new Date()) {
  const normalizedChanges = Array.isArray(changes) ? changes : [];
  const message = document.createElement("article");
  message.className = "message file-change";

  const summaryText = normalizedChanges.length === 1
    ? "1 file changed"
    : `${normalizedChanges.length} files changed`;
  const paths = normalizedChanges
    .slice(0, 4)
    .map((change) => `${change.kind || "change"}: ${change.path || "unknown path"}`)
    .join("\n");
  const overflow = normalizedChanges.length > 4 ? `\n+${normalizedChanges.length - 4} more` : "";

  message.innerHTML = `
    <div class="message-header">
      <div class="role">File change</div>
      <time datetime="${timestamp.toISOString()}">${escapeHtml(formatMessageTimestamp(timestamp))}</time>
    </div>
    <details class="file-change-details">
      <summary>
        <span>${escapeHtml(summaryText)}</span>
        <small>Expand diff</small>
      </summary>
      <pre></pre>
    </details>
  `;
  const details = message.querySelector("details");
  const summary = message.querySelector("summary");
  const pre = message.querySelector("pre");
  summary.title = paths ? `${paths}${overflow}` : summaryText;
  pre.textContent = normalizedChanges
    .map((change) => {
      const path = change.path || "unknown path";
      const kind = change.kind || "change";
      const diff = change.diff || "";
      return `# ${kind}: ${path}\n${diff}`;
    })
    .join("\n\n");
  if (!pre.textContent) pre.textContent = "No diff content was provided.";
  details.addEventListener("toggle", () => {
    const label = details.open ? "Collapse diff" : "Expand diff";
    summary.querySelector("small").textContent = label;
  });
  $("messages").appendChild(message);
  scrollMessagesToBottom();
  return message;
}

function addCommandMessage(label, command, output = "", open = false, timestamp = new Date()) {
  const message = document.createElement("article");
  message.className = "message command-message";
  const normalizedCommand = String(command || "").trim();
  const normalizedOutput = String(output || "").trim();
  const hasCommand = Boolean(normalizedCommand);
  const hasOutput = Boolean(normalizedOutput);
  const summaryText = commandPreview(normalizedCommand);
  message.innerHTML = `
    <div class="message-header">
      <div class="role">${escapeHtml(label)}</div>
      <time datetime="${timestamp.toISOString()}">${escapeHtml(formatMessageTimestamp(timestamp))}</time>
    </div>
    <details class="command-details"${open ? " open" : ""}>
      <summary>
        <span></span>
        <small>${hasOutput ? "Expand output" : "Expand command"}</small>
      </summary>
      <pre></pre>
    </details>
  `;
  const details = message.querySelector("details");
  const summary = message.querySelector("summary");
  const pre = message.querySelector("pre");
  summary.querySelector("span").textContent = summaryText;
  summary.title = normalizedCommand || summaryText;
  const commandBlock = hasCommand ? `$ ${normalizedCommand}` : "$ Command content was not provided.";
  pre.textContent = truncateCommandOutput(
    hasOutput ? `${commandBlock}\n\n${normalizedOutput}` : commandBlock
  );
  details.addEventListener("toggle", () => {
    const expandedLabel = hasOutput ? "Collapse output" : "Collapse command";
    const collapsedLabel = hasOutput ? "Expand output" : "Expand command";
    summary.querySelector("small").textContent = details.open ? expandedLabel : collapsedLabel;
  });
  $("messages").appendChild(message);
  scrollMessagesToBottom();
  return message;
}

function appendAgentDelta(text) {
  if (!state.activeAgentMessage) {
    state.activeAgentMessage = addMessage("Codex", "", "agent", new Date());
  }
  const body = state.activeAgentMessage.querySelector(".body");
  body.textContent += text;
  scrollMessagesToBottom();
}

function displayUserMessageText(text) {
  const match = String(text || "").match(/^Message received from .+? conversation .+? by .+?\.\n\n([\s\S]*)$/);
  return match ? match[1] : text;
}

function renderThread(thread) {
  clearMessages();
  const title = thread.name || thread.preview || "Untitled thread";
  $("thread-title").textContent = title;
  const boundRepository = threadRunSettings(thread.id)?.repository_resource_id || "";
  const repositoryMeta = boundRepository ? ` · repository ${boundRepository}` : "";
  $("thread-meta").textContent = `${thread.id} · ${thread.cwd || ""}${repositoryMeta}`;
  const turns = thread.turns || [];
  turns.forEach((turn) => {
    (turn.items || []).forEach((item) => renderItem(item, turn));
  });
  pfUi.render(thread.id);
}

function renderNewThreadShell(thread) {
  clearMessages();
  $("thread-title").textContent = thread.name || "Untitled thread";
  const repositoryId = currentRunSettings().repositoryResourceId;
  const repositoryMeta = repositoryId ? ` · repository ${repositoryId}` : "";
  $("thread-meta").textContent = `${thread.id} · ${thread.cwd || activeProject()?.path || ""}${repositoryMeta}`;
  renderRepositoryTargetStatus();
}

function renderItem(item, turn = {}) {
  const timestamp = itemTimestamp(item, turn);
  if (item.type === "userMessage") {
    const text = (item.content || []).map((part) => part.text || part.path || part.url || "").join("\n");
    addMessage("You", displayUserMessageText(text), "user", timestamp);
  } else if (item.type === "agentMessage") {
    addMessage("Codex", item.text || "", "agent", timestamp);
  } else if (item.type === "commandExecution") {
    addCommandMessage("Command", item.command || "", item.aggregatedOutput || "", false, timestamp);
  } else if (item.type === "fileChange") {
    addFileChangeMessage(item.changes || [], timestamp);
  } else if (item.type === "reasoning" && item.summary?.length) {
    addMessage("Reasoning", item.summary.join("\n"), "tool", timestamp);
  }
}

async function refresh({ reloadProjects = false } = {}) {
  const generation=state.refreshGeneration+1;
  state.refreshGeneration=generation;
  state.refreshController?.abort();
  const controller=new AbortController();
  state.refreshController=controller;
  const projectId=state.projectId;
  const search=$("thread-search").value.trim();
  const startedAt=performance.now();

  const task=(async()=>{
    const snapshot=await loadProjectUiState({
      api,
      projectId,
      search,
      projects:state.projects,
      reloadProjects,
      models:state.models,
      cachedStatic:state.projectUiStatic[projectId]||null,
      signal:controller.signal,
      onModelError:(error)=>{
        logEvent("models.error",{message:error.message});
      },
    });
    if(
      controller.signal.aborted
      || generation!==state.refreshGeneration
      || projectId!==state.projectId
      || search!==$("thread-search").value.trim()
    )return;

    state.projects=snapshot.projects;
    state.models=snapshot.models;
    state.projectResources=snapshot.resources;
    state.botBindings=snapshot.bindings;
    state.threadSettings=snapshot.threadSettings;
    state.botChannels=snapshot.channels;
    state.threads=snapshot.threads;
    if(snapshot.staticState){
      state.projectUiStatic[projectId]=snapshot.staticState;
      if(snapshot.staticState.executionProfiles){
        ep.setCatalog(snapshot.staticState.executionProfiles);
      }
    }

    renderRepositoryTargets();
    applyRunSettings();
    renderGitLabIntegration();
    renderAgentChannelPresence();
    const threads=(
      state.threads?.data
      ||state.threads?.threads
      ||state.threads
      ||[]
    );
    hydrateThreadListActivity(threads);
    renderProjects();
    renderThreads();
    markMilestone("project-useful",startedAt);
  })();

  state.refreshInFlight=task;
  try{
    await task;
  }catch(error){
    if(error?.name!=="AbortError")throw error;
  }finally{
    if(state.refreshInFlight===task)state.refreshInFlight=null;
    if(state.refreshController===controller)state.refreshController=null;
  }
}

function scheduleRefresh(delay = 100) {
  if (state.refreshTimer) clearTimeout(state.refreshTimer);
  state.refreshTimer = setTimeout(() => {
    state.refreshTimer = null;
    refresh().catch(console.error);
  }, delay);
}

async function refreshModels() {
  try {
    const response = await api("/api/models");
    state.models = Array.isArray(response.data) ? response.data : [];
  } catch (error) {
    logEvent("models.error", { message: error.message });
    state.models = [];
  }
}

async function refreshBotConnections() {
  state.botConnections = await api("/api/bots/connections");
  renderBotConnectionOptions();
}

function renderBotConnectionOptions(selectedId = $("bot-connection")?.value || "") {
  const select = $("bot-connection");
  if (!select) return;
  const provider = $("bot-provider")?.value || "slack";
  select.innerHTML = `<option value="">New connection</option>`;
  state.botConnections
    .filter((connection) => connection.provider === provider)
    .forEach((connection) => {
      const option = document.createElement("option");
      option.value = connection.id;
      option.textContent = connection.name;
      select.appendChild(option);
    });
  select.value = state.botConnections.some((connection) => connection.id === selectedId && connection.provider === provider) ? selectedId : "";
}

function applyBotProvider(provider) {
  const isSlack = provider === "slack";
  $("bot-provider").value = isSlack ? "slack" : "telegram";
  $("bot-token-label").textContent = isSlack ? "Slack bot token" : "Telegram bot token";
  $("bot-token").placeholder = isSlack
    ? "xoxb token; leave blank to keep existing"
    : "Telegram bot token; leave blank to keep existing";
  $("bot-slack-app-token").placeholder = "xapp token for Socket Mode";
  $("bot-signing-secret").placeholder = "Used to verify Slack events";
  $("bot-webhook-secret").placeholder = "Secret token sent by Telegram";
  $("slack-fields").hidden = !isSlack;
  $("telegram-fields").hidden = isSlack;
  $("bot-slack-app-token").required = isSlack && !$("bot-connection").value;
  $("bot-conversation-id").required = isSlack;
  $("bot-telegram-chat-id").required = !isSlack;
  $("bot-signing-secret").required = false;
  $("bot-webhook-secret").required = false;
  if (isSlack) {
    $("bot-telegram-chat-id").value = "";
    $("bot-webhook-secret").value = "";
  } else {
    $("bot-conversation-id").value = "";
    $("bot-slack-app-token").value = "";
    $("bot-signing-secret").value = "";
    $("bot-post-in-thread").checked = false;
  }
}

function selectedBotConnection() {
  const id = $("bot-connection").value;
  return state.botConnections.find((connection) => connection.id === id) || null;
}

function fillBotDialogFromConnection(connection) {
  if (!connection) {
    const target = state.botIntegrationTarget;
    $("bot-name").value = target ? `${target.title} bot` : "";
    $("bot-token").value = "";
    $("bot-slack-app-token").value = "";
    $("bot-signing-secret").value = "";
    $("bot-webhook-secret").value = "";
    $("bot-conversation-id").value = "";
    $("bot-telegram-chat-id").value = "";
    $("bot-external-name").value = "";
    if (target) $("bot-route-prefix").value = target.title;
    applyBotProvider($("bot-provider").value);
    return;
  }
  $("bot-provider").value = connection.provider;
  applyBotProvider(connection.provider);
  renderBotConnectionOptions(connection.id);
  $("bot-name").value = connection.name || "";
  $("bot-token").value = "";
  $("bot-token").placeholder = connection.bot_token
    ? `${connection.bot_token} stored`
    : (connection.provider === "slack" ? "xoxb token; leave blank to keep existing" : "Telegram bot token; leave blank to keep existing");
  $("bot-slack-app-token").value = "";
  $("bot-slack-app-token").placeholder = connection.slack_app_token ? `${connection.slack_app_token} stored` : "xapp token for Socket Mode";
  $("bot-signing-secret").value = "";
  $("bot-signing-secret").placeholder = connection.signing_secret ? `${connection.signing_secret} stored` : "Used to verify Slack events";
  $("bot-webhook-secret").value = "";
  $("bot-webhook-secret").placeholder = connection.webhook_secret ? `${connection.webhook_secret} stored` : "Secret token sent by Telegram";
  $("bot-conversation-id").value = connection.provider === "slack" ? (connection.default_external_conversation_id || "") : "";
  $("bot-telegram-chat-id").value = connection.provider === "telegram" ? (connection.default_external_conversation_id || "") : "";
  $("bot-external-name").value = connection.default_external_name || "";
  $("bot-post-in-thread").checked = false;
}

async function openBotIntegration(target) {
  state.botIntegrationTarget = target;
  const bindExisting = target.scope === "thread";
  $("bot-context").textContent = `${target.scope === "thread" ? "Thread" : "Project"}: ${target.title}`;
  $("bot-connection").value = "";
  applyBotProvider("slack");
  $("bot-name").value = `${target.title} bot`;
  $("bot-token").value = "";
  $("bot-slack-app-token").value = "";
  $("bot-signing-secret").value = "";
  $("bot-webhook-secret").value = "";
  $("bot-conversation-id").value = "";
  $("bot-telegram-chat-id").value = "";
  $("bot-post-in-thread").checked = false;
  $("bot-external-name").value = "";
  $("bot-route-prefix").value = target.title;
  $("bot-bind-existing-thread").checked = bindExisting;
  $("bot-bind-existing-thread").disabled = !bindExisting;
  $("bot-result").hidden = true;
  const dialog = $("bot-dialog");
  if (!dialog.open) dialog.showModal();
  try {
    await refreshBotConnections();
  } catch (error) {
    $("bot-result").hidden = false;
    $("bot-result").textContent = error.message;
  }
}

async function saveBotIntegration(event) {
  event.preventDefault();
  const target = state.botIntegrationTarget;
  if (!target) return;
  const provider = $("bot-provider").value;
  const conversationId = provider === "slack"
    ? $("bot-conversation-id").value.trim()
    : $("bot-telegram-chat-id").value.trim();
  if (!conversationId) {
    $("bot-result").hidden = false;
    $("bot-result").textContent = provider === "slack" ? "Slack channel ID is required." : "Telegram chat ID is required.";
    return;
  }
  const connectionPayload = {
    id: $("bot-connection").value || null,
    provider,
    name: $("bot-name").value.trim(),
    project_id: target.projectId,
    bot_token: $("bot-token").value.trim() || null,
    slack_app_token: provider === "slack" ? ($("bot-slack-app-token").value.trim() || null) : null,
    signing_secret: provider === "slack" ? ($("bot-signing-secret").value.trim() || null) : null,
    webhook_secret: provider === "telegram" ? ($("bot-webhook-secret").value.trim() || null) : null,
    default_external_conversation_id: conversationId,
    default_external_name: $("bot-external-name").value.trim() || null,
  };
  const connection = await api("/api/bots/connections", {
    method: "POST",
    body: JSON.stringify(connectionPayload),
  });
  const bindToThread = target.scope === "thread" && $("bot-bind-existing-thread").checked;
  const binding = await api("/api/bots/bindings", {
    method: "POST",
    body: JSON.stringify({
      connection_id: connection.id,
      provider,
      external_conversation_id: conversationId,
      external_name: $("bot-external-name").value.trim() || null,
      project_id: target.projectId,
      thread_id: bindToThread ? target.threadId : null,
      thread_name: bindToThread ? null : $("bot-route-prefix").value.trim(),
      route_prefix: $("bot-route-prefix").value.trim() || target.title,
      post_in_thread: provider === "slack" ? $("bot-post-in-thread").checked : false,
      sandbox: currentRunSettings().sandbox,
      approval_policy: currentRunSettings().approvalPolicy,
    }),
  });
  $("bot-result").hidden = false;
  $("bot-result").textContent = `Saved ${connection.name}; bound ${provider} conversation ${conversationId} to thread ${binding.thread_id}.`;
  await refreshBotConnections();
  await refresh();
  $("bot-dialog").close();
}

async function loadThread(threadId) {
  state.threadId = threadId;
  updateWaitingFromState();
  renderTokenUsage();
  const history = threadHistoryController();
  const readQs = new URLSearchParams();
  const messageLimit = history?.messageLimit?.(threadId);
  if (messageLimit) readQs.set("message_limit", String(messageLimit));
  const query = readQs.toString();
  const [data]=await Promise.all([api(`/api/threads/${threadId}${query?`?${query}`:""}`),pfUi.load(threadId)]);
  const thread = data.thread || data;
  history?.recordThread?.(threadId, thread);
  hydrateThreadActivity(thread);
  await refreshQueueStatus(threadId);
  renderThread(thread);
  history?.afterThreadRendered?.(threadId, $("messages"));
  renderThreads();
  renderTokenUsage();
  renderRepositoryTargetStatus();
  updateWaitingFromState();
}

threadHistoryController()?.configure?.({ reloadThread: loadThread });

async function newThread() {
  persistRunSettings();
  const settings = currentRunSettings();
  const qs = new URLSearchParams({
    project_id: state.projectId,
    sandbox: settings.sandbox,
    approval_policy: settings.approvalPolicy,
  });
  ep.applyThreadQuery(qs,settings);
  if(settings.repositoryResourceId)qs.set("repository_resource_id",settings.repositoryResourceId);
  settings.readOnlyRepositoryResourceIds.forEach((id) => {
    qs.append("read_only_repository_resource_id", id);
  });
  const data = await api(`/api/threads?${qs}`, { method: "POST" });
  const thread = data.thread || data;
  state.threadId = thread.id;
  setWaiting(false);
  renderNewThreadShell(thread);
  renderTokenUsage();
 scheduleRefresh(100);
}

function blockRepositoryTarget(target) {
  if (!target.blocked) return false;
  renderRepositoryTargetStatus();
  $("repository-target")?.focus();
  addMessage("Execution blocked", `${target.code}: ${target.message}`, "tool", new Date());
  return true;
}

async function sendPrompt() {
  const prompt = $("prompt").value.trim();
  if (!prompt) return;
  persistRunSettings();
  if (blockRepositoryTarget(repositoryTargetState())) return;
  if (!state.threadId) await newThread();
  const threadId = state.threadId;
  if (blockRepositoryTarget(repositoryTargetState(threadId))) return;
  const willQueue = isThreadBusy(threadId) || queuedDepth(threadId) > 0;
  $("prompt").value = "";
  resizePromptInput();
  state.activeAgentMessage = null;
  const message = addMessage(willQueue ? "You (queued)" : "You", prompt, "user", new Date());
  if (willQueue) {
    setThreadQueueDepth(threadId, queuedDepth(threadId) + 1);
    attachQueuedSteer(message, threadId);
  } else {
    markThreadBusy(threadId);
  }
  const threadOptions = selectedThreadTurnOptions(threadId);
  const runSettings = currentRunSettings();
  const selectedThreadSettings = threadRunSettings(threadId);
  const turnPayload = {
    message: prompt,
    project_id: state.projectId,
    sandbox: runSettings.sandbox,
    approval_policy: runSettings.approvalPolicy,
    model: threadOptions.model,
    reasoning_effort: threadOptions.reasoningEffort,
    repository_resource_id: selectedThreadSettings.repository_resource_id || runSettings.repositoryResourceId || null,
    ...rtui.scope(),
    read_only_repository_resource_ids: selectedThreadSettings.read_only_repository_resource_ids || runSettings.readOnlyRepositoryResourceIds || [],
    execution_profile_id:selectedThreadSettings.execution_profile_id||runSettings.profileId||ep.defaultId(),
  };
  try {
    let targetThreadId = threadId;
    let response;
    try {
      response = await api(`/api/threads/${targetThreadId}/turns`, {
        method: "POST",
        body: JSON.stringify(turnPayload),
      });
    } catch (error) {
      const replacement = replacementFrom(error);
      if (!replacement) throw error;
      await applyThreadReplacement(replacement.oldThreadId, replacement.newThreadId);
      targetThreadId = replacement.newThreadId;
      response = await api(`/api/threads/${targetThreadId}/turns`, {
        method: "POST",
        body: JSON.stringify(turnPayload),
      });
    }
    const replacement = replacementFrom(response);
    if (replacement) {
      await applyThreadReplacement(replacement.oldThreadId, replacement.newThreadId);
      targetThreadId = replacement.newThreadId;
      response = await api(`/api/threads/${targetThreadId}/turns`, {
        method: "POST",
        body: JSON.stringify(turnPayload),
      });
    }
    if (response.queued) {
      attachQueuedSteer(message, targetThreadId, response.queuedId);
      setQueuedMessageId(message, response.queuedId);
      setThreadQueueDepth(targetThreadId, response.queueDepth || queuedDepth(targetThreadId) || 1);
    }
  } catch (error) {
    if (willQueue) {
      setThreadQueueDepth(threadId, Math.max(0, queuedDepth(threadId) - 1));
    } else {
      clearThreadBusy(threadId);
    }
    if(!(await pfUi.handleError(error,threadId)))addMessage("Error",error.message,"tool",new Date());
  }
}

async function steerQueuedMessage(message) {
  const threadId = message?.dataset?.threadId || state.threadId;
  const queuedId = message?.dataset?.queuedId;
  if (!threadId || !queuedId) return;
  const button = message.querySelector("[data-action='steer']");
  if (button) {
    button.disabled = true;
    button.textContent = "Steering...";
  }
  markThreadBusy(threadId);
  try {
    const response = await api(`/api/threads/${threadId}/queue/${queuedId}/steer`, { method: "POST" });
    setThreadQueueDepth(threadId, response.queueDepth || 0);
    if (button) button.textContent = "Steered";
    message.classList.remove("queued-message");
  } catch (error) {
    if (button) {
      button.disabled = false;
      button.textContent = "Steer now";
    }
    addMessage("Queue", error.message, "tool", new Date());
    await refreshQueueStatus(threadId);
  }
}

async function renameThread() {
  if (!state.threadId) return;
  const currentTitle = $("thread-title").textContent === "No thread selected" ? "" : $("thread-title").textContent;
  const name = window.prompt("Thread name", currentTitle);
  if (!name || !name.trim()) return;
  const threadId = state.threadId;
  await api(`/api/threads/${threadId}/name`, {
    method: "POST",
    body: JSON.stringify({ name: name.trim() }),
  });
  $("thread-title").textContent = name.trim();
  uiEvents.patchThread(threadId,{name:name.trim(),updatedAt:Date.now()/1000});
}

function handleEvent(event) {
  if(event.type==="work_item.run.updated"){window.dispatchEvent(new CustomEvent("codex:work-item-run-updated",{detail:event}));return;}
  if(event.type==="binding.updated"){uiEvents.handleBindingEvent(event);return;}
  if (event.type === "bot.thread.replaced") {
    applyThreadReplacement(event.oldThreadId, event.newThreadId).catch((error) => {
      logEvent("thread.replacement.error", { message: error.message });
    });
    return;
  }
  if (event.type === "bot.inbound") {
    if (event.threadId === state.threadId) {
      const message = addMessage(event.queued ? "You (queued)" : "You", event.text || "", "user", new Date());
      if (event.queued) {
        attachQueuedSteer(message, event.threadId, event.queuedId);
        setThreadQueueDepth(event.threadId, event.queueDepth || 1);
      } else {
        setWaiting(true, "Waiting for Codex");
      }
    }
    uiEvents.handleBotInbound(event);
    return;
  }
  if (event.type === "queue.status") {
    setThreadQueueDepth(event.threadId, event.queueDepth || 0);
    if (event.active) markThreadBusy(event.threadId);
    return;
  }
  if (event.type === "queue.error") {
    setThreadQueueDepth(event.threadId, event.queueDepth || 0);
    if (event.threadId === state.threadId) addMessage("Queue", event.error || "Queued message failed.", "tool", new Date());
    return;
  }
  if (event.type === "gitlab.routing.updated") {
    state.gitlabIntegration = event.settings || state.gitlabIntegration;
    renderGitLabIntegration();
    if (developerPanelOpen()) refreshDeveloperInfo().catch(console.error);
    return;
  }
  if (event.type === "agent.channels.updated") {
    state.agentChannelPresence = event.settings || state.agentChannelPresence;
    renderAgentChannelPresence();
    if (developerPanelOpen()) refreshDeveloperInfo().catch(console.error);
    return;
  }
  if (event.type === "approval.request") {
    state.approvals.set(String(event.request.id), event.request);
    renderApprovals();
    updateWaitingFromState();
    return;
  }
  if (event.type === "approval.resolved") {
    state.approvals.delete(String(event.id));
    renderApprovals();
    updateWaitingFromState();
    return;
  }
  if (event.type === "codex.closed" || event.type === "codex.error") {
    state.activeTurnsByThread.clear();
    setWaiting(false);
    return;
  }
  if (event.type !== "codex.event") return;
  const message = event.message;
  const threadId = eventThreadId(message);
  const summary=uiEvents.handleCodexSummary(message,event);
  if(summary.name&&threadId===state.threadId)$("thread-title").textContent=summary.name;
  updateThreadActivityFromEvent(message);
  if (message.method === "thread/tokenUsage/updated") {
    const params = message.params || {};
    if (params.threadId && params.tokenUsage) {
      state.tokenUsageByThread[params.threadId] = normalizeTokenUsage(params.tokenUsage);
      saveTokenUsageCache();
      renderTokenUsage();
    }
  } else if (message.method === "account/rateLimits/updated") {
    state.accountRateLimits = normalizeRateLimits(message.params?.rateLimits);
    renderTokenUsage();
  }
  if (!isActiveThreadEvent(message)) {
    updateWaitingFromState();
    return;
  }
  if (message.method === "item/agentMessage/delta") {
    appendAgentDelta(message.params?.delta || "");
  } else if (message.method === "turn/started") {
    setWaiting(true, "Waiting for Codex");
  } else if (message.method === "item/started") {
    const item = message.params?.item;
    setWaiting(true, "Waiting for Codex");
    if (item?.type === "commandExecution") addCommandMessage("Command started", item.command || "", "", false, itemTimestamp(item));
  } else if (message.method === "item/completed") {
    const item = message.params?.item;
    if (item?.type === "agentMessage") {
      state.activeAgentMessage = null;
    } else if (item?.type === "commandExecution") {
      addCommandMessage("Command result", item.command || "", item.aggregatedOutput || "", false, itemTimestamp(item));
    } else if (item?.type === "fileChange") {
      addFileChangeMessage(item.changes || [], itemTimestamp(item));
    }
  } else if (message.method === "turn/completed") {
    if (threadId === state.threadId) state.activeAgentMessage = null;
    updateWaitingFromState();
  } else if (message.method === "turn/failed") {
    if (threadId === state.threadId) state.activeAgentMessage = null;
    updateWaitingFromState();
  } else if (message.method === "thread/status/changed") {
    updateWaitingFromState();
  }
}

function eventThreadId(message) {
  return message?.params?.threadId || message?.params?.turn?.threadId || null;
}

function eventTurnId(message) {
  return message?.params?.turnId || message?.params?.turn?.id || null;
}

function updateThreadActivityFromEvent(message) {
  const threadId = eventThreadId(message);
  if (!threadId) return;
  const method = message.method;
  const turnId = eventTurnId(message);
  if (method === "turn/started" || method === "item/started") {
    markThreadBusy(threadId, turnId);
  } else if (method === "turn/completed" || method === "turn/failed") {
    clearThreadBusy(threadId, turnId);
  } else if (method === "thread/status/changed") {
    const statusType = message.params?.status?.type;
    if (statusType === "active") {
      markThreadBusy(threadId);
    } else if (statusType === "idle" || statusType === "systemError" || statusType === "notLoaded") {
      clearThreadBusy(threadId);
    }
  }
}

function isActiveThreadEvent(message) {
  const threadId = eventThreadId(message);
  return !threadId || threadId === state.threadId;
}

function eventLogPayload(event) {
  if (event.type === "codex.event") {
    return {
      method: event.message?.method,
      itemType: event.message?.params?.item?.type,
      threadId: event.message?.params?.threadId,
    };
  }
  if (event.type === "approval.request") {
    return { method: event.request?.method, id: event.request?.id };
  }
  return event;
}

function logEvent(type, payload = {}) {
  state.eventLog.unshift({
    at: new Date().toISOString(),
    type,
    payload,
  });
  state.eventLog = state.eventLog.slice(0, 120);
  scheduleCommunicationLogRender();
}

function renderCommunicationLog() {
  const container = $("comm-log");
  if (!container) return;
  container.innerHTML = "";
  state.eventLog.forEach((entry) => {
    const row = document.createElement("div");
    row.className = "comm-row";
    const time = document.createElement("time");
    time.textContent = new Date(entry.at).toLocaleTimeString();
    const type = document.createElement("strong");
    type.textContent = entry.type;
    const payload = document.createElement("code");
    payload.textContent = JSON.stringify(entry.payload);
    row.append(time, type, payload);
    container.appendChild(row);
  });
}

function renderBotAuditLog(events = []) {
  const container = $("bot-audit-log");
  if (!container) return;
  container.innerHTML = "";
  events.slice().reverse().forEach((entry) => {
    const row = document.createElement("div");
    row.className = "comm-row";
    const time = document.createElement("time");
    time.textContent = new Date((entry.created_at || Date.now() / 1000) * 1000).toLocaleTimeString();
    const type = document.createElement("strong");
    type.textContent = entry.type || "event";
    const payload = document.createElement("code");
    payload.textContent = JSON.stringify(entry);
    row.append(time, type, payload);
    container.appendChild(row);
  });
  if (!events.length) {
    const row = document.createElement("div");
    row.className = "comm-row";
    const text = document.createElement("code");
    text.textContent = "No bot audit events recorded.";
    row.append(document.createElement("time"), document.createElement("strong"), text);
    container.appendChild(row);
  }
}

function fillRouteTestDefaults(diagnostics) {
  const provider = $("route-test-provider");
  const conversation = $("route-test-conversation");
  if (!provider || !conversation || conversation.value.trim()) return;
  const bindings = diagnostics?.bindings || [];
  const activeThreadBinding = bindings.find((binding) => binding.thread_id === state.threadId);
  const primaryBinding = bindings.find((binding) => binding.is_master || binding.is_primary_channel);
  const selected = activeThreadBinding || primaryBinding || bindings[0];
  if (!selected) return;
  provider.value = selected.provider || "slack";
  conversation.value = selected.external_conversation_id || "";
}

function formatLinesFromObject(values = {}) {
  return Object.entries(values)
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([key, value]) => `${key}=${Array.isArray(value) ? value.join(",") : value}`)
    .join("\n");
}

function parseList(value) {
  return String(value || "")
    .split(/[,\n]/)
    .map((item) => item.trim().toLowerCase())
    .filter(Boolean);
}

function parseKeyValueLines(value, { listValues = false } = {}) {
  const result = {};
  String(value || "")
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
    .forEach((line) => {
      const separator = line.includes("=") ? "=" : ":";
      const index = line.indexOf(separator);
      if (index < 0) return;
      const key = line.slice(0, index).trim().toLowerCase();
      const rawValue = line.slice(index + 1).trim();
      if (!key || !rawValue) return;
      result[key] = listValues
        ? rawValue.split(",").map((item) => item.trim().toLowerCase()).filter(Boolean)
        : rawValue;
    });
  return result;
}

function gitLabProjectsCopy(settings) {
  return JSON.parse(JSON.stringify(settings?.projects || {}));
}

function defaultGitLabProjectSettings(enabled = false) {
  return {
    enabled,
    channel_ids: [],
    route_agents: [],
    project_paths: [],
    fallback_agents_by_kind: {
      build: ["quinn"],
      merge_request: ["quinn"],
      pipeline: ["quinn"],
    },
  };
}

function activeGitLabProjectSettings(settings = state.gitlabIntegration) {
  const projectSettings = (settings?.projects || {})[state.projectId];
  return projectSettings ? { ...defaultGitLabProjectSettings(true), ...projectSettings } : defaultGitLabProjectSettings(false);
}

function agentChannelProjectsCopy(settings) {
  return JSON.parse(JSON.stringify(settings?.projects || {}));
}

function defaultAgentChannelPresenceProjectSettings() {
  return {
    agent_channels: {},
  };
}

function activeAgentChannelPresenceProjectSettings(settings = state.agentChannelPresence) {
  const projectSettings = (settings?.projects || {})[state.projectId];
  return projectSettings
    ? { ...defaultAgentChannelPresenceProjectSettings(), ...projectSettings }
    : defaultAgentChannelPresenceProjectSettings();
}

function normalizeGitLabSelectedChannels(value) {
  if (Array.isArray(value)) return value.filter(Boolean);
  if (value) return [value];
  return [];
}

function gitLabChannelChoices(selectedIds) {
  const selectedSet = new Set(normalizeGitLabSelectedChannels(selectedIds));
  const seen = new Set();
  const channels = [...state.botChannels]
    .filter((channel) => channel?.id && !seen.has(channel.id) && seen.add(channel.id))
    .sort((left, right) => (left.label || left.name || left.id).localeCompare(right.label || right.name || right.id));
  const options = [];
  channels.forEach((channel) => {
    options.push({
      label: channel.label || channel.name || channel.id,
      value: channel.id,
      title: channel.id,
      selected: selectedSet.has(channel.id),
    });
  });
  selectedSet.forEach((selectedId) => {
    if (selectedId && !seen.has(selectedId)) {
      options.push({
        label: selectedId,
        value: selectedId,
        title: selectedId,
        selected: true,
      });
    }
  });
  return options;
}

function gitLabSlackRouteChannelChoices(selectedIds) {
  const selectedSet = new Set(normalizeGitLabSelectedChannels(selectedIds));
  const seen = new Set();
  const channels = [...state.botChannels]
    .filter((channel) => channel?.provider === "slack" && channel?.id && !seen.has(channel.id) && seen.add(channel.id))
    .sort((left, right) => (left.label || left.name || left.id).localeCompare(right.label || right.name || right.id));
  const options = [];
  channels.forEach((channel) => {
    options.push({
      label: channel.label || channel.name || channel.id,
      value: channel.id,
      title: channel.id,
      selected: selectedSet.has(channel.id),
    });
  });
  selectedSet.forEach((selectedId) => {
    if (!selectedId || seen.has(selectedId)) return;
    options.push({
      label: selectedId,
      value: selectedId,
      title: selectedId,
      selected: true,
    });
  });
  return options;
}

function gitLabChannelOptions(selectedIds) {
  return gitLabChannelChoices(selectedIds).map((choice) => {
    const option = new Option(choice.label, choice.value);
    option.title = choice.title;
    option.selected = choice.selected;
    return option;
  });
}

function gitLabChoiceSummary(choices, selectedIds, emptyLabel) {
  const selectedSet = new Set(normalizeGitLabSelectedChannels(selectedIds));
  const selected = choices.filter((choice) => selectedSet.has(choice.value));
  if (!selected.length) return emptyLabel;
  if (selected.length === 1) return selected[0].label;
  if (selected.length === 2) return `${selected[0].label}, ${selected[1].label}`;
  return `${selected.length} selected`;
}

function createGitLabMultiSelectDropdown({
  dataAttribute,
  dataValue = "true",
  selectedValues = [],
  choices = [],
  emptyLabel,
  emptyMeta,
  defaultLabel,
  onSelectionChange,
}) {
  const picker = document.createElement("div");
  picker.className = "gitlab-agent-channel-picker";

  const details = document.createElement("details");
  details.className = "gitlab-channel-dropdown";

  const summary = document.createElement("summary");
  summary.innerHTML = `
    <span class="gitlab-channel-summary">
      <strong></strong>
      <small></small>
    </span>
  `;
  const summaryTitle = summary.querySelector("strong");
  const summaryMeta = summary.querySelector("small");

  const menu = document.createElement("div");
  menu.className = "gitlab-channel-menu";

  const select = document.createElement("select");
  select.multiple = true;
  select.hidden = true;
  select.tabIndex = -1;
  select.setAttribute("aria-hidden", "true");
  select.dataset[dataAttribute] = dataValue;

  const defaultLabelEl = document.createElement("label");
  defaultLabelEl.className = "gitlab-channel-option gitlab-channel-default";
  const defaultInput = document.createElement("input");
  defaultInput.type = "checkbox";
  defaultInput.dataset.gitlabChannelDefault = "true";
  const defaultText = document.createElement("span");
  defaultText.textContent = defaultLabel;
  defaultLabelEl.append(defaultInput, defaultText);
  menu.appendChild(defaultLabelEl);

  function selectedIds() {
    return [...select.selectedOptions].map((option) => option.value).filter(Boolean);
  }

  function syncSummary() {
    const current = selectedIds();
    summaryTitle.textContent = gitLabChoiceSummary(choices, current, emptyLabel);
    summaryMeta.textContent = current.length ? `${current.length} selected` : emptyMeta;
    defaultInput.checked = current.length === 0;
  }

  function setChoices(nextChoices, nextSelectedValues = selectedIds()) {
    choices = nextChoices;
    select.innerHTML = "";
    menu.querySelectorAll("[data-gitlab-channel-value]").forEach((element) => element.closest("label")?.remove());
    const selectedSet = new Set(normalizeGitLabSelectedChannels(nextSelectedValues));
    nextChoices.forEach((choice) => {
      const option = new Option(choice.label, choice.value);
      option.title = choice.title;
      option.selected = selectedSet.has(choice.value);
      select.appendChild(option);

      const optionLabel = document.createElement("label");
      optionLabel.className = "gitlab-channel-option";
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.checked = selectedSet.has(choice.value);
      checkbox.dataset.gitlabChannelValue = choice.value;
      checkbox.title = choice.title;
      checkbox.addEventListener("change", () => {
        [...select.options].forEach((item) => {
          if (item.value === choice.value) item.selected = checkbox.checked;
        });
        syncSummary();
        onSelectionChange?.(selectedIds(), { setChoices });
      });
      const text = document.createElement("span");
      text.textContent = choice.label;
      optionLabel.append(checkbox, text);
      menu.appendChild(optionLabel);
    });
    syncSummary();
  }

  defaultInput.addEventListener("change", () => {
    if (!defaultInput.checked) {
      defaultInput.checked = selectedIds().length === 0;
      return;
    }
    [...select.options].forEach((option) => {
      option.selected = false;
    });
    menu.querySelectorAll("[data-gitlab-channel-value]").forEach((input) => {
      input.checked = false;
    });
    syncSummary();
    onSelectionChange?.([], { setChoices });
  });

  setChoices(choices, selectedValues);
  details.append(summary, menu);
  picker.append(details, select);
  return { picker, select, setChoices, selectedIds };
}

function bindingAgentName(binding) {
  if (!binding || binding.is_master) return "";
  const raw = String(binding.route_prefix || binding.thread_name || "").trim().toLowerCase();
  if (!raw) return "";
  return raw.split(" - ", 1)[0].split(/\s+/, 1)[0].trim();
}

function agentChannelNames(projectSettings) {
  const agents = new Set(GITLAB_AGENT_NAMES);
  (state.botBindings || []).forEach((binding) => {
    if (binding.project_id && binding.project_id !== state.projectId) return;
    const agent = bindingAgentName(binding);
    if (agent) agents.add(agent);
  });
  Object.keys(projectSettings.agent_channels || {}).forEach((agent) => agents.add(agent));
  return [...agents].sort();
}

function gitLabRouteAgentChoices(selectedChannelIds = [], selectedAgents = []) {
  const presence = activeAgentChannelPresenceProjectSettings();
  const allowedChannels = new Set(normalizeGitLabSelectedChannels(selectedChannelIds));
  const agents = new Set();
  Object.entries(presence.agent_channels || {}).forEach(([agent, channels]) => {
    const normalizedChannels = normalizeGitLabSelectedChannels(channels);
    if (!allowedChannels.size || normalizedChannels.some((channel) => allowedChannels.has(channel))) {
      agents.add(agent);
    }
  });
  normalizeGitLabSelectedChannels(selectedAgents).forEach((agent) => agents.add(agent));
  return [...agents]
    .sort()
    .map((agent) => ({
      label: agent,
      value: agent,
      title: agent,
      selected: normalizeGitLabSelectedChannels(selectedAgents).includes(agent),
    }));
}

function renderAgentChannelPresence(projectSettings = activeAgentChannelPresenceProjectSettings()) {
  const container = $("agent-channel-presence");
  if (!container) return;
  container.innerHTML = "";
  agentChannelNames(projectSettings).forEach((agent) => {
    const row = document.createElement("div");
    row.className = "gitlab-agent-channel-row";
    const name = document.createElement("span");
    name.textContent = agent;
    const selectedIds = (projectSettings.agent_channels || {})[agent] || [];
    const dropdown = createGitLabMultiSelectDropdown({
      dataAttribute: "gitlabAgentChannel",
      dataValue: agent,
      selectedValues: selectedIds,
      choices: gitLabChannelChoices(selectedIds),
      emptyLabel: "Binding default",
      emptyMeta: "Default route",
      defaultLabel: "Binding default",
    });
    row.append(name, dropdown.picker);
    container.appendChild(row);
  });
}

function collectAgentChannelPresence() {
  const channels = {};
  document.querySelectorAll("[data-gitlab-agent-channel]").forEach((select) => {
    const selected = [...select.selectedOptions]
      .map((option) => option.value)
      .filter(Boolean);
    if (selected.length) channels[select.dataset.gitlabAgentChannel] = selected;
  });
  return channels;
}

function renderGitLabRoutingRoutes(projectSettings = activeGitLabProjectSettings()) {
  const container = $("gitlab-routing-routes");
  if (!container) return;
  container.innerHTML = "";
  const row = document.createElement("div");
  row.className = "gitlab-routing-route-row";

  const sender = document.createElement("span");
  sender.className = "gitlab-routing-sender";
  sender.textContent = "GitLab";

  const channelDropdown = createGitLabMultiSelectDropdown({
    dataAttribute: "gitlabRouteChannel",
    selectedValues: projectSettings.channel_ids || [],
    choices: gitLabSlackRouteChannelChoices(projectSettings.channel_ids || []),
    emptyLabel: "Automatic routing",
    emptyMeta: "Any matching channel",
    defaultLabel: "Automatic routing",
    onSelectionChange: (selectedChannels) => {
      const selectedAgents = agentDropdown.selectedIds();
      agentDropdown.setChoices(
        gitLabRouteAgentChoices(selectedChannels, selectedAgents),
        selectedAgents
      );
    },
  });

  const routeAgents = normalizeGitLabSelectedChannels(projectSettings.route_agents || []);
  const agentDropdown = createGitLabMultiSelectDropdown({
    dataAttribute: "gitlabRouteAgent",
    selectedValues: routeAgents,
    choices: gitLabRouteAgentChoices(channelDropdown.selectedIds(), routeAgents),
    emptyLabel: "Automatic agents",
    emptyMeta: "Use GitLab ownership",
    defaultLabel: "Automatic agents",
  });

  channelDropdown.setChoices(
    gitLabSlackRouteChannelChoices(projectSettings.channel_ids || []),
    projectSettings.channel_ids || []
  );
  agentDropdown.setChoices(
    gitLabRouteAgentChoices(channelDropdown.selectedIds(), routeAgents),
    routeAgents
  );

  row.append(sender, channelDropdown.picker, agentDropdown.picker);
  container.appendChild(row);
}

function collectGitLabRoutingSelection() {
  return {
    channel_ids: [...document.querySelectorAll("[data-gitlab-route-channel]")]
      .flatMap((select) => [...select.selectedOptions].map((option) => option.value).filter(Boolean)),
    route_agents: [...document.querySelectorAll("[data-gitlab-route-agent]")]
      .flatMap((select) => [...select.selectedOptions].map((option) => option.value).filter(Boolean)),
  };
}

function renderGitLabIntegration() {
  const settings = state.gitlabIntegration;
  if (!settings || !$("gitlab-enabled")) return;
  const projectSettings = activeGitLabProjectSettings(settings);
  $("gitlab-enabled").checked = Boolean(projectSettings.enabled);
  $("gitlab-webhook-path").value = settings.webhookPath || "/bots/gitlab/events";
  $("gitlab-token-status").value = settings.tokenVerification ? "Enabled" : "Not configured";
  $("gitlab-ignored-kinds").value = (settings.ignored_event_kinds || []).join(", ");
  $("gitlab-project-paths").value = (projectSettings.project_paths || []).join("\n");
  renderGitLabRoutingRoutes(projectSettings);
}

async function refreshGitLabIntegration() {
  state.gitlabIntegration = await api("/api/integrations/gitlab");
  renderGitLabIntegration();
}

async function refreshAgentChannelPresence() {
  state.agentChannelPresence = await api("/api/integrations/agent-presence");
  renderAgentChannelPresence();
}

async function saveGitLabIntegration() {
  const result = $("gitlab-routing-result");
  if (result) {
    result.hidden = false;
    result.textContent = "Saving...";
  }
  const projects = gitLabProjectsCopy(state.gitlabIntegration);
  const routingSelection = collectGitLabRoutingSelection();
  projects[state.projectId] = {
    enabled: $("gitlab-enabled").checked,
    channel_ids: routingSelection.channel_ids,
    route_agents: routingSelection.route_agents,
    project_paths: parseList($("gitlab-project-paths").value),
    fallback_agents_by_kind: activeGitLabProjectSettings(state.gitlabIntegration).fallback_agents_by_kind || {},
  };
  const payload = {
    enabled: state.gitlabIntegration?.enabled ?? true,
    ignored_event_kinds: parseList($("gitlab-ignored-kinds").value),
    projects,
  };
  try {
    const response = await api("/api/integrations/gitlab", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    state.gitlabIntegration = response;
    renderGitLabIntegration();
    if (result) result.textContent = "Saved";
    await refreshDeveloperInfo();
  } catch (error) {
    if (result) result.textContent = error.message;
  }
}

async function saveAgentChannelPresence() {
  const result = $("agent-channel-presence-result");
  if (result) {
    result.hidden = false;
    result.textContent = "Saving...";
  }
  const projects = agentChannelProjectsCopy(state.agentChannelPresence);
  projects[state.projectId] = {
    agent_channels: collectAgentChannelPresence(),
  };
  try {
    const response = await api("/api/integrations/agent-presence", {
      method: "POST",
      body: JSON.stringify({ projects }),
    });
    state.agentChannelPresence = response;
    renderAgentChannelPresence();
    if (result) result.textContent = "Saved";
    await refreshDeveloperInfo();
  } catch (error) {
    if (result) result.textContent = error.message;
  }
}

async function refreshDeveloperInfo() {
  const panel = $("developer-panel");
  if (panel && !panel.open) return;
  const daemonInfo = $("daemon-info");
  const summary = $("developer-summary");
  const lastRefresh = $("developer-last-refresh");
  if (daemonInfo) daemonInfo.textContent = "Loading...";
  try {
    const [diagnostics] = await Promise.all([
      api(`/api/diagnostics?project_id=${encodeURIComponent(state.projectId)}`),
      refreshGitLabIntegration(),
      refreshAgentChannelPresence(),
    ]);
    state.diagnostics = diagnostics;
    const info = {
      daemon: diagnostics.status,
      health: diagnostics.health,
      activeTurns: diagnostics.activeTurns,
      queues: diagnostics.queues,
      queueTasks: diagnostics.queueTasks,
      connections: diagnostics.connections,
      botBindings: diagnostics.bindings,
      replyTargets: diagnostics.replyTargets,
      deliveryTargets: diagnostics.deliveryTargets,
      client: {
        projectId: state.projectId,
        threadId: state.threadId,
        waiting: state.waiting,
        theme: currentTheme(),
        tokenUsage: state.threadId ? state.tokenUsageByThread[state.threadId] : null,
        accountRateLimits: state.accountRateLimits,
        agentChannelPresence: state.agentChannelPresence,
      },
    };
    if (daemonInfo) daemonInfo.textContent = JSON.stringify(info, null, 2);
    renderBotAuditLog(diagnostics.recentBotEvents || []);
    fillRouteTestDefaults(diagnostics);
    if (summary) {
      summary.textContent = diagnostics.status.ok
        ? `Daemon pid ${diagnostics.status.pid || "unknown"} · ${diagnostics.bindings.length} bot bindings`
        : `Daemon not ready${diagnostics.status.error ? ` · ${diagnostics.status.error}` : ""}`;
    }
    if (lastRefresh) lastRefresh.textContent = `Updated ${new Date().toLocaleTimeString()}`;
  } catch (error) {
    if (daemonInfo) daemonInfo.textContent = error.message;
    if (summary) summary.textContent = "Unable to load daemon status";
  }
}

async function runRouteTest() {
  const result = $("route-test-result");
  if (!result) return;
  result.textContent = "Testing...";
  try {
    const response = await api("/api/diagnostics/route-test", {
      method: "POST",
      body: JSON.stringify({
        provider: $("route-test-provider").value,
        external_conversation_id: $("route-test-conversation").value.trim(),
        text: $("route-test-text").value.trim() || "Test message",
        project_id: state.projectId,
      }),
    });
    result.textContent = JSON.stringify(response, null, 2);
  } catch (error) {
    result.textContent = error.message;
  }
}

async function recoverDaemon() {
  const daemonInfo = $("daemon-info");
  if (daemonInfo) daemonInfo.textContent = "Scheduling recovery...";
  try {
    const response = await api("/api/recovery/resume", { method: "POST" });
    logEvent("recovery.resume", response);
    await refreshDeveloperInfo();
  } catch (error) {
    if (daemonInfo) daemonInfo.textContent = error.message;
  }
}

async function refreshTokenUsage() {
  try {
    const limits = await api("/api/account/rate-limits");
    state.accountRateLimits = normalizeRateLimits(limits);
    renderTokenUsage();
  } catch (error) {
    logEvent("token.error", { message: error.message });
    renderTokenUsage();
  }
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

const selectProject=createProjectNavigator(state,{refresh,applyRunSettings,onError:(error)=>addMessage("Error",error.message,"tool",new Date())});

$("refresh").addEventListener("click", () => refresh({ reloadProjects: true }));
$("new-thread").addEventListener("click", newThread);
$("send").addEventListener("click", sendPrompt);
$("theme-toggle").addEventListener("click", () => {
  applyTheme(currentTheme() === "dark" ? "light" : "dark");
});
$("developer-panel").addEventListener("toggle", () => {
  if (!developerPanelOpen()) return;
  renderCommunicationLog();
  refreshDeveloperInfo().catch(console.error);
});
$("refresh-developer").addEventListener("click", refreshDeveloperInfo);
$("recover-daemon").addEventListener("click", recoverDaemon);
$("run-route-test").addEventListener("click", runRouteTest);
$("save-gitlab-routing").addEventListener("click", saveGitLabIntegration);
$("save-agent-channel-presence").addEventListener("click", saveAgentChannelPresence);
$("refresh-token-usage").addEventListener("click", (event) => {
  event.stopPropagation();
  refreshTokenUsage();
});
$("bot-provider").addEventListener("change", () => {
  $("bot-connection").value = "";
  applyBotProvider($("bot-provider").value);
  renderBotConnectionOptions();
  fillBotDialogFromConnection(null);
});
$("bot-connection").addEventListener("change", () => fillBotDialogFromConnection(selectedBotConnection()));
$("close-bot-dialog").addEventListener("click", () => $("bot-dialog").close());
$("cancel-bot-integration").addEventListener("click", () => $("bot-dialog").close());
$("save-bot-integration").addEventListener("click", (event) => saveBotIntegration(event).catch((error) => {
  $("bot-result").hidden = false;
  $("bot-result").textContent = error.message;
}));
$("execution-profile").addEventListener("change",()=>{persistRunSettings();ep.render(currentRunSettings().profileId,escapeHtml);renderRepositoryTargets();});
$("repository-target").addEventListener("change", () => {
  persistRunSettings();
  renderRepositoryTargets();
});
$("repository-read-context").addEventListener("change", persistRunSettings);
$("sandbox").addEventListener("change", () => {
  persistRunSettings();
  syncThreadRunSettings().catch(console.error);
});
$("approval-policy").addEventListener("change", () => {
  persistRunSettings();
  syncThreadRunSettings().catch(console.error);
});
$("rename-thread").addEventListener("click", renameThread);
$("prompt").addEventListener("keydown", (event) => {
  if (event.key !== "Enter") return;
  if (event.altKey) return;
  event.preventDefault();
  sendPrompt();
});
$("prompt").addEventListener("input", resizePromptInput);
$("thread-search").addEventListener("input", () => {
  if (state.searchTimer) clearTimeout(state.searchTimer);
  state.searchTimer = setTimeout(() => {
    state.searchTimer = null;
    scheduleRefresh(0);
  }, 180);
});
$("archive-thread").addEventListener("click", async () => {
  if (!state.threadId) return;
  await api(`/api/threads/${state.threadId}/archive`, { method: "POST" });
  state.threadId = null;
  clearMessages();
  await refresh();
});
$("new-project").addEventListener("click", () => $("project-dialog").showModal());
$("save-project").addEventListener("click", async (event) => {
  event.preventDefault();
  const payload = {
    name: $("project-name").value,
    path: $("project-path").value,
    model: $("project-model").value || null,
  };
  const project = await api("/api/projects", { method: "POST", body: JSON.stringify(payload) });
  state.projects = [
    ...state.projects.filter((item) => item.id !== project.id),
    project,
  ];
  delete state.projectUiStatic[project.id];
  activateProject(state,project.id);
  window.dispatchEvent(new CustomEvent("codex:project-created",{detail:{projectId:project.id,freshBootstrap:project.freshBootstrap||null}}));
  $("project-dialog").close();
  await refresh();
});

activateProject(state,state.projectId);
startLongTaskObserver();
applyTheme(currentTheme());
setupSidebarControls();
applySidebarPreference();
state.tokenUsageByThread = loadTokenUsageCache();
renderTokenUsage();
connectProjectUiEventStream({base:BASE,reconciler:uiEvents,onEvent:handleEvent,logEvent,onDisconnect:()=>{state.activeTurnsByThread.clear();setWaiting(false);}});
refreshTokenUsage();
refresh().catch((error) => {
  addMessage("Error", error.message, "tool", new Date());
});
