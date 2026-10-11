import {normalizeCodexRateLimitResources, normalizeUsageResources} from "./usage_resources.js";

// Coalesce runtime bursts without introducing a polling loop. Explicit and
// project-change refreshes run immediately; event refreshes share one timer.
export function createUsageRefresher({refresh, eventDelayMs = 5000,
  schedule = setTimeout, cancel = clearTimeout}) {
  let timer = null;
  let running = null;
  let pending = false;
  let pendingFull = false;

  function run() {
    if (running || !pending) return running;
    const full = pendingFull;
    pending = false;
    pendingFull = false;
    // Defer refresh until running is installed, even for synchronous callbacks.
    const task = Promise.resolve().then(() => refresh({includeRateLimits: full}));
    running = task;
    task.finally(() => {
      running = null;
      if (pending) {
        if (pendingFull) run();
        else if (timer === null) timer = schedule(onTimer, eventDelayMs);
      }
    }).catch(() => {});
    return task;
  }

  function onTimer() {
    timer = null;
    run();
  }

  return {
    request({event = false} = {}) {
      pending = true;
      pendingFull ||= !event;
      if (event) {
        if (timer === null && !running) timer = schedule(onTimer, eventDelayMs);
        return running;
      }
      if (timer !== null) { cancel(timer); timer = null; }
      return run();
    },
  };
}

export function createHeaderUsageController({state, api, render, log, schedule, cancel}) {
  let rateLimitGeneration = 0;
  async function refresh({includeRateLimits}) {
    const projectId = state.projectId;
    const generation = rateLimitGeneration;
    try {
      const [limitsResult, usageResult] = await Promise.allSettled([
        includeRateLimits ? api("/api/account/rate-limits") : Promise.resolve(null),
        api(`/api/agent-runtime-usage?include_items=false${projectId ? `&project_id=${encodeURIComponent(projectId)}` : ""}`),
      ]);
      if (projectId !== state.projectId) return;
      const providerResources = !includeRateLimits || generation !== rateLimitGeneration
        ? state.accountUsageResources.filter(resource => resource.kind === "rate_limit")
        : limitsResult.status === "fulfilled" ? normalizeUsageResources(limitsResult.value) : [];
      const resources = [
        ...providerResources,
        ...(usageResult.status === "fulfilled" && Array.isArray(usageResult.value.resources) ? usageResult.value.resources : []),
      ];
      const unique = new Map(resources.map(resource => [
        [resource.provider_id, resource.account_id, resource.runtime_id, resource.model_id, resource.resource_id, resource.period_started_at, resource.reset_at].join("|"),
        resource,
      ]));
      state.accountUsageResources = [...unique.values()];
      state.usageProjectId = projectId;
      render();
    } catch (error) {
      log("token.error", {message: error.message});
      render();
    }
  }
  const refresher = createUsageRefresher({refresh, schedule, cancel});
  return {
    request: () => refresher.request(),
    handleRateLimits(payload) {
      const resources = normalizeCodexRateLimitResources(payload);
      if (resources.length) {
        rateLimitGeneration += 1;
        state.accountUsageResources = [
          ...state.accountUsageResources.filter(resource =>
            !(resource.provider_id === "openai" && resource.runtime_id === "codex" && resource.kind === "rate_limit")),
          ...resources,
        ];
        render();
      }
      refresher.request({event: true});
    },
  };
}
