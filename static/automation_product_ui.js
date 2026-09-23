const state = {
  projectId: "",
  automations: [],
  selectedId: "",
  runs: [],
  loading: false,
  error: "",
};

function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = payload?.detail;
    throw new Error(typeof detail === "string" ? detail : `Request failed (${response.status})`);
  }
  return payload;
}

function host() {
  return document.querySelector('[data-product-workspace-host="autonomy"]');
}

function statusClass(status) {
  if (["enabled", "succeeded", "running", "admitted"].includes(status)) return "status-positive";
  if (["blocked", "failed", "cancelled"].includes(status)) return "status-negative";
  if (status === "paused") return "status-warning";
  return "status-neutral";
}

function triggerLabel(trigger = {}) {
  const kind = trigger.type || "unknown";
  if (kind === "schedule") return trigger.cron ? `Schedule · ${trigger.cron}` : "Schedule";
  if (kind === "one_shot") return trigger.run_at ? `One shot · ${trigger.run_at}` : "One shot";
  if (kind === "canonical_event") return `Event · ${trigger.event_type || "canonical"}`;
  if (kind === "provider_event") return `Provider event · ${trigger.event_type || trigger.provider_id || "provider"}`;
  return kind === "manual" ? "Manual" : kind;
}

function refLabel(ref = {}) {
  if (!ref?.record_id) return "No pinned definition";
  return `r${ref.revision ?? "?"} · ${ref.record_id}`;
}

function selected() {
  return state.automations.find((item) => item.id === state.selectedId) || null;
}

function runRow(run) {
  const when = run.created_at ? new Date(run.created_at * 1000).toLocaleString() : "Unknown time";
  const refs = [
    run.work_item_ref ? `Work Item ${run.work_item_ref}` : null,
    ...(run.execution_ids || []).map((id) => `Execution ${id}`),
  ].filter(Boolean);
  return `
    <article class="automation-run-row" data-automation-run="${esc(run.id)}">
      <div>
        <strong>${esc(run.id)}</strong>
        <small>${esc(when)}</small>
      </div>
      <span class="product-status-badge ${statusClass(run.status)}">${esc(run.status)}</span>
      <small>${esc(refs.join(" · ") || run.block_reason || "No linked execution yet")}</small>
    </article>`;
}

function detailMarkup(item) {
  if (!item) {
    return '<div class="workspace-state workspace-state-empty">Select an Automation to inspect its canonical definition and run history.</div>';
  }
  const definition = item.definition || {};
  const target = definition.target || {};
  const budget = definition.budget || {};
  const retry = definition.retry || {};
  return `
    <section class="automation-detail">
      <header class="automation-detail-header">
        <div>
          <small>Automation</small>
          <h3>${esc(definition.name || item.id)}</h3>
          <p>${esc(definition.description || definition.instructions || "No description.")}</p>
        </div>
        <span class="product-status-badge ${statusClass(definition.lifecycle)}">${esc(definition.lifecycle || "unknown")}</span>
      </header>
      <div class="automation-facts">
        <div><small>Trigger</small><strong>${esc(triggerLabel(definition.trigger))}</strong></div>
        <div><small>Target</small><strong>${esc(target.kind || "unknown")} · ${esc(target.id || "unresolved")}</strong></div>
        <div><small>Work Item policy</small><strong>${esc(definition.work_item_policy || "reuse_or_create")}</strong></div>
        <div><small>Approval</small><strong>${definition.approval_required ? "Required" : "Canonical policy"}</strong></div>
        <div><small>Concurrency</small><strong>${esc(budget.max_concurrency ?? 1)}</strong></div>
        <div><small>Retry</small><strong>${esc(retry.max_attempts ?? 1)} attempts · ${esc(retry.backoff_seconds ?? 0)}s backoff</strong></div>
      </div>
      <details>
        <summary>Exact provenance and limits</summary>
        <dl class="automation-provenance">
          <dt>Definition</dt><dd>${esc(refLabel(item.definitionRef))}</dd>
          <dt>Owner</dt><dd>${esc(definition.owner_identity_id || "Not configured")}</dd>
          <dt>Execution profile</dt><dd>${esc(definition.execution_profile_ref?.definition_id || "Canonical default")}</dd>
          <dt>Skills</dt><dd>${esc((definition.skill_refs || []).map((ref) => `${ref.definition_id}@${ref.revision}`).join(", ") || "None")}</dd>
          <dt>Token budget</dt><dd>${esc([budget.max_input_tokens, budget.max_output_tokens].filter((v) => v != null).join(" / ") || "Not bounded here")}</dd>
          <dt>Cost budget</dt><dd>${budget.max_cost_usd == null ? "Not bounded here" : `$${esc(budget.max_cost_usd)}`}</dd>
        </dl>
      </details>
      <div class="automation-actions">
        <button type="button" class="primary-button" data-automation-run-now ${definition.lifecycle !== "enabled" ? "disabled" : ""}>Run now</button>
        <button type="button" class="ghost-button" data-automation-refresh>Refresh</button>
      </div>
      <section>
        <h4>Recent runs</h4>
        <div class="automation-run-list">
          ${state.runs.length ? state.runs.slice(0, 20).map(runRow).join("") : '<div class="workspace-state workspace-state-empty">No runs recorded.</div>'}
        </div>
      </section>
    </section>`;
}

function render() {
  const root = host();
  if (!root) return;
  let card = root.querySelector("[data-automation-product]");
  if (!card) {
    card = document.createElement("section");
    card.className = "developer-card product-adopted-card automation-product-card";
    card.dataset.automationProduct = "true";
    root.prepend(card);
  }
  card.innerHTML = `
    <div class="automation-product-heading">
      <div><h2>Automations</h2><p>Versioned triggers and runs over the canonical scheduler, event bus, Agent/Team execution and Work Items.</p></div>
      <button type="button" class="ghost-button" data-automation-refresh-list>Refresh</button>
    </div>
    ${state.error ? `<div class="workspace-state workspace-state-error">${esc(state.error)}</div>` : ""}
    <div class="automation-product-layout">
      <nav class="automation-list" aria-label="Automations">
        ${state.loading ? '<div class="workspace-state workspace-state-loading">Loading Automations…</div>' :
          state.automations.length ? state.automations.map((item) => `
            <button type="button" class="automation-list-item ${item.id === state.selectedId ? "active" : ""}" data-automation-id="${esc(item.id)}">
              <strong>${esc(item.definition?.name || item.id)}</strong>
              <small>${esc(triggerLabel(item.definition?.trigger))} · ${esc(item.definition?.target?.kind || "target")}</small>
              <span class="product-status-badge ${statusClass(item.definition?.lifecycle)}">${esc(item.definition?.lifecycle || "unknown")}</span>
            </button>`).join("") :
            '<div class="workspace-state workspace-state-empty">No effective Automations for this Project.</div>'}
      </nav>
      <div class="automation-detail-host">${detailMarkup(selected())}</div>
    </div>`;

  card.querySelectorAll("[data-automation-id]").forEach((button) => {
    button.addEventListener("click", () => selectAutomation(button.dataset.automationId));
  });
  card.querySelectorAll("[data-automation-refresh-list],[data-automation-refresh]").forEach((button) => {
    button.addEventListener("click", () => load());
  });
  card.querySelector("[data-automation-run-now]")?.addEventListener("click", runNow);
}

async function loadRuns(id) {
  if (!id) {
    state.runs = [];
    return;
  }
  const payload = await api(`/api/automations/${encodeURIComponent(id)}/runs`);
  state.runs = payload.items || [];
}

async function selectAutomation(id) {
  state.selectedId = id;
  state.error = "";
  try {
    await loadRuns(id);
  } catch (error) {
    state.error = error.message;
  }
  render();
}

async function load(projectId = document.body.dataset.activeProject || "") {
  const root = host();
  if (!root) return;
  state.projectId = projectId || "";
  state.loading = true;
  state.error = "";
  render();
  try {
    const query = state.projectId ? `?project_id=${encodeURIComponent(state.projectId)}` : "";
    const payload = await api(`/api/automations${query}`);
    state.automations = payload.items || [];
    if (!state.automations.some((item) => item.id === state.selectedId)) {
      state.selectedId = state.automations[0]?.id || "";
    }
    await loadRuns(state.selectedId);
  } catch (error) {
    state.error = error.message;
    state.automations = [];
    state.runs = [];
  } finally {
    state.loading = false;
    render();
  }
}

async function runNow() {
  const item = selected();
  if (!item) return;
  state.error = "";
  render();
  try {
    const key = `ui-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    const admitted = await api(`/api/automations/${encodeURIComponent(item.id)}/runs/manual`, {
      method: "POST",
      body: JSON.stringify({ idempotency_key: key, project_id: state.projectId || null }),
    });
    if (admitted.launchAllowed) {
      await api(`/api/automation-runs/${encodeURIComponent(admitted.run.id)}/launch`, {
        method: "POST",
        body: JSON.stringify({
          work_item_ref: null,
          repository_resource_id: null,
          read_only_repository_resource_ids: [],
        }),
      });
    }
    await loadRuns(item.id);
  } catch (error) {
    state.error = error.message;
  }
  render();
}

function install() {
  const attempt = () => {
    if (!host()) {
      requestAnimationFrame(attempt);
      return;
    }
    load();
  };
  attempt();
  window.addEventListener("codex:project-changed", (event) => {
    load(event.detail?.projectId || "");
  });
  window.addEventListener("hashchange", () => {
    if (window.location.hash === "#workspace/autonomy") load();
  });
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", install, { once: true });
} else {
  install();
}
