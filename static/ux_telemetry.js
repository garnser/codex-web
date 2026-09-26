const BASE = window.location.pathname.startsWith("/codex") ? "/codex" : "";
const POLICY_PATH = "/api/ux-telemetry/policy";
const EVENT_PATH = "/api/ux-telemetry/events";
const JOURNEY_KEY = "codex-web-ux-journey-v1";

let policyPromise = null;

export function journeyId() {
  let value = sessionStorage.getItem(JOURNEY_KEY);
  if (value) return value;
  value = typeof crypto?.randomUUID === "function"
    ? crypto.randomUUID().replaceAll("-", "_")
    : `journey_${Date.now()}_${Math.random().toString(36).slice(2)}`;
  sessionStorage.setItem(JOURNEY_KEY, value);
  return value;
}

export function coarseRoute(locationLike = window.location) {
  const path = String(locationLike?.pathname || "");
  const hash = String(locationLike?.hash || "");
  if (path.includes("/administration") || hash.startsWith("#administration")) return "administration";
  if (path.includes("/projects/") && path.includes("/overview")) return "project_overview";
  if (path.includes("/work-items") || hash.includes("workspace/work")) return "project_work";
  if (path.includes("/threads") || hash.includes("workspace/threads")) return "project_threads";
  if (path.includes("/agents") || hash.includes("workspace/agents")) return "project_agents";
  if (path.includes("/automations") || hash.includes("workspace/autonomy")) return "project_automation";
  if (path.includes("/operations") || hash.includes("workspace/operations")) return "project_operations";
  if (path.includes("/setup") || hash.includes("workspace/setup")) return "project_setup";
  return "other";
}

async function policy() {
  if (!policyPromise) {
    policyPromise = fetch(BASE + POLICY_PATH, {
      headers: { "Accept": "application/json" },
    })
      .then(async (response) => {
        if (!response.ok) return { enabled: false };
        const payload = await response.json();
        return payload && typeof payload === "object" ? payload : { enabled: false };
      })
      .catch(() => ({ enabled: false }));
  }
  return policyPromise;
}

export async function trackUx(eventName, {
  workflow,
  step = null,
  routeGroup = null,
  durationMs = null,
  retryCount = null,
  journey = null,
} = {}) {
  try {
    const currentPolicy = await policy();
    if (!currentPolicy.enabled) return false;
    const payload = {
      event_name: eventName,
      workflow,
      step,
      route_group: routeGroup,
      duration_ms: durationMs == null ? null : Math.max(0, Math.round(Number(durationMs) || 0)),
      retry_count: retryCount == null ? null : Math.max(0, Math.round(Number(retryCount) || 0)),
      journey_id: journey || journeyId(),
    };
    const response = await fetch(BASE + EVENT_PATH, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      keepalive: true,
    });
    return response.ok;
  } catch {
    return false;
  }
}

export function resetUxTelemetryPolicyCache() {
  policyPromise = null;
}
