(async () => {
  const BASE = window.location.pathname.startsWith("/codex") ? "/codex" : "";
  const { referenceLink } = await import(`${BASE}/static/reference_navigation.js`);
  const { request: apiRequest } = await import(`${BASE}/static/api_client.js`);
  const { esc: escapeHtml, secretLinks, referenceAttributes, focusReference, bindWhenReady } = await import(`${BASE}/static/reference_links.js`);
  const { populateTaskRouteControls, readTaskRoutePreferences } = await import(
    `${BASE}/static/model_gateway_route_controls.js`
  );
  const { providerCards } = await import(`${BASE}/static/model_provider_cards.js`);
  const { renderModelCatalogs, renderModelDefinitions, bindModelCatalogRefresh } = await import(`${BASE}/static/model_catalog_ui.js`);
  const { mergeProviderEligibility, routeEligibilityHtml, candidateExecutionPathHtml } = await import(`${BASE}/static/model_gateway_eligibility_ui.js`);
  const { renderModelInvocations } = await import(`${BASE}/static/model_gateway_invocation_ui.js`);
  const MAX_INVOCATIONS = 50;
  let snapshot = { providers: [], providerEligibility: [], models: [], catalogs: [], prompts: [], policy: null, invocations: [], capacity: [], capacityWaits: [] };


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
    const host = document.getElementById("model-provider-list"); if (!host) return;
    host.innerHTML = providerCards(items, snapshot.capacity, timeText, listText); focusReference(host);
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

  function populatePreviewControls() {
    const classes = Array.from(new Set(
      snapshot.models.flatMap((item) => item.model_classes || []),
    )).sort();
    const datalist = document.getElementById("model-class-options");
    if (datalist) datalist.innerHTML = classes.map((value) => `<option value="${escapeHtml(value)}"></option>`).join("");
    const classInput = document.getElementById("model-route-class");
    if (classInput && !classInput.value && classes.length) classInput.value = classes[0];

    populateTaskRouteControls(snapshot.models);

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
      <strong>Deterministic route · ${escapeHtml(result.model_class)} · ${escapeHtml(result.workload_class || "unspecified workload")}</strong>
      <small>Template: ${escapeHtml(result.prompt_template_id)} @ ${escapeHtml(result.prompt_template_version)} · checksum ${escapeHtml(result.prompt_template_checksum_sha256)}</small>
      <small>Policy attempts: ${escapeHtml(result.policy_max_attempts)} · fingerprint ${escapeHtml(result.policy_fingerprint_sha256)}</small>
      <small>Routing definition: ${escapeHtml(result.routing_definition_id || "legacy class routing")} @ ${escapeHtml(result.routing_definition_revision ?? "n/a")} · role ${escapeHtml(result.routing_role || "primary")} · qualification ${escapeHtml(result.qualification_revision || "not mapped")}</small>
      ${routeEligibilityHtml(result, escapeHtml)}
      <small>Effective residency: ${listText(result.effective_required_residency_tags)} · compliance: ${listText(result.effective_required_compliance_tags)} · max cost: ${result.effective_max_cost_usd == null ? "none" : `$${escapeHtml(result.effective_max_cost_usd)}`}</small>
      <small>Override: ${escapeHtml(result.pinned_model_id || "automatic")} · Preferred latency: ${listText(result.preferred_latency_classes)} · Prefer lower cost: ${result.prefer_lower_cost ? "yes" : "no"}</small>
      ${(result.candidates || []).map((candidate, index) => `<div class="comm-entry">
        <strong>#${index + 1} ${escapeHtml(candidate.provider_id)} / ${escapeHtml(candidate.model_id)}</strong>
        ${candidateExecutionPathHtml(candidate, escapeHtml)}
        <small>${escapeHtml(candidate.concrete_model)}${candidate.model_version ? ` @ ${escapeHtml(candidate.model_version)}` : ""} · ${escapeHtml(candidate.routing_reason)}</small>
        <small>Qualification revision: ${escapeHtml(candidate.qualification_revision_id || "not mapped")}</small>
        <small>Catalog: ${escapeHtml(candidate.catalog_revision || "static")} · upstream ${escapeHtml(candidate.upstream_provider_id || "unknown")} / ${escapeHtml(candidate.upstream_model_id || "unknown")}</small>
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
          ...readTaskRoutePreferences(),
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
          routing_role: document.getElementById("model-route-role")?.value || "primary",
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
      const [providers, providerEligibility, models, catalogs, prompts, policy, invocations, capacity] = await Promise.all([
        apiRequest("/api/model-gateway/providers"),
        apiRequest("/api/model-gateway/provider-eligibility"),
        apiRequest("/api/model-gateway/models"),
        apiRequest("/api/model-gateway/catalogs"),
        apiRequest("/api/model-gateway/prompts"),
        apiRequest("/api/model-gateway/policy"),
        apiRequest(`/api/model-gateway/invocations?limit=${MAX_INVOCATIONS}`),
        apiRequest("/api/provider-capacity"),
      ]);
      snapshot = {
        providers: mergeProviderEligibility(providers.items || [], providerEligibility.items || []),
        providerEligibility: providerEligibility.items || [],
        models: models.items || [],
        catalogs: catalogs.items || [],
        prompts: prompts.items || [],
        policy: policy.item,
        invocations: invocations.items || [],
        capacity: capacity.items || [],
        capacityWaits: capacity.waits || [],
      };
      renderPolicy(snapshot.policy);
      renderProviders(snapshot.providers);
      renderModelCatalogs({ host: document.getElementById("model-catalog-list"), providers: snapshot.providers, catalogs: snapshot.catalogs, escapeHtml, timeText, listText });
      renderModelDefinitions({ host: document.getElementById("model-definition-list"), items: snapshot.models, escapeHtml, listText, referenceLink, referenceAttributes, focusReference });
      renderPrompts(snapshot.prompts);
      renderModelInvocations(snapshot.invocations, { escapeHtml, timeText, listText, referenceLink, referenceAttributes, focusReference, limit: MAX_INVOCATIONS });
      populatePreviewControls();
      window.dispatchEvent(new CustomEvent("codex:model-gateway-rendered", {
        detail: snapshot,
      }));
      const waiting = snapshot.capacityWaits.filter((item) => item.status === "waiting").length;
      setStatus(`${snapshot.providers.length} provider(s) · ${snapshot.models.length} model(s) · ${snapshot.catalogs.length} catalog state(s) · ${snapshot.prompts.length} prompt version(s) · ${snapshot.invocations.length} invocation record(s) · ${waiting} capacity wait(s). Credentials remain secret references.`);
    } catch (error) {
      setStatus(`Model Gateway unavailable: ${error.message}`);
    }
  }

  function bind() {
    const panel = document.getElementById("developer-panel");
    document.getElementById("refresh-model-gateway")?.addEventListener("click", refresh);
    document.getElementById("refresh-developer")?.addEventListener("click", refresh);
    document.getElementById("preview-model-route")?.addEventListener("click", () => previewRoute().catch(console.error));
    document.getElementById("model-route-preferred-providers")?.addEventListener("change", event => {
      populateTaskRouteControls(snapshot.models, Array.from(event.currentTarget.selectedOptions).map(item => item.value));
    });
    bindModelCatalogRefresh({ host: document.getElementById("model-catalog-list"), api: apiRequest, setStatus, refresh });
    panel?.addEventListener("toggle", () => {
      if (panel.open) refresh().catch(console.error);
    });
    if (panel?.open) refresh().catch(console.error);
  }

  bindWhenReady(bind);
})();
