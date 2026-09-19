# Canonical event bus and ingestion boundary

## Status

**Canonical event orchestration contract.** This boundary builds on the versioned
`CanonicalEventEnvelope` defined by the compatibility/versioning policy and is the
required ingress path for new event-driven autonomy.

## Flow

```text
Provider webhook / deterministic source fact
        ↓
Provider adapter normalization
        ↓
CanonicalEventEnvelope v1.0
        ↓
Durable idempotency + event persistence
        ↓
Deterministic type/predicate filtering
        ↓
Canonical subscribers
        ↓
The bounded-autonomy reasoning gate
```

Persistence and duplicate suppression happen before subscriber dispatch. A
repeated delivery therefore does not retrigger downstream orchestration, even
after a process restart.

## Event envelope

The bus transports `codex_web.compatibility.CanonicalEventEnvelope`. Event
schema compatibility remains owned by the canonical compatibility/versioning contract.
Unknown event-envelope versions fail before payload interpretation.

The first code-owned event classes are:

- `work.transition`
- `code.pull_request`
- `ci.pipeline`
- `deployment.status`
- `incident.status`
- `failure.observed`
- `task_source.event`
- `schedule.due`

Provider-specific actions and status values remain structured payload facts
instead of expanding the top-level taxonomy for every provider event name.

## Idempotency and correlation

Every ingress operation requires an idempotency key. The canonical event ID is
derived deterministically from the source and idempotency key. The durable
store records the key-to-event mapping atomically with the event.

A replay with the same key and equivalent semantic event returns the original
event and performs zero subscriber dispatch. Reusing a key for a different
event payload fails closed.

The first event in a causal chain defaults its correlation ID to its canonical
event ID. Callers may supply existing correlation and causation IDs when
normalizing downstream events.

GitLab webhook delivery IDs are used as durable idempotency cursors. GitLab
issue events are first normalized through the provider-neutral TaskSource
contract. Merge-request, pipeline, deployment, incident, and failure facts are
mapped to canonical event classes before any agent dispatch.

## Deterministic-first boundary

The event store, bus, ingestion service, type filters, and subscriber
predicates contain no model invocation. They operate on known structured facts
only. An idle bus has no polling loop and consumes no model tokens.

The bounded-autonomy controller owns the reasoning gate.
Consumers must not treat untrusted event content as authority; canonical
identity, policy, approval, ActionIntent, worker, and evidence boundaries still
apply.

Durable scheduler firings use `schedule.due` with the stable schedule
ID and scheduled occurrence in the payload. Scheduler retries reuse the same
event idempotency boundary rather than dispatching a second timer-specific path.

External side-effect completion and reconciliation remain owned by the durable
canonical ActionIntent/ActionProvider boundary. The event bus does not create a
second provider mutation path.

## Persistence and retention

Canonical events are stored in the shared SQLite state store under the
`canonical_events` namespace. The initial local deployment keeps a bounded
history and prunes idempotency entries together with pruned events. Future
distributed ownership/retention changes must preserve idempotency and replay
semantics and are coordinated with replicated ownership and replay/evaluation semantics.

## UI impact

This backend slice deliberately adds no shadow UI state. The orchestration
orchestration inspector/event timeline and the cross-cutting operator workspaces
must project canonical events from this boundary, including event ID, type,
source, correlation/causation, tenant/workspace scope, duplicate/replay state,
and downstream outcome.
