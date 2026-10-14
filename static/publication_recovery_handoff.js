import {request as apiRequest} from './api_client.js';
import {confirmAction} from './action_confirmation.js';

export function createPublicationRecoveryUI({BASE, escapeHtml, timeText, canRecover, getActor, getInspections, refresh, setStatus}) {
  function recoveryHtml(item) {
    const workspace = item.workspace;
    const scope = workspace.publication_recovery;
    const history = workspace.publication_recovery_history || [];
    const retired = history.map((entry) => `<details><summary>Retired scope: ${escapeHtml(entry.prior_scope.work_item_ref)} · ${timeText(entry.retired_at)}</summary>
      <small>Exact prior scope: ${escapeHtml(JSON.stringify(entry.prior_scope))}</small>
      <small>Terminal source: ${escapeHtml(JSON.stringify(entry.source_identity))} · ${escapeHtml(entry.source_state)}</small>
      <small>Implementation merge: ${escapeHtml(entry.merge_revision)} · reviewed workspace head ${escapeHtml(entry.head_revision)}</small>
      <small>Definition: ${escapeHtml(JSON.stringify(entry.execution_profile_definition))} · agent revision ${escapeHtml(entry.agent_profile_record_id)}</small>
      <small>Actor: ${escapeHtml(entry.authorized_by)} · handoff ${escapeHtml(entry.idempotency_key)}</small>
      <small>Receipts: ${escapeHtml((entry.receipt_ids || []).join(", "))} · verifications: ${escapeHtml((entry.verification_ids || []).join(", "))}</small>
      <small>Exact validation checks: ${escapeHtml(JSON.stringify(entry.validation_checks))}</small>
      ${(entry.completion_intent_ids || []).map((id) => `<a href="${BASE}/?workspace=operations&action_intent=${encodeURIComponent(id)}">Inspect completion intent ${escapeHtml(id)}</a>`).join(" ")}
      </details>`).join("");
    if (!scope) return `<small>Publication recovery: none.</small>${retired}`;
    const allowed = canRecover() && workspace.owner_identity_id === getActor()?.identity_id && item.lease_active;
    return `<small>Current publication recovery (does not relabel the execution subject): ${escapeHtml(JSON.stringify(scope))}</small>${retired}
      ${allowed ? `<form data-recovery-handoff="${escapeHtml(workspace.id)}">
        <label>Next owned Work Item <input name="work_item_ref" required maxlength="500" placeholder="owner/repository#issue"></label>
        <label>Verified completion intent IDs <input name="completion_intent_ids" required placeholder="branch intent, merge intent"></label>
        <label>Expected current repository head <input name="expected_head_revision" required pattern="[0-9a-f]{40}" value="${escapeHtml(workspace.head_revision || "")}"></label>
        <button type="submit" class="ghost-button">Preview publication recovery handoff</button>
        <small role="status" data-handoff-result>Preview verifies terminal source, implementation, validation, publication and unresolved intents. Confirmation retires only this issue scope.</small>
      </form>` : '<small>Handoff requires the owning administrator, step-up assurance and a live lease. Inspect canonical ownership before requesting handoff.</small>'}`;
  }

  async function handoff(form) {
    const item = getInspections().find((value) => value.workspace.id === form.dataset.recoveryHandoff);
    if (!item) return;
    const workspace = item.workspace;
    const scope = workspace.publication_recovery;
    const fields = new FormData(form);
    const payload = {
      work_item_ref: String(fields.get("work_item_ref") || "").trim(),
      expected_prior_scope: scope, expected_assignment_id: scope.assignment_id,
      expected_assignment_fence: scope.assignment_fence, execution_workspace_id: workspace.id,
      repository_resource_id: scope.repository_resource_id,
      expected_head_revision: fields.get("expected_head_revision"),
      completion_intent_ids: String(fields.get("completion_intent_ids") || "").split(/[,\s]+/).filter(Boolean),
      idempotency_key: `recovery-handoff-${crypto.randomUUID()}`,
    };
    const status = form.querySelector("[data-handoff-result]");
    const button = form.querySelector("button");
    button.disabled = true;
    try {
      status.textContent = "Checking canonical scope and authoritative completion evidence…";
      const preview = await apiRequest("/api/control-plane-broker/publication-recovery/preview", {method: "POST", body: JSON.stringify(payload)});
      if (!preview.allowed) throw new Error(preview.reason || "Handoff denied by canonical validation");
      status.textContent = preview.next_action;
      if (!await confirmAction({action: "Retire completed publication recovery scope", target: `${scope.work_item_ref} → ${payload.work_item_ref}`,
        risk: "high", consequence: `Workspace ${workspace.id}; repository ${scope.repository_resource_id}; assignment ${scope.assignment_id} fence ${scope.assignment_fence}; head ${payload.expected_head_revision}. Terminal source ${preview.source_identity?.revision}; merge ${preview.merge_revision}; ${preview.validation_checks?.length || 0} validated checks.`,
        recovery: "Old history and receipts remain inspectable. This does not create a worker, extend a lease, grant permissions or replay an intent."})) return;
      await apiRequest("/api/control-plane-broker/publication-recovery/handoff", {method: "POST", body: JSON.stringify(payload)});
      await refresh();
      setStatus("Publication recovery scope handed off; retired history and exact evidence retained.");
    } catch (error) {
      const detail = error.payload?.detail || error.detail;
      status.textContent = `Handoff denied: ${detail?.reason || error.message}. ${detail?.next_action || "Inspect current scope and unresolved intent receipts; preserve this draft."}`;
    } finally {
      button.disabled = false;
    }
  }

  return {render: recoveryHtml, submit: handoff};
}
