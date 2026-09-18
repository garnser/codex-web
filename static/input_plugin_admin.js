(async () => {
  const BASE = window.location.pathname.startsWith("/codex") ? "/codex" : "";
  const { request: apiRequest } = await import(`${BASE}/static/api_client.js`);
  let projection = { definition: null, registrations: [], catalog: [] };
  let invocations = [];
  let securityEvents = [];

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;").replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;").replaceAll('"', "&quot;");
  }

  function timeText(value) {
    if (!value) return "none";
    const date = new Date(Number(value) * 1000);
    return Number.isNaN(date.valueOf()) ? "unknown" : date.toLocaleString();
  }

  function listText(values) {
    return values?.length ? values.map(escapeHtml).join(", ") : "none";
  }

  function signedDelta(after, before) {
    const value = Number(after || 0) - Number(before || 0);
    return value > 0 ? `+${value}` : String(value);
  }

  function setStatus(message) {
    const host = document.getElementById("input-plugin-status");
    if (host) {
      host.hidden = false;
      host.textContent = message;
    }
  }

  function searchTerm() {
    return (document.getElementById("input-plugin-search")?.value || "").trim().toLowerCase();
  }

  function outcomeFilter() {
    return document.getElementById("input-plugin-outcome-filter")?.value || "";
  }

  function renderDefinition() {
    const host = document.getElementById("input-plugin-definition");
    if (!host) return;
    const item = projection.definition;
    if (!item) {
      host.innerHTML = '<div class="comm-entry"><strong>No effective input_pipeline definition.</strong></div>';
      return;
    }
    host.innerHTML = `<div class="comm-entry">
      <strong>${escapeHtml(item.definition_id)} · r${escapeHtml(item.revision)} · ${escapeHtml(item.lifecycle)}</strong>
      <small>Record: ${escapeHtml(item.record_id)} · schema: ${escapeHtml(item.definition_schema_version)} · scope: ${escapeHtml(item.scope_type)}${item.scope_id ? `:${escapeHtml(item.scope_id)}` : ""}</small>
      <small>Checksum: ${escapeHtml(item.checksum)} · published by: ${escapeHtml(item.published_by || "none")} · ${timeText(item.published_at)}</small>
      <small>Effective: ${timeText(item.effective_from)} → ${timeText(item.effective_until)}. Configuration changes must use the canonical Definition Registry lifecycle; this view owns no plugin configuration state.</small>
    </div>`;
  }

  function installedVersions(pluginId) {
    return projection.catalog
      .filter((item) => item.plugin_id === pluginId)
      .map((item) => `${item.plugin_version} (${item.transport})`);
  }

  function settingsText(settings) {
    const entries = Object.entries(settings || {});
    return entries.length
      ? entries.map(([key, value]) => `${escapeHtml(key)}=${escapeHtml(value)}`).join(" · ")
      : "none";
  }

  function registrationSearchText(item) {
    return [
      item.plugin_id, item.plugin_version, item.phase, item.failure_policy,
      item.implementation_transport,
      ...(item.conditions?.purposes || []),
      ...(item.conditions?.model_classes || []),
      ...Object.keys(item.settings || {}),
      ...Object.values(item.settings || {}).map(String),
    ].filter(Boolean).join(" ").toLowerCase();
  }

  function renderRegistrations() {
    const host = document.getElementById("input-plugin-registrations");
    if (!host) return;
    const term = searchTerm();
    const rows = (projection.registrations || []).filter(
      (item) => !term || registrationSearchText(item).includes(term),
    );
    host.innerHTML = rows.map((item) => {
      const alternatives = installedVersions(item.plugin_id);
      const availability = item.implementation_available
        ? `available locally via ${escapeHtml(item.implementation_transport)}`
        : `UNAVAILABLE exact version; installed versions: ${listText(alternatives)}`;
      return `<details class="comm-entry">
        <summary><strong>${escapeHtml(item.plugin_id)}@${escapeHtml(item.plugin_version)} · ${escapeHtml(item.phase)} · ${item.enabled ? "enabled" : "disabled"}</strong></summary>
        <small>Order: ${escapeHtml(item.order)} · failure policy: ${escapeHtml(item.failure_policy)} · implementation: ${availability}</small>
        <small>Conditions · purposes: ${listText(item.conditions?.purposes)} · model classes: ${listText(item.conditions?.model_classes)}</small>
        <small>Bounds · max patch: ${escapeHtml(item.max_patch_bytes)} bytes · max context/input growth: ${escapeHtml(item.max_added_characters)} characters</small>
        <small>Non-secret settings: ${settingsText(item.settings)}</small>
        <small>${item.implementation_transport === "skill" ? "SKILL.md transport metadata only; the skill body is intentionally not rendered." : "Implementation transport is code/local-catalog metadata, not Definition Registry authority."}</small>
      </details>`;
    }).join("") || '<div class="comm-entry"><strong>No configured registrations match the search.</strong></div>';

    if (!(projection.registrations || []).length) {
      host.innerHTML = '<div class="comm-entry"><strong>The effective input pipeline is empty.</strong><small>Existing provider behavior remains unchanged until an operator publishes registrations through Definition Registry.</small></div>';
    }
  }

  function provenanceSearchText(invocation) {
    return [
      invocation.id, invocation.actor_id, invocation.purpose, invocation.model_class,
      invocation.work_item_ref, invocation.goal_id, invocation.decision_id,
      invocation.execution_id, invocation.status,
      ...(invocation.input_plugin_provenance || []).flatMap((item) => [
        item.plugin_id, item.plugin_version, item.transport, item.phase, item.outcome,
        ...(item.applied_fields || []), ...(item.proposed_gated_fields || []),
        ...(item.rejected_fields || []),
      ]),
      ...(invocation.input_gated_proposals || []).map((item) => item.field),
    ].filter(Boolean).join(" ").toLowerCase();
  }

  function matchesOutcome(invocation) {
    const filter = outcomeFilter();
    if (!filter) return true;
    const outcomes = (invocation.input_plugin_provenance || []).map((item) => item.outcome);
    if (filter === "applied") return outcomes.includes("applied");
    if (filter === "skipped") return outcomes.some((value) => String(value).includes("skip"));
    return outcomes.some((value) => (
      String(value).includes("fail") || String(value).includes("denied")
      || String(value).includes("reject")
    ));
  }

  function definitionRefText(ref) {
    if (!ref) return "none";
    return `${ref.definition_id}@r${ref.revision} · record ${ref.record_id} · ${ref.checksum}`;
  }

  function renderProvenance(item) {
    const warningCount = item.warnings?.length || 0;
    return `<div class="comm-entry">
      <strong>${escapeHtml(item.plugin_id)}@${escapeHtml(item.plugin_version)} · ${escapeHtml(item.transport)} · ${escapeHtml(item.phase)} · ${escapeHtml(item.outcome)}</strong>
      <small>Definition: ${escapeHtml(definitionRefText(item.definition_ref))}</small>
      <small>Hashes · input: ${escapeHtml(item.input_sha256)} · patch: ${escapeHtml(item.patch_sha256 || "none")} · output: ${escapeHtml(item.output_sha256)}</small>
      <small>Applied fields: ${listText(item.applied_fields)} · gated proposals: ${listText(item.proposed_gated_fields)} · rejected fields: ${listText(item.rejected_fields)}</small>
      <small>Characters: ${escapeHtml(item.input_characters_before)} → ${escapeHtml(item.input_characters_after)} (${escapeHtml(signedDelta(item.input_characters_after, item.input_characters_before))}) · estimated tokens: ${escapeHtml(item.estimated_tokens_before)} → ${escapeHtml(item.estimated_tokens_after)} (${escapeHtml(signedDelta(item.estimated_tokens_after, item.estimated_tokens_before))})</small>
      <small>Duration: ${escapeHtml(Number(item.duration_seconds || 0).toFixed(4))}s · warnings recorded: ${warningCount}. Warning bodies are not rendered.</small>
    </div>`;
  }

  function renderInvocations() {
    const host = document.getElementById("input-plugin-invocations");
    if (!host) return;
    const term = searchTerm();
    const rows = invocations.filter((item) => (
      (item.input_plugin_provenance?.length || item.input_gated_proposals?.length)
      && (!term || provenanceSearchText(item).includes(term))
      && matchesOutcome(item)
    ));
    host.innerHTML = rows.map((item) => `<details class="comm-entry">
      <summary><strong>${escapeHtml(item.id)} · ${escapeHtml(item.model_class)} · ${escapeHtml(item.purpose)} · ${escapeHtml(item.status)}</strong></summary>
      <small>Actor: ${escapeHtml(item.actor_id)} · created: ${timeText(item.created_at)} · provider/model: ${escapeHtml(item.selected_provider_id || "none")} / ${escapeHtml(item.selected_model_id || "none")}</small>
      <small>Work: ${escapeHtml(item.work_item_ref || "none")} · Goal: ${escapeHtml(item.goal_id || "none")} · Decision: ${escapeHtml(item.decision_id || "none")} · Execution: ${escapeHtml(item.execution_id || "none")}</small>
      ${(item.input_plugin_provenance || []).map(renderProvenance).join("")}
      ${(item.input_gated_proposals || []).map((proposal) => `<div class="comm-entry">
        <strong>Gated proposal · ${escapeHtml(proposal.plugin_id)}@${escapeHtml(proposal.plugin_version)} · ${escapeHtml(proposal.field)}</strong>
        <small>Phase: ${escapeHtml(proposal.phase)} · proposed value hash: ${escapeHtml(proposal.value_sha256)}. The proposed value itself is intentionally not rendered and this UI cannot make it effective.</small>
      </div>`).join("")}
      <small>Prompt/message/context bodies are never loaded by this view.</small>
    </details>`).join("") || '<div class="comm-entry"><strong>No plugin-affected model invocations match the current filters.</strong></div>';
  }

  const SAFE_EVENT_DETAIL_KEYS = new Set([
    "request_id", "plugin_id", "plugin_version", "transport", "phase",
    "failure_policy", "plugin_outcome", "exception_type", "rejected_fields",
    "duration_seconds",
  ]);

  function safeEventDetails(item) {
    return Object.entries(item.details || {})
      .filter(([key]) => SAFE_EVENT_DETAIL_KEYS.has(key))
      .map(([key, value]) => `${escapeHtml(key)}=${escapeHtml(value)}`)
      .join(" · ");
  }

  function renderSecurityEvents() {
    const host = document.getElementById("input-plugin-security-events");
    if (!host) return;
    const term = searchTerm();
    const rows = securityEvents.filter((item) => (
      String(item.event_type || "").startsWith("input_plugin.")
      && (!term || [
        item.event_type, item.reason, item.actor_identity_id, item.work_item_ref,
        item.execution_id, safeEventDetails(item),
      ].filter(Boolean).join(" ").toLowerCase().includes(term))
    ));
    host.innerHTML = rows.map((item) => `<div class="comm-entry">
      <strong>${escapeHtml(item.event_type)} · ${escapeHtml(item.outcome)} · ${timeText(item.occurred_at)}</strong>
      <small>Actor: ${escapeHtml(item.actor_identity_id || "unknown")} · Work: ${escapeHtml(item.work_item_ref || "none")} · Execution: ${escapeHtml(item.execution_id || "none")}</small>
      <small>Reason: ${escapeHtml(item.reason || "input plugin boundary event")}</small>
      <small>Safe metadata: ${safeEventDetails(item) || "none"}</small>
      <small>Prompt/context/warning/transport payload bodies and credential material are not rendered.</small>
    </div>`).join("") || '<div class="comm-entry"><strong>No input-plugin security events match the current search.</strong></div>';
  }

  function renderAll() {
    renderDefinition();
    renderRegistrations();
    renderInvocations();
    renderSecurityEvents();
    const unavailable = (projection.registrations || []).filter((item) => (
      item.enabled && !item.implementation_available
    )).length;
    const affected = invocations.filter((item) => item.input_plugin_provenance?.length).length;
    const rejected = securityEvents.filter((item) => String(item.event_type || "").startsWith("input_plugin.")).length;
    setStatus(`${projection.registrations?.length || 0} configured registration(s) · ${unavailable} enabled exact-version implementation(s) unavailable · ${affected} recent plugin-affected invocation(s) · ${rejected} recorded boundary failure/rejection event(s).`);
  }

  function openDefinitionRegistry() {
    const target = document.getElementById("definition-registry-status");
    const card = target?.closest?.(".developer-card");
    card?.scrollIntoView({ behavior: "smooth", block: "start" });
    target?.focus?.();
  }

  async function refresh() {
    setStatus("Loading effective input pipeline, invocation provenance and boundary events...");
    try {
      const [pipeline, invocationResponse, eventResponse] = await Promise.all([
        apiRequest("/api/input-plugins"),
        apiRequest("/api/model-gateway/invocations?limit=100"),
        apiRequest("/api/security/events"),
      ]);
      projection = pipeline;
      invocations = invocationResponse.items || [];
      securityEvents = eventResponse.items || [];
      renderAll();
    } catch (error) {
      setStatus(`Input Plugin Pipeline unavailable: ${error.message}`);
    }
  }

  function bind() {
    const panel = document.getElementById("developer-panel");
    document.getElementById("refresh-input-plugins")?.addEventListener("click", refresh);
    document.getElementById("refresh-developer")?.addEventListener("click", refresh);
    document.getElementById("input-plugin-search")?.addEventListener("input", renderAll);
    document.getElementById("input-plugin-outcome-filter")?.addEventListener("change", renderInvocations);
    document.getElementById("open-input-pipeline-definition")?.addEventListener("click", openDefinitionRegistry);
    panel?.addEventListener("toggle", () => {
      if (panel.open) refresh().catch(console.error);
    });
    if (panel?.open) refresh().catch(console.error);
  }

  window.addEventListener("DOMContentLoaded", bind);
})();
