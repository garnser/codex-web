(async () => {
  const BASE = window.location.pathname.startsWith("/codex") ? "/codex" : "";
  const { focusReference } = await import(`${BASE}/static/reference_links.js`);
  const { projectViewOperation } = await import(`${BASE}/static/project_view_scope.js`);
  const apiRequest = (...args) => projectViewOperation(setStatus).request(...args);
  const { timeText, scopeText, renderRecord } = await import(`${BASE}/static/definition_record_view.js`);
  let records = [];
  let projects = [];
  let schemas = [];
  let generation = 0;
  let controller = null;
  let openedRecord = '';
  function initialProjectId() {
    const match = location.pathname.match(/\/projects\/([^/]+)(?:\/|$)/);
    if (match) { try { return decodeURIComponent(match[1]); } catch { return ''; } }
    return document.body?.dataset.projectId || document.body?.dataset.activeProject
      || new URLSearchParams(location.search).get('project') || '';
  }
  let projectId = initialProjectId();
  function current(requestGeneration, requestedProject) {
    return requestGeneration === generation && requestedProject === projectId;
  }
  function publishCatalog(bootstrap = {}) {
    window.dispatchEvent(new CustomEvent('codex:definition-registry-rendered', {
      detail: { records, schemas, projects, bootstrap, projectId },
    }));
  }
  function clearProject(nextProject) {
    generation += 1;
    controller?.abort();
    projectId = nextProject;
    records = [];
    projects = [];
    for (const id of ['definition-search', 'definition-kind-filter', 'definition-scope-filter', 'definition-lifecycle-filter']) {
      const control = document.getElementById(id);
      if (control) control.value = '';
    }
    for (const id of ['definition-registry-list', 'definition-bootstrap', 'definition-resolve-result', 'definition-diff-result']) {
      document.getElementById(id)?.replaceChildren();
    }
    populateControls();
    publishCatalog();
    setStatus(projectId ? 'Loading definitions for this Project…' : 'Select a Project to inspect definitions.');
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");
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


  function renderRecords() {
    const host = document.getElementById("definition-registry-list");
    if (!host) return;
    const visible = records.filter(matches);
    host.innerHTML = visible.map(renderRecord).join("")
      || '<div class="comm-entry"><strong>No definition revisions match the current filters.</strong></div>';
    const requested = new URLSearchParams(location.search).get('definition_record');
    const target = [...host.querySelectorAll('[data-definition-record]')].find(item => item.dataset.definitionRecord === requested);
    if (target && openedRecord !== requested) {
      openedRecord = requested; target.open = true;
      target.scrollIntoView({ block: 'nearest' });
    }
    setStatus(`${visible.length} of ${records.length} visible definition revision(s). Definitions are versioned data; schema/interpreter/security engines remain code-owned.`);
    focusReference(host);
    publishCatalog();
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
      resolveProject.innerHTML = ''
        + projects.map((project) => (
          `<option value="${escapeHtml(project.id)}">${escapeHtml(project.name)} · ${escapeHtml(project.id)}</option>`
        )).join("");
      resolveProject.value = projectId;
      resolveProject.disabled = true;
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
    if (!projectId) return;
    const requestedProject = projectId;
    const requestGeneration = generation;
    try {
      const response = await apiRequest("/api/definitions/resolve", {
        method: "POST",
        body: JSON.stringify({
          definition_id: definitionId,
          kind,
          context: projectId ? { project_id: projectId } : {},
        }),
      });
      if (!current(requestGeneration, requestedProject)) return;
      renderResolved(response.record);
    } catch (error) {
      if (!current(requestGeneration, requestedProject)) return;
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
    const requestedProject = projectId;
    const requestGeneration = generation;
    try {
      const response = await apiRequest(
        `/api/definitions/diff?left=${encodeURIComponent(left)}&right=${encodeURIComponent(right)}`,
      );
      if (!current(requestGeneration, requestedProject)) return;
      host.innerHTML = `<div class="comm-entry">
        <strong>${escapeHtml(response.kind)} · ${escapeHtml(response.definition_id)} · changed: ${response.changed ? "yes" : "no"}</strong>
        <small>Left r${escapeHtml(response.left?.revision)} · ${escapeHtml(response.left?.checksum)} · Right r${escapeHtml(response.right?.revision)} · ${escapeHtml(response.right?.checksum)}</small>
        <small>Changed paths: ${listText(response.changed_paths)}</small>
      </div>`;
    } catch (error) {
      if (!current(requestGeneration, requestedProject)) return;
      host.innerHTML = `<div class="comm-entry"><strong>Diff unavailable</strong><small>${escapeHtml(error.message)}</small></div>`;
    }
  }

  async function refresh() {
    if (!projectId) { clearProject(''); return; }
    const requestedProject = projectId;
    const requestGeneration = ++generation;
    controller?.abort();
    controller = new AbortController();
    const options = { signal: controller.signal };
    setStatus("Loading canonical Definition Registry state...");
    try {
      const [schemaResponse, bootstrap, recordResponse, project] = await Promise.all([
        apiRequest("/api/definitions/schemas", options),
        apiRequest("/api/definitions/bootstrap", options),
        apiRequest(`/api/definitions/records?project_id=${encodeURIComponent(requestedProject)}`, options),
        apiRequest(`/api/projects/${encodeURIComponent(requestedProject)}`, options),
      ]);
      if (!current(requestGeneration, requestedProject)) return;
      schemas = schemaResponse.items || [];
      records = recordResponse.items || [];
      projects = [project];
      const scopedBootstrap = { ...bootstrap, records: records.length,
        active: records.filter(record => record.lifecycle === 'published').length };
      renderBootstrap(scopedBootstrap);
      populateControls();
      renderRecords();
      publishCatalog(scopedBootstrap);
    } catch (error) {
      if (!current(requestGeneration, requestedProject)) return;
      clearProject(requestedProject);
      setStatus(`Definition Registry unavailable: ${error.message}`);
    }
  }

  function bind() {
    projectId = initialProjectId();
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
    const workspace = document.querySelector('[data-product-workspace-panel="definitions"]');
    if (panel?.open || (workspace && !workspace.hidden)) refresh().catch(console.error);
    window.addEventListener('codex:project-changed', event => {
      const next = String(event.detail?.projectId || '').trim();
      if (next === projectId) return;
      clearProject(next);
      const workspace = document.querySelector('[data-product-workspace-panel="definitions"]');
      if (workspace && !workspace.hidden) refresh().catch(console.error);
    });
  }

  window.addEventListener('codex:definition-registry-request', () => publishCatalog());
  if (document.readyState === 'loading') window.addEventListener("DOMContentLoaded", bind, { once: true });
  else bind();
})();
