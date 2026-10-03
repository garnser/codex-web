(async () => {
  let latest = null, ready = false;
  window.addEventListener("codex:model-gateway-rendered", event => {
    latest = event.detail || {};
    if (ready) hydrate(latest).catch(console.error);
  });
  const BASE = window.location.pathname.startsWith("/codex") ? "/codex" : "";
  const { confirmAction } = await import(`${BASE}/static/action_confirmation.js`);
  const { request: apiRequest } = await import(`${BASE}/static/api_client.js`);
  const edits = await import(`${BASE}/static/model_gateway_editor_state.js`);
  const { bindWhenReady } = await import(`${BASE}/static/reference_links.js`);
  let snapshot = { providers: [], models: [], prompts: [], policy: null };
  let actor = null;
  let secrets = [];
  let secretError = null;

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");
  }

  function csvValue(values) {
    return (values || []).join(", ");
  }

  function csv(id) {
    return (document.getElementById(id)?.value || "")
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean);
  }

  function numberOrNull(id) {
    const value = document.getElementById(id)?.value ?? "";
    return value === "" ? null : Number(value);
  }

  function setStatus(message) {
    const element = document.getElementById("model-gateway-management-status");
    if (element) {
      element.hidden = false;
      element.textContent = message;
    }
  }

  function refreshGateway() {
    document.getElementById("refresh-model-gateway")?.click();
  }

  function setSelectValues(select, values) {
    const selected = new Set(values || []);
    Array.from(select?.options || []).forEach((option) => {
      option.selected = selected.has(option.value);
    });
  }

  function optionList(items, label) {
    return items.map((item) => (
      `<option value="${escapeHtml(item.id)}">${escapeHtml(label(item))} · ${escapeHtml(item.id)}</option>`
    )).join("");
  }

  function populateCredentialRefs(current = "") {
    const select = document.getElementById("model-provider-credential");
    if (!select) return;
    const rows = secrets.filter((item) => item.status === "active" || item.id === current);
    select.innerHTML = '<option value="">No credential reference</option>' + rows.map((item) => (
      `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)} · ${escapeHtml(item.provider || "generic")} · ${escapeHtml(item.status)} · ${escapeHtml(item.id)}</option>`
    )).join("");
    if (current && !rows.some((item) => item.id === current)) {
      select.insertAdjacentHTML(
        "beforeend",
        `<option value="${escapeHtml(current)}">Current reference · ${escapeHtml(current)}</option>`,
      );
    }
    select.value = current || "";
    select.disabled = Boolean(secretError && !current);
  }

  function populateProviderControls() {
    const existing = document.getElementById("model-provider-existing");
    if (existing) {
      const previous = existing.value;
      existing.innerHTML = '<option value="">New provider</option>'
        + optionList(snapshot.providers, (item) => item.display_name);
      if (snapshot.providers.some((item) => item.id === previous)) existing.value = previous;
    }
    const modelProvider = document.getElementById("model-definition-provider");
    if (modelProvider) {
      const previous = modelProvider.value;
      modelProvider.innerHTML = optionList(snapshot.providers, (item) => item.display_name);
      if (snapshot.providers.some((item) => item.id === previous)) modelProvider.value = previous;
    }
    const policy = document.getElementById("model-policy-providers");
    if (policy) {
      policy.innerHTML = optionList(snapshot.providers, (item) => item.display_name);
      setSelectValues(policy, snapshot.policy?.allowed_provider_ids);
    }
    const preferredCurrent = document.getElementById("model-provider-existing")?.value || "";
    const selected = snapshot.providers.find((item) => item.id === preferredCurrent);
    populateCredentialRefs(selected?.credential_ref || "");
  }

  function populateModelControls() {
    const existing = document.getElementById("model-definition-existing");
    if (existing) {
      const previous = existing.value;
      existing.innerHTML = '<option value="">New model</option>'
        + optionList(snapshot.models, (item) => `${item.id} · ${item.concrete_model}`);
      if (snapshot.models.some((item) => item.id === previous)) existing.value = previous;
    }
    const policy = document.getElementById("model-policy-models");
    if (policy) {
      policy.innerHTML = optionList(snapshot.models, (item) => `${item.id} · ${item.concrete_model}`);
      setSelectValues(policy, snapshot.policy?.allowed_model_ids);
    }
  }

  function populatePromptSources() {
    const source = document.getElementById("model-prompt-source");
    if (!source) return;
    source.innerHTML = '<option value="">Start blank</option>' + snapshot.prompts.map((item, index) => (
      `<option value="${index}">${escapeHtml(item.template_id)} @ ${escapeHtml(item.version)} · ${item.active ? "active" : "inactive"}</option>`
    )).join("");
  }

  function populatePolicy() {
    const policy = snapshot.policy || {};
    document.getElementById("model-policy-residency").value = csvValue(policy.required_residency_tags);
    document.getElementById("model-policy-compliance").value = csvValue(policy.required_compliance_tags);
    document.getElementById("model-policy-max-cost").value = policy.max_invocation_cost_usd ?? "";
    document.getElementById("model-policy-max-attempts").value = policy.max_attempts ?? 2;
    setSelectValues(document.getElementById("model-policy-providers"), policy.allowed_provider_ids);
    setSelectValues(document.getElementById("model-policy-models"), policy.allowed_model_ids);
  }

  function loadProvider(id) {
    const item = snapshot.providers.find((value) => value.id === id);
    if (!item) {
      document.getElementById("model-provider-id").value = "";
      document.getElementById("model-provider-name").value = "";
      document.getElementById("model-provider-adapter").value = "";
      document.getElementById("model-provider-base-url").value = "";
      document.getElementById("model-provider-residency").value = "";
      document.getElementById("model-provider-compliance").value = "";
      document.getElementById("model-provider-status").value = "active";
      document.getElementById("model-provider-credential-required").checked = true;
      populateCredentialRefs("");
      return;
    }
    document.getElementById("model-provider-id").value = item.id;
    document.getElementById("model-provider-name").value = item.display_name;
    document.getElementById("model-provider-adapter").value = item.adapter_type;
    document.getElementById("model-provider-base-url").value = item.base_url || "";
    document.getElementById("model-provider-residency").value = csvValue(item.residency_tags);
    document.getElementById("model-provider-compliance").value = csvValue(item.compliance_tags);
    document.getElementById("model-provider-status").value = item.status;
    document.getElementById("model-provider-credential-required").checked = Boolean(item.credential_required);
    populateCredentialRefs(item.credential_ref || "");
  }

  function loadModel(id) {
    const item = snapshot.models.find((value) => value.id === id);
    if (!item) {
      for (const field of ["model-definition-id", "model-definition-concrete", "model-definition-version", "model-definition-classes", "model-definition-workloads", "model-definition-residency", "model-definition-compliance"]) {
        document.getElementById(field).value = "";
      }
      document.getElementById("model-definition-capabilities").value = "text";
      document.getElementById("model-definition-modalities").value = "text";
      document.getElementById("model-definition-tools").checked = false;
      document.getElementById("model-definition-context").value = 128000;
      document.getElementById("model-definition-output").value = 8192;
      document.getElementById("model-definition-latency").value = "standard";
      document.getElementById("model-definition-input-price").value = "";
      document.getElementById("model-definition-output-price").value = "";
      document.getElementById("model-definition-priority").value = 100;
      document.getElementById("model-definition-lifecycle").value = "active";
      return;
    }
    document.getElementById("model-definition-id").value = item.id;
    document.getElementById("model-definition-provider").value = item.provider_id;
    document.getElementById("model-definition-concrete").value = item.concrete_model;
    document.getElementById("model-definition-version").value = item.model_version || "";
    document.getElementById("model-definition-classes").value = csvValue(item.model_classes);
    document.getElementById("model-definition-workloads").value = csvValue(item.workload_classes);
    document.getElementById("model-definition-capabilities").value = csvValue(item.capabilities);
    document.getElementById("model-definition-modalities").value = csvValue(item.modalities);
    document.getElementById("model-definition-tools").checked = Boolean(item.supports_tools);
    document.getElementById("model-definition-context").value = item.context_window_tokens;
    document.getElementById("model-definition-output").value = item.max_output_tokens;
    document.getElementById("model-definition-latency").value = item.latency_class;
    document.getElementById("model-definition-input-price").value = item.input_price_per_million_usd ?? "";
    document.getElementById("model-definition-output-price").value = item.output_price_per_million_usd ?? "";
    document.getElementById("model-definition-residency").value = csvValue(item.residency_tags);
    document.getElementById("model-definition-compliance").value = csvValue(item.compliance_tags);
    document.getElementById("model-definition-priority").value = item.route_priority;
    document.getElementById("model-definition-lifecycle").value = item.lifecycle;
  }

  function loadPrompt(indexValue) {
    if (indexValue === "") {
      document.getElementById("model-prompt-id").value = "";
      document.getElementById("model-prompt-version").value = "";
      document.getElementById("model-prompt-content").value = "";
      document.getElementById("model-prompt-active").checked = true;
      return;
    }
    const item = snapshot.prompts[Number(indexValue)];
    if (!item) return;
    document.getElementById("model-prompt-id").value = item.template_id;
    document.getElementById("model-prompt-version").value = item.version;
    document.getElementById("model-prompt-content").value = item.content;
    document.getElementById("model-prompt-active").checked = Boolean(item.active);
    setStatus("Loaded an existing prompt version. Its content is immutable; change the version before changing content.");
  }

  let hydration = 0;
  async function hydrate(detail) {
    const visit = ++hydration, view = edits.modelView();
    if (edits.modelEditsPending()) return setStatus('Catalog display update deferred. Save or discard unsaved changes, then refresh.');
    actor = null;
    secrets = [];
    secretError = null;
    let actorError = null;
    await Promise.all([
      apiRequest("/api/identity/me")
        .then((value) => { actor = value; })
        .catch((error) => { actorError = error.message; }),
      apiRequest("/api/secrets")
        .then((value) => { secrets = value.items || []; })
        .catch((error) => { secretError = error.message; }),
    ]);
    if (visit !== hydration || !view.current()) return;
    if (edits.modelEditsPending()) return setStatus('Pending edits kept; refresh after saving or discarding.');
    snapshot = detail;
    const assurance = document.getElementById("model-gateway-management-assurance");
    if (assurance) {
      assurance.textContent = `Sensitive routing administration requires canonical admin authority and MFA/step-up assurance. Current assurance: ${actor?.assurance || "unknown"}.${actorError ? ` Identity metadata unavailable: ${actorError}.` : ""}${secretError ? ` Secret metadata unavailable: ${secretError}.` : ""}`;
    }
    populateProviderControls();
    populateModelControls();
    populatePromptSources();
    populatePolicy();
    edits.resetModelEditors();
  }

  async function saveProvider() {
    const submitted = edits.modelSubmission("provider");
    const id = document.getElementById("model-provider-id")?.value.trim() || "";
    const displayName = document.getElementById("model-provider-name")?.value.trim() || "";
    const adapterType = document.getElementById("model-provider-adapter")?.value.trim() || "";
    if (!id || !displayName || !adapterType) return setStatus("Provider ID, display name and adapter type are required.");
    const payload = {
      id,
      adapter_type: adapterType,
      display_name: displayName,
      base_url: document.getElementById("model-provider-base-url")?.value.trim() || null,
      credential_ref: document.getElementById("model-provider-credential")?.value || null,
      credential_required: Boolean(document.getElementById("model-provider-credential-required")?.checked),
      residency_tags: csv("model-provider-residency"),
      compliance_tags: csv("model-provider-compliance"),
      status: document.getElementById("model-provider-status")?.value || "active",
    };
    if (!await confirmAction({ action: 'Save model provider', target: id, risk: 'high', consequence: `Save provider ${id} as ${payload.status}? Adapter: ${adapterType}; credential reference: ${payload.credential_ref || "none"}; residency: ${payload.residency_tags.join(", ") || "none"}. This can change where model data is routed.`, recovery: 'A later authorized configuration change can restore prior settings; sent model data cannot be recalled.', current: submitted.current })) return;
    try {
      await apiRequest(`/api/model-gateway/providers/${encodeURIComponent(id)}`, { method: "PUT", body: JSON.stringify(payload) });
      if (!submitted.current()) return;
      submitted.saved();
      setStatus(`Saved provider ${id}.`);
      refreshGateway();
    } catch (error) {
      if (submitted.current()) setStatus(`Provider update failed: ${error.message}`);
    }
  }

  async function saveModel() {
    const submitted = edits.modelSubmission("model");
    const id = document.getElementById("model-definition-id")?.value.trim() || "";
    const providerId = document.getElementById("model-definition-provider")?.value || "";
    const concreteModel = document.getElementById("model-definition-concrete")?.value.trim() || "";
    const classes = csv("model-definition-classes");
    if (!id || !providerId || !concreteModel || !classes.length) return setStatus("Model ID, provider, concrete model and at least one model class are required.");
    const payload = {
      id,
      provider_id: providerId,
      concrete_model: concreteModel,
      model_version: document.getElementById("model-definition-version")?.value.trim() || null,
      model_classes: classes,
      workload_classes: csv("model-definition-workloads"),
      capabilities: csv("model-definition-capabilities"),
      modalities: csv("model-definition-modalities"),
      supports_tools: Boolean(document.getElementById("model-definition-tools")?.checked),
      context_window_tokens: Number(document.getElementById("model-definition-context")?.value || 128000),
      max_output_tokens: Number(document.getElementById("model-definition-output")?.value || 8192),
      latency_class: document.getElementById("model-definition-latency")?.value || "standard",
      input_price_per_million_usd: numberOrNull("model-definition-input-price"),
      output_price_per_million_usd: numberOrNull("model-definition-output-price"),
      residency_tags: csv("model-definition-residency"),
      compliance_tags: csv("model-definition-compliance"),
      route_priority: Number(document.getElementById("model-definition-priority")?.value || 100),
      lifecycle: document.getElementById("model-definition-lifecycle")?.value || "active",
    };
    if (!await confirmAction({ action: 'Save model definition', target: id, risk: 'high', consequence: `Save model definition ${id}? Provider: ${providerId}; classes: ${classes.join(", ")}; lifecycle: ${payload.lifecycle}; priority: ${payload.route_priority}. This changes deterministic routing eligibility.`, recovery: 'A later authorized change can restore eligibility; prior invocations retain their attributed model.', current: submitted.current })) return;
    try {
      await apiRequest(`/api/model-gateway/models/${encodeURIComponent(id)}`, { method: "PUT", body: JSON.stringify(payload) });
      if (!submitted.current()) return;
      submitted.saved();
      setStatus(`Saved model definition ${id}.`);
      refreshGateway();
    } catch (error) {
      if (submitted.current()) setStatus(`Model definition update failed: ${error.message}`);
    }
  }

  async function savePrompt() {
    const submitted = edits.modelSubmission("prompt");
    const templateId = document.getElementById("model-prompt-id")?.value.trim() || "";
    const version = document.getElementById("model-prompt-version")?.value.trim() || "";
    const content = document.getElementById("model-prompt-content")?.value || "";
    if (!templateId || !version || !content) return setStatus("Template ID, version and content are required.");
    if (!await confirmAction({ action: 'Publish prompt template', target: `${templateId}@${version}`, risk: 'bounded', consequence: `Publish prompt template ${templateId}@${version}? Existing version content is immutable; changing content for an existing version will be rejected. Exact checksum/version is attributed to future invocations.`, recovery: 'Published content is immutable; corrections require a new version.', current: submitted.current })) return;
    try {
      await apiRequest(
        `/api/model-gateway/prompts/${encodeURIComponent(templateId)}/${encodeURIComponent(version)}`,
        {
          method: "PUT",
          body: JSON.stringify({
            template_id: templateId,
            version,
            content,
            active: Boolean(document.getElementById("model-prompt-active")?.checked),
          }),
        },
      );
      if (!submitted.current()) return;
      submitted.saved();
      setStatus(`Published prompt template ${templateId}@${version}.`);
      refreshGateway();
    } catch (error) {
      if (submitted.current()) setStatus(`Prompt template update failed: ${error.message}`);
    }
  }

  async function savePolicy() {
    const submitted = edits.modelSubmission("policy");
    const allowedProviderIds = Array.from(document.getElementById("model-policy-providers")?.selectedOptions || []).map((item) => item.value);
    const allowedModelIds = Array.from(document.getElementById("model-policy-models")?.selectedOptions || []).map((item) => item.value);
    const payload = {
      allowed_provider_ids: allowedProviderIds,
      allowed_model_ids: allowedModelIds,
      required_residency_tags: csv("model-policy-residency"),
      required_compliance_tags: csv("model-policy-compliance"),
      max_invocation_cost_usd: numberOrNull("model-policy-max-cost"),
      max_attempts: Number(document.getElementById("model-policy-max-attempts")?.value || 2),
    };
    const providersText = allowedProviderIds.length ? allowedProviderIds.join(", ") : "all registered providers";
    const modelsText = allowedModelIds.length ? allowedModelIds.join(", ") : "all registered models";
    if (!await confirmAction({ action: 'Replace model routing policy', target: "current tenant routing policy", risk: 'high', consequence: `Replace tenant routing policy? Providers: ${providersText}; models: ${modelsText}; residency: ${payload.required_residency_tags.join(", ") || "none"}; compliance: ${payload.required_compliance_tags.join(", ") || "none"}; max attempts: ${payload.max_attempts}. Empty allowlists mean unrestricted within the registered tenant catalog.`, recovery: 'Restore prior settings through a new authorized update; dispatched invocations are not undone.', current: submitted.current })) return;
    try {
      await apiRequest("/api/model-gateway/policy", { method: "PUT", body: JSON.stringify(payload) });
      if (!submitted.current()) return;
      submitted.saved();
      setStatus("Tenant routing policy saved.");
      refreshGateway();
    } catch (error) {
      if (submitted.current()) setStatus(`Routing policy update failed: ${error.message}`);
    }
  }

  function bind() {
    edits.setupModelEditors();
    document.getElementById("model-provider-existing")?.addEventListener("change", (event) => edits.selectModelSource("provider", event.target.value, loadProvider));
    document.getElementById("model-definition-existing")?.addEventListener("change", (event) => edits.selectModelSource("model", event.target.value, loadModel));
    document.getElementById("model-prompt-source")?.addEventListener("change", (event) => edits.selectModelSource("prompt", event.target.value, loadPrompt));
    document.getElementById("save-model-provider")?.addEventListener("click", () => saveProvider().catch(console.error));
    document.getElementById("save-model-definition")?.addEventListener("click", () => saveModel().catch(console.error));
    document.getElementById("save-model-prompt")?.addEventListener("click", () => savePrompt().catch(console.error));
    document.getElementById("save-model-policy")?.addEventListener("click", () => savePolicy().catch(console.error));
  }

  bindWhenReady(() => { bind(); ready = true; if (latest) hydrate(latest).catch(console.error); });
})();
