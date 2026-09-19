# Capacity, backpressure and resilience qualification

## Status

Milestone 11 overload-control contract. Capacity is enforced deterministically;
an overloaded codex-web must delay or shed lower-priority execution rather than
create unbounded queues, retries, model calls or provider actions.

Canonical durable state remains authoritative. Capacity control does not discard
committed canonical events, ActionIntents, approvals, incidents or
reconciliation work.

## Existing bounded backlog surfaces

CapacityPolicy complements existing bounded mechanisms rather than replacing
them:

- per-thread turn queues already have a configured maximum depth;
- canonical EventTransport uses bounded local queues and durable outbox retry;
- outbox dispatcher and transport consumer process bounded batches;
- Scheduler claims/fires bounded batches and applies explicit misfire policy;
- ActionIntent retry policies and worker leases are bounded;
- ProviderCapacityService honors provider quota/throttle state and Retry-After;
- autonomy reasoning/actions have scoped token/cost/action budgets.

The M11 CapacityService owns shared execution admission and qualification across
those domains.

## Shared admission leases

Capacity leases live in the shared StateStore, so replicated control-plane
instances see the same utilization rather than independent process-local
semaphores.

Policy defines:

- hard global in-flight ceiling;
- hard tenant/workspace in-flight ceiling;
- reserved global/tenant headroom for CRITICAL work;
- per-workload bulkheads;
- low/normal load-shed utilization thresholds;
- lease expiry;
- recovery-admission rate;
- circuit failure/cooldown thresholds;
- bounded admission-history size.

Expired leases are deterministically reclaimed.

A noisy tenant reaches its tenant ceiling/reserve boundary before consuming the
whole global pool. This does not replace entitlements/quotas; it is an execution
fairness boundary.

## Priority policy

Priority is explicit:

- CRITICAL: incidents, recovery/rollback, reconciliation, security and critical
  approval paths;
- HIGH: other high/critical-risk operational execution;
- NORMAL: ordinary action/model/work execution;
- LOW: deferrable/background work.

LOW sheds first. NORMAL sheds at the configured load threshold. HIGH can use the
remaining noncritical pool. CRITICAL can use reserved headroom, but **cannot**
exceed the hard global or tenant ceilings.

Therefore safety work has deterministic preference under saturation without a
privileged unlimited execution path.

## ActionIntent integration

External provider execution acquires an ACTION capacity lease only after the
existing Role-authority, security and entitlement checks pass and before the
provider is called.

When capacity is unavailable:

- the provider is not called;
- the ActionIntent is returned from CLAIMED to durable PENDING;
- its worker lease is cleared;
- not_before is set from the capacity retry boundary;
- the intent remains reconcilable/idempotent canonical state.

The capacity lease covers the expensive provider call and is released after the
call. Provider timeout/exception/failed results feed the component circuit;
successful provider completion closes it.

## Circuit breakers and bulkheads

Circuits are scoped by tenant/workspace and component key. Consecutive failures
open the circuit. Before cooldown expires, new execution is deferred. After
cooldown one HALF_OPEN probe is admitted; concurrent probes are deferred.
Success closes/reset the circuit.

Per-workload limits provide bulkheads even when the global pool has room.

Provider-specific account/rate-limit state remains owned by
ProviderCapacityService. Generic action/component circuits complement it rather
than duplicating provider quota truth.

## Recovery-storm control

RECOVERY admissions are capped per tenant/workspace per rolling minute in
addition to the normal bulkheads. This bounds restart/failover catch-up after
outage and prevents a recovered dependency from triggering an uncontrolled
action/model storm.

Scheduler misfire and EventTransport outbox batching remain additional upstream
storm controls.

## Qualification Evidence

CapacityQualificationReport records deterministic load/stress/burst results:

- deployment mode and workload;
- target concurrency and burst size;
- test duration;
- p95 latency;
- error rate;
- lost-work count;
- retry amplification;
- maximum tenant share;
- whether saturation was exercised.

Qualification fails on lost work, excessive error/latency/retry amplification,
unfair tenant concentration, or a claimed tested concurrency above configured
hard capacity.

Results can publish canonical POLICY_EVALUATION Evidence with source
capacity-resilience-qualification. M11 production autonomy already exposes the
CAPACITY qualification gate; policy can bind that gate to the exact PASS
Evidence for the intended deployment scale.

## UI

/api/capacity exposes effective policy, current tenant/global utilization,
load-shed mode, open circuits and qualification history. Policy/qualification
mutations require administrator + MFA or capacity:admin service scope.

Issues #121/#125/#141 should render saturation, open circuits, provider
throttling, recovery storm pressure and capacity Evidence directly from these
canonical/deterministic surfaces without model calls.
