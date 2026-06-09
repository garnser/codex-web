const state = {
  projects: [],
  projectId: "home",
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
  botConnections: [],
  botBindings: [],
  botChannels: [],
  botIntegrationTarget: null,
  expandedItems: new Set(),
};

const $ = (id) => document.getElementById(id);
const BASE = window.location.pathname.startsWith("/codex") ? "/codex" : "";
const THEME_KEY = "codex-web-theme";
const SETTINGS_KEY = "codex-web-project-settings";
const TOKEN_USAGE_KEY = "codex-web-token-usage";
const SIDEBAR_KEY = "codex-web-sidebar";

function preferredTheme() {
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function currentTheme() {
  return document.documentElement.dataset.theme || localStorage.getItem(THEME_KEY) || preferredTheme();
}

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

async function api(path, options = {}) {
  logEvent("api.request", { path, method: options.method || "GET" });
  const response = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) {
    const text = await response.text();
    logEvent("api.error", { path, status: response.status, text });
    throw new Error(text || response.statusText);
  }
  const result = await response.json();
  logEvent("api.response", { path, status: response.status });
  return result;
}

function activeProject() {
  return state.projects.find((project) => project.id === state.projectId) || state.projects[0];
}

function loadProjectSettings() {
  try {
    return JSON.parse(localStorage.getItem(SETTINGS_KEY) || "{}");
  } catch {
    return {};
  }
}

function savedProjectSettings(projectId = state.projectId) {
  return loadProjectSettings()[projectId] || {};
}

function currentRunSettings() {
  const project = activeProject();
  const saved = savedProjectSettings(project?.id || state.projectId);
  return {
    sandbox: saved.sandbox || project?.sandbox || "workspace-write",
    approvalPolicy: saved.approvalPolicy || project?.approval_policy || "on-request",
  };
}

function applyRunSettings() {
  const settings = currentRunSettings();
  $("sandbox").value = settings.sandbox;
  $("approval-policy").value = settings.approvalPolicy;
}

function persistRunSettings() {
  const project = activeProject();
  if (!project) return;
  const allSettings = loadProjectSettings();
  allSettings[project.id] = {
    sandbox: $("sandbox").value,
    approvalPolicy: $("approval-policy").value,
  };
  localStorage.setItem(SETTINGS_KEY, JSON.stringify(allSettings));
}

async function syncThreadRunSettings() {
  if (!state.threadId) return;
  const settings = currentRunSettings();
  await api(`/api/threads/${state.threadId}/settings`, {
    method: "POST",
    body: JSON.stringify({
      sandbox: settings.sandbox,
      approval_policy: settings.approvalPolicy,
    }),
  });
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
  state.projects.forEach((project) => {
    const item = document.createElement("div");
    const expanded = isItemExpanded("project", project.id);
    item.className = `item ${project.id === state.projectId ? "active" : ""} ${expanded ? "expanded" : ""}`;
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
    item.querySelector(".item-main").addEventListener("click", async () => {
      state.projectId = project.id;
      applyRunSettings();
      await refresh();
    });
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
}

function renderThreads() {
  $("threads").innerHTML = "";
  const threads = state.threads?.data || state.threads?.threads || state.threads || [];
  threads.forEach((thread) => {
    const item = document.createElement("div");
    const title = thread.name || thread.preview || "Untitled thread";
    const updated = thread.updatedAt ? new Date(thread.updatedAt * 1000).toLocaleString() : "";
    const expanded = isItemExpanded("thread", thread.id);
    const isPrimary = state.botBindings.some((binding) => (
      binding.project_id === state.projectId
      && binding.thread_id === thread.id
      && binding.is_master
    ));
    const primaryChannel = state.botBindings.find((binding) => (
      binding.project_id === state.projectId
      && binding.thread_id === thread.id
      && binding.is_primary_channel
    ));
    const channelOptions = state.botChannels
      .map((channel) => `<option value="${escapeHtml(`${channel.provider}:${channel.id}`)}" ${primaryChannel?.provider === channel.provider && primaryChannel?.external_conversation_id === channel.id ? "selected" : ""}>${escapeHtml(channel.label || channel.id)}</option>`)
      .join("");
    item.className = `item ${thread.id === state.threadId ? "active" : ""} ${expanded ? "expanded" : ""}`;
    item.innerHTML = `
      <div class="item-header">
        <div class="item-main">
          <strong>${escapeHtml(title)}</strong>
          <span>${escapeHtml(updated || "No activity yet")}</span>
        </div>
        <button type="button" class="item-expand-button" data-action="expand" aria-expanded="${expanded}" title="${expanded ? "Hide actions" : "Show actions"}">Actions</button>
      </div>
      <div class="item-actions" aria-label="Thread actions" ${expanded ? "" : "hidden"}>
        <button type="button" class="item-action-button" data-action="bot">Bot Integration</button>
        <label class="item-action-check">
          <input type="checkbox" data-action="primary" ${isPrimary ? "checked" : ""} />
          Primary catch-all
        </label>
        <label class="item-action-select">
          <span>Primary channel</span>
          <select data-action="primary-channel">
            <option value="">Default</option>
            ${channelOptions}
          </select>
        </label>
      </div>
    `;
    item.querySelector(".item-main").addEventListener("click", () => loadThread(thread.id));
    item.querySelector('[data-action="expand"]').addEventListener("click", (event) => {
      event.stopPropagation();
      toggleItemExpanded("thread", thread.id);
      renderThreads();
    });
    item.querySelector('[data-action="bot"]').addEventListener("click", (event) => {
      event.stopPropagation();
      openBotIntegration({
        scope: "thread",
        projectId: state.projectId,
        threadId: thread.id,
        title,
      });
    });
    item.querySelector('[data-action="primary"]').addEventListener("change", async (event) => {
      event.stopPropagation();
      const response = await api(`/api/threads/${thread.id}/primary`, {
        method: "POST",
        body: JSON.stringify({
          primary: event.target.checked,
          project_id: state.projectId,
        }),
      });
      state.botBindings = response.bindings || state.botBindings;
      await refresh();
    });
    item.querySelector('[data-action="primary-channel"]').addEventListener("change", async (event) => {
      event.stopPropagation();
      const [provider, ...channelParts] = event.target.value.split(":");
      const channelId = channelParts.join(":") || null;
      const response = await api(`/api/threads/${thread.id}/primary-channel`, {
        method: "POST",
        body: JSON.stringify({
          project_id: state.projectId,
          provider: provider || "slack",
          external_conversation_id: channelId,
        }),
      });
      state.botBindings = response.bindings || state.botBindings;
      await refresh();
    });
    $("threads").appendChild(item);
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
  updateWaitingFromState();
}

function clearThreadBusy(threadId, turnId = null) {
  if (!threadId || !state.activeTurnsByThread.has(threadId)) return;
  if (turnId) {
    state.activeTurnsByThread.get(threadId).delete(turnId);
  } else {
    state.activeTurnsByThread.delete(threadId);
    updateWaitingFromState();
    return;
  }
  if (state.activeTurnsByThread.get(threadId).size === 0) {
    state.activeTurnsByThread.delete(threadId);
  }
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

function coerceMessageDate(value) {
  if (!value) return null;
  if (typeof value === "number") {
    return new Date(value > 1_000_000_000_000 ? value : value * 1000);
  }
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

function messageTimestamp(...candidates) {
  for (const candidate of candidates) {
    const date = coerceMessageDate(candidate);
    if (date) return date;
  }
  return new Date();
}

function formatMessageTimestamp(value) {
  const date = coerceMessageDate(value) || new Date();
  return date.toLocaleString();
}

function itemTimestamp(item = {}, turn = {}) {
  return messageTimestamp(
    item.createdAt,
    item.created_at,
    item.timestamp,
    item.completedAt,
    item.completed_at,
    item.updatedAt,
    item.updated_at,
    turn.createdAt,
    turn.created_at,
    turn.startedAt,
    turn.started_at,
    turn.updatedAt,
    turn.updated_at,
  );
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
  $("messages").scrollTop = $("messages").scrollHeight;
  return message;
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
  $("messages").scrollTop = $("messages").scrollHeight;
  return message;
}

function addCommandMessage(label, command, output = "", open = false, timestamp = new Date()) {
  const message = document.createElement("article");
  message.className = "message command-message";
  const hasOutput = Boolean(output && output.trim());
  const summaryText = command || "Command";
  message.innerHTML = `
    <div class="message-header">
      <div class="role">${escapeHtml(label)}</div>
      <time datetime="${timestamp.toISOString()}">${escapeHtml(formatMessageTimestamp(timestamp))}</time>
    </div>
    <details class="command-details"${open ? " open" : ""}>
      <summary>
        <span></span>
        <small>${hasOutput ? "Expand output" : "No output"}</small>
      </summary>
      <pre></pre>
    </details>
  `;
  const details = message.querySelector("details");
  const summary = message.querySelector("summary");
  const pre = message.querySelector("pre");
  summary.querySelector("span").textContent = summaryText;
  summary.title = summaryText;
  pre.textContent = hasOutput ? `${command || ""}\n\n${output}`.trim() : command || "No command content was provided.";
  details.addEventListener("toggle", () => {
    if (!hasOutput) return;
    summary.querySelector("small").textContent = details.open ? "Collapse output" : "Expand output";
  });
  $("messages").appendChild(message);
  $("messages").scrollTop = $("messages").scrollHeight;
  return message;
}

function appendAgentDelta(text) {
  if (!state.activeAgentMessage) {
    state.activeAgentMessage = addMessage("Codex", "", "agent", new Date());
  }
  const body = state.activeAgentMessage.querySelector(".body");
  body.textContent += text;
  $("messages").scrollTop = $("messages").scrollHeight;
}

function displayUserMessageText(text) {
  const match = String(text || "").match(/^Message received from .+? conversation .+? by .+?\.\n\n([\s\S]*)$/);
  return match ? match[1] : text;
}

function renderThread(thread) {
  clearMessages();
  const title = thread.name || thread.preview || "Untitled thread";
  $("thread-title").textContent = title;
  $("thread-meta").textContent = `${thread.id} · ${thread.cwd || ""}`;
  const turns = thread.turns || [];
  turns.forEach((turn) => {
    (turn.items || []).forEach((item) => renderItem(item, turn));
  });
}

function renderNewThreadShell(thread) {
  clearMessages();
  $("thread-title").textContent = thread.name || "Untitled thread";
  $("thread-meta").textContent = `${thread.id} · ${thread.cwd || activeProject()?.path || ""}`;
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

async function refresh() {
  state.projects = await api("/api/projects");
  state.botBindings = await api("/api/bots/bindings");
  applyRunSettings();
  state.botChannels = await api(`/api/bots/channels?project_id=${encodeURIComponent(state.projectId)}`);
  const search = $("thread-search").value.trim();
  const qs = new URLSearchParams({ project_id: state.projectId, archived: "false" });
  if (search) qs.set("search", search);
  state.threads = await api(`/api/threads?${qs}`);
  const threads = state.threads?.data || state.threads?.threads || state.threads || [];
  hydrateThreadListActivity(threads);
  renderProjects();
  renderThreads();
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
  const settings = currentRunSettings();
  await api(`/api/threads/${threadId}/resume?project_id=${encodeURIComponent(state.projectId)}&sandbox=${encodeURIComponent(settings.sandbox)}&approval_policy=${encodeURIComponent(settings.approvalPolicy)}`, { method: "POST" });
  const data = await api(`/api/threads/${threadId}`);
  const thread = data.thread || data;
  hydrateThreadActivity(thread);
  await refreshQueueStatus(threadId);
  renderThread(thread);
  renderThreads();
  renderTokenUsage();
  updateWaitingFromState();
}

async function newThread() {
  persistRunSettings();
  const settings = currentRunSettings();
  const data = await api(`/api/threads?project_id=${encodeURIComponent(state.projectId)}&sandbox=${encodeURIComponent(settings.sandbox)}&approval_policy=${encodeURIComponent(settings.approvalPolicy)}`, { method: "POST" });
  const thread = data.thread || data;
  state.threadId = thread.id;
  setWaiting(false);
  renderNewThreadShell(thread);
  renderTokenUsage();
  await refresh();
}

async function sendPrompt() {
  const prompt = $("prompt").value.trim();
  if (!prompt) return;
  persistRunSettings();
  if (!state.threadId) await newThread();
  const threadId = state.threadId;
  const willQueue = isThreadBusy(threadId) || queuedDepth(threadId) > 0;
  $("prompt").value = "";
  state.activeAgentMessage = null;
  const message = addMessage(willQueue ? "You (queued)" : "You", prompt, "user", new Date());
  if (willQueue) {
    setThreadQueueDepth(threadId, queuedDepth(threadId) + 1);
    attachQueuedSteer(message, threadId);
  } else {
    markThreadBusy(threadId);
  }
  try {
    const response = await api(`/api/threads/${threadId}/turns`, {
      method: "POST",
      body: JSON.stringify({
        message: prompt,
        project_id: state.projectId,
        sandbox: currentRunSettings().sandbox,
        approval_policy: currentRunSettings().approvalPolicy,
      }),
    });
    if (response.queued) {
      attachQueuedSteer(message, threadId, response.queuedId);
      setQueuedMessageId(message, response.queuedId);
      setThreadQueueDepth(threadId, response.queueDepth || queuedDepth(threadId) || 1);
    }
  } catch (error) {
    if (willQueue) {
      setThreadQueueDepth(threadId, Math.max(0, queuedDepth(threadId) - 1));
    } else {
      clearThreadBusy(threadId);
    }
    addMessage("Error", error.message, "tool", new Date());
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
  await api(`/api/threads/${state.threadId}/name`, {
    method: "POST",
    body: JSON.stringify({ name: name.trim() }),
  });
  $("thread-title").textContent = name.trim();
  await refresh();
}

function connectEvents() {
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${scheme}://${location.host}${BASE}/ws`);
  logEvent("ws.opening", { url: `${BASE}/ws` });
  ws.onopen = () => logEvent("ws.open", {});
  ws.onmessage = (event) => {
    const payload = JSON.parse(event.data);
    logEvent("ws.message", eventLogPayload(payload));
    handleEvent(payload);
  };
  ws.onerror = () => logEvent("ws.error", {});
  ws.onclose = () => {
    logEvent("ws.close", {});
    state.activeTurnsByThread.clear();
    setWaiting(false);
    setTimeout(connectEvents, 1000);
  };
}

function handleEvent(event) {
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
    refresh().catch(console.error);
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
    if (message.method === "thread/name/updated" || message.method === "thread/status/changed") {
      refresh().catch(console.error);
    }
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
    refresh().catch(console.error);
  } else if (message.method === "turn/failed") {
    if (threadId === state.threadId) state.activeAgentMessage = null;
    updateWaitingFromState();
  } else if (message.method === "thread/name/updated") {
    refresh().catch(console.error);
  } else if (message.method === "thread/status/changed") {
    refresh().catch(console.error);
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
  renderCommunicationLog();
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

async function refreshDeveloperInfo() {
  const panel = $("developer-panel");
  if (panel && !panel.open) return;
  const daemonInfo = $("daemon-info");
  const summary = $("developer-summary");
  const lastRefresh = $("developer-last-refresh");
  if (daemonInfo) daemonInfo.textContent = "Loading...";
  try {
    const [status, bots, bindings] = await Promise.all([
      api("/api/status"),
      api("/api/bots"),
      api("/api/bots/bindings"),
    ]);
    const info = {
      daemon: status,
      bots,
      botBindings: bindings,
      client: {
        projectId: state.projectId,
        threadId: state.threadId,
        waiting: state.waiting,
        theme: currentTheme(),
        tokenUsage: state.threadId ? state.tokenUsageByThread[state.threadId] : null,
        accountRateLimits: state.accountRateLimits,
      },
    };
    if (daemonInfo) daemonInfo.textContent = JSON.stringify(info, null, 2);
    if (summary) {
      summary.textContent = status.ok
        ? `Daemon pid ${status.pid || "unknown"} · ${bindings.length} bot bindings`
        : `Daemon not ready${status.error ? ` · ${status.error}` : ""}`;
    }
    if (lastRefresh) lastRefresh.textContent = `Updated ${new Date().toLocaleTimeString()}`;
  } catch (error) {
    if (daemonInfo) daemonInfo.textContent = error.message;
    if (summary) summary.textContent = "Unable to load daemon status";
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

$("refresh").addEventListener("click", refresh);
$("new-thread").addEventListener("click", newThread);
$("send").addEventListener("click", sendPrompt);
$("theme-toggle").addEventListener("click", () => {
  applyTheme(currentTheme() === "dark" ? "light" : "dark");
});
$("developer-panel").addEventListener("toggle", () => refreshDeveloperInfo().catch(console.error));
$("refresh-developer").addEventListener("click", refreshDeveloperInfo);
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
$("thread-search").addEventListener("input", () => refresh().catch(console.error));
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
  state.projectId = project.id;
  $("project-dialog").close();
  await refresh();
});

applyTheme(currentTheme());
setupSidebarControls();
applySidebarPreference();
state.tokenUsageByThread = loadTokenUsageCache();
renderTokenUsage();
connectEvents();
refreshTokenUsage();
refresh().catch((error) => {
  addMessage("Error", error.message, "tool", new Date());
});
