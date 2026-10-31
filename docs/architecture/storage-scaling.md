# Storage scaling path

PostgreSQL keyed-record ranges and cursors use explicit `C` collation so encoded
storage keys keep byte ordering regardless of the database locale. Initialization
adds the matching `codex_state_documents_namespace_c_idx` B-tree expression index
to existing tables; record payloads, keys, primary-key uniqueness and schema
versions are unchanged. Building that index can temporarily block writers and
requires space for an additional namespace index. Deploy through the existing
drained upgrade procedure. Older binaries can read the same schema, but retain
the locale-dependent range bug; a downgrade is not a correctness rollback for
non-C databases. Distributed CI exercises both C and en_US.utf8 table collations,
including bounded index plans, replacement, deletion and revision compatibility.

PostgreSQL mutation callbacks may perform synchronous canonical read-only
lookups through their owning StateStore. Those reads reuse the callback's
transaction connection, including document, selected/batch record, inventory,
revision and status queries. Only the outer mutation commits, rolls back and
returns the lease. Thread-local custody prevents concurrent threads or a second
StateStore instance from borrowing that connection. Ordinary operations retain
their separate bounded-pool leases. Nested mutations are refused before another
checkout; callbacks cannot expand their write scope with independent side
effects. Count and page reads reuse initialized collections; their legacy
initialization must occur before the callback or in its outer mutation, rather
than opening an undeclared write from inside a read. Callback exceptions clear reader custody before returning the rolled
back connection. Pool size one is supported without increasing acquisition or
operational limits. This changes no persisted schema, namespace-lock ordering,
domain authority or operator surface.

PostgreSQL `documents()` exports use a transaction-local named server cursor
and batches of at most 64 rows. A single SELECT retains one consistent query
snapshot across FETCH operations; callbacks reuse their owning transaction and
pool lease, including visibility of their uncommitted changes. The exporter
never commits that transaction or publishes a partial mapping on interruption.
Each FETCH reapplies the remaining statement timeout, and row decoding and
final assembly check the existing cooperative operation budget. Cursor cleanup
and the existing connection context retain rollback/lease ownership.

This removes the extra complete encoded result buffered by an unnamed cursor
and `fetchall()` alongside the decoded export. It is a row-count bound, not a
hard byte quota: one large row and the final complete mapping still occupy
memory. Snapshot fields, legacy-over-keyed precedence, canonical checksums,
backup encryption/key manifests and audit/restore semantics are unchanged.
No persisted migration or new operator surface is introduced. Synthetic
allocation evidence does not establish the cause of a historical host OOM or
qualify the whole backup pipeline's memory use.

### Bounded semantic Recovery capture

Recovery capture uses a metadata inventory and indexed payload batches instead
of retaining the complete decoded catalog. PostgreSQL metadata and payload reads
share one owning repeatable-read transaction; SQLite uses one read transaction.
Physical rows are validated even when a legacy singleton shadows keyed rows.
Logical namespace/key ordering, legacy precedence, empty markers and the
`BackupSnapshot` 1.0 canonical plaintext/checksum remain unchanged. No plaintext
payload spool, second database or replacement storage-preservation format is
created.

The current capture contract admits at most 1,000,000 physical rows, 2 GiB of
encoded source plus namespace bytes, 4,096 logical namespaces, a 512 MiB estimated
metadata inventory, and 32 MiB per physical payload. Metadata admission reserves
512 bytes per row plus four times the encoded namespace bytes. Payload reads
are limited by both 512 rows and 32 MiB per batch. Small control namespaces have
a separate 32 MiB aggregate limit. The audit record catalog is streamed through
the existing audit integrity owner; all record schemas are checked before
partition filtering, selected records are ordered by their payload sequence,
and checkpoint append order is preserved. Large legacy singleton rows remain
subject to the physical-row limit.

Within that admitted row bound, capture uses native canonical JSON encoding of
one physical payload at a time, with cooperative deadline checks before encoding
and between output chunks of at most 64 KiB. The single-row working allocation
includes decoded values and escaped JSON text/bytes overlap. Namespace overrides
retain incremental encoding. Checksum traversal validates each chosen logical
row; a separate pass validates ignored/shadowed physical rows, so legacy
precedence cannot conceal malformed input. Audit-only reads still validate every
physical row. Both immutable-string observations remain before native encoding.
This avoids Python work proportional to every JSON punctuation token without
changing the global incremental encoder or snapshot bytes. Synthetic timing
measurements do not qualify the shared live capture/old-source verification
window; both operations still must complete within their existing deadline.
The existing cooperative storage budget optionally retains at most eight fixed
capture phase names with monotonic elapsed seconds. Cold CLI success/refusal
journals include that closed timing projection when capture began, so a timeout
can identify the last phase without payloads, credentials, exception prose or a
new telemetry store. Nested live-audit inventory remains attributed to live
audit. Phase observation does not extend the deadline or authorize a retry.

The final compatible plaintext buffer and existing crypto/envelope allocations
retain explicit stage admission against actual host/cgroup headroom with the
existing safety margin. Capture and live-audit validation release their owning
transactions sequentially, so the official PostgreSQL pool still supports size
one. No persisted schema, authority, deadline, production service memory limit
or Recovery inspector surface changes. Existing Recovery/Upgrade results remain
the operator surface for capacity refusals and backup/restore evidence.

A bound refusal means the capture contract cannot safely represent the current
catalog within those limits; operators must inspect the closed inventory and
stage sizing rather than increase a service limit or retry unchanged. Synthetic
allocation/restore tests qualify their fixtures only. Actual cold capture and
old-source isolated restore verification remain separate release requirements.

codex-web keeps SQLite as the default local durable store and exposes the same transactional document-store contract through an optional PostgreSQL backend for shared deployments. Local installation remains broker-free by default; shared delivery and coordination are explicit deployment choices rather than hidden requirements.

A distributed deployment must preserve the same canonical-state guarantees. PostgreSQL, Redis, NATS, RabbitMQ, Kafka, or any other infrastructure component is never allowed to become an alternate source of business truth merely because it participates in storage, coordination, or event delivery.

### PostgreSQL selected-record projection

Initialized PostgreSQL write owners can explicitly enable derived projection
version 1 for Governance and Organizational Memory `records` arrays. The two
rebuildable backend tables hold envelope metadata and complete original member
TEXT by original ordinal, with an identity index. Canonical documents remain the
only authority. Every projected lookup compares SHA-256 of the current complete
document with its projection header in the same statement snapshot. Existing
current actor, tenant, source, policy and domain decoding checks still run.
Duplicate identity order, first-global-before-scope decisions and raw numeric
values are retained. Unsupported identity types or oversized index keys, unknown/legacy envelope shapes,
missing or stale projections use the original document reader.

The optional array format version 2 extends this same derived projection to the
complete original `artifacts`, `evidence` and `verifications` members of
Artifact/Evidence state. The SQL table layout and its version-1 marker remain
unchanged. The optional `document-array-projection-format=2` and
`document-array-projection-v2:<namespace>=2` metadata plus a version-2 header
identify the new encoding. Identity keys use the SQL JSON encoding of
`[array_name, id]`; ordinal slots `original_ordinal * 4 + slot`, with slots 1, 2
and 3 for the three arrays, distinguish members with the same ID and position in
different arrays. Encoded key sizes and ordinal bounds must be checked before
index writes. This is an evolution
of derived SQL functions and row format, not a change to canonical domain models,
Definition schemas or API/worker protocols. Canonical document TEXT remains the
only truth; projected payloads do not confer authority or bypass current domain,
policy, actor, resource or lifecycle validation.

The same optional format also supports the fixed Secret `references` group,
using the previously unused ordinal slot 0 (`original_ordinal * 4`). Its separate
`document-reference-projection-v2:<namespace>=2` opt-in retains the same global
format-2 marker, table-layout marker 1 and header version 2. The two namespace
opt-in groups cannot be enabled over one another. Only complete reference
metadata is copied: credential material remains behind its existing backend and
`audit` members are never projected. Reference reads preserve first-global-ID
selection before tenant filtering; a foreign first duplicate or missing ID still
uses the original Secret owner to retain its denial/not-found behavior. Existing
custom-owner fallbacks, current ACL, service scope, lifecycle and rotation checks
are unchanged on every metadata/Use request.

Secret writers use the distinct `codex_prepare_document_references_v2` helper.
An older initialized binary may replace its three-array helper without replacing
this reference-aware helper; a new Secret write must never refresh a header hash
through a helper that cannot maintain slot 0. The reference-only digest lets an
audit append update the current source fingerprint without rewriting unchanged
reference rows. Old Secret writes bypass the new helper and the existing
hash-invalidation trigger erases every projected reference in that transaction.
Unknown reference opt-in markers retain canonical fallback without being
rewritten by initialization. This is optional backend metadata and SQL-helper
evolution; canonical Secret-state and API/worker schema versions do not change.

Version-2 reads retain the current whole-document SHA-256 comparison in the same
statement snapshot, envelope/schema validation and complete original member
TEXT. Artifact and Evidence point reads select the first matching scoped member;
Verification reads retain all matching scoped duplicates so the owner can refuse
ambiguity. Unsupported identities or shapes, oversized encoded keys, missing or
stale projections and unknown optional formats use the canonical reader. No
validated result is cached across provider I/O or subsequent admission checks.

New writers prepare projection changes under the existing namespace lock, with
the document row locked before derived metadata. A deferred foreign key permits
initial document insertion in the same transaction. Unchanged raw rows are
excluded before conflict locks; unchanged record-array SHA-256 skips row work
when a Memory retrieval audit is appended. Removed or changed records lose their
previous projected row versions in the same transaction. An old/direct document
update whose new SHA-256 does not match the prepared projection deletes all
current projected raw rows transactionally; canonical deletion cascades. This is
ordinary PostgreSQL MVCC erasure, not a claim to overwrite historical disk bytes.

Only `initialize=True` owners install the additive backend schema. Explicit
projection opt-in seeds its version/namespace metadata and records; a bare
initialized restore target retains exactly its original schema-version seed.
Initialization DDL is serialized per canonical SQL schema. Explicit opt-in
rechecks the current version marker even on an already initialized owner,
refusing unknown imported versions rather than overwriting them. Enabled-domain
empty targets remain nonseed targets and the existing strict restore refusal
is unchanged.
Cold/read-only `initialize=False` compositions and constructors inside an owning
callback never enable, rebuild or open another write transaction. Read callbacks
reuse their existing connection and see their own canonical uncommitted changes.
Older writers/readers can continue using the canonical documents: old
initializers tolerate the additional optional format marker, and version-1
readers decline version-2 headers. Their changed writes invalidate every derived
array in the namespace rather than leaving stale sensitive content. The
invalidation trigger is independent of array tags and removes all projected raw
rows in the same transaction; canonical deletion still cascades. New writers
must likewise remove superseded, redacted or deleted members across every
projected array. A format transition cannot reuse rows encoded for the other
format merely because an array digest is unchanged. These are ordinary MVCC
erasure guarantees, not physical overwriting of historical database pages.
The main document schema and domain schema versions do not change. Backup exports
contain authoritative documents, and derived rows can be rebuilt after restore.
The selected canonical Upgrade plan governs any recovery requirements; this
format-compatible derived projection does not introduce a routine backup or
restore-rehearsal gate. Retain the plan's drain and admission safeguards. Index
construction takes startup time and adds database storage. It is not a new
domain object or operator control, and introduces no UI flow.

Private PostgreSQL measurements use synthetic canonical model-shaped documents
and genuine authority, policy, source checks and durable retrieval-audit writes.
They qualify storage semantics and measured cost tradeoffs, not the actual live
Executive workload, service concurrency, complete preparation deadline or model
consultation. Those remain deployed acceptance checks under #1800 and #1733.

In one bounded synthetic PostgreSQL comparison (12,539 Governance records,
14.61 MB; 240 Memory records and 599 retrieval runs, 5.17 MB), complete Memory
search including its real private audit write took 3.49–4.60 s with the original
reader/writer and 1.02–1.10 s with the projection. Initial seeding took 4.13 s
outside search. A Governance append took 2.49 s versus 2.91 s; all 12,539 existing
projection row versions remained unchanged, and gross private WAL was about
305 KB in both cases. These measurements include four selected results, unlike
the observed production single-result query; they illustrate the startup,
write-time and storage tradeoff rather than guarantee a live deadline.

## Current guarantees

Turn queue and model-list mutable loads retain their concurrent-save baselines
only while the exact returned collection is alive. Weak references release
discarded read-only views without evicting a live editor's baseline. Saving
consumes that baseline once, before the existing store operation; ordinary
collections, copied views and collections from another repository retain their
existing save fallback. Cleanup and save both verify object identity, so reused
object IDs cannot borrow an unrelated baseline. Repository merge, validation,
transaction and rollback-mirror behavior are unchanged. This bounds retained
baselines by live loaded views, not by a fixed byte quota; callers can still
retain large live views. A demonstrated discarded-view leak does not establish
its share of a historical service OOM.

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
PostgreSQL full-collection reads, counts, replacements, and deletes use the
same encoded namespace lower/upper bounds against the primary key. They do not
apply a function to the indexed namespace column, and the encoded boundary
keeps adjacent and Unicode logical namespaces isolated without a schema or
index migration.

A first `record_page()` read may initialize or migrate its keyed collection.
Execution preflight storage performs that bounded initialization when composed,
before queue admission can consult its retained-request index inside an atomic
queue update. This prevents a fresh SQLite queue write from waiting on a second
writer for lazy preflight initialization; the retained-request duplicate check
still runs inside the queue transaction.

`record_get_many()` reads at most 1,000 explicitly named keys through one
connection and collection check. Empty input performs no storage I/O; reads
retain legacy-document compatibility without migrating or rewriting it.
`ModelMapRepository.get_many()` provides a request-local lazy mapping: only
visited selected records are validated and cached. Work Item list pagination
uses batches of at most 100 canonical records before applying its existing
scope and filter checks. It retains ordering, scan budgets, cursor revision
checks and missing/moved-record repair; no separate owner projection is
introduced. Compositions predating the optional batch dependency may use the
batch reader only when their getter is the exact bound
`ModelMapRepository.get`; custom or overridden getters retain their callback
behavior. This reduces database round trips for sparse filters while scan
CPU still depends on the number of candidates visited.

`record_mutate()` is the conditional keyed-write primitive. It acquires the
collection transaction lock, reads only the named records, and applies one
bounded upsert/delete delta. The updater may emit derived index keys, allowing
the canonical event store to change an outbox record and its readiness index in
the same transaction. PostgreSQL uses the namespace advisory lock plus row
locks; SQLite uses `BEGIN IMMEDIATE`. This primitive must not be used to bypass
cross-domain `update_many` transactions where a domain document and event must
commit atomically.

Canonical event storage uses a dedicated versioned keyed collection. Stable
event and idempotency aliases provide point reads, pending-outbox keys are
ordered by `not_before`, and inbox receipts are exact-key dedupe records. The
legacy monolithic event document is migrated once and retained only as an
explicitly refreshed rollback checkpoint. This keeps outbox dispatch cost
bounded as retained event history grows without changing event identity,
delivery ordering, or replay semantics.

### Cache revisions for keyed state

`namespace_revision()` is an opaque equality token for read projections, not a
wall-clock timestamp or an authority decision. Keyed collection revision reads
use a primary-key range scoped to the encoded namespace; they do not scan
unrelated logical collections. The maximum includes the collection marker and
record timestamps so reads remain compatible with older supported writers.

Every successful keyed upsert, deletion of an existing record, and replacement
updates the collection marker in the same transaction. Empty deltas and deletion
of missing records preserve the revision of an existing collection. Marker
revisions advance even when the system clock is frozen or moves backwards, and
removing an older or final record still invalidates readers in another process.
A pre-existing marker is initialized from retained record timestamps once on its
first mutation; the additive metadata preserves schema-version compatibility.
PostgreSQL mapping `update` and `update_many` keep their complete read and
transaction contracts, but write only added, changed and removed records.
Serialized snapshots precede the mutable callback, so nested in-place changes
and distinct JSON boolean/number types remain visible. Unchanged rows retain
their payload bytes, timestamps and physical identity. Successful mapping
updates still advance each collection revision, including unchanged mappings;
explicit `record_replace` retains its replacement semantics. These operations
still read the requested collections and are not bounded point reads. Failed
writes roll back both data and revision.

PostgreSQL collection writers acquire the namespace advisory lock before record
and marker locks, including point updates and replacement. This serializes
mutations within one logical collection and prevents lock-order inversions;
independent collections retain concurrency. SQLite uses its existing immediate
write transaction. These revisions support existing read-only caches without
introducing a second canonical store, TTL-based authority, or a Redis dependency.

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

### Slack channel discovery fan-out

Channel discovery snapshots local connection/binding state once per uncached
request and groups Slack connections by credential identity before contacting
the provider. Equivalent connections therefore share one
`conversations.list` discovery operation. Names returned by that list are
authoritative for the request and do not trigger redundant
`conversations.info` calls.

Only channels still lacking a usable name enter metadata fallback. Fallback is
deduplicated by channel and relevant credential group, concurrency-bounded, and
protected by short negative caches plus rate-limit cooldowns. A provider failure
or missing/inaccessible channel cannot create an immediate retry storm.
Explicit configuration invalidation clears those suppression caches.

Bot status exposes the last per-Project discovery counters, including cache hit,
credential groups, provider/list calls, metadata candidates/lookups, unresolved
channels, negative-cache hits, cooldown skips, rate-limit events, failures, and
duration. These counters describe provider discovery work rather than canonical
channel state.

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

PostgreSQL canonical state uses the official psycopg connection pool so keyed
operations reuse authenticated connections instead of reopening them for each
statement. Each checkout retains its own transaction; normal exits commit and
exceptional exits roll back before reuse. Pool resources are shared only across stores with identical DSN/TLS and pool
bounds. Closing a short-lived store releases its lease; application shutdown and
process exit close the shared pools after runtime owners stop. Its default bounds are one retained
connection, at most sixteen concurrent connections, and a five-second acquisition
wait; backend construction can supply tighter deployment bounds. Custom injected
connection factories retain the existing one-operation connection behavior.
Pool limits and counters are exposed through existing state-storage diagnostics.

### Recovery and archived thread projections

Recovery archives the predecessor in the canonical thread index before asking
its provider to archive it. Provider failure remains visible in recovery events;
subsequent active discovery cannot re-import a known archived row. Explicit
unarchive is the operation that clears local archival. Historical transcripts
and provider session identities are retained, rather than concatenated into a
new provider conversation.

Replacement rows carry their canonical Project ID. Naming retains Project and
archive metadata when provider metadata is partial. First-page discovery repairs
missing Project index metadata for the current canonical bot bindings using the
Project binding index, unique thread IDs and existing tenant/Project ownership
checks; it does not merge arbitrary conversations just because names match.

An explicit archive request may archive an unavailable assignment-bound
predecessor locally only when no active turn or current bot binding references
it and its canonical assignment is absent or terminal without a lease. A live
or pending claim and provider authority denials remain failures. The response
and event distinguish canonical archival from pending provider archival. These
projections do not release workspaces or alter worker authority.

### Exact Thread ownership reads

Single-Thread scope resolution uses the existing canonical AgentSession native-ID
alias range, across every provider/runtime and tenant. It reads at most 100 aliases
and one bounded batch of their exact primary records, preserving the existing
owner-dedup semantics for retained/rebound sessions even across runtimes. Every
alias must validate and agree on one owner; a later foreign owner is ambiguous.
The 100-alias inspection budget matches the existing native-index page size;
a continuation cursor remains unknown even if all inspected aliases agree.
It never loads unrelated sessions/events. A continuation cursor, malformed alias,
missing target, or primary/key mismatch fails closed before legacy fallback.
Writers maintain primary records and native aliases in the existing transaction;
this change adds no index, cache, store, schema or repair writer. As with other
canonical indexes, arbitrary out-of-band deletion of every alias is not detectable
without a full integrity scan; ordinary APIs cannot perform that partial write.
A complete empty range permits the existing real legacy ThreadIndex fallback,
which still requires a tenant-scoped explicit Project or an unambiguous path.
The existing one-time legacy collection migration is unchanged.

FastAPI Thread scope and context status move synchronous canonical storage reads
into awaited worker-thread boundaries with inherited ContextVars, preserving
identity-before-scope ordering and the current exact Project. Context and runner
responses revalidate that Project after storage awaits. These are read scheduling
boundaries; canonical admission, authority, lease/fence and supervision budgets
remain unchanged. Context eligibility remains an observation, not an admission.
Collection-list inspection remains a distinct snapshot operation; the exact lookup
does not make a full list an indexed point read.

## Current execution assignment selection

The canonical worker owner may read complete assignment records for one exact
execution, organization and workspace using the optional PostgreSQL/SQLite
selector. Every matching status and Project remains visible; the existing
newest `(created_at, id)` ordering and agreement across **all** repository scopes
and targets remain the authority-resolution rules. This is not a single-row or
active-assignment shortcut, and callers cannot supply an assignment ID hint.

Each call makes a fresh database observation. Selected records receive complete
domain validation; malformed selector headers remain fail-closed, and unsupported
legacy JSON conversion retains the full canonical reader. Well-typed unrelated
execution/tenant headers do not require validation of their unrelated record
bodies: this is a selected-record boundary, not a catalog-wide integrity scan.
Legacy-state migration and custom list/backend owners retain their original
reader paths. No identity, lease, fence, freshness, current authority, storage
format, write owner, cache or execution deadline changes.
Existing assignment inspection and operator UI use the same canonical records;
this internal read optimization introduces no new public fields or UI controls.

## Synchronous Definition read observations

Canonical Skill and procedure validators can share one fresh Definition document
observation within a synchronous validation call. The Definition owner supplies
an explicit read-only view to that call and closes it when the call returns or
raises. The view refuses use by another owner or thread and use after closure.
Trusted callers must keep that synchronous scope clear of content and provider
I/O and must not pass the view into a later validation call.

Only the document read is shared. Each lookup still decodes its selected records
and applies the existing schema, checksum, scope, lifecycle, effective-window,
Skill security and authority checks. Selected members are copied before
validation, so a returned payload cannot modify the observation. Exact-record
lookups retain validation of every matching duplicate, and slot resolution
retains all relevant revisions and its existing ambiguity rules. Observed
resolution bypasses the ordinary resolved-record cache.

The next validation call obtains a fresh observation. Unsupported legacy shapes
and custom validation owners retain their ordinary read paths. This introduces
no persisted format, SQL migration, permission, deadline, provider protocol or
operator setting; existing Definition and procedure inspectors remain the
operator surfaces.

Native credential-custody projection shares this observation across execution
profile resolution, sealed-Skill preparation and the selected procedure reads.
That scope is synchronous metadata validation only. Every live lease, identity,
resource, provider binding and SecretUse check still runs independently; the
result of a custody judgment is never cached. Custom preparation validators keep
their original complete path and signatures. No observation is carried into
managed content reads, provider I/O or a subsequent admission checkpoint.


## Deferred discovery context for current procedure admission

The canonical ActionIntent provider boundary retains its fresh intent, requester,
repository-scope and Role-authority checks before mediating a current durable
procedure step. When the complete canonical mediator admits that step, its result
does not consume a discovery `OperationContext`. That path therefore omits
construction of the unused context. Context construction otherwise resolves
Resource facts, assignment/runtime capabilities and the AgentProvider catalog;
the latter can load the complete ModelGateway document through linked-provider
discovery even though no model call is made.

This optimization requires captured original methods bound to their original
owners: the context builder, shared discovery context methods, provider hook,
complete procedure mediator and capability resolver. Custom owners, subclasses,
class-level overrides and methods rebound from another owner use the original
eager ordering. Rollback also builds and rewrites its context before mediation.
A false canonical mediation constructs the ordinary context and runs fallback
discovery once; mediation is never repeated to obtain that context.

No admission decision or read observation is cached. Every subsequent callback
performs fresh current checks. The independent protected-step validation still
resolves required provider capabilities when declared and still refuses revoked
provider grants, resources, identities, leases, custody or Role authority. This
change removes discarded discovery work; it does not remove those judgments or
change any deadline, canonical format, SQL object, API or worker protocol.

Existing procedure/action inspectors and the bounded verification-stage logs
remain the operator surfaces. No new public fields, UI controls or success
classification are introduced; a known completed action with failed verification
continues to require reconciliation.
