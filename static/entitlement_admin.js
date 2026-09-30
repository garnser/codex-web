import { captureProjectView, currentProjectId } from './project_view_scope.js';
import { renderManagement, discardManagement, disposeManagement } from './entitlement_editor.js';
import { bindWhenReady } from './reference_links.js';
(async () => {
  const BASE = window.location.pathname.startsWith("/codex") ? "/codex" : "";
  const { request: apiRequest } = await import(`${BASE}/static/api_client.js`);
  const MAX_USAGE_ROWS = 100;
  let capabilities = [];
  let quotas = [];
  let usage = [];
  let mode = "unknown";
  let generation = 0;
  function operation() {
    const view = captureProjectView(); const project = currentProjectId(); const started = generation;
    const current = () => view.current() && started === generation;
    return { current, async request(path, options) {
      if (!current()) throw new DOMException('View changed', 'AbortError');
      const url = new URL(path, location.origin); if (project) url.searchParams.set('project_id', project);
      const result = await apiRequest(url.pathname + url.search, options);
      if (!current()) throw new DOMException('View changed', 'AbortError'); return result;
    } };
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");
  }

  function timeText(value) {
    if (!value) return "none";
    const date = new Date(Number(value) * 1000);
    return Number.isNaN(date.valueOf()) ? "unknown" : date.toLocaleString();
  }

  function setStatus(message) {
    const host = document.getElementById("entitlement-status");
    if (host) {
      host.hidden = false;
      host.textContent = message;
    }
  }

  function renderMode() {
    const host = document.getElementById("entitlement-mode");
    if (!host) return;
    const unlimited = mode === "self_hosted_unlimited";
    host.innerHTML = `<div class="comm-entry">
      <strong>${escapeHtml(mode)}</strong>
      <small>${unlimited
        ? "Self-hosted unlimited mode bypasses commercial entitlement/quota enforcement, but does not grant RBAC, policy, agent, or action authority."
        : "Enforced mode requires both canonical entitlement/quota checks and independent authorization/policy checks."}</small>
    </div>`;
  }

  function renderCapabilities() {
    const host = document.getElementById("entitlement-capabilities");
    if (!host) return;
    host.innerHTML = capabilities.map((item, index) => {
      const decision = item.decision || {};
      return `<div class="comm-entry">
        <strong>${escapeHtml(item.capability)} · ${item.enabled ? "enabled" : "disabled"}</strong>
        <small>Canonical decision: ${decision.allowed ? "allowed" : "denied"} · reason: ${escapeHtml(decision.reason || "unknown")} · mode: ${escapeHtml(decision.mode || mode)}</small>
        <small>Source: ${escapeHtml(item.source)} · Starts: ${timeText(item.starts_at)} · Expires: ${timeText(item.expires_at)}</small>
        <small>Updated by: ${escapeHtml(item.updated_by)} · ${timeText(item.updated_at)} · ID: ${escapeHtml(item.id)}</small>
      </div>`;
    }).join("") || `<div class="comm-entry">
      <strong>No explicit capability entitlement records.</strong>
      <small>${mode === "self_hosted_unlimited"
        ? "This is expected in explicit self-hosted unlimited mode."
        : "In enforced mode, missing capability records produce deterministic capability_not_entitled decisions."}</small>
    </div>`;
  }

  function renderQuotas() {
    const host = document.getElementById("entitlement-quotas");
    if (!host) return;
    host.innerHTML = quotas.map((item) => `<div class="comm-entry">
      <strong>${escapeHtml(item.metric)} · limit ${escapeHtml(item.limit)} / ${escapeHtml(item.window)}</strong>
      <small>Behavior: ${escapeHtml(item.behavior)} · warning threshold: ${escapeHtml(Math.round(Number(item.warning_fraction || 0) * 100))}% · source: ${escapeHtml(item.source)}</small>
      <small>Updated by: ${escapeHtml(item.updated_by)} · ${timeText(item.updated_at)} · ID: ${escapeHtml(item.id)}</small>
      <small>Current usage in this policy window: ${escapeHtml(item.current_usage ?? "unknown")}. Quota status is evaluated canonically for a selected capability below; usage never grants authority.</small>
    </div>`).join("") || '<div class="comm-entry"><strong>No quota policies configured.</strong></div>';
  }

  function renderUsage() {
    const host = document.getElementById("entitlement-usage");
    if (!host) return;
    const totals = new Map();
    for (const item of usage) {
      totals.set(item.metric, (totals.get(item.metric) || 0) + Number(item.amount || 0));
    }
    const summary = Array.from(totals.entries())
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([metric, total]) => `${metric}=${total}`)
      .join(" · ");
    const rows = usage.slice(-MAX_USAGE_ROWS).reverse();
    host.innerHTML = `<div class="comm-entry">
      <strong>Usage totals in loaded ledger</strong>
      <small>${escapeHtml(summary || "none")}</small>
    </div>` + rows.map((item) => `<div class="comm-entry">
      <strong>${escapeHtml(item.metric)} · ${escapeHtml(item.amount)} · ${escapeHtml(item.source)}</strong>
      <small>Occurred: ${timeText(item.occurred_at)} · Received: ${timeText(item.received_at)} · actor: ${escapeHtml(item.actor_id)}</small>
      <small>Project: ${escapeHtml(item.project_id || "none")} · Resource: ${escapeHtml(item.resource_id || "none")} · Work: ${escapeHtml(item.work_item_ref || "none")} · ActionIntent: ${escapeHtml(item.action_intent_id || "none")}</small>
      <small>Idempotency key: ${escapeHtml(item.idempotency_key)} · Event: ${escapeHtml(item.id)}</small>
    </div>`).join("");
  }

  function populatePreview() {
    const capabilitySelect = document.getElementById("entitlement-preview-capability");
    const metricSelect = document.getElementById("entitlement-preview-metric");
    if (capabilitySelect) {
      const names = Array.from(new Set(capabilities.map((item) => item.capability))).sort();
      capabilitySelect.innerHTML = names.map((name) => (
        `<option value="${escapeHtml(name)}">${escapeHtml(name)}</option>`
      )).join("");
    }
    if (metricSelect) {
      const names = Array.from(new Set([
        ...quotas.map((item) => item.metric),
        ...usage.map((item) => item.metric),
      ])).sort();
      metricSelect.innerHTML = '<option value="">Capability only</option>'
        + names.map((name) => `<option value="${escapeHtml(name)}">${escapeHtml(name)}</option>`).join("");
    }
  }

  function renderDecision(decision) {
    const host = document.getElementById("entitlement-preview-result");
    if (!host) return;
    const quota = decision.quota;
    host.innerHTML = `<div class="comm-entry">
      <strong>${decision.allowed ? "Allowed" : "Denied"} · ${escapeHtml(decision.capability)} · ${escapeHtml(decision.reason)}</strong>
      <small>Entitlement mode: ${escapeHtml(decision.mode)}</small>
      ${quota ? `<small>Quota ${escapeHtml(quota.metric)}: usage ${escapeHtml(quota.usage)} · projected ${escapeHtml(quota.projected_usage)} · limit ${escapeHtml(quota.limit)} · remaining ${escapeHtml(quota.remaining)}</small>
      <small>Window: ${escapeHtml(quota.window)} · ${timeText(quota.window_start)} → ${timeText(quota.window_end)} · behavior: ${escapeHtml(quota.behavior)} · warning: ${quota.warning ? "yes" : "no"} · exceeded: ${quota.exceeded ? "yes" : "no"}</small>` : ""}
      <small>This decision answers service entitlement/quota only; it does not grant human RBAC, agent authority, policy approval, or provider permission.</small>
    </div>`;
  }

  async function preview() {
    const op = operation();
    const capability = document.getElementById("entitlement-preview-capability")?.value || "";
    const metric = document.getElementById("entitlement-preview-metric")?.value || "";
    const projectedAmount = Number(document.getElementById("entitlement-preview-amount")?.value || 0);
    if (!capability) {
      setStatus("Choose an entitled capability to evaluate.");
      return;
    }
    const params = new URLSearchParams({
      capability,
      projected_amount: String(Number.isFinite(projectedAmount) ? Math.max(0, projectedAmount) : 0),
    });
    if (metric) params.set("metric", metric);
    try {
      const decision = await op.request(`/api/entitlements/status?${params.toString()}`);
      renderDecision(decision);
    } catch (error) {
      if (!op.current()) return;
      const host = document.getElementById("entitlement-preview-result");
      if (host) host.innerHTML = `<div class="comm-entry"><strong>Entitlement evaluation unavailable</strong><small>${escapeHtml(error.message)}</small></div>`;
    }
  }

  async function refresh(force = false) {
    if (!force && !discardManagement()) return;
    generation++; disposeManagement();
    for (const id of ['entitlement-management', 'entitlement-mode', 'entitlement-capabilities', 'entitlement-quotas', 'entitlement-usage', 'entitlement-preview-result']) document.getElementById(id)?.replaceChildren();
    const op = operation();
    setStatus("Loading canonical entitlement, quota and usage state...");
    try {
      const [snapshot, usageResponse] = await Promise.all([
        op.request("/api/entitlements/administration"), op.request("/api/entitlements/usage"),
      ]);
      if (!op.current()) return;
      mode = snapshot.mode; capabilities = snapshot.capabilities || []; quotas = snapshot.quotas || []; usage = usageResponse.items || [];
      renderMode(); renderCapabilities(); renderQuotas(); renderUsage(); populatePreview();
      renderManagement(document.getElementById('entitlement-management'), snapshot, op, () => refresh(true));
      setStatus(`${capabilities.length} capability record(s) · ${quotas.length} quota policy(s) · ${usage.length} usage event(s) · mode ${mode}.`);
      window.dispatchEvent(new CustomEvent("codex:entitlement-state-rendered", { detail: { mode, capabilities, quotas, usage } }));
    } catch (error) { if (op.current()) setStatus(`Entitlement administration unavailable: ${error.message}`); }
  }

  function bind() {
    const panel = document.getElementById("developer-panel");
    document.getElementById("refresh-entitlements")?.addEventListener("click", () => void refresh());
    document.getElementById("refresh-developer")?.addEventListener("click", () => void refresh());
    document.getElementById("preview-entitlement")?.addEventListener("click", () => preview().catch(console.error));
    panel?.addEventListener("toggle", () => {
      if (panel.open) refresh().catch(console.error);
    });
    if (panel?.open) refresh().catch(console.error);
  }

  bindWhenReady(bind);
  window.addEventListener('codex:project-changed', () => void refresh(true));
  window.addEventListener('codex:project-workspace-page', event => {
    if (event.detail?.workspace === 'settings' && event.detail?.page === 'configuration') void refresh();
  });
})();
