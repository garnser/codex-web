(async () => {
  const BASE = window.location.pathname.startsWith("/codex") ? "/codex" : "";
  const { confirmAction } = await import(`${BASE}/static/action_confirmation.js`);
  const { request: apiRequest } = await import(`${BASE}/static/api_client.js`);
  const { referenceLink } = await import(`${BASE}/static/reference_navigation.js`);
  const { referenceAttributes, focusReference } = await import(`${BASE}/static/reference_links.js`);
  const { captureProjectView, currentProjectId } = await import(`${BASE}/static/project_view_scope.js`);
  const { decisionHtml, leaseHtml, failureHtml, recoveryHtml } = await import(`${BASE}/static/action_intent_outcome_view.js`);
  let actor = null;
  let intents = [];
  let refreshGeneration = 0;

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;").replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;").replaceAll('"', "&quot;");
  }

  function timeText(value) {
    if (!value) return "none";
    const date = new Date(Number(value) * 1000);
    return Number.isNaN(date.valueOf()) ? "unknown" : date.toLocaleString();
  }

  function listText(values) {
    return values?.length ? values.map(escapeHtml).join(", ") : "none";
  }

  function setStatus(message) {
    const host = document.getElementById("action-intent-status");
    if (host) {
      host.hidden = false;
      host.textContent = message;
    }
  }

  function setRecoveryStatus(message) {
    const host = document.getElementById("action-intent-recovery-status");
    if (host) {
      host.hidden = false;
      host.textContent = message;
    }
  }

  function canRecover() {
    if (!actor) return false;
    if (actor.principal_kind === "service") {
      return (actor.service_scopes || []).includes("action-intent:admin");
    }
    return ["mfa", "local_trusted"].includes(actor.assurance)
      && (actor.roles || []).some((role) => ["owner", "admin"].includes(role));
  }

  function renderAssurance() {
    const host = document.getElementById("action-intent-recovery-assurance");
    if (host && actor) {
      host.textContent = `Current actor ${actor.identity_id} · ${actor.principal_kind} · assurance ${actor.assurance}. Stale-claim recovery: ${canRecover() ? "allowed" : "blocked"}. Provider execution/reconciliation remains on canonical worker/admin APIs.`;
    } else if (host) host.textContent = 'Current actor authority has not been loaded for this view.';
    const button = document.getElementById("recover-stale-action-intents");
    if (button) button.disabled = !canRecover();
  }

  function searchTerm() {
    return (document.getElementById("action-intent-search")?.value || "").trim().toLowerCase();
  }

  function statusFilter() {
    return document.getElementById("action-intent-status-filter")?.value || "";
  }

  function searchable(intent) {
    const parameterKeys = Object.keys(intent.request?.parameters || {});
    return [
      intent.id, intent.status, intent.work_item_ref, intent.goal_id, intent.decision_id,
      intent.execution_id, intent.binding_id, intent.provider_type, intent.provider_instance,
      intent.action_id, intent.requested_by, intent.correlation_id, intent.causation_id,
      intent.idempotency_key, intent.credential_ref, intent.last_error,
      intent.last_receipt_id, intent.last_verification_id,
      intent.failure?.reason_code, intent.failure?.summary,
      intent.failure?.remediation_key,
      ...(intent.resource_ids || []), ...parameterKeys,
      intent.authority_decision?.source, intent.authority_decision?.reason,
      intent.policy_decision?.source, intent.policy_decision?.reason,
      intent.security_decision?.source, ...(intent.security_decision?.reasons || []),
    ].filter(Boolean).join(" ").toLowerCase();
  }

  function visibleIntents() {
    const term = searchTerm();
    const status = statusFilter();
    return intents.filter((intent) => {
      if (currentProjectId() && intent.project_id && intent.project_id !== currentProjectId()) return false;
      const execution = new URLSearchParams(location.search).get('action_execution');
      if (execution && intent.execution_id !== execution) return false;
      if (status && intent.status !== status) return false;
      return !term || searchable(intent).includes(term);
    });
  }

  function capabilityText(capabilities) {
    if (!capabilities) return "none";
    return Object.entries(capabilities)
      .filter(([, value]) => value === true)
      .map(([key]) => key)
      .join(", ") || "none";
  }

  function renderIntent(intent) {
    const definition = intent.action_definition || {};
    const request = intent.request || {};
    const security = intent.security_decision || {};
    const retry = intent.retry_policy || {};
    const paramKeys = Object.keys(request.parameters || {});
    const publicationRecovery = intent.action_id === "code-host.branch.publish"
      && paramKeys.includes("existing_change_request_number");
    const ref = (kind, id) => referenceLink(kind, id, {projectId:intent.project_id || currentProjectId()});
    return `<details class="comm-entry" ${referenceAttributes('action_intent', intent.id)} data-action-intent-row="${escapeHtml(intent.id)}">
      <summary><strong>${escapeHtml(intent.action_id)} · ${escapeHtml(intent.provider_type)}/${escapeHtml(intent.provider_instance)} · ${escapeHtml(intent.status)}</strong></summary>
      <small>ID: ${escapeHtml(intent.id)} · requested by ${escapeHtml(intent.requested_by)} · created ${timeText(intent.created_at)} · updated ${timeText(intent.updated_at)}</small>
      <small>Project: ${escapeHtml(intent.project_id || "unscoped")} · Work: ${ref('work_item', intent.work_item_ref)} · Goal: ${ref('goal', intent.goal_id)} · Decision: ${ref('decision', intent.decision_id)} · Execution: ${escapeHtml(intent.execution_id || "none")}</small>
      ${publicationRecovery ? `<small>Publication recovery provenance: Work Item ${escapeHtml(intent.work_item_ref || "none")} · original execution ${escapeHtml(intent.execution_id || "none")} · workspace reference retained in the governed request.</small>` : ""}
      <small>Binding: ${escapeHtml(intent.binding_id)} · risk: ${escapeHtml(definition.risk_class || "unknown")} · capabilities: ${escapeHtml(capabilityText(definition.capabilities))}</small>
      <small>Resources: ${(intent.resource_ids || []).map(id => ref('resource',id)).join(', ') || 'none'} · credential reference: ${escapeHtml(intent.credential_ref || "none")} · dry-run: ${request.dry_run ? "yes" : "no"}</small>
      <small>Request parameter keys only: ${listText(paramKeys)}. Parameter values and provider output bodies are intentionally not rendered in this timeline.</small>
      ${decisionHtml("Authority", intent.authority_decision)}
      ${decisionHtml("Policy", intent.policy_decision)}
      <small>Security: ${escapeHtml(security.outcome || "unknown")} · source ${escapeHtml(security.source || "unknown")} · sandbox ${escapeHtml(security.sandbox || "unknown")} · network ${security.network_enabled ? "enabled" : "disabled"} · reasons ${listText(security.reasons)}</small>
      <small>Idempotency key: ${escapeHtml(intent.idempotency_key)} · provider idempotency: ${intent.provider_idempotency_supported ? "yes" : "no"} · attempt ${escapeHtml(intent.attempt)} / ${escapeHtml(retry.max_attempts || "unknown")} · backoff ${escapeHtml(retry.backoff_seconds ?? "unknown")}s</small>
      <small>Verification required: ${intent.verification_required ? "yes" : "no"} · rollback required: ${intent.rollback_required ? "yes" : "no"} · expected evidence rules: ${escapeHtml(intent.expected_evidence?.length || 0)} · timeout ${escapeHtml(intent.timeout_seconds)}s</small>
      ${leaseHtml(intent.lease)}
      <small>Execution started: ${timeText(intent.execution_started_at)} · completed: ${timeText(intent.completed_at)} · not before: ${timeText(intent.not_before)}</small>
      <small>Correlation: ${escapeHtml(intent.correlation_id)} · causation: ${escapeHtml(intent.causation_id || "none")} · last receipt: ${escapeHtml(intent.last_receipt_id || "none")} · last verification: ${escapeHtml(intent.last_verification_id || "none")}</small>
      ${intent.last_error ? `<small>Last error: ${escapeHtml(intent.last_error)}</small>` : ""}
      ${failureHtml(intent.failure)}
      ${recoveryHtml(intent, retry)}
      <button type="button" class="ghost-button" data-action-intent-history="${escapeHtml(intent.id)}">Load side-effect timeline</button>
      <div id="action-intent-history-${escapeHtml(intent.id)}"></div>
    </details>`;
  }

  function render() {
    const host = document.getElementById("action-intent-list");
    if (!host) return;
    const visible = visibleIntents();
    host.innerHTML = visible.map(renderIntent).join("")
      || '<div class="comm-entry"><strong>No ActionIntents match the current filters.</strong></div>';
    focusReference(host);
    const attention = visible.filter((item) => ["uncertain", "requires_reconciliation"].includes(item.status)).length;
    const executing = visible.filter((item) => ["claimed", "executing"].includes(item.status)).length;
    setStatus(`${visible.length} ActionIntent(s) in this view · ${executing} claimed/executing · ${attention} uncertain/reconciliation-required. Durable intent/receipt/verification state is canonical; provider logs are not the source of truth.`);
  }

  function receiptHtml(receipt) {
    const result = receipt.result || null;
    const providerEvidence = result?.evidence || [];
    return `<div class="comm-entry">
      <strong>Provider receipt · attempt ${escapeHtml(receipt.attempt)} · ${escapeHtml(receipt.outcome)}</strong>
      <small>ID: ${escapeHtml(receipt.id)} · received ${timeText(receipt.received_at)} · provider external ID ${escapeHtml(receipt.provider_external_id || result?.external_id || "none")}</small>
      <small>Action: ${escapeHtml(receipt.action_id)} · idempotency ${escapeHtml(receipt.idempotency_key)} · correlation ${escapeHtml(receipt.correlation_id)}</small>
      ${result ? `<small>Provider result: ${escapeHtml(result.status)} · execution ${escapeHtml(result.execution_id)} · started ${timeText(result.started_at)} · completed ${timeText(result.completed_at)}</small>
        <small>Error: ${escapeHtml(result.error_code || "none")} · ${escapeHtml(result.error_message || "none")} · evidence references: ${providerEvidence.map((item) => escapeHtml(item.reference || item.evidence_type)).join(", ") || "none"}</small>
        <small>Provider output body and rollback token are intentionally not rendered.</small>` : "<small>No durable provider result body is attached to this receipt.</small>"}
    </div>`;
  }

  function verificationHtml(item) {
    const provider = item.provider_verification || {};
    return `<div class="comm-entry">
      <strong>Verification receipt · ${item.verified ? "verified" : "not verified"}</strong>
      <small>ID: ${escapeHtml(item.id)} · verified ${timeText(item.verified_at)} · evidence gate ${item.evidence_satisfied == null ? "not evaluated" : item.evidence_satisfied ? "satisfied" : "blocked"}</small>
      <small>Findings: ${listText(item.findings)} · provider verified: ${provider.verified == null ? "not evaluated" : provider.verified ? "yes" : "no"} · provider findings: ${listText(provider.findings)}</small>
      <small>Evidence-evaluation payload is retained canonically but not dumped into this UI; use the Artifact/Evidence explorer for evidence detail.</small>
    </div>`;
  }

  function inboxHtml(item) {
    return `<div class="comm-entry">
      <strong>Provider callback · ${escapeHtml(item.event_type)} · ${item.duplicate ? "duplicate" : "accepted"}</strong>
      <small>Delivery: ${escapeHtml(item.delivery_id)} · received ${timeText(item.received_at)} · processed ${timeText(item.processed_at)} · outcome ${escapeHtml(item.outcome || "none")}</small>
      <small>Provider external ID: ${escapeHtml(item.provider_external_id || "none")} · idempotency: ${escapeHtml(item.idempotency_key || "none")} · correlation: ${escapeHtml(item.correlation_id || "none")}</small>
      <small>Callback payload body is intentionally not rendered.</small>
    </div>`;
  }

  async function loadHistory(button) {
    const view = captureProjectView();
    const id = button.dataset.actionIntentHistory;
    if (!id) return;
    const host = document.getElementById(`action-intent-history-${id}`);
    if (!host) return;
    button.disabled = true;
    host.innerHTML = "<small>Loading canonical side-effect history...</small>";
    try {
      const history = await apiRequest(`/api/action-intents/${encodeURIComponent(id)}/history`);
      if (!view.current() || !host.isConnected) return;
      const receipts = history.receipts || [];
      const verifications = history.verifications || [];
      const inbox = history.inbox || [];
      host.innerHTML = `<div class="comm-entry">
        <strong>Intent → provider execution → receipt → verification/reconciliation</strong>
        <small>${receipts.length} provider receipt(s) · ${verifications.length} verification receipt(s) · ${inbox.length} callback message(s)</small>
      </div>`
        + receipts.map(receiptHtml).join("")
        + inbox.map(inboxHtml).join("")
        + verifications.map(verificationHtml).join("");
    } catch (error) {
      if (view.current() && host.isConnected) host.innerHTML = `<small>ActionIntent history unavailable: ${escapeHtml(error.message)}</small>`;
    } finally {
      button.disabled = false;
    }
  }

  async function recoverStale() {
    if (!canRecover()) {
      return setRecoveryStatus("Stale ActionIntent recovery requires admin + MFA/step-up or action-intent:admin service authority.");
    }
    if (!await confirmAction({ action: 'Recover stale ActionIntents', target: 'Expired canonical worker claims', risk: 'high', consequence: 'Expired CLAIMED intents return to PENDING. Expired EXECUTING intents become UNCERTAIN because external effects may already exist.', recovery: 'Reconcile uncertain outcomes before any safe retry; recovery is not evidence that the provider action failed.' })) return;
    try {
      const result = await apiRequest("/api/action-intents/recover-stale", { method: "POST" });
      setRecoveryStatus(`Recovered ${result.intent_ids?.length || 0} stale ActionIntent claim(s). Executing expiries remain explicitly uncertain until reconciled.`);
      await refresh();
    } catch (error) {
      setRecoveryStatus(`Stale ActionIntent recovery failed: ${error.message}`);
    }
  }

  async function refresh() {
    const view = captureProjectView();
    const revision = ++refreshGeneration;
    actor = null;
    renderAssurance();
    setStatus("Loading canonical ActionIntents...");
    try {
      const [response, me] = await Promise.all([
        apiRequest("/api/action-intents"),
        apiRequest("/api/identity/me"),
      ]);
      if (!view.current() || revision !== refreshGeneration) return;
      intents = response.items || [];
      actor = me;
      renderAssurance();
      render();
    } catch (error) {
      if (!view.current() || revision !== refreshGeneration) return;
      intents = [];
      actor = null;
      renderAssurance();
      render();
      setStatus(`ActionIntent timeline ${[401,403].includes(error.status) ? 'access denied' : 'unavailable'}: ${error.message}. Provider outcomes are unknown until a successful read.`);
    }
  }

  function bind() {
    const panel = document.getElementById("developer-panel");
    document.getElementById("refresh-action-intents")?.addEventListener("click", refresh);
    document.getElementById("refresh-developer")?.addEventListener("click", refresh);
    document.getElementById("action-intent-search")?.addEventListener("input", render);
    document.getElementById("action-intent-status-filter")?.addEventListener("change", render);
    document.getElementById("recover-stale-action-intents")?.addEventListener("click", () => recoverStale().catch(console.error));
    document.getElementById("action-intent-list")?.addEventListener("click", (event) => {
      const button = event.target.closest?.("[data-action-intent-history]");
      if (button) loadHistory(button).catch(console.error);
    });
    panel?.addEventListener("toggle", () => {
      if (panel.open) refresh().catch(console.error);
    });
    if (panel?.open) refresh().catch(console.error);
  }

  if (document.readyState === 'loading') window.addEventListener("DOMContentLoaded", bind, {once:true});
  else bind();
  window.addEventListener('codex:project-changed', () => {
    refreshGeneration += 1;
    intents = [];
    actor = null;
    renderAssurance();
    render();
    setStatus('Project changed. Refresh ActionIntents to inspect the selected scope.');
  });
})();
