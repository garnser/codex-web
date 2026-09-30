import { captureProjectView } from "./project_view_scope.js";
import { observeEventVisible } from "./frontend_perf.js";
export function connectProjectUiEventStream({
  base = "",
  reconciler,
  onEvent,
  logEvent,
  onDisconnect,
  reconnectDelay = 1000,
}) {
  const connect = () => {
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(
      `${scheme}://${location.host}${base}/ws`,
    );
    logEvent("ws.opening", { url: `${base}/ws` });
    ws.onopen = () => logEvent("ws.open", {});
    ws.onmessage = (message) => {
      const payload = JSON.parse(message.data);
      logEvent("ws.message", {
        type: payload?.type,
        method: payload?.message?.method,
        threadId: (
          payload?.threadId
          || payload?.message?.params?.threadId
          || payload?.message?.params?.turn?.threadId
        ),
      });
      if (reconciler.accept(payload)) onEvent(payload);
    };
    ws.onerror = () => logEvent("ws.error", {});
    ws.onclose = () => {
      logEvent("ws.close", {});
      reconciler.noteDisconnect();
      onDisconnect();
      setTimeout(connect, reconnectDelay);
    };
    return ws;
  };
  return connect();
}

export function createProjectUiEventReconciler({
  state,
  api,
  renderThreads,
  reconcileWorkspace,
  logEvent,
  getSearch = () => "",
}) {
  let streamId = null;
  let lastSequence = 0;
  let connectedOnce = false;
  let disconnected = false;
  const timers = new Map();
  const inflight = new Map();
  const rerun = new Map();

  function rows() {
    const value = state.threads?.data
      || state.threads?.threads
      || state.threads
      || [];
    return Array.isArray(value) ? value : [];
  }

  function runKeyed(key, task, delay = 80) {
    if (timers.has(key)) clearTimeout(timers.get(key));
    timers.set(key, setTimeout(async () => {
      timers.delete(key);
      if (inflight.has(key)) {
        rerun.set(key, task);
        return;
      }
      const promise = Promise.resolve()
        .then(task)
        .catch((error) => {
          logEvent("ui.reconcile.error", {
            kind: key.split(":")[0],
            message: error?.message || String(error),
          });
        })
        .finally(() => {
          inflight.delete(key);
          const next = rerun.get(key);
          rerun.delete(key);
          if (next) runKeyed(key, next, 0);
        });
      inflight.set(key, promise);
      await promise;
    }, delay));
  }

  function scheduleWorkspace(reason) {
    runKeyed(
      "workspace",
      async () => {
        logEvent("ui.reconcile.workspace", { reason });
        await reconcileWorkspace();
      },
      100,
    );
  }

  function captureScope() {
    const view = captureProjectView();
    const projectId = state.projectId;
    const refresh = state.refreshGeneration;
    const search = String(getSearch() || "").trim();
    return {
      projectId, search,
      key: JSON.stringify([view.generation, projectId, refresh, search]),
      current: () => view.current() && state.projectId === projectId
        && state.refreshGeneration === refresh && String(getSearch() || "").trim() === search,
    };
  }

  async function revalidateThreadPage(scope) {
    if (!scope.current()) return;
    const qs = new URLSearchParams({
      project_id: scope.projectId,
      archived: "false",
      limit: "50",
    });
    const search = scope.search;
    if (search) qs.set("search", search);
    const response = await api(`/api/threads?${qs}`);
    if (!scope.current()) return;
    state.threads = response;
    renderThreads();
  }

  function scheduleThreadPage(reason) {
    const scope = captureScope();
    runKeyed(
      `threads:${scope.key}`,
      async () => {
        logEvent("ui.reconcile.threads", { reason });
        await revalidateThreadPage(scope);
      },
      80,
    );
  }

  async function revalidateBindings(threadId, scope) {
    if (!scope.current()) return;
    const projectAtStart = scope.projectId;
    const response = await api(
      `/api/projects/${encodeURIComponent(projectAtStart)}/ui-state/bindings?thread_id=${encodeURIComponent(threadId)}`,
    );
    if (!scope.current()) return;
    const replacements = response.bindings?.items || [];
    state.botBindings = [
      ...(state.botBindings || []).filter((binding) => !(
        binding.project_id === projectAtStart
        && (binding.thread_id === threadId || binding.is_master)
      )),
      ...replacements,
    ];

    const channels = new Map(
      (state.botChannels || []).map((channel) => [
        `${channel.provider}:${channel.id}`,
        channel,
      ]),
    );
    (response.channels?.items || []).forEach((channel) => {
      channels.set(`${channel.provider}:${channel.id}`, channel);
    });
    state.botChannels = [...channels.values()];
    renderThreads();
  }

  function scheduleBindings(threadId, reason = "binding.updated") {
    if (!threadId) return;
    const scope = captureScope();
    runKeyed(
      `bindings:${scope.key}:${threadId}`,
      async () => {
        logEvent("ui.reconcile.bindings", { reason, threadId });
        await revalidateBindings(threadId, scope);
      },
      80,
    );
  }

  function patchThread(threadId, patch) {
    if (!threadId) return false;
    const row = rows().find((item) => item?.id === threadId);
    if (!row) return false;
    Object.assign(row, patch);
    renderThreads();
    return true;
  }

  function eventTime(event) {
    const value = Number(event?.eventPublishedAt);
    return Number.isFinite(value) && value > 0
      ? value
      : Date.now() / 1000;
  }

  function handleBotInbound(event) {
    if (event.projectId && event.projectId !== state.projectId) return;
    const patched = patchThread(event.threadId, {
      updatedAt: eventTime(event),
      status: event.queued
        ? { type: "queued" }
        : { type: "active" },
    });
    if(patched)observeEventVisible(event?.eventPublishedAt);
    else scheduleThreadPage("bot.inbound.missing-thread");
  }

  function handleCodexSummary(message, event) {
    if (event?.projectId && event.projectId !== state.projectId) return {};
    const params = message?.params || {};
    const threadId = (
      params.threadId
      || params.turn?.threadId
      || null
    );
    if (!threadId) return {};
    const updatedAt = eventTime(event);
    if (message.method === "thread/name/updated") {
      const name = (
        params.name
        || params.thread?.name
        || params.title
        || null
      );
      if (name) {
        const patched=patchThread(threadId,{name,updatedAt});
        if(patched)observeEventVisible(event?.eventPublishedAt);
        return { threadId, name };
      }
      scheduleThreadPage("thread.name.missing-payload");
      return { threadId };
    }
    if (
      message.method === "turn/started"
      || message.method === "item/started"
    ) {
      const patched=patchThread(threadId,{
        updatedAt,
        status:{type:"active"},
      });
      if(patched)observeEventVisible(event?.eventPublishedAt);
    } else if (
      message.method === "turn/completed"
      || message.method === "turn/failed"
    ) {
      const patched=patchThread(threadId,{
        updatedAt,
        status:{type:"idle"},
      });
      if(patched)observeEventVisible(event?.eventPublishedAt);
    } else if (message.method === "thread/status/changed") {
      const patched=patchThread(threadId,{
        updatedAt,
        status:params.status||{type:"notLoaded"},
      });
      if(patched)observeEventVisible(event?.eventPublishedAt);
    }
    return { threadId };
  }

  function accept(event) {
    if (event?.type === "hello") {
      const incomingStream = event.streamId || null;
      const incomingSequence = Number(event.sequence || 0);
      const isReconnect = connectedOnce || disconnected;
      if (
        isReconnect
        && (
          incomingStream !== streamId
          || incomingSequence !== lastSequence
        )
      ) {
        scheduleWorkspace("websocket-reconnect");
      }
      streamId = incomingStream;
      lastSequence = incomingSequence;
      connectedOnce = true;
      disconnected = false;
      return false;
    }

    const incomingStream = event?.eventStreamId || null;
    const incomingSequence = Number(event?.eventSequence || 0);
    if (!incomingStream || !incomingSequence) {
      scheduleWorkspace("unversioned-event");
      return true;
    }
    if (streamId && incomingStream !== streamId) {
      streamId = incomingStream;
      lastSequence = incomingSequence;
      scheduleWorkspace("event-stream-changed");
      return true;
    }
    if (
      incomingStream === streamId
      && incomingSequence <= lastSequence
    ) {
      logEvent("ui.event.stale", {
        sequence: incomingSequence,
        lastSequence,
      });
      return false;
    }
    if (
      incomingStream === streamId
      && lastSequence
      && incomingSequence !== lastSequence + 1
    ) {
      scheduleWorkspace("event-sequence-gap");
    }
    streamId = incomingStream;
    lastSequence = incomingSequence;
    return true;
  }

  function noteDisconnect() {
    disconnected = true;
  }

  function handleBindingEvent(event) {
    if (
      event.projectId
      && event.projectId !== state.projectId
    ) {
      return;
    }
    scheduleBindings(event.threadId);
  }

  return {
    accept,
    noteDisconnect,
    patchThread,
    handleBotInbound,
    handleCodexSummary,
    handleBindingEvent,
    scheduleBindings,
    scheduleThreadPage,
    scheduleWorkspace,
    status: () => ({
      streamId,
      lastSequence,
      connectedOnce,
      disconnected,
      pending: [...timers.keys()],
      inflight: [...inflight.keys()],
    }),
  };
}
