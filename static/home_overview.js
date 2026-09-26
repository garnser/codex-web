import { request as apiRequest } from "./api_client.js";
import {
  identityChip,
  statePanel,
  statusBadge,
  surfaceCard,
} from "./workspace_components.js";

const SECTION_LABELS = {
  project_readiness: "Project readiness",
  attention: "Needs attention",
  approvals: "Pending approvals",
  active_work: "Active work",
  blocked_work: "Blocked work",
  recently_completed: "Recently completed",
  agents: "Agents working",
  incidents: "Active incidents",
  goals: "Project goals",
  automations: "Recent automations",
};

let controller = null;
let generation = 0;

function projectId() {
  return document.getElementById("product-project-switcher")?.value
    || document.body?.dataset.projectId
    || new URLSearchParams(location.search).get("project")
    || "home";
}

function itemBody(item) {
  const body = document.createElement("div");
  body.className = "home-overview-item-body";
  if (item.next_action) {
    const next = document.createElement("p");
    next.innerHTML = "<strong>Next:</strong> ";
    next.append(document.createTextNode(item.next_action));
    body.appendChild(next);
  }
  if (item.blocked_reason) {
    const blocked = document.createElement("p");
    blocked.className = "home-overview-blocked";
    blocked.innerHTML = "<strong>Blocked:</strong> ";
    blocked.append(document.createTextNode(item.blocked_reason));
    body.appendChild(blocked);
  }
  if (item.completion_fraction != null) {
    const progress = document.createElement("progress");
    progress.max = 1;
    progress.value = Number(item.completion_fraction) || 0;
    progress.setAttribute("aria-label", `${item.title} completion`);
    body.appendChild(progress);
  }
  return body;
}

function itemFooter(item, enabled) {
  const footer = document.createElement("div");
  if (item.updated_at != null) {
    const time = document.createElement("time");
    time.textContent = new Date(Number(item.updated_at) * 1000).toLocaleString();
    footer.appendChild(time);
  }
  if (enabled && item.href) {
    const link = document.createElement("a");
    link.className = "ghost-button";
    link.href = item.href;
    link.textContent = "Open";
    footer.appendChild(link);
  }
  return footer;
}

function renderReadinessSection(section) {
  const wrapper = document.createElement("section");
  wrapper.className = "home-overview-section home-overview-readiness";
  wrapper.dataset.homeSection = "project_readiness";
  const heading = document.createElement("header");
  const title = document.createElement("h3");
  title.textContent = SECTION_LABELS.project_readiness;
  heading.append(title, statusBadge(section.status, section.status));
  wrapper.appendChild(heading);
  if (section.status !== "current") {
    wrapper.appendChild(statePanel({
      kind: section.status === "denied" ? "empty" : "degraded",
      title: section.status === "denied" ? "Readiness not available to this identity" : "Project readiness unavailable",
      detail: section.detail || "No next action is shown until canonical readiness can be verified.",
    }));
    return wrapper;
  }
  const readiness = section.items?.[0];
  if (!readiness) {
    wrapper.appendChild(statePanel({ kind: "degraded", title: "Project readiness unavailable", detail: "Canonical readiness returned no Project state." }));
    return wrapper;
  }
  if (readiness.execution_ready) {
    const detail = document.createElement("p");
    detail.textContent = "The Project is execution ready. Start a first task or Thread; readiness does not itself mean that useful work has completed.";
    const link = document.createElement("a");
    link.className = "ghost-button";
    link.href = "#workspace/threads";
    link.textContent = "Start a first task";
    wrapper.append(detail, link);
    return wrapper;
  }
  const blockers = (readiness.checks || []).filter((check) => check.status === "blocked");
  wrapper.appendChild(statePanel({
    kind: "degraded",
    title: "Project setup needs attention",
    detail: blockers.map((check) => check.message).filter(Boolean).join(" ") || "Execution remains gated until required readiness checks pass.",
  }));
  const link = document.createElement("a");
  link.className = "ghost-button";
  link.href = "#workspace/setup";
  link.textContent = "Review Project setup";
  wrapper.appendChild(link);
  return wrapper;
}

function renderSection(name, section) {
  if (name === "project_readiness") return renderReadinessSection(section);
  const wrapper = document.createElement("section");
  wrapper.className = "home-overview-section";
  wrapper.dataset.homeSection = name;

  const heading = document.createElement("header");
  const title = document.createElement("h3");
  title.textContent = SECTION_LABELS[name] || name;
  heading.append(title, statusBadge(section.status, section.status));
  wrapper.appendChild(heading);

  if (section.status !== "current") {
    const denied = section.status === "denied";
    wrapper.appendChild(statePanel({
      kind: denied ? "empty" : "degraded",
      title: denied ? "Not available to this identity" : "Source degraded",
      detail: section.detail || "This source could not be refreshed. Actions are suppressed until current state is available.",
    }));
    return wrapper;
  }
  if (!section.items?.length) {
    wrapper.appendChild(statePanel({
      kind: "empty",
      title: "Nothing here right now",
      detail: "The canonical source returned no matching current items.",
    }));
    return wrapper;
  }

  const list = document.createElement("div");
  list.className = "home-overview-list";
  for (const item of section.items) {
    const identity = item.owner
      ? { id: item.owner, label: item.owner, kind: name === "agents" ? "agent" : "person" }
      : null;
    list.appendChild(surfaceCard({
      title: item.title || item.id,
      description: item.id,
      status: item.health || item.status,
      identity,
      body: itemBody(item),
      footer: itemFooter(item, true),
      kind: name === "attention" || name === "approvals" ? "attention" : "default",
    }));
  }
  wrapper.appendChild(list);
  return wrapper;
}

function renderPayload(host, payload) {
  host.innerHTML = "";
  const header = document.createElement("header");
  header.className = "home-overview-header";
  const titleBox = document.createElement("div");
  const eyebrow = document.createElement("small");
  eyebrow.textContent = "Current Project";
  const title = document.createElement("h2");
  title.textContent = payload.project.name;
  const path = document.createElement("p");
  path.textContent = payload.project.path;
  titleBox.append(eyebrow, title, path);
  header.append(titleBox, statusBadge(payload.status));
  host.appendChild(header);

  if (payload.status === "partial") {
    host.appendChild(statePanel({
      kind: "degraded",
      title: "Home is partially degraded",
      detail: `Unavailable sections: ${payload.degraded_sections.join(", ")}. Current sections remain usable.`,
    }));
  }

  const grid = document.createElement("div");
  grid.className = "home-overview-grid";
  for (const name of [
    "project_readiness",
    "attention",
    "approvals",
    "active_work",
    "blocked_work",
    "agents",
    "incidents",
    "goals",
    "automations",
    "recently_completed",
  ]) {
    if (payload.sections[name]) grid.appendChild(renderSection(name, payload.sections[name]));
  }
  host.appendChild(grid);
}

export async function renderHomeOverview(host, selectedProjectId = projectId()) {
  if (!(host instanceof HTMLElement)) return;
  controller?.abort();
  controller = new AbortController();
  const requestGeneration = ++generation;
  const requestedProject = selectedProjectId || projectId();
  host.replaceChildren(statePanel({
    kind: "loading",
    title: "Loading current workspace",
    detail: "Reading bounded canonical summaries.",
    busy: true,
  }));
  try {
    const payload = await apiRequest(
      `/api/home?project_id=${encodeURIComponent(requestedProject)}`,
      { signal: controller.signal },
    );
    if (requestGeneration !== generation || requestedProject !== projectId()) return;
    renderPayload(host, payload);
  } catch (error) {
    if (error?.name === "AbortError" || requestGeneration !== generation) return;
    host.replaceChildren(statePanel({
      kind: "error",
      title: "Home could not load",
      detail: error.message,
    }));
  }
}

export function refreshHomeIfVisible() {
  const host = document.querySelector("[data-home-overview]");
  const panel = host?.closest("[data-product-workspace-panel]");
  if (host && panel && !panel.hidden) renderHomeOverview(host, projectId());
}
