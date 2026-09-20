(async () => {
  const BASE = window.location.pathname.startsWith("/codex") ? "/codex" : "";
  const { request: apiRequest } = await import(`${BASE}/static/api_client.js`);

  const $ = (id) => document.getElementById(id);
  const esc = (value) => String(value ?? "")
    .replaceAll("&", "&amp;").replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;").replaceAll('"', "&quot;");

  let plan = null;

  function threadHtml(item) {
    const difference = item.authority_difference || {};
    const summary = (difference.summary || []).map((line) => `<li>${esc(line)}</li>`).join("");
    return `<div class="comm-entry">
      <strong>${esc(item.name || item.thread_id)} · ${esc(item.disposition)}</strong>
      <small>Thread: ${esc(item.thread_id)} · reason: ${esc(item.reason_code)}</small>
      <small>${esc(item.reason)}</small>
      <small>Sandbox: ${esc(item.current_sandbox || "unset")} → ${esc(item.proposed_sandbox || "unset")} · profile: ${esc(item.current_execution_profile_id || "legacy/unprofiled")} → ${esc(item.proposed_execution_profile_id || "none")}</small>
      <small>Repository: ${esc(item.current_repository_resource_id || "none")} → ${esc(item.proposed_repository_key || item.proposed_repository_resource_id || "none")}</small>
      ${summary ? `<details><summary>Authority difference${difference.requires_operator_approval ? " · approval required" : ""}</summary><ul>${summary}</ul></details>` : ""}
    </div>`;
  }

  function renderPlan(value) {
    plan = value;
    const repositories = value.repositories || [];
    const threads = value.threads || [];
    $("legacy-migration-status").textContent =
      `Plan ${value.id} · ${repositories.length} repositories · ${threads.length} threads · ${value.blockers?.length || 0} blockers${value.requires_approval ? " · approval required" : ""}.`;
    $("legacy-migration-repositories").innerHTML = repositories.map((item) => `<div class="comm-entry">
      <strong>${esc(item.name)} · ${esc(item.key)}</strong>
      <small>${esc(item.absolute_path)}</small>
      <small>Canonical Resource: ${esc(item.existing_resource_id || "create on apply")} · default candidate: ${item.default_candidate ? "yes" : "no"}</small>
    </div>`).join("") || "<small>No Git repositories discovered.</small>";
    $("legacy-migration-threads").innerHTML = threads.map(threadHtml).join("") || "<small>No persistent Project threads discovered.</small>";
    $("legacy-migration-rollback").textContent = value.rollback_boundary || "";
    $("apply-legacy-migration").disabled = Boolean(value.blockers?.length);
  }

  async function loadProjects() {
    const projects = await apiRequest("/api/projects");
    $("legacy-migration-project").innerHTML = projects.map((project) =>
      `<option value="${esc(project.id)}">${esc(project.name)} · ${esc(project.id)}</option>`
    ).join("");
  }

  async function dryRun() {
    const projectId = $("legacy-migration-project").value;
    if (!projectId) return;
    $("legacy-migration-status").textContent = "Computing read-only migration plan…";
    try {
      const value = await apiRequest(
        `/api/projects/${encodeURIComponent(projectId)}/legacy-migration/dry-run`,
        { method: "POST" },
      );
      renderPlan(value);
    } catch (error) {
      $("legacy-migration-status").textContent = error.message || String(error);
    }
  }

  async function apply() {
    if (!plan) return;
    $("legacy-migration-status").textContent = "Applying approved migration plan…";
    try {
      const result = await apiRequest(
        `/api/projects/${encodeURIComponent(plan.project_id)}/legacy-migration/apply`,
        {
          method: "POST",
          body: JSON.stringify({
            plan,
            approve_material_authority_changes: $("legacy-migration-approve-authority").checked,
            compatibility_window_seconds: Number($("legacy-migration-window").value || 604800),
          }),
        },
      );
      $("legacy-migration-status").textContent =
        `Migration ${result.status} · ${result.applied_operation_ids?.length || 0} durable operation checkpoints · ${result.compatibility_mappings?.length || 0} compatibility mappings.`;
      await dryRun();
    } catch (error) {
      $("legacy-migration-status").textContent = error.message || String(error);
    }
  }

  $("dry-run-legacy-migration")?.addEventListener("click", dryRun);
  $("apply-legacy-migration")?.addEventListener("click", apply);
  $("legacy-migration-project")?.addEventListener("change", () => {
    plan = null;
    $("legacy-migration-status").textContent = "Run dry-run to inspect migration.";
    $("legacy-migration-repositories").innerHTML = "";
    $("legacy-migration-threads").innerHTML = "";
  });

  try {
    await loadProjects();
  } catch (error) {
    $("legacy-migration-status").textContent = error.message || String(error);
  }
})();
