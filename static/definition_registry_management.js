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

  function canApprove(scopeType) {
    if (!actor) return false;
    if (actor.principal_kind === "service") {
      return scopeType === "global"
        ? (actor.service_scopes || []).includes("definitions:global-approve")
        : (actor.service_scopes || []).includes("definitions:approve");
    }
    const elevated = ["mfa", "local_trusted"].includes(actor.assurance)
      && (actor.roles || []).some((role) => ["owner", "admin", "approver"].includes(role));
    if (!elevated) return false;
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
    const approvable = canApprove(record.scope_type);
    if (!manageable && !approvable) {
      return '<small>Lifecycle mutation/approval unavailable for the current actor/assurance.</small>';
    }
    const actions = [];
    if (["draft", "validated"].includes(record.lifecycle)) {
      if (manageable) {
        actions.push(`<button type="button" class="ghost-button" data-definition-action="validate" data-record-id="${escapeHtml(record.record_id)}">Validate schema</button>`);
      }
      if (approvable) {
        actions.push(`<button type="button" class="ghost-button" data-definition-action="approve" data-record-id="${escapeHtml(record.record_id)}">Record publication approval</button>`);
      }
      if (manageable) {
        actions.push(`<button type="button" class="ghost-button" data-definition-action="publish" data-record-id="${escapeHtml(record.record_id)}">Publish revision</button>`);
      }
    }
    if (manageable && record.lifecycle !== "quarantined") {
      actions.push(`<button type="button" class="ghost-button" data-definition-action="quarantine" data-record-id="${escapeHtml(record.record_id)}">Quarantine</button>`);
    }
    const active = activeFor(record);
    if (manageable && active && active.record_id !== record.record_id) {
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

  async function publicationAssessment(record) {
    return apiRequest(
      `/api/definitions/${encodeURIComponent(record.record_id)}/publication-assessment`,
    );
  }

  async function approveRecord(record) {
    let assessment;
    try {
      assessment = await publicationAssessment(record);
    } catch (error) {
      setStatus(`Publication assessment failed: ${error.message}`);
      return;
    }
    const reasons = assessment.reasons?.length
      ? assessment.reasons.join(" · ")
      : "No sensitive expansion detected; approval is optional.";
    const reference = window.prompt(
      `Approval reference for ${record.definition_id} r${record.revision}.\n\nClassifier: ${reasons}`,
      "",
    );
    if (reference === null || !reference.trim()) return;
    const reason = window.prompt(
      "Approval reason (this becomes durable evidence tied to the candidate checksum and current active revision):",
      "",
    );
    if (reason === null || !reason.trim()) return;
    if (!window.confirm(
      `Record publication approval as ${actor?.identity_id || "current actor"}? A sensitive publication still requires a different identity to perform the final publish.`,
    )) return;
    try {
      const response = await apiRequest(
        `/api/definitions/${encodeURIComponent(record.record_id)}/publication-approvals`,
        {
          method: "POST",
          body: JSON.stringify({
            reference: reference.trim(),
            reason: reason.trim(),
          }),
        },
      );
      const count = response.record?.publication_approvals?.length || 0;
      setStatus(`Recorded durable publication approval for ${record.definition_id} r${record.revision}; ${count} approval(s) now attached.`);
      document.getElementById("refresh-definitions")?.click();
    } catch (error) {
      setStatus(`Publication approval failed: ${error.message}`);
    }
  }

  async function publishRecord(record) {
    const active = activeFor(record);
    let assessment = null;
    try {
      assessment = await publicationAssessment(record);
    } catch (error) {
      setStatus(`Publication assessment failed: ${error.message}`);
      return;
    }
    const independentApprovals = (assessment.approvals || []).filter(
      (item) => item.approved_by !== actor?.identity_id,
    );
    if (assessment.requires_independent_approval && !independentApprovals.length) {
      setStatus(
        `Sensitive publication requires independent approval before publish: ${(assessment.reasons || []).join(" · ")}`,
      );
      return;
    }
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
    const gate = assessment.requires_independent_approval
      ? ` Sensitive expansion classifier: ${(assessment.reasons || []).join(" · ")}. Independent approval evidence: ${independentApprovals.length}.`
      : " No sensitive authority expansion was detected by the code-owned classifier.";
    if (!window.confirm(
      `Publish ${record.kind}:${record.definition_id} r${record.revision}?${impact}${gate} Publication changes canonical runtime definition resolution; code-owned security invariants are unchanged.`,
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
      if (error.detail?.code === "definition_approval_required" && error.detail?.record_id) {
        setStatus(
          `Rollback prepared draft ${error.detail.record_id}, but activation requires independent approval: ${(error.detail.reasons || []).join(" · ")}. Refresh, record approval on that draft, then publish it.`,
        );
        document.getElementById("refresh-definitions")?.click();
      } else {
        setStatus(`Definition rollback failed: ${error.message}`);
      }
    }
  }

  async function mutate(button) {
    const record = records.find((item) => item.record_id === button.dataset.recordId);
    if (!record) return;
    const action = button.dataset.definitionAction;
    button.disabled = true;
    try {
      if (action === "validate") await validateRecord(record);
      else if (action === "approve") await approveRecord(record);
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

  async function exportDefinitions() {
    try {
      const documentValue = await apiRequest("/api/definitions/export");
      const host = document.getElementById("definition-transfer-document");
      if (host) host.value = JSON.stringify(documentValue, null, 2);
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
      setStatus(`Import document is not valid JSON: ${error.message}`);
      return;
    }
    const imported = Array.isArray(documentValue?.records) ? documentValue.records : [];
    if (!imported.length) {
      setStatus("Import document must contain at least one definition record.");
      return;
    }
    const blocked = imported.filter((record) => !canManage(record.scope_type));
    if (blocked.length) {
      const scopes = [...new Set(blocked.map((record) => record.scope_type))].join(", ");
      setStatus(`Current actor/assurance cannot import every requested scope (${scopes}); nothing was submitted.`);
      return;
    }
    if (!window.confirm(
      `Import ${imported.length} versioned definition record(s) as new inactive drafts? All records are revalidated server-side and none are published automatically.`,
    )) return;
    try {
      const response = await apiRequest("/api/definitions/import", {
        method: "POST",
        body: JSON.stringify({ document: documentValue }),
      });
      setStatus(`Imported ${response.count} record(s) as inactive draft revisions.`);
      document.getElementById("refresh-definitions")?.click();
    } catch (error) {
      setStatus(`Definition import failed: ${error.message}`);
    }
  }

  function bind() {
    document.getElementById("definition-draft-scope")?.addEventListener("change", updateDraftScopeState);
    document.getElementById("create-definition-draft")?.addEventListener("click", () => createDraft().catch(console.error));
    document.getElementById("export-definitions")?.addEventListener("click", () => exportDefinitions().catch(console.error));
    document.getElementById("import-definitions")?.addEventListener("click", () => importDefinitions().catch(console.error));
    document.getElementById("definition-registry-list")?.addEventListener("click", (event) => {
      const button = event.target.closest?.("[data-definition-action]");
      if (button) mutate(button).catch(console.error);
    });
    loadActor().catch(console.error);
  }

  window.addEventListener("codex:definition-registry-rendered", (event) => hydrate(event.detail || {}));
  window.addEventListener("DOMContentLoaded", bind);
})();
