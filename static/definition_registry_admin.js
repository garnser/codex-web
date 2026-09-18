(async () => {
  const BASE = window.location.pathname.startsWith("/codex") ? "/codex" : "";
  const { request: apiRequest } = await import(`${BASE}/static/api_client.js`);
  let records = [];
  let projects = [];
  let schemas = [];

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

  function setStatus(message) {
    const host = document.getElementById("definition-registry-status");
    if (host) {
      host.hidden = false;
      host.textContent = message;
    }
  }

  function definitionKey(record) {
    return `${record.kind}::${record.definition_id}`;
  }

  function scopeText(record) {
    return record.scope_type === "global"
      ? "global"
      : `${record.scope_type}:${record.scope_id || "missing"}`;
  }

  function effectiveText(record) {
    const from = record.effective_from ? timeText(record.effective_from) : "immediate";
    const until = record.effective_until ? timeText(record.effective_until) : "no expiry";
    return `${from} → ${until}`;
  }

  function compatibilityText(record) {
    if (!record.min_engine_version && !record.max_engine_version) return "engine unrestricted";
    return `${record.min_engine_version || "any"} ≤ engine ≤ ${record.max_engine_version || "any"}`;
  }

  function provenanceHtml(record) {
    const links = [
      record.supersedes_record_id ? `supersedes ${record.supersedes_record_id}` : null,
      record.superseded_by_record_id ? `superseded by ${record.superseded_by_record_id}` : null,
      record.rollback_of_record_id ? `rollback of ${record.rollback_of_record_id}` : null,
    ].filter(Boolean);
    const approvals = Object.entries(record.approval_metadata || {})
      .map(([key, value]) => `${key}=${value}`)
      .join(" · ");
    return `<small>Created: ${escapeHtml(record.created_by)} · ${timeText(record.created_at)}${record.create_reason ? ` · reason: ${escapeHtml(record.create_reason)}` : ""}</small>
      <small>Validated: ${escapeHtml(record.validated_by || "none")} · ${timeText(record.validated_at)} · Published: ${escapeHtml(record.published_by || "none")} · ${timeText(record.published_at)}</small>
      ${record.publish_reason ? `<small>Publish/lifecycle reason: ${escapeHtml(record.publish_reason)}</small>` : ""}
      ${approvals ? `<small>Approvals: ${escapeHtml(approvals)}</small>` : ""}
      ${links.length ? `<small>Revision links: ${escapeHtml(links.join(" · "))}</small>` : ""}`;
  }

  function matches(record) {
    const search = (document.getElementById("definition-search")?.value || "").trim().toLowerCase();
    const kind = document.getElementById("definition-kind-filter")?.value || "";
    const scope = document.getElementById("definition-scope-filter")?.value || "";
    const lifecycle = document.getElementById("definition-lifecycle-filter")?.value || "";
    if (kind && record.kind !== kind) return false;
    if (scope && record.scope_type !== scope) return false;
    if (lifecycle && record.lifecycle !== lifecycle) return false;
    if (!search) return true;
    const haystack = [
      record.definition_id,
      record.kind,
      record.record_id,
      record.checksum,
      record.definition_schema_version,
      record.created_by,
      record.validated_by,
      record.published_by,
      record.create_reason,
      record.publish_reason,
      record.scope_type,
      record.scope_id,
      JSON.stringify(record.payload || {}),
    ].filter(Boolean).join(" ").toLowerCase();
    return haystack.includes(search);
  }

  function renderRecord(record) {
    const payload = JSON.stringify(record.payload || {}, null, 2);
    return `<details class="comm-entry" data-definition-record="${escapeHtml(record.record_id)}">
      <summary><strong>${escapeHtml(record.kind)} · ${escapeHtml(record.definition_id)} · r${escapeHtml(record.revision)} · ${escapeHtml(record.lifecycle)}</strong></summary>
      <small>Record: ${escapeHtml(record.record_id)} · Scope: ${escapeHtml(scopeText(record))} · Definition schema: ${escapeHtml(record.definition_schema_version)}</small>
      <small>Checksum: ${escapeHtml(record.checksum)} · Effective: ${escapeHtml(effectiveText(record))} · Compatibility: ${escapeHtml(compatibilityText(record))}</small>
      ${provenanceHtml(record)}
      <pre>${escapeHtml(payload)}</pre>
      <button type="button" class="ghost-button" data-definition-usage="${escapeHtml(record.record_id)}">Load usage/references</button>
      <div id="definition-usage-${escapeHtml(record.record_id)}"></div>
    </details>`;
  }

  function renderRecords() {
    const host = document.getElementById("definition-registry-list");
    if (!host) return;
    const visible = records.filter(matches);
    host.innerHTML = visible.map(renderRecord).join("")
      || '<div class="comm-entry"><strong>No definition revisions match the current filters.</strong></div>';
    setStatus(`${visible.length} of ${records.length} visible definition revision(s). Definitions are versioned data; schema/interpreter/security engines remain code-owned.`);
  }

  function renderBootstrap(status) {
    const host = document.getElementById("definition-bootstrap");
    if (!host) return;
    const registered = schemas.map((item) => `${item.kind}@${item.schema_version}`);
    host.innerHTML = `<div class="comm-entry">
      <strong>Definition Registry · engine ${escapeHtml(status.engine_version)}</strong>
      <small>Visible records: ${escapeHtml(status.records)} · active published: ${escapeHtml(status.active)} · registered schemas: ${registered.length}</small>
      <small>Schemas: ${listText(registered)}</small>
      <small>Published definitions are canonical runtime data. Stored payloads cannot add executable code or weaken code-owned security invariants.</small>
    </div>`;
  }

  function populateControls() {
    const kinds = Array.from(new Set(records.map((record) => record.kind))).sort();
    const kindFilter = document.getElementById("definition-kind-filter");
    if (kindFilter) {
      const current = kindFilter.value;
      kindFilter.innerHTML = '<option value="">All kinds</option>'
        + kinds.map((kind) => `<option value="${escapeHtml(kind)}">${escapeHtml(kind)}</option>`).join("");
      kindFilter.value = kinds.includes(current) ? current : "";
    }

    const keys = Array.from(new Map(
      records.map((record) => [definitionKey(record), record]),
    ).entries()).sort(([left], [right]) => left.localeCompare(right));
    const resolveKey = document.getElementById("definition-resolve-key");
    if (resolveKey) {
      resolveKey.innerHTML = keys.map(([key, record]) => (
        `<option value="${escapeHtml(key)}">${escapeHtml(record.kind)} · ${escapeHtml(record.definition_id)}</option>`
      )).join("");
    }

    const resolveProject = document.getElementById("definition-resolve-project");
    if (resolveProject) {
      resolveProject.innerHTML = '<option value="">No project context</option>'
        + projects.map((project) => (
          `<option value="${escapeHtml(project.id)}">${escapeHtml(project.name)} · ${escapeHtml(project.id)}</option>`
        )).join("");
    }

    const left = document.getElementById("definition-diff-left");
    if (left) {
      const previous = left.value;
      left.innerHTML = records.map((record) => (
        `<option value="${escapeHtml(record.record_id)}">${escapeHtml(record.kind)} · ${escapeHtml(record.definition_id)} · r${escapeHtml(record.revision)} · ${escapeHtml(scopeText(record))} · ${escapeHtml(record.lifecycle)}</option>`
      )).join("");
      if (records.some((record) => record.record_id === previous)) left.value = previous;
    }
    populateDiffRight();
  }

  function populateDiffRight() {
    const left = document.getElementById("definition-diff-left");
    const right = document.getElementById("definition-diff-right");
    if (!left || !right) return;
    const selected = records.find((record) => record.record_id === left.value);
    const previous = right.value;
    const candidates = selected
      ? records.filter((record) => (
          record.kind === selected.kind
          && record.definition_id === selected.definition_id
          && record.record_id !== selected.record_id
        ))
      : [];
    right.innerHTML = candidates.map((record) => (
      `<option value="${escapeHtml(record.record_id)}">${escapeHtml(record.kind)} · ${escapeHtml(record.definition_id)} · r${escapeHtml(record.revision)} · ${escapeHtml(scopeText(record))} · ${escapeHtml(record.lifecycle)}</option>`
    )).join("");
    if (candidates.some((record) => record.record_id === previous)) right.value = previous;
  }

  function renderResolved(record) {
    const host = document.getElementById("definition-resolve-result");
    if (!host) return;
    host.innerHTML = `<div class="comm-entry">
      <strong>Effective: ${escapeHtml(record.kind)} · ${escapeHtml(record.definition_id)} · r${escapeHtml(record.revision)}</strong>
      <small>Record: ${escapeHtml(record.record_id)} · Scope: ${escapeHtml(scopeText(record))} · Checksum: ${escapeHtml(record.checksum)}</small>
      <small>Lifecycle: ${escapeHtml(record.lifecycle)} · Published by: ${escapeHtml(record.published_by || "none")} · ${timeText(record.published_at)}</small>
    </div>`;
  }

  async function resolveEffective() {
    const raw = document.getElementById("definition-resolve-key")?.value || "";
    const separator = raw.indexOf("::");
    if (separator < 1) return;
    const kind = raw.slice(0, separator);
    const definitionId = raw.slice(separator + 2);
    const projectId = document.getElementById("definition-resolve-project")?.value || null;
    try {
      const response = await apiRequest("/api/definitions/resolve", {
        method: "POST",
        body: JSON.stringify({
          definition_id: definitionId,
          kind,
          context: projectId ? { project_id: projectId } : {},
        }),
      });
      renderResolved(response.record);
    } catch (error) {
      const host = document.getElementById("definition-resolve-result");
      if (host) host.innerHTML = `<div class="comm-entry"><strong>Resolution failed</strong><small>${escapeHtml(error.message)}</small></div>`;
    }
  }

  async function loadUsage(button) {
    const recordId = button.dataset.definitionUsage;
    if (!recordId) return;
    const host = document.getElementById(`definition-usage-${recordId}`);
    if (!host) return;
    button.disabled = true;
    host.innerHTML = "<small>Loading canonical usage references...</small>";
    try {
      const response = await apiRequest(
        `/api/definitions/${encodeURIComponent(recordId)}/usage`,
      );
      host.innerHTML = (response.items || []).map((item) => {
        const detail = Object.entries(item)
          .map(([key, value]) => `${key}=${typeof value === "object" ? JSON.stringify(value) : value}`)
          .join(" · ");
        return `<div class="comm-entry"><small>${escapeHtml(detail)}</small></div>`;
      }).join("") || "<small>No tenant-visible canonical usage references.</small>";
    } catch (error) {
      host.innerHTML = `<small>Usage unavailable: ${escapeHtml(error.message)}</small>`;
    } finally {
      button.disabled = false;
    }
  }

  async function compareRevisions() {
    const left = document.getElementById("definition-diff-left")?.value || "";
    const right = document.getElementById("definition-diff-right")?.value || "";
    const host = document.getElementById("definition-diff-result");
    if (!left || !right || !host) return;
    try {
      const response = await apiRequest(
        `/api/definitions/diff?left=${encodeURIComponent(left)}&right=${encodeURIComponent(right)}`,
      );
      host.innerHTML = `<div class="comm-entry">
        <strong>${escapeHtml(response.kind)} · ${escapeHtml(response.definition_id)} · changed: ${response.changed ? "yes" : "no"}</strong>
        <small>Left r${escapeHtml(response.left?.revision)} · ${escapeHtml(response.left?.checksum)} · Right r${escapeHtml(response.right?.revision)} · ${escapeHtml(response.right?.checksum)}</small>
        <small>Changed paths: ${listText(response.changed_paths)}</small>
      </div>`;
    } catch (error) {
      host.innerHTML = `<div class="comm-entry"><strong>Diff unavailable</strong><small>${escapeHtml(error.message)}</small></div>`;
    }
  }

  async function refresh() {
    setStatus("Loading canonical Definition Registry state...");
    let projectError = null;
    try {
      const [schemaResponse, bootstrap, recordResponse] = await Promise.all([
        apiRequest("/api/definitions/schemas"),
        apiRequest("/api/definitions/bootstrap"),
        apiRequest("/api/definitions/records"),
        apiRequest("/api/projects")
          .then((value) => { projects = Array.isArray(value) ? value : (value.items || []); })
          .catch((error) => { projects = []; projectError = error.message; }),
      ]);
      schemas = schemaResponse.items || [];
      records = recordResponse.items || [];
      renderBootstrap(bootstrap);
      populateControls();
      renderRecords();
      if (projectError) {
        setStatus(`${records.length} visible definition revision(s). Project resolution labels unavailable: ${projectError}`);
      }
      window.dispatchEvent(new CustomEvent("codex:definition-registry-rendered", {
        detail: { records, schemas, projects, bootstrap },
      }));
    } catch (error) {
      setStatus(`Definition Registry unavailable: ${error.message}`);
      const host = document.getElementById("definition-registry-list");
      if (host) host.innerHTML = "";
    }
  }

  function bind() {
    const panel = document.getElementById("developer-panel");
    document.getElementById("refresh-definitions")?.addEventListener("click", refresh);
    document.getElementById("refresh-developer")?.addEventListener("click", refresh);
    document.getElementById("definition-search")?.addEventListener("input", renderRecords);
    document.getElementById("definition-kind-filter")?.addEventListener("change", renderRecords);
    document.getElementById("definition-scope-filter")?.addEventListener("change", renderRecords);
    document.getElementById("definition-lifecycle-filter")?.addEventListener("change", renderRecords);
    document.getElementById("definition-diff-left")?.addEventListener("change", populateDiffRight);
    document.getElementById("resolve-definition")?.addEventListener("click", () => resolveEffective().catch(console.error));
    document.getElementById("diff-definitions")?.addEventListener("click", () => compareRevisions().catch(console.error));
    document.getElementById("definition-registry-list")?.addEventListener("click", (event) => {
      const button = event.target.closest?.("[data-definition-usage]");
      if (button) loadUsage(button).catch(console.error);
    });
    panel?.addEventListener("toggle", () => {
      if (panel.open) refresh().catch(console.error);
    });
    if (panel?.open) refresh().catch(console.error);
  }

  window.addEventListener("DOMContentLoaded", bind);
})();
