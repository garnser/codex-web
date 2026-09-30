import { request } from "./api_client.js";
import { endpointIdentity } from "./frontend_perf.js";

export function createLoggedApi(logEvent, getProjectId = () => "") {
  return async function api(path, options = {}) {
    if (/^\/api\/(threads(?:\/|\?|$)|thread-settings(?:\?|$)|turns\/interrupt(?:\?|$))/.test(path)) {
      const url = new URL(path, location.origin);
      let projectId = getProjectId();
      if (typeof options.body === "string") {
        try { projectId = JSON.parse(options.body)?.project_id ?? projectId; } catch { /* API validates bodies. */ }
      }
      if (projectId && !url.searchParams.has("project_id")) url.searchParams.set("project_id", projectId);
      path = url.pathname + url.search;
    }
    const endpoint = endpointIdentity(path);
    logEvent("api.request", {
      endpoint,
      method: options.method || "GET",
    });
    try {
      const result = await request(path, options);
      logEvent("api.response", { endpoint });
      return result;
    } catch (error) {
      if (error?.name !== "AbortError") {
        logEvent("api.error", {
          endpoint,
          status: error?.status || 0,
        });
      }
      throw error;
    }
  };
}
