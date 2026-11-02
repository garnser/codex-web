# Skill operational-procedure contracts

An operational procedure is the optional `operational_procedure` field of an
`agent.skill@1.0` Definition, with its own explicit `schema_version: "1.0"`.
It shares the Skill's stable identity, tenant scope, immutable record/revision,
checksum, ownership, source provenance and lifecycle. It is neither a second
catalog nor current operational state, policy, configuration or Memory Knowledge.
The code-owned schema is `OperationalProcedure` in `skill_procedures.py`.

## Declarative content and bounds

The contract describes exact task/operation triggers, canonical Resource selectors
(IDs, types and tags), environments, inclusive service-version intervals, owner,
typed required inputs, review/freshness dates, provider/worker requirements and a
conflict group with numeric precedence. Equal/conflicting matches must fail closed;
precedence can only order otherwise eligible procedures and cannot override policy.
Matching and checking known facts requires no model. Schema authoring does not
perform discovery or admission; those consumers are tracked separately.

A procedure contains 1–32 ordered diagnostic, mutation, verification or conditional
recovery steps. Each declares typed inputs/outputs, preconditions, required Evidence,
explicit timeout, attempts, backoff, failure outcome, escalation owner and reason.
Conditions reference declared inputs or preceding step outcomes; they cannot contain
expressions or executable code. Recovery references must point forward to a recovery
step conditional on the referring failure. Mutation and recovery must describe
bounded rotation/restart/deployment/configuration/rollback impact and point to a
later verification conditional on success. Unknown outcomes require reconciliation
before retry. Recovery failure cannot create a backwards loop.

Each step permits at most five attempts, a 3,600-second timeout and a 300-second
backoff. The sum of all worst-case step times, including conditional recovery,
must fit the declared duration (at most 86,400 seconds). The complete serialized
contract is limited to 64 KiB. Input/output types permit references, including
secret references, but no embedded values/defaults, arbitrary parameter bags,
scripts, commands, URLs, shell or network grants. Executable helper assets cannot
be embedded in a procedure Skill. Obvious executable blocks and raw secret material
are rejected structurally; remaining prose is untrusted and reaches the scanner.

## Authority and operation references

External mutations reference registered ActionProvider type, instance, operation ID
and exact provider contract version. Validation reads tenant-visible registration
metadata only; it cannot call provider preparation/execution, resolve credentials
or create bindings. Unsupported actions, contract mismatch, Resource-type mismatch,
missing declared permission requirements, excessive registered timeout/retry bounds,
non-idempotent mutation retries and unsupported reversibility fail closed.
Diagnostic/verification ActionProvider steps require a registered read operation.
Executable provider work additionally requires a declared isolated worker capability.

Worker steps reference an existing capability enum and an immutable artifact ID and
checksum, target a declared Resource selector, and explicitly require the canonical
isolated worker boundary. A capability requirement is not a capability grant.
Artifact existence, authorization and execution compatibility remain admission checks,
not assertions inferred from stored descriptions. The schema does not load artifacts
or grant host, filesystem, network, credential or control-plane access.

Permissions and approval counts/roles are requirements. Consumers must evaluate the
acting identity, tenant, Project, Resource and environment through canonical policy,
Authority and ApprovalRequest services; effective requirements include the registered
operation and policy requirements even when stricter than the procedure. They must
use ActionIntent/ActionProvider for external changes and fenced assignments/leases,
network policy and resource limits for executable work. Content cannot reduce these
requirements, approve itself or authorize recovery. This delivery adds no operation
planner, runner, learning path or model invocation.

## Registry, review, security and imports

The existing Skill API authors workspace-scoped definitions. Procedure semantic
validation resolves that canonical workspace to its organization using trusted
identity state. Resource IDs and tenant provider registrations are checked in that
scope. Unsupported or unresolved tenant scopes fail visibly. Ordinary Skill scope
behavior is unchanged. Definition scope and Resource selectors describe eligibility;
actual resource authority remains independent.

Draft creation and explicit validation check references. Publication rechecks them,
freshness and the existing revision-bound Skill security policy. A code-owned
Definition schema publication hook enforces the same security gate for generic
Definition publication, atomic publication and rollback, not only the Skill API.
Scanner content includes the structured procedure; scans cannot be inherited by a
changed checksum. Scanner failures/quarantine and policy-controlled audited overrides
retain their existing semantics. Expected metadata/security/freshness hook denials
are canonical `DefinitionGateDeniedError` results: generic Definition HTTP routes
return HTTP 409 with a stable procedure code, gate, exact Definition reference and
available security/policy evidence. A rejected publish preserves its draft; a
rejected rollback retains its new inactive revision and original history/scan evidence. Unavailable security/tenant readers fail closed. Pre-registering the schema helper
installs fail-closed unbound procedure gates while preserving ordinary Skills.
Constructing the canonical SkillService binds those gates without replacing the
validator, checksum contract or persisted payloads; an incompatible/custom
registration or a second service binding is rejected visibly.
Archiving an existing procedure remains a safe inactive transition and does not
require fresh operation registrations or reactivation approval.

Repository runbooks enter through an ordinary Skill bundle/catalog import. Imports
retain source/evidence/revision provenance, are marked imported and stay inactive
drafts. No filesystem is polled or used as an authoritative procedure catalog.
Repository examples are import templates, never installed procedures. Review and
publication remain explicit canonical operations by an authorized owner/operator.

## Evolution, pins and required failures

Adding the optional contract does not rewrite old records. Normalization omits an
absent contract and retains the old canonical Skill payload/checksum shape. Existing
schema-1.0 Skills therefore resolve unchanged. Changes to procedure content create
new immutable Skill revisions. Supersession preserves exact historical payloads,
record IDs, schema versions and checksums; rollback creates a new reviewed revision
and passes the current gates. Imports never carry activation or scan approval.

The nested procedure and referenced provider versions are explicit compatibility
boundaries. Unknown versions fail closed. A future incompatible schema must register
an explicit validator/migration and create a new revision; it cannot reinterpret or
rewrite a historical payload. Exact payload inspection remains available independently
of current provider availability or freshness; new use is always checked afresh.

`validate_required_procedure` and its tenant-scope counterpart are required-procedure
facets of the existing exact Skill reference resolver. They return that same
`DefinitionReference`, including checksum, or typed `SkillResolutionFailure` codes:
`procedure_missing`, `procedure_disabled`, `procedure_quarantined`,
`procedure_expired`, `procedure_incompatible`, or `procedure_security_blocked`.
Foreign/missing records share the missing result. Ordinary Skills remain usable
without the extension. Procedure references also participate in existing capability
requirement aggregation; this checks requirements without granting them.

## Operator/UI delivery

The existing Definition inspector shows full canonical JSON and revision/checksum,
validation/publication/quarantine/rollback state. Skill details expose the full
contract through the canonical API and portable bundle; the current Skill form is
not a structured procedure editor. `GET /api/skills/procedure-schema` exposes the
code-owned JSON schema to authenticated readers. Existing updates preserve the
extension unless explicitly replaced or cleared.

The Skill editor derives structured authoring controls from the canonical schema.
History/diff, scope/provenance, validation, exact-revision scanning and lifecycle
operations use the existing Skill/Definition APIs. Applicability previews and
assignment/ActionIntent/Incident traces consume their canonical services; a
browser form never becomes a procedure, policy or run authority. See the
[operator guide](../operations/procedure-admission.md).
Discovery/admission, runtime execution and outcome-derived draft work remain separate
contracts; this schema does not implement those downstream delivery slices.

Discovery consumers use the [canonical discovery/admission contract](procedure-discovery.md).
It preserves this exact Skill identity and fails visibly for unsupported native
mutation enforcement. See the [operator guide](../operations/procedure-admission.md)
for outcomes and provenance inspection.

Procedure contract 1.1 adds declared governed judgment and originating-intent provider rollback; 1.0 serialization/checksums remain unchanged. See [durable execution](procedure-execution.md) for the runtime and compatibility contracts.

## Governed observation inputs and same-head continuation

Nested procedure schema `1.2` adds the optional, code-owned `input_protocol`.
The fixed `code-host-mr-procedure-input.v1` protocol requires the managed input
Artifact returned by assignment-bound `verification.create` method
`code-host-mr-continuation-input.v1`. A caller-authored fingerprint is not a
provider fact. Start and every input use reconstruct the exact typed content
from the original three governed provider Evidence records and Verification,
checking current assignment/fence, producer, Project/Resource, provider binding,
ancestry, authority, lifecycle and the original finite observation expiry.
Pending, denied, incomplete or rejected provider observations remain observations;
they do not satisfy generic passing Evidence requirements or grant merge consent.

The content identity includes actual head/scope and review/check/policy business
facts, including UNKNOWN/null states. Observation times, delivery request IDs,
provider check run IDs, proof/Artifact IDs and URLs do not create new semantic
input. Repeated identical facts therefore deduplicate even through a new read.
A genuinely changed same-head observation can start the next settled procedure
run within the existing assignment. Original receipts and budgets remain intact;
there is no model polling loop, assignment renewal, new grant or second store.

The existing schema-driven Skill editor exposes and preserves the required
protocol. Publish the reviewed exact new Skill reference and explicitly bind the
appropriate Profile revision only after deployment; changing prose or silently
replacing an old pin is insufficient. Legacy `1.0`/`1.1` serialization is unchanged.
Those readers visibly refuse `1.2`; no automatic downgrade or operational rollback
across a published new protocol is implied.

The original observation is not valid forever: current start/advance use must
meet its original 30-second bound. An explicit advance may carry
`current_input_snapshot_id` as a fresh canonical witness with exactly the same
immutable content/checksum, assignment, scope and protocol. It does not replace
the pinned original Artifact, run identity, history, steps or budgets. Changed
content cannot witness an active run; it requires ordinary settled rollover.
No implicit latest-proof selection or expiry extension occurs. A request-scoped
witness is cleared even on refusal and is never reused by an unrelated task.

Publishing input content uses the existing managed content-addressed backend.
Canonical Artifact/Evidence/Verification pointers and Governance commit together
under the existing guarded transaction, including late authority/freshness
checks. Blob persistence itself is not a filesystem transaction; a refused
append can leave an unreferenced blob. Never delete a shared digest as rollback.

## Canonical control-plane steps (procedure schema 1.4)

An explicit `control_plane` step references a code-registered `operation_id`,
`contract_version` and `resource_selector`. This fourth operation arm is mutually
exclusive with ActionProvider, worker and judgment arms. Earlier schema versions
reject it, including explicit null, and retain their serialized payload shape.
No stored URL, actor, provider impersonation or arbitrary argument bag can register
an operation or confer authority. Skill validation checks the registered typed
input/output contracts, capabilities, Resource types, read/mutation kind and finite
single-attempt bounds. Ordinary ActionProvider request keys and proof paths remain
unchanged. Existing native preparation attestation remains required; a registered
control-only contract does not substitute for a live session proof.

The interpreter runs current identity, assignment/fence/lease, Resource, approval
and policy checks before dispatch and again at the actual registered canonical
owner boundary. Handler context contains server-resolved principals, including the
existing worker actor, and only the selected step's already assigned Resources.
A zero-resource orchestration assignment cannot acquire repository authority from
a Work reference or a procedure selector. Provision a genuinely authorized scoped
supervisor through normal admission or retain a missing-target-context blocker.
An expired target cannot execute its own recovery procedure.

The existing assignment checkpoint reserves RUNNING before an operation. A repeated
or interrupted mutating step calls its registered reconciliation owner rather than
executing again. Waiting, refusal and uncertain outcomes remain distinct; uncertainty
blocks later effects and survives subsequent lease or authority failures. Bounded
outputs use managed Artifacts and canonical Evidence, preserving original references
without relabelling provider receipts or inferring native completion. A process-local
original-intent capability is available only to a registered recovery owner and is
never installable by Definition/input JSON; ordinary action matching is unchanged.

Custody operations use exact current Work/Role/scope and handoff nonce, positively
classify busy/unknown/idle/actually expired/terminal custody, and preserve old native
UNKNOWNs. Exact-target expiry uses the normal worker expiry writer and clock rules;
other assignments are untouched. Successor preparation and dispatch remain with the
existing handoff owner and its durable reservation, process join, native admission
and queue preservation guards. Final acceptance requires the actual matching scoped
START claim/correlation/evidence/receipt plus legitimate recipient ACK. Sender
responsibility is never transferred by interpreting instructions as acknowledgement.
A policy-selected coordination Role is not an Identity: unresolved routing produces
explicit Attention instead of a guessed person or a grant.

These operations are executable through the interpreter once trusted application
composition registers their owners and a reviewed procedure is published. They do
not themselves install an automatic event consumer. Automatic deterministic
start/advance must be wired through existing CanonicalEvent, AutomationRun and
coordinator owners; no autonomous recovery claim follows from manual API coverage,
and no poller, idle model turn or second execution ledger is introduced here.

A resumed canonical mutation always enters its registered reconciliation owner.
A truthful pre-effect waiting observation may permit that owner to make its
first attempt after an actual material precondition changes. The interpreter
supplies an immutable `previous_step` containing the previous status, failure
code, verified diagnostic operation code and exact validated diagnostic Evidence references; input content cannot
supply it. The existing RUNNING checkpoint reservation consumes that marker
before reconciliation. An interruption therefore leaves no reusable waiting
witness. Waiting alone, an absent receipt, or an UNKNOWN result never permits
execution. The owner must still revalidate the exact original target, fence,
nonce, authority and canonical reservation before any effect.


### Event-owned canonical procedure Automations

Automation schema 1.2 adds an explicit `canonical_procedure` target with an
exact published procedure Definition reference, a canonical supervisor Role,
and a finite `max_advances` (1–64). It accepts only canonical `work.transition`
triggers, reuse-only Work policy, concurrency/attempt limits of one, zero model
budgets and a finite duration. It does not grant an assignment, Resource,
Identity, transport, or recovery permission. Earlier Automation schema validators
reject this arm, including explicit null new fields.

Work watchdog events supplied with a canonical Work snapshot normalize
`work_item_ref` and `handoff_status` (`pending`, `accepted`, or `none`) from
that snapshot before ingestion and matching. Legacy `ref` remains available.
Caller matching fields cannot override these canonical values; a pending-only
procedure filter does not adopt ordinary owner events without a pending handoff.

The composition owner connects the existing Work watchdog event ingress to the
Automation event matcher before native reasoning. An explicit matching procedure
policy consumes that observation deterministically, including truthful missing
scope, ambiguous Role, busy and unknown refusals. It does not fall back to a
model. Current canonical routing must resolve one existing supervisor bootstrap
whose actual assignment already owns the exact required Resource context. A
zero-repository supervisor cannot infer authority from the Work item. Publisher
selection of an unsuitable supervisor yields a blocker; ordinary authorized
admission must establish the required scope separately.

The existing AutomationRun owns one optional typed procedure-custody facet:
exact assignment/worker/execution/workspace/fence, immutable input Artifact and
checksum, exact procedure reference/checkpoint, advance reservation count and
material observation digest. Its stable dedupe key follows the exact Definition
and original handoff nonce/target inputs, rather than watchdog window IDs. Each
advance is reserved atomically before the existing interpreter is called. The
existing keyed coordinator coalesces concurrent observations and joins shutdown;
one bounded startup recovery resumes retained active Runs. There is no new
ledger, periodic recovery poll, model turn or counter reset.

Unchanged observations cannot advance a waiting checkpoint. Material changes
(native activity, real lease expiry, canonical custody, delivery or ACK) may
resume only the same original inputs under fresh controls, Role/Profile scope,
fence and interpreter guards. Normal recipient preparation may change current
recipient custody, but it cannot silently replace the original recovery target
or nonce. Archived/expired handoffs are never recreated. Mutating resumed steps
always use their registered reconciliation owner; positive engine-validated
pre-effect diagnostics may be renewed while still busy without invoking a
mutation owner. Interrupted/uncertain attempts lose that reusable witness and
remain reconciliation-only. Completed Run status requires the exact completed
checkpoint and governed step Evidence, not an idle or successful native turn.

Existing Run payloads continue omitting the unset facet and retain state schema
1.4. The first populated facet writes additive state schema 1.5 in the same
namespace. The compatibility registry accepts both and registers the additive
transition; there is no SQL DDL, new state namespace, outcome backfill or startup
migration. Before publishing Automation 1.2 or writing a facet, all writers must
support the new contracts. An older binary is not promised to read a published
1.2 target or populated 1.5 state. Code-only deployment does not itself publish
an Automation or repin any Profile. The existing Automation editor exposes the
exact procedure reference, Role, finite limit, and Run checkpoint/counter/result;
manual Run now remains disabled for this event-only target.
