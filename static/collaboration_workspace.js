import { request } from "./api_client.js";
import {
  identityChip,
  metadataGrid,
  statePanel,
  statusBadge,
  timeline,
} from "./workspace_components.js";

const state = { profiles: [], teams: [], skills: [], loaded: false };

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

function agentCard(profile) {
  const article = el("article", "collab-card collab-agent-card");
  const head = el("div", "collab-card-head");
  const identity = agentIdentity(profile);
  head.append(identity);
  article.appendChild(head);
  if (profile.description) article.appendChild(el("p", "collab-description", profile.description));
  const manageSkills = el("a", "collab-manage-link", "Manage");
  manageSkills.href = projectPageHref("skills");
  manageSkills.dataset.manageAgentSkills = profile.profile_id;
  manageSkills.setAttribute("aria-label", `Manage Skills for ${profile.name || profile.profile_id}`);
  const skillManagement = el("span", "collab-skill-management");
  skillManagement.append(
    document.createTextNode((profile.skill_refs || profile.skillRefs || []).map(refId).filter(Boolean).join(", ") || "None"),
    manageSkills,
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

function teamCard(team, profilesById) {
  const article = el("article", "collab-card collab-team-card");
  article.appendChild(teamIdentity(team));
  if (team.description) article.appendChild(el("p", "collab-description", team.description));

  const leader = profilesById.get(team.leader_profile_id);
  const members = team.members || [];
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
    agents.appendChild(statePanel({ kind: "empty", title: "No Agent Profiles", detail: "No visible canonical Agent Profiles exist in this workspace." }));
  } else {
    state.profiles.forEach((profile) => agents.appendChild(agentCard(profile)));
  }

  const profilesById = new Map(state.profiles.map((profile) => [profile.profile_id, profile]));
  if (!state.teams.length) {
    teams.appendChild(statePanel({ kind: "empty", title: "No Teams", detail: "No visible canonical Agent Teams exist in this workspace." }));
  } else {
    state.teams.forEach((team) => teams.appendChild(teamCard(team, profilesById)));
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
      <div><h2>Agent Profiles & Teams</h2><p>Stable collaborator identity, teams, skills and canonical work context.</p></div>
      <button type="button" class="ghost-button" data-collab-refresh>Refresh</button>
    </div>
    <div class="collab-status" data-collab-status role="status" aria-live="polite">Not loaded.</div>
    <section class="collab-section"><h3>Agents</h3><div class="collab-grid" data-collab-agents></div></section>
    <section class="collab-section"><h3>Teams</h3><div class="collab-grid" data-collab-teams></div></section>
    <section class="collab-section"><h3>Skill consumers</h3><div class="collab-skill-grid" data-collab-skills></div></section>
  `;
  grid.appendChild(card);
  card.querySelector("[data-collab-refresh]").addEventListener("click", () => void refresh(card));
  void refresh(card);
}

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", install, { once: true });
else install();
