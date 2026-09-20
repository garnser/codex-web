# Operator troubleshooting matrix

Start with canonical state and correlation IDs. Do not use model conversation as
the primary diagnostic record.

| Symptom | Inspect first | Safe next action | Avoid |
| --- | --- | --- | --- |
| Invalid/missing Definition | Definition record, schema/version, effective revision, usage | fix/validate draft or restore compatible published revision | raw DB edit or client-side fallback |
| Stale Definition cache | canonical revision vs node/cache revision | reload/reconcile from canonical state | continuing with stale authority/execution meaning |
| Failed Definition publication | validation/approval/lifecycle state | correct blocker and republish new revision | mutating published record in place |
| TaskSource sync failure | credential ref, provider health, cursor/provenance | restore credential/provider then reconcile | recreating canonical Work Items blindly |
| Action provider timeout | ActionIntent, receipt, idempotency, provider state | reconcile unknown outcome before retry | assuming timeout means failure |
| Model provider degraded | ProviderCapacity, routing, circuit/Retry-After | wait/fallback according to canonical routing policy | ad-hoc untracked provider call |
| Worker quarantined | lifecycle, version, capability, sandbox failure, lease | drain/replace with compatible worker | forcing assignment to incompatible worker |
| Extension quarantined | manifest/version, signature, grants, migration/conformance | inspect/repair or roll back compatible extension | re-enable without resolving failure |
| Credential revoked | SecretReference status/binding | rotate/rebind reference | placing raw replacement token in logs/prompts |
| Encryption key unavailable | key ID/version/backend and dependent objects | restore backend/key version or follow recovery policy | substituting a different key version |
| Session/token revoked | identity/session/service-token state | reauthenticate/reissue least-privilege token | bypassing auth with local flags |
| Execution preflight blocked | retained attempt, correlation ID, effective profile/repository/sandbox, typed blocker | repair canonical target/worker/credential/lease then use authorized retained Retry | retyping the task, editing retained state, or weakening the blocker |
| Repository target ambiguous | Project Resource bindings and target provenance | bind/select one authorized mutable repository target | choosing the first path or asking a model to decide authority |
| Execution lease expired | assignment/workspace lease and fencing | reclaim/reconcile via canonical worker APIs | reusing stale fencing token |
| Missed schedule | due time, owner, lease, misfire policy | let bounded scheduler recovery apply | firing all overdue jobs manually |
| Evaluation/replay failure | pinned Definition/model/runtime refs and Evidence | restore compatible inputs, rerun bounded evaluation | comparing against changed unpinned definitions |
| Attention not delivered | Attention item status/routing/expiry | repair routing while preserving item | creating a parallel notification truth |
| Approval stuck | target digest/version, quorum, assurance, expiry | obtain canonical decisions or create new request after expiry | editing approval status directly |
| Quota/entitlement denied | entitlement mode/limit/usage/scope | adjust canonical entitlement or reduce workload | hiding denial in UI |
| Capacity load-shed | tenant/global utilization, bulkheads, circuits | allow backlog to drain; preserve critical headroom | spawning duplicate retries |
| Failed release gate | artifact digest, SBOM/provenance/signature, Evidence/approval | fix gate and promote same immutable qualified artifact | rebuilding under same release identity |
| Upgrade incompatibility | source/target matrix, worker/extension/Definition compatibility | stop at preflight/drain and resolve blocker | forcing rollout across unsupported skew |
| Migration step failed | step idempotency, attempt/result, irreversible flag | resume only if declared idempotent or follow rollback/recovery | automatic replay of non-idempotent step |
| Failed restore verification | checksum, schema, key manifest, audit root, destination | repair dependency and rerun isolated verification | enabling side effects from unverified restore |
| Split-brain suspicion | coordination owner/lease/fencing token | pause protected execution and reconcile ownership | allowing both owners to act |
| Duplicate event/action | event ID/idempotency key/receipt | dedupe/reconcile canonical state | deleting audit history |
| Stuck Work Item/handoff | lifecycle, owner, blocker, acknowledgement, Attention | reassign/acknowledge through canonical work APIs | inferring ownership from chat |
| Active Incident not resolving | severity/commander, containment, restoration Evidence | complete recovery verification/postmortem gates | marking resolved from prose assertion |
| Provider outage recovery storm | scheduler/outbox/capacity/recovery admission rates | allow bounded catch-up | removing rate/bulkhead controls |

## Diagnostic order

1. tenant/workspace and authenticated identity;
2. canonical object lifecycle/state;
3. effective Definition/policy/configuration revision;
4. Attention/Incident/Approval blockers;
5. correlation/causation/execution/ActionIntent identifiers;
6. provider/worker/extension external state;
7. Evidence/verification and audit integrity;
8. logs/metrics/traces.

## Unknown outcomes and reconciliation

Unknown external outcome is a distinct state, not generic failure. Preserve the
ActionIntent and provider identifiers, query/reconcile the external system, then
record the canonical result before any retry.

## Stale/conflict behavior

A stale revision, expired lease, version mismatch or optimistic-concurrency
conflict is expected to block mutation. Refresh canonical state and retry the
intentional operation against the current revision. Do not weaken concurrency
checks to make the UI “work.”

## Secret/key safety while troubleshooting

Never ask an operator to paste raw credentials/private keys into issue comments,
chat, screenshots or model prompts. Diagnose by reference ID/version/status and
backend health.

For multi-repository execution diagnostics and recovery, see [Multi-repository Projects](../operations/multi-repository-projects.md).
