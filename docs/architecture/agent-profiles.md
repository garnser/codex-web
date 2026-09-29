# Agent Profiles

Agent Profiles are stable human-facing collaborator identities. They are not provider registrations, model IDs, runtime registrations, worker identities, or execution records.

## Identity boundary

A profile keeps one stable `profile_id` across configuration revisions and executions. Changing provider, runtime, model preference, worker, instructions, skills, access policy, or execution defaults creates or selects configuration/provenance without changing the logical agent identity.

Keep these concepts distinct:

- **Agent Profile** — reusable collaborator identity, pinned configuration and invocation policy.
- **Definition** — versioned instructions, skills, Roles and authority catalogs referenced by exact record/revision.
- **AgentProvider / AgentRuntime** — eligible execution infrastructure selected per execution.
- **ExecutionAssignment / Run** — one concrete invocation and its immutable provenance.
- **Execution Worker** — the trusted executor that claims a bounded assignment.

Provider/runtime availability never changes the identity or lifecycle of an Agent Profile.

## Canonical profile state

Agent Profiles are tenant/workspace scoped and revisioned. A revision contains:

- stable profile ID plus immutable revision/record ID;
- display name, avatar reference and description;
- lifecycle: `active`, `disabled`, or `archived`;
- owner and creator/updater provenance;
- Role and exact Role Definition reference;
- exact instructions Definition reference;
- exact Skill Definition references;
- model-class/provider preference policy;
- provider/runtime capability, allowlist, preference, residency, compliance, sandbox and network constraints;
- execution-profile and sandbox requirements;
- invocation access policy;
- canonical authority Role ceiling/reference;
- bounded concurrency/token/model-call/cost defaults;
- change reason and timestamps.

Secret values, provider credentials, worker capabilities and mutable provider/runtime health are not copied into the profile.

## Definition pinning

Instructions, Skills, Role definitions and authority catalogs are stored as exact `DefinitionReference` values. Creating or selecting a newer Definition does not reinterpret an existing profile revision or an already-started execution.

A later metadata-only Agent Profile revision retains its existing exact Definition references unless the operator explicitly changes them.

Executions pin the complete Agent Profile execution binding, including the exact profile revision and Definition references, so audit/replay does not depend on whatever definitions are current later.

## Access versus authority

Profile access answers only **who may invoke this collaborator**.

Supported access modes are tenant, owner-only and explicit identity/Role allowlists. Access is tenant scoped.

Invocation also evaluates canonical Role/authority independently. An Agent Profile cannot grant a Role, secret, ActionIntent permission, resource authority, approval bypass, worker capability or sandbox authority merely because prose or profile access says it may.

An authority Role configured on the profile is a ceiling/requirement: the actor must already possess that canonical authority.

Disabled or archived profiles cannot receive new work. Historical executions and revisions remain attributable.

## Lifecycle consumer impact

`usage` is a deterministic, read-only version `1.0` projection over latest Team
revisions, effective Automation Definitions in tenant-visible Projects, execution
assignments, and queued turns. It does not persist a second consumer registry.
Enabled Automations, active Teams, pending/claimed/running assignments and queued
invocations block disable/archive. Inactive consumers and historical assignments
remain visible but do not block. Project usage is derived from these canonical
consumers; a Project is not given an invented profile ownership field.

Profile visibility controls the projection. Team metadata the actor cannot view
is represented only by restricted counts; foreign-tenant consumers are excluded.
Queued messages, reply targets, and credentials are never returned. The view
shows at most 100 consumers, while counts and the lifecycle check include all
verified consumers, with a 5,000-consumer fail-closed bound.

Disable/archive re-read consumer impact on the server after normal mutation
authorization. Missing or failed impact projection returns a conflict rather
than permitting the transition. The UI offers a Usage view and impact preview,
requires a reason and explicit confirmation, and submits `expected_revision`.
A stale revision returns conflict. Restore remains available when impact is
unavailable so a retired identity can be recovered. No hard-delete or separate
retire operation is introduced; archive is the supported retirement operation.
Historical revisions and pinned execution provenance remain immutable.

The projection is a preflight observation, not a reservation over every consumer
store. Invocation still checks the current profile lifecycle before starting new
work. A concurrent new reference cannot authorize invoking an inactive profile.
Paused Automations and blocked preflight attempts still face that invocation gate
if retried; impact coverage is explicitly limited to the canonical sources above.

## Routing

For a new profile execution:

1. resolve current profile lifecycle/access and the requested exact profile revision;
2. evaluate canonical authority;
3. combine request constraints with profile constraints without broadening either allowlist;
4. discover eligible canonical providers/runtimes;
5. apply deterministic capability/health/compliance/cost routing and allowed fallback;
6. pin selected provider/runtime and their revisions into the profile execution binding;
7. create the canonical ExecutionAssignment with that binding;
8. when a worker claims the assignment, pin the worker ID into the execution binding and assignment lease/provenance.

Fallback may choose another eligible provider/runtime only when both request and profile policy allow it. It cannot change the Agent Profile or broaden authority.

If no runtime satisfies the profile/request contract, routing returns a structured `agent_runtime_unavailable` blocker with rejected reasons and a remediation route. It never silently selects an unauthorized runtime.

## Execution provenance

The canonical assignment records:

- Agent Profile ID/revision/record ID;
- pinned instructions/Skill/Role/authority Definition references;
- selected provider/runtime IDs and provider/runtime capability revisions;
- execution profile and sandbox requirement;
- model provider/model where applicable;
- assigned worker ID once claimed;
- normal assignment subject, Project, resources, lease/fence and execution controls.

Re-binding an existing execution ID to another profile revision, runtime, execution profile, repository target or control set fails closed. Post-claim worker enrichment is permitted only for the same pinned profile/runtime binding.

## API projections

Agent Profile APIs are tenant scoped:

```text
GET    /api/agent-profiles
GET    /api/agent-profiles/picker
POST   /api/agent-profiles
GET    /api/agent-profiles/{profile_id}
PATCH  /api/agent-profiles/{profile_id}
POST   /api/agent-profiles/{profile_id}/disable
POST   /api/agent-profiles/{profile_id}/archive
POST   /api/agent-profiles/{profile_id}/restore
GET    /api/agent-profiles/{profile_id}/access
GET    /api/agent-profiles/{profile_id}/revisions
GET    /api/agent-profiles/{profile_id}/audit
GET    /api/agent-profiles/{profile_id}/executions
GET    /api/agent-profiles/{profile_id}/usage
```

The picker exposes collaborator identity and policy hints; it does not present a provider/runtime as the agent identity.

The bounded `executions` projection is backed by canonical ExecutionAssignments and separates workload from provider/runtime availability. The richer Work-Item-centric immutable Run Timeline is a separate execution-history domain rather than shadow state on Agent Profiles.

The `audit` projection is derived from immutable profile revisions and exposes updater, lifecycle, reason and revision provenance.

The execution projection accepts optional `project_id`. It filters assignments
before computing totals, active counts and the bounded result window; another
Project's newer assignments cannot hide the selected Project's history. Omitting
the parameter retains the existing authorized workspace projection.

## UI contract

Product UI should display the Agent Profile as the collaborator. Provider/runtime/worker information is execution provenance and availability detail, not the profile's identity.

Profile detail surfaces may combine profile identity, access, workload and runtime availability, but each must retain its canonical source and vocabulary rather than reconstructing shadow profile state in the browser.

Agent Profiles and Teams are labeled as reusable workspace identities,
not Project-owned records. Their lazy workload and invocation-access panels use
the selected Project. Switching Projects removes the previous panels, aborts
reads and rejects late results; no selected Project means no context request.
Editing a shared identity retains its canonical workspace authority boundary.
