(async () => {
  let latest = null;
  window.addEventListener("codex:model-gateway-rendered", event => {
    latest = event.detail || {};
  });
  const BASE = window.location.pathname.startsWith("/codex") ? "/codex" : "";
  const { confirmAction } = await import(`${BASE}/static/action_confirmation.js`);
  const { request: apiRequest } = await import(`${BASE}/static/api_client.js`);
  const { bindWhenReady, esc } = await import(`${BASE}/static/reference_links.js`);
  let snapshot = { models: [], baseline: null, baselineDefinition: null, qualificationProfiles: [], qualifications: [], routingDefinitions: [] };

  const csv = (id) => (document.getElementById(id)?.value || "")
    .split(",").map(value => value.trim()).filter(Boolean);
  const value = (id) => document.getElementById(id)?.value.trim() || "";
  const status = (message) => {
    const host = document.getElementById("model-gateway-management-status");
    if (host) { host.hidden = false; host.textContent = message; }
  };
  const refresh = () => document.getElementById("refresh-model-gateway")?.click();
  const time = (seconds) => seconds ? new Date(Number(seconds) * 1000).toLocaleString() : "unknown";

  function render() {
    const host = document.getElementById("model-qualification-list");
    if (!host) return;
    const profiles = snapshot.qualificationProfiles || [];
    const qualifications = snapshot.qualifications || [];
    const mappings = snapshot.routingDefinitions || [];
    host.innerHTML = [
      ...(snapshot.baseline ? [`<details class="comm-entry"><summary><strong>Initial replaceable baseline · ${esc(snapshot.baseline.evaluation_revision)}</strong></summary><small>Evaluated ${time(snapshot.baseline.evaluated_at)} · Definition Registry record ${esc(snapshot.baselineDefinition?.record_id || "unknown")} · ${snapshot.baseline.replaceable ? "replaceable through qualified mapping revisions" : "fixed"}</small>${(snapshot.baseline.entries || []).map(item => `<small>${esc(item.label)} · ${item.deterministic ? "deterministic / no model" : `${esc(item.workload_class)} · primary ${esc((item.primary_models || []).join(" → ") || "none")} · escalation ${esc((item.escalation_models || []).join(" → ") || "none")} · critic ${esc((item.critic_models || []).join(" / ") || "none")}`}</small>`).join("")}</details>`] : []),
      ...profiles.map(item => `<div class="comm-entry"><strong>Profile · ${esc(item.workload_class)} @ ${esc(item.revision)}</strong><small>Quality ≥ ${esc(item.minimum_quality_score)} · cost/success ${item.maximum_cost_per_successful_outcome_usd == null ? "unbounded" : `$${esc(item.maximum_cost_per_successful_outcome_usd)}`} · suites ${esc((item.required_suite_ids || []).join(", ") || "none")}</small><small>Provider cost required: ${item.require_provider_reported_cost ? "yes" : "no"} · canary required: ${item.require_canary ? "yes" : "no"} · metrics ${esc((item.metrics || []).join(", ") || "none")} · published by ${esc(item.created_by)} · ${time(item.created_at)}</small></div>`),
      ...qualifications.map(item => `<div class="comm-entry"><strong>${esc(item.model_id)} · ${esc(item.workload_class)} · ${esc(item.status)} @ ${esc(item.revision)}</strong><small>Provider ${esc(item.provider_id)} · model version ${esc(item.model_version || "unspecified")} · profile ${esc(item.evaluation_profile_revision)}</small><small>Evaluation runs: ${esc((item.evaluation_run_ids || []).join(", ") || "none")} · quality ${esc(item.quality_score ?? "from evidence")} · cost/success ${esc(item.cost_per_successful_outcome_usd ?? "from evidence")}</small><small>${esc(item.reason)} · immutable ID ${esc(item.id)} · ${time(item.created_at)}</small></div>`),
      ...mappings.map(item => `<div class="comm-entry"><strong>${esc(item.mapping_id)} @ ${esc(item.revision)} · ${item.active ? "active" : "historical"}</strong><small>Function ${esc(item.function_id)} · workload ${esc(item.workload_class)} · evaluated ${time(item.evaluated_at)} · qualification ${esc(item.qualification_revision)}</small><small>Primary: ${esc((item.primary_model_ids || []).join(", "))} · escalation: ${esc((item.escalation_model_ids || []).join(", ") || "none")} · critic: ${esc((item.critic_model_ids || []).join(", ") || "none")}</small><small>Capabilities ${esc((item.required_capabilities || []).join(", ") || "none")} · max cost ${item.max_cost_per_invocation_usd == null ? "none" : `$${esc(item.max_cost_per_invocation_usd)}`} · fallback ${item.allow_fallback ? "bounded" : "disabled"} · source ${esc(item.source)}</small>${item.active && item.revision > 1 ? `<button type="button" class="ghost-button" data-routing-rollback="${esc(item.mapping_id)}" data-routing-target="${esc(item.revision - 1)}">Roll back to revision ${esc(item.revision - 1)}</button>` : ""}</div>`),
    ].join("") || '<div class="comm-entry"><strong>No workload qualification state.</strong><small>Discovered models remain ineligible for governed mappings until replay evidence is recorded.</small></div>';
    host.querySelectorAll("[data-routing-rollback]").forEach(button => button.addEventListener("click", () => rollback(button)));
  }

  function populate() {
    const model = document.getElementById("model-qualification-model");
    if (model) model.innerHTML = (snapshot.models || []).map(item => `<option value="${esc(item.id)}">${esc(item.id)} · ${esc(item.provider_id)}</option>`).join("");
    const workloads = Array.from(new Set((snapshot.qualificationProfiles || []).map(item => item.workload_class)));
    for (const id of ["model-qualification-workload", "model-mapping-workload"]) {
      const field = document.getElementById(id);
      if (field) field.innerHTML = workloads.map(item => `<option value="${esc(item)}">${esc(item)}</option>`).join("");
    }
  }

  async function load(detail) {
    snapshot.models = detail.models || [];
    try {
      const [baseline, profiles, qualifications, mappings] = await Promise.all([
        apiRequest("/api/model-gateway/routing-baseline"),
        apiRequest("/api/model-gateway/qualification-profiles"),
        apiRequest("/api/model-gateway/qualifications"),
        apiRequest("/api/model-gateway/routing-definitions"),
      ]);
      snapshot.baseline = baseline.item || null;
      snapshot.baselineDefinition = baseline.definition || null;
      snapshot.qualificationProfiles = profiles.items || [];
      snapshot.qualifications = qualifications.items || [];
      snapshot.routingDefinitions = mappings.items || [];
      populate(); render();
    } catch (error) { status(`Qualification state unavailable: ${error.message}`); }
  }

  async function publishProfile() {
    const payload = {
      workload_class: value("qualification-profile-workload"),
      metrics: csv("qualification-profile-metrics"),
      minimum_quality_score: Number(value("qualification-profile-quality") || 0),
      maximum_cost_per_successful_outcome_usd: value("qualification-profile-cost") ? Number(value("qualification-profile-cost")) : null,
      required_suite_ids: csv("qualification-profile-suites"),
      require_provider_reported_cost: Boolean(document.getElementById("qualification-profile-provider-cost")?.checked),
      require_canary: Boolean(document.getElementById("qualification-profile-canary")?.checked),
    };
    if (!await confirmAction({ action: "Publish workload evaluation profile", target: payload.workload_class, risk: "bounded", consequence: `Publish a new immutable evaluation profile revision for ${payload.workload_class}?`, recovery: "Publish a later profile revision.", current: () => true })) return;
    try {
      await apiRequest("/api/model-gateway/qualification-profiles", { method: "POST", body: JSON.stringify(payload) });
      status(`Published evaluation profile for ${payload.workload_class}.`); refresh();
    } catch (error) { status(`Evaluation profile failed: ${error.message}`); }
  }

  async function recordQualification() {
    const payload = {
      model_id: value("model-qualification-model"), workload_class: value("model-qualification-workload"),
      status: value("model-qualification-status"), evaluation_profile_revision: Number(value("model-qualification-profile-revision") || 1),
      evaluation_run_ids: csv("model-qualification-runs"), reason: value("model-qualification-reason"),
      quality_score: value("model-qualification-quality") ? Number(value("model-qualification-quality")) : null,
      cost_per_successful_outcome_usd: value("model-qualification-cost") ? Number(value("model-qualification-cost")) : null,
      cost_source: value("model-qualification-cost-source") || null,
    };
    if (!payload.reason) return status("A qualification reason is required.");
    if (!await confirmAction({ action: "Record model qualification", target: `${payload.model_id}/${payload.workload_class}`, risk: "high", consequence: `Record immutable status ${payload.status}? Active routing eligibility can change.`, recovery: "Record a later restricted or qualified revision with new evidence.", current: () => true })) return;
    try {
      await apiRequest("/api/model-gateway/qualifications", { method: "POST", body: JSON.stringify(payload) });
      status(`Recorded ${payload.status} qualification for ${payload.model_id}.`); refresh();
    } catch (error) { status(`Qualification failed: ${error.message}`); }
  }

  async function publishMapping() {
    const date = value("model-mapping-evaluated-at");
    const payload = {
      mapping_id: value("model-mapping-id"), function_id: value("model-mapping-function"), workload_class: value("model-mapping-workload"),
      primary_model_ids: csv("model-mapping-primary"), escalation_model_ids: csv("model-mapping-escalation"), critic_model_ids: csv("model-mapping-critic"),
      required_capabilities: csv("model-mapping-capabilities"), qualification_revision: value("model-mapping-evaluation-revision"),
      evaluated_at: date ? new Date(date).valueOf() / 1000 : Date.now() / 1000,
      max_cost_per_invocation_usd: value("model-mapping-max-cost") ? Number(value("model-mapping-max-cost")) : null,
      allow_fallback: Boolean(document.getElementById("model-mapping-fallback")?.checked), source: "operator-ui",
    };
    if (!payload.mapping_id || !payload.function_id || !payload.qualification_revision) return status("Mapping ID, function ID and evaluation revision are required.");
    if (!await confirmAction({ action: "Publish model routing revision", target: payload.mapping_id, risk: "high", consequence: `Publish this ${payload.workload_class} mapping after qualification checks? It controls future model selection.`, recovery: "Use the recorded rollback control to publish a known-good revision.", current: () => true })) return;
    try {
      await apiRequest("/api/model-gateway/routing-definitions", { method: "POST", body: JSON.stringify(payload) });
      status(`Published mapping ${payload.mapping_id}.`); refresh();
    } catch (error) { status(`Mapping publish failed: ${error.message}`); }
  }

  async function rollback(button) {
    const mapping = button.dataset.routingRollback, target = Number(button.dataset.routingTarget);
    if (!await confirmAction({ action: "Roll back model routing", target: mapping, risk: "high", consequence: `Publish a new revision cloned from ${mapping}@${target}?`, recovery: "Publish another immutable mapping revision.", current: () => button.isConnected })) return;
    try {
      await apiRequest(`/api/model-gateway/routing-definitions/${encodeURIComponent(mapping)}/rollback`, { method: "POST", body: JSON.stringify({ target_revision: target, reason: "operator rollback" }) });
      status(`Published rollback revision for ${mapping}.`); refresh();
    } catch (error) { status(`Routing rollback failed: ${error.message}`); }
  }

  function bind() {
    document.getElementById("publish-qualification-profile")?.addEventListener("click", publishProfile);
    document.getElementById("record-model-qualification")?.addEventListener("click", recordQualification);
    document.getElementById("publish-model-mapping")?.addEventListener("click", publishMapping);
    window.addEventListener("codex:model-gateway-rendered", event => {
      load(event.detail || {}).catch(console.error);
    });
    if (latest) load(latest).catch(console.error);
  }
  bindWhenReady(bind);
})();
