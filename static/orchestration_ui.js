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
    #orchestration-inspector-card { grid-column: 1 / -1; min-width: 0; }
    .orch-toolbar, .orch-control-row, .orch-filter-row, .orch-actions {
      display: flex; gap: .5rem; flex-wrap: wrap; align-items: center; margin: .5rem 0;
    }
    .orch-toolbar button, .orch-control-row button, .orch-actions button { white-space: nowrap; }
    .orch-control-row label { display: inline-flex; align-items: center; gap: .35rem; }
    .orch-status { font-size: .82rem; opacity: .8; }
    .orch-status[data-error="true"] { color: var(--danger, #b42318); }
    .orch-timeline, .orch-record-list { display: grid; gap: .65rem; margin-top: .75rem; }
    .orch-event, .orch-record {
      min-width: 0; border: 1px solid color-mix(in srgb, currentColor 18%, transparent);
      border-radius: .55rem; padding: .7rem; background: color-mix(in srgb, currentColor 3%, transparent);
    }
    .orch-event-head, .orch-record-head {
      display: flex; justify-content: space-between; align-items: flex-start; gap: .75rem; flex-wrap: wrap;
    }
    .orch-event-head code, .orch-record code, .orch-break { overflow-wrap: anywhere; word-break: break-word; }
    .orch-meta {
      display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
      gap: .3rem .8rem; margin-top: .45rem; font-size: .84rem;
    }
    .orch-cycle { margin-top: .55rem; padding-top: .55rem; border-top: 1px solid color-mix(in srgb, currentColor 12%, transparent); }
    .orch-pill {
      display: inline-flex; align-items: center; padding: .12rem .45rem; border-radius: 999px;
      border: 1px solid color-mix(in srgb, currentColor 20%, transparent); font-size: .76rem; margin-right: .3rem;
    }
    .orch-pipeline {
      display: grid; grid-template-columns: repeat(8, minmax(150px, 1fr));
      gap: .45rem; margin-top: .7rem; overflow-x: auto; padding-bottom: .25rem;
    }
    .orch-stage {
      min-width: 0; border: 1px solid color-mix(in srgb, currentColor 15%, transparent);
      border-radius: .45rem; padding: .55rem; display: grid; gap: .3rem;
    }
    .orch-stage small { opacity: .72; overflow-wrap: anywhere; }
    .orch-stage strong { font-size: .78rem; text-transform: uppercase; letter-spacing: .02em; }
    .orch-dead { border-left: 3px solid var(--danger, #b42318); padding-left: .6rem; margin: .5rem 0; }
    .orch-dependency { opacity: .82; font-size: .82rem; }
    .orch-empty { opacity: .7; padding: .8rem 0; }
    .orch-actions a, .orch-links a { margin-right: .5rem; overflow-wrap: anywhere; }
    .orch-record p { margin: .35rem 0; }
    .orch-kv {
      display: grid; grid-template-columns: repeat(auto-fit, minmax(145px, 1fr));
      gap: .35rem .7rem; margin-top: .45rem; font-size: .8rem;
    }
    .orch-kv span { min-width: 0; overflow-wrap: anywhere; }
    .orch-sublist { display: grid; gap: .35rem; margin-top: .5rem; }
    .orch-subrecord { border-left: 2px solid color-mix(in srgb, currentColor 18%, transparent); padding-left: .55rem; }
    .orch-regression { font-weight: 700; }
    .orch-section-count { opacity: .7; font-size: .8rem; }
    @media (max-width: 780px) {
      .orch-pipeline { grid-template-columns: 1fr; overflow: visible; }
      .orch-filter-row input { flex: 1 1 100%; min-width: 0; }
      .orch-control-row button { flex: 1 1 auto; min-height: 40px; }
      .orch-event, .orch-record { padding: .6rem; }
      .orch-meta, .orch-kv { grid-template-columns: 1fr; }
    }
  `;
  document.head.appendChild(style);
}

function formatTime(seconds) {
  if (!seconds) return "—";
  return new Date(Number(seconds) * 1000).toLocaleString();
}

function badge(value) {
  return `<span class="orch-pill">${esc(value ?? "—")}</span>`;
}

function cycleMarkup(cycle) {
  const reasoning = cycle.reasoning_invoked ? "LLM/reasoning invoked" : "Reasoning skipped";
  const actionLinks = (cycle.action_intent_ids || [])
    .map((id) => `<a href="/api/action-intents/${encodeURIComponent(id)}" target="_blank" rel="noreferrer">${esc(id)}</a>`)
    .join("");
  return `
    <div class="orch-cycle">
      <div>
        ${badge(cycle.outcome)}
        ${badge(reasoning)}
        ${badge(`attempts ${cycle.reasoning_attempts ?? 0}`)}
        ${badge(`depth ${cycle.recursion_depth ?? 0}`)}
        ${badge(`actions ${cycle.action_count ?? 0}`)}
      </div>
      <div><strong>Why:</strong> ${esc(cycle.reason)}</div>
      ${cycle.last_error ? `<div><strong>Error:</strong> ${esc(cycle.last_error)}</div>` : ""}
      ${actionLinks ? `<div class="orch-links"><strong>ActionIntents:</strong> ${actionLinks}</div>` : ""}
      <small>Cycle ${esc(cycle.id)} · completed ${esc(formatTime(cycle.completed_at))}</small>
    </div>
  `;
}

function stageSummary(stage) {
  const detail = stage.detail || {};
  if (stage.stage === "reasoning_gate") {
    return `${detail.attempts ?? 0} attempt(s) · ${(detail.reasons || []).join(" · ") || "no recorded reason"}`;
  }
  if (stage.stage === "routing") {
    const session = detail.agent_sessions?.[0];
    const model = detail.model_invocations?.[0];
    const runtimeText = session ? `${session.provider_id}/${session.runtime_id}` : "no runtime";
    const modelText = model ? `${model.selected_provider_id || "?"}/${model.selected_model_id || model.selected_concrete_model || "?"}` : "no model route";
    return `${runtimeText} · ${modelText}`;
  }
  if (stage.stage === "authority_approval") {
    return `${detail.action_intents?.length || 0} intent(s) · ${detail.approval_requests?.length || 0} approval(s)`;
  }
  if (stage.stage === "execution") {
    return (detail.actions || []).map((item) => `${item.id}: ${item.status}`).join(" · ") || "no external action";
  }
  if (stage.stage === "verification") {
    return `${detail.verifications?.length || 0} verification set(s) · ${detail.evidence?.length || 0} evidence record(s)`;
  }
  if (stage.stage === "human_attention") {
    return `${detail.attention_items?.length || 0} Attention item(s)`;
  }
  return detail.reason || detail.event_type || "";
}

function pipelineMarkup(stages = []) {
  if (!stages.length) return "";
  return `
    <div class="orch-pipeline" aria-label="Orchestration pipeline">
      ${stages.map((stage) => `
        <div class="orch-stage">
          <strong>${esc(stage.stage.replaceAll("_", " "))}</strong>
          ${badge(stage.status)}
          <small>${esc(stageSummary(stage))}</small>
        </div>
      `).join("")}
    </div>
  `;
}

function relationLinks(item) {
  const links = [];
  (item.action_intents || []).forEach((intent) => {
    links.push(`<a href="/api/action-intents/${encodeURIComponent(intent.id)}" target="_blank" rel="noreferrer">ActionIntent ${esc(intent.id)}</a>`);
  });
  (item.approval_requests || []).forEach((approval) => {
    links.push(`<a href="/api/approval-requests/${encodeURIComponent(approval.id)}" target="_blank" rel="noreferrer">Approval ${esc(approval.id)}</a>`);
  });
  (item.attention_items || []).forEach((attention) => {
    links.push(`<a href="/api/attention/${encodeURIComponent(attention.id)}" target="_blank" rel="noreferrer">Attention ${esc(attention.id)}</a>`);
  });
  (item.agent_sessions || []).forEach((session) => {
    links.push(`<a href="/api/agent-sessions/${encodeURIComponent(session.id)}" target="_blank" rel="noreferrer">AgentSession ${esc(session.id)}</a>`);
  });
  (item.evidence || []).forEach((evidence) => {
    links.push(`<a href="/api/evidence/${encodeURIComponent(evidence.id)}" target="_blank" rel="noreferrer">Evidence ${esc(evidence.id)}</a>`);
  });
  return links.length ? `<div class="orch-links">${links.join("")}</div>` : "";
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
        <span><strong>Idempotency:</strong> ${esc(event.idempotency_key || "—")}</span>
        <span><strong>Filter:</strong> ${esc(item.filtering_result)}</span>
        <span><strong>Reasoning:</strong> ${esc(reasoningText)}</span>
        <span><strong>Correlation:</strong> ${esc(event.correlation_id || "—")}</span>
        <span><strong>Causation:</strong> ${esc(event.causation_id || "—")}</span>
      </div>
      ${pipelineMarkup(item.pipeline)}
      ${relationLinks(item)}
      ${cycles.map(cycleMarkup).join("")}
    </article>
  `;
}

function renderDependencies(dependencies = {}) {
  return Object.entries(dependencies).map(([name, item]) => (
    `<div class="orch-dependency"><strong>${esc(name.replaceAll("_", " "))}:</strong> ${item.available ? "available" : "unavailable"} · #${esc(item.issue)} · ${esc(item.reason || "")}</div>`
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

function recurrenceText(schedule) {
  if (!schedule.recurrence) return "one shot";
  if (schedule.recurrence.kind === "interval") return `every ${schedule.recurrence.interval_seconds}s`;
  return `daily ${schedule.recurrence.local_time} · ${schedule.recurrence.timezone}`;
}

function scheduleMarkup(schedule) {
  const firings = schedule.recent_firings || [];
  const active = schedule.status === "active";
  const paused = schedule.status === "paused";
  return `
    <article class="orch-record" data-schedule-record="${esc(schedule.id)}">
      <div class="orch-record-head">
        <div><strong>${esc(schedule.name)}</strong><div><code>${esc(schedule.id)}</code></div></div>
        ${badge(schedule.status)}
      </div>
      <div class="orch-kv">
        <span><strong>Owner/scope:</strong> ${esc(schedule.created_by)} · ${esc(schedule.tenant_id)}/${esc(schedule.workspace_id || "all workspaces")}</span>
        <span><strong>Trigger:</strong> ${esc(schedule.trigger_type)}</span>
        <span><strong>Recurrence:</strong> ${esc(recurrenceText(schedule))}</span>
        <span><strong>Next:</strong> ${esc(formatTime(schedule.next_run_at))}</span>
        <span><strong>Last fired:</strong> ${esc(formatTime(schedule.last_fired_at))}</span>
        <span><strong>Misfire:</strong> ${esc(schedule.misfire_policy)} · grace ${esc(schedule.misfire_grace_seconds)}s · catch-up ${esc(schedule.catch_up_limit)}</span>
      </div>
      <div class="orch-actions">
        ${active ? `<button type="button" class="ghost-button" data-schedule-action="pause" data-schedule-id="${esc(schedule.id)}">Pause</button>` : ""}
        ${paused ? `<button type="button" class="ghost-button" data-schedule-action="resume" data-schedule-id="${esc(schedule.id)}">Resume</button>` : ""}
        ${!["cancelled", "completed"].includes(schedule.status) ? `<button type="button" class="ghost-button" data-schedule-action="cancel" data-schedule-id="${esc(schedule.id)}">Cancel</button>` : ""}
      </div>
      <div class="orch-sublist">
        ${firings.length ? firings.map((firing) => `
          <div class="orch-subrecord">
            <a href="/api/orchestration/inspector?source=${encodeURIComponent(schedule.canonical_event_source)}" target="_blank" rel="noreferrer">${esc(firing.event_id)}</a>
            · scheduled ${esc(formatTime(firing.scheduled_for))} · fired ${esc(formatTime(firing.occurred_at))}
          </div>
        `).join("") : '<small>No canonical firing events recorded yet.</small>'}
      </div>
    </article>
  `;
}

function modelPins(run) {
  return (run.models || []).map((model) =>
    `${model.provider_id}/${model.model_id}${model.model_version ? `@${model.model_version}` : ""} · prompt ${model.prompt_template_id}@${model.prompt_template_version} · policy ${model.policy_fingerprint_sha256?.slice(0, 12) || "—"}…`
  ).join(" · ") || "none";
}

function evaluationRunMarkup(run) {
  const failed = (run.assertions || []).filter((assertion) => !assertion.passed);
  const trace = run.trace || {};
  return `
    <article class="orch-record">
      <div class="orch-record-head">
        <div><strong>${esc(run.scenario_id)}@${esc(run.scenario_version)}</strong><div><code>${esc(run.id)}</code></div></div>
        ${badge(run.passed ? "passed" : "failed")} ${badge(run.mode)}
      </div>
      <div class="orch-kv">
        <span><strong>Fixture:</strong> ${esc(run.replay_fixture_id)} · ${esc(run.fixture_checksum_sha256?.slice(0, 16) || "—")}…</span>
        <span><strong>Backend:</strong> ${esc(run.backend_id)}</span>
        <span><strong>Runtime:</strong> ${run.runtime ? esc(`${run.runtime.agent_provider_id}/${run.runtime.runtime_id} r${run.runtime.capability_revision}`) : "none"}</span>
        <span><strong>Definitions:</strong> ${esc((run.definitions || []).map((d) => `${d.definition_id}@r${d.revision}`).join(", ") || "none")}</span>
        <span><strong>Models/prompts:</strong> ${esc(modelPins(run))}</span>
        <span><strong>Usage:</strong> ${esc(trace.reasoning_calls || 0)} calls · ${esc((trace.input_tokens || 0) + (trace.output_tokens || 0))} tokens · $${esc(trace.cost_usd || 0)}</span>
        <span><strong>Actions/retries:</strong> ${esc(trace.actions?.length || 0)} / ${esc(trace.retries || 0)}</span>
        <span><strong>Failure injection:</strong> ${esc(run.failure_injection || "none")}</span>
      </div>
      ${failed.length ? `<div class="orch-sublist"><strong>Failed invariants</strong>${failed.map((item) => `<div class="orch-subrecord">${esc(item.kind)} · expected ${esc(item.expected)} · actual ${esc(item.actual)}${item.detail ? ` · ${esc(item.detail)}` : ""}</div>`).join("")}</div>` : ""}
      <div class="orch-links">
        ${run.evidence_id ? `<a href="/api/evidence/${encodeURIComponent(run.evidence_id)}" target="_blank" rel="noreferrer">Evidence ${esc(run.evidence_id)}</a>` : ""}
        ${run.baseline_run_id ? `<a href="/api/evaluations/runs/${encodeURIComponent(run.baseline_run_id)}" target="_blank" rel="noreferrer">Baseline ${esc(run.baseline_run_id)}</a>` : ""}
      </div>
    </article>
  `;
}

function comparisonMarkup(item) {
  return `
    <article class="orch-record">
      <div class="orch-record-head">
        <strong>Candidate vs baseline · ${esc(item.scenario_id)}@${esc(item.scenario_version)}</strong>
        ${badge(item.passed ? "within thresholds" : "regression")}
      </div>
      <div class="orch-kv">
        <span><strong>Baseline:</strong> ${esc(item.baseline_run_id)}</span>
        <span><strong>Candidate:</strong> ${esc(item.candidate_run_id)}</span>
        <span><strong>Tokens:</strong> ${esc(item.token_delta >= 0 ? "+" : "")}${esc(item.token_delta)}</span>
        <span><strong>Cost:</strong> ${esc(item.cost_delta_usd >= 0 ? "+" : "")}$${esc(item.cost_delta_usd)}</span>
        <span><strong>Latency:</strong> ${esc(item.latency_delta_seconds >= 0 ? "+" : "")}${esc(item.latency_delta_seconds)}s</span>
        <span><strong>Actions:</strong> ${esc(item.action_delta >= 0 ? "+" : "")}${esc(item.action_delta)}</span>
        <span><strong>Interventions:</strong> ${esc(item.intervention_delta >= 0 ? "+" : "")}${esc(item.intervention_delta)}</span>
        <span><strong>Pin changes:</strong> definitions ${esc(item.definition_resolution_changed)} · runtime ${esc(item.runtime_changed)} · model/prompt ${esc(item.model_or_prompt_changed)}</span>
      </div>
      ${(item.regression_reasons || []).length ? `<div class="orch-sublist">${item.regression_reasons.map((reason) => `<div class="orch-subrecord orch-regression">${esc(reason)}</div>`).join("")}</div>` : ""}
    </article>
  `;
}

function attentionMarkup(item) {
  return `
    <article class="orch-record">
      <div class="orch-record-head">
        <div><strong>${esc(item.type)}</strong><div><code>${esc(item.id)}</code></div></div>
        ${badge(item.severity)} ${badge(item.status)}
      </div>
      <p>${esc(item.reason)}</p>
      <div class="orch-kv">
        <span><strong>Source:</strong> ${esc(item.source?.object_type)} · ${esc(item.source?.object_id)}</span>
        <span><strong>Owner:</strong> ${esc(item.owner_identity_id || "unassigned")}</span>
        <span><strong>Due:</strong> ${esc(formatTime(item.due_at))}</span>
        <span><strong>Escalations:</strong> ${esc(item.escalation_count || 0)}</span>
      </div>
      <div class="orch-links"><a href="/api/attention/${encodeURIComponent(item.id)}" target="_blank" rel="noreferrer">Open canonical Attention item</a></div>
    </article>
  `;
}

function approvalMarkup(item) {
  const approved = (item.decisions || []).filter((decision) => decision.outcome === "approve").length;
  return `
    <article class="orch-record">
      <div class="orch-record-head">
        <div><strong>${esc(item.target?.operation)}</strong><div><code>${esc(item.id)}</code></div></div>
        ${badge(item.status)}
      </div>
      <div class="orch-kv">
        <span><strong>Target:</strong> ${esc(item.target?.object_type)} · ${esc(item.target?.object_id)} · ${esc(item.target?.target_version || "unversioned")}</span>
        <span><strong>Quorum:</strong> ${esc(approved)}/${esc(item.requirement?.quorum || 1)} · distinct humans ${esc(item.requirement?.distinct_humans)}</span>
        <span><strong>Assurance:</strong> ${esc(item.requirement?.required_assurance)}</span>
        <span><strong>Policy:</strong> ${esc(item.policy_source || "—")}</span>
        <span><strong>Authority:</strong> ${esc(item.authority_source || "—")}</span>
        <span><strong>Expires:</strong> ${esc(formatTime(item.expires_at))}</span>
      </div>
      <div class="orch-links"><a href="/api/approval-requests/${encodeURIComponent(item.id)}" target="_blank" rel="noreferrer">Open canonical ApprovalRequest</a></div>
    </article>
  `;
}

function render(panel, data) {
  const control = data.control || {};
  panel.querySelector("[data-orch-mode]").textContent = control.mode || "unknown";
  panel.querySelector("[data-orch-dry-run]").checked = Boolean(control.dry_run);
  panel.querySelector("[data-orch-simulation]").checked = Boolean(control.simulation);
  panel.querySelector("[data-orch-limits]").textContent =
    `threshold ${control.reasoning_threshold ?? "—"} · cooldown ${control.cooldown_seconds ?? "—"}s · retries ${control.max_reasoning_attempts ?? "—"} · backoff ${control.reasoning_backoff_seconds ?? "—"}s · depth ${control.max_recursion_depth ?? "—"} · actions/cycle ${control.max_actions_per_cycle ?? "—"}`;

  panel.querySelector("[data-orch-timeline]").innerHTML = data.timeline?.length
    ? data.timeline.map(eventMarkup).join("")
    : '<div class="orch-empty">No matching canonical events.</div>';
  panel.querySelector("[data-orch-dead-letters]").innerHTML = renderDeadLetters(data.dead_letters);
  panel.querySelector("[data-orch-dependencies]").innerHTML = renderDependencies(data.dependencies);

  const schedules = data.schedules || [];
  panel.querySelector("[data-orch-schedule-count]").textContent = schedules.length;
  panel.querySelector("[data-orch-schedules]").innerHTML = schedules.length
    ? schedules.map(scheduleMarkup).join("")
    : '<div class="orch-empty">No canonical schedules in this workspace.</div>';

  const evaluations = data.evaluations || {};
  const runs = evaluations.runs || [];
  const comparisons = evaluations.comparisons || [];
  const suites = evaluations.suite_runs || [];
  panel.querySelector("[data-orch-evaluation-count]").textContent = runs.length;
  panel.querySelector("[data-orch-evaluations]").innerHTML = [
    ...comparisons.map(comparisonMarkup),
    ...runs.map(evaluationRunMarkup),
    ...suites.map((suite) => `
      <article class="orch-record">
        <div class="orch-record-head"><strong>Suite ${esc(suite.suite_id)}</strong>${badge(suite.passed ? "passed" : "failed")}</div>
        <div class="orch-kv"><span><strong>Runs:</strong> ${esc((suite.run_ids || []).join(", "))}</span><span><strong>Comparisons:</strong> ${esc((suite.comparison_ids || []).join(", ") || "none")}</span></div>
        <div class="orch-links">${(suite.evidence_ids || []).map((id) => `<a href="/api/evidence/${encodeURIComponent(id)}" target="_blank" rel="noreferrer">Evidence ${esc(id)}</a>`).join("")}</div>
      </article>
    `),
  ].join("") || '<div class="orch-empty">No evaluation or replay results recorded.</div>';

  const attention = data.attention_items || [];
  panel.querySelector("[data-orch-attention-count]").textContent = attention.length;
  panel.querySelector("[data-orch-attention]").innerHTML = attention.length
    ? attention.map(attentionMarkup).join("")
    : '<div class="orch-empty">No canonical Attention items.</div>';

  const approvals = data.approval_requests || [];
  panel.querySelector("[data-orch-approval-count]").textContent = approvals.length;
  panel.querySelector("[data-orch-approvals]").innerHTML = approvals.length
    ? approvals.map(approvalMarkup).join("")
    : '<div class="orch-empty">No canonical ApprovalRequests.</div>';

  panel.querySelectorAll("[data-schedule-action]").forEach((button) => {
    button.addEventListener("click", () => mutate(
      panel,
      `/api/schedules/${encodeURIComponent(button.dataset.scheduleId)}/${button.dataset.scheduleAction}`,
      { method: "POST" },
      "Applying canonical schedule transition…",
    ));
  });
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
    status.textContent = `${data.event_count} events · ${data.cycle_count} recent cycles · read-only refresh (no reasoning, provider polling, or evaluator execution)`;
  } catch (error) {
    status.dataset.error = "true";
    status.textContent = `Inspector unavailable: ${error.message}`;
  }
}

async function mutate(panel, path, options = {}, pending = "Applying canonical autonomy control…") {
  const status = panel.querySelector("[data-orch-status]");
  status.dataset.error = "false";
  status.textContent = pending;
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
        <small>Event → deterministic filter → reasoning gate → routing → authority/approval → execution → verification → Attention</small>
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
      <summary>Durable schedules <span class="orch-section-count" data-orch-schedule-count>0</span></summary>
      <div class="orch-record-list" data-orch-schedules></div>
    </details>
    <details>
      <summary>Evaluation & replay <span class="orch-section-count" data-orch-evaluation-count>0</span></summary>
      <div class="orch-record-list" data-orch-evaluations></div>
    </details>
    <details>
      <summary>ApprovalRequests <span class="orch-section-count" data-orch-approval-count>0</span></summary>
      <div class="orch-record-list" data-orch-approvals></div>
    </details>
    <details>
      <summary>Human Attention <span class="orch-section-count" data-orch-attention-count>0</span></summary>
      <div class="orch-record-list" data-orch-attention></div>
    </details>
    <details>
      <summary>Dead letters</summary>
      <div data-orch-dead-letters></div>
    </details>
    <details>
      <summary>Canonical inspector foundations</summary>
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
