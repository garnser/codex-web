# Operator runbooks

These runbooks assume canonical UI/API state is available. Prefer the owning
domain state and Evidence over model prose or copied provider screenshots.

## Startup and shutdown

### Startup

1. verify database/shared-store connectivity;
2. verify configured key/secret backends;
3. start the web/control plane;
4. check `/api/livez` and deeper health/observability;
5. confirm scheduler/event-transport/coordination ownership;
6. verify worker pools/extensions/providers are compatible;
7. leave autonomy paused/bounded until recovery/audit/capacity blockers are
   understood.

### Shutdown

1. pause/drain new autonomous/worker execution where required;
2. allow/contain active external actions;
3. preserve ActionIntent reconciliation state;
4. stop schedulers/workers before destructive infrastructure work;
5. shut down the control plane and backing services.

Never terminate a potentially non-idempotent provider action and blindly replay
it after restart.

## Health, logs and observability

Inspect, in order:

- liveness/readiness;
- structured logs by correlation/causation/execution/ActionIntent;
- operations/observability health;
- canonical Attention and Incident state;
- provider capacity/circuit state;
- worker leases/fencing;
- audit integrity when required by production policy.

See [Observability](../architecture/observability.md).

## Release promotion and rollback

1. identify the immutable Release/artifact digest/source revision;
2. verify SBOM/provenance/signature and required Evidence;
3. verify approvals and target environment;
4. execute the canonical promotion/ActionIntent;
5. observe rollout/canary state;
6. require post-promotion verification Evidence;
7. rollback only to the declared compatible rollback target.

Do not deploy a rebuilt “equivalent” artifact under the same release identity.

See [Releases](../architecture/releases.md).

## Upgrade, migration and version skew

Use [Upgrade and rollback procedure](upgrade-and-rollback.md).

Key operator rules:

- run preflight before maintenance/drain;
- verify source/target version-skew matrix;
- capture required pre-upgrade backup;
- execute expand → rollout → verify → contract phases;
- obtain exact approval before irreversible steps;
- resume only idempotent failed migration steps automatically;
- do not claim rollback after an irreversible/incompatible boundary.

## Backup, restore and recovery drill

1. verify RecoveryPolicy and backup destination health;
2. create/confirm encrypted backup with correct key version;
3. verify key manifest and audit root/checkpoint;
4. run isolated restore verification;
5. confirm checksum/schema/key/audit continuity;
6. confirm measured RPO/RTO posture;
7. publish/retain restore Evidence.

An isolated restore intentionally pauses autonomy/schedules and puts
nonterminal ActionIntents into reconciliation. Do not re-enable side effects
until reconciliation and compatibility checks are complete.

See [Recovery continuity](../architecture/recovery-continuity.md).

## Incident response

For a production Incident:

1. confirm severity, commander/owner and affected Resources;
2. record timeline/source detections;
3. contain through canonical ActionIntents;
4. preserve provider receipts and Evidence;
5. recover with bounded capacity/retry behavior;
6. require restoration verification;
7. resolve only when Incident policy allows;
8. record postmortem/follow-up actions.

SEV1 restoration should use independent verification where policy requires it.

See [Incident domain](../architecture/incidents.md).

## Capacity and degraded dependencies

When saturation/throttling occurs:

- inspect global/tenant utilization and workload bulkheads;
- inspect provider Retry-After/quota state;
- inspect generic circuit state;
- preserve critical reserved capacity;
- let durable pending work wait rather than creating duplicate retries;
- monitor recovery-admission rate after outage.

Never bypass load-shed/circuit state by creating ad-hoc direct provider calls.

## Schedules and missed work

Inspect schedule state, lease owner/fencing, next due time and misfire policy.
After downtime, bounded scheduler/outbox batches should recover work. Do not
manually fire every overdue schedule simultaneously.

## Execution worker drain, quarantine and replacement

1. stop new assignments to the target worker/pool;
2. inspect claimed/running assignments and leases;
3. let safe work finish or expire/reconcile leases;
4. quarantine workers with sandbox/version/capability failures;
5. register compatible replacement capacity;
6. verify execution-contract support before returning the pool to service.

## Extension upgrade, quarantine and uninstall

1. verify target package provenance/signature/digest;
2. verify host/API compatibility;
3. review requested/granted capabilities;
4. drain dependent work if migration requires it;
5. run extension migration/conformance checks;
6. enable the new version only after verification;
7. quarantine on unsafe behavior;
8. uninstall only after references/config/migrations are reconciled.

Installation never grants authority by itself.

## Key rotation and recovery

Before rotating/revoking a key:

- list dependent encrypted objects/backups;
- create/activate replacement version;
- migrate/re-encrypt supported data;
- verify new reads/writes and Recovery drills;
- retain old version while historical dependencies require it;
- revoke only after dependency checks pass.

If a required key version is lost/revoked, Recovery verification should fail
rather than silently substitute another key.

## Replicated/failover mode

Before enabling production replicated autonomy:

- verify shared StateStore and transport configuration;
- verify coordination health and lease/fencing semantics;
- ensure only fenced owner instances perform singleton responsibilities;
- test takeover after lease expiry;
- verify no duplicate external action occurs during failover;
- verify audit/recovery/capacity qualification for the intended topology.

During suspected split brain, pause protected execution and inspect fencing/
ownership before resuming.

## Recovery from unknown provider outcome

For an ActionIntent with uncertain outcome:

1. stop automatic replay;
2. inspect provider state/receipt using the canonical reconciliation path;
3. decide whether the external action happened;
4. record reconciliation result;
5. retry only when idempotency/verification proves it is safe.

This is safer than assuming timeout means failure.
