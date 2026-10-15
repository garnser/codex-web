import {createQueuedOperatorAdmissionUi} from './queued_operator_admission_ui.js';
import { createQueueOrderRecoveryUi } from "./queue_order_recovery_ui.js";
import { createQueuedBootstrapRecoveryUi } from "./queued_bootstrap_recovery_ui.js";
import { captureProjectView } from "./project_view_scope.js";
import { createNotificationBatchUi } from "./notification_batch_ui.js";
export function createExecutionPreflightUi({
  api,
  addMessage,
  loadThread,
  scheduleRefresh,
  logEvent,
  captureView = () => captureProjectView().current,
}) {
  const attemptsByThread = new Map();
  const operatorAdmission=createQueuedOperatorAdmissionUi({api,loadThread,scheduleRefresh,captureView});
  const batches = createNotificationBatchUi({api, addMessage, captureView, loadThread, scheduleRefresh});
  const orderRecovery=createQueueOrderRecoveryUi({api,addMessage,loadThread,scheduleRefresh,captureView});
  const bootstrapRecovery=createQueuedBootstrapRecoveryUi({api,loadThread,scheduleRefresh,captureView});

  async function load(threadId, limit = 50) {
    batches.selectThread(threadId);
    const current = captureView();
    try {
      const response = await api(
        `/api/threads/${threadId}/preflight-attempts?limit=${limit}`,
      );
      if (!current()) return;
      attemptsByThread.set(
        threadId,
        Array.isArray(response?.items) ? response.items : [],
      );
      await orderRecovery.load(threadId);
      await batches.load(threadId);
    } catch (error) {
      if (!current()) return;
      logEvent?.("preflight.error", { message: error.message });
      attemptsByThread.set(threadId, []);
    }
  }

  function render(threadId) {
    orderRecovery.render(threadId);
    batches.render(threadId);
    [...(attemptsByThread.get(threadId) || [])]
      .reverse()
      .forEach((attempt) => {
        const status = String(attempt?.status || "blocked");
        const correlation = (
          attempt?.correlation_id
          || attempt?.execution_id
          || attempt?.id
          || ""
        );
        const blockers = Array.isArray(attempt?.blockers)
          ? attempt.blockers
          : [];
        const retirement = attempt?.scheduling_retirement || null;
        const schedulingRetired = retirement?.state === "retired";
        const schedulingRetirementPrepared = retirement?.state === "prepared";
        const batchMember = Boolean(attempt?.notification_batch_id);

        if (status !== "started") {
          addMessage(
            status === "retrying" ? "You (retrying)" : ['queued','admitting'].includes(status)
              ? "You (retained)" : status === 'reconciliation_required' ? "You (outcome unknown)" : "You (blocked)",
            attempt?.message || "",
            "user",
            new Date((attempt?.created_at || Date.now() / 1000) * 1000),
          );
        }

        const context = [
          attempt?.queued_turn_snapshot?.id
            ? `Original queued request: ${attempt.queued_turn_snapshot.id}`
            : "",
          attempt?.queued_turn_snapshot?.work_item_ref
            ? `Work item: ${attempt.queued_turn_snapshot.work_item_ref}`
            : "",
          retirement?.work_item_ref
            ? `Retirement proof: ${retirement.work_item_ref} · provider ${retirement.source_state} · canonical ${retirement.canonical_stage}`
            : "",
          attempt?.agent_profile_id
            ? `Agent profile: ${attempt.agent_profile_id} · revision ${attempt.agent_profile_revision || 'unknown'}`
            : "",
          attempt?.execution_profile_id
            ? `Profile: ${attempt.execution_profile_id}`
            : "",
          attempt?.repository_resource_id
            ? `Repository: ${attempt.repository_resource_id}`
            : "",
          attempt?.read_only_repository_resource_ids?.length
            ? `Read-only repositories: ${attempt.read_only_repository_resource_ids.join(", ")}`
            : "",
          attempt?.sandbox ? `Sandbox: ${attempt.sandbox}` : "",
          attempt?.approval_policy
            ? `Approval: ${attempt.approval_policy}`
            : "",
        ].filter(Boolean);
        const lines = [
          `Preflight attempt ${Number(attempt?.attempt_number || 1)} · ${status}`,
          batchMember ? `Shared notification admission: ${attempt.notification_batch_id}. Individual recovery is unavailable; inspect the batch receipt and retained originals.`
            : status === 'reconciliation_required'
            ? (attempt.queued_start_evidence
              ? 'Authoritative native response captured; finish receipt reconciliation without resending.'
              : 'Native start outcome is unknown. Reconcile the original execution before retrying.')
            : status === 'queued' ? 'Original request retained for admission; no start is confirmed.'
              : status === 'admitting' ? 'Pre-start admission reserved. Retry fences this reservation before native startup.' : '',
          schedulingRetired
            ? (attempt.queued_start_receipt
              ? 'Queue scheduling remains retired. A later exact native start was confirmed; no execution was resumed and no replay is permitted.'
              : attempt.queued_start_evidence
                ? 'Queue scheduling remains retired. A later authoritative positive response is captured; receipt reconciliation cannot requeue or resume execution.'
                : 'Queue scheduling was retired after terminal WorkItem verification. The earlier native start outcome remains unknown and cannot be retried.')
            : schedulingRetirementPrepared
              ? (attempt.queued_start_receipt || attempt.queued_start_evidence
                ? 'Scheduling retirement was prepared while the native outcome was unknown. A later positive native response is retained; exact queue removal is not confirmed.'
                : 'Scheduling retirement was prepared and admission is fenced, but exact queue removal is not confirmed. Reconciliation is required.')
              : '',
          correlation ? `Correlation: ${correlation}` : "",
          attempt.queued_start_receipt
            ? `Confirmed native turn: ${attempt.queued_start_receipt.provider_native_turn_id}` : '',
          attempt.queued_start_receipt
            ? `Native runtime: ${attempt.queued_start_receipt.provider_id}/${attempt.queued_start_receipt.runtime_id}` : '',
          attempt.queued_start_receipt
            ? `Requested execution: ${attempt.queued_start_receipt.requested_execution_id}; admitted execution: ${attempt.queued_start_receipt.execution_id}` : '',
          attempt.queued_start_correlation
            ? `Native correlation v${attempt.queued_start_correlation.schema_version}: ${attempt.queued_start_correlation.runtime_generation} / ${attempt.queued_start_correlation.rpc_request_id}` : '',
          attempt.queued_start_correlation
            ? `Attribution: ${attempt.queued_start_correlation.organization_id}/${attempt.queued_start_correlation.workspace_id} · Project ${attempt.queued_start_correlation.project_id} · actor ${attempt.queued_start_correlation.actor_identity_id}` : '',
          attempt.queued_start_correlation
            ? `Assignment: ${attempt.queued_start_correlation.assignment_id} · worker ${attempt.queued_start_correlation.worker_id} · send fence ${attempt.queued_start_correlation.fence}` : '',
          attempt.queued_start_correlation
            ? `Native session: ${attempt.queued_start_correlation.provider_native_session_id} · original SHA256 ${attempt.queued_start_correlation.snapshot_sha256}` : '',
          attempt.queued_start_correlation
            ? `Resources at send: ${attempt.queued_start_correlation.assignment_resource_ids.join(', ')}` : '',
          attempt.queued_start_correlation
            ? (attempt.queued_start_receipt ? 'Exact positive evidence reconciled; no replay is permitted.'
              : attempt.queued_start_evidence ? 'Authoritative positive response captured; receipt projection pending. This grants no execution authority.'
                : 'Pre-send attribution retained; native outcome remains unknown without an exact positive response.') : '',
          attempt.replacement_thread_id
            ? `Original thread: ${attempt.thread_id}; current thread: ${attempt.replacement_thread_id}`
            : "",
          ...context,
          ...blockers.map((blocker) => {
            const target = blocker?.target_type
              ? ` Target: ${blocker.target_type}${blocker?.target_id ? `/${blocker.target_id}` : ""}.`
              : "";
            const remediation = blocker?.remediation
              ? ` Remediation: ${blocker.remediation}`
              : "";
            return `${blocker?.code || "execution_preflight_blocked"}: ${blocker?.message || "Execution could not start."}${target}${remediation}`;
          }),
        ].filter(Boolean);

        const card = addMessage(
          batchMember ? "Batch original retained · outcome unresolved"
            : schedulingRetired ? (attempt.queued_start_receipt
            ? "Scheduling retired · native start confirmed"
            : attempt.queued_start_evidence ? "Scheduling retired · native receipt pending"
            : "Scheduling retired · native outcome unknown")
            : schedulingRetirementPrepared ? "Scheduling retirement requires reconciliation"
            : status === "started" ? "Preflight resolved"
            : attempt.queued_start_evidence ? "Native start receipt pending" : ['queued','admitting'].includes(status)
            ? "Admission retained" : status === 'reconciliation_required' ? "Reconciliation required" : "Execution blocked",
          lines.join("\n"),
          "tool",
          new Date((attempt?.updated_at || Date.now() / 1000) * 1000),
        );
        if (attempt.operator_admission) {
          const audit = document.createElement('pre');
          audit.textContent = `Operator admission: ${attempt.operator_admission.requested_by}
Reason: ${attempt.operator_admission.reason}
Claim: ${attempt.operator_admission.claim_id}`;
          card?.querySelector('.body')?.append(audit);
        }
        if (batchMember) {
          batches.attach(attempt.notification_batch_href, card?.querySelector('.body'));
          return;
        }
        bootstrapRecovery.attach(attempt, card?.querySelector('.body'));
        operatorAdmission.attach(attempt, card?.querySelector('.body'));
        if (
          status === "started"
          || (!attempt?.can_retry && !attempt?.can_reconcile_prestart && !attempt?.can_retire_scheduling)
          || !(attempt?.can_retire_scheduling ? attempt?.retire_scheduling_href
            : attempt?.can_reconcile_prestart ? attempt?.reconcile_prestart_href : attempt?.retry_href)
        ) {
          return;
        }

        const body = card?.querySelector(".body");
        if (!body) return;
        const actions = document.createElement("div");
        actions.className = "message-actions";
        const reconcile = Boolean(attempt?.can_reconcile_prestart);
        const retire = Boolean(attempt?.can_retire_scheduling);
        const actionHref = retire ? attempt.retire_scheduling_href
          : reconcile ? attempt.reconcile_prestart_href : attempt.retry_href;
        const actionLabel = retire ? 'Retire terminal scheduling'
          : reconcile ? (attempt.queued_start_evidence ? 'Reconcile captured native start' : 'Reconcile unstarted refusal') : 'Retry';
        const retry = document.createElement("button");
        retry.type = "button";
        retry.textContent = status === "retrying"
          ? "Retry in progress"
          : actionLabel;
        retry.disabled = status === "retrying";
        retry.addEventListener("click", async () => {
          const current = captureView();
          retry.disabled = true;
          retry.textContent = retire ? 'Retiring scheduling…'
            : reconcile ? 'Reconciling…' : 'Retrying…';
          let targetThreadId = threadId;
          try {
            const response = await api(actionHref, { method: "POST" });
            targetThreadId = response?.threadId || threadId;
          } catch (error) {
            targetThreadId = error?.detail?.threadId || targetThreadId;
            if (current() && error?.detail?.code !== "execution_preflight_blocked") {
              addMessage("Error", error.message, "tool", new Date());
            }
          } finally {
            if (current()) {
              await loadThread(targetThreadId);
              if (current()) scheduleRefresh(0);
            }
          }
        });
        actions.appendChild(retry);
        body.appendChild(actions);
      });
  }

  async function handleError(error, threadId) {
    if (error?.detail?.code !== "execution_preflight_blocked") {
      return false;
    }
    await loadThread(threadId, { force: true });
    scheduleRefresh(0);
    return true;
  }

  return { load, render, handleError };
}
