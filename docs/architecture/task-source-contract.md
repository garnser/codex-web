# Authoritative Task-Source Contract

## Status

**Milestone 2 architecture contract.** Canonical codex-web work-item behavior must not depend on GitLab, GitHub, Jira, Linear, or any other provider-specific task schema.

GitLab remains the current operational provider while it is migrated behind this contract. Defining the boundary first prevents a second work/execution model and gives future adapters the same deterministic integration surface.

## Core boundary

`codex_web.services.task_sources.TaskSource` is the provider-neutral protocol for authoritative external task systems.

Adapters expose:

- `source_type` — provider family, for example `gitlab` or `jira`;
- `source_instance` — configured provider instance/account/workspace identifier;
- immutable declared `capabilities`;
- provider-neutral discovery/read results as `TaskSourceSnapshot`;
- normalized external events as `TaskSourceEvent`;
- optional owner/state/comment/artifact write-back operations.

Provider-specific API response objects must not cross this boundary into canonical work-item reconciliation.

## Identity and provenance

`TaskSourceIdentity` carries the external provenance needed to identify and reconcile one authoritative item:

- source type;
- source instance;
- external item ID;
- external URL when available;
- source revision when available;
- source event cursor when available.

The identity object rejects missing source type, source instance, or external ID. Persisting this provenance on canonical work items is a follow-up Milestone 2 subtask; this contract defines the shape without prematurely changing persisted state.

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

Provider-specific mapping into canonical `WorkItemState` remains adapter/reconciliation work. Keeping `source_state` provider-native at this boundary makes the later mapping explicit and testable rather than silently pretending all providers share one state vocabulary.

`TaskSourceEvent` provides an identity, event type, optional timestamp, and optional normalized snapshot. Event normalization itself must not mutate canonical work-item state.

## Authority invariant

Exactly one external task source should be authoritative for mutable external task state for a canonical work item unless a future explicit federation policy says otherwise. Defining the protocol does not create a second source of truth: canonical codex-web lifecycle/execution state remains canonical inside codex-web, and the configured task source is the authoritative external projection/reconciliation peer.

## Migration sequence

The intended incremental migration is:

1. define and test this provider-neutral contract;
2. persist task-source identity/provenance on `WorkItemState`;
3. implement a GitLab `TaskSource` adapter;
4. move discovery/read/event normalization behind the adapter;
5. move supported write-back behind adapter capabilities;
6. make stale-event/conflict/split-brain handling provider-neutral;
7. add a shared adapter conformance suite and a non-GitLab/reference adapter.

Each step must preserve the existing canonical work-item, Executive, queue, sandbox, approval, and execution paths.

## Token efficiency

Task-source selection, capability checks, identity, event ordering, and provider mapping are deterministic application concerns. They must not invoke an LLM.
