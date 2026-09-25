(async () => {
  const BASE = window.location.pathname.startsWith("/codex") ? "/codex" : "";
  const { request: apiRequest } = await import(`${BASE}/static/api_client.js`);
  const SCOPE_PRECEDENCE = ["deployment", "global", "organization", "workspace", "project", "resource"];
  let specs = [];
  let records = [];
  let projects = [];
  let resources = [];
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
  function setStatus(message) {
    const host = document.getElementById("configuration-status");
    if (host) { host.hidden = false; host.textContent = message; }
  }
  function listText(values) {
    return values?.length ? values.map(escapeHtml).join(", ") : "none";
  }
  function specFor(key) { return specs.find((item) => item.key === key) || null; }
  function valueText(value, spec) {
    if (spec?.value_kind === "secret_ref") {
      return `Secret reference only: ${value?.secret_id || "missing"}`;
    }
    if (spec?.value_kind === "definition_ref") {
      return `Definition reference: ${value?.definition_id || "missing"} @ ${value?.revision || "missing"}`;
    }
    return JSON.stringify(value);
  }
  function scopeText(record) {
    return ["deployment", "global"].includes(record.scope_type)
      ? record.scope_type
      : `${record.scope_type}:${record.scope_id || "missing"}`;
  }
  function matchesSpec(spec) {
    const search = (document.getElementById("configuration-search")?.value || "").trim().toLowerCase();
    const category = document.getElementById("configuration-category-filter")?.value || "";
    if (category && spec.category !== category) return false;
    if (!search) return true;
    const haystack = [
      spec.key,
      spec.description,
      spec.category,
      spec.value_kind,
      ...(spec.allowed_scopes || []),
      ...(spec.allowed_values || []),
    ].filter(Boolean).join(" ").toLowerCase();
    return haystack.includes(search);
  }
  function renderSpecs() {
    const host = document.getElementById("configuration-spec-list");
    if (!host) return;
    const visible = specs.filter(matchesSpec);
    host.innerHTML = visible.map((spec) => `<details class="comm-entry">
      <summary><strong>${escapeHtml(spec.key)} · ${escapeHtml(spec.category || "Advanced")} · ${escapeHtml(spec.value_kind)}${spec.feature_flag ? " · feature flag" : ""}</strong></summary>
      ${spec.description ? `<small>${escapeHtml(spec.description)}</small>` : ""}
      <small>Default: ${escapeHtml(valueText(spec.default, spec))} · Required: ${spec.required ? "yes" : "no"} · Editable: ${spec.editable ? "yes" : "no"} · Sensitive: ${spec.sensitive ? "yes (reference metadata only)" : "no"}</small>
      <small>Allowed scopes: ${listText(spec.allowed_scopes)} · Hot reloadable: ${spec.hot_reloadable ? "yes" : "no"} · Startup only: ${spec.startup_only ? "yes" : "no"}</small>
      <small>Allowed values: ${listText(spec.allowed_values)} · Minimum: ${escapeHtml(spec.minimum ?? "none")} · Maximum: ${escapeHtml(spec.maximum ?? "none")}</small>
      <small>Feature flag: ${spec.feature_flag ? "yes" : "no"} · Kill switch capable: ${spec.kill_switch_capable ? "yes" : "no"} · Grants authority: ${spec.grants_authority ? "yes" : "no"}</small>
      <small>Runtime configuration does not grant RBAC, authority, approval, entitlement, or secret access.</small>
    </details>`).join("") || '<div class="comm-entry"><strong>No specs match the filters.</strong></div>';
  }
  function populateCategoryFilter() {
    const filter = document.getElementById("configuration-category-filter");
    if (!filter) return;
    const previous = filter.value;
    const categories = [...new Set(specs.map((spec) => spec.category || "Advanced"))].sort();
    filter.innerHTML = '<option value="">All categories</option>' + categories.map((category) => (
      `<option value="${escapeHtml(category)}">${escapeHtml(category)}</option>`
    )).join("");
    if (categories.includes(previous)) filter.value = previous;
  }
  function matches(record) {
    const search = (document.getElementById("configuration-search")?.value || "").trim().toLowerCase();
    const scope = document.getElementById("configuration-scope-filter")?.value || "";
    const state = document.getElementById("configuration-state-filter")?.value || "";
    const category = document.getElementById("configuration-category-filter")?.value || "";
    const spec = specFor(record.key);
    if (scope && record.scope_type !== scope) return false;
    if (state && record.state !== state) return false;
    if (category && spec?.category !== category) return false;
    if (!search) return true;
    const haystack = [
      record.key,
      spec?.description,
      spec?.category,
      spec?.value_kind,
      record.id,
      record.scope_type,
      record.scope_id,
      record.state,
      record.created_by,
      record.published_by,
      record.create_reason,
      record.publish_reason,
      valueText(record.value, spec),
      record.feature_targeting ? JSON.stringify(record.feature_targeting) : "",
    ].filter(Boolean).join(" ").toLowerCase();
    return haystack.includes(search);
  }
  function targetingHtml(record) {
    const targeting = record.feature_targeting;
    if (!targeting) return "<small>Feature targeting: none.</small>";
    return `<small>Feature targeting: ${escapeHtml(targeting.percentage)}% · cohorts: ${listText(targeting.cohorts)} · owner: ${escapeHtml(targeting.owner || "none")} · expires: ${timeText(targeting.expires_at)}</small>`;
  }
  function renderRecord(record) {
    const spec = specFor(record.key);
    const links = [
      record.supersedes_id ? `supersedes ${record.supersedes_id}` : null,
      record.superseded_by_id ? `superseded by ${record.superseded_by_id}` : null,
      record.rollback_of_id ? `rollback of ${record.rollback_of_id}` : null,
    ].filter(Boolean);
    return `<details class="comm-entry" data-configuration-record="${escapeHtml(record.id)}">
      <summary><strong>${escapeHtml(record.key)} · r${escapeHtml(record.revision)} · ${escapeHtml(record.state)} · ${escapeHtml(scopeText(record))}</strong></summary>
      <small>Record: ${escapeHtml(record.id)} · Type: ${escapeHtml(spec?.value_kind || "unknown")} · Value: ${escapeHtml(valueText(record.value, spec))}</small>
      <small>Created by: ${escapeHtml(record.created_by)} · ${timeText(record.created_at)}${record.create_reason ? ` · reason: ${escapeHtml(record.create_reason)}` : ""}</small>
      <small>Published by: ${escapeHtml(record.published_by || "none")} · ${timeText(record.published_at)}${record.publish_reason ? ` · reason: ${escapeHtml(record.publish_reason)}` : ""}</small>
      ${targetingHtml(record)}
      <small>Force-disabled kill switch: ${record.force_disabled ? "yes" : "no"} · Hot reloadable: ${spec?.hot_reloadable ? "yes" : "no"} · Startup only: ${spec?.startup_only ? "yes" : "no"}</small>
      ${links.length ? `<small>Revision links: ${escapeHtml(links.join(" · "))}</small>` : ""}
      <button type="button" class="ghost-button" data-configuration-impact="${escapeHtml(record.id)}">Load impact preview</button>
      <div id="configuration-impact-${escapeHtml(record.id)}"></div>
      <div data-configuration-management-host="${escapeHtml(record.id)}"></div>
    </details>`;
  }
  function renderRecords() {
    const host = document.getElementById("configuration-record-list");
    if (!host) return;
    const visible = records.filter(matches);
    host.innerHTML = visible.map(renderRecord).join("")
      || '<div class="comm-entry"><strong>No records match the filters.</strong></div>';
    setStatus(`${visible.length} of ${records.length} revisions · ${specs.length} specs · ${SCOPE_PRECEDENCE.join(" → ")}.`);
  }
  function populateResolveControls() {
    const key = document.getElementById("configuration-resolve-key");
    if (key) {
      const previous = key.value;
      key.innerHTML = specs.map((spec) => (
        `<option value="${escapeHtml(spec.key)}">${escapeHtml(spec.key)} · ${escapeHtml(spec.value_kind)}</option>`
      )).join("");
      if (specs.some((spec) => spec.key === previous)) key.value = previous;
    }
    const project = document.getElementById("configuration-resolve-project");
    if (project) {
      const previous = project.value;
      project.innerHTML = '<option value="">No project context</option>' + projects.map((item) => (
        `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)} · ${escapeHtml(item.id)}</option>`
      )).join("");
      if (projects.some((item) => item.id === previous)) project.value = previous;
    }
    const resource = document.getElementById("configuration-resolve-resource");
    if (resource) {
      const previous = resource.value;
      resource.innerHTML = '<option value="">No resource context</option>' + resources.map((item) => (
        `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)} · ${escapeHtml(item.resource_type)} · ${escapeHtml(item.id)}</option>`
      )).join("");
      if (resources.some((item) => item.id === previous)) resource.value = previous;
    }
  }
  function renderEffective(effective) {
    const host = document.getElementById("configuration-resolve-result");
    if (!host) return;
    const spec = specFor(effective.key);
    host.innerHTML = `<div class="comm-entry">
      <strong>${escapeHtml(effective.key)} · source ${escapeHtml(effective.source)} · reason ${escapeHtml(effective.reason)}</strong>
      <small>Effective value: ${escapeHtml(valueText(effective.value, spec))}</small>
      <small>Record: ${escapeHtml(effective.record_id || "default/unset")} · revision: ${escapeHtml(effective.revision ?? "none")} · scope: ${escapeHtml(effective.scope_type || "default")}${effective.scope_id ? `:${escapeHtml(effective.scope_id)}` : ""}</small>
      <small>Published by: ${escapeHtml(effective.published_by || "none")} · ${timeText(effective.published_at)}${effective.publish_reason ? ` · reason: ${escapeHtml(effective.publish_reason)}` : ""}</small>
      <small>Hot reloadable: ${effective.hot_reloadable ? "yes" : "no"} · Startup only: ${effective.startup_only ? "yes" : "no"} · Feature flag: ${effective.feature_flag ? "yes" : "no"}</small>
    </div>`;
  }
  async function resolveEffective() {
    const key = document.getElementById("configuration-resolve-key")?.value || "";
    if (!key) return setStatus("Choose a registered configuration key.");
    const projectId = document.getElementById("configuration-resolve-project")?.value || null;
    const resourceId = document.getElementById("configuration-resolve-resource")?.value || null;
    const subjectId = document.getElementById("configuration-resolve-subject")?.value.trim() || null;
    const cohort = document.getElementById("configuration-resolve-cohort")?.value.trim() || null;
    try {
      const response = await apiRequest("/api/configuration/resolve", {
        method: "POST",
        body: JSON.stringify({
          key,
          context: {
            project_id: projectId,
            resource_id: resourceId,
            subject_id: subjectId,
            cohort,
          },
        }),
      });
      renderEffective(response.effective);
    } catch (error) {
      const host = document.getElementById("configuration-resolve-result");
      if (host) host.innerHTML = `<div class="comm-entry"><strong>Resolution failed</strong><small>${escapeHtml(error.message)}</small></div>`;
    }
  }
  async function loadImpact(button) {
    const recordId = button.dataset.configurationImpact;
    if (!recordId) return;
    const host = document.getElementById(`configuration-impact-${recordId}`);
    if (!host) return;
    button.disabled = true;
    host.innerHTML = "<small>Loading canonical impact preview...</small>";
    try {
      const impact = await apiRequest(`/api/configuration/${encodeURIComponent(recordId)}/impact`);
      const overrides = impact.more_specific_overrides || [];
      host.innerHTML = `<div class="comm-entry">
        <small>Scope: ${escapeHtml(impact.scope?.type)}${impact.scope?.id ? `:${escapeHtml(impact.scope.id)}` : ""} · Hot reloadable: ${impact.hot_reloadable ? "yes" : "no"} · Startup only: ${impact.startup_only ? "yes" : "no"}</small>
        <small>More-specific published overrides: ${overrides.length ? overrides.map((item) => `${item.scope_type}${item.scope_id ? `:${item.scope_id}` : ""}@r${item.revision}`).map(escapeHtml).join(" · ") : "none"}</small>
      </div>`;
    } catch (error) {
      host.innerHTML = `<small>Impact preview unavailable: ${escapeHtml(error.message)}</small>`;
    } finally {
      button.disabled = false;
    }
  }
  async function refresh() {
    setStatus("Loading canonical typed configuration...");
    let projectError = null;
    let resourceError = null;
    try {
      const [specResponse, recordResponse] = await Promise.all([
        apiRequest("/api/configuration/specs"),
        apiRequest("/api/configuration/records"),
        apiRequest("/api/projects")
          .then((value) => { projects = Array.isArray(value) ? value : (value.items || []); })
          .catch((error) => { projects = []; projectError = error.message; }),
        apiRequest("/api/resources")
          .then((value) => { resources = value.items || []; })
          .catch((error) => { resources = []; resourceError = error.message; }),
      ]);
      specs = specResponse.items || [];
      records = recordResponse.items || [];
      populateCategoryFilter();
      renderSpecs();
      populateResolveControls();
      renderRecords();
      const warnings = [
        projectError ? `project labels unavailable: ${projectError}` : null,
        resourceError ? `resource labels unavailable: ${resourceError}` : null,
      ].filter(Boolean);
      if (warnings.length) setStatus(`${records.length} visible configuration revision(s). ${warnings.join(" · ")}`);
      window.dispatchEvent(new CustomEvent("codex:configuration-state-rendered", {
        detail: { specs, records, projects, resources },
      }));
    } catch (error) {
      setStatus(`Configuration administration unavailable: ${error.message}`);
      const host = document.getElementById("configuration-record-list");
      if (host) host.innerHTML = "";
    }
  }
  function bind() {
    const panel = document.getElementById("developer-panel");
    document.getElementById("refresh-configuration")?.addEventListener("click", refresh);
    document.getElementById("refresh-developer")?.addEventListener("click", refresh);
    document.getElementById("configuration-search")?.addEventListener("input", () => {
      renderSpecs();
      renderRecords();
    });
    document.getElementById("configuration-category-filter")?.addEventListener("change", () => {
      renderSpecs();
      renderRecords();
    });
    document.getElementById("configuration-scope-filter")?.addEventListener("change", renderRecords);
    document.getElementById("configuration-state-filter")?.addEventListener("change", renderRecords);
    document.getElementById("resolve-configuration")?.addEventListener("click", () => resolveEffective().catch(console.error));
    document.getElementById("configuration-record-list")?.addEventListener("click", (event) => {
      const button = event.target.closest?.("[data-configuration-impact]");
      if (button) loadImpact(button).catch(console.error);
    });
    panel?.addEventListener("toggle", () => {
      if (panel.open) refresh().catch(console.error);
    });
    if (panel?.open) refresh().catch(console.error);
  }
  window.addEventListener("DOMContentLoaded", bind);
})();
