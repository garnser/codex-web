(async () => {
  const BASE = window.location.pathname.startsWith("/codex") ? "/codex" : "";
  const { request: apiRequest } = await import(`${BASE}/static/api_client.js`);
  const MAX_INVOCATIONS = 50;
  let snapshot = { providers: [], models: [], prompts: [], policy: null, invocations: [], capacity: [], capacityWaits: [] };

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

  function listText(values) {
    return values?.length ? values.map(escapeHtml).join(", ") : "none";
  }

  function csv(id) {
    return (document.getElementById(id)?.value || "")
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean);
  }

  function setStatus(message) {
    const element = document.getElementById("model-gateway-status");
    if (element) {
      element.hidden = false;
      element.textContent = message;
    }
  }

  function renderPolicy(policy) {
    const host = document.getElementById("model-gateway-policy");
    if (!host) return;
    host.innerHTML = `<div class="comm-entry">
      <strong>Tenant model policy</strong>
      <small>Allowed providers: ${listText(policy.allowed_provider_ids)} · Allowed models: ${listText(policy.allowed_model_ids)}</small>
      <small>Required residency: ${listText(policy.required_residency_tags)} · Required compliance: ${listText(policy.required_compliance_tags)}</small>
      <small>Max invocation cost: ${policy.max_invocation_cost_usd == null ? "unbounded by tenant policy" : `$${escapeHtml(policy.max_invocation_cost_usd)}`} · Max attempts: ${escapeHtml(policy.max_attempts)}</small>
      <small>Updated by: ${escapeHtml(policy.updated_by)} · ${timeText(policy.updated_at)}</small>
    </div>`;
  }

  function renderProviders(items) {
    const host = document.getElementById("model-provider-list");
    if (!host) return;
    host.innerHTML = items.map((item) => {
      const capacity = snapshot.capacity.find((record) => (
        record.provider_id === item.id && !record.runtime_id
      ));
      const capacityLine = capacity
        ? `<small>Capacity: ${escapeHtml(capacity.status)} · retry/reset: ${timeText(capacity.retry_at)}${capacity.reason ? ` · ${escapeHtml(capacity.reason)}` : ""}</small>`
        : "<small>Capacity: no active throttle/depletion record</small>";
      return `<div class="comm-entry">
      <strong>${escapeHtml(item.display_name)} · ${escapeHtml(item.status)}</strong>
      <small>ID: ${escapeHtml(item.id)} · Adapter: ${escapeHtml(item.adapter_type)} · Base URL: ${escapeHtml(item.base_url || "provider default")}</small>
      <small>Credential reference: ${escapeHtml(item.credential_ref || "none")} · Credential required: ${item.credential_required ? "yes" : "no"}</small>
      <small>Residency: ${listText(item.residency_tags)} · Compliance: ${listText(item.compliance_tags)}</small>
      ${capacityLine}
      <small>Updated by: ${escapeHtml(item.updated_by)} · ${timeText(item.updated_at)}</small>
    </div>`;
    }).join("") || '<div class="comm-entry"><strong>No model providers registered.</strong></div>';
  }

  function renderModels(items) {
    const host = document.getElementById("model-definition-list");
    if (!host) return;
    host.innerHTML = items.map((item) => `<div class="comm-entry">
      <strong>${escapeHtml(item.id)} · ${escapeHtml(item.lifecycle)}</strong>
      <small>Provider: ${escapeHtml(item.provider_id)} · Concrete model: ${escapeHtml(item.concrete_model)}${item.model_version ? ` · version ${escapeHtml(item.model_version)}` : ""}</small>
      <small>Classes: ${listText(item.model_classes)} · Capabilities: ${listText(item.capabilities)} · Modalities: ${listText(item.modalities)} · Tools: ${item.supports_tools ? "yes" : "no"}</small>
      <small>Context: ${escapeHtml(item.context_window_tokens)} · Max output: ${escapeHtml(item.max_output_tokens)} · Latency: ${escapeHtml(item.latency_class)} · Route priority: ${escapeHtml(item.route_priority)}</small>
      <small>Pricing / 1M tokens: input ${item.input_price_per_million_usd == null ? "unknown" : `$${escapeHtml(item.input_price_per_million_usd)}`} · output ${item.output_price_per_million_usd == null ? "unknown" : `$${escapeHtml(item.output_price_per_million_usd)}`}</small>
      <small>Residency: ${listText(item.residency_tags)} · Compliance: ${listText(item.compliance_tags)} · Updated by: ${escapeHtml(item.updated_by)}</small>
    </div>`).join("") || '<div class="comm-entry"><strong>No model definitions registered.</strong></div>';
  }

  function renderPrompts(items) {
    const host = document.getElementById("model-prompt-list");
    if (!host) return;
    host.innerHTML = items.map((item) => `<details class="comm-entry">
      <summary><strong>${escapeHtml(item.template_id)} @ ${escapeHtml(item.version)} · ${item.active ? "active" : "inactive"}</strong></summary>
      <small>Checksum: ${escapeHtml(item.checksum_sha256)} · Updated by: ${escapeHtml(item.updated_by)} · ${timeText(item.updated_at)}</small>
      <pre>${escapeHtml(item.content)}</pre>
    </details>`).join("") || '<div class="comm-entry"><strong>No prompt templates registered.</strong></div>';
  }

  function renderInvocations(items) {
    const host = document.getElementById("model-invocation-list");
    if (!host) return;
    host.innerHTML = items.slice(0, MAX_INVOCATIONS).map((item) => {
      const attempts = (item.attempts || []).map((attempt, index) => `<small>
        Attempt ${index + 1}: ${escapeHtml(attempt.provider_id)}/${escapeHtml(attempt.model_id)} · ${escapeHtml(attempt.outcome)}
        ${attempt.error_code ? ` · ${escapeHtml(attempt.error_code)}` : ""}
        · input ${escapeHtml(attempt.input_tokens ?? "n/a")} · output ${escapeHtml(attempt.output_tokens ?? "n/a")}
        · cost ${attempt.actual_cost_usd == null ? "n/a" : `$${escapeHtml(attempt.actual_cost_usd)}`}
      </small>`).join("");
      const refs = [
        item.work_item_ref ? `work=${item.work_item_ref}` : null,
        item.goal_id ? `goal=${item.goal_id}` : null,
        item.decision_id ? `decision=${item.decision_id}` : null,
        item.execution_id ? `execution=${item.execution_id}` : null,
      ].filter(Boolean).join(" · ");
      return `<details class="comm-entry">
        <summary><strong>${escapeHtml(item.model_class)} · ${escapeHtml(item.status)} · ${escapeHtml(item.purpose)}</strong></summary>
        <small>ID: ${escapeHtml(item.id)} · ${timeText(item.created_at)} · actor ${escapeHtml(item.actor_id)}</small>
        <small>Selected: ${escapeHtml(item.selected_provider_id || "none")} / ${escapeHtml(item.selected_model_id || "none")} · ${escapeHtml(item.selected_concrete_model || "none")}${item.selected_model_version ? ` @ ${escapeHtml(item.selected_model_version)}` : ""}</small>
        <small>Template: ${escapeHtml(item.prompt_template_id)} @ ${escapeHtml(item.prompt_template_version)} · checksum ${escapeHtml(item.prompt_template_checksum_sha256)}</small>
        <small>Route reason: ${escapeHtml(item.route_reason)} · Policy fingerprint: ${escapeHtml(item.policy_fingerprint_sha256)}</small>
        <small>Capabilities: ${listText(item.required_capabilities)} · Residency: ${listText(item.required_residency_tags)} · Compliance: ${listText(item.required_compliance_tags)} · Max cost: ${item.max_cost_usd == null ? "none" : `$${escapeHtml(item.max_cost_usd)}`}</small>
        ${refs ? `<small>References: ${escapeHtml(refs)}</small>` : ""}
        ${attempts}
      </details>`;
    }).join("") || '<div class="comm-entry"><strong>No model invocation metadata.</strong><small>Prompt/message content is not stored in this feed.</small></div>';
  }

  function populatePreviewControls() {
    const classes = Array.from(new Set(
      snapshot.models.flatMap((item) => item.model_classes || []),
    )).sort();
    const datalist = document.getElementById("model-class-options");
    if (datalist) datalist.innerHTML = classes.map((value) => `<option value="${escapeHtml(value)}"></option>`).join("");
    const classInput = document.getElementById("model-route-class");
    if (classInput && !classInput.value && classes.length) classInput.value = classes[0];

    const template = document.getElementById("model-route-template");
    if (template) {
      template.innerHTML = snapshot.prompts.map((item, index) => (
        `<option value="${index}">${escapeHtml(item.template_id)} @ ${escapeHtml(item.version)} · ${item.active ? "active" : "inactive"}</option>`
      )).join("");
      const activeIndex = snapshot.prompts.findIndex((item) => item.active);
      if (activeIndex >= 0) template.value = String(activeIndex);
    }

    const providers = document.getElementById("model-route-preferred-providers");
    if (providers) {
      providers.innerHTML = snapshot.providers.map((item) => (
        `<option value="${escapeHtml(item.id)}">${escapeHtml(item.display_name)} · ${escapeHtml(item.status)} · ${escapeHtml(item.id)}</option>`
      )).join("");
    }
  }

  function renderRoutePreview(result) {
    const host = document.getElementById("model-route-preview-result");
    if (!host) return;
    host.innerHTML = `<div class="comm-entry">
      <strong>Deterministic route · ${escapeHtml(result.model_class)}</strong>
      <small>Template: ${escapeHtml(result.prompt_template_id)} @ ${escapeHtml(result.prompt_template_version)} · checksum ${escapeHtml(result.prompt_template_checksum_sha256)}</small>
      <small>Policy attempts: ${escapeHtml(result.policy_max_attempts)} · fingerprint ${escapeHtml(result.policy_fingerprint_sha256)}</small>
      <small>Effective residency: ${listText(result.effective_required_residency_tags)} · compliance: ${listText(result.effective_required_compliance_tags)} · max cost: ${result.effective_max_cost_usd == null ? "none" : `$${escapeHtml(result.effective_max_cost_usd)}`}</small>
      ${(result.candidates || []).map((candidate, index) => `<div class="comm-entry">
        <strong>#${index + 1} ${escapeHtml(candidate.provider_id)} / ${escapeHtml(candidate.model_id)}</strong>
        <small>${escapeHtml(candidate.concrete_model)}${candidate.model_version ? ` @ ${escapeHtml(candidate.model_version)}` : ""} · ${escapeHtml(candidate.routing_reason)}</small>
        <small>Estimated input: ${escapeHtml(candidate.estimated_input_tokens)} · max output: ${escapeHtml(candidate.max_output_tokens)} · upper cost: ${candidate.estimated_upper_cost_usd == null ? "unknown" : `$${escapeHtml(candidate.estimated_upper_cost_usd)}`}</small>
      </div>`).join("")}
    </div>`;
  }

  async function previewRoute() {
    const modelClass = document.getElementById("model-route-class")?.value.trim() || "";
    const selectedTemplateIndex = Number(document.getElementById("model-route-template")?.value ?? -1);
    const selectedTemplate = snapshot.prompts[selectedTemplateIndex] || null;
    if (!modelClass || !selectedTemplate) {
      setStatus("Route preview requires a model class and prompt template.");
      return;
    }
    const maxCostValue = document.getElementById("model-route-max-cost")?.value || "";
    const preferredProviders = Array.from(
      document.getElementById("model-route-preferred-providers")?.selectedOptions || [],
    ).map((item) => item.value);
    try {
      const result = await apiRequest("/api/model-gateway/route", {
        method: "POST",
        body: JSON.stringify({
          model_class: modelClass,
          messages: [{ role: "user", content: "deterministic route preview" }],
          system_prompt: "",
          prompt_template_id: selectedTemplate.template_id,
          prompt_template_version: selectedTemplate.version,
          required_capabilities: csv("model-route-capabilities"),
          required_residency_tags: csv("model-route-residency"),
          required_compliance_tags: csv("model-route-compliance"),
          preferred_provider_ids: preferredProviders,
          max_output_tokens: Number(document.getElementById("model-route-max-output")?.value || 2048),
          max_cost_usd: maxCostValue ? Number(maxCostValue) : null,
          allow_fallback: Boolean(document.getElementById("model-route-fallback")?.checked),
          purpose: "ui.route-preview",
        }),
      });
      renderRoutePreview(result);
      setStatus("Route preview completed deterministically; no model/provider invocation occurred.");
    } catch (error) {
      setStatus(`Route preview failed: ${error.message}`);
      const host = document.getElementById("model-route-preview-result");
      if (host) host.innerHTML = "";
    }
  }

  async function refresh() {
    setStatus("Loading canonical model-gateway state...");
    try {
      const [providers, models, prompts, policy, invocations, capacity] = await Promise.all([
        apiRequest("/api/model-gateway/providers"),
        apiRequest("/api/model-gateway/models"),
        apiRequest("/api/model-gateway/prompts"),
        apiRequest("/api/model-gateway/policy"),
        apiRequest(`/api/model-gateway/invocations?limit=${MAX_INVOCATIONS}`),
        apiRequest("/api/provider-capacity"),
      ]);
      snapshot = {
        providers: providers.items || [],
        models: models.items || [],
        prompts: prompts.items || [],
        policy: policy.item,
        invocations: invocations.items || [],
        capacity: capacity.items || [],
        capacityWaits: capacity.waits || [],
      };
      renderPolicy(snapshot.policy);
      renderProviders(snapshot.providers);
      renderModels(snapshot.models);
      renderPrompts(snapshot.prompts);
      renderInvocations(snapshot.invocations);
      populatePreviewControls();
      window.dispatchEvent(new CustomEvent("codex:model-gateway-rendered", {
        detail: snapshot,
      }));
      const waiting = snapshot.capacityWaits.filter((item) => item.status === "waiting").length;
      setStatus(`${snapshot.providers.length} provider(s) · ${snapshot.models.length} model(s) · ${snapshot.prompts.length} prompt version(s) · ${snapshot.invocations.length} invocation record(s) · ${waiting} capacity wait(s). Credentials remain secret references.`);
    } catch (error) {
      setStatus(`Model Gateway unavailable: ${error.message}`);
    }
  }

  function bind() {
    const panel = document.getElementById("developer-panel");
    document.getElementById("refresh-model-gateway")?.addEventListener("click", refresh);
    document.getElementById("refresh-developer")?.addEventListener("click", refresh);
    document.getElementById("preview-model-route")?.addEventListener("click", () => previewRoute().catch(console.error));
    panel?.addEventListener("toggle", () => {
      if (panel.open) refresh().catch(console.error);
    });
    if (panel?.open) refresh().catch(console.error);
  }

  window.addEventListener("DOMContentLoaded", bind);
})();
