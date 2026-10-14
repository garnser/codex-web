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

Structured authoring, applicability previews, step traces, denied/blocked/expired
states and contextual help are explicitly tracked by [the linked UI issue](https://github.com/garnser/codex-web/issues/1428).
Discovery/admission, runtime execution and outcome-derived draft work remain separate
contracts; this schema does not implement those downstream delivery slices.

Discovery consumers use the [canonical discovery/admission contract](procedure-discovery.md).
It preserves this exact Skill identity and fails visibly for unsupported native
mutation enforcement. See the [operator guide](../operations/procedure-admission.md)
for outcomes and provenance inspection.
