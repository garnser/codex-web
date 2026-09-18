(async () => {
  const BASE = window.location.pathname.startsWith("/codex") ? "/codex" : "";
  const { request: apiRequest } = await import(`${BASE}/static/api_client.js`);
  let actor = null;
  let records = [];
  let schemas = [];
  let projects = [];

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");
  }

  function setStatus(message) {
    const host = document.getElementById("definition-lifecycle-status");
    if (host) {
      host.hidden = false;
      host.textContent = message;
    }
  }

  function elevatedHuman() {
    return actor?.principal_kind === "human"
      && ["mfa", "local_trusted"].includes(actor?.assurance)
      && (actor?.roles || []).some((role) => ["owner", "admin"].includes(role));
  }

  function canManage(scopeType) {
    if (!actor) return false;
    if (actor.principal_kind === "service") {
      return scopeType === "global"
        ? (actor.service_scopes || []).includes("definitions:global-admin")
        : (actor.service_scopes || []).includes("definitions:admin");
    }
    if (!elevatedHuman()) return false;
    return scopeType !== "global" || actor.assurance === "local_trusted";
  }

  function sameSlot(left, right) {
    return left.definition_id === right.definition_id
      && left.kind === right.kind
      && left.scope_type === right.scope_type
      && (left.scope_id || null) === (right.scope_id || null);
  }

  function activeFor(record) {
    return records.find((item) => sameSlot(item, record) && item.lifecycle === "published") || null;
  }

  function timestamp(id) {
    const value = document.getElementById(id)?.value || "";
    if (!value) return null;
    const millis = new Date(value).getTime();
    return Number.isNaN(millis) ? null : millis / 1000;
  }

  function schemaSelection() {
    const raw = document.getElementById("definition-draft-schema")?.value || "";
    const split = raw.indexOf("::");
    if (split < 1) return null;
    return {
      kind: raw.slice(0, split),
      schemaVersion: raw.slice(split + 2),
    };
  }

  function scopeSelection() {
    const scopeType = document.getElementById("definition-draft-scope")?.value || "workspace";
    let scopeId = null;
    if (scopeType === "organization") scopeId = actor?.organization_id || null;
    if (scopeType === "workspace") scopeId = actor?.workspace_id || null;
    if (scopeType === "project") scopeId = document.getElementById("definition-draft-project")?.value || null;
    return { scopeType, scopeId };
  }

  function renderAssurance() {
    const host = document.getElementById("definition-lifecycle-assurance");
    if (!host || !actor) return;
    const global = canManage("global") ? "allowed" : "blocked";
    const tenant = canManage("workspace") ? "allowed" : "blocked";
    host.textContent = `Current actor ${actor.identity_id} · ${actor.principal_kind} · assurance ${actor.assurance}. Tenant/project definition mutation: ${tenant}; platform-global mutation: ${global}. Server authorization remains authoritative.`;
  }

  function populateDraftControls() {
    const schema = document.getElementById("definition-draft-schema");
    if (schema) {
      const previous = schema.value;
      schema.innerHTML = schemas.map((item) => (
        `<option value="${escapeHtml(item.kind)}::${escapeHtml(item.schema_version)}">${escapeHtml(item.kind)} @ ${escapeHtml(item.schema_version)}</option>`
      )).join("");
      if ([...schema.options].some((item) => item.value === previous)) schema.value = previous;
    }
    const project = document.getElementById("definition-draft-project");
    if (project) {
      const previous = project.value;
      project.innerHTML = '<option value="">Select project</option>' + projects.map((item) => (
        `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)} · ${escapeHtml(item.id)}</option>`
      )).join("");
      if ([...project.options].some((item) => item.value === previous)) project.value = previous;
    }
    updateDraftScopeState();
  }

  function updateDraftScopeState() {
    const scope = document.getElementById("definition-draft-scope")?.value || "workspace";
    const project = document.getElementById("definition-draft-project");
    const button = document.getElementById("create-definition-draft");
    if (project) project.disabled = scope !== "project";
    if (button) button.disabled = !canManage(scope);
  }

  function actionButtons(record) {
    const manageable = canManage(record.scope_type);
    if (!manageable) {
      return '<small>Lifecycle mutation unavailable for the current actor/assurance.</small>';
    }
    const actions = [];
    if (["draft", "validated"].includes(record.lifecycle)) {
      actions.push(`<button type="button" class="ghost-button" data-definition-action="validate" data-record-id="${escapeHtml(record.record_id)}">Validate schema</button>`);
      actions.push(`<button type="button" class="ghost-button" data-definition-action="publish" data-record-id="${escapeHtml(record.record_id)}">Publish revision</button>`);
    }
    if (record.lifecycle !== "quarantined") {
      actions.push(`<button type="button" class="ghost-button" data-definition-action="quarantine" data-record-id="${escapeHtml(record.record_id)}">Quarantine</button>`);
    }
    const active = activeFor(record);
    if (active && active.record_id !== record.record_id) {
      actions.push(`<button type="button" class="ghost-button" data-definition-action="rollback" data-record-id="${escapeHtml(record.record_id)}">Rollback to r${escapeHtml(record.revision)}</button>`);
    }
    return actions.length
      ? `<div class="developer-toolbar">${actions.join("")}</div>`
      : "<small>No lifecycle mutations apply to this revision.</small>";
  }

  function hydrateHosts() {
    records.forEach((record) => {
      const host = Array.from(document.querySelectorAll("[data-definition-management-host]"))
        .find((item) => item.dataset.definitionManagementHost === record.record_id);
      if (host) host.innerHTML = actionButtons(record);
    });
  }

  async function createDraft() {
    const definitionId = document.getElementById("definition-draft-id")?.value.trim() || "";
    const selected = schemaSelection();
    const scope = scopeSelection();
    const rawPayload = document.getElementById("definition-draft-payload")?.value || "";
    if (!definitionId || !selected || !rawPayload.trim()) {
      setStatus("Definition ID, registered schema and JSON payload are required.");
      return;
    }
    if (scope.scopeType === "project" && !scope.scopeId) {
      setStatus("Choose a canonical project for project-scoped definitions.");
      return;
    }
    let payload;
    try {
      payload = JSON.parse(rawPayload);
    } catch (error) {
      setStatus(`Definition payload must be valid JSON: ${error.message}`);
      return;
    }
    const reason = document.getElementById("definition-draft-reason")?.value.trim() || null;
    const body = {
      definition_id: definitionId,
      kind: selected.kind,
      definition_schema_version: selected.schemaVersion,
      scope_type: scope.scopeType,
      scope_id: scope.scopeId,
      payload,
      reason,
      effective_from: timestamp("definition-draft-effective-from"),
      effective_until: timestamp("definition-draft-effective-until"),
      min_engine_version: document.getElementById("definition-draft-min-engine")?.value.trim() || null,
      max_engine_version: document.getElementById("definition-draft-max-engine")?.value.trim() || null,
    };
    if (!window.confirm(
      `Create a new ${selected.kind} draft for ${definitionId} at ${scope.scopeType}${scope.scopeId ? `:${scope.scopeId}` : ""}? The server will validate the payload against the code-owned schema; this does not publish or activate it.`,
    )) return;
    try {
      const response = await apiRequest("/api/definitions/drafts", {
        method: "POST",
        body: JSON.stringify(body),
      });
      setStatus(`Created draft r${response.record.revision} · ${response.record.record_id}. It is not active until published.`);
      document.getElementById("refresh-definitions")?.click();
    } catch (error) {
      setStatus(`Draft creation failed: ${error.message}`);
    }
  }

  async function validateRecord(record) {
    if (!window.confirm(
      `Validate ${record.kind}:${record.definition_id} r${record.revision} against code-owned schema ${record.definition_schema_version}? Validation does not activate the revision.`,
    )) return;
    try {
      await apiRequest(
        `/api/definitions/${encodeURIComponent(record.record_id)}/validate`,
        { method: "POST", body: JSON.stringify({}) },
      );
      setStatus(`Validated ${record.definition_id} r${record.revision}.`);
      document.getElementById("refresh-definitions")?.click();
    } catch (error) {
      setStatus(`Definition validation failed: ${error.message}`);
    }
  }

  async function publishRecord(record) {
    const active = activeFor(record);
    let impactCount = 0;
    if (active) {
      try {
        const usage = await apiRequest(
          `/api/definitions/${encodeURIComponent(active.record_id)}/usage`,
        );
        impactCount = Number(usage.count || 0);
      } catch (_) {
        impactCount = -1;
      }
    }
    const reason = window.prompt(
      `Publication reason for ${record.definition_id} r${record.revision}:`,
      "",
    );
    if (reason === null) return;
    const approvalRaw = window.prompt(
      "Approval metadata as JSON object (optional):",
      "{}",
    );
    if (approvalRaw === null) return;
    let approvalMetadata;
    try {
      approvalMetadata = JSON.parse(approvalRaw || "{}");
      if (!approvalMetadata || Array.isArray(approvalMetadata) || typeof approvalMetadata !== "object") throw new Error("object required");
    } catch (error) {
      setStatus(`Approval metadata must be a JSON object: ${error.message}`);
      return;
    }
    const impact = active
      ? ` Active r${active.revision} will be superseded; ${impactCount < 0 ? "usage impact could not be loaded" : `${impactCount} tenant-visible usage reference(s) currently point to it`}.`
      : " No active revision currently occupies this canonical slot.";
    if (!window.confirm(
      `Publish ${record.kind}:${record.definition_id} r${record.revision}?${impact} Publication changes canonical runtime definition resolution; code-owned security invariants are unchanged.`,
    )) return;
    try {
      await apiRequest(
        `/api/definitions/${encodeURIComponent(record.record_id)}/publish`,
        {
          method: "POST",
          body: JSON.stringify({
            reason: reason.trim() || null,
            expected_active_revision: active?.revision ?? null,
            approval_metadata: approvalMetadata,
          }),
        },
      );
      setStatus(`Published ${record.definition_id} r${record.revision}.`);
      document.getElementById("refresh-definitions")?.click();
    } catch (error) {
      setStatus(`Definition publication failed: ${error.message}`);
    }
  }

  async function quarantineRecord(record) {
    const reason = window.prompt(
      `Quarantine reason for ${record.definition_id} r${record.revision}:`,
      "",
    );
    if (reason === null || !reason.trim()) return;
    if (!window.confirm(
      `Quarantine ${record.definition_id} r${record.revision}? If it is currently effective, canonical resolution will fail closed or fall back only according to remaining valid scoped definitions.`,
    )) return;
    try {
      await apiRequest(
        `/api/definitions/${encodeURIComponent(record.record_id)}/quarantine`,
        { method: "POST", body: JSON.stringify({ reason: reason.trim() }) },
      );
      setStatus(`Quarantined ${record.definition_id} r${record.revision}.`);
      document.getElementById("refresh-definitions")?.click();
    } catch (error) {
      setStatus(`Definition quarantine failed: ${error.message}`);
    }
  }

  async function rollbackRecord(record) {
    const active = activeFor(record);
    if (!active || active.record_id === record.record_id) return;
    const reason = window.prompt(
      `Reason for rollback to ${record.definition_id} r${record.revision}:`,
      "",
    );
    if (reason === null) return;
    if (!window.confirm(
      `Rollback ${record.kind}:${record.definition_id} from active r${active.revision} to the payload of r${record.revision}? The server creates a new immutable revision and publishes it with optimistic active-revision protection; historical records are preserved.`,
    )) return;
    try {
      const response = await apiRequest("/api/definitions/rollback", {
        method: "POST",
        body: JSON.stringify({
          definition_id: record.definition_id,
          kind: record.kind,
          scope_type: record.scope_type,
          scope_id: record.scope_id,
          target_revision: record.revision,
          reason: reason.trim() || null,
          expected_active_revision: active.revision,
        }),
      });
      setStatus(`Rollback published as new r${response.record.revision}; target history r${record.revision} remains immutable.`);
      document.getElementById("refresh-definitions")?.click();
    } catch (error) {
      setStatus(`Definition rollback failed: ${error.message}`);
    }
  }

  async function mutate(button) {
    const record = records.find((item) => item.record_id === button.dataset.recordId);
    if (!record) return;
    const action = button.dataset.definitionAction;
    button.disabled = true;
    try {
      if (action === "validate") await validateRecord(record);
      else if (action === "publish") await publishRecord(record);
      else if (action === "quarantine") await quarantineRecord(record);
      else if (action === "rollback") await rollbackRecord(record);
    } finally {
      if (document.contains(button)) button.disabled = false;
    }
  }

  function hydrate(detail) {
    records = detail.records || [];
    schemas = detail.schemas || [];
    projects = detail.projects || [];
    populateDraftControls();
    renderAssurance();
    hydrateHosts();
  }

  async function loadActor() {
    try {
      actor = await apiRequest("/api/identity/me");
      renderAssurance();
      updateDraftScopeState();
      hydrateHosts();
    } catch (error) {
      actor = null;
      setStatus(`Identity assurance unavailable: ${error.message}`);
      updateDraftScopeState();
      hydrateHosts();
    }
  }

  function bind() {
    document.getElementById("definition-draft-scope")?.addEventListener("change", updateDraftScopeState);
    document.getElementById("create-definition-draft")?.addEventListener("click", () => createDraft().catch(console.error));
    document.getElementById("definition-registry-list")?.addEventListener("click", (event) => {
      const button = event.target.closest?.("[data-definition-action]");
      if (button) mutate(button).catch(console.error);
    });
    loadActor().catch(console.error);
  }

  window.addEventListener("codex:definition-registry-rendered", (event) => hydrate(event.detail || {}));
  window.addEventListener("DOMContentLoaded", bind);
})();
