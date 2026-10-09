# Authoritative Task-Source Contract

## Status

**Canonical task-source architecture contract.** Canonical codex-web work-item behavior must not depend on GitLab, GitHub, Jira, Linear, or any other provider-specific task schema.

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

- `create` — optional additive capability for creating one new authoritative task;
- `discovery`;
- `read`;
- `events`;
- `owner_write`;
- `state_write`;
- `comments`;
- `artifact_links`.

Unsupported behavior must fail deterministically with `UnsupportedTaskSourceCapability`. Core code must not assume that every task provider has GitLab-equivalent labels, assignees, comments, artifact links, or write semantics.

## Authoritative task creation

Task creation is an additive capability rather than a mandatory method on the TaskSource 1.0 base protocol. Adapters that advertise `TaskSourceCapability.CREATE` must also implement `TaskSourceCreateCapable.create(...)`; the shared conformance suite fails closed when capability declaration and implementation disagree. Existing 1.0 adapters that do not advertise CREATE remain protocol-compatible.

`TaskSourceCreateRequest` is deliberately provider-neutral and bounded to creation facts needed across providers: title, optional body, owners, and labels/tags. The adapter chooses provider-native transport and returns a normalized `TaskSourceSnapshot` containing the authoritative external identity. Core code must never invent an external/provider identifier.

Creation is resolved from the canonical project's singular `TaskSourceConfiguration` before any Work Item exists. `TaskSourceRegistry.resolve_project(...)` validates tenant-selected binding, source type, source instance, and contract compatibility. The configured `scope` is the adapter-owned writable container; providers must fail visibly if the configured scope cannot accept creation.

`WorkItemService.create_authoritative(...)` is an internal execution seam: it resolves the configured source, requires CREATE, performs the provider creation, and only then projects the returned snapshot into canonical Work Item state. Missing authority configuration, unsupported creation, source-resolution failure, or provider failure therefore cannot create a local shadow Work Item.

Creating an external task is a privileged side effect. Product flows such as Goal decomposition must not expose `create_authoritative(...)` as a direct user mutation or call provider transports themselves. The code-owned `task-source/authoritative` ActionProvider now supplies that bridge: `task-source.create` is prepared and persisted as an ActionIntent before execution delegates to this seam. The action accepts only provider-neutral creation facts, returns the real projected Work Item ref/source identity, and intentionally does not claim cross-provider idempotency or rollback semantics. Provider identity/receipt/evidence therefore remains attributable to the durable action.

GitLab READ accepts both repository-qualified issue identities (`project#iid`)
and merge-request identities (`project!iid`). They use distinct native endpoints
even when their IIDs match. MR reads preserve the requested provenance and
reject mismatched provider identities. Discovery and task mutation/writeback
remain issue-specific; MR READ does not grant issue mutation or bypass canonical
actor, tenant, repository, or credential authorization.

## Normalized facts

`TaskSourceSnapshot` intentionally contains a small provider-neutral fact set:

- identity/provenance;
- title;
- provider-neutral body text, including issue scope and acceptance criteria;
- source-native state string;
- zero or more owners;
- zero or more labels/tags;
- zero or more artifact links.

An adapter read may additionally return provider-verified artifact relations.
Each relation retains the provider instance, repository-qualified artifact
identity, exact head revision, provider revision, native state, and URL. `None`
means the read did not query relations; an empty collection is fresh evidence
that no supported relation exists. GitLab issue reads obtain merge-request
relations from the native related-MR endpoint and accept only same-project
source/target MRs with an exact commit head. Discovery does not add one provider
request per issue merely to populate these relations.

GitLab relation reads enumerate numeric pages on the same credential-bound
endpoint, with limits of ten pages, 1,000 items, 512 KiB per page, 4 MiB total,
and sixty seconds overall. Missing pagination headers require an empty page
before enumeration is complete. Invalid continuations, repeated items,
inconsistent totals, malformed pages, provider errors, and exhausted limits
fail the read visibly before projection. Partial collections must never become
fresh absence or remove a previously verified MR/reviewer lane. This leaves
the existing snapshot contract unchanged: a returned relation collection is
complete; a failed read returns no snapshot to reconcile.

Assignment-bound Work Item reads expose this normalized body through the
credential-free TaskSource read boundary. When canonical `next_action` is
absent, the broker's `assigned_scope` points the worker to the authoritative
title/body and source identity instead of requiring direct provider access.
Task text remains untrusted data and cannot grant authority or change execution
policy.

GitLab reads route provider-qualified issue (`project#iid`) and merge-request
(`project!iid`) identities to their respective native endpoints. Merge-request
normalization is read-only: issue write-back continues to require an issue
identity, and neither identity form broadens the canonical tenant, Project,
actor, secret-binding, or assignment repository checks. Missing or malformed
identities fail with a typed, sanitized error rather than provider-path guessing.

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

Provider-verified artifact relations are persisted separately from legacy branch
or prose hints. A fresh open relation may preserve a truthful nonterminal
validation handoff across later issue-only discovery snapshots. A fresh empty,
closed, cross-project, malformed, or older conflicting relation cannot create or
retain that proof. Merge, approval, successful acceptance, and terminal closure
remain independent facts and are never inferred from the relationship alone.

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
- `scope` — the provider-native discovery/read scope interpreted by that adapter;
- optional `credential_secret_id` — a stable canonical SecretReference, never credential material;
- optional typed `provider_settings` — the supported adapter schema (Jira account metadata or ServiceNow table/state mapping), validated against the source type.

Because the field is singular, a project can represent exactly one authoritative mutable external task source or none. A list of simultaneous mutable authorities is deliberately not part of the model; federation would require an explicit future policy rather than emerging accidentally from configuration.

Raw provider credentials, tokens and webhook secrets do not belong in
`TaskSourceConfiguration`. A credential reference does not grant use permission:
the runtime still resolves it through SecretBroker under its canonical actor.
Typed non-secret adapter settings are schema-owned; arbitrary executable or
credential-bearing settings are not accepted. Transport implementations remain
integration concerns. The singular Project binding identifies the authoritative
source and references the configuration needed by its supported adapter.

The canonical project API exposes:

- `PUT /api/projects/{project_id}/task-source` — set or replace the authoritative binding;
- `DELETE /api/projects/{project_id}/task-source` — clear the binding.

Project creation may also include the same optional binding. Existing stored projects remain valid because the field is optional.

Replacing or clearing a project binding does not silently rewrite `WorkItemState.source_identity`. Existing work retains its provenance. If that provenance does not match the configured authority, reconciliation must surface the mismatch/conflict until an explicit migration or reassignment path resolves it. This prevents a configuration edit from silently transferring authority over existing work.

The Project Work Items editor exposes these same set/replace/clear operations,
typed provider settings and canonical credential selectors. Its provenance text
distinguishes the explicit Project binding from mere adapter availability. Adapter
metadata may come from that Project or the shared catalog, never a sibling
Project. Clearing requires confirmation and does not delete provider tasks, revoke
shared credentials or invent a replacement authority. The editor does not offer
an inherited-source reset when no such canonical inheritance primitive exists.

The TaskSource catalog adds `configuration_schema`, generated from the canonical
Pydantic configuration model. ServiceNow field-name controls and all supported
state mappings derive from that schema and preserve existing values. Schema and
protocol field names remain code-owned; mutable mapping values stay in the
canonical Project binding. A missing schema cannot silently erase mappings.

Draft field values remain ephemeral and use the shared dirty-editor/inline-error
controls. Failed writes preserve metadata edits. Save, clear, refresh and resync
responses are fenced to the originating Project visit, including A→B→A changes;
Project switches clear prior source fields. Server admin and tenant checks remain
authoritative. Readiness blockers for missing authority open this editor, while
credential/configuration blockers open Project Secrets or Configuration. These
navigation and state checks are deterministic and add no model calls or direct
provider mutation path.

## Migration architecture

The provider-neutral migration is intentionally incremental:

1. establish the task-source protocol, capability declarations, normalized snapshot/event types, and canonical source identity;
2. persist source provenance independently from canonical work-item identity;
3. establish provider-neutral idempotency, stale-event, conflict, and drift diagnostics;
4. define per-project/workspace authoritative-source configuration and exactly-one-source enforcement;
5. require deterministic provider-to-canonical projection through the shared adapter contract;
6. implement concrete provider adapters and move discovery/read/event normalization and supported write-back behind declared capabilities;
7. run the shared conformance suite against every concrete provider and the provider-neutral reference adapter;
8. add provider-neutral authoritative creation only as an explicit additive capability, resolving the configured project source before a Work Item exists;
9. expose provenance, synchronization/conflict diagnostics, and permitted reconciliation actions through canonical APIs/UI.

Each step must preserve the existing canonical work-item, Executive, queue, sandbox, approval, and execution paths.

## Token efficiency

Task-source selection, capability checks, identity, event ordering, provider mapping, idempotency, stale-event detection, and drift/conflict checks are deterministic application concerns. They must not invoke an LLM.
