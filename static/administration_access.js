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
    <article class="administration-access-assignment" data-access-source="${esc(item.source_type)}" data-access-source-id="${esc(item.source_id)}">
      <strong>${esc(item.role_id)}</strong>
      <span>${esc(item.source_type === "delegation" ? "Delegated" : "Direct")}</span>
      <small>${esc(item.source_id)}${item.project_ids?.length ? ` · projects ${esc(item.project_ids.join(", "))}` : " · all Projects in definition scope"}</small>
      ${item.source_type === "binding" ? `<button type="button" data-access-remove-binding="${esc(item.source_id)}">Remove direct assignment</button>` : ""}
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
      <div class="administration-user-create-fields">
        <label><span>Direct operational Role</span>
          <select data-access-role>
            <option value="">Select a canonical Role…</option>
          </select>
        </label>
        <label><span>Change reason</span>
          <input data-access-reason type="text" placeholder="Why is this access change needed?" />
        </label>
      </div>
      <div class="administration-membership-actions">
        <button type="button" data-access-add-binding>Stage direct assignment</button>
        <button type="button" data-access-target-load>Who can access this selected scope?</button>
      </div>
      <small>New authority can require independent approval before it becomes effective. Removing a direct assignment is confirmed and still goes through the canonical versioned authority catalog.</small>
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
  const role = container.querySelector("[data-access-role]");
  const reason = container.querySelector("[data-access-reason]");
  const addButton = container.querySelector("[data-access-add-binding]");
  const targetButton = container.querySelector("[data-access-target-load]");
  const targetResults = container.querySelector("[data-access-target-results]");
  let current = null;
  let targetLoaded = false;

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

  const loadRoles = async () => {
    role.disabled = true;
    try {
      const result = await api(`/api/authority/roles${query({ project_id: project.value })}`);
      const items = result?.items || [];
      role.innerHTML = [
        '<option value="">Select a canonical Role…</option>',
        ...items.map((item) => `<option value="${esc(item.id)}" title="${esc(item.description || "")}">${esc(item.name || item.id)} · ${esc(item.id)}</option>`),
      ].join("");
    } catch (error) {
      role.innerHTML = '<option value="">Canonical Roles unavailable</option>';
      setMessage(error?.message || "Unable to load canonical operational Roles.", "error");
    } finally {
      role.disabled = false;
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
    return loadRoles();
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
      targetLoaded = true;
    } catch (error) {
      setMessage(error?.message || "Unable to load canonical access subjects.", "error");
      targetResults.innerHTML = '<div class="workspace-state">No access projection is available until the canonical request succeeds.</div>';
    } finally {
      targetButton.disabled = false;
    }
  };

  const refreshAfterMutation = async () => {
    await loadEffective();
    if (targetLoaded && (project.value || repository.value)) await loadSubjects();
  };

  const mutationReason = () => String(reason.value || "").trim();

  addButton.addEventListener("click", async () => {
    if (!identity.value || !role.value) {
      setMessage("Select a user and canonical Role before staging a direct assignment.", "error");
      return;
    }
    const why = mutationReason();
    if (!why) {
      setMessage("A change reason is required for auditable access mutation.", "error");
      reason.focus();
      return;
    }
    addButton.disabled = true;
    setMessage("Staging canonical direct assignment…");
    try {
      const result = await api("/api/authority/direct-bindings", {
        method: "POST",
        body: JSON.stringify({
          identity_id: identity.value,
          role_id: role.value,
          project_id: project.value || null,
          reason: why,
        }),
      });
      if (result?.status === "pending_approval") {
        setMessage("Direct assignment is staged and pending independent publication approval.", "info");
      } else if (result?.status === "already_effective") {
        setMessage("That direct assignment is already effective.", "info");
      } else {
        setMessage("Direct assignment published.", "info");
      }
      await refreshAfterMutation();
    } catch (error) {
      setMessage(error?.message || "Unable to stage canonical direct assignment.", "error");
    } finally {
      addButton.disabled = false;
    }
  });

  assignments.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-access-remove-binding]");
    if (!button) return;
    const why = mutationReason();
    if (!why) {
      setMessage("A change reason is required before removing direct access.", "error");
      reason.focus();
      return;
    }
    const bindingId = button.dataset.accessRemoveBinding;
    if (!window.confirm(`Remove direct authority binding ${bindingId} from this scope?`)) return;
    button.disabled = true;
    setMessage("Removing canonical direct assignment…");
    try {
      const result = await api(`/api/authority/direct-bindings/${encodeURIComponent(bindingId)}`, {
        method: "DELETE",
        body: JSON.stringify({
          project_id: project.value || null,
          reason: why,
        }),
      });
      setMessage(
        result?.status === "published"
          ? "Direct assignment removed and authority reduction published."
          : "Direct assignment removal staged.",
        "info",
      );
      await refreshAfterMutation();
    } catch (error) {
      setMessage(error?.message || "Unable to remove canonical direct assignment.", "error");
      button.disabled = false;
    }
  });

  identity.addEventListener("change", () => void loadEffective());
  project.addEventListener("change", () => {
    targetLoaded = false;
    targetResults.innerHTML = '<div class="workspace-state">Load the canonical access-subject projection for this scope.</div>';
    void Promise.all([loadRoles(), loadEffective()]);
  });
  repository.addEventListener("change", () => {
    targetLoaded = false;
    renderResult();
  });
  targetButton.addEventListener("click", () => void loadSubjects());
}
