function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function scopedHumans(context) {
  const activeMembershipIds = new Set(
    (context?.identity?.memberships || [])
      .filter((item) => (
        item.principal_kind === "human"
        && item.organization_id === context.organizationId
        && (item.workspace_id == null || item.workspace_id === context.workspaceId)
        && item.revoked_at == null
      ))
      .map((item) => item.identity_id),
  );
  return (context?.identity?.humans || [])
    .filter((item) => item.disabled_at == null && activeMembershipIds.has(item.id))
    .sort((a, b) => String(a.display_name || a.id).localeCompare(String(b.display_name || b.id)));
}

function constraintSummary(row) {
  const parts = [];
  if (row.project_ids?.length) parts.push(`projects: ${row.project_ids.join(", ")}`);
  if (row.resource_ids?.length) parts.push(`resources: ${row.resource_ids.join(", ")}`);
  if (row.resource_types?.length) parts.push(`resource types: ${row.resource_types.join(", ")}`);
  if (row.environments?.length) parts.push(`environments: ${row.environments.join(", ")}`);
  return parts.length ? parts.join(" · ") : "No narrower Project/resource constraint on this grant";
}

function sourceLabel(row) {
  if (row.source_type === "delegation") return `delegation ${row.source_id}`;
  return `direct binding ${row.source_id}`;
}

function assignmentRows(result) {
  const items = result?.assignments || [];
  if (!items.length) return '<div class="workspace-state">No matching operational Role binding or delegation in this scope.</div>';
  return items.map((item) => `
    <article class="administration-access-assignment" data-access-source="${esc(item.source_type)}">
      <strong>${esc(item.role_id)}</strong>
      <span>${esc(item.source_type === "delegation" ? "Delegated" : "Direct")}</span>
      <small>${esc(item.source_id)}${item.project_ids?.length ? ` · projects ${esc(item.project_ids.join(", "))}` : " · all Projects in definition scope"}</small>
    </article>
  `).join("");
}

function permissionRows(result, repositoryId = "") {
  let items = result?.permission_matrix || [];
  if (repositoryId) {
    items = items.filter((row) => (
      !(row.resource_ids || []).length
      || row.resource_ids.includes(repositoryId)
      || (row.resource_types || []).includes("repository")
    ));
  }
  if (!items.length) return '<div class="workspace-state">No effective operational grants match this view.</div>';
  return items.map((row) => `
    <article class="administration-access-grant" data-capability="${esc(row.capability)}">
      <header>
        <strong>${esc(row.capability)}</strong>
        <span class="product-status-badge status-positive">${esc(row.level)}</span>
      </header>
      <p>${esc(sourceLabel(row))} · role ${esc(row.role_id)} · grant ${esc(row.grant_id)}</p>
      <small>Inheritance: ${esc((row.inheritance_path || []).join(" → ") || row.role_id)}</small>
      <small>${esc(constraintSummary(row))}</small>
    </article>
  `).join("");
}

function subjectRows(result) {
  const items = result?.items || [];
  if (!items.length) {
    return '<div class="workspace-state">No active human identities have matching canonical grants for this selected scope.</div>';
  }
  return items.map((item) => {
    const identity = item.identity || {};
    const sources = (item.assignments || []).map((assignment) => (
      `${assignment.source_type === "delegation" ? "Delegated" : "Direct"} ${assignment.role_id} · ${assignment.source_id}`
    ));
    const capabilities = (item.permission_matrix || []).map((row) => `${row.capability} (${row.level})`);
    return `
      <article class="administration-user-card" data-access-subject="${esc(identity.id)}">
        <header>
          <div>
            <h3>${esc(identity.display_name || identity.id)}</h3>
            <p>${esc(identity.email || "No email")} · human identity ${esc(identity.id)}</p>
          </div>
        </header>
        <p><strong>Authority sources</strong> · ${esc(sources.join(" · ") || "No matching source")}</p>
        <p><strong>Effective grants</strong> · ${esc(capabilities.join(" · ") || "No matching grants")}</p>
      </article>
    `;
  }).join("");
}

function query(params) {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value) search.set(key, value);
  });
  const encoded = search.toString();
  return encoded ? `?${encoded}` : "";
}

export function renderAdministrationAccess(container, { context, api } = {}) {
  if (!container) return;
  if (!context?.allowed) {
    container.innerHTML = '<div class="workspace-state workspace-state-error">Administration access is required.</div>';
    return;
  }

  const humans = scopedHumans(context);
  container.className = "administration-access-surface";
  container.innerHTML = `
    <section class="administration-users-toolbar">
      <div>
        <strong>Effective access</strong>
        <p>Operational access is resolved by the canonical authority service. Direct bindings, delegations and inherited grants are shown separately from membership roles.</p>
      </div>
    </section>
    <div class="administration-users-message" data-access-message hidden></div>
    <section class="administration-user-create">
      <div class="administration-user-create-fields">
        <label><span>User</span>
          <select data-access-identity>
            <option value="">Select a human user…</option>
            ${humans.map((human) => `<option value="${esc(human.id)}">${esc(human.display_name || human.id)}</option>`).join("")}
          </select>
        </label>
        <label><span>Project context</span>
          <select data-access-project><option value="">Organization / Workspace</option></select>
        </label>
        <label><span>Repository view</span>
          <select data-access-repository><option value="">All resources</option></select>
        </label>
      </div>
      <small>Repository registration/binding describes where a repository exists. Authorization to use it comes from canonical Role grants and is shown below.</small>
      <div class="administration-membership-actions">
        <button type="button" data-access-target-load>Who can access this selected scope?</button>
      </div>
      <small>Select a Project, repository, or both. The subject list is computed by the server from canonical Role bindings, delegations and grant constraints.</small>
    </section>
    <section>
      <h3>People with access to selected scope</h3>
      <div data-access-target-results><div class="workspace-state">Choose a Project or repository, then load the canonical access-subject projection.</div></div>
    </section>
    <section>
      <h3>Assignments</h3>
      <div data-access-assignments><div class="workspace-state">Select a user to inspect direct and delegated access.</div></div>
    </section>
    <section>
      <h3>Effective grants</h3>
      <div data-access-grants><div class="workspace-state">Select a user to inspect server-computed effective grants.</div></div>
    </section>
  `;

  const message = container.querySelector("[data-access-message]");
  const identity = container.querySelector("[data-access-identity]");
  const project = container.querySelector("[data-access-project]");
  const repository = container.querySelector("[data-access-repository]");
  const assignments = container.querySelector("[data-access-assignments]");
  const grants = container.querySelector("[data-access-grants]");
  const targetButton = container.querySelector("[data-access-target-load]");
  const targetResults = container.querySelector("[data-access-target-results]");
  let current = null;

  const setMessage = (value, kind = "info") => {
    message.hidden = !value;
    message.dataset.kind = kind;
    message.textContent = value || "";
  };

  const renderResult = () => {
    assignments.innerHTML = assignmentRows(current);
    grants.innerHTML = permissionRows(current, repository.value);
  };

  const loadEffective = async () => {
    if (!identity.value) {
      current = null;
      assignments.innerHTML = '<div class="workspace-state">Select a user to inspect direct and delegated access.</div>';
      grants.innerHTML = '<div class="workspace-state">Select a user to inspect server-computed effective grants.</div>';
      return;
    }
    setMessage("Loading canonical effective access…");
    try {
      current = await api(`/api/authority/effective${query({ identity_id: identity.value, project_id: project.value })}`);
      setMessage("");
      renderResult();
    } catch (error) {
      current = null;
      setMessage(error?.message || "Unable to load canonical effective access.", "error");
      renderResult();
    }
  };

  Promise.all([
    api("/api/projects"),
    api("/api/resources?resource_type=repository&lifecycle=active"),
  ]).then(([projects, resources]) => {
    const projectItems = Array.isArray(projects) ? projects : projects?.items || [];
    project.innerHTML = [
      '<option value="">Organization / Workspace</option>',
      ...projectItems.map((item) => `<option value="${esc(item.id)}">${esc(item.name || item.id)}</option>`),
    ].join("");
    const resourceItems = resources?.items || [];
    repository.innerHTML = [
      '<option value="">All resources</option>',
      ...resourceItems.map((item) => `<option value="${esc(item.id)}">${esc(item.name || item.id)}</option>`),
    ].join("");
  }).catch((error) => setMessage(error?.message || "Unable to load access scope.", "error"));

  const loadSubjects = async () => {
    if (!project.value && !repository.value) {
      setMessage("Select a Project or repository before loading who has access.", "error");
      targetResults.innerHTML = '<div class="workspace-state">A Project or repository scope is required for this object-centric view.</div>';
      return;
    }
    setMessage("Loading canonical access subjects…");
    targetButton.disabled = true;
    try {
      const result = await api(`/api/authority/access-subjects${query({
        project_id: project.value,
        resource_id: repository.value,
      })}`);
      setMessage("");
      targetResults.innerHTML = subjectRows(result);
    } catch (error) {
      setMessage(error?.message || "Unable to load canonical access subjects.", "error");
      targetResults.innerHTML = '<div class="workspace-state">No access projection is available until the canonical request succeeds.</div>';
    } finally {
      targetButton.disabled = false;
    }
  };

  identity.addEventListener("change", () => void loadEffective());
  project.addEventListener("change", () => void loadEffective());
  repository.addEventListener("change", renderResult);
  targetButton.addEventListener("click", () => void loadSubjects());
}
