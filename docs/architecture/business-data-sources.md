# BusinessDataSource ingestion, synchronization and reconciliation

## Boundary

BusinessDataSource is a provider-neutral, read/synchronize-only extension
contract for bringing minimum-sufficient business records into the canonical
BusinessEntity, ExternalRecordRef and CompanyFact model.

It is deliberately not an ActionProvider. The contract contains discovery,
read, event normalization and synchronization methods only. Any external write
must use the existing ActionProvider/ActionIntent boundary.

## Capability declaration

Adapters declare capabilities explicitly:

- discovery;
- paged discovery;
- incremental synchronization;
- read;
- events;
- historical read;
- tombstones.

The runtime does not infer unsupported behavior. A polling-only billing source
therefore does not need to emulate CRM webhooks or deltas.

## Configuration

A configured source records:

- tenant/workspace scope;
- source type and provider instance;
- provider/capacity identity;
- provider-native scope and object type;
- business-entity type and stable entity-key namespace;
- entity-name authority/priority;
- selected field-to-CompanyFact mappings;
- SecretReference identifier, never raw credentials;
- classification;
- page size;
- optional reconciliation interval;
- declared adapter capabilities;
- durable cursor/checkpoint, status and health metadata.

Adapters are resolved from BusinessDataSourceRegistry. Extension integrations
should register factories only while the extension is compatible and enabled.
A quarantined extension/source must not be resolved for synchronization.

The built-in `codex-work-items` adapter is a credentialless, read-only
projection over codex-web's canonical Work Item state. It emits one bounded
`project_delivery` snapshot per configured Project with the current open Work
Item count. It does not call GitLab directly or acquire provider-write
authority; upstream TaskSource synchronization remains responsible for keeping
the canonical Work Item projection current. Its reconciliation cursor is a
deterministic digest of the contributing Work Item states, so unchanged polls
are idempotent and changed counts produce new source revisions.

## Minimum-sufficient projection

BusinessDataSnapshot accepts bounded scalar fields only. Arbitrary nested
provider payloads are rejected before the canonical boundary.

For each snapshot the runtime:

1. commits a business_data.event canonical event with normalized selected data;
2. resolves one deterministic BusinessEntity from tenant + entity-key namespace
   + entity key;
3. creates or updates the provider-owned ExternalRecordRef;
4. applies authoritative field mappings into CompanyFacts;
5. records a durable projection receipt;
6. advances the provider cursor only after the complete page projected.

This ordering means a crash cannot silently move the reconciliation position
past unprojected records.

## Stable identity and multi-provider convergence

Provider record identity includes tenant/workspace, source system, provider
instance, object type and external ID. Separate CRM instances may therefore use
the same provider-native external ID without collision.

Business entity identity is derived from tenant/workspace + configured
entity-key namespace + provider-normalized entity key. CRM and billing sources
that represent the same customer can converge on one canonical entity while
retaining separate ExternalRecordRefs and CompanyFact provenance.

If two source configurations bind the same business key to incompatible entity
types, projection fails closed.

## Source authority and conflicts

Field mappings declare CompanyFact authority, priority, quality, confidence,
freshness and classification. Entity display-name mappings separately declare
authority and priority.

BusinessDataSource never hides provider disagreement. CompanyFact
resolution deterministically selects the highest-ranked fresh value while
retaining conflict fact IDs. The drift endpoint exposes provider record
position plus conflicting canonical facts.

Provider records remain visibly provider-owned; the selected CompanyFact remains
canonical codex-web state with source provenance.

## Events, deduplication and ordering

Provider events must first normalize into BusinessDataSourceEvent. They are then
committed as business_data.event canonical events before projection.

Canonical event idempotency plus projection receipts make repeated delivery
safe. For polled snapshots, a stable normalized snapshot fingerprint supplies
the idempotency key.

When a provider supplies source_sequence, a snapshot whose sequence is not newer
than the stored ExternalRecordRef is stale. Otherwise source_updated_at and
source_revision provide the supported ordering fallback.

A stale event is recorded as stale and cannot overwrite newer canonical source
position or CompanyFacts.

Webhook events intentionally do not advance the durable reconciliation cursor.
Opaque provider cursors cannot be safely ordered across out-of-order webhook
delivery. Scheduled/manual reconciliation owns cursor advancement.

## Tombstones and deletion

A provider tombstone revokes the ExternalRecordRef. Facts sourced from that
record remain historical but are excluded from current CompanyFact resolution.

Governed deletion/redaction/anonymization still goes through DataGovernance. A
provider tombstone is source synchronization state, not a bypass around
retention/legal-hold rules.

## Reconciliation and backfill

Manual synchronization and Scheduler-triggered reconciliation share the same
runtime.

Incremental-capable adapters use the durable source cursor. Other adapters use
bounded paged discovery. Full resync restarts discovery from the beginning but
remains idempotent through canonical event receipts.

Each run is bounded by page count and configured page size. The runtime never
replays an entire provider dataset into model context.

The Scheduler emits schedule.due canonical events; it does not invoke a model.
BusinessDataSource handles its own schedule event deterministically.

## Outages, rate limits and backoff

Before provider access, synchronization checks ProviderCapacity for the
provider/source pair.

Provider throttling/quota exceptions use the shared ProviderCapacity
classification/backoff path. Other provider failures record UNAVAILABLE with a
bounded retry time.

On failure:

- source status becomes degraded;
- last error/health time is visible;
- the last fully projected cursor remains unchanged;
- no records after the failed page are assumed synchronized.

After the capacity window expires, reconciliation resumes from that durable
position.

## Extension lifecycle and quarantine

An extension-backed adapter should be registered only when its package is
verified, compatible, configured and enabled. Capability grant and extension
installation remain separate platform concepts.

Unsafe or incompatible sources can be paused/quarantined. Scheduled
reconciliation is paused while quarantined. Re-enabling requires an available
compatible adapter; synchronization does not silently switch provider contract.

## Reference fixtures

Two intentionally different fixtures are maintained:

- ReferenceCRMDataSource: delta/event/tombstone capable;
- ReferenceBillingDataSource: polling/paged-read only.

The shared conformance tests prove the runtime is capability-driven rather than
CRM-specific.

## API and authority

Authenticated tenant users may inspect source state and drift. Configuration,
manual sync, event ingestion and lifecycle controls require administrator + MFA
or a service identity with business-data:admin.

That scope authorizes mutation of codex-web synchronization state only. It does
not grant authority to mutate the external provider.

## Failure semantics

- duplicate canonical event -> no duplicate projection;
- out-of-order provider event -> stale receipt, current state unchanged;
- tombstone -> source revoked, sourced fact unavailable as current truth;
- provider outage/rate limit -> degraded + durable cursor retained;
- unsupported capability -> visible validation failure;
- adapter version mismatch -> fail closed;
- source/entity type conflict -> fail closed;
- cross-tenant lookup -> not found;
- source paused/quarantined -> no synchronization.

These are expected safe states, not conditions for heuristic fallback.
