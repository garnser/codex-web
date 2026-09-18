(async () => {
  const BASE = window.location.pathname.startsWith("/codex") ? "/codex" : "";
  const { request: apiRequest } = await import(`${BASE}/static/api_client.js`);
  const MAX_INVOCATIONS = 100;

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");
  }

  function listText(values) {
    return values?.length ? values.map(escapeHtml).join(", ") : "none";
  }

  function money(value) {
    return value == null ? "unknown" : `$${Number(value).toFixed(6)}`;
  }

  function timeText(value) {
    if (!value) return "none";
    const date = new Date(Number(value) * 1000);
    return Number.isNaN(date.valueOf()) ? "unknown" : date.toLocaleString();
  }

  function renderProviders(items) {
    const host = document.getElementById("model-provider-list");
    if (!host) return;
    host.innerHTML = items.map((item) => `<div class="comm-entry">
      <strong>${escapeHtml(item.display_name)} · ${escapeHtml(item.id)} · ${escapeHtml(item.status)}</strong>
      <small>Adapter: ${escapeHtml(item.adapter_type)} · Endpoint: ${escapeHtml(item.base_url || "provider default")}</small>
      <small>Credential reference: ${escapeHtml(item.credential_ref || "none")} · Credential required: ${item.credential_required ? "yes" : "no"}</small>
      <small>Residency: ${listText(item.residency_tags)} · Compliance: ${listText(item.compliance_tags)}</small>
      <small>Updated by ${escapeHtml(item.updated_by)} · ${timeText(item.updated_at)}</small>
    </div>`).join("") || '<div class="comm-entry"><strong>No model providers registered.</strong></div>';
  }

  function renderModels(items) {
    const host = document.getElementById("model-definition-list");
    if (!host) return;
    host.innerHTML = items.map((item) => `<div class="comm-entry">
      <strong>${escapeHtml(item.id)} · ${escapeHtml(item.lifecycle)}</strong>
      <small>Provider: ${escapeHtml(item.provider_id)} · Concrete model: ${escapeHtml(item.concrete_model)} · Version: ${escapeHtml(item.model_version || "unspecified")}</small>
      <small>Classes: ${listText(item.model_classes)} · Capabilities: ${listText(item.capabilities)} · Modalities: ${listText(item.modalities)} · Tools: ${item.supports_tools ? "yes" : "no"}</small>
      <small>Context: ${escapeHtml(item.context_window_tokens)} · Max output: ${escapeHtml(item.max_output_tokens)} · Latency: ${escapeHtml(item.latency_class)} · Route priority: ${escapeHtml(item.route_priority)}</small>
      <small>Pricing / 1M tokens: input ${money(item.input_price_per_million_usd)} · output ${money(item.output_price_per_million_usd)}</small>
      <small>Residency: ${listText(item.residency_tags)} · Compliance: ${listText(item.compliance_tags)}</small>
    </div>`).join("") || '<div class="comm-entry"><strong>No model definitions registered.</strong></div>';
  }

  function renderPolicy(item) {
    const host = document.getElementById("model-policy");
    if (!host) return;
    host.innerHTML = `<div class="comm-entry">
      <strong>Effective tenant model policy</strong>
      <small>Allowed providers: ${listText(item.allowed_provider_ids)} · Allowed models: ${listText(item.allowed_model_ids)}</small>
      <small>Required residency: ${listText(item.required_residency_tags)} · Required compliance: ${listText(item.required_compliance_tags)}</small>
      <small>Max invocation cost: ${money(item.max_invocation_cost_usd)} · Max attempts: ${escapeHtml(item.max_attempts)}</small>
      <small>Updated by ${escapeHtml(item.updated_by)} · ${timeText(item.updated_at)}</small>
    </div>`;
  }

  function renderPrompts(items) {
    const host = document.getElementById("model-prompt-list");
    if (!host) return;
    host.innerHTML = items.map((item) => `<div class="comm-entry">
      <strong>${escapeHtml(item.template_id)} @ ${escapeHtml(item.version)} · ${item.active ? "active" : "inactive"}</strong>
      <small>SHA-256: ${escapeHtml(item.checksum_sha256)} · Content length: ${escapeHtml(item.content?.length || 0)} characters</small>
      <small>Updated by ${escapeHtml(item.updated_by)} · ${timeText(item.updated_at)}</small>
    </div>`).join("") || '<div class="comm-entry"><strong>No prompt templates registered.</strong></div>';
  }

  function attemptText(attempt) {
    const tokens = [
      attempt.input_tokens != null ? `in=${attempt.input_tokens}` : null,
      attempt.output_tokens != null ? `out=${attempt.output_tokens}` : null,
      attempt.actual_cost_usd != null ? `cost=${money(attempt.actual_cost_usd)}` : null,
    ].filter(Boolean).join(" · ");
    return `${attempt.provider_id}/${attempt.model_id} · ${attempt.outcome}${attempt.error_code ? ` · ${attempt.error_code}` : ""}${tokens ? ` · ${tokens}` : ""}`;
  }

  function renderInvocations(items) {
    const host = document.getElementById("model-invocation-list");
    if (!host) return;
    const rows = (items || []).slice(0, MAX_INVOCATIONS);
    host.innerHTML = rows.map((item) => `<div class="comm-entry">
      <strong>${escapeHtml(item.model_class)} · ${escapeHtml(item.status)} · ${escapeHtml(item.purpose)}</strong>
      <small>${timeText(item.created_at)} · actor ${escapeHtml(item.actor_id)} · selected: ${escapeHtml(item.selected_provider_id || "none")}/${escapeHtml(item.selected_model_id || "none")} ${escapeHtml(item.selected_concrete_model || "")}</small>
      <small>Prompt: ${escapeHtml(item.prompt_template_id)} @ ${escapeHtml(item.prompt_template_version)} · checksum ${escapeHtml(item.prompt_template_checksum_sha256)}</small>
      <small>Routing: ${escapeHtml(item.route_reason)} · Policy fingerprint: ${escapeHtml(item.policy_fingerprint_sha256)} · Max cost: ${money(item.max_cost_usd)}</small>
      <small>Capabilities: ${listText(item.required_capabilities)} · Residency: ${listText(item.required_residency_tags)} · Compliance: ${listText(item.required_compliance_tags)}</small>
      <small>Context attribution: work ${escapeHtml(item.work_item_ref || "none")} · goal ${escapeHtml(item.goal_id || "none")} · decision ${escapeHtml(item.decision_id || "none")} · execution ${escapeHtml(item.execution_id || "none")}</small>
      <small>Attempts: ${item.attempts?.length ? item.attempts.map(attemptText).map(escapeHtml).join(" | ") : "none"}</small>
    </div>`).join("") || '<div class="comm-entry"><strong>No model invocation records.</strong><small>Prompt/message content is intentionally not stored in invocation metadata.</small></div>';
  }

  async function refresh() {
    const status = document.getElementById("model-gateway-status");
    if (status) status.textContent = "Loading canonical model routing state...";
    try {
      const [providers, models, prompts, policy, invocations] = await Promise.all([
        apiRequest("/api/model-gateway/providers"),
        apiRequest("/api/model-gateway/models"),
        apiRequest("/api/model-gateway/prompts"),
        apiRequest("/api/model-gateway/policy"),
        apiRequest(`/api/model-gateway/invocations?limit=${MAX_INVOCATIONS}`),
      ]);
      renderProviders(providers.items || []);
      renderModels(models.items || []);
      renderPrompts(prompts.items || []);
      renderPolicy(policy.item || {});
      renderInvocations(invocations.items || []);
      if (status) status.textContent = `${providers.items?.length || 0} provider(s) · ${models.items?.length || 0} model(s) · ${prompts.items?.length || 0} prompt revision(s). Credentials remain secret references.`;
      window.dispatchEvent(new CustomEvent("codex:model-gateway-rendered", {
        detail: {
          providers: providers.items || [],
          models: models.items || [],
          prompts: prompts.items || [],
          policy: policy.item || {},
        },
      }));
    } catch (error) {
      if (status) status.textContent = `Model Gateway unavailable: ${error.message}`;
    }
  }

  function bind() {
    const panel = document.getElementById("developer-panel");
    document.getElementById("refresh-model-gateway")?.addEventListener("click", refresh);
    document.getElementById("refresh-developer")?.addEventListener("click", refresh);
    panel?.addEventListener("toggle", () => {
      if (panel.open) refresh().catch(console.error);
    });
    if (panel?.open) refresh().catch(console.error);
  }

  window.addEventListener("DOMContentLoaded", bind);
})();
