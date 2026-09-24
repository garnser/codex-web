const ROLE_PRESENTATION = Object.freeze({
  owner: "Full organization/workspace administration and authority.",
  admin: "Administrative management within the assigned scope.",
  approver: "May satisfy approval requirements when canonical policy selects this role.",
  member: "Standard scoped membership without administrative authority.",
});

function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function scopedMemberships(context, { includeRevoked = true } = {}) {
  const memberships = context?.identity?.memberships || [];
  return memberships.filter((item) => (
    item.principal_kind === "human"
    && item.organization_id === context.organizationId
    && (item.workspace_id == null || item.workspace_id === context.workspaceId)
    && (includeRevoked || item.revoked_at == null)
  ));
}

function humanById(context) {
  return new Map((context?.identity?.humans || []).map((item) => [item.id, item]));
}

function providerLabel(human) {
  const providers = [...new Set((human?.external_links || []).map((link) => link.provider).filter(Boolean))];
  return providers.length ? providers.join(", ") : "local/canonical";
}

function membershipScopeLabel(item, context) {
  return item.workspace_id == null
    ? `Organization · ${context.organizationId}`
    : `Workspace · ${item.workspace_id}`;
}

function membershipStatus(item, human) {
  if (human?.disabled_at != null) return "disabled";
  if (item.revoked_at != null) return "revoked";
  return "active";
}

function errorMessage(error) {
  if (error?.status === 401) {
    return "Authentication or CSRF verification is required before this change can be applied.";
  }
  if (error?.status === 403) {
    return "Administrator MFA/step-up is required, or canonical policy denied this change.";
  }
  if (error?.status === 409) {
    return "The membership changed concurrently or is no longer mutable. Refresh canonical state and retry.";
  }
  return error?.message || "The canonical identity operation failed.";
}

function roleOptions(item) {
  const selected = new Set(item.roles || []);
  return Object.entries(ROLE_PRESENTATION)
    .map(([role, description]) => (
      `<option value="${esc(role)}" ${selected.has(role) ? "selected" : ""} title="${esc(description)}">${esc(role)}</option>`
    ))
    .join("");
}

function membershipRow(item, human, context) {
  const status = membershipStatus(item, human);
  const active = status === "active";
  return `
    <article class="administration-membership-row" data-membership-id="${esc(item.id)}" data-membership-status="${esc(status)}">
      <header>
        <div>
          <strong>${esc(membershipScopeLabel(item, context))}</strong>
          <small>${esc(item.id)}</small>
        </div>
        <span class="product-status-badge status-${active ? "positive" : "neutral"}">${esc(status)}</span>
      </header>
      <label>
        <span>Direct roles</span>
        <select multiple data-membership-roles ${active ? "" : "disabled"}>
          ${roleOptions(item)}
        </select>
      </label>
      <div class="administration-role-help">
        ${(item.roles || []).map((role) => `<p><strong>${esc(role)}</strong> · ${esc(ROLE_PRESENTATION[role] || "Canonical scoped role.")}</p>`).join("")}
      </div>
      <div class="administration-membership-actions">
        <button type="button" data-save-membership ${active ? "" : "disabled"}>Save roles</button>
        <button type="button" data-revoke-membership ${active ? "" : "disabled"}>Revoke membership</button>
      </div>
      ${item.revoked_at != null ? `<small>Revoked by ${esc(item.revoked_by || "unknown")} · ${esc(new Date(item.revoked_at * 1000).toISOString())}</small>` : ""}
    </article>
  `;
}

function userCard(human, memberships, context) {
  const status = human.disabled_at == null ? "active" : "disabled";
  return `
    <article class="administration-user-card" data-human-id="${esc(human.id)}" data-human-status="${esc(status)}">
      <header>
        <div>
          <h3>${esc(human.display_name)}</h3>
          <p>${esc(human.email || "No email")} · identity source: ${esc(providerLabel(human))}</p>
        </div>
        <span class="product-status-badge status-${status === "active" ? "positive" : "neutral"}">${esc(status)}</span>
      </header>
      <small>Human identity ${esc(human.id)}</small>
      <section class="administration-user-memberships">
        <h4>Direct memberships and roles</h4>
        ${memberships.length
          ? memberships.map((item) => membershipRow(item, human, context)).join("")
          : '<div class="workspace-state">No membership in the active administration scope.</div>'}
      </section>
    </article>
  `;
}

function groupSection(context) {
  const groups = (context?.identity?.teams || []).filter((team) => (
    team.organization_id === context.organizationId
    && (team.workspace_id == null || team.workspace_id === context.workspaceId)
  ));
  return `
    <section class="administration-human-groups">
      <header>
        <div>
          <h3>Human groups</h3>
          <p>Identity groups for people. These are not Agent Teams/Squads and do not represent agent delegation.</p>
        </div>
      </header>
      ${groups.length
        ? groups.map((group) => `
          <article>
            <strong>${esc(group.name)}</strong>
            <small>${esc(group.id)} · ${group.workspace_id ? `workspace ${esc(group.workspace_id)}` : "organization-wide"}</small>
            <p>${(group.member_identity_ids || []).length} human member(s)</p>
          </article>
        `).join("")
        : '<div class="workspace-state">No human groups in this scope.</div>'}
    </section>
  `;
}

export function administrationUserView(context, query = "") {
  const humans = humanById(context);
  const memberships = scopedMemberships(context);
  const byHuman = new Map();
  for (const membership of memberships) {
    const items = byHuman.get(membership.identity_id) || [];
    items.push(membership);
    byHuman.set(membership.identity_id, items);
  }
  const normalized = query.trim().toLowerCase();
  return [...byHuman.entries()]
    .map(([identityId, items]) => ({ human: humans.get(identityId), memberships: items }))
    .filter((entry) => entry.human)
    .filter(({ human }) => (
      !normalized
      || human.display_name.toLowerCase().includes(normalized)
      || String(human.email || "").toLowerCase().includes(normalized)
      || human.id.toLowerCase().includes(normalized)
    ))
    .sort((a, b) => a.human.display_name.localeCompare(b.human.display_name));
}

export function renderAdministrationUsers(container, {
  context,
  api,
  page = "users",
  query = "",
  confirm = (message) => window.confirm(message),
  onChanged = null,
} = {}) {
  if (!container) return;
  if (!context?.allowed) {
    container.innerHTML = '<div class="workspace-state workspace-state-error">Administration access is required.</div>';
    return;
  }
  const entries = administrationUserView(context, query);
  container.className = "administration-users-surface";
  container.innerHTML = `
    <section class="administration-users-toolbar">
      <div>
        <strong>${page === "memberships" ? "Memberships & roles" : "Users"}</strong>
        <p>Canonical human identities and direct Organization/Workspace authority. Revoked membership never counts as active authority.</p>
      </div>
      <label>
        <span>Search users</span>
        <input type="search" value="${esc(query)}" data-administration-user-search placeholder="Name, email or identity ID" />
      </label>
    </section>
    <div class="administration-users-message" data-administration-users-message hidden></div>
    <section class="administration-user-list" data-administration-user-list>
      ${entries.length
        ? entries.map(({ human, memberships }) => userCard(human, memberships, context)).join("")
        : '<div class="workspace-state">No human users match this active Organization/Workspace scope.</div>'}
    </section>
    ${groupSection(context)}
  `;

  const message = container.querySelector("[data-administration-users-message]");
  const setMessage = (value, kind = "info") => {
    if (!message) return;
    message.hidden = !value;
    message.dataset.kind = kind;
    message.textContent = value || "";
  };

  container.querySelector("[data-administration-user-search]")?.addEventListener("input", (event) => {
    renderAdministrationUsers(container, {
      context,
      api,
      page,
      query: event.target.value,
      confirm,
      onChanged,
    });
    container.querySelector("[data-administration-user-search]")?.focus();
  });

  container.querySelectorAll("[data-save-membership]").forEach((button) => {
    button.addEventListener("click", async () => {
      const row = button.closest("[data-membership-id]");
      const membershipId = row?.dataset.membershipId;
      const roles = [...(row?.querySelector("[data-membership-roles]")?.selectedOptions || [])]
        .map((option) => option.value);
      if (!membershipId || !roles.length) {
        setMessage("Select at least one canonical role.", "error");
        return;
      }
      button.disabled = true;
      setMessage("Saving canonical roles…");
      try {
        await api(`/api/identity/memberships/${encodeURIComponent(membershipId)}`, {
          method: "PATCH",
          body: JSON.stringify({ roles }),
        });
        setMessage("Membership roles updated.", "success");
        await onChanged?.();
      } catch (error) {
        setMessage(errorMessage(error), "error");
      } finally {
        button.disabled = false;
      }
    });
  });

  container.querySelectorAll("[data-revoke-membership]").forEach((button) => {
    button.addEventListener("click", async () => {
      const row = button.closest("[data-membership-id]");
      const membershipId = row?.dataset.membershipId;
      if (!membershipId) return;
      if (!confirm("Revoke this canonical membership and its direct roles?")) return;
      button.disabled = true;
      setMessage("Revoking membership…");
      try {
        await api(`/api/identity/memberships/${encodeURIComponent(membershipId)}`, {
          method: "DELETE",
        });
        setMessage("Membership revoked. Its direct authority is no longer active.", "success");
        await onChanged?.();
      } catch (error) {
        setMessage(errorMessage(error), "error");
      } finally {
        button.disabled = false;
      }
    });
  });
}
