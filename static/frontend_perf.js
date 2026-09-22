const MAX_SAMPLES = 200;

export const FRONTEND_BUDGETS = Object.freeze({
  projectUsefulMs: 1000,
  websocketVisibleMs: 250,
  turnCompletionRequests: 2,
  maxRequestBurst: 12,
  maxSameEndpointBurst: 4,
  maxThreadRows: 100,
  maxWorkItemRows: 60,
  maxRoutineResponseBytes: 1_000_000,
  maxRenderMs: 200,
  maxLongTaskMs: 200,
});

const state = {
  requests: [],
  renders: [],
  eventLatencies: [],
  longTasks: [],
  milestones: {},
};

function pushBounded(target, value) {
  target.push(value);
  if (target.length > MAX_SAMPLES) {
    target.splice(0, target.length - MAX_SAMPLES);
  }
}

export function endpointIdentity(path) {
  try {
    const url = new URL(String(path || ""), window.location.origin);
    const keys = [...url.searchParams.keys()].sort();
    return keys.length
      ? `${url.pathname}?${keys.join("&")}`
      : url.pathname;
  } catch {
    return String(path || "").split("?", 1)[0];
  }
}

export function responseBytes(text) {
  if (!text) return 0;
  if (typeof TextEncoder !== "undefined") {
    return new TextEncoder().encode(text).byteLength;
  }
  return String(text).length;
}

export function observeRequest({
  path,
  method = "GET",
  status = 0,
  bytes = 0,
  latencyMs = 0,
  parseMs = 0,
  aborted = false,
} = {}) {
  pushBounded(state.requests, {
    endpoint: endpointIdentity(path),
    method: String(method || "GET").toUpperCase(),
    status: Number(status || 0),
    bytes: Math.max(0, Number(bytes || 0)),
    latencyMs: Math.max(0, Number(latencyMs || 0)),
    parseMs: Math.max(0, Number(parseMs || 0)),
    aborted: Boolean(aborted),
    at: Date.now(),
  });
}

export function requestWindowStatus({
  sinceMs = 1000,
  maxRequests = FRONTEND_BUDGETS.maxRequestBurst,
  maxPerEndpoint = FRONTEND_BUDGETS.maxSameEndpointBurst,
} = {}) {
  const threshold = Date.now() - Math.max(0, Number(sinceMs || 0));
  const requests = state.requests.filter((item) => item.at >= threshold);
  const counts = new Map();
  requests.forEach((item) => {
    const key = `${item.method} ${item.endpoint}`;
    counts.set(key, (counts.get(key) || 0) + 1);
  });
  const endpoints = [...counts.entries()]
    .map(([key, count]) => ({ key, count }))
    .sort((a, b) => b.count - a.count || a.key.localeCompare(b.key));
  const repeated = endpoints.filter((item) => item.count > maxPerEndpoint);
  return {
    ok: requests.length <= maxRequests && repeated.length === 0,
    total: requests.length,
    maxRequests,
    maxPerEndpoint,
    repeated,
    endpoints,
  };
}

export function observeRender(
  name,
  startedAt,
  { rows = 0, nodes = null } = {},
) {
  const durationMs = Math.max(0, performance.now() - Number(startedAt || 0));
  pushBounded(state.renders, {
    name: String(name || "unknown"),
    durationMs,
    rows: Math.max(0, Number(rows || 0)),
    nodes: nodes == null ? null : Math.max(0, Number(nodes || 0)),
    at: Date.now(),
  });
  return durationMs;
}

export function observeEventVisible(eventPublishedAt) {
  const published = Number(eventPublishedAt || 0);
  if (!published) return null;
  const publishedMs = published > 1e12 ? published : published * 1000;
  const latencyMs = Math.max(0, Date.now() - publishedMs);
  pushBounded(state.eventLatencies, { latencyMs, at: Date.now() });
  return latencyMs;
}

export function markMilestone(name, startedAt) {
  const durationMs = Math.max(0, performance.now() - Number(startedAt || 0));
  state.milestones[String(name)] = {
    durationMs,
    at: Date.now(),
  };
  return durationMs;
}

export function startLongTaskObserver() {
  if (
    typeof PerformanceObserver === "undefined"
    || !PerformanceObserver.supportedEntryTypes?.includes("longtask")
  ) {
    return () => {};
  }
  const observer = new PerformanceObserver((list) => {
    list.getEntries().forEach((entry) => {
      pushBounded(state.longTasks, {
        durationMs: Math.max(0, Number(entry.duration || 0)),
        at: Date.now(),
      });
    });
  });
  observer.observe({ type: "longtask", buffered: true });
  return () => observer.disconnect();
}

export function snapshot() {
  return {
    budgets: { ...FRONTEND_BUDGETS },
    requests: state.requests.map((item) => ({ ...item })),
    renders: state.renders.map((item) => ({ ...item })),
    eventLatencies: state.eventLatencies.map((item) => ({ ...item })),
    longTasks: state.longTasks.map((item) => ({ ...item })),
    milestones: { ...state.milestones },
  };
}

export function reset() {
  state.requests.length = 0;
  state.renders.length = 0;
  state.eventLatencies.length = 0;
  state.longTasks.length = 0;
  state.milestones = {};
}

export function budgetStatus() {
  const latestProject = state.milestones["project-useful"]?.durationMs ?? 0;
  const latestEvent = state.eventLatencies.at(-1)?.latencyMs ?? 0;
  const maxResponse = Math.max(0, ...state.requests.map((item) => item.bytes));
  const maxRender = Math.max(0, ...state.renders.map((item) => item.durationMs));
  const maxLongTask = Math.max(0, ...state.longTasks.map((item) => item.durationMs));
  const requestWindow = requestWindowStatus();
  return {
    projectUseful: latestProject <= FRONTEND_BUDGETS.projectUsefulMs,
    websocketVisible: latestEvent <= FRONTEND_BUDGETS.websocketVisibleMs,
    responseBytes: maxResponse <= FRONTEND_BUDGETS.maxRoutineResponseBytes,
    render: maxRender <= FRONTEND_BUDGETS.maxRenderMs,
    longTask: maxLongTask <= FRONTEND_BUDGETS.maxLongTaskMs,
    requestBurst: requestWindow.total <= requestWindow.maxRequests,
    duplicateRequests: requestWindow.repeated.length === 0,
  };
}

if (typeof window !== "undefined") {
  window.__codexFrontendPerf = {
    budgets: FRONTEND_BUDGETS,
    snapshot,
    reset,
    budgetStatus,
    requestWindowStatus,
  };
}
