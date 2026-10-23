# Durable procedure execution

Verified outcomes feed the governed, inactive
[procedure draft conversion](procedure-outcomes.md) path. Publication and future
assignment remain separate explicit operations.

The interpreter is a facet of the canonical `ExecutionAssignment`, not a job,
worker, approval or procedure catalog. It consumes published `agent.skill`
procedures selected by canonical discovery. It never enables native operational
tools or direct adapter calls. The existing isolated native no-match preparation
policy and all unsupported-path refusals remain in force.

## Pinned content and checkpoint

Worker state 1.11 adds `procedure_checkpoint` (nested contract 1.0). Its compare
and swap revision serializes step dispatch inside the assignment store. It retains
the exact Definition record/revision/checksum, governed immutable input Artifact
ID/content checksum, material Resource-context checksum, deadline, current and
completed steps, bounded output Artifact IDs/checksums and name-to-reference indexes, Evidence references, current authority
decisions, attempts, model invocation IDs, approval IDs, originating rollback
receipt, deviations, recovery and Attention references. A new publication never
replaces these pins. Exact Skill security/lifecycle/freshness checks still apply;
quarantine, disablement and revocation block an existing pin.

Inputs are a managed JSON Artifact in the same tenant/Project/resource scope.
Definitions declare types; runtime validates required values and reference types.
No literals or executable expressions are interpreted. Each output snapshot is a managed governed Artifact capped at
16 KiB, with input/Evidence derivation and lifecycle checks before reuse; at most 32 steps and five attempts per step are permitted. Inputs/code
Artifacts are capped at 64 KiB and cryptographically checked on every use.
Secret values never enter parameters, checkpoints, model context or output:
only assigned, currently usable SecretReferences cross the credential boundary.

`POST /api/execution-workers/assignments/{id}/procedure/start` binds an admitted
Definition and input Artifact to a live assignment. `/advance` processes at most
one step per request/event. `/procedure` reads the checkpoint; `/deviate` records
a current policy-authorized diagnostic deviation with reason and Evidence.
There is no autonomous polling or private timer. Persisted `not_before` values
and canonical ActionIntent backoff survive restart; subsequent explicit events
or operator advance requests observe them. Expired/changed ownership is not a
persistent execution grant. Existing assignment inspection exposes the checkpoint;
ActionIntent history exposes the effect and rollback records.

## Protected operations

Before a step, and again at the shared provider mutation boundary, the interpreter
resolves the current requester identity, exact Skill lifecycle, active Resource
selectors and operational facts, current Role authority, assignment/worker fence,
capabilities, workspace resource lease, provider contract and SecretReferences.
A pin is content attribution, never permission. Missing required facts and
unsupported transports are typed failures that retain recovery/Attention state.
Material fact changes require explicit recovery/re-admission, never silent adoption.

Diagnostics execute checksum-bound Artifact bytes through the existing
`LocalExecutionWorkerRuntime` and Bubblewrap backend. The same assignment owns
the execution; no second worker is provisioned. Arbitrary diagnostic Artifacts
always use the existing read-only, network-disabled sandbox, even when the parent
assignment permits network or broader filesystem access. The child has only the
command capability, no SecretReferences, runtime/agent binding, broker URL,
container socket mount, authentication environment or writable repository mount.
Assignment resources/time remain bounded; live worker identity, fence, deadline,
Resource scope and workspace lease are checked before, during and after execution.
An ownership-check exception terminates and reaps the shared backend process group. They receive bounded JSON inputs and return structured output/checks.
The runtime records canonical Evidence for observed declared checks. HTTP/service
readiness uses a registered scoped read ActionProvider/observation through the
canonical ActionIntent and Evidence boundaries; an Artifact does not receive
network access for readiness. Read operations must declare read authority and
use the registered provider's current Resource target and egress policy. Readiness
must cover every declared worker and HTTP, not merely one process or a provider's
success response. An interrupted Artifact diagnostic may repeat only inside its
declared attempt budget under this enforced network-disabled boundary. Interrupted
provider observations resume through their durable receipt/reconciliation path.

Mutations use registered ActionProviders and ActionIntent creation, current
policy/security/authority, stable request-bound idempotency, claims, provider
receipts and verification. Every replay uses the same intent. Unstarted claims
use existing stale-claim recovery. An attempted effect with no trustworthy result
blocks further mutation and rollback until canonical reconciliation. The
interpreter never restarts services or reapplies configuration to discover what
happened. Known failures retry only when the canonical failure taxonomy and
provider idempotency permit it, within both provider and procedure bounds.

## Rollback

Procedure contract 1.1 adds optional `recovery: {mode: provider_rollback,
origin_step_id: ...}` to a conditional recovery step. It requires a preceding
mutation with the same exact action reference and exactly one rollback attempt.
The failure edge and verification step remain explicit. Ordinary separately
registered inverse actions can still be described as recovery without this field.

Provider rollback calls `ActionIntentService.rollback` on the originating intent,
never an inverse `execute` substitute. It verifies the originating successful
receipt/result, exact tenant/execution/requester/provider/Resource, current
ActionDefinition compatibility and rollback capability, current permissions,
secrets, material facts and live fences. Unmediated/unrelated rollback refuses.

ActionIntent document 1.5 / keyed-record metadata 1.2 retain a rollback checkpoint
with originating receipt, started state, outcome and rollback receipt. This is
reserved atomically before provider I/O. A crash after the possible effect cannot
send rollback again. A durable rollback receipt can repair a lost local status
update; otherwise reconciliation stays blocked. Reconciliation never verifies the
old forward result as proof that an unknown rollback succeeded and never requeues
the forward mutation. Failed/unavailable/incompatible rollback creates actionable
Attention. Receipt-backed rollback still requires the declared subsequent
rollback-verification Evidence before the procedure reports `recovered`.
`recovered` is distinct from successful completion of the original operation.

## Conditions, judgment and completion

Conditions, selection, progression, waits, approvals and Evidence gates make zero
model calls. Procedure 1.1 judgment steps declare why novelty needs reasoning,
role/model class, per-call input/output token and cost ceilings, maximum calls,
attempts and time bounds. The interpreter reserves each call durably, invokes the
existing governed ModelGateway with fallback disabled (one provider attempt),
retains invocation IDs and execution/Work Item/role/pin attribution, and validates
structured JSON output. It retrieves only declared input values; governed source
filtering precedes invocation. Recommendations become governed report Artifacts
with source lineage, not action authority or operational verification. Lost or
invalid responses spend the reserved budget; no hidden retries or handoffs exist.

Completion rechecks all declared Evidence, retention/lifecycle, Artifact validity,
exact execution scope and each successful mutation's required verification step.
A provider acknowledgement or model statement cannot complete a procedure.
Failure, exhausted budgets and unsuccessful verification stop visibly or enter
an explicitly declared compatible recovery branch. Policy-authorized deviations
retain reason, Evidence and the exact authority Definition; they cannot skip a
required mutation or fabricate successful verification. Canonical ApprovalRequest
consumption is exact-target/idempotent; existing satisfied approval is reused.

## Compatibility and UI

Ordinary procedure 1.0 payload serialization is unchanged; 1.1 is required for
judgment/provider rollback. No installed mutable workflow catalog is added.
Worker 1.10 and ActionIntent 1.3/1.4 documents migrate explicitly. Old keyed writers
refuse metadata 1.2. Do not mix old/new writers or downgrade after these contracts
have been persisted; drain execution and restore a compatible verified snapshot
if application rollback is necessary. Restore does not automatically replay
pending effects, configuration, service restarts or rollback.

The operator trace/authoring/recovery UI remains explicitly tracked in
[#1428](https://github.com/garnser/codex-web/issues/1428), including step controls,
Incident links, applicability preview and richer recovery presentation. Existing
assignment and ActionIntent inspectors are the canonical read surfaces. See the
[operator recovery guide](../operations/procedure-execution.md).

Registered procedure adapters return `ActionResult.output` with `outputs` (the
exact declared typed value map) and `evidence` (declared proof name to canonical
Evidence ID). The Evidence must be scoped to the exact execution, observed after
the step began and have `source=action-intent:{id}` for the actual canonical
intent context. A rollback's proof must be observed after its own recovery step
began; forward proof cannot stand in for restoration. Worker proof instead binds
the assigned worker identity and `procedure:{assignment_id}:{step_id}` source;
judgment proof names its actual invocation. Stale/cross-step/foreign proof refuses.
An adapter without this result/Evidence contract fails explicitly; installing the
interpreter does not claim arbitrary existing operational adapters are supported.

An optional implementation adapter registered with the existing ActionProvider
registry can project an exact provider/action/contract's legacy result into this
contract. GitLab contract 1.2 supports typed READY and merge receipt projections;
the stored flat `ActionResult` remains unchanged for verification/reconciliation.
The procedure owner requires the same succeeded canonical intent, current receipt,
attempt, request/key, tenant/Project/execution and Resource attribution. Supported
outputs are fixed by that adapter, never inferred from arbitrary output keys.
READY outputs contain its exact head/base target and `draft=false`; merge outputs
contain the PR number, observed head and merge commit. READY does not prove review,
approval or merge, and must not be repeated for an already non-Draft MR.

These steps declare Evidence names `receipt` (kind `provider_receipt`) and/or
`verification` (kind `verification`). Receipt Evidence retains the actual original
receipt/result identity and timestamp. Verification additionally requires the same
intent's canonical successful provider-verification record; execution success is
insufficient. Canonical Evidence idempotency keeps these point writes stable across
an interrupted checkpoint save and rejects different content under the same key.
The usual governed immutable output Artifact and all subsequent proof checks remain
mandatory. UNKNOWN, missing/foreign/malformed proof or changed provider contract
refuses; projection itself never invokes execute, verify, reconcile or a model.
Unregistered providers retain the existing strict `outputs`/`evidence` contract.

This adapter adds no read-only MR or Work Item ActionProvider operation, native
procedure enforcement, publication, Profile activation or autonomous step timer.
Canonical broker facts may inform a separately admitted step's inputs; provider
mutation still performs its ordinary current protected-fact/policy checks. A full
workflow requires separately supported operations and actual live qualification.

The step policy snapshot attributes the exact published definition and live
assignment checks; it grants no Role authority or provider capability. Current
Role authority and canonical approval quorum are required separately, including
at ActionIntent creation and provider dispatch. Provider binding security policy
still constrains filesystem, process and network effects. Expired fences, revoked
worker identities or changed scope cannot be overridden by a stored policy allow.
