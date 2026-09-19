# Storage scaling path

codex-web currently uses SQLite as the primary durable state store. That is intentional for the single-instance control-plane deployment: it keeps installation and rollback simple while service boundaries are still being extracted.

A distributed deployment must preserve the same canonical-state guarantees. PostgreSQL, Redis, NATS, RabbitMQ, Kafka, or any other infrastructure component is never allowed to become an alternate source of business truth merely because it participates in storage, coordination, or event delivery.

## Current guarantees

`SQLiteStateStore` provides:

- transactional document updates using `BEGIN IMMEDIATE`
- WAL mode with a five-second busy timeout
- private database/WAL/SHM permissions
- an explicit schema version
- `PRAGMA quick_check` health reporting
- WAL checkpoint support
- SQLite-native transactional backups
- compatibility JSON mirrors in the repositories that still require rollback support

A database created by a newer codex-web schema is rejected rather than silently opened by an older binary.

## Scaling principles

The scaling architecture separates three responsibilities:

1. **Durable canonical state**
   - business/domain state, canonical events, ActionIntents, approvals, evidence, configuration, definitions, and other authoritative records
   - SQLite remains appropriate for a simple single-instance installation
   - a shared durable backend such as PostgreSQL is required before replicated control-plane instances can safely share state

2. **Event delivery**
   - wakeup, fan-out, consumer delivery, acknowledgement, retry, and dead-letter handling
   - owned by the provider-neutral `EventTransport` contract
   - may be in-process for a single-instance installation or broker-backed for a distributed deployment

3. **Distributed coordination**
   - leader election, leases, fencing, singleton responsibility ownership, provider socket ownership, and short-lived coordination state
   - owned by a separate `CoordinationBackend` contract
   - may be implemented by the same product used for transport, but remains a distinct architectural boundary

Keeping these responsibilities separate prevents a broker outage, transport replay, or lease-store implementation detail from redefining canonical application state.

## Durable outbox boundary

When a canonical transaction must cause asynchronous delivery, the application uses a durable outbox pattern:

```text
BEGIN TRANSACTION

update canonical state
append canonical event
append outbox record

COMMIT

        |
        v

outbox dispatcher
        |
        v
EventTransport.publish(...)
```

The database transaction establishes truth. Broker publication does not.

Required behavior:

- canonical state, canonical event identity, and the outbox record are committed atomically where they belong to the same operation
- the dispatcher publishes only committed outbox entries
- a crash after commit but before publication leaves a recoverable outbox entry
- retries may deliver the same event more than once
- consumers deduplicate using canonical event identity/idempotency keys
- broker delivery IDs, offsets, sequence numbers, or cursors remain infrastructure metadata rather than canonical object identity
- broker acknowledgement means only that a delivery was handled according to transport semantics; it does not independently prove a domain state transition or external side effect succeeded

Durable inbox/delivery-receipt state may be used when a consumer or provider requires explicit acknowledgement or replay tracking, but it follows the same rule: transport metadata cannot become a parallel source of business truth.

## EventTransport

`EventTransport` is the replaceable delivery boundary for asynchronous wakeup and fan-out.

The contract should support the smallest useful common semantics:

- publish a canonical event envelope or stable canonical event reference
- subscribe/consume
- acknowledge successful delivery
- negative acknowledgement/retry where supported
- bounded retry/backoff
- dead-letter/failure reporting
- consumer identity/group semantics where supported
- health and capability inspection

The local implementation should remain available:

```text
InProcessEventTransport
```

so a normal single-instance codex-web deployment does not require an external broker.

Distributed adapters can then be added without changing canonical event/domain code, for example:

```text
RedisStreamsEventTransport
NatsJetStreamEventTransport
RabbitMqEventTransport
KafkaEventTransport
```

These names are illustrative. The contract matters more than the product.

Kafka should not be introduced merely because the architecture supports it. Broker choice should follow actual deployment scale, durability, ordering, throughput, and operational requirements.

## CoordinationBackend

A message broker and a distributed coordination system solve related but different problems.

`CoordinationBackend` owns primitives such as:

- leader election
- distributed lease acquisition and renewal
- fencing tokens
- expiry and takeover
- singleton queue/recovery ownership
- scheduler ownership
- provider/runtime socket ownership
- worker/action responsibility ownership
- short-lived distributed wakeup/dedupe coordination where appropriate

Stale owners must be fenced from protected mutations after lease takeover.

A Redis-backed deployment may initially implement both `EventTransport` and `CoordinationBackend`, but codex-web must keep the contracts separate so Redis does not become an architectural dependency.

## Before a replicated deployment

Running multiple application instances requires more than replacing the database. Several runtime concerns are intentionally process-local today, including task ownership, queue-drain scheduling, provider sockets, active recovery work, and some transient deduplication.

The migration therefore requires all of the following:

1. **Shared durable state**
   - projects, thread settings and indexes
   - work-item state/events
   - canonical events and outbox/inbox state
   - Executive context, history, and knowledge
   - bot/integration connection and binding metadata
   - approvals, routing, ActionIntents, evidence, and other canonical records

2. **Shared coordination**
   - leases and fencing for singleton responsibilities
   - failover/takeover semantics
   - shared ownership for provider/runtime workers and scheduled jobs

3. **Shared event delivery where required**
   - broker-backed `EventTransport` for replicated wakeup/fan-out
   - durable outbox replay after process or broker failure
   - duplicate/reordered delivery tolerance
   - explicit transport health/backlog/dead-letter visibility

Do not enable multi-instance execution merely because PostgreSQL or a broker is available. The runtime supervisor and provider services must first use shared leases/fencing so only one valid owner controls each protected responsibility.

## Broker failure semantics

The broker is an optimization and delivery substrate, not a durability substitute.

Therefore:

- broker loss must not lose committed canonical work
- temporary publish failure leaves outbox entries pending
- transport reconnection replays pending committed entries
- duplicate transport delivery is safe
- broker replay cannot recreate canonical events with new identities
- consumers recover authoritative state from codex-web storage rather than trusting transient message history
- prolonged backlog/dead-letter state is observable and may suspend affected autonomous execution when policy requires it
- retries and recovery remain bounded so transport recovery cannot create a retry storm

## Deployment progression

A reasonable progression is:

1. **Local/simple**
   - SQLite
   - `InProcessEventTransport`
   - local/single-instance coordination

2. **Shared durable state**
   - PostgreSQL or equivalent shared canonical store
   - existing local transport while the deployment remains single-active
   - shared `CoordinationBackend` introduced before replicated ownership

3. **Replicated control plane**
   - shared canonical store
   - shared `CoordinationBackend`
   - broker-backed `EventTransport`, such as Redis Streams or NATS JetStream
   - outbox/inbox recovery and failover tests

4. **Higher-scale broker choices where justified**
   - RabbitMQ, Kafka, or other adapters only when their operational characteristics are actually required

This preserves the local-first installation model while allowing codex-web to scale without rewriting orchestration around a specific broker.

## Migration sequence

1. Keep the current repository/service interfaces as the application boundary.
2. Introduce PostgreSQL implementations behind those interfaces and run dual-read/verification tests.
3. Introduce durable outbox/inbox persistence for asynchronously delivered canonical events.
4. Define `EventTransport` and keep the in-process adapter as the default.
5. Define `CoordinationBackend` and move singleton ownership/leases/fencing behind it.
6. Add at least one broker-backed transport and one shared coordination implementation.
7. Add a migration command that snapshots SQLite, copies documents transactionally, verifies counts/checksums, and can be re-run safely.
8. Exercise broker outage/recovery, duplicate delivery, outbox replay, stale-owner fencing, and two-instance failover before declaring replicated mode supported.

The SQLite backup/status methods added here are prerequisites for migration tooling; they are not a claim that codex-web is already safe for active/active deployment.

## Required validation

Before replicated mode is considered supported, tests must demonstrate:

- crash after canonical commit but before transport publish recovers through the outbox
- repeated broker delivery does not duplicate canonical work or external side effects
- transport outage preserves committed work and exposes backlog/degraded health
- stale lease holders cannot mutate protected responsibilities after takeover
- broker replay preserves canonical correlation, causation, and event identity
- two instances transfer ownership deterministically without double execution
- transport and coordination backends can be replaced independently at their contracts
- a single-instance deployment continues to work without any external broker
