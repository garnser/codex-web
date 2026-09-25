import { request } from "./api_client.js";

let snapshot = { mode: "unknown", capabilities: [], quotas: [] };

function element(id) { return document.getElementById(id); }
function value(id) { return element(id)?.value?.trim() || ""; }
function numberOrNull(id) {
  const raw = value(id);
  if (!raw) return null;
  const parsed = Number(raw);
  if (!Number.isFinite(parsed)) throw new Error(`${id} must be numeric.`);
  return parsed;
}
function result(message, failed = false) {
  const host = element("entitlement-management-result");
  if (!host) return;
  host.hidden = false;
  host.textContent = message;
  host.classList.toggle("workspace-state-error", failed);
}
function requireConfirmed(id) {
  if (!element(id)?.checked) throw new Error("Review and confirm the impact before applying.");
}
function refresh() {
  element("refresh-entitlements")?.click();
}
function panel() {
  element("entitlement-management-panel")?.setAttribute("open", "");
}
function fillMode() {
  const select = element("entitlement-manage-mode");
  if (select && ["self_hosted_unlimited", "enforced"].includes(snapshot.mode)) select.value = snapshot.mode;
}
function fillCapability(item) {
  panel();
  element("entitlement-manage-capability").value = item?.capability || "";
  element("entitlement-manage-enabled").checked = item?.enabled !== false;
  element("entitlement-manage-capability-source").value = item?.source || "manual";
  element("entitlement-manage-starts").value = item?.starts_at ?? "";
  element("entitlement-manage-expires").value = item?.expires_at ?? "";
  element("entitlement-capability-confirm").checked = false;
  element("entitlement-manage-capability").focus();
}
function fillQuota(item) {
  panel();
  element("entitlement-manage-metric").value = item?.metric || "";
  element("entitlement-manage-limit").value = item?.limit ?? "";
  element("entitlement-manage-window").value = item?.window || "month";
  element("entitlement-manage-behavior").value = item?.behavior || "hard_stop";
  element("entitlement-manage-warning").value = item?.warning_fraction ?? 0.8;
  element("entitlement-manage-quota-source").value = item?.source || "manual";
  element("entitlement-quota-confirm").checked = false;
  element("entitlement-manage-metric").focus();
}
async function setMode() {
  requireConfirmed("entitlement-mode-confirm");
  const mode = value("entitlement-manage-mode");
  if (!["self_hosted_unlimited", "enforced"].includes(mode)) throw new Error("Choose a valid tenant mode.");
  result("Applying tenant-wide entitlement mode…");
  await request("/api/entitlements/mode", { method: "PUT", body: JSON.stringify({ mode }) });
  element("entitlement-mode-confirm").checked = false;
  result(`Tenant mode set to ${mode}.`);
  refresh();
}
async function setCapability() {
  requireConfirmed("entitlement-capability-confirm");
  const capability = value("entitlement-manage-capability");
  if (!capability) throw new Error("Capability is required.");
  const source = value("entitlement-manage-capability-source");
  if (!source) throw new Error("Source is required.");
  const payload = {
    enabled: Boolean(element("entitlement-manage-enabled")?.checked),
    source,
    starts_at: numberOrNull("entitlement-manage-starts"),
    expires_at: numberOrNull("entitlement-manage-expires"),
  };
  if (payload.starts_at != null && payload.expires_at != null && payload.expires_at <= payload.starts_at) {
    throw new Error("Expiration must be later than the start time.");
  }
  result(`Applying capability ${capability}…`);
  await request(`/api/entitlements/capabilities/${encodeURIComponent(capability)}`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });
  element("entitlement-capability-confirm").checked = false;
  result(`Capability ${capability} updated.`);
  refresh();
}
async function setQuota() {
  requireConfirmed("entitlement-quota-confirm");
  const metric = value("entitlement-manage-metric");
  if (!metric) throw new Error("Quota metric is required.");
  const limit = numberOrNull("entitlement-manage-limit");
  if (limit == null || limit < 0) throw new Error("Quota limit must be zero or greater.");
  const warning = numberOrNull("entitlement-manage-warning");
  if (warning == null || warning < 0 || warning > 1) throw new Error("Warning fraction must be between 0 and 1.");
  const source = value("entitlement-manage-quota-source");
  if (!source) throw new Error("Source is required.");
  const payload = {
    limit,
    window: value("entitlement-manage-window"),
    behavior: value("entitlement-manage-behavior"),
    warning_fraction: warning,
    source,
  };
  result(`Applying quota ${metric}…`);
  await request(`/api/entitlements/quotas/${encodeURIComponent(metric)}`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });
  element("entitlement-quota-confirm").checked = false;
  result(`Quota ${metric} updated.`);
  refresh();
}
function run(button, fn) {
  button?.addEventListener("click", async () => {
    button.disabled = true;
    try { await fn(); } catch (error) { result(error.message || "Entitlement change rejected.", true); }
    finally { button.disabled = false; }
  });
}
function bind() {
  run(element("set-entitlement-mode"), setMode);
  run(element("set-entitlement-capability"), setCapability);
  run(element("set-entitlement-quota"), setQuota);
  window.addEventListener("codex:entitlement-state-rendered", (event) => {
    snapshot = event.detail || snapshot;
    fillMode();
  });
  document.addEventListener("click", (event) => {
    const capability = event.target.closest?.("[data-manage-entitlement-capability]")?.dataset.manageEntitlementCapability;
    if (capability) fillCapability(snapshot.capabilities.find((item) => item.capability === capability) || { capability });
    const metric = event.target.closest?.("[data-manage-entitlement-quota]")?.dataset.manageEntitlementQuota;
    if (metric) fillQuota(snapshot.quotas.find((item) => item.metric === metric) || { metric });
  });
}
if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", bind, { once: true });
else bind();
