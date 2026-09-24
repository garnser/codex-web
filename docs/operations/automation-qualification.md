# Automation qualification

Issue #533 is qualified as a facade over existing canonical services rather than a parallel automation engine.

## Canonical ownership

Automation Definitions are versioned Definition Registry records. Schedule triggers materialize into the durable Scheduler, event triggers consume the Canonical Event Bus, execution launches through Agent Profile / Team services, Work Item creation uses ActionIntent + authoritative TaskSource, approvals use ApprovalRequest, failures project into Attention, and outcomes retain canonical execution/Evidence provenance.

AutomationRun is the bounded occurrence/history ledger. It does not replace scheduler, worker, Work Item, ApprovalRequest, ActionIntent, Evidence, Attention, Agent Profile, Team, provider, or runtime truth.

## Acceptance evidence

| Requirement | Canonical path | Qualification |
| --- | --- | --- |
| Recurring schedule / cron + timezone | AutomationScheduleMaterializer -> SchedulerService | `test_daily_cron_materializes_without_second_timer_engine` |
| One-shot schedule | AutomationScheduleMaterializer -> SchedulerService | `test_one_shot_schedule_materialization_is_exact_and_idempotent` |
| Misfire behavior | Scheduler `fire_once` policy | one-shot materialization asserts canonical misfire policy/grace |
| Event filtering/idempotency | AutomationEventTriggerService -> CanonicalEventBus | `test_canonical_event_trigger_filters_and_dedupes_dispatch`, provider-event qualification |
| Restart-safe occurrence dedupe | AutomationRunStore | `test_duplicate_occurrence_is_deduped_across_service_restart` |
| Shared run history | AutomationRun | `test_manual_schedule_and_event_share_one_history_model` |
| Pause / revision safety | Definition + schedule/run admission | `test_paused_revision_pauses_prior_materialized_schedule`, paused admission tests |
| Runtime authority re-evaluation | AgentProfile/Team execution services | `test_agent_profile_authority_denial_blocks_before_execution` |
| Work Item reuse/create | WorkItemService + ActionIntent TaskSource | reuse/create/wait/resume tests in `test_automation_execution.py` |
| Approval wait | ApprovalRequestService | approval wait/consume/reject tests in `test_automation_execution.py` |
| Token/cost/duration budgets | canonical runtime usage + assignment timestamps | budget tests in `test_automation_outcomes.py` |
| Concurrency budget | AutomationRun admission | concurrency test plus 100-occurrence trigger-burst qualification |
| Retry/backoff | SchedulerService + canonical failure taxonomy | transient retry, mixed-outcome suppression, retry limit, duplicate delivery, revision/pause tests |
| External-side-effect replay safety | failure taxonomy + ActionIntent idempotency + whole-run retry guard | mixed success/failure never schedules whole-Automation retry |
| Failure escalation | AttentionService | failure Attention create/disable/resolve tests |
| Operator explanation | Automation list/detail/run history | persisted status, block_code/block_reason, result_code/result_reason, exact Definition revision, Work Item/execution/Evidence links |

## Safety invariants

- Paused or concurrency-blocked occurrences do not invoke the execution launcher.
- Waiting for Work Item creation or human approval consumes no model tokens.
- Approval-required runs cannot create a Work Item, start an Agent/Team execution, or perform an Automation-owned side effect before the exact ApprovalRequest is consumed.
- Automatic retry is permitted only when every failed execution has a canonical transient failure classification. A mixed run with any successful execution is never replayed wholesale.
- Retry occurrences use the durable Scheduler and stable dedupe provenance. A changed or paused Definition cannot silently inherit an older retry authorization.
- Trigger bursts are bounded at admission; excess occurrences are recorded as blocked rather than starting hidden concurrent work.
- Automation content and UI state do not grant authority. Current identity/Profile/Team/worker/action/approval boundaries remain authoritative at execution time.
- Secret values are never part of Automation Definitions or run diagnostics.

## Operational diagnosis

For an occurrence that did not run, inspect the Automation run first:

- `status=blocked`: use `block_code` and `block_reason`.
- `status=waiting_for_work_item`: follow `work_item_action_intent_id`.
- `status=waiting_for_approval`: follow `approval_request_id`.
- `status=failed`: use `result_code`, `result_reason`, linked executions and Evidence.
- scheduled/event occurrences retain source schedule/event provenance and exact Definition revision.

The first-class Automation workspace renders these canonical records; it does not infer a second lifecycle.
