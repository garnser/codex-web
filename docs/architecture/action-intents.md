# Durable ActionIntent outbox, inbox, and side-effect reconciliation

External mutations are never considered successful merely because codex-web attempted a provider call. Every side effect is represented by a durable canonical `ActionIntent` before execution.

## Intent record

An ActionIntent snapshots the information needed to reconstruct why and how an external mutation was requested:

- organization/workspace/Project and canonical Resource targets;
- optional Work Item/Goal/Decision/execution attribution;
- provider binding, provider type/instance, action ID, and the full machine-readable ActionDefinition snapshot;
- provider-neutral ActionRequest;
- authority and policy decision snapshots;
- credential reference, never raw secret material;
- canonical and provider idempotency information;
- expected structured Evidence requirements;
- timeout and retry policy;
- verification and rollback requirements;
- correlation/causation IDs;
- optional canonical Work Item update to apply only after verification succeeds.

A denied authority/policy decision is still persisted for audit, but the intent is created as `cancelled` and is never claimable.

## Outbox lifecycle

The primary states are:

`pending -> claimed -> executing -> succeeded|failed|uncertain|requires_reconciliation|rolled_back`

`cancelled` is terminal for work that never begins.

A worker must hold an active lease before changing an intent to `executing`. The transition to `executing` and attempt increment are persisted **before** the provider call.

This creates a safe crash boundary:

- claim expires before execution starts -> return to `pending`;
- worker/process disappears after `executing` but before a durable receipt -> `uncertain`;
- provider timeout/transport exception after execution starts -> durable unknown receipt + `uncertain`;
- explicit provider failure -> `failed`;
- provider success without required verification/evidence -> `requires_reconciliation`;
- required provider verification + Evidence gate satisfied -> canonical success transition, then `succeeded`.

The application performs stale-claim recovery on startup.

## Idempotency and retries

The intent always has a canonical idempotency key. It is sent to the provider only when that action declares idempotency support.

For idempotent providers, an omitted caller key is generated from the stable intent ID and retained across retries.

Caller-provided provider idempotency keys also suppress duplicate intent creation for the same tenant/binding/action.

Once provider execution has started, replay is rejected unless the provider declared idempotency support. Non-idempotent uncertain actions therefore require reconciliation rather than a blind retry.

Retry attempts are bounded by both the intent retry policy and the provider action's declared retry maximum.

## Receipts and verification

Every provider attempt produces durable history:

- `ActionIntentReceipt` records attempt, provider/action, idempotency key, correlation ID, provider external identity and normalized ActionResult when known;
- unknown timeout/crash windows use explicit `unknown` receipts;
- `ActionIntentVerificationReceipt` stores provider verification plus canonical Evidence gate evaluation and findings.

History is reconstructable through `GET /api/action-intents/{intent_id}/history`.

## Canonical state advancement

An ActionIntent may declare a bounded `work_item_success` transition.

That transition is never applied at request, claim, or provider-response time. It occurs only after every required provider verification and structured Evidence requirement succeeds.

The verification receipt is durable before the Work Item update. If the canonical Work Item update fails, the intent remains `requires_reconciliation` instead of falsely reporting success.

This means a crash cannot leave codex-web claiming the external side effect succeeded solely because an outbound request was sent.

## Inbox and provider callbacks

Provider callbacks/events enter a durable inbox keyed by:

- tenant;
- provider type/instance;
- delivery ID.

Duplicate deliveries are acknowledged without adding duplicate inbox history.

Callbacks may resolve an intent through explicit intent ID or an unambiguous idempotency key. A callback must match the intent's configured provider.

A provider "success" callback is an acknowledgement, not proof. It moves the action toward `requires_reconciliation` until required provider verification/Evidence succeeds.

## Reconciliation

Reconciliation can:

- re-run provider verification for a durable successful result;
- re-evaluate canonical Evidence requirements;
- complete a verified Work Item transition;
- requeue an action only when provider idempotency makes replay safe;
- retain `requires_reconciliation` when no trustworthy result exists.

A rolled-back provider result is canonicalized as `rolled_back`.

## Rollback

Rollback requires a provider result plus rollback capability. If rollback fails or returns an unexpected outcome, the intent remains `requires_reconciliation`.

Successful rollback appends a new receipt and moves the intent to terminal `rolled_back`.

## Worker and callback authority

The control plane and execution plane use distinct identities.

Human tenant administrators may inspect intents and perform authorized control-plane operations such as create/retry/cancel and MFA-gated stale-claim recovery, but they **cannot impersonate an ActionIntent worker** or synthesize provider callbacks.

Execution-plane and callback operations require explicit service identities:

- `action-intent:worker` for claim/renew/execute/reconcile/rollback;
- `action-intent:callback` for provider inbox delivery;
- `action-intent:admin` is control-plane automation authority only and does not imply either worker or callback authority.

This separation makes every provider execution/reconciliation/rollback attributable to an explicit worker service principal and every provider callback attributable to a callback service principal. Actual provider execution still passes through ActionProvider resource/credential validation and the credential broker.

## Goal decomposition commit boundary

Accepted Goal decomposition does not execute TaskSource creation inline. The Goal
commit service first preflights the complete proposal, persists a per-item commit
plan, and then queues one Goal-attributed `task-source.create` ActionIntent per
proposed item. Leased ActionIntent workers remain the only execution plane.

Goal reconciliation reads the durable intent status and receipt. A generated Work
Item becomes traceable to the Goal only when a successful ActionResult contains
the actual tenant-visible canonical Work Item ref. Failed, uncertain, cancelled,
or reconciliation-required intents remain visible on the proposal and do not
cause replacement external creates. Once all items have trustworthy refs, the
deterministic Work Graph and Goal bindings are materialized and the proposal is
marked committed.

## Autonomy boundary

Autonomy no longer executes external actions inline. The historical `_execute_external_action` composition seam now queues an ActionIntent. Leased ActionIntent workers own external mutation.

Side-effect-free provider `prepare` remains available for previews.

## APIs

- `GET/POST /api/action-intents`
- `POST /api/action-intents/claim`
- `GET /api/action-intents/{intent_id}`
- `GET /api/action-intents/{intent_id}/history`
- `POST /api/action-intents/{intent_id}/claim`
- `POST /api/action-intents/{intent_id}/renew`
- `POST /api/action-intents/{intent_id}/execute`
- `POST /api/action-intents/{intent_id}/retry`
- `POST /api/action-intents/{intent_id}/cancel`
- `POST /api/action-intents/{intent_id}/reconcile`
- `POST /api/action-intents/{intent_id}/rollback`
- `POST /api/action-intents/inbox`
- `POST /api/action-intents/recover-stale`

#141 should display durable intent state, receipts, uncertainty, verification, and reconciliation requirements rather than inferring external success from request logs.
