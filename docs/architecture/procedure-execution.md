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

Worker state 1.13 retains a current `procedure_checkpoint` and up to 63 completed
runs in `procedure_history`. New nested checkpoints use contract 1.1 with an
immutable `procedure-run-*` ID and canonical typed-input checksum. Legacy 1.0
checkpoints retain their exact serialized fields and original action/output/
approval identities; their read-only run identifier is `legacy`. The explicit
1.12→1.13 domain migration initializes empty history without rewriting legacy
proofs. Older assignment readers reject the new facet rather than ignoring its
current-run custody. Physical stores and their schemas do not change.

A completed run may be archived atomically only after all its started effects
have trustworthy successful original receipts and no live claim. Current worker,
requester, lease, scope and published Definition admission remain required. A
new admitted input selects a new current run within the SAME assignment, native
process, resource lease, limits, counters and assignment deadline. No artificial
assignment renewal or terminal worker outcome is created. The new run has its
own Definition-bounded procedure deadline, still constrained by the original
assignment lifetime. Capacity exhaustion refuses; history is never evicted.
A copied input Artifact or changed JSON formatting cannot replay the same typed
values for the same exact Definition, including a retained run.

Run ID plus checkpoint revision serializes step writes. New Intent idempotency,
output/Evidence origins, approval targets and outcome/Memory delivery identity
include that run ID; legacy origin keys remain unchanged. An archived checkpoint
is read-only. These facets retain
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

New native thread bootstraps can receive one server-owned credential-custody
admission after the actual worker is claimed and before initial native input.
The bootstrap builder alone creates the pending `native_credential_custody`
marker; public AssignmentCreate cannot supply it. Canonical native preparation
selects effective published procedures and requires the exact current execution
Profile's procedure operations. It derives only the selected procedures' unique,
enabled provider bindings within the assignment's Project/Resources, using the
same selector as procedure execution. The canonical action requester remains
`assignment.created_by`; an authorized web/Slack input actor is checked separately
by native input/discovery admission and does not acquire provider SecretUse.
Both that action requester and the actual authenticated worker must currently
be allowed to use every admitted credential reference.

Supporting Memory retrieval appends its normal canonical retrieval audit outside
the credential transaction. Locked credential revalidation and the selected-procedure
native input guard repeat the same current procedure selection, security, instruction-capacity
and attestation checks without that optional retrieval. Ordinary preparation
still retrieves bounded supporting context; it grants no credential authority.

A canonical transaction rechecks the live worker, lease/fence, workspace,
selected Definitions, bindings, security and policy before sealing the finite
reference union and its provenance once. Only an unused pending bootstrap may
transition; historical assignments are never retrofitted. Later preparation and
the guarded native-input path validate the immutable pins without adding refs.
New publications, changed bindings, revoked Use or changed custody refuse rather
than enlarge the old scope. Existing discovery/audit records still retain denied
admissions. A normal same-token/worker/fence lease renewal is allowed, while
scope, token, worker, fence and assignment deadline remain fixed CAS predicates.
An initial no-match seals an empty procedure scope as well.

Runtime authentication stays separate: the original runtime credential reference
(or explicit `None` for agent-owned local sign-in) is pinned at bootstrap. Reuse
must resolve to that same reference and authentication mode; a mutable config
change cannot substitute a provider credential. Purpose-specific runtime
delegation remains unchanged. Provider values are borrowed only by the existing
ActionExecution/SecretBroker boundary, never sent to native prompts, environment,
shell or a generic secret-delivery API. Procedure `_secret` keeps its explicit
assignment-membership guard and checks current native custody/Use at execution.
Unmarked generic/legacy procedures retain their prior requester-use contract.

This is worker **state** contract 1.14, distinct from the unchanged thread-turn/
bootstrap execution-contract versions. Its 1.13→1.14 domain migration grants
nothing: historical assignments remain unmarked and serialize byte-for-byte
without the optional field. There is no SQL schema migration. Marked assignment
rows are not readable by the prior 5936 `extra=forbid` model, including direct
keyed reads; rolling back to that binary after new custody records are written
is unsupported without explicit compatible-state recovery. This compatibility
limit does not itself impose a backup gate. Existing authenticated worker
inspection exposes reference-only pending/admitted/legacy status and provenance;
recorded custody is not a claim of current authority.

`POST /api/execution-workers/assignments/{id}/procedure/start` binds an admitted
Definition and input Artifact to a live assignment. `/advance` processes at most
one step per request/event. After rollover, `/advance` and `/deviate` require
`run_id` equal to the current run; a stale caller cannot operate its successor.
`/procedure?run_id=...` reads an exact current or retained run, with
`current_run_id` and a bounded `procedure_runs` index. The existing assignment
inspector exposes a retained-run selector/deep link and ActionIntent lookup
includes retained runs. Outcome inspection/conversion also accepts `run_id` and
resolves original source Knowledge to that run. `/procedure` reads the current checkpoint; `/deviate` records
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

GitLab additionally registers `code-host.pull-request.verify-merge` as a bounded
read operation under existing `repository.read.metadata` authority. It accepts
only `pull_request_number` (positive integer), `head_sha` and `merge_commit_sha`
(exact lowercase SHA-1 revisions). These names consume the originating merge
step's typed outputs without a caller script or transformation. Its fixed MR GET
requires actual merged state and those exact revisions; independent provider
verification performs another fixed GET. Its typed outputs retain those three
fields plus `merge_verified=true`, backed by the actual original verification.
It never performs READY, merge, CI refresh/rerun or a fallback write.

Only this exact registered GitLab 1.2 read contract may use an assignment's
writable **or** read-only repository scope. Ordinary mutations still require the
writable scope. Current binding/target, actor authority, Profile/Definition,
assignment/worker fence, lease and procedure step are checked through existing
admission before and after each observation. The scope predicate reads metadata;
it neither resolves a credential nor lends another principal credential rights.
The worker's normal credential boundary remains mandatory. Changed custody,
binding, proof or an unknown read outcome retains the original intent rather than
retrying with another key. This supplies a real later `verification` step after a
merge; it is not permission to execute a required procedure on native tools.

The step policy snapshot attributes the exact published definition and live
assignment checks; it grants no Role authority or provider capability. Current
Role authority and canonical approval quorum are required separately, including
at ActionIntent creation and provider dispatch. Provider binding security policy
still constrains filesystem, process and network effects. Expired fences, revoked
worker identities or changed scope cannot be overridden by a stored policy allow.

### Assignment-bound procedure and Executive adapters

The assignment control-plane broker exposes `procedure.read`, `procedure.start`
and `procedure.advance` through the existing assignment procedure routes. The
path assignment must be the current broker assignment. Start takes the canonical
exact Skill Definition reference and input Artifact ID; the procedure owner still
checks published applicability, governed input, isolated workspace, worker lease,
fence, budgets and checkpoint CAS. Advance executes one existing interpreter
step, without an independent controller or automatic retry.

`executive.activation.create`, `.read` and `.consult` use the canonical Executive
activation service. Read on `/api/executive/activations` returns only this exact
assignment origin, so a lost create response does not require another activation.
The origin is `assignment:<assignment_id>:fence:<fence>`. This initial native
adapter deliberately supports one activation per assignment/fence. Create must
supply that exact `trigger_ref`, explicit `requested_role_ids` and matching
`max_roles`. Its canonical activation ID is deterministically derived from tenant
and origin; the existing store's ID uniqueness prevents concurrent duplicate
creation. Reusing the origin with different canonical request content refuses.
Creation does not consult or materialize proposals. A completed consultation can
be read again; a concurrently reserved or failed consultation is not retried.

Every operation requires an explicit allowlist entry in the current pinned
Execution Profile, current requester authority, authenticated worker identity and
live canonical assignment/resource lease. No identity, assurance, tenant,
resource, provider or scope override is accepted. Service requesters additionally
retain the canonical `executive:consult`/`executive:admin` scope requirement.
Credential-free requester refresh does not borrow human MFA from the worker.
Executive context references must remain inside the assignment project and
resource envelope. For `orchestration-admin` only, the existing current pinned
`administrative_repository_ids` declaration can supply an explicit target envelope:
every referenced repository is reauthorized through the canonical administrative
target checks and ordinary authority. Empty assignment resources never mean all
resources. This administrative expansion does not apply to procedure execution;
an MR procedure needs its actual compatible repository assignment and lease. Automatically included WorkGraph nodes are checked against
that envelope. Business and Memory projections without an assignment Resource
proof are unsupported in this native adapter and fail closed; human Executive
management remains a separate canonical authority boundary.

Executive preparation is bounded to five seconds before persistence; its existing
finite role/call/token/cost budgets govern consultation. Consultation reserves
PLANNED status, revision and budget inside the canonical store critical section
before invoking a model. Routine facts, routing and MR transitions remain
deterministic; the adapter does not require an Executive call for each MR.

These adapters do not make arbitrary ActionProviders procedure-compatible.
GitLab's ordinary flat ActionResult and provider receipt are not the typed
`outputs`/canonical `evidence` map required by procedure execution. A workflow
must use a registered adapter that satisfies that contract before publishing an
enforced operational procedure. Existing procedure trace and Executive management
UI continue to inspect the same canonical records.

### Attested native preparation for provider procedures

A selected required procedure ordinarily refuses native tools whose future
operation is unknown. A narrow exception permits isolated **preparation** for
procedures whose privileged operations all resolve to registered canonical
ActionProviders. This exception is installed only by the existing native turn
owner and interpreter; it is not a serialized request/Definition flag.

The first discovery must have selected exact procedure revisions and have only
the existing unsupported-tool-enforcement blocker. Missing environment, failed
reads, inaccessible/stale Skills, conflicting selection or missing capabilities
remain refusals. The actual assignment-bound process session must freshly prove
its current worker/fence/delegation, ready owned process, current workspace lease,
OS-enforced network-disabled Bubblewrap backend and serving broker bound to the
same assignment. A configured broker URL, stored digest, generic AgentSession or
trusted-host backend is insufficient. Existing profile policy must additionally
exclude network and host mutation. Worker/local procedure steps remain unsupported
by this preparation path, including recovery steps without a canonical provider.

The interpreter resolves every selected action's exact registered provider,
instance, contract, enabled binding and target envelope without credentials or
provider transport. The canonical ActionIntent pre-dispatch hook must actually
be installed. A process-local live validation scope permits one second discovery
only for the exact same selected revisions; its bounded provenance participates
in the discovery fingerprint. Changed subsets or facts fail closed. The existing
guarded native resume/start boundary revalidates that same fingerprint, session
and broker immediately before input. Unsupported unguarded transports refuse.
No alternate controller, polling loop, store or remote-write path is introduced.

Preparation does not create a checkpoint, approve a step or execute a provider.
Direct provider mutation without an ActionIntent remains denied. An ordinary
ActionIntent matching a required procedure still cannot execute without the
exact current durable interpreter step, request, approvals and receipts. Local
repository edits cannot publish remotely through the isolated network envelope;
remote operations retain canonical broker/ActionIntent checks. Existing trace UI
shows the selected revisions and `canonical:native-provider-preparation` source;
the source string itself confers no authority.

A native scratch context can prove its physical resource scope is exactly empty through the same live broker/session owner, current scratch/none Definition, workspace and resource lease. Only registered provider-only procedures may then be rejected as `native_physical_scope_empty` for that single discovery; pinned-reference errors and local/worker procedures retain ordinary refusal. This does not select or authorize a future remote target. Later broker actions resolve their actual target and enforce the required procedure normally. The proof is rechecked at native input. Nonempty preparation additionally requires the current Definition to declare all three procedure broker operations and the actual broker to use the same interpreter.

### Exact context reads through the assignment broker

`goal.read` and `decision.read` expose one canonical object in the assignment's
tenant and Project. Goals require exclusive bindings to that Project; linked
Work roots retain Resource checks. `skill.procedure.read` reads one effective
published operational-procedure Skill through its canonical owner, including
the full Definition reference, procedure and security view. The content GET query
must provide the exact `record_id`, `revision` and `checksum`; a superseded or
changed reference is refused rather than silently replaced by a newer revision.

For a known procedure Skill ID, the same declared `skill.procedure.read` operation
also accepts the sole query `view=reference`. For example,
`GET /api/skills/my-procedure?view=reference` returns only
`{"item":{"definitionReference":{...}}}`. This metadata-only mode resolves the
current effective published reference under the assignment's tenant and Project,
validates the procedure's current security, lifecycle, review and compatibility,
then rechecks publication and validation before returning. It does not return Skill
content, list definitions or alter any assignment or execution pin. It allows a
repository-free coordinator whose admission selects no repository procedure to
discover the exact reference after publication changes without operator-supplied
record/checksum values. The existing content read and procedure START continue to
require exact pins and current revalidation. Empty, mixed, repeated or unknown
query fields fail closed; a stale exact read is never converted into resolution.
Scope, security, lifecycle and freshness gates remain in force. No inventory or
generic Definition payload endpoint is exposed by these operations.

UI impact: no new object or control is introduced. Existing Skill/Definition
inspection and broker denial/provenance surfaces remain canonical; the advertised
operation help describes both modes. Reference resolution is deterministic and
uses no model or provider calls, new store, cache, schema migration or grant.
Existing Profiles declaring this read operation need no pin rewrite; a Profile
that does not declare it still cannot resolve or read a procedure.

All three reads require an explicit operation declaration in the current pinned
execution Definition, current worker and workspace leases, requester authority,
and assignment/fence revalidation before returning data. The assignment catalog
advertises only currently declared reads. The repository-write bootstrap seed
declares them for new installations; an existing administrative catalog requires
a separately reviewed canonical Definition publication. Deployment does not
rewrite existing catalog data or assignment pins. These reads grant no mutation,
provider, verification-publication or procedure-execution rights. Repository-free
Orchestrator coordination remains separate from a repository worker's WRITE lease.

### Versioned MR observation validity

Procedure schema `1.3` adds the explicit `code-host-mr-procedure-input.v2`
protocol. It consumes only a broker-published v2 continuation Artifact and its
original Evidence, exact provider/head/provenance and canonical governance links.
Schema `1.2` / input v1 keeps its fixed 30-second window. A v2 input cannot be
passed through generic Verification assertions or silently consumed by a v1 Skill.
The procedure-schema API and broker catalog advertise the new versions. Existing
published Skill revisions and assignment admission pins remain unchanged; adopting
v2 requires a separately reviewed, scanned and published Skill revision.

The canonical deployment Configuration key
`code_host.mr_observation_window_seconds` has default `30` and inclusive bounds
`1..300`. The existing Configuration editor supports draft, validation,
publication, effective-value inspection and revision history for this key. Changing
it affects newly resolved v2 observations only. Each observation records the exact
value, resolution time, Configuration record/revision and immutable payload digest;
default resolution has explicit default provenance. A later value never extends an
old expiry. Resetting the override revokes observations pinned before that reset,
even if another override is subsequently published.

The registered read-only readiness action uses the same v2 policy mechanism for
its ActionResult and derived provider-receipt/Verification Evidence. Verification
of that ActionResult reuses its original pin, not a newly enlarged window. Current
principal, published Skill, lease/fence, credential version, binding, provider and
exact-head checks still run before and after the fresh provider read. Configuration
does not extend HTTP timeouts, procedure deadlines, leases, retries or mutation
permissions. The result remains an observation: pending review/QA cannot become
merge approval. Evidence metadata exposes the pinned policy and absolute expiry
through existing Evidence/procedure inspectors; historical records are distinct
from currently usable observations.

Observation validity is separate from operation budgets. The original registered
read-only action/ActionIntent execution budget remains 30 seconds, including its
canonical execution checkpoints. A larger observation window alone promises no
additional execution time.

The additive `code-host.pull-request.observe-readiness.v2` operation instead
registers a 300-second hard maximum and requires an exact current published schema
`1.3` / input v2 procedure. Its existing persisted ActionDefinition timeout field
and the step's declared timeout carry the bound; there is no new request timeout
field or stored schema. Execute, fresh verification, output projection and final
completion share the earliest of the procedure deadline, step start plus its
timeout, and any assignment deadline. Fresh verification does not reset the
deadline. Current lease/fence checks remain independent, allowing ordinary valid
same-fence renewal without granting more procedure time. Canonical result
projection for the new operation uses the existing joined owner and cooperative
storage budget with only the remaining shared deadline; it does not start another
execution window. Legacy projection retains its five-second bound.

Each guarded reader permits at most 30 cumulative seconds of actual provider I/O
across its requests; canonical before/after checks run within the wall deadline
but outside that network counter. Trusted v2 broker publication reads use the
same separation, capped at 300 seconds and the current assignment deadline.
Legacy readers and the original action keep their whole-operation bounds.
Existing input expiry, current-use governance, authority and exact-head checks
are unchanged. A known durable execution result is retained if a later phase
refuses completion. The provider-wide contract stays `1.2`; old binaries reject
the new operation ID before execution. A separately reviewed, scanned and
published Skill revision adopts it, with operation ID and timeout visible through
existing catalog and intent inspectors. See [guarded readiness execution](code-host-providers.md#guarded-readiness-execution).

The registered maximum is a code-owned safety ceiling, not a replacement for
published procedure capacity. A new reviewed Skill revision may declare a
240-second step and 300-second parent; previously sealed 120-second steps retain
120 seconds. An already-created observation ActionIntent also retains its
original ActionDefinition snapshot and timeout; the larger current registry does
not rewrite those durable fields or extend the sealed step. Read-only observation
execution still revalidates current authority and the published procedure. It
has no new blanket snapshot-drift refusal; the separate PR-ready mutation's
snapshot-equality checks are not observation semantics. Raising the ceiling also
raises the trusted v2 broker publication
wall allowance, because it uses the same bound, without raising either reader's
30-second I/O allowance. No action ID, provider contract or wire/schema changes.

Observation validity must be published separately through canonical Configuration.
A new 300-second policy applies only to new observations. Historical 180-second
pins keep their original records, digests and expiry. The final same-run advance
still reads current inputs and proofs before committing completion; its work and
pre-step publication/admission latency consume freshness too. These finite bounds
provide operational capacity, not a guaranteed completion margin or permission
to reuse stale facts. Old binaries can read the ordinary records but their
120-second registry cannot publish/admit a new step above 120 seconds. Existing
catalog, Skill, Configuration and ActionIntent inspectors expose the same fields;
there is no separate UI budget or authority grant.

An execution timeout remains UNKNOWN until its exact original intent/receipt is
reconciled; it does not authorize another request, new nonce or provider replay.
The controlled private 47-second input-to-start case is not a live host-capacity
or network-delivery guarantee.

Procedure schema support is owned by the central interpreter and advertised in
the broker's `procedure_schema_versions` plus the generated Skill procedure
schema. Local worker registration advertises execution transport contracts and
capabilities, not a second procedure-schema version. Existing runtime capability
revision and worker/lease checks remain enforced; a new published Skill revision
cannot enlarge an existing assignment's sealed procedure custody.

MR input admission evaluates the requester, context requester and executing worker
against one fresh selected Governance record observation per validator call. Each
principal retains its independent classification/lifecycle filter and current API
and Role checks. The observation never crosses a provider/network checkpoint or
request. Custom Governance filters and different tenants retain ordinary separate
filter calls. This changes no stored format, authority or observation deadline;
there is no additional operator UI setting.
