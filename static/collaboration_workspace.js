import { request } from "./api_client.js";
import {
  identityChip,
  metadataGrid,
  statePanel,
  statusBadge,
  timeline,
} from "./workspace_components.js";
import { managementActions, openEditor } from "./collaboration_management.js";

const state = { profiles: [], teams: [], skills: [], loaded: false };
let activeCollaborationPage = "agents";

function applyCollaborationPage(card, page = activeCollaborationPage) {
  activeCollaborationPage = ["agent-profiles", "teams"].includes(page) ? page : "agents";
  const visibility = {
    agents: activeCollaborationPage !== "teams",
    teams: activeCollaborationPage !== "agent-profiles",
    skills: activeCollaborationPage === "agents",
  };
  card.querySelector('[data-collab-section="agents"]')?.toggleAttribute("hidden", !visibility.agents);
  card.querySelector('[data-collab-section="teams"]')?.toggleAttribute("hidden", !visibility.teams);
  card.querySelector('[data-collab-section="skills"]')?.toggleAttribute("hidden", !visibility.skills);
  const heading = card.querySelector("[data-collab-heading-title]");
  const detail = card.querySelector("[data-collab-heading-detail]");
  if (heading) heading.textContent = activeCollaborationPage === "agent-profiles"
    ? "Agent Profiles"
    : activeCollaborationPage === "teams" ? "Teams / Squads" : "Agent Profiles & Teams";
  if (detail) detail.textContent = activeCollaborationPage === "agent-profiles"
    ? "Reusable individual agent identities, lifecycle and execution preferences."
    : activeCollaborationPage === "teams"
      ? "Bounded delegation groups with leaders, membership, routing and execution policy."
      : "Stable collaborator identity, teams, skills and canonical work context.";
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = String(text);
  return node;
}

function projectPageHref(page) {
  const path = window.location.pathname;
  const route = path.match(/^(\/codex)?\/projects\/([^/]+)/);
  const prefix = route?.[1] || (path.startsWith("/codex") ? "/codex" : "");
  const projectId = route?.[2]
    ? decodeURIComponent(route[2])
    : document.body?.dataset.activeProject || "home";
  return `${prefix}/projects/${encodeURIComponent(projectId)}/${page}`;
}

function lifecycle(item) {
  return String(item?.lifecycle || "unknown").toLowerCase();
}

function refId(ref) {
  return ref?.definition_id || ref?.definitionId || ref?.record_id || ref?.recordId || "";
}

function skillReference(skill) {
  const definitionId = skill?.skillId || skill?.definition_id || skill?.definitionId || "";
  const reference = { definition_id: definitionId };
  const recordId = skill?.record_id || skill?.recordId;
  if (recordId) reference.record_id = recordId;
  if (skill?.revision != null) reference.revision = skill.revision;
  return reference;
}

function openAgentSkillEditor(profile, onChanged) {
  const dialog = document.createElement("dialog");
  dialog.className = "product-section-dialog";
  const selected = new Set((profile.skill_refs || profile.skillRefs || []).map(refId).filter(Boolean));
  dialog.innerHTML = `
    <div class="product-section-dialog-shell">
      <header>
        <div>
          <h2>Skills · ${profile.name || profile.profile_id}</h2>
          <p>Attach or detach Skills from this Agent Profile. Saving creates a new immutable Agent Profile revision.</p>
        </div>
        <button type="button" class="icon-button" data-close aria-label="Close">×</button>
      </header>
      <div class="route-test" data-skill-options></div>
      <label>Change reason <textarea data-reason rows="3" maxlength="1000" placeholder="Why are these Skill assignments changing?"></textarea></label>
      <div class="form-result" data-status hidden aria-live="polite"></div>
      <button type="button" class="primary-button" data-save>Save Skill assignments</button>
    </div>`;
  const options = dialog.querySelector("[data-skill-options]");
  for (const skill of state.skills) {
    const id = refId(skill) || skill.skillId;
    const label = document.createElement("label");
    label.className = "checkbox-line";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.value = id;
    input.checked = selected.has(id);
    input.dataset.skillId = id;
    label.append(input, document.createTextNode(` ${skill.name || id} · revision ${skill.revision ?? "current"}`));
    options.appendChild(label);
  }
  if (!state.skills.length) {
    options.appendChild(statePanel({ kind: "empty", title: "No Skills available", detail: "Create a Skill before attaching it to this Agent Profile." }));
  }
  const close = () => dialog.close();
  dialog.querySelector("[data-close]").addEventListener("click", close);
  dialog.querySelector("[data-save]").addEventListener("click", async (event) => {
    const reason = dialog.querySelector("[data-reason]").value.trim();
    const status = dialog.querySelector("[data-status]");
    if (!reason) {
      status.hidden = false;
      status.textContent = "A change reason is required.";
      status.classList.add("workspace-state-error");
      return;
    }
    const button = event.currentTarget;
    button.disabled = true;
    const checked = new Set(Array.from(options.querySelectorAll("input:checked"), (input) => input.dataset.skillId));
    const refs = state.skills.filter((skill) => checked.has(refId(skill) || skill.skillId)).map(skillReference);
    try {
      const result = await request(`/api/agent-profiles/${encodeURIComponent(profile.profile_id)}`, {
        method: "PATCH",
        body: JSON.stringify({ skill_refs: refs, reason }),
      });
      await onChanged?.(result?.item);
      dialog.close();
    } catch (error) {
      status.hidden = false;
      status.textContent = error.message || "Skill assignment change rejected.";
      status.classList.add("workspace-state-error");
      button.disabled = false;
    }
  });
  document.body.appendChild(dialog);
  dialog.addEventListener("close", () => dialog.remove(), { once: true });
  dialog.showModal();
}


function openTeamMembersEditor(team, onChanged) {
  const dialog = document.createElement("dialog");
  dialog.className = "product-section-dialog";
  const existingMembers = new Map((team.members || []).map((member) => [member.profile_id, member]));
  const selected = new Set(existingMembers.keys());
  if (team.leader_profile_id) selected.add(team.leader_profile_id);
  dialog.innerHTML = `
    <div class="product-section-dialog-shell">
      <header>
        <div>
          <h2>Members · ${team.name || team.team_id}</h2>
          <p>Add or remove Team members in context. The leader remains selected and changes create a new immutable Team revision.</p>
        </div>
        <button type="button" class="icon-button" data-close aria-label="Close">×</button>
      </header>
      <div class="route-test" data-member-options></div>
      <label>Change reason <textarea data-reason rows="3" maxlength="1000" placeholder="Why is Team membership changing?"></textarea></label>
      <div class="form-result" data-status hidden aria-live="polite"></div>
      <button type="button" class="primary-button" data-save>Save members</button>
    </div>`;
  const options = dialog.querySelector("[data-member-options]");
  for (const profile of state.profiles) {
    const label = document.createElement("label");
    label.className = "checkbox-line";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.dataset.profileId = profile.profile_id;
    input.checked = selected.has(profile.profile_id);
    if (profile.profile_id === team.leader_profile_id) {
      input.disabled = true;
      input.title = "The Team leader cannot be removed from this membership editor.";
    }
    label.append(
      input,
      document.createTextNode(` ${profile.name || profile.profile_id}${profile.profile_id === team.leader_profile_id ? " · leader" : ""}`),
    );
    options.appendChild(label);
  }
  if (!state.profiles.length) {
    options.appendChild(statePanel({
      kind: "empty",
      title: "No Agent Profiles available",
      detail: "Create an Agent Profile before adding Team members.",
    }));
  }

  dialog.querySelector("[data-close]").addEventListener("click", () => dialog.close());
  dialog.querySelector("[data-save]").addEventListener("click", async (event) => {
    const reason = dialog.querySelector("[data-reason]").value.trim();
    const status = dialog.querySelector("[data-status]");
    if (!reason) {
      status.hidden = false;
      status.textContent = "A change reason is required.";
      status.classList.add("workspace-state-error");
      return;
    }
    const button = event.currentTarget;
    button.disabled = true;
    const checked = new Set(Array.from(
      options.querySelectorAll("input:checked"),
      (input) => input.dataset.profileId,
    ));
    if (team.leader_profile_id) checked.add(team.leader_profile_id);
    const members = Array.from(checked, (profileId) => {
      const current = existingMembers.get(profileId);
      return current ? { ...current } : { profile_id: profileId };
    });
    try {
      const result = await request(`/api/agent-teams/${encodeURIComponent(team.team_id)}`, {
        method: "PATCH",
        body: JSON.stringify({ members, reason }),
      });
      await onChanged?.(result?.item);
      dialog.close();
    } catch (error) {
      status.hidden = false;
      status.textContent = error.message || "Team membership change rejected.";
      status.classList.add("workspace-state-error");
      button.disabled = false;
    }
  });
  document.body.appendChild(dialog);
  dialog.addEventListener("close", () => dialog.remove(), { once: true });
  dialog.showModal();
}

function skillConsumers() {
  const consumers = new Map();
  for (const profile of state.profiles) {
    for (const ref of profile.skill_refs || profile.skillRefs || []) {
      const id = refId(ref);
      if (!id) continue;
      if (!consumers.has(id)) consumers.set(id, []);
      consumers.get(id).push(profile);
    }
  }
  return consumers;
}

function agentIdentity(profile) {
  return identityChip({
    id: profile.profile_id,
    label: profile.name || profile.profile_id,
    kind: "agent",
    status: lifecycle(profile),
  });
}

function teamIdentity(team) {
  return identityChip({
    id: team.team_id,
    label: team.name || team.team_id,
    kind: "team",
    status: lifecycle(team),
  });
}

function executionSummary(host, payload) {
  host.replaceChildren();
  const items = payload?.items || [];
  const summary = el("div", "collab-execution-summary");
  summary.append(
    metadataGrid([
      { label: "Active", value: payload?.activeCount ?? 0 },
      { label: "Recent", value: items.length },
      { label: "Available", value: payload?.available === false ? "No" : "Yes" },
    ]),
  );
  host.appendChild(summary);
  if (!items.length) {
    host.appendChild(statePanel({
      kind: "empty",
      title: "No recent executions",
      detail: "No canonical execution assignments are available for this Agent.",
    }));
    return;
  }
  const rows = items.slice(0, 10).map((item) => ({
    label: item.status || "execution",
    detail: [
      item.projectId ? `Project ${item.projectId}` : "",
      item.executionId ? `Execution ${item.executionId}` : "",
      item.workerId ? `Worker ${item.workerId}` : "",
    ].filter(Boolean).join(" · "),
    time: item.updatedAt ? new Date(Number(item.updatedAt) * 1000).toLocaleString() : "",
  }));
  host.appendChild(timeline(rows));
}

async function loadAgentDetails(profile, details) {
  if (details.dataset.loaded === "true" || details.dataset.loading === "true") return;
  details.dataset.loading = "true";
  const target = details.querySelector(".collab-agent-details");
  target.replaceChildren(statePanel({ kind: "loading", title: "Loading Agent context…", busy: true }));
  const projectId = document.body?.dataset.projectId || new URLSearchParams(location.search).get("project") || "";
  try {
    const [executions, access] = await Promise.all([
      request(`/api/agent-profiles/${encodeURIComponent(profile.profile_id)}/executions?limit=10`),
      request(`/api/agent-profiles/${encodeURIComponent(profile.profile_id)}/access${projectId ? `?project_id=${encodeURIComponent(projectId)}` : ""}`).catch(() => null),
    ]);
    target.replaceChildren();
    if (access?.decision) {
      const accessBox = el("section", "collab-access");
      accessBox.append(
        el("strong", "", "Access / authority"),
        statusBadge(access.decision.allowed ? "approved" : "denied", access.decision.allowed ? "Allowed" : "Denied"),
        el("small", "", (access.decision.reasons || []).join(" · ") || "Canonical access decision"),
      );
      target.appendChild(accessBox);
    }
    const executionHost = el("section", "collab-agent-executions");
    executionHost.appendChild(el("h4", "", "Current / recent execution work"));
    executionSummary(executionHost, executions);
    target.appendChild(executionHost);

    const executionPolicy = el("details", "collab-secondary");
    executionPolicy.innerHTML = "<summary>Execution policy details</summary>";
    executionPolicy.appendChild(metadataGrid([
      { label: "Execution profile", value: profile.execution_profile_id },
      { label: "Model class", value: profile.model_policy?.model_class },
      { label: "Provider preference", value: (profile.runtime_policy?.preferred_provider_ids || []).join(", ") },
      { label: "Runtime preference", value: (profile.runtime_policy?.preferred_runtime_ids || []).join(", ") },
      { label: "Sandbox", value: profile.sandbox_requirement || profile.runtime_policy?.required_sandbox_profile },
      { label: "Max concurrency", value: profile.budgets?.max_concurrency },
    ]));
    target.appendChild(executionPolicy);
    details.dataset.loaded = "true";
  } catch (error) {
    target.replaceChildren(statePanel({
      kind: "degraded",
      title: "Agent execution context unavailable",
      detail: error.message || "Canonical execution history could not be loaded.",
    }));
  } finally {
    details.dataset.loading = "false";
  }
}

function agentCard(profile, onChanged) {
  const article = el("article", "collab-card collab-agent-card");
  const head = el("div", "collab-card-head");
  const identity = agentIdentity(profile);
  const teamRefs = state.teams.filter((team) => (
    team.leader_profile_id === profile.profile_id
    || (team.members || []).some((member) => member.profile_id === profile.profile_id)
  ));
  head.append(identity, managementActions("profile", profile, {
    usage: teamRefs.length ? `Referenced by ${teamRefs.length} Team(s): ${teamRefs.map((team) => team.name || team.team_id).join(", ")}.` : "No Team references found.",
    onChanged,
  }));
  article.appendChild(head);
  if (profile.description) article.appendChild(el("p", "collab-description", profile.description));
  const editSkills = el("button", "ghost-button collab-manage-skills", "Edit skills");
  editSkills.type = "button";
  editSkills.dataset.manageAgentSkills = profile.profile_id;
  editSkills.setAttribute("aria-label", `Edit Skills for ${profile.name || profile.profile_id}`);
  editSkills.addEventListener("click", () => openAgentSkillEditor(profile, onChanged));
  const browseSkills = el("a", "collab-manage-link", "Browse registry");
  browseSkills.href = projectPageHref("skills");
  const skillManagement = el("span", "collab-skill-management");
  skillManagement.append(
    document.createTextNode((profile.skill_refs || profile.skillRefs || []).map(refId).filter(Boolean).join(", ") || "None"),
    editSkills,
    browseSkills,
  );
  article.appendChild(metadataGrid([
    { label: "Role", value: profile.role_id },
    { label: "Owner", value: profile.owner_identity_id },
    { label: "Revision", value: profile.revision },
    { label: "Skills", node: skillManagement },
  ]));
  const details = el("details", "collab-agent-context");
  const summary = el("summary", "", "Work, access and execution context");
  const body = el("div", "collab-agent-details");
  body.appendChild(el("small", "", "Open to load bounded canonical execution history and access."));
  details.append(summary, body);
  details.addEventListener("toggle", () => {
    if (details.open) void loadAgentDetails(profile, details);
  });
  article.appendChild(details);
  return article;
}

function teamCard(team, profilesById, onChanged) {
  const article = el("article", "collab-card collab-team-card");
  const leader = profilesById.get(team.leader_profile_id);
  const members = team.members || [];
  const head = el("div", "collab-card-head");
  head.append(teamIdentity(team), managementActions("team", team, {
    usage: `${members.length} member(s); leader ${team.leader_profile_id || "unset"}.`,
    onChanged,
  }));
  article.appendChild(head);
  if (team.description) article.appendChild(el("p", "collab-description", team.description));

  const rosterHeader = el("div", "section-title");
  rosterHeader.appendChild(el("h4", "", "Members"));
  const manageMembers = el("button", "ghost-button", "+ Add / manage members");
  manageMembers.type = "button";
  manageMembers.dataset.manageTeamMembers = team.team_id;
  manageMembers.setAttribute("aria-label", `Add or remove members for ${team.name || team.team_id}`);
  manageMembers.addEventListener("click", () => openTeamMembersEditor(team, onChanged));
  rosterHeader.appendChild(manageMembers);
  article.appendChild(rosterHeader);

  const roster = el("div", "collab-roster");
  const leaderRow = el("div", "collab-roster-row");
  leaderRow.append(el("span", "", "Leader"), leader ? agentIdentity(leader) : el("strong", "", team.leader_profile_id || "—"));
  roster.appendChild(leaderRow);
  for (const member of members) {
    const row = el("div", "collab-roster-row");
    const profile = profilesById.get(member.profile_id);
    row.append(
      el("span", "", member.role || "Member"),
      profile ? agentIdentity(profile) : el("strong", "", member.profile_id),
    );
    if ((member.capability_tags || []).length) {
      row.appendChild(el("small", "", member.capability_tags.join(" · ")));
    }
    roster.appendChild(row);
  }
  article.appendChild(roster);
  article.appendChild(metadataGrid([
    { label: "Revision", value: team.revision },
    { label: "Max participants", value: team.budgets?.max_participants },
    { label: "Parallel executions", value: team.budgets?.max_parallel_executions },
    { label: "Escalation", value: team.escalation_target },
  ]));
  const rules = el("details", "collab-secondary");
  rules.innerHTML = "<summary>Routing / delegation rules</summary>";
  rules.appendChild(metadataGrid([
    { label: "Allowed identities", value: (team.allowed_identity_ids || []).join(", ") || "Tenant policy" },
    { label: "Allowed roles", value: (team.allowed_role_ids || []).join(", ") || "Tenant policy" },
    { label: "Max handoffs", value: team.budgets?.max_handoffs },
    { label: "Coordinator rounds", value: team.budgets?.max_coordinator_rounds },
    { label: "Instructions", value: refId(team.instructions_ref) || "—" },
  ]));
  article.appendChild(rules);
  return article;
}

function skillUsageCard(skill, consumers) {
  const id = skill.skill_id || skill.skillId || skill.definition_id || skill.id || skill.name;
  const definition = skill.skill || skill.definition || skill;
  const article = el("article", "collab-skill-usage");
  const top = el("div", "collab-skill-usage-head");
  top.append(
    el("strong", "", definition.name || id),
    statusBadge(definition.lifecycle || skill.definitionLifecycle || "active"),
  );
  article.append(top, el("small", "", `Revision ${skill.revision || skill.active_revision || "—"} · used by ${consumers.length} Agent${consumers.length === 1 ? "" : "s"}`));
  if (consumers.length) {
    const chips = el("div", "collab-consumer-list");
    consumers.forEach((profile) => chips.appendChild(agentIdentity(profile)));
    article.appendChild(chips);
  }
  const provenance = definition.provenance || skill.provenance;
  if (provenance) article.appendChild(el("small", "collab-provenance", `Source: ${provenance.source_type || "manual"}${provenance.source_ref ? ` · ${provenance.source_ref}` : ""}`));
  return article;
}

function render(card) {
  const agents = card.querySelector("[data-collab-agents]");
  const teams = card.querySelector("[data-collab-teams]");
  const skills = card.querySelector("[data-collab-skills]");
  agents.replaceChildren();
  teams.replaceChildren();
  skills.replaceChildren();

  if (!state.profiles.length) {
    const empty = statePanel({ kind: "empty", title: "No Agent Profiles", detail: "No visible canonical Agent Profiles exist in this workspace." });
    const create = el("button", "primary-button", "+ Create Agent Profile");
    create.type = "button";
    create.addEventListener("click", () => openEditor("profile", null, { onChanged: () => refresh(card) }));
    empty.appendChild(create);
    agents.appendChild(empty);
  } else {
    state.profiles.forEach((profile) => agents.appendChild(agentCard(profile, () => refresh(card))));
  }

  const profilesById = new Map(state.profiles.map((profile) => [profile.profile_id, profile]));
  if (!state.teams.length) {
    const empty = statePanel({ kind: "empty", title: "No Teams", detail: "No visible canonical Agent Teams exist in this workspace." });
    const create = el("button", "primary-button", "+ Create Team");
    create.type = "button";
    create.addEventListener("click", () => openEditor("team", null, { onChanged: () => refresh(card) }));
    empty.appendChild(create);
    teams.appendChild(empty);
  } else {
    state.teams.forEach((team) => teams.appendChild(teamCard(team, profilesById, () => refresh(card))));
  }

  const consumers = skillConsumers();
  if (!state.skills.length) {
    skills.appendChild(statePanel({ kind: "empty", title: "No Skills", detail: "No visible canonical Skills are available." }));
  } else {
    state.skills.forEach((skill) => {
      const id = skill.skill_id || skill.skillId || skill.definition_id || skill.id || "";
      skills.appendChild(skillUsageCard(skill, consumers.get(id) || []));
    });
  }
}

async function refresh(card) {
  const status = card.querySelector("[data-collab-status]");
  status.textContent = "Loading canonical Agent, Team and Skill state…";
  card.setAttribute("aria-busy", "true");
  try {
    const [profiles, teams, skills] = await Promise.all([
      request("/api/agent-profiles?include_archived=true"),
      request("/api/agent-teams?include_archived=true"),
      request("/api/skills?include_drafts=true"),
    ]);
    state.profiles = profiles?.items || [];
    state.teams = teams?.items || [];
    state.skills = skills?.items || [];
    state.loaded = true;
    render(card);
    status.textContent = `${state.profiles.length} Agents · ${state.teams.length} Teams · ${state.skills.length} Skills`;
  } catch (error) {
    status.textContent = error.message || "Collaboration workspace unavailable";
    const host = card.querySelector("[data-collab-agents]");
    host.replaceChildren(statePanel({
      kind: "degraded",
      title: "Collaboration state unavailable",
      detail: status.textContent,
    }));
  } finally {
    card.removeAttribute("aria-busy");
  }
}

function install() {
  if (document.getElementById("collaboration-workspace-card")) return;
  const grid = document.querySelector("#developer-panel .developer-grid") || document.body;
  const card = el("section", "developer-card collaboration-workspace");
  card.id = "collaboration-workspace-card";
  card.innerHTML = `
    <div class="collab-heading">
      <div><h2 data-collab-heading-title>Agent Profiles & Teams</h2><p data-collab-heading-detail>Stable collaborator identity, teams, skills and canonical work context.</p></div>
      <button type="button" class="ghost-button" data-collab-refresh>Refresh</button>
    </div>
    <div class="collab-status" data-collab-status role="status" aria-live="polite">Not loaded.</div>
    <section class="collab-section" data-collab-section="agents"><div class="section-title"><h3>Agents</h3><button type="button" class="ghost-button" data-create-agent>Create Agent Profile</button></div><div class="collab-grid" data-collab-agents></div></section>
    <section class="collab-section" data-collab-section="teams"><div class="section-title"><h3>Teams</h3><button type="button" class="ghost-button" data-create-team>Create Team</button></div><div class="collab-grid" data-collab-teams></div></section>
    <section class="collab-section" data-collab-section="skills"><h3>Skill consumers</h3><div class="collab-skill-grid" data-collab-skills></div></section>
  `;
  grid.appendChild(card);
  card.querySelector("[data-collab-refresh]").addEventListener("click", () => void refresh(card));
  card.querySelector("[data-create-agent]").addEventListener("click", () => openEditor("profile", null, { onChanged: () => refresh(card) }));
  card.querySelector("[data-create-team]").addEventListener("click", () => openEditor("team", null, { onChanged: () => refresh(card) }));
  window.addEventListener("codex:project-workspace-page", (event) => {
    if (event.detail?.workspace === "agents") applyCollaborationPage(card, event.detail?.page);
  });
  const routePage = window.location.pathname.match(/\/projects\/[^/]+\/(agent-profiles|teams)\/?$/)?.[1] || "agents";
  applyCollaborationPage(card, routePage);
  void refresh(card);
}

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", install, { once: true });
else install();
