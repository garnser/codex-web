# Procedure discovery and operational admission

Operational procedures remain immutable `agent.skill` Definitions. Discovery uses
`OperationalProcedure@1.0`, the Skill resolver/security gates, canonical Resources,
Project scope, current requester identity and existing action authority/policy. No
new catalog, policy evaluator, retrieval store, operation runner or model call is
introduced. Execution/verification and outcome-derived learning remain separate.

`ProcedureDiscovery@1.0` is metadata embedded in the existing assignment,
ActionIntent and Thread history. It records actor/tenant/Project, target IDs,
operation, context checksum, evaluation time, policy source/result, selected exact
Definition references, candidate outcomes/reasons, blockers, supporting source
references and instruction checksum/size. It contains no instruction bodies,
credentials or raw reader exceptions. ActionIntent history is append-only and
bounded to 64 admissions; assignments use the same bound. Exhaustion requires a
new admitted execution rather than silently dropping old evidence. Thread history
retains its existing 1,000-entry bound and now accepts/migrates schema 1.0 to 1.1.

## Deterministic selection

Only tenant-visible governed publication history participates. Latest slot
history includes disabled, quarantined and expired records; those states cannot
vanish through an active-only query. Definition scope precedence selects the
canonical slot. Exact operation/step references and canonical action capability
requirements identify potentially applicable operations even when another provider
uses a different action ID. Native free text is unknown; it is never interpreted
as a reliable operation declaration by a model or keyword classifier.

Selectors use canonical Resource identity/type plus typed operational facts.
`Resource.operational_context` holds optional environment, bounded tags and service
versions. These are current Resource facts, not reusable definitions or permission
grants. Existing Resource administration APIs own updates. Missing facts remain
unknown and block potentially required guidance; no environment is inferred from
names, paths, prose, procedure content or request parameters. Resource catalog 1.0
migrates to 1.1 with those facts unset. Conflicting/partial target environments
remain unresolved. Project and Resource lookup failure is never no-match.

Applicable records pass the existing exact Skill revision/checksum, compatibility,
lifecycle, freshness and security gates. Payload integrity and selector schema are
verified before applicability; an unrelated engine-incompatible candidate is
explained/rejected before compatibility can block the requested operation.
Required provider/worker capabilities are requirements rather than grants.
The full registered worker capability set is read under its live assignment fence,
not inferred from assignment minima. Provider capabilities intersect the exact
pinned runtime registration, actual adapter and current canonical provider grants.
Missing/revoked facts are typed unknown, distinct from an attested empty set.
Evidence records worker identity/version/capability checksum and runtime/provider
revisions; it neither grants authority nor enables assignment network access. A different provider/contract cannot silently
implement a selected procedure's operation. Highest precedence selects within each
conflict group; equal precedence is ambiguous. Rejected, stale, unavailable and
conflicting candidates retain explicit reasons. Candidate and context limits fail
visibly instead of returning an incomplete no-match.

The selected Skill instructions, declarative procedure and relevant repository
assets use existing bounded Skill composition. Supporting Memory retrieval uses the
existing authorized/governed search, exact Project/relationship filtering, at most
three results from 32 candidates and at most 1,000 context tokens. It adds source
and retrieval-run references. Required instructions are never silently truncated.
The complete input is bounded to 12,000 characters and wrapped as untrusted tool
material. Retrieval failure blocks admission; it cannot erase a required procedure.

## Canonical mutation boundary

ActionIntent creation persists discovery alongside existing canonical authority,
policy and security decisions. A discovery blocker cancels admission. Immediately
before provider credential use/mutation, the shared ActionExecution boundary
requires an internal ActionIntent execution context, checks the exact binding and
request, resolves the original requester's current identity and Role authority,
repeats discovery and persists another result. Assignment-backed actions also
append assignment provenance with the live fence/lease. Direct provider mutation
without the canonical intent context refuses. The original creation-time trace is
retained. Discovery/precondition failure before invocation is a known failure,
never an unknown external outcome.

Changes to target, operation, environment, versions, capability context or policy
invalidate the earlier applicability checksum and cause fresh selection. A changed
procedure revision within an unchanged context requires explicit re-admission;
history does not silently adopt new instructions. Quarantine/expiry/security and
current authority are checked again even for previously admitted requests.
Rollback without an applicable procedure can retain existing policy authority;
a procedure requiring an unsupported rollback path refuses explicitly. Discovery
does not execute procedure steps, verify their Evidence or synthesize rollback.

Existing authorization and policy decisions remain independent. A match never
creates an approval or asks for user confirmation. `procedure_deviation_reason`
is bounded request/audit data on the existing ActionIntent; it changes no gate,
grants no authority and supplies no approval. Policy-required approval continues
through existing canonical controls. Rationale cannot repair failed discovery,
ambiguity, inaccessible instructions or unsupported enforcement.

All assignment-broker handlers also run discovery after canonical authorization
and before dispatch, including internal WorkItem/WorkGraph and workspace actions.
Those handlers use their exact canonical operation and capability, target state
and assignment fence. A refresh that awaits provider materialization rechecks
again before its local mutation. Unsupported/failed admission returns a typed
broker error with canonical discovery evidence. Read-only no-match broker
exploration remains under the existing authority/profile policy.

## Native runtime coverage and explicit limitation

The shared native turn-start boundary calls admission before `turn/start`,
independent of provider/model, including Thread, WorkItem, Executive/delegation,
queued and resumed/recovery turns. Provider-neutral AgentSession starts, resumes
and native objectives also run discovery/admission, covering Goal continuation.
The generic direct AgentSession adapter has no attested assignment-bound worker
transport; caller-supplied IDs/cwd/sandbox cannot prove isolation. Its protected
native start/resume/objective paths therefore remain typed unsupported. Supported
local preparation uses the common assignment-bound TurnExecution path.
Raw provider RPC start/resume/objective escape hatches refuse explicitly. It records results in canonical Thread history
and, where present, the exact live assignment. Native providers currently expose
no universal hook intercepting every operational tool mutation. A successful
no-match retains existing policy-authorized isolated repository/scratch preparation,
including ordinary local repository writes. This requires an exact effective
execution-profile pin, matching bounded sandbox, live worker assignment and
workspace leases, registered capability evidence, repository-only local targets
(or resource-free scratch), and disabled network/host mutation. The profile is
resolved through the existing Definition service; task prose supplies no authority.
Changed pins, broader scope, unknown envelopes and matching required procedures
return `procedure_unsupported_enforcement`. Prompt instructions or another user
confirmation cannot enable unsupported native operational writes. Trusted-local sessions
without the isolated assignment boundary also refuse. This limitation is explicit;
this change does not claim safe operational writes through arbitrary native tools.
Protected provider mutations use the separately enforced ActionIntent boundary.


The supported inner modes are `read-only`, `workspace-write` and
`danger-full-access`. The last disables Codex's inner command sandbox; the outer
[isolated worker boundary](execution-worker-boundary.md#local-isolated-execution-backend)
continues to enforce filesystem mounts, resource limits and network isolation.
All writable inner modes require mutable repository access, matching writable
repository scope and write leases. Scratch preparation remains resource-free.
The named Agent Profile and actual inner sandbox value are preserved.

Assignment networking is owned by the exact resolved execution profile,
independently of the inner mode. Current profile schemas require disabled network
and host mutation; no broader profile is made publishable here. If a future
resolved contract permits networking, assignment construction preserves its network
capability even with a restricted inner mode. Backend enforcement and native
admission remain independent gates: native preparation still refuses any enabled
assignment/profile networking or host mutation. Legacy unprofiled assignment
construction retains its sandbox-derived network setting, but cannot obtain this
profile-pinned native preparation admission.

UI impact: existing assignment inspection renders the actual sandbox separately
from effective network policy, scope, leases and exact profile provenance. The
execution-profile inspector exposes host-mutation constraints; procedure inspection
retains blockers. These canonical surfaces explain the independent constraints;
no new UI state, mutable definition, schema migration or model call is introduced.
Production compatibility requires separate runtime qualification.

## Inspection, evolution and operations

`POST /api/skills/procedure-applicability` is an authenticated, side-effect-free
preview over the same resolver, canonical provider contract, Resource facts and
current authority. It neither creates an intent nor authorizes mutation.
Optional `task` supplies a bounded preview filter. Optional `expected_environment`
only detects disagreement with resolved Resource facts and adds
`procedure_preview_environment_conflict`; it cannot overwrite context. The response
includes resolved `input_context` and the existing canonical authority snapshot
with reasons and exact Definition references. These are additive read-only fields;
selection/admission engines and persisted discovery contracts are unchanged.
ActionIntent history exposes every result; its existing browser inspector shows
scope, exact pins/checksums, sources, candidates and blockers. Assignment inspection
exposes the same typed evidence. The existing Skill editor, Resource editor and
assignment/ActionIntent/Incident inspectors consume these canonical services.
Authoring controls derive from the code-owned procedure schema and create inactive
Skill revisions. Scope visit fences include tenant/identity changes and reject
late responses even after an A→B→A Project visit. No browser-owned procedure/run
state or extra model call is introduced.

ActionIntent document schema 1.2 migrates to 1.3 with empty discovery history;
worker-state 1.9 migrates to 1.10 with empty assignment evidence. Old exact Skill
records are unchanged. Unknown nested versions fail closed. Older binaries cannot
consume the new fields/state versions: stop workers and restore a compatible
snapshot before downgrading. Never run mixed writers or imply rollback of newly
persisted contracts. No deployment, existing-input replay, host access, credential
expansion or automatic procedure publication follows from source installation.

## Assignment history exhaustion during messaging

An assignment retains at most 64 procedure-discovery observations. Exhaustion
raises a typed controller refusal before native input; the ordinary message API
retains the complete original in the existing execution-preflight domain and
shows its blocker in the thread inspector.

A new human web message may perform one bounded renewal followed by the existing
fenced preflight retry. The original is durable before the first renewal await.
Renewal shares the thread's admission lock and requires the exact authenticated
owner, tenant/Project and bootstrap, live assignment/fence, no active turn or
handoff, no pending executor work, inspectable managed-validation custody and a
provider-confirmed idle state for the same Codex native thread. Running, malformed
or incomplete managed output vetoes renewal; absence of output alone is never
idle proof. Each repository member is inspected read-only in the current isolated
worker. Dirty checkouts or commits beyond the original base require a canonical
handoff before renewal. Inspection and preparation have a 30-second aggregate
deadline, and owned synchronous work is settled before cancellation returns.

The existing bootstrap cancellation fences the predecessor and retains its files
and discovery history. Fresh admission preserves requested repository scope,
runtime/profile bindings and resource limits. Bootstrap reassociation compares
the expected predecessor atomically. Ordinary resume performs the existing
canonical AgentSession custody transaction before any turn input; it does not
create a native thread or set/replace a native Goal. Concurrent messages retain
their separate originals and retry claims.

Explicit retry of an exhausted retained queue original can use the same renewal
checks. Automatic renewal is not an idle timer or an agent-origin-to-human
conversion. It adds no model call, provider fallback, history deletion or cap
increase. An uncertain resume/input or cancellation after delivery admission holds
the original as `reconciliation_required`; expiry of a retry claim cannot authorize
replay. A cancelled renewal inspection leaves the original blocked and recoverable.
The existing preflight UI exposes renewal requirements and hides Retry for unknown
outcomes. Existing fields/statuses are reused without a persisted schema change;
the platform's existing drain, compatibility and rollback constraints still apply.
