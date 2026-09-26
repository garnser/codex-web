const POSITIVE = new Set(["active","ready","healthy","fresh","current","succeeded","verified","available","approved","complete","completed","online"]);
const WARNING = new Set(["pending","degraded","partial","stale","warning","paused","quota","throttled","waiting","running","unknown"]);
const NEGATIVE = new Set(["denied","failed","invalid","blocked","quarantined","unavailable","conflict","revoked","offline","error"]);

export function statusFamily(status) {
  const value = String(status || "unknown").toLowerCase();
  if (POSITIVE.has(value)) return "positive";
  if (WARNING.has(value)) return "warning";
  if (NEGATIVE.has(value)) return "negative";
  return "neutral";
}

function text(value) {
  return value == null || value === "" ? "—" : String(value);
}

export function statusBadge(status, label = null, options = {}) {
  const value = String(status || "unknown").toLowerCase();
  const node = document.createElement("span");
  node.className = ["cw-status-badge", `cw-status-${statusFamily(value)}`, options.className || ""].filter(Boolean).join(" ");
  node.dataset.status = value;
  node.setAttribute("role", "status");
  node.textContent = label || value;
  return node;
}

export function identityChip(identity = {}, options = {}) {
  const node = document.createElement("span");
  const kind = String(identity.kind || identity.type || "identity").toLowerCase();
  node.className = ["cw-identity-chip", `cw-identity-${kind}`, options.className || ""].filter(Boolean).join(" ");
  node.dataset.identityKind = kind;
  if (identity.id) node.dataset.identityId = identity.id;

  const marker = document.createElement("span");
  marker.className = "cw-identity-marker";
  marker.setAttribute("aria-hidden", "true");
  marker.textContent = kind === "agent" ? "A" : kind === "team" ? "T" : kind === "service" ? "S" : "P";

  const label = document.createElement("span");
  label.className = "cw-identity-label";
  label.textContent = identity.label || identity.name || identity.display_name || identity.id || "Unknown identity";
  node.append(marker, label);

  if (identity.status) node.appendChild(statusBadge(identity.status));
  return node;
}

export function objectHeader({ eyebrow = "", title = "", description = "", status = "", identity = null, actions = [] } = {}) {
  const header = document.createElement("header");
  header.className = "cw-object-header";

  const body = document.createElement("div");
  body.className = "cw-object-header-body";
  if (eyebrow) {
    const small = document.createElement("small");
    small.className = "cw-eyebrow";
    small.textContent = eyebrow;
    body.appendChild(small);
  }
  const heading = document.createElement("h2");
  heading.textContent = title;
  body.appendChild(heading);
  if (description) {
    const p = document.createElement("p");
    p.textContent = description;
    body.appendChild(p);
  }

  const meta = document.createElement("div");
  meta.className = "cw-object-header-meta";
  if (identity) meta.appendChild(identityChip(identity));
  if (status) meta.appendChild(statusBadge(status));

  const actionHost = document.createElement("div");
  actionHost.className = "cw-object-header-actions";
  for (const action of actions) {
    if (action instanceof Node) actionHost.appendChild(action);
  }
  meta.appendChild(actionHost);
  header.append(body, meta);
  return header;
}

export function metadataGrid(entries = [], options = {}) {
  const dl = document.createElement("dl");
  dl.className = ["cw-metadata-grid", options.className || ""].filter(Boolean).join(" ");
  for (const entry of entries) {
    const wrapper = document.createElement("div");
    const dt = document.createElement("dt");
    const dd = document.createElement("dd");
    dt.textContent = entry.label || entry.key || "Value";
    if (entry.node instanceof Node) dd.appendChild(entry.node);
    else dd.textContent = text(entry.value);
    wrapper.append(dt, dd);
    dl.appendChild(wrapper);
  }
  return dl;
}

export function actionFeedback({ state = "acknowledged", title = "", detail = "", action = null } = {}) {
  const normalized = ["acknowledged", "in_progress", "succeeded", "failed", "needs_attention"].includes(state)
    ? state
    : "acknowledged";
  const section = document.createElement("section");
  section.className = `cw-action-feedback cw-action-feedback-${normalized}`;
  section.dataset.actionState = normalized;
  section.setAttribute("role", normalized === "failed" ? "alert" : "status");
  section.setAttribute("aria-live", normalized === "failed" ? "assertive" : "polite");
  const heading = document.createElement("strong");
  heading.textContent = title || normalized.replaceAll("_", " ");
  section.appendChild(heading);
  if (detail) {
    const description = document.createElement("p");
    description.textContent = detail;
    section.appendChild(description);
  }
  if (action instanceof Node) section.appendChild(action);
  return section;
}

export function statePanel({ kind = "empty", title = "", detail = "", action = null, busy = false } = {}) {
  const section = document.createElement("section");
  section.className = `cw-state-panel cw-state-${kind}`;
  section.dataset.state = kind;
  section.setAttribute("role", kind === "error" || kind === "offline" ? "alert" : "status");
  if (busy) section.setAttribute("aria-busy", "true");

  const icon = document.createElement("span");
  icon.className = "cw-state-icon";
  icon.setAttribute("aria-hidden", "true");
  icon.textContent = kind === "error" ? "!" : kind === "degraded" ? "△" : kind === "offline" ? "×" : kind === "loading" ? "…" : "○";

  const body = document.createElement("div");
  const strong = document.createElement("strong");
  strong.textContent = title || (kind === "loading" ? "Loading…" : "Nothing to show");
  body.appendChild(strong);
  if (detail) {
    const p = document.createElement("p");
    p.textContent = detail;
    body.appendChild(p);
  }
  section.append(icon, body);
  if (action instanceof Node) section.appendChild(action);
  return section;
}

export function skeleton({ rows = 3, label = "Loading" } = {}) {
  const root = document.createElement("div");
  root.className = "cw-skeleton";
  root.setAttribute("role", "status");
  root.setAttribute("aria-label", label);
  root.setAttribute("aria-busy", "true");
  for (let index = 0; index < Math.max(1, rows); index += 1) {
    const row = document.createElement("span");
    row.className = "cw-skeleton-row";
    row.setAttribute("aria-hidden", "true");
    root.appendChild(row);
  }
  return root;
}

export function timeline(entries = [], options = {}) {
  const list = document.createElement("ol");
  list.className = ["cw-timeline", options.className || ""].filter(Boolean).join(" ");
  for (const entry of entries) {
    const item = document.createElement("li");
    const marker = document.createElement("span");
    marker.className = "cw-timeline-marker";
    marker.setAttribute("aria-hidden", "true");

    const body = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = entry.label || entry.title || "Event";
    body.appendChild(title);
    if (entry.detail) {
      const detail = document.createElement("small");
      detail.textContent = entry.detail;
      body.appendChild(detail);
    }
    if (entry.time) {
      const time = document.createElement("time");
      time.textContent = entry.time;
      if (entry.dateTime) time.dateTime = entry.dateTime;
      body.appendChild(time);
    }
    item.append(marker, body);
    list.appendChild(item);
  }
  return list;
}

export function provenanceDisclosure({ label = "Provenance", summary = "", entries = [] } = {}) {
  const details = document.createElement("details");
  details.className = "cw-provenance";
  const summaryNode = document.createElement("summary");
  summaryNode.textContent = summary ? `${label} · ${summary}` : label;
  details.append(summaryNode, timeline(entries, { className: "cw-provenance-timeline" }));
  return details;
}

export function surfaceCard({ title = "", description = "", status = "", identity = null, body = null, footer = null, kind = "default" } = {}) {
  const article = document.createElement("article");
  article.className = `cw-card cw-card-${kind}`;
  const head = document.createElement("div");
  head.className = "cw-card-head";
  const titleBox = document.createElement("div");
  const strong = document.createElement("strong");
  strong.textContent = title;
  titleBox.appendChild(strong);
  if (description) {
    const small = document.createElement("small");
    small.textContent = description;
    titleBox.appendChild(small);
  }
  head.appendChild(titleBox);
  const chips = document.createElement("div");
  chips.className = "cw-card-chips";
  if (identity) chips.appendChild(identityChip(identity));
  if (status) chips.appendChild(statusBadge(status));
  head.appendChild(chips);
  article.appendChild(head);
  if (body instanceof Node) article.appendChild(body);
  if (footer instanceof Node) {
    footer.classList.add("cw-card-footer");
    article.appendChild(footer);
  }
  return article;
}

export const WorkspaceComponents = Object.freeze({
  statusFamily,
  statusBadge,
  identityChip,
  objectHeader,
  metadataGrid,
  statePanel,
  skeleton,
  timeline,
  provenanceDisclosure,
  surfaceCard,
});
