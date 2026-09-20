import { request } from "./api_client.js";
import { endpointIdentity } from "./frontend_perf.js";

export function createLoggedApi(logEvent) {
  return async function api(path, options = {}) {
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
