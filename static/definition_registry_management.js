(async () => {
  const BASE = window.location.pathname.startsWith("/codex") ? "/codex" : "";
  const { request: apiRequest } = await import(`${BASE}/static/api_client.js`);
  let state = { records: [], schemas: [], projects: [], bootstrap: null };
  let actor = null;

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");
  }

  function setStatus(message) {
    const host = document.getElementById("definition-management-status");
    if (host) {
      host.hidden = false;
      host.textContent = message;
    }
  }

  function refreshRegistry() {
    document.getElementById("refresh-definitions")?.click();
  }

  function definitionSlot(record) {
    return [
      record.kind,
      record.definition_id,
      record.scope_type,
      record.scope_id || "",
    ].join("::");
  }

  function activeFor(record) {
    return state.records.find((item) => (
      definitionSlot(item) === definitionSlot(record)
      && item.lifecycle === "published"
    )) || null;
  }

  function asTimestamp(id) {
    const raw = document.getElementById(id)?.value || "";
    if (!raw) return null;
    const value = new Date(raw).getTime();
    return Number.isNaN(value) ? null : value / 1000;
  }

  function objectFromJson(id) {
    const raw = document.getElementById(id)?.value || "";
    const value = JSON.parse(raw || "{}");
    if (!value || Array.isArray(value) || typeof value !== "object") {
      throw new Error("Definition payload must be a JSON object.");
    }
    return value;
  }

  function managementAllowedText() {
    if (!actor) return "Identity metadata unavailable; server-side authorization still applies.";
    if (actor.principal_kind === "service") {
      const scopes = actor.service_scopes || [];
      return `Service actor ${actor.identity_id}; workspace mutation requires definitions:admin and global mutation requires definitions:global-admin. Current scopes: ${scopes.join(", ") || "none"}.`;
    }
    const roles = actor.roles || [];
    return `Human actor ${actor.identity_id}; definition mutation requires tenant admin authority plus MFA/step-up. Global mutation additionally requires local-trusted platform context. Current assurance: ${actor.assurance}; roles: ${roles.join(", ") || "none"}.`;
  }

  function populateSchemas() {
    const select = document.getElementById("definition-draft-schema");
    if (!select) return;
    const previous = select.value;
    select.innerHTML = (state.schemas || []).map((item) => {
      const key = `${item.kind}::${item.schema_version}`;
      return `<option value="${escapeHtml(key)}">${escapeHtml(item.kind)} @ ${escapeHtml(item.schema_version)}</option>`;
    }).join("");
    if (Array.from(select.options).some((item) => item.value === previous)) select.value = previous;
  }

  function populateSources() {
    const select = document.getElementById("definition-draft-source");
    if (!select) return;
    const previous = select.value;
    const rows = [...state.records].sort((left, right) => (
      left.kind.localeCompare(right.kind)
      || left.definition_id.localeCompare(right.definition_id)
      || Number(right.revision) - Number(left.revision)
    ));
    select.innerHTML = '<option value="">New definition</option>' + rows.map((item) => (
      `<option value="${escapeHtml(item.record_id)}">${escapeHtml(item.kind)} · ${escapeHtml(item.definition_id)} · r${escapeHtml(item.revision)} · ${escapeHtml(item.scope_type)}${item.scope_id ? `:${escapeHtml(item.scope_id)}` : ""} · ${escapeHtml(item.lifecycle)}</option>`
    )).join("");
    if (rows.some((item) => item.record_id === previous)) select.value = previous;
  }

  function populateScopeTargets(preferred = null) {
    const scope = document.getElementById("definition-draft-scope")?.value || "global";
    const select = document.getElementById("definition-draft-scope-id");
    if (!select) return;
    if (scope === "global") {
      select.innerHTML = '<option value="">Platform global</option>';
      select.disabled = true;
      return;
    }
    select.disabled = false;
    if (scope === "organization") {
      const value = actor?.organization_id || preferred || "";
      select.innerHTML = `<option value="${escapeHtml(value)}">${escapeHtml(value || "authenticated organization")}</option>`;
      return;
    }
    if (scope === "workspace") {
      const value = actor?.workspace_id || preferred || "";
      select.innerHTML = `<option value="${escapeHtml(value)}">${escapeHtml(value || "authenticated workspace")}</option>`;
      return;
    }
    const projects = state.projects || [];
    select.innerHTML = projects.map((project) => (
      `<option value="${escapeHtml(project.id)}">${escapeHtml(project.name)} · ${escapeHtml(project.id)}</option>`
    )).join("");
    if (preferred && projects.some((project) => project.id === preferred)) select.value = preferred;
  }

  function dateLocal(timestamp) {
    if (!timestamp) return "";
    const date = new Date(Number(timestamp) * 1000);
    const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
    return local.toISOString().slice(0, 16);
  }

  function loadSource(recordId) {
    const record = state.records.find((item) => item.record_id === recordId);
    if (!record) {
      document.getElementById("definition-draft-id").value = "";
      document.getElementById("definition-draft-payload").value = "{\n  \n}";
      document.getElementById("definition-draft-reason").value = "";
      document.getElementById("definition-draft-from").value = "";
      document.getElementById("definition-draft-until").value = "";
      document.getElementById("definition-draft-min-engine").value = "";
      document.getElementById("definition-draft-max-engine").value = "";
      return;
    }
    document.getElementById("definition-draft-id").value = record.definition_id;
    document.getElementById("definition-draft-scope").value = record.scope_type;
    populateScopeTargets(record.scope_id || null);
    const schema = document.getElementById("definition-draft-schema");
    if (schema) schema.value = `${record.kind}::${record.definition_schema_version}`;
    document.getElementById("definition-draft-payload").value = JSON.stringify(record.payload || {}, null, 2);
    document.getElementById("definition-draft-reason").value = `follow-up to revision ${record.revision}`;
    document.getElementById("definition-draft-from").value = dateLocal(record.effective_from);
    document.getElementById("definition-draft-until").value = dateLocal(record.effective_until);
    document.getElementById("definition-draft-min-engine").value = record.min_engine_version || "";
    document.getElementById("definition-draft-max-engine").value = record.max_engine_version || "";
  }

  function addLifecycleButtons() {
    for (const record of state.records) {
      const details = document.querySelector(`[data-definition-record="${CSS.escape(record.record_id)}"]`);
      if (!details || details.querySelector("[data-definition-lifecycle-controls]")) continue;
      const controls = document.createElement("div");
      controls.className = "route-test";
      controls.dataset.definitionLifecycleControls = "1";
      const actions = [];
      if (record.lifecycle === "draft") actions.push(["validate", "Validate"]);
      if (record.lifecycle === "draft" || record.lifecycle === "validated") actions.push(["publish", "Publish"]);
      if (["draft", "validated", "published"].includes(record.lifecycle)) actions.push(["quarantine", "Quarantine"]);
      if (record.lifecycle !== "draft") actions.push(["rollback", `Rollback to r${record.revision}`]);
      controls.innerHTML = actions.map(([action, label]) => (
        `<button type="button" class="ghost-button" data-definition-action="${action}" data-record-id="${escapeHtml(record.record_id)}">${escapeHtml(label)}</button>`
      )).join("");
      details.appendChild(controls);
    }
  }

  async function createDraft() {
    const definitionId = document.getElementById("definition-draft-id")?.value.trim() || "";
    const schemaRaw = document.getElementById("definition-draft-schema")?.value || "";
    const separator = schemaRaw.lastIndexOf("::");
    if (!definitionId || separator < 1) return setStatus("Definition ID and registered schema are required.");
    const kind = schemaRaw.slice(0, separator);
    const schemaVersion = schemaRaw.slice(separator + 2);
    const scopeType = document.getElementById("definition-draft-scope")?.value || "global";
    const scopeId = scopeType === "global" ? null : (document.getElementById("definition-draft-scope-id")?.value || null);
    let payload;
    try {
      payload = objectFromJson("definition-draft-payload");
    } catch (error) {
      return setStatus(error.message);
    }
    const request = {
      definition_id: definitionId,
      kind,
      definition_schema_version: schemaVersion,
      scope_type: scopeType,
      scope_id: scopeId,
      payload,
      reason: document.getElementById("definition-draft-reason")?.value.trim() || null,
      effective_from: asTimestamp("definition-draft-from"),
      effective_until: asTimestamp("definition-draft-until"),
      min_engine_version: document.getElementById("definition-draft-min-engine")?.value.trim() || null,
      max_engine_version: document.getElementById("definition-draft-max-engine")?.value.trim() || null,
    };
    if (!window.confirm(`Create a new draft revision for ${kind}:${definitionId} at ${scopeType}${scopeId ? `:${scopeId}` : ""}? The payload will be schema-validated server-side and remains inactive until published.`)) return;
    try {
      const response = await apiRequest("/api/definitions/drafts", {
        method: "POST",
        body: JSON.stringify(request),
      });
      setStatus(`Created draft r${response.record.revision} (${response.record.record_id}).`);
      refreshRegistry();
    } catch (error) {
      setStatus(`Draft creation failed: ${error.message}`);
    }
  }

  async function validateRecord(record) {
    if (!window.confirm(`Validate ${record.kind}:${record.definition_id} r${record.revision}? This validates schema/engine compatibility but does not activate the revision.`)) return;
    try {
      await apiRequest(`/api/definitions/${encodeURIComponent(record.record_id)}/validate`, {
        method: "POST",
        body: JSON.stringify({}),
      });
      setStatus(`Validated r${record.revision}.`);
      refreshRegistry();
    } catch (error) {
      setStatus(`Validation failed: ${error.message}`);
    }
  }

  async function publishRecord(record) {
    const active = activeFor(record);
    const expected = active && active.record_id !== record.record_id ? active.revision : null;
    const reason = window.prompt("Publication reason (optional):", record.create_reason || "") ?? null;
    if (reason === null) return;
    const approvalRaw = window.prompt("Approval metadata JSON object (optional):", "{}");
    if (approvalRaw === null) return;
    let approvalMetadata;
    try {
      approvalMetadata = JSON.parse(approvalRaw || "{}");
      if (!approvalMetadata || Array.isArray(approvalMetadata) || typeof approvalMetadata !== "object") throw new Error("Approval metadata must be an object.");
    } catch (error) {
      return setStatus(`Publication metadata invalid: ${error.message}`);
    }
    if (!window.confirm(`Publish ${record.kind}:${record.definition_id} r${record.revision}? Current active revision: ${expected ?? "none"}. Publishing supersedes the currently active revision for this exact canonical slot.`)) return;
    try {
      await apiRequest(`/api/definitions/${encodeURIComponent(record.record_id)}/publish`, {
        method: "POST",
        body: JSON.stringify({
          reason: reason || null,
          expected_active_revision: expected,
          approval_metadata: approvalMetadata,
        }),
      });
      setStatus(`Published r${record.revision}.`);
      refreshRegistry();
    } catch (error) {
      setStatus(`Publication failed: ${error.message}`);
    }
  }

  async function quarantineRecord(record) {
    const reason = window.prompt("Quarantine reason (required):", "");
    if (!reason?.trim()) return setStatus("Quarantine requires a non-empty reason.");
    if (!window.confirm(`Quarantine ${record.kind}:${record.definition_id} r${record.revision}? If this is the active revision, runtime resolution can fail closed until another compatible revision is published.`)) return;
    try {
      await apiRequest(`/api/definitions/${encodeURIComponent(record.record_id)}/quarantine`, {
        method: "POST",
        body: JSON.stringify({ reason: reason.trim() }),
      });
      setStatus(`Quarantined r${record.revision}.`);
      refreshRegistry();
    } catch (error) {
      setStatus(`Quarantine failed: ${error.message}`);
    }
  }

  async function rollbackRecord(record) {
    const active = activeFor(record);
    const expected = active?.revision ?? null;
    if (!active) return setStatus("Rollback requires a currently published revision in the same canonical slot.");
    const reason = window.prompt("Rollback reason (optional):", `rollback to revision ${record.revision}`);
    if (reason === null) return;
    if (!window.confirm(`Rollback ${record.kind}:${record.definition_id} from active r${active.revision} to the payload of r${record.revision}? Rollback creates and publishes a new immutable revision; it does not reactivate the historical record in place.`)) return;
    try {
      const response = await apiRequest("/api/definitions/rollback", {
        method: "POST",
        body: JSON.stringify({
          definition_id: record.definition_id,
          kind: record.kind,
          scope_type: record.scope_type,
          scope_id: record.scope_id,
          target_revision: record.revision,
          reason: reason || null,
          expected_active_revision: expected,
        }),
      });
      setStatus(`Rollback published as new r${response.record.revision}.`);
      refreshRegistry();
    } catch (error) {
      setStatus(`Rollback failed: ${error.message}`);
    }
  }

  async function lifecycleAction(button) {
    const record = state.records.find((item) => item.record_id === button.dataset.recordId);
    if (!record) return setStatus("Definition revision is no longer available; refresh and retry.");
    if (button.dataset.definitionAction === "validate") return validateRecord(record);
    if (button.dataset.definitionAction === "publish") return publishRecord(record);
    if (button.dataset.definitionAction === "quarantine") return quarantineRecord(record);
    if (button.dataset.definitionAction === "rollback") return rollbackRecord(record);
  }

  async function exportDefinitions() {
    try {
      const documentValue = await apiRequest("/api/definitions/export");
      document.getElementById("definition-transfer-document").value = JSON.stringify(documentValue, null, 2);
      setStatus(`Loaded ${documentValue.records?.length || 0} tenant-visible definition record(s) for export.`);
    } catch (error) {
      setStatus(`Definition export failed: ${error.message}`);
    }
  }

  async function importDefinitions() {
    const raw = document.getElementById("definition-transfer-document")?.value || "";
    let documentValue;
    try {
      documentValue = JSON.parse(raw);
    } catch (error) {
      return setStatus(`Import document is not valid JSON: ${error.message}`);
    }
    const count = Array.isArray(documentValue?.records) ? documentValue.records.length : 0;
    if (!count) return setStatus("Import document must contain at least one definition record.");
    if (!window.confirm(`Import ${count} definition record(s) as new draft revisions? Every record is revalidated and authorization is checked for its target scope; imported records are never activated automatically.`)) return;
    try {
      const response = await apiRequest("/api/definitions/import", {
        method: "POST",
        body: JSON.stringify({ document: documentValue }),
      });
      setStatus(`Imported ${response.count} record(s) as drafts.`);
      refreshRegistry();
    } catch (error) {
      setStatus(`Definition import failed: ${error.message}`);
    }
  }

  async function hydrate(detail) {
    state = {
      records: detail.records || [],
      schemas: detail.schemas || [],
      projects: detail.projects || [],
      bootstrap: detail.bootstrap || null,
    };
    try {
      actor = await apiRequest("/api/identity/me");
    } catch (error) {
      actor = null;
      setStatus(`Identity metadata unavailable: ${error.message}`);
    }
    const assurance = document.getElementById("definition-management-assurance");
    if (assurance) assurance.textContent = managementAllowedText();
    populateSchemas();
    populateSources();
    populateScopeTargets();
    addLifecycleButtons();
  }

  function bind() {
    document.getElementById("definition-draft-source")?.addEventListener("change", (event) => loadSource(event.target.value));
    document.getElementById("definition-draft-scope")?.addEventListener("change", () => populateScopeTargets());
    document.getElementById("create-definition-draft")?.addEventListener("click", () => createDraft().catch(console.error));
    document.getElementById("export-definitions")?.addEventListener("click", () => exportDefinitions().catch(console.error));
    document.getElementById("import-definitions")?.addEventListener("click", () => importDefinitions().catch(console.error));
    document.getElementById("definition-registry-list")?.addEventListener("click", (event) => {
      const button = event.target.closest?.("[data-definition-action]");
      if (button) lifecycleAction(button).catch(console.error);
    });
  }

  window.addEventListener("codex:definition-registry-rendered", (event) => hydrate(event.detail || {}));
  window.addEventListener("DOMContentLoaded", bind);
})();
