# Canonical Role Authority

Operational Roles are the M6 authorization contract for deciding whether an
authenticated actor may perform a capability at a requested authority level and
scope.

The core boundary is:

> **Identity answers who is acting. Operational Role authority answers what that
> actor may do. Execution roles answer which work lane an agent is performing.**

These are separate domains and must not be inferred from one another.

## Definition Registry ownership

Mutable operational Role data is stored in the canonical Definition Registry:

- kind: `authority-role-catalog`
- stable ID: `authority.roles.default`
- schema: `1.0`

The database owns Role/grant/binding/delegation values and their version history.
Code owns the schema, inheritance validator, evaluation algorithm, fail-closed
semantics, rank ordering and security invariants.

The bootstrap seed contains only one compatibility Role for the trusted
single-user local administrator. It is scoped to organization `local`,
workspace `default`. Bootstrap does not overwrite an existing canonical slot.

## Roles, bindings and delegations

An operational Role contains whole atomic grants. A grant names:

- capability and authority level: read, recommend, prepare, execute or approve;
- optional project and canonical Resource constraints;
- optional Resource type, risk and sensitivity constraints;
- optional development/test/staging/production environment classes;
- optional monetary and model/token ceilings;
- maximum autonomous-risk class;
- approval-count/approver-Role requirements.

Roles may inherit other Roles. Inheritance cycles, unknown parents, duplicate
Role IDs and duplicate grant IDs are invalid definitions.

Bindings attach Roles to canonical human/service identities or Teams and may
further restrict organization, workspace and projects.

Delegations temporarily attach a Role to a canonical identity. They preserve the
delegator, reason, tenant/project scope and mandatory expiry. Expired delegation
is ignored deterministically.

## Atomic least-authority evaluation

The evaluator does **not** merge partial authority from separate grants. One
complete grant must satisfy the entire request:

1. capability;
2. requested authority level;
3. project;
4. Resource IDs/types/risk/sensitivity;
5. environment class;
6. monetary limit;
7. input/output token and model-call limits;
8. autonomous-risk ceiling;
9. approval requirement.

For example, an execute grant with the correct repository but insufficient
production scope cannot borrow production scope from a separate read grant.
This prevents privilege amplification through permissive union of independent
constraints.

Role inheritance contributes additional complete grants; it does not loosen the
constraints of existing grants.

## Canonical resources

Resource-scoped evaluation resolves IDs through the M3 Resource Catalog.
Definitions may constrain stable Resource IDs or their canonical type/risk/
sensitivity metadata. The authority catalog does not duplicate resource facts.

A resource-restricted grant requires explicit target Resource IDs. Missing,
unknown or cross-tenant resources fail closed.

## Definition scope and project overrides

The authority service resolves the catalog through Definition Registry scope
precedence using the acting organization/workspace and requested project.
A project-specific published catalog can therefore override a less-specific
catalog using the same stable definition ID. The selected immutable
`DefinitionReference` is included in every decision.

M6 #108 extends administration/lifecycle UX and broader contract composition on
the same registry. It must not introduce another Role store or evaluator.

## Decisions and provenance

Each evaluation returns a structured `AuthorityDecision` containing:

- allow or deny;
- canonical source `canonical:role-authority`;
- actor and tenant;
- the exact evaluation request;
- exact Definition Registry reference when one was resolvable;
- matched Role/grant/delegation IDs;
- delegation expiry when authority is temporary;
- explicit deterministic reasons;
- evaluation timestamp.

A missing, corrupt, ambiguous, quarantined or incompatible authority definition
produces a deny decision rather than a fallback Role.

The ActionIntent enforcement integration is a separate #107 delivery slice. It
will persist this canonical decision at the external-action boundary and remove
caller-authored authority assertions as an authorization source.

## ActionIntent enforcement boundary

External/state-changing actions are authorized at the durable ActionIntent
boundary. The caller may still submit an `authority_decision` field for
backward-compatible audit payloads, but that field is **not** an authorization
source.

On intent creation the service:

1. resolves the canonical ActionProvider binding and ActionDefinition;
2. requires the ActionDefinition to declare one or more
   `required_authority` capabilities;
3. derives the requested authority level from the code-owned
   `required_authority_level`;
4. evaluates every required capability through the canonical Role authority
   service using the canonical project and Resource targets;
5. persists the aggregate canonical decision and exact Definition Registry
   provenance;
6. cancels the intent before provider execution if any required capability is
   denied or cannot be evaluated.

A caller-authored `ALLOW` snapshot cannot override this result.

Immediately before provider execution the service re-resolves the original
requesting identity's current membership/Team context, re-resolves the current
ActionDefinition, and performs the same canonical Role evaluation again. This
catches Role-definition quarantine/revision, revoked membership, removed Team
membership, expired delegation, resource changes, and other authority drift
between queueing and execution.

The original creation-time decision remains immutable audit provenance.
Execution-time authority is stored separately as `authority_recheck`.

If the requester identity, authority service, authority definition, or canonical
scope cannot be re-resolved, execution fails closed without calling the provider.

## Token efficiency

Role evaluation is deterministic application logic. It never invokes a model.
Prompt instructions, retrieved text, task descriptions, provider output or
model output cannot grant authority.
