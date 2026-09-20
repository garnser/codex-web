# Storage scaling path

codex-web keeps SQLite as the default local durable store and exposes the same transactional document-store contract through an optional PostgreSQL backend for shared deployments. Local installation remains broker-free by default; shared delivery and coordination are explicit deployment choices rather than hidden requirements.

A distributed deployment must preserve the same canonical-state guarantees. PostgreSQL, Redis, NATS, RabbitMQ, Kafka, or any other infrastructure component is never allowed to become an alternate source of business truth merely because it participates in storage, coordination, or event delivery.

## Current guarantees

Canonical repositories consume the `StateStore` contract: transactional
`get/put/update/update_many`, namespace discovery/deletion for migration, and
backend health. `SQLiteStateStore` remains the default and provides:

- transactional document updates using `BEGIN IMMEDIATE`
- WAL mode with a five-second busy timeout
- private database/WAL/SHM permissions
- an explicit schema version
- `PRAGMA quick_check` health reporting
- WAL checkpoint support
- SQLite-native transactional backups
- compatibility JSON mirrors in the repositories that still require rollback support

A database created by a newer codex-web schema is rejected rather than silently opened by an older binary.

`PostgresStateStore` implements the same contract for a shared control plane.
Namespace updates use transaction-scoped PostgreSQL advisory locks plus row
locking, and cross-namespace `update_many` acquires namespace locks in sorted
order before reading or mutating state. The PostgreSQL and Redis client
dependencies are optional and live in `requirements-distributed.txt`; selecting
those backends without the optional dependency fails visibly.

High-churn runtime state that historically had JSON files (thread settings,
active turns, work-item state, turn queues, bot bindings/connections and
auxiliary state) is StateStore-primary. JSON files remain compatibility
checkpoints for rollback, not an alternate authority. The project registry and
work-item event journal likewise use shared StateStore state in shared
deployments.

### Keyed operational records

High-churn mapping domains may opt into the keyed-record extension of the
`StateStore` contract:

- `record_get(namespace, key)`
- `record_apply(namespace, upserts=..., deletes=...)`
- `record_items(namespace)`
- `record_page(namespace, key_prefix=..., after=..., limit=...)`
- `record_replace(namespace, records)`

SQLite and PostgreSQL store these records as separate physical rows under a
reserved internal namespace. A logical `get(namespace)`, `documents()`,
backup, or migration still reconstructs the canonical mapping, so callers and
recovery tooling do not observe the physical sharding.

The first keyed mutation of an older monolithic mapping migrates that namespace
transactionally and idempotently. Thereafter, a single Work Item, active turn,
thread setting, or per-thread queue mutation updates only its keyed row rather
than deserializing and replacing the entire namespace document.

Compatibility JSON is intentionally **not** rewritten for every keyed mutation.
Doing so would reintroduce the multi-megabyte serialization/disk-write hot path
that keyed persistence removes. Bulk compatibility saves and explicit
`flush_legacy_mirror()` checkpoints refresh those files. Before rolling back
to a release that still reads the historical JSON files directly, operators
must take a compatibility checkpoint as part of the supported upgrade/rollback
procedure. Canonical StateStore data remains authoritative between checkpoints.

Both canonical keyed mutations and compatibility checkpoints expose separate
timing counters. State-store status reports `keyedMutationMetrics`; distributed
runtime diagnostics report per-namespace compatibility checkpoint metrics.
This keeps canonical write latency distinguishable from compatibility-export
cost and failure.

Bounded `record_page()` reads use the physical primary-key ordering and accept
a key prefix plus cursor. They are the read-side primitive for provider,
conversation, Project, or tenant scoped indexes; callers should encode those
scope dimensions into domain keys rather than loading the full collection.

### Bot routing read indexes

Bot binding routing maintains a process-local index over canonical binding state
for binding ID, thread ID, provider + Project, provider + external
conversation, and Project masters. The index records the canonical namespace
revision and rebuilds when that revision changes, including writes performed by
another control-plane process. Callers receive model copies rather than mutable
references into the cached index.

Reply and delivery targets do not require a second full in-memory registry.
Normal routing uses canonical keyed reads for binding, thread-scoped, and
provider + conversation + external message/thread aliases. A single typed
routing context memoizes active-turn and target reads across nested selection
helpers so one delivery decision cannot repeatedly reload the same state.

Legacy target records missing the external alias may use one bounded
provider/conversation-prefixed repair page. Repair scans are intentionally
bounded and counted separately from normal keyed reads; a successful repair
backfills the canonical alias so subsequent routing remains exact-key. Runtime
diagnostics expose binding-index status plus target keyed-read and compatibility
repair counters without serializing the target registries themselves.

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

## Deployment modes and fail-closed enablement

`CODEX_WEB_STATE_BACKEND` selects `sqlite` (default) or `postgresql`.
`CODEX_WEB_EVENT_TRANSPORT` selects `in-process` (default), `redis-streams`,
or an explicitly disabled transport. `CODEX_WEB_DEPLOYMENT_MODE` is `local`
by default.

A requested `replicated` deployment fails startup unless all three invariants
are true:

- the configured StateStore reports shared durability;
- CoordinationBackend is shared;
- EventTransport is durable and provides consumer-group semantics.

The check prevents installing PostgreSQL or Redis from silently implying
active/active safety. Singleton watchdog/recovery responsibilities and long-lived
bot provider sockets use renewable coordination leases. Per-thread queue drains
use renewable exclusive leases before an execution assignment exists; after
assignment, the worker plane's existing lease token and monotonic assignment
fence remain authoritative.

Scheduler instances may run on multiple replicas because schedule claims are
transactional and completion validates owner/revision; occurrence publication is
canonical-event-idempotent. Shared transport consumer groups are the only
subscriber dispatch path in replicated delivery mode, so the producer does not
also invoke local subscribers.

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

The local implementation is `InProcessEventTransport`, so a normal
single-instance codex-web deployment does not require an external broker.
`RedisStreamsEventTransport` is the first shared adapter. It preserves
canonical event IDs inside each message while Redis stream IDs remain delivery
metadata. The Redis client is injected at the adapter boundary and imported only
when that backend is selected.

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

`StateStoreCoordinationBackend` is the first coordination implementation. On
SQLite it provides local/single-host serialization; on PostgreSQL the same
transactional namespace becomes shared coordination. Lease takeover increments a
monotonic fencing token, and `fenced_update` verifies the live lease and
canonical mutation in one cross-namespace StateStore transaction. Stale owners
cannot release, renew, or perform a fenced protected mutation after takeover.

Redis may still be used for EventTransport independently. A future Redis-backed
CoordinationBackend can be added without changing transport or domain code.

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

## Operations and diagnostics

`GET /api/operations/distributed` exposes deployment mode and instance ID,
StateStore backend/health, EventTransport backend/health, durable outbox
pending/published/dead-letter counts, CoordinationBackend health, current
singleton ownership/fencing tokens, and transport-runtime recovery state.
Transport degradation is reported independently from canonical database health.

A broker outage therefore appears as outbox backlog/degraded transport while
committed canonical events remain in the database. Broker acknowledgement only
records delivery completion; it never establishes a work-item transition,
approval, ActionIntent outcome, or provider side effect.

## SQLite to shared-store migration

The migration tool is deterministic and re-runnable:

```text
python -m codex_web.state_migration migrate \
  --sqlite /path/state.sqlite3 \
  --postgres-dsn '<dsn>' \
  --backup /safe/path/pre-postgres.sqlite3 \
  --manifest /safe/path/state-migration.json
```

It takes a SQLite-native backup first, copies canonical StateStore documents,
compares per-namespace canonical JSON and whole-state SHA-256 checksums, and
writes a private manifest naming only namespaces it inserted. Re-running against
an unchanged target verifies equal namespaces without duplicating data.
Unexpected destination namespaces or mismatched content fail closed.

`verify` repeats count/content/checksum validation without mutation.
`rollback-target` removes only namespaces inserted by the recorded migration
and only when their values still match the source snapshot; the source SQLite
database is never mutated by migration, so deployment rollback remains a
configuration switch plus the preserved backup.

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

## Validation contract

The shared-runtime conformance suite exercises the same EventTransport semantics
against the in-process transport and a Redis Streams-compatible client boundary.
Two independently constructed coordination clients over the same durable store
exercise lease exclusion, expiry/takeover, monotonically increasing fencing and
stale-owner rejection. Durable-event tests force a transport failure immediately
after canonical commit and verify later outbox recovery, canonical identity
preservation, inbox deduplication and one subscriber execution. Concurrent
project and same-thread queue tests exercise shared-store delta merge behavior.

The final merge gate remains the repository-wide Python, JavaScript, Chromium
and Docker CI suite.

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
