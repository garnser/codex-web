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

For `code-host.pull-request.upsert`, changed metadata can include an
`origin_intent_id` parameter referencing the original successful creation,
alongside a fresh idempotency key. The canonical service checks the original
completed provider receipt and verified result, exact tenant/Project/resource,
provider binding, head/base and Work Item attribution, and both immutable agent
profile/role bindings. The current assignment must have a live matching fence,
trusted active/draining worker and WRITE scope. The original historical lease
is never reused; missing retained identity or receipt evidence fails closed.

The claimed executor repeats these checks before invoking the provider and
establishes ephemeral context bound to the exact request and provider binding.
Direct provider calls cannot supply that context through request parameters.
GitLab and GitHub require the original provider target ID/number, original
ownership marker, open state and head/base to match before metadata update;
continuation never creates a replacement or adopts a foreign request. The new
intent and receipt retain their own key and include the origin reference.
Original intents, receipts, failures and unknown outcomes remain unchanged.
This is provider execution context derived from canonical evidence, not another
ownership store, repository grant, validation result or merge authorization.

The assignment broker gives branch publication, pull-request creation or update,
and pull-request merge a bounded
120-second provider execution deadline and a 180-second claim lease, leaving
time to record receipts and verification. Other repository operations retain
their ActionIntent defaults. These bounds are selected by the broker operation;
callers cannot override either value. Timeout uncertainty and retry authority
remain unchanged.

The async repository broker offloads synchronous intent creation, claim, and
fallback lookup to worker threads so catalog and store reads do not block the
control-plane event loop. Calls retain sequential ordering, propagated request
context and actors, and the canonical service transaction and authority guards;
claimed execution still uses the asynchronous ActionIntent executor.

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
- a GitLab change-request upsert rejected by its pure local request contract,
  before the first MR API call, raises the existing typed
  `ActionRequirementError` and records a known `failed` receipt; the diagnostic
  lists the supported canonical fields without echoing rejected values;
- provider success without required verification/evidence -> `requires_reconciliation`;
- required provider verification + Evidence gate satisfied -> canonical success transition, then `succeeded`.

The application performs stale-claim recovery on startup.

This local classification is confined to the first-party parser invocation.
It does not classify arbitrary `ValueError` exceptions as safe: errors from a
provider read, write, or response normalization still record an unknown outcome
and retain the existing reconciliation and duplicate-prevention requirements.

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

Provider idempotency makes a replay technically safe; it does not grant more
retry authority. When the immutable attempt budget is exhausted, reconciliation
must not return the intent to `pending`. A known provider failure remains
`failed`; an unknown outcome remains `requires_reconciliation`, with the
exhausted/nonclaimable condition visible in the existing operator surface. If
delivery is still required after the provider outcome is established, an
authorized caller creates a replacement ActionIntent that passes current
authority, policy, security, provider, and resource checks.

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

Reconciliation never treats an ambiguous mutation as permission to replay it.
When an already-started pull-request merge is uncertain or requires
reconciliation and has no durable provider result, a verification-only
reconciliation reconstructs the expected result shape from the original
canonical request, calls the provider's side-effect-free prepare contract, and
reads provider state through verification. It records a reconciled receipt only
when the exact pull request and mandatory expected head are confirmed merged.
An unstarted intent or a missing, different, or unreadable provider result
remains visibly reconcilable.

#141 should display durable intent state, receipts, uncertainty, verification, and reconciliation requirements rather than inferring external success from request logs.

## Keyed persistence and rollback

Action execution uses the versioned `action_intents.records.v1` collection.
Intent rows, per-intent receipts/verification/inbox history, provider-idempotency
aliases, tenant-scoped pending indexes, and active-lease indexes share the same
StateStore transaction lock. Claims recheck the selected current intent under
that lock; a stale page cannot authorize a second claimant. Lease renewal and
execution admission recheck current ownership, status, expiry and attempt bounds
inside the transaction. Empty claims and recovery cycles do not rewrite state.

The first access migrates the legacy v1.2 document transactionally and
idempotently. Unknown keyed schema versions fail closed. The legacy document is
an explicit rollback checkpoint; normal keyed writes do not refresh it. Controlled
shutdown calls `flush_legacy_mirror()` after runtime owners stop. Before downgrading
to a pre-keyed binary, stop execution and capture that checkpoint. Do not run old
and keyed binaries concurrently against the same state, or resume a keyed binary
following old-binary writes without reconciling/restoring a compatible snapshot.
Backups retain both namespaces; isolated restore disables pending/leased intent
execution in the keyed records as well as the legacy document.

Async execution offloads storage-bearing synchronous helpers through the bounded
asyncio executor, retaining sequential transition ordering and actor/context
propagation. Workspace/session point reads fetch only the workspace and lease;
turn admission fetches only the referenced Work Item execution metadata. Bulk
administrative snapshots remain explicit bulk operations, separate from claim,
execution, receipt and status hot paths.

### Deferred assignment-bound repository delivery

The supervised local execution worker periodically examines at most 256 scoped pending intents and handles at most eight never-started assignment-broker repository actions per cycle. A valid request resumes through the existing canonical claim and execute boundaries after maintenance admission reopens. Recovery rechecks the exact originating assignment, worker lifecycle, lease/fence/deadline, tenant/project, singular writable repository, requester identity and current broker authority. Normal provider policy, approval, credential, workspace attestation, remote compare-and-swap and verification checks remain required.

Recovery never automatically repeats attempted or uncertain mutations. A released, expired or missing origin becomes a known `failed` authorization rejection with an explicit commit-preserving recovery reason, using an atomic pending/attempt-zero/no-lease/no-start/no-receipt/no-verification comparison so a concurrent claim or result cannot be overwritten. The provider execution attempt remains zero and no receipt, verification, delivery success or work-item closure is created. Failure-taxonomy occurrence counts remain separate from provider attempts. Only an actual state transition notifies observers. Such failures remain actionable but do not count as unknown external mutations blocking a service drain; historical uncertain records remain unchanged. The existing ActionIntent inspector exposes this state. A replacement publication requires a newly authorized live assignment after observing any relevant provider state; historical authority is never transplanted. Temporary storage errors remain retriable supervisor errors. The supervisor owns task startup and cancellation; maintenance/draining workers suspend processing. No LLM calls or additional authority grants are introduced.

Code-host execution first validates its side-effect-free preparation contract. Invalid parameters, missing credentials, workspace-attestation rejection, and explicitly identified pre-write ownership/draft/head conflicts produce typed `ActionRequirementError` failures. They never imply an unknown provider outcome. Transport errors and malformed responses after a possible mutation retain uncertain-outcome handling. Historical uncertain records require separate provider observation/disposition and are not automatically reclassified from current validation rules.

### Recovering an absent publication branch

A governed `code-host.branch.publish` request can explicitly set `expected_remote_absent: true` for a preserved `codex/` branch. This is mutually exclusive with `expected_remote_revision` and cannot be used for existing-MR recovery. The publisher uses `--force-with-lease=refs/heads/<branch>:` so creation fails atomically if another writer creates the remote ref. Omitting the field or setting it to false does not supply a recovery precondition. Writable workspace, active lease, tenant/resource scope, clean tree, exact local head and base ancestry checks still apply. The receipt output records the absence precondition. A failed or uncertain historical request must not be replayed; submit a new currently authorized request only after checking its disposition.

Every assignment receives this recovery contract through the shared control-plane instructions, including bootstrap threads without a Work Item. Broker operation discovery exposes the publication parameters and mutually exclusive remote preconditions. The provider error names both supported recovery modes. Observe the destination through the governed repository reader: a confirmed absent preserved `codex/` ref requires explicit absence; an existing ref requires its observed full commit SHA. Timeout or denial is not evidence of absence. Keep the local checkout on the new assignment's attested branch, even when the publication destination is the preserved older branch. Released assignments require a new authorized assignment and active lease. Preserve local commits and evidence before release; publication and MR handoff still require successful receipts, exact remote verification and the existing owner/validation process.

### Publication-recovery revision fencing

Scope-bound code-host intents retain `publication_recovery_scope`, the exact
workspace recovery provenance and execution-profile Definition reference. All
broker-created repository intents in an active recovery scope receive its Work
Item attribution without relabelling the underlying execution. Canonical scope
is captured by the service and cannot be supplied as an authority assertion in
an ActionIntentCreate request. Insert and provider-execution admission atomically
recheck the recovery revision with live workspace/worker/assignment state.
Completed-issue handoff shares that transaction boundary and retains old intent
and receipt history. An old revision never executes against the replacement
scope. See the execution-worker boundary for completion evidence, compatibility
and operator confirmation contracts. Ordinary unscoped and Work Item executions
retain their existing paths. No new model calls or provider mutations are added.

The scoped broker admission path carries an internal exact assignment lookup hint
and uses bounded cross-collection record deltas for its new recovery guards.
Guarded insert/execution reads the specific outbox row or idempotency index plus
four scope rows, using the same locks as retirement. Read counts and unrelated-row
preservation are tested; guards do not load global intent, worker or workspace
collections. Existing unscoped execution lookups remain compatible. Typed provider
receipts and canonical attribution establish issue/PR relationships independently
of close-keyword prose; separately verified terminal issue closure remains valid.

Recovery qualification reads a versioned GitHub default-setup configuration through
the existing CodeHost checks/logs/artifacts capabilities and credential-reference boundary,
retaining its exact state/languages/update/query-suite observation with each job.
Expected analysis membership comes from that configuration, with protocol alias
normalization; successful observed jobs never define their own required set.
The existing literal functional matrix in `tests.yml` supplies shard membership.
Only that canonical workflow metadata is included in the runtime build context;
unsupported matrices and missing/unreadable packaging fail with typed recovery
refusals. The Docker smoke gate executes these qualification regressions inside
the supported image without network or writable runtime state.

Recovery publication additionally requires the existing immutable candidate
artifact and its versioned manifest to identify the exact merged source, image
digest and complete deterministic qualification. The workflow control revision
alone cannot identify a `workflow_run` producer's source. Provider artifact
inventory/digest and bounded in-memory ZIP/manifest validation retain only the
safe protocol fields; no archive extraction, logs or secret material enter scope
history. Missing, expired, truncated, ambiguous or mismatched candidate evidence
refuses handoff and requires canonical publication inspection.

Every intent tagged with the retiring scope participates in the pending/unknown
barrier, including intents whose immediate resource is an artifact rather than
the repository. Untagged legacy attribution remains bounded to the originating
repository/execution. Recovery rollback admission shares the same canonical
scope guard and atomically marks the existing intent executing before the provider
call; handoff cannot race that side effect or permit rollback under a retired scope.
Native PR CodeQL results are inspected independently of analysis-job success,
with exact source and native issuer provenance; missing/failed/cancelled/pending
PR results refuse qualification. Recovery artifact source and workflow control
revision are recorded separately. Bounded recent main workflow inspection can
find a candidate whose control revision advanced, while its immutable artifact
still proves the requested source; unknown or absent proof never grants eligibility.
### Procedure discovery admission

The shared provider mutation boundary also enforces the
[procedure discovery contract](procedure-discovery.md). Creation and immediate
pre-mutation results retain exact Skill references, context, candidates and
blockers in the existing intent; assignment-backed actions retain fenced evidence.
A direct provider call cannot substitute for this admission. A procedure match
never grants authority or creates an extra confirmation requirement.

The combined ActionIntent state contract is `1.5`. Migration from `1.3`/`1.4` preserves
procedure discoveries and recovery snapshots unchanged; older records receive
empty optional histories. Readers that cannot understand both admission records
must upgrade before consuming state written by this version.

Procedure-discovery evidence appended by these paths also uses the existing
point assignment transaction, with live fence/lease checks. It must not scan or
rewrite unrelated global worker/assignment records before provider execution.
Procedure execution adds a single-attempt originating-receipt rollback checkpoint and forbids forward retry after any rollback attempt. ActionIntent document 1.5/keyed metadata 1.2 require compatible writers. See [procedure execution](procedure-execution.md) for crash, reconciliation and verification semantics.
