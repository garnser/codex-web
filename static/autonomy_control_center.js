const accEsc = (value) => String(value ?? "")
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;")
  .replaceAll("'", "&#039;");

async function accRequest(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const payload = await response.json();
      detail = payload?.detail?.message || payload?.detail || detail;
    } catch {}
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return response.json();
}

function accInstallStyles() {
  if (document.getElementById("autonomy-control-center-styles")) return;
  const style = document.createElement("style");
  style.id = "autonomy-control-center-styles";
  style.textContent =
    "#autonomy-control-center-card{grid-column:1/-1;min-width:0}" +
    ".acc-head,.acc-toolbar,.acc-actions{display:flex;gap:.5rem;flex-wrap:wrap;align-items:center}" +
    ".acc-head{justify-content:space-between;align-items:flex-start}.acc-head small{display:block;opacity:.72;max-width:72rem}" +
    ".acc-toolbar{margin:.6rem 0}.acc-summary{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:.5rem;margin:.7rem 0}" +
    ".acc-tile,.acc-section{border:1px solid color-mix(in srgb,currentColor 15%,transparent);border-radius:.55rem;padding:.65rem;min-width:0}" +
    ".acc-tile span,.acc-tile strong{display:block;overflow-wrap:anywhere}.acc-tile span{font-size:.76rem;opacity:.7}" +
    ".acc-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:.65rem}.acc-wide{grid-column:1/-1}" +
    ".acc-row{padding:.5rem 0;border-top:1px solid color-mix(in srgb,currentColor 10%,transparent);overflow-wrap:anywhere}.acc-row:first-child{border-top:0}" +
    ".acc-row small{display:block;opacity:.75;margin-top:.15rem}.acc-alert{border-left:3px solid var(--danger,#b42318);padding:.5rem .65rem;margin:.4rem 0}" +
    ".acc-pill{display:inline-flex;border:1px solid color-mix(in srgb,currentColor 20%,transparent);border-radius:999px;padding:.1rem .4rem;font-size:.74rem;margin:.15rem}" +
    ".acc-status{font-size:.82rem;opacity:.78;min-height:1.1rem}.acc-status[data-error=true]{color:var(--danger,#b42318);opacity:1}" +
    ".acc-explain{display:grid;grid-template-columns:minmax(180px,1fr) auto;gap:.5rem;margin:.5rem 0}" +
    ".acc-chain{display:grid;grid-template-columns:repeat(8,minmax(145px,1fr));gap:.4rem;overflow-x:auto;padding:.4rem 0}" +
    ".acc-stage{border:1px solid color-mix(in srgb,currentColor 15%,transparent);border-radius:.45rem;padding:.5rem}.acc-stage strong{display:block;font-size:.73rem;text-transform:uppercase}.acc-stage small{display:block;opacity:.75;overflow-wrap:anywhere}" +
    ".acc-json{max-height:22rem;overflow:auto;font-size:.74rem}" +
    "@media(max-width:900px){.acc-grid{grid-template-columns:1fr}.acc-wide{grid-column:auto}.acc-chain{grid-template-columns:1fr;overflow:visible}}" +
    "@media(max-width:600px){.acc-toolbar button{flex:1 1 auto;min-height:40px}.acc-explain{grid-template-columns:1fr}.acc-summary{grid-template-columns:repeat(2,minmax(0,1fr))}}";
  document.head.appendChild(style);
}

function accLink(path, label) {
  return '<a href="' + accEsc(path) + '" target="_blank" rel="noreferrer">' + accEsc(label) + "</a>";
}

function accRow(title, detail, links) {
  return '<div class="acc-row"><strong>' + accEsc(title) + "</strong>" +
    (detail ? "<small>" + accEsc(detail) + "</small>" : "") +
    (links ? '<div class="acc-actions">' + links + "</div>" : "") + "</div>";
}

function accEmpty(text) {
  return accRow(text, "", "");
}

function accJson(value) {
  return '<pre class="acc-json">' + accEsc(JSON.stringify(value ?? null, null, 2)) + "</pre>";
}

function accSetStatus(card, text, error) {
  const host = card.querySelector("[data-acc-status]");
  host.textContent = text;
  host.dataset.error = error ? "true" : "false";
}

function accRender(card, data) {
  const control = data.autonomy?.control || {};
  const effective = data.autonomy?.effective_policy || {};
  const recovery = data.recovery || {};
  const capacity = data.capacity?.health || {};
  const audit = data.audit?.integrity || {};
  const replication = data.replication || {};
  card.querySelector("[data-acc-mode]").textContent = control.mode || "unknown";
  card.querySelector("[data-acc-dry]").checked = Boolean(control.dry_run);
  card.querySelector("[data-acc-sim]").checked = Boolean(control.simulation);
  const pauses = data.autonomy?.scoped_pauses || control.scoped_pauses || [];

  const tiles = [
    ["Mode", control.mode || "unknown"],
    ["Autonomy", effective.level || "unknown"],
    ["Blockers", (data.blockers || []).length],
    ["Approvals", data.approvals?.count || 0],
    ["Attention", data.attention?.count || 0],
    ["Incidents", data.incidents?.count || 0],
    ["Recovery", recovery.recovery_qualified ? "qualified" : "blocked"],
    ["Capacity", capacity.load_shed_mode ? "load shed" : "normal"],
    ["Audit", audit.status || "unknown"],
    ["Coordination", replication.coordination?.shared ? "shared" : "local"],
    ["Upgrades", data.upgrades?.count || 0],
    ["Cycles", data.orchestration?.cycle_count || 0],
  ];
  card.querySelector("[data-acc-summary]").innerHTML = tiles.map(function (item) {
    return '<div class="acc-tile"><span>' + accEsc(item[0]) + "</span><strong>" + accEsc(item[1]) + "</strong></div>";
  }).join("");

  const blockers = data.blockers || [];
  card.querySelector("[data-acc-blockers]").innerHTML = blockers.length ? blockers.map(function (item) {
    return '<div class="acc-alert"><strong>' + accEsc(item.domain) + " · " + accEsc(item.severity || "blocking") +
      "</strong><small>" + accEsc((item.reasons || []).join(" · ")) + "</small></div>";
  }).join("") : accRow("No current cross-domain blockers reported.", "Eligibility still follows the effective canonical policy and bound qualification Evidence.", "");

  const qualifications = data.autonomy?.qualification || [];
  card.querySelector("[data-acc-qualification]").innerHTML = qualifications.length ? qualifications.map(function (item) {
    const ids = item.evidence?.evidence_ids || [];
    return accRow(item.gate + " · " + (item.configured ? "configured" : "not configured"),
      ids.length ? "Evidence: " + ids.join(", ") : "No bound Evidence configured", "");
  }).join("") : accEmpty("No required qualification gates.");

  const approvals = data.approvals?.pending || [];
  card.querySelector("[data-acc-approvals]").innerHTML = approvals.length ? approvals.slice(0, 20).map(function (item) {
    const approved = (item.decisions || []).filter(function (d) { return d.outcome === "approve"; }).length;
    const links = accLink("/api/approval-requests/" + encodeURIComponent(item.id), "Open " + item.id) +
      '<button type="button" class="ghost-button" data-acc-approval="' + accEsc(item.id) + '" data-acc-outcome="approve">Approve</button>' +
      '<button type="button" class="ghost-button" data-acc-approval="' + accEsc(item.id) + '" data-acc-outcome="reject">Reject</button>';
    return accRow((item.target?.operation || "approval") + " · " + item.status,
      (item.target?.object_type || "") + " " + (item.target?.object_id || "") + " · quorum " + approved + "/" + (item.requirement?.quorum || 1),
      links);
  }).join("") : accEmpty("No pending canonical ApprovalRequests.");

  const attention = data.attention?.active || [];
  card.querySelector("[data-acc-attention]").innerHTML = attention.length ? attention.slice(0, 20).map(function (item) {
    return accRow(item.severity + " · " + item.type + " · " + item.status,
      item.reason + " · source " + (item.source?.object_type || "") + " " + (item.source?.object_id || ""),
      accLink("/api/attention/" + encodeURIComponent(item.id), "Open " + item.id));
  }).join("") : accEmpty("No active canonical Attention items.");

  const incidents = data.incidents?.active || [];
  card.querySelector("[data-acc-incidents]").innerHTML = incidents.length ? incidents.slice(0, 20).map(function (item) {
    return accRow(item.severity + " · " + item.title + " · " + item.status,
      "Commander " + (item.commander_identity_id || "unassigned") + " · resources " + ((item.affected_resource_ids || []).join(", ") || "none"),
      accLink("/api/incidents/" + encodeURIComponent(item.id), "Open " + item.id));
  }).join("") : accEmpty("No active incidents.");

  const latest = data.releases?.latest;
  card.querySelector("[data-acc-releases]").innerHTML = latest ? accRow(
    latest.name + " " + latest.version + " · " + latest.status,
    "Artifact " + (latest.build?.artifact_id || "—") + " · " + (latest.build?.digest || "—") +
      " · source " + (latest.build?.source_revision || "—") + " · SBOM " + (latest.build?.sbom_artifact_id || "missing") +
      " · rollback " + (latest.rollback_release_id || "none"),
    accLink("/api/releases/" + encodeURIComponent(latest.id), "Open " + latest.id)
  ) + (latest.promotions || []).map(function (item) {
    return accRow(item.environment_name + " · " + item.status,
      item.artifact_digest + " · rollout " + (item.rollout?.strategy || "—") + " · blockers " + ((item.blockers || []).join(", ") || "none"), "");
  }).join("") : accEmpty("No canonical releases.");

  card.querySelector("[data-acc-recovery]").innerHTML = accRow(
    recovery.recovery_qualified ? "Recovery qualified" : "Recovery not qualified",
    "Backup " + (recovery.latest_backup_id || "none") + " · RPO " + (recovery.rpo_satisfied ? "pass" : "fail") +
      " · restore " + (recovery.latest_restore_verification_id || "none") + " · RTO " + (recovery.rto_satisfied ? "pass" : "fail") +
      " · blockers " + ((recovery.blockers || []).join(", ") || "none"),
    accLink("/api/recovery/status", "Recovery state")
  );

  const providerRecords = data.capacity?.provider_records || [];
  card.querySelector("[data-acc-capacity]").innerHTML = accRow(
    capacity.load_shed_mode ? "Load-shed mode" : "Capacity normal",
    "Tenant inflight " + (capacity.active_tenant || 0) + " · global " + (capacity.active_global || 0) +
      " · open circuits " + ((capacity.open_circuits || []).join(", ") || "none") +
      " · recent deferred " + (capacity.recent_deferred || 0), ""
  ) + providerRecords.map(function (item) {
    return accRow(item.provider_id + "/" + (item.runtime_id || "*") + " · " + item.status,
      item.reason || "available", "");
  }).join("");

  const upgrades = data.upgrades?.active || [];
  card.querySelector("[data-acc-upgrades]").innerHTML = upgrades.length ? upgrades.map(function (item) {
    const preflight = item.preflight ? (item.preflight.satisfied ? "preflight pass" : "preflight blocked: " + (item.preflight.blockers || []).join(", ")) : "preflight not run";
    return accRow(item.current_app_version + " → " + item.target_app_version + " · " + item.status,
      preflight + " · maintenance " + item.maintenance_mode + " · rollback " + item.rollback_available + " · irreversible " + item.irreversible_boundary_crossed,
      accLink("/api/upgrades/" + encodeURIComponent(item.id), "Open " + item.id));
  }).join("") : accEmpty("No active upgrade plans.");

  const workers = data.execution_plane?.workers || [];
  const assignments = data.execution_plane?.assignments || [];
  const activeAssignments = assignments.filter(function (item) { return ["pending", "claimed", "running"].includes(item.status); });
  card.querySelector("[data-acc-execution]").innerHTML = accRow(
    workers.length + " worker(s) · " + activeAssignments.length + " active assignment(s)",
    "Coordination " + (replication.coordination?.backend_id || "unknown") + " · shared " + Boolean(replication.coordination?.shared) +
      " · instance " + (replication.ownership?.instanceId || "unknown") + " · leases " + (replication.coordination?.active_leases || 0), ""
  ) + workers.slice(0, 15).map(function (item) {
    return accRow(item.id + " · " + item.lifecycle + " · " + item.version,
      "Pool " + item.pool + " · contracts " + ((item.supported_execution_contract_versions || []).join(", ") || "none"), "");
  }).join("") + "<details><summary>Ownership / fencing</summary>" + accJson(replication.ownership?.responsibilities || {}) + "</details>";

  const metrics = data.audit?.metrics || {};
  card.querySelector("[data-acc-scoped-pauses]").innerHTML = pauses.length ? pauses.map(function (item) {
    return accRow(item.scope + " · " + item.scope_id,
      item.reason + (item.expires_at ? " · expires " + item.expires_at : ""),
      '<button type="button" class="ghost-button" data-acc-unpause="' + accEsc(item.id) + '">Remove pause</button>');
  }).join("") : accEmpty("No active scoped pauses.");

  const sim = data.simulation || {};
  card.querySelector("[data-acc-simulation]").innerHTML =
    accRow(
      "Dry-run " + Boolean(sim.dry_run_enabled) + " · simulation " + Boolean(sim.simulation_enabled),
      "Simulated actions " + (sim.simulated_action_count || 0) +
        " · executed actions " + (sim.executed_action_count || 0) +
        " · evaluation records " + (sim.evaluation_count || 0),
      ""
    ) +
    "<details><summary>Recent simulated / dry-run cycles</summary>" + accJson(sim.simulated_cycles || []) + "</details>" +
    "<details><summary>Recent executed/prepared cycles</summary>" + accJson(sim.executed_cycles || []) + "</details>";

  card.querySelector("[data-acc-audit]").innerHTML = accRow(
    "Audit integrity · " + (audit.status || "unknown"),
    "Root " + (audit.root_hash || "—") + " · checkpoint " + (audit.checkpoint_id || "—") + " · " + (audit.reason || "no integrity failure"),
    '<button type="button" class="ghost-button" data-acc-verify-audit>Verify + Evidence</button>' +
      accLink("/api/autonomy/audit/checkpoints", "Checkpoints")
  ) + "<details><summary>Reliability / efficiency metrics</summary>" + accJson(metrics) + "</details>" +
    (data.audit?.active_signals || []).map(function (item) {
      return '<div class="acc-alert"><strong>' + accEsc(item.severity) + " · " + accEsc(item.signal_type || item.id) +
        "</strong><small>" + accEsc(item.reason) + "</small></div>";
    }).join("");

  card.querySelectorAll("[data-acc-approval]").forEach(function (button) {
    button.addEventListener("click", async function () {
      const id = button.dataset.accApproval;
      const outcome = button.dataset.accOutcome;
      const reason = window.prompt("Optional decision reason:") || null;
      try {
        await accRequest("/api/approval-requests/" + encodeURIComponent(id) + "/decisions", {
          method: "POST",
          body: JSON.stringify({
            outcome,
            reason,
            idempotency_key: "control-center:" + id + ":" + outcome + ":" + Date.now(),
          }),
        });
        await accLoad(card);
      } catch (error) {
        accSetStatus(card, "Approval decision failed: " + error.message, true);
      }
    });
  });
  card.querySelectorAll("[data-acc-unpause]").forEach(function (button) {
    button.addEventListener("click", function () {
      accMutate(
        card,
        "/api/autonomy/scoped-pauses/" + encodeURIComponent(button.dataset.accUnpause),
        { method: "DELETE" },
        "Removing scoped pause…"
      );
    });
  });
  card.querySelector("[data-acc-add-pause]")?.addEventListener("click", function () {
    const scope = card.querySelector("[data-acc-pause-scope]").value;
    const scopeId = card.querySelector("[data-acc-pause-id]").value.trim();
    const reason = card.querySelector("[data-acc-pause-reason]").value.trim();
    if (!scopeId || !reason) {
      accSetStatus(card, "Scoped pause requires a scope ID and reason.", true);
      return;
    }
    accMutate(
      card,
      "/api/autonomy/scoped-pauses",
      { method: "POST", body: JSON.stringify({ scope, scope_id: scopeId, reason }) },
      "Applying scoped pause…"
    );
  });
  card.querySelector("[data-acc-verify-audit]")?.addEventListener("click", function () {
    accMutate(card, "/api/autonomy/audit/verify?publish_evidence=true", { method: "POST" }, "Verifying audit integrity…");
  });
}

function accStage(title, detail) {
  return '<div class="acc-stage"><strong>' + accEsc(title) + "</strong><small>" + accEsc(detail || "not recorded") + "</small></div>";
}

function accRenderExplain(card, data) {
  const authority = data.authority?.recheck || data.authority?.initial || {};
  const action = data.action_intent || {};
  const provider = data.provider_action || {};
  const evidence = data.evidence || [];
  const verification = data.verification || {};
  const blast = data.blast_radius || {};
  const trigger = data.trigger;
  const chain =
    accStage("Trigger / request", trigger ? trigger.event_type + " · " + trigger.source + " · " + trigger.event_id : (action.request?.action_id || action.action_id || "recorded request")) +
    accStage("Goal / decision", "goal " + (data.goal_id || "none") + " · decision " + (data.decision_id || "none") + " · work " + (data.work_item_ref || "none")) +
    accStage("Role / authority", (authority.outcome || "unknown") + " · " + (authority.reason || "no reason")) +
    accStage("Effective policy", (data.policy?.outcome || "unknown") + " · " + (data.policy?.source || "unknown") + " · " + (data.policy?.reason || "no reason")) +
    accStage("Execution contract", (data.execution_contract?.execution_id || "none") + " · action " + (data.execution_contract?.action_definition?.action_id || provider.action_id || "unknown")) +
    accStage("ActionIntent", data.intent_id + " · " + (data.result?.status || action.status || "unknown") + " · resources " + ((action.resource_ids || []).join(", ") || "none")) +
    accStage("Provider action", (provider.provider_type || "?") + "/" + (provider.provider_instance || "?") + " · " + (provider.action_id || "?") + " · receipts " + ((provider.receipts || []).length)) +
    accStage("Evidence / result", evidence.length + " evidence · " + ((verification.receipts || []).length) + " verification(s) · " + (data.result?.status || "unknown")) +
    accStage("Blast radius", (blast.high_impact ? "HIGH IMPACT · " : "") + (blast.resource_count || 0) + " resource(s) · risk " + (blast.risk_class || "unknown") + " · provider " + (blast.provider || "unknown"));
  card.querySelector("[data-acc-explain-result]").innerHTML =
    '<div class="acc-chain">' + chain + "</div>" +
    '<div class="acc-actions">' + accLink("/api/action-intents/" + encodeURIComponent(data.intent_id) + "/history", "Action history") +
    (data.approval_requests || []).map(function (item) { return accLink("/api/approval-requests/" + encodeURIComponent(item.id), "Approval " + item.id); }).join("") +
    (data.attention_items || []).map(function (item) { return accLink("/api/attention/" + encodeURIComponent(item.id), "Attention " + item.id); }).join("") +
    "</div><details><summary>Exact stored provenance</summary>" + accJson(data) + "</details>";
}

async function accExplain(card) {
  const id = card.querySelector("[data-acc-intent]").value.trim();
  if (!id) {
    accSetStatus(card, "Enter an ActionIntent ID to explain.", true);
    return;
  }
  card.querySelector("[data-acc-explain-result]").innerHTML = accEmpty("Loading stored provenance…");
  try {
    const data = await accRequest("/api/autonomy/control-center/actions/" + encodeURIComponent(id) + "/explain");
    accRenderExplain(card, data);
    accSetStatus(card, "Explain Action loaded for " + id + " without a model call.", false);
  } catch (error) {
    card.querySelector("[data-acc-explain-result]").innerHTML = accEmpty("Explain Action unavailable: " + error.message);
    accSetStatus(card, "Explain Action failed: " + error.message, true);
  }
}

async function accLoad(card) {
  accSetStatus(card, "Loading canonical production-autonomy state…", false);
  try {
    const data = await accRequest("/api/autonomy/control-center");
    accRender(card, data);
    card.dataset.loaded = "true";
    accSetStatus(card, "Canonical state loaded · " + (data.blockers || []).length + " blocker(s) · " +
      (data.orchestration?.cycle_count || 0) + " recent cycle(s) · refresh performs no model/provider work.", false);
  } catch (error) {
    accSetStatus(card, "Control Center unavailable: " + error.message, true);
  }
}

async function accMutate(card, path, options, pending) {
  accSetStatus(card, pending, false);
  try {
    await accRequest(path, options);
    await accLoad(card);
  } catch (error) {
    accSetStatus(card, "Control failed: " + error.message, true);
  }
}

function accBuild() {
  const card = document.createElement("div");
  card.id = "autonomy-control-center-card";
  card.className = "developer-card";
  card.innerHTML =
    '<div class="acc-head"><div><h2>Autonomy Control Center</h2><small>Canonical M11 production controls and readiness across policy, approvals, Attention, incidents, releases, recovery, capacity, upgrades, workers, audit integrity and replicated ownership.</small></div><button type="button" class="ghost-button" data-acc-refresh>Refresh</button></div>' +
    '<div class="acc-toolbar"><strong>Mode: <span data-acc-mode>loading</span></strong><button type="button" class="ghost-button" data-acc-pause>Pause</button><button type="button" class="ghost-button" data-acc-resume>Resume</button><button type="button" class="ghost-button" data-acc-kill>Global kill</button><label><input type="checkbox" data-acc-dry> Dry-run</label><label><input type="checkbox" data-acc-sim> Simulation</label><a href="#orchestration-inspector-card">Orchestration Inspector</a></div>' +
    '<div class="acc-status" data-acc-status>Open Developer tools or refresh to load canonical state.</div><div class="acc-summary" data-acc-summary></div>' +
    '<div class="acc-grid">' +
      '<section class="acc-section acc-wide"><h3>Production readiness & qualification</h3><div data-acc-blockers></div><details><summary>Required qualification Evidence</summary><div data-acc-qualification></div></details></section>' +
      '<section class="acc-section"><h3>Pending approvals</h3><div data-acc-approvals></div></section>' +
      '<section class="acc-section"><h3>Human Attention</h3><div data-acc-attention></div></section>' +
      '<section class="acc-section"><h3>Incidents & escalation</h3><div data-acc-incidents></div></section>' +
      '<section class="acc-section"><h3>Release & promotion</h3><div data-acc-releases></div></section>' +
      '<section class="acc-section"><h3>Recovery / DR</h3><div data-acc-recovery></div></section>' +
      '<section class="acc-section"><h3>Capacity & provider pressure</h3><div data-acc-capacity></div></section>' +
      '<section class="acc-section"><h3>Upgrade / migration</h3><div data-acc-upgrades></div></section>' +
      '<section class="acc-section"><h3>Execution plane & failover</h3><div data-acc-execution></div></section>' +
      '<section class="acc-section"><h3>Scoped pause controls</h3><div class="acc-toolbar"><select data-acc-pause-scope><option value="project">Project</option><option value="identity">Agent / identity</option><option value="resource">Resource</option></select><input data-acc-pause-id placeholder="scope ID"><input data-acc-pause-reason placeholder="reason"><button type="button" class="ghost-button" data-acc-add-pause>Pause scope</button></div><div data-acc-scoped-pauses></div></section>' +
      '<section class="acc-section"><h3>Simulation / dry-run comparison</h3><div data-acc-simulation></div></section>' +
      '<section class="acc-section acc-wide"><h3>Audit integrity & reliability</h3><div data-acc-audit></div></section>' +
      '<section class="acc-section acc-wide"><h3>Explain this action</h3><small>Trace exact stored provenance from trigger/request through Goal/Decision, authority/policy, execution contract, ActionIntent, provider receipt, Evidence and verification. No reasoning call is made.</small><div class="acc-explain"><input data-acc-intent placeholder="action-intent-…"><button type="button" class="ghost-button" data-acc-explain>Explain Action</button></div><div data-acc-explain-result>' + accEmpty("Enter an ActionIntent ID.") + "</div></section>" +
    "</div>";
  card.querySelector("[data-acc-refresh]").addEventListener("click", function () { accLoad(card); });
  card.querySelector("[data-acc-pause]").addEventListener("click", function () { accMutate(card, "/api/autonomy/pause", { method: "POST" }, "Pausing autonomy…"); });
  card.querySelector("[data-acc-resume]").addEventListener("click", function () { accMutate(card, "/api/autonomy/resume", { method: "POST" }, "Resuming autonomy…"); });
  card.querySelector("[data-acc-kill]").addEventListener("click", function () { accMutate(card, "/api/autonomy/kill", { method: "POST" }, "Applying global kill…"); });
  card.querySelector("[data-acc-dry]").addEventListener("change", function (event) { accMutate(card, "/api/autonomy/control", { method: "PATCH", body: JSON.stringify({ dry_run: event.target.checked }) }, "Updating dry-run…"); });
  card.querySelector("[data-acc-sim]").addEventListener("change", function (event) { accMutate(card, "/api/autonomy/control", { method: "PATCH", body: JSON.stringify({ simulation: event.target.checked }) }, "Updating simulation…"); });
  card.querySelector("[data-acc-explain]").addEventListener("click", function () { accExplain(card); });
  card.querySelector("[data-acc-intent]").addEventListener("keydown", function (event) { if (event.key === "Enter") accExplain(card); });
  return card;
}

function accInstall() {
  const grid = document.querySelector("#developer-panel .developer-grid");
  if (!grid || document.getElementById("autonomy-control-center-card")) return;
  accInstallStyles();
  const card = accBuild();
  grid.prepend(card);
  const developer = document.getElementById("developer-panel");
  developer?.addEventListener("toggle", function () {
    if (developer.open && !card.dataset.loaded) accLoad(card);
  });
  if (developer?.open) accLoad(card);
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", accInstall, { once: true });
} else {
  accInstall();
}
