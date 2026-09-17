# Authoritative Task-Source Contract

## Status

**Milestone 2 architecture contract.** Canonical codex-web work-item behavior must not depend on GitLab, GitHub, Jira, Linear, or any other provider-specific task schema.

GitHub Issues, Milestones, Projects, and linked Pull Requests are the delivery source of truth. This document defines the durable provider-neutral boundary, invariants, and migration architecture only.

GitLab remains the current operational provider while it is migrated behind this contract. Defining the boundary first prevents a second work/execution model and gives future adapters the same deterministic integration surface.

## Core boundary

`codex_web.services.task_sources.TaskSource` is the provider-neutral protocol for authoritative external task systems.

Adapters expose:

- `source_type` — provider family, for example `gitlab` or `jira`;
- `source_instance` — configured provider instance/account/workspace identifier;
- immutable declared `capabilities`;
- provider-neutral discovery/read results as `TaskSourceSnapshot`;
- normalized external events as `TaskSourceEvent`;
- a mandatory deterministic `project(...)` mapping from normalized provider facts to `TaskSourceCanonicalProjection`;
- optional owner/state/comment/artifact write-back operations.

Provider-specific API response objects must not cross this boundary into canonical work-item reconciliation.

## Identity and provenance

`TaskSourceIdentity` is a frozen canonical model in `codex_web.models` and carries the external provenance needed to identify and reconcile one authoritative item:

- source type;
- source instance;
- external item ID;
- external URL when available;
- source revision when available;
- source event cursor when available.

`WorkItemState.source_identity` persists that object separately from `WorkItemState.ref`. The canonical work-item ref therefore does not need to be rewritten when a provider adapter changes or when an external system uses a different ID vocabulary.

Existing persisted work items remain compatible because `source_identity` is optional during migration. The current GitLab discovery/sync path backfills provenance for discovered items using the GitLab API base as `source_instance`, the routable full external reference as external identity, and the provider URL/revision when present.

Webhook/event provenance moves behind provider adapters as their event paths are migrated; provider-specific event logic must not be duplicated merely to populate canonical provenance.

## Capabilities

Adapters declare support explicitly using `TaskSourceCapability`:

- `discovery`;
- `read`;
- `events`;
- `owner_write`;
- `state_write`;
- `comments`;
- `artifact_links`.

Unsupported behavior must fail deterministically with `UnsupportedTaskSourceCapability`. Core code must not assume that every task provider has GitLab-equivalent labels, assignees, comments, artifact links, or write semantics.

## Normalized facts

`TaskSourceSnapshot` intentionally contains a small provider-neutral fact set:

- identity/provenance;
- title;
- source-native state string;
- zero or more owners;
- zero or more labels/tags;
- zero or more artifact links.

Provider-specific mapping into canonical work-item semantics is explicit. Keeping `source_state` provider-native at the adapter boundary avoids pretending all providers share one state vocabulary.

`TaskSourceEvent` provides an identity, event type, optional timestamp, and optional normalized snapshot. Event normalization itself must not mutate canonical work-item state.

## Canonical projection and reconciliation

Every adapter implements `TaskSource.project(snapshot, current_stage=...)` and returns `TaskSourceCanonicalProjection`. The mapping is synchronous and deterministic because translating already-normalized provider facts is application logic, not model reasoning. `current_stage` may be used when a provider state is less specific than codex-web's lifecycle, but projection itself must not mutate canonical state.

The projection contains only canonical fields needed at the boundary, currently stage and owner, plus the native source-state value for diagnostics. The projection type lives with the `TaskSource` contract so adapters do not depend on the reconciliation engine merely to describe their canonical mapping output.

`TaskSourceReconciliationPolicy` evaluates a normalized event without mutating state and returns exactly one deterministic outcome:

- `apply` — the event belongs to the configured authoritative item and is neither known duplicate nor stale;
- `duplicate` — the normalized event idempotency key has already been applied;
- `stale` — the provider timestamp predates the last applied authoritative-source event;
- `conflict` — the incoming event belongs to a different authoritative source item.

Event idempotency prefers a provider event cursor when one is available. Otherwise codex-web hashes normalized provider-neutral event facts. Raw provider payload bytes are not part of core idempotency semantics.

Provider revisions and event cursors are intentionally treated as opaque strings unless an adapter provides deterministic ordering semantics. Core reconciliation must not guess ordering from provider-specific revision formats. When an event has no usable provider timestamp, the core does not invent staleness; adapters or later conflict policy may provide stronger evidence.

`task_source_projection_drift` reports provider-neutral stage/owner differences as structured findings. Reporting drift is separate from deciding whether to apply it because canonical state can legitimately preserve a handoff or another internal invariant. Reconciliation policy therefore remains deterministic without treating every difference as an automatic overwrite.

## Shared adapter conformance gate

`TaskSourceConformanceSuite` is the provider-neutral validation gate reused by adapter tests. It validates:

- that an adapter implements the complete `TaskSource` protocol, including deterministic projection;
- non-empty source type and source instance plus a valid capability declaration;
- that normalized identities belong to the adapter's source type and instance;
- that discovery/read outputs are `TaskSourceSnapshot` objects;
- that event outputs are `TaskSourceEvent` objects and event/snapshot identities identify the same item;
- that canonical mapping outputs are `TaskSourceCanonicalProjection` objects, retain source identity, and use a canonical work-item stage.

Each concrete provider adapter must run representative discovery/read/event/projection outputs through the same suite. Provider-specific tests may add stronger assertions, but they must not replace the shared conformance gate. This makes portability testable instead of relying on convention.

## Authority invariant

Exactly one external task source should be authoritative for mutable external task state for a canonical work item unless a future explicit federation policy says otherwise. Defining the protocol does not create a second source of truth: canonical codex-web lifecycle/execution state remains canonical inside codex-web, and the configured task source is the authoritative external projection/reconciliation peer.

Source identity matching uses provider family, source instance, and external item ID. A different authoritative identity is a conflict rather than an implicit source switch. Source changes must happen through explicit project/workspace configuration and migration policy.

## Project/workspace authoritative-source configuration

The existing canonical `Project` object carries one optional `authoritative_task_source` binding. The binding is represented by `TaskSourceConfiguration` and contains only provider-neutral routing identity:

- `source_type` — the adapter/provider family;
- `source_instance` — the configured external system instance or workspace identifier;
- `scope` — the provider-native discovery/read scope interpreted by that adapter.

Because the field is singular, a project can represent exactly one authoritative mutable external task source or none. A list of simultaneous mutable authorities is deliberately not part of the model; federation would require an explicit future policy rather than emerging accidentally from configuration.

Provider credentials, tokens, webhook secrets, provider-specific routing options, and transport configuration do not belong in `TaskSourceConfiguration`. Those remain integration concerns. The project binding says **which configured task source is authoritative**, not how to authenticate to it.

The canonical project API exposes:

- `PUT /api/projects/{project_id}/task-source` — set or replace the authoritative binding;
- `DELETE /api/projects/{project_id}/task-source` — clear the binding.

Project creation may also include the same optional binding. Existing stored projects remain valid because the field is optional.

Replacing or clearing a project binding does not silently rewrite `WorkItemState.source_identity`. Existing work retains its provenance. If that provenance does not match the configured authority, reconciliation must surface the mismatch/conflict until an explicit migration or reassignment path resolves it. This prevents a configuration edit from silently transferring authority over existing work.

## Migration architecture

The provider-neutral migration is intentionally incremental:

1. establish the task-source protocol, capability declarations, normalized snapshot/event types, and canonical source identity;
2. persist source provenance independently from canonical work-item identity;
3. establish provider-neutral idempotency, stale-event, conflict, and drift diagnostics;
4. define per-project/workspace authoritative-source configuration and exactly-one-source enforcement;
5. require deterministic provider-to-canonical projection through the shared adapter contract;
6. implement concrete provider adapters and move discovery/read/event normalization and supported write-back behind declared capabilities;
7. run the shared conformance suite against every concrete provider and the provider-neutral reference adapter;
8. expose provenance, synchronization/conflict diagnostics, and permitted reconciliation actions through canonical APIs/UI.

Each step must preserve the existing canonical work-item, Executive, queue, sandbox, approval, and execution paths.

## Token efficiency

Task-source selection, capability checks, identity, event ordering, provider mapping, idempotency, stale-event detection, and drift/conflict checks are deterministic application concerns. They must not invoke an LLM.
