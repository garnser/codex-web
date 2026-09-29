(async () => {
  const BASE = window.location.pathname.startsWith("/codex") ? "/codex" : "";
  const { request: apiRequest } = await import(`${BASE}/static/api_client.js`);
  const { formValidation } = await import(`${BASE}/static/form_validation.js`);
  const { valueEditor, draftValue } = await import(`${BASE}/static/configuration_value_editor.js`);
  let validation = null;
  function draftValidation() {
    return validation ||= formValidation(document.getElementById('configuration-draft-key').closest('.route-test'));
  }
  let actor = null;
  let specs = [];
  let records = [];
  let projects = [];
  let resources = [];
  let secrets = [];
  let definitions = [];
  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;").replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;").replaceAll('"', "&quot;");
  }
  function setStatus(message) {
    const host = document.getElementById("configuration-management-status");
    if (host) { host.hidden = false; host.textContent = message; }
  }
  function selectedSpec() {
    const key = document.getElementById("configuration-draft-key")?.value || "";
    return specs.find((item) => item.key === key) || null;
  }
  function elevatedHuman() {
    return actor?.principal_kind === "human"
      && ["mfa", "local_trusted"].includes(actor?.assurance)
      && (actor?.roles || []).some((role) => ["owner", "admin"].includes(role));
  }
  function canManage(scope) {
    if (!actor) return false;
    const platform = ["deployment", "global"].includes(scope);
    if (actor.principal_kind === "service") {
      return (actor.service_scopes || []).includes(
        platform ? "configuration:global-admin" : "configuration:admin",
      );
    }
    return elevatedHuman() && (!platform || actor.assurance === "local_trusted");
  }
  function sameSlot(left, right) {
    return left.key === right.key
      && left.scope_type === right.scope_type
      && (left.scope_id || null) === (right.scope_id || null);
  }
  function activeFor(record) {
    return records.find((item) => sameSlot(item, record) && item.state === "published") || null;
  }
  function dateTimestamp(id) {
    const raw = document.getElementById(id)?.value || "";
    if (!raw) return null;
    const millis = new Date(raw).getTime();
    return Number.isNaN(millis) ? null : millis / 1000;
  }
  function renderAssurance() {
    const host = document.getElementById("configuration-management-assurance");
    if (!host || !actor) return;
    const tenant = canManage("workspace") ? "allowed" : "blocked";
    const platform = canManage("global") ? "allowed" : "blocked";
    host.textContent = `Current actor ${actor.identity_id} · ${actor.principal_kind} · assurance ${actor.assurance}. Tenant/project/resource mutation: ${tenant}; deployment/global mutation: ${platform}. Server authorization remains authoritative.`;
  }
  function renderKeyOptions() {
    const key = document.getElementById("configuration-draft-key");
    if (!key) return;
    const previous = key.value;
    key.innerHTML = specs.map((spec) => (
      `<option value="${escapeHtml(spec.key)}">${escapeHtml(spec.key)} · ${escapeHtml(spec.value_kind)}</option>`
    )).join("");
    if (specs.some((item) => item.key === previous)) key.value = previous;
    updateSpecControls();
  }
  function renderScopes(spec) {
    const scope = document.getElementById("configuration-draft-scope");
    if (!scope) return;
    const previous = scope.value;
    scope.innerHTML = (spec?.allowed_scopes || []).map((value) => (
      `<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`
    )).join("");
    if ((spec?.allowed_scopes || []).includes(previous)) scope.value = previous;
    updateScopeTargets();
  }
  function updateScopeTargets() {
    const scope = document.getElementById("configuration-draft-scope")?.value || "";
    const project = document.getElementById("configuration-draft-project");
    const resource = document.getElementById("configuration-draft-resource");
    if (project) {
      project.disabled = scope !== "project";
      project.innerHTML = '<option value="">Select project</option>' + projects.map((item) => (
        `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)} · ${escapeHtml(item.id)}</option>`
      )).join("");
    }
    if (resource) {
      resource.disabled = scope !== "resource";
      resource.innerHTML = '<option value="">Select resource</option>' + resources.map((item) => (
        `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)} · ${escapeHtml(item.resource_type)} · ${escapeHtml(item.id)}</option>`
      )).join("");
    }
    const button = document.getElementById("create-configuration-draft");
    if (button) button.disabled = !scope || !canManage(scope) || selectedSpec()?.editable === false;
  }
  function updateSpecControls() {
    validation?.clear();
    const spec = selectedSpec();
    renderScopes(spec);
    const host = document.getElementById("configuration-draft-value-host");
    if (host) host.innerHTML = valueEditor(spec, { secrets, definitions });
    const targeting = document.getElementById("configuration-targeting-panel");
    if (targeting) {
      targeting.hidden = !spec?.feature_flag;
      targeting.open = Boolean(spec?.feature_flag);
      targeting.querySelectorAll('input').forEach(input => { input.disabled = !spec?.feature_flag; });
    }
    const kill = document.getElementById("configuration-force-disabled");
    if (kill) {
      kill.disabled = !spec?.kill_switch_capable;
      if (!spec?.kill_switch_capable) kill.checked = false;
    }
  }
  function scopeId(scope) {
    if (["deployment", "global"].includes(scope)) return null;
    if (scope === "organization") return actor?.organization_id || null;
    if (scope === "workspace") return actor?.workspace_id || null;
    if (scope === "project") return document.getElementById("configuration-draft-project")?.value || null;
    if (scope === "resource") return document.getElementById("configuration-draft-resource")?.value || null;
    return null;
  }
  function featureTargeting(spec, forceDisabled) {
    if (!spec.feature_flag || forceDisabled) return null;
    const percentage = Number(document.getElementById("configuration-target-percentage")?.value || 100);
    const cohorts = (document.getElementById("configuration-target-cohorts")?.value || "")
      .split(",").map((item) => item.trim()).filter(Boolean);
    const owner = document.getElementById("configuration-target-owner")?.value.trim() || null;
    const expiresAt = dateTimestamp("configuration-target-expires");
    if (percentage === 100 && !cohorts.length && !owner && expiresAt === null) return null;
    return { percentage, cohorts, owner, expires_at: expiresAt };
  }
  async function createDraft() {
    const errors = [];
    const feedback = draftValidation();
    const spec = selectedSpec();
    if (!spec) return feedback.show([{ field: 'configuration-draft-key', message: 'Choose a configuration key.' }]);
    if (!spec.editable) return feedback.show([{ message: 'This setting is read-only. Use its owning workflow to change it.' }]);
    const scope = document.getElementById("configuration-draft-scope")?.value || "";
    const target = scopeId(scope);
    if (!["deployment", "global"].includes(scope) && !target) {
      errors.push({ field: `configuration-draft-${scope}`, message: `Choose a ${scope} target before saving.` });
    }
    const forceDisabled = Boolean(document.getElementById("configuration-force-disabled")?.checked);
    let value;
    try {
      value = forceDisabled ? false : draftValue(spec, { definitions });
    } catch (error) {
      errors.push({ field: 'configuration-draft-value', message: error.message });
    }
    if (forceDisabled && !spec.kill_switch_capable) {
      errors.push({ field: 'configuration-force-disabled', message: 'This setting does not support a kill switch. Clear Force-disabled.' });
    }
    if (!feedback.validate(errors)) return;
    const targeting = featureTargeting(spec, forceDisabled);
    const reason = document.getElementById("configuration-draft-reason")?.value.trim() || null;
    if (!window.confirm(
      `Create a typed draft for ${spec.key} at ${scope}${target ? `:${target}` : ""}? This does not publish or activate the value. ${forceDisabled ? "This draft is an explicit force-disabled kill switch." : ""}`,
    )) return;
    try {
      const response = await apiRequest("/api/configuration/drafts", {
        method: "POST",
        body: JSON.stringify({
          key: spec.key,
          scope_type: scope,
          scope_id: target,
          value,
          reason,
          feature_targeting: targeting,
          force_disabled: forceDisabled,
        }),
      });
      setStatus(`Created ${spec.key} draft r${response.record.revision}; publish to activate.`);
      feedback.clear();
      document.getElementById("refresh-configuration")?.click();
    } catch (error) {
      feedback.server(error, {
        key: 'configuration-draft-key', scope_type: 'configuration-draft-scope',
        scope_id: `configuration-draft-${scope}`, value: 'configuration-draft-value',
        reason: 'configuration-draft-reason', force_disabled: 'configuration-force-disabled',
        'feature_targeting.percentage': 'configuration-target-percentage',
        'feature_targeting.owner': 'configuration-target-owner',
        'feature_targeting.expires_at': 'configuration-target-expires',
      });
    }
  }
  function actionButtons(record) {
    if (!canManage(record.scope_type)) {
      return "<small>Mutation unavailable for this actor/assurance.</small>";
    }
    const actions = [];
    if (record.state === "draft") {
      actions.push(["validate", "Validate"]);
      actions.push(["publish", "Publish"]);
    }
    const active = activeFor(record);
    if (active && active.id === record.id) actions.push(["reset", "Revert to inherited/default"]);
    if (active && active.id !== record.id) actions.push(["rollback", `Rollback to r${record.revision}`]);
    return actions.length
      ? `<div class="developer-toolbar">${actions.map(([action, label]) => `<button type="button" class="ghost-button" data-configuration-action="${action}" data-record-id="${escapeHtml(record.id)}">${escapeHtml(label)}</button>`).join("")}</div>`
      : "<small>No lifecycle actions for this revision.</small>";
  }
  function hydrateHosts() {
    for (const record of records) {
      const host = Array.from(document.querySelectorAll("[data-configuration-management-host]"))
        .find((item) => item.dataset.configurationManagementHost === record.id);
      if (host) host.innerHTML = actionButtons(record);
    }
  }
  async function validateRecord(record) {
    try {
      const result = await apiRequest(`/api/configuration/${encodeURIComponent(record.id)}/validate`, { method: "POST" });
      setStatus(`Validated ${record.key} r${record.revision} as ${result.spec?.value_kind || "unknown"}.`);
    } catch (error) {
      setStatus(`Configuration validation failed: ${error.message}`);
    }
  }
  async function publishRecord(record) {
    const active = activeFor(record);
    let impact = null;
    try {
      impact = await apiRequest(`/api/configuration/${encodeURIComponent(record.id)}/impact`);
    } catch (_) {}
    const reason = window.prompt(`Publication reason for ${record.key} r${record.revision}:`, record.create_reason || "");
    if (reason === null) return;
    const overrides = impact?.more_specific_overrides?.length || 0;
    const startup = impact?.startup_only ? " This is startup-only and will not hot-reload." : "";
    if (!window.confirm(
      `Publish ${record.key} r${record.revision}? Active revision in this exact slot: ${active?.revision ?? "none"}. ${overrides} more-specific published override(s) currently exist.${startup} Publishing changes runtime configuration but cannot grant authority.`,
    )) return;
    try {
      await apiRequest(`/api/configuration/${encodeURIComponent(record.id)}/publish`, {
        method: "POST",
        body: JSON.stringify({
          reason: reason.trim() || null,
          expected_active_revision: active?.revision ?? null,
        }),
      });
      setStatus(`Published ${record.key} r${record.revision}.`);
      document.getElementById("refresh-configuration")?.click();
    } catch (error) {
      setStatus(`Configuration publication failed: ${error.message}`);
    }
  }
  async function rollbackRecord(record) {
    const active = activeFor(record);
    if (!active || active.id === record.id) return;
    const reason = window.prompt(`Reason for rollback to ${record.key} r${record.revision}:`, "");
    if (reason === null) return;
    if (!window.confirm(
      `Rollback ${record.key} from active r${active.revision} to the value of r${record.revision}? The server creates and publishes a new immutable revision; history is preserved.`,
    )) return;
    try {
      const response = await apiRequest("/api/configuration/rollback", {
        method: "POST",
        body: JSON.stringify({
          key: record.key,
          scope_type: record.scope_type,
          scope_id: record.scope_id,
          target_revision: record.revision,
          reason: reason.trim() || null,
          expected_active_revision: active.revision,
        }),
      });
      setStatus(`Rollback published as new ${record.key} r${response.record.revision}.`);
      document.getElementById("refresh-configuration")?.click();
    } catch (error) {
      setStatus(`Configuration rollback failed: ${error.message}`);
    }
  }
  async function resetRecord(record) {
    const active = activeFor(record);
    if (!active || active.id !== record.id) return;
    const reason = window.prompt(`Reason for reverting ${record.key} r${record.revision} to inherited/default resolution:`, "");
    if (reason === null) return;
    if (!window.confirm(
      `Revert the explicit ${record.key} override at ${record.scope_type}${record.scope_id ? `:${record.scope_id}` : ""}? The active revision is preserved in history and superseded by a disabled tombstone. Resolution will fall through to the next applicable published scope or code-owned default.`,
    )) return;
    try {
      const response = await apiRequest("/api/configuration/reset", {
        method: "POST",
        body: JSON.stringify({
          key: record.key,
          scope_type: record.scope_type,
          scope_id: record.scope_id,
          reason: reason.trim() || null,
          expected_active_revision: active.revision,
        }),
      });
      setStatus(`Reverted ${record.key} with tombstone r${response.record.revision}; resolution now inherits.`);
      document.getElementById("refresh-configuration")?.click();
    } catch (error) {
      setStatus(`Configuration reset failed: ${error.message}`);
    }
  }
  async function mutate(button) {
    const record = records.find((item) => item.id === button.dataset.recordId);
    if (!record) return;
    button.disabled = true;
    try {
      if (button.dataset.configurationAction === "validate") await validateRecord(record);
      else if (button.dataset.configurationAction === "publish") await publishRecord(record);
      else if (button.dataset.configurationAction === "rollback") await rollbackRecord(record);
      else if (button.dataset.configurationAction === "reset") await resetRecord(record);
    } finally {
      if (document.contains(button)) button.disabled = false;
    }
  }
  async function loadDependencies() {
    const [me, secretResponse, definitionResponse] = await Promise.all([
      apiRequest("/api/identity/me"),
      apiRequest("/api/secrets").catch(() => ({ items: [] })),
      apiRequest("/api/definitions/records").catch(() => ({ items: [] })),
    ]);
    actor = me;
    secrets = secretResponse.items || [];
    definitions = definitionResponse.items || [];
    renderAssurance();
  }
  async function hydrate(detail) {
    specs = detail.specs || [];
    records = detail.records || [];
    projects = detail.projects || [];
    resources = detail.resources || [];
    try {
      if (!actor) await loadDependencies();
    } catch (error) {
      actor = null;
      setStatus(`Configuration management identity unavailable: ${error.message}`);
    }
    renderKeyOptions();
    hydrateHosts();
  }
  function bind() {
    document.getElementById("configuration-draft-key")?.addEventListener("change", updateSpecControls);
    document.getElementById("configuration-draft-scope")?.addEventListener("change", updateScopeTargets);
    document.getElementById("configuration-force-disabled")?.addEventListener("change", updateSpecControls);
    document.getElementById("create-configuration-draft")?.addEventListener("click", () => createDraft().catch(console.error));
    document.getElementById("configuration-record-list")?.addEventListener("click", (event) => {
      const button = event.target.closest?.("[data-configuration-action]");
      if (button) mutate(button).catch(console.error);
    });
  }
  window.addEventListener("codex:configuration-state-rendered", (event) => hydrate(event.detail || {}));
  if (document.readyState === 'loading') window.addEventListener("DOMContentLoaded", bind, { once: true });
  else bind();
})();
