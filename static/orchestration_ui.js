const esc = (value) => String(value ?? "")
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;")
  .replaceAll("'", "&#039;");

async function request(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const payload = await response.json();
      detail = payload?.detail?.message || payload?.detail || detail;
    } catch {
      // Keep the HTTP status text when the body is not JSON.
    }
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return response.json();
}

function installStyles() {
  if (document.getElementById("orchestration-inspector-styles")) return;
  const style = document.createElement("style");
  style.id = "orchestration-inspector-styles";
  style.textContent = `
    #orchestration-inspector-card { grid-column: 1 / -1; }
    .orch-toolbar, .orch-control-row, .orch-filter-row {
      display: flex; gap: .5rem; flex-wrap: wrap; align-items: center; margin: .5rem 0;
    }
    .orch-toolbar button, .orch-control-row button { white-space: nowrap; }
    .orch-control-row label { display: inline-flex; align-items: center; gap: .35rem; }
    .orch-status { font-size: .82rem; opacity: .8; }
    .orch-status[data-error="true"] { color: var(--danger, #b42318); }
    .orch-timeline { display: grid; gap: .65rem; margin-top: .75rem; }
    .orch-event {
      border: 1px solid color-mix(in srgb, currentColor 18%, transparent);
      border-radius: .55rem; padding: .7rem; background: color-mix(in srgb, currentColor 3%, transparent);
    }
    .orch-event-head { display: flex; justify-content: space-between; gap: .75rem; flex-wrap: wrap; }
    .orch-event-head code { overflow-wrap: anywhere; }
    .orch-meta { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: .3rem .8rem; margin-top: .45rem; font-size: .84rem; }
    .orch-cycle { margin-top: .55rem; padding-top: .55rem; border-top: 1px solid color-mix(in srgb, currentColor 12%, transparent); }
    .orch-pill {
      display: inline-flex; align-items: center; padding: .12rem .45rem; border-radius: 999px;
      border: 1px solid color-mix(in srgb, currentColor 20%, transparent); font-size: .76rem; margin-right: .3rem;
    }
    .orch-dead { border-left: 3px solid var(--danger, #b42318); padding-left: .6rem; margin: .5rem 0; }
    .orch-dependency { opacity: .75; font-size: .82rem; }
    .orch-empty { opacity: .7; padding: .8rem 0; }
    .orch-actions a { margin-right: .5rem; }
  `;
  document.head.appendChild(style);
}

function formatTime(seconds) {
  if (!seconds) return "—";
  return new Date(Number(seconds) * 1000).toLocaleString();
}

function cycleMarkup(cycle) {
  const reasoning = cycle.reasoning_invoked ? "LLM/reasoning invoked" : "Reasoning skipped";
  const actionLinks = (cycle.action_intent_ids || [])
    .map((id) => `<a href="/api/action-intents/${encodeURIComponent(id)}" target="_blank" rel="noreferrer">${esc(id)}</a>`)
    .join("");
  return `
    <div class="orch-cycle">
      <div>
        <span class="orch-pill">${esc(cycle.outcome)}</span>
        <span class="orch-pill">${esc(reasoning)}</span>
        <span class="orch-pill">attempts ${esc(cycle.reasoning_attempts)}</span>
        <span class="orch-pill">depth ${esc(cycle.recursion_depth)}</span>
        <span class="orch-pill">actions ${esc(cycle.action_count)}</span>
      </div>
      <div><strong>Why:</strong> ${esc(cycle.reason)}</div>
      ${cycle.last_error ? `<div><strong>Error:</strong> ${esc(cycle.last_error)}</div>` : ""}
      ${actionLinks ? `<div class="orch-actions"><strong>ActionIntents:</strong> ${actionLinks}</div>` : ""}
      <small>Cycle ${esc(cycle.id)} · completed ${esc(formatTime(cycle.completed_at))}</small>
    </div>
  `;
}

function eventMarkup(item) {
  const event = item.event || {};
  const cycles = item.cycles || [];
  const reasoningText = item.reasoning?.invoked
    ? "Reasoning invoked"
    : (cycles.length ? "Reasoning skipped" : "No autonomy cycle");
  return `
    <article class="orch-event">
      <div class="orch-event-head">
        <strong>${esc(event.event_type)}</strong>
        <code>${esc(event.event_id)}</code>
      </div>
      <div class="orch-meta">
        <span><strong>Source:</strong> ${esc(event.source)}</span>
        <span><strong>Occurred:</strong> ${esc(formatTime(event.occurred_at))}</span>
        <span><strong>Filter:</strong> ${esc(item.filtering_result)}</span>
        <span><strong>Reasoning:</strong> ${esc(reasoningText)}</span>
        <span><strong>Correlation:</strong> ${esc(event.correlation_id || "—")}</span>
        <span><strong>Causation:</strong> ${esc(event.causation_id || "—")}</span>
      </div>
      ${cycles.map(cycleMarkup).join("")}
    </article>
  `;
}

function renderDependencies(dependencies = {}) {
  return Object.entries(dependencies).map(([name, item]) => (
    `<div class="orch-dependency"><strong>${esc(name.replaceAll("_", " "))}:</strong> ${item.available ? "available" : `awaiting #${esc(item.issue)}`} · ${esc(item.reason || "")}</div>`
  )).join("");
}

function renderDeadLetters(items = []) {
  if (!items.length) return '<div class="orch-empty">No dead-lettered autonomy cycles.</div>';
  return items.map((item) => `
    <div class="orch-dead">
      <strong>${esc(item.event_type)}</strong> · ${esc(item.reason)}
      ${item.error ? `<div>${esc(item.error)}</div>` : ""}
      <small>${esc(item.event_id)} · attempts ${esc(item.attempts)} · ${esc(formatTime(item.created_at))}</small>
    </div>
  `).join("");
}

function render(panel, data) {
  const control = data.control || {};
  panel.querySelector("[data-orch-mode]").textContent = control.mode || "unknown";
  panel.querySelector("[data-orch-dry-run]").checked = Boolean(control.dry_run);
  panel.querySelector("[data-orch-simulation]").checked = Boolean(control.simulation);
  panel.querySelector("[data-orch-limits]").textContent =
    `threshold ${control.reasoning_threshold ?? "—"} · cooldown ${control.cooldown_seconds ?? "—"}s · retries ${control.max_reasoning_attempts ?? "—"} · depth ${control.max_recursion_depth ?? "—"} · actions/cycle ${control.max_actions_per_cycle ?? "—"}`;
  panel.querySelector("[data-orch-timeline]").innerHTML = data.timeline?.length
    ? data.timeline.map(eventMarkup).join("")
    : '<div class="orch-empty">No matching canonical events.</div>';
  panel.querySelector("[data-orch-dead-letters]").innerHTML = renderDeadLetters(data.dead_letters);
  panel.querySelector("[data-orch-dependencies]").innerHTML = renderDependencies(data.dependencies);
}

async function load(panel) {
  const status = panel.querySelector("[data-orch-status]");
  const eventType = panel.querySelector("[data-orch-event-type]").value.trim();
  const source = panel.querySelector("[data-orch-source]").value.trim();
  status.dataset.error = "false";
  status.textContent = "Loading canonical orchestration state…";
  try {
    const query = new URLSearchParams({ limit: "100" });
    if (eventType) query.set("event_type", eventType);
    if (source) query.set("source", source);
    const data = await request(`/api/orchestration/inspector?${query}`);
    render(panel, data);
    status.textContent = `${data.event_count} events · ${data.cycle_count} recent cycles · read-only refresh (no reasoning)`;
  } catch (error) {
    status.dataset.error = "true";
    status.textContent = `Inspector unavailable: ${error.message}`;
  }
}

async function mutate(panel, path, options = {}) {
  const status = panel.querySelector("[data-orch-status]");
  status.dataset.error = "false";
  status.textContent = "Applying canonical autonomy control…";
  try {
    await request(path, options);
    await load(panel);
  } catch (error) {
    status.dataset.error = "true";
    status.textContent = `Control update failed: ${error.message}`;
  }
}

function buildPanel() {
  const panel = document.createElement("div");
  panel.id = "orchestration-inspector-card";
  panel.className = "developer-card";
  panel.innerHTML = `
    <div class="section-title">
      <div>
        <h2>Orchestration Inspector</h2>
        <small>Canonical event → deterministic filter → reasoning gate → authority/action</small>
      </div>
      <button type="button" class="ghost-button" data-orch-refresh>Refresh</button>
    </div>
    <div class="orch-toolbar">
      <strong>Mode: <span data-orch-mode>loading</span></strong>
      <span class="orch-status" data-orch-limits></span>
    </div>
    <div class="orch-control-row">
      <button type="button" class="ghost-button" data-orch-pause>Pause</button>
      <button type="button" class="ghost-button" data-orch-resume>Resume</button>
      <button type="button" class="ghost-button" data-orch-kill>Global kill</button>
      <label><input type="checkbox" data-orch-dry-run /> Dry-run</label>
      <label><input type="checkbox" data-orch-simulation /> Simulation</label>
    </div>
    <div class="orch-filter-row">
      <input data-orch-event-type placeholder="Event type filter" />
      <input data-orch-source placeholder="Source filter" />
      <button type="button" class="ghost-button" data-orch-apply-filter>Apply filters</button>
    </div>
    <div class="orch-status" data-orch-status>Open Developer tools or refresh to load canonical state.</div>
    <details open>
      <summary>Event timeline</summary>
      <div class="orch-timeline" data-orch-timeline></div>
    </details>
    <details>
      <summary>Dead letters</summary>
      <div data-orch-dead-letters></div>
    </details>
    <details>
      <summary>Pending inspector foundations</summary>
      <div data-orch-dependencies></div>
    </details>
  `;

  panel.querySelector("[data-orch-refresh]").addEventListener("click", () => load(panel));
  panel.querySelector("[data-orch-apply-filter]").addEventListener("click", () => load(panel));
  panel.querySelector("[data-orch-pause]").addEventListener("click", () => mutate(panel, "/api/autonomy/pause", { method: "POST" }));
  panel.querySelector("[data-orch-resume]").addEventListener("click", () => mutate(panel, "/api/autonomy/resume", { method: "POST" }));
  panel.querySelector("[data-orch-kill]").addEventListener("click", () => mutate(panel, "/api/autonomy/kill", { method: "POST" }));
  panel.querySelector("[data-orch-dry-run]").addEventListener("change", (event) => mutate(panel, "/api/autonomy/control", {
    method: "PATCH",
    body: JSON.stringify({ dry_run: event.target.checked }),
  }));
  panel.querySelector("[data-orch-simulation]").addEventListener("change", (event) => mutate(panel, "/api/autonomy/control", {
    method: "PATCH",
    body: JSON.stringify({ simulation: event.target.checked }),
  }));
  return panel;
}

function install() {
  const grid = document.querySelector("#developer-panel .developer-grid");
  if (!grid || document.getElementById("orchestration-inspector-card")) return;
  installStyles();
  const panel = buildPanel();
  grid.appendChild(panel);
  const developer = document.getElementById("developer-panel");
  developer?.addEventListener("toggle", () => {
    if (developer.open && !panel.dataset.loaded) {
      panel.dataset.loaded = "true";
      load(panel);
    }
  });
  if (developer?.open) {
    panel.dataset.loaded = "true";
    load(panel);
  }
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", install, { once: true });
} else {
  install();
}
