const state = {
  projectId: "",
  automations: [],
  selectedId: "",
  runs: [],
  loading: false,
  error: "",
  editing: false,
  creating: false,
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
  if (kind === "recurring_schedule") return trigger.cron ? `Schedule · ${trigger.cron}` : "Schedule";
  if (kind === "one_shot_schedule") return trigger.due_at ? `One shot · ${new Date(trigger.due_at * 1000).toLocaleString()}` : "One shot";
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


function numberValue(value) {
  return value == null ? "" : String(value);
}

function editorMarkup(item) {
  if (!state.editing) return "";
  const definition = item?.definition || {};
  const trigger = definition.trigger || { type: "manual" };
  const target = definition.target || { kind: "agent_profile", id: "" };
  const budget = definition.budget || {};
  const retry = definition.retry || {};
  const automationId = state.creating ? "" : (item?.id || "");
  return `
    <form class="automation-editor" data-automation-editor>
      <div class="automation-product-heading">
        <div>
          <h3>${state.creating ? "New Automation" : "Edit Automation"}</h3>
          <p>Draft and publish through the canonical Definition Registry. Existing pinned Skill, execution-profile and authority references are preserved.</p>
        </div>
        <button type="button" class="ghost-button" data-automation-edit-cancel>Cancel</button>
      </div>
      <div class="form-grid">
        <label>Automation ID
          <input name="automation_id" value="${esc(automationId)}" ${state.creating ? "" : "readonly"} required pattern="[a-z0-9][a-z0-9._-]*" />
        </label>
        <label>Name
          <input name="name" value="${esc(definition.name || "")}" required />
        </label>
        <label>Lifecycle
          <select name="lifecycle">
            <option value="paused" ${definition.lifecycle !== "enabled" ? "selected" : ""}>Paused</option>
            <option value="enabled" ${definition.lifecycle === "enabled" ? "selected" : ""}>Enabled</option>
          </select>
        </label>
        <label>Work Item policy
          <select name="work_item_policy">
            ${["reuse_or_create", "always_create", "reuse_only"].map((value) =>
              `<option value="${value}" ${(definition.work_item_policy || "reuse_or_create") === value ? "selected" : ""}>${value}</option>`
            ).join("")}
          </select>
        </label>
        <label>Trigger type
          <select name="trigger_type">
            ${["manual", "recurring_schedule", "one_shot_schedule", "canonical_event", "provider_event"].map((value) =>
              `<option value="${value}" ${trigger.type === value ? "selected" : ""}>${value}</option>`
            ).join("")}
          </select>
        </label>
        <label>Event type
          <input name="event_type" value="${esc(trigger.event_type || "")}" placeholder="ci.pipeline" />
        </label>
        <label>Provider ID
          <input name="provider_id" value="${esc(trigger.provider_id || "")}" placeholder="gitlab" />
        </label>
        <label>Cron
          <input name="cron" value="${esc(trigger.cron || "")}" placeholder="0 2 * * *" />
        </label>
        <label>Timezone
          <input name="timezone" value="${esc(trigger.timezone || "")}" placeholder="Europe/Stockholm" />
        </label>
        <label>One-shot due at
          <input name="due_at" type="datetime-local" value="${trigger.due_at ? new Date(trigger.due_at * 1000).toISOString().slice(0, 16) : ""}" />
        </label>
        <label>Target
          <select name="target_kind">
            <option value="agent_profile" ${target.kind !== "team" ? "selected" : ""}>Agent Profile</option>
            <option value="team" ${target.kind === "team" ? "selected" : ""}>Team</option>
          </select>
        </label>
        <label>Target ID
          <input name="target_id" value="${esc(target.id || "")}" required />
        </label>
        <label>Owner identity
          <input name="owner_identity_id" value="${esc(definition.owner_identity_id || "")}" />
        </label>
        <label class="checkbox-line">
          <input name="approval_required" type="checkbox" ${definition.approval_required ? "checked" : ""} />
          Approval required
        </label>
        <label>Max input tokens
          <input name="max_input_tokens" type="number" min="0" value="${esc(numberValue(budget.max_input_tokens))}" />
        </label>
        <label>Max output tokens
          <input name="max_output_tokens" type="number" min="0" value="${esc(numberValue(budget.max_output_tokens))}" />
        </label>
        <label>Max cost USD
          <input name="max_cost_usd" type="number" min="0" step="0.01" value="${esc(numberValue(budget.max_cost_usd))}" />
        </label>
        <label>Max duration seconds
          <input name="max_duration_seconds" type="number" min="1" value="${esc(numberValue(budget.max_duration_seconds))}" />
        </label>
        <label>Max concurrency
          <input name="max_concurrency" type="number" min="1" max="100" value="${esc(numberValue(budget.max_concurrency ?? 1))}" required />
        </label>
        <label>Retry attempts
          <input name="retry_max_attempts" type="number" min="1" max="100" value="${esc(numberValue(retry.max_attempts ?? 1))}" required />
        </label>
        <label>Retry backoff seconds
          <input name="retry_backoff_seconds" type="number" min="0" step="0.1" value="${esc(numberValue(retry.backoff_seconds ?? 0))}" required />
        </label>
      </div>
      <label>Description
        <textarea name="description" rows="2">${esc(definition.description || "")}</textarea>
      </label>
      <label>Instructions
        <textarea name="instructions" rows="5" required>${esc(definition.instructions || "")}</textarea>
      </label>
      <div class="automation-actions">
        <button class="primary-button" type="submit">Publish Automation</button>
      </div>
    </form>`;
}

function nullableNumber(form, name) {
  const raw = form.elements[name].value.trim();
  return raw === "" ? null : Number(raw);
}

function definitionFromEditor(form, existing) {
  const triggerType = form.elements.trigger_type.value;
  const trigger = { type: triggerType };
  if (triggerType === "recurring_schedule") {
    trigger.cron = form.elements.cron.value.trim();
    trigger.timezone = form.elements.timezone.value.trim();
  } else if (triggerType === "one_shot_schedule") {
    const value = form.elements.due_at.value;
    trigger.due_at = value ? new Date(value).getTime() / 1000 : null;
  } else if (triggerType === "canonical_event") {
    trigger.event_type = form.elements.event_type.value.trim();
    trigger.event_filter = existing?.trigger?.event_filter || {};
  } else if (triggerType === "provider_event") {
    trigger.event_type = form.elements.event_type.value.trim();
    trigger.provider_id = form.elements.provider_id.value.trim();
    trigger.event_filter = existing?.trigger?.event_filter || {};
  }
  return {
    name: form.elements.name.value.trim(),
    description: form.elements.description.value.trim() || null,
    lifecycle: form.elements.lifecycle.value,
    trigger,
    target: {
      kind: form.elements.target_kind.value,
      id: form.elements.target_id.value.trim(),
    },
    instructions: form.elements.instructions.value.trim(),
    skill_refs: existing?.skill_refs || [],
    execution_profile_ref: existing?.execution_profile_ref || null,
    authority_ref: existing?.authority_ref || null,
    approval_required: form.elements.approval_required.checked,
    budget: {
      max_input_tokens: nullableNumber(form, "max_input_tokens"),
      max_output_tokens: nullableNumber(form, "max_output_tokens"),
      max_cost_usd: nullableNumber(form, "max_cost_usd"),
      max_duration_seconds: nullableNumber(form, "max_duration_seconds"),
      max_concurrency: Number(form.elements.max_concurrency.value),
    },
    retry: {
      max_attempts: Number(form.elements.retry_max_attempts.value),
      backoff_seconds: Number(form.elements.retry_backoff_seconds.value),
    },
    dedupe_key_template: existing?.dedupe_key_template || null,
    work_item_policy: form.elements.work_item_policy.value,
    failure_attention: existing?.failure_attention ?? true,
    owner_identity_id: form.elements.owner_identity_id.value.trim() || null,
  };
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
        <button type="button" class="ghost-button" data-automation-edit>Edit</button>
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
      <div class="automation-actions">
        <button type="button" class="primary-button" data-automation-new>New Automation</button>
        <button type="button" class="ghost-button" data-automation-refresh-list>Refresh</button>
      </div>
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
      <div class="automation-detail-host">${editorMarkup(selected()) || detailMarkup(selected())}</div>
    </div>`;

  card.querySelectorAll("[data-automation-id]").forEach((button) => {
    button.addEventListener("click", () => selectAutomation(button.dataset.automationId));
  });
  card.querySelectorAll("[data-automation-refresh-list],[data-automation-refresh]").forEach((button) => {
    button.addEventListener("click", () => load());
  });
  card.querySelector("[data-automation-run-now]")?.addEventListener("click", runNow);
  card.querySelector("[data-automation-edit]")?.addEventListener("click", () => {
    state.editing = true;
    state.creating = false;
    render();
  });
  card.querySelector("[data-automation-new]")?.addEventListener("click", () => {
    state.editing = true;
    state.creating = true;
    render();
  });
  card.querySelector("[data-automation-edit-cancel]")?.addEventListener("click", () => {
    state.editing = false;
    state.creating = false;
    render();
  });
  card.querySelector("[data-automation-editor]")?.addEventListener("submit", (event) => {
    saveEditor(event).catch((error) => {
      state.error = error.message;
      render();
    });
  });
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


async function saveEditor(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const existingItem = state.creating ? null : selected();
  const definition = definitionFromEditor(form, existingItem?.definition || {});
  const automationId = form.elements.automation_id.value.trim();
  const draft = await api("/api/automations/drafts", {
    method: "POST",
    body: JSON.stringify({
      automation_id: automationId,
      definition,
      project_id: state.projectId || null,
      reason: state.creating ? "Created from Automation workspace" : "Edited from Automation workspace",
      derived_from_record_id: existingItem?.definitionRef?.record_id || null,
    }),
  });
  const recordId = draft?.record?.record_id;
  if (!recordId) throw new Error("Automation draft returned no record id");
  const published = await api(`/api/automations/drafts/${encodeURIComponent(recordId)}/publish`, {
    method: "POST",
    body: JSON.stringify({
      reason: "Publish from Automation workspace",
      expected_active_revision: existingItem?.definitionRef?.revision || null,
      approval_metadata: {},
    }),
  });
  if (published.scheduleError) {
    state.error = `Automation published, but schedule needs repair: ${published.scheduleError}`;
  } else {
    state.error = "";
  }
  state.editing = false;
  state.creating = false;
  state.selectedId = automationId;
  await load(state.projectId);
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
  window.addEventListener("codex:projects-rendered", (event) => {
    load(event.detail?.projectId || document.body.dataset.activeProject || "");
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
