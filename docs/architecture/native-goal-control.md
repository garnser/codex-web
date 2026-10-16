# Native Goal control dependency contract

The bounded source patch under `patches/codex/goal-control-v1` targets official
Codex commit `83086188ae0614bd882818ff1aa23f3194962012`. It is not installed or
advertised. Upstream 0.163.0 interrupted-turn accounting, turn-start lock ordering
and parallel Goal GET fixes remain unchanged. The rebase preserves new state
exports and adds only the GoalControlRequest export beside them. Cargo.lock, upstream license, source hashes and public build commands
are pinned in UPSTREAM.json. No whole SDK is vendored.

The private native Goal database gains a monotonic revision across SQL writers,
a fenced write hold and an immutable status-control receipt. The status-only
transaction compares Goal identity/revision/owner epoch and preserves objective,
created identity, accounting and budget. A failed transaction rolls back its hold
transition and receipt. Reconciliation reads the original immutable result; a
same-value readback is not attribution. Accounting preparation errors abort.

This is a prerequisite, not a complete all-writer runtime barrier. The trusted
native coordinator must first prevent starts, drain delivered model/tool results
and extension/descendant accounting callbacks, and successfully flush progress.
SQL fencing then prevents writes; it does not stop in-memory writers or secure a
file accessible to tool processes. Store ownership, proc/stdio/socket isolation,
coordinator lease expiry/crash recovery and all-writer drain remain unqualified.

The new adapter reserves contract1.0 capability/commit/receipt messages. It refuses
missing or partial capabilities and has no raw goal/set fallback. The capability
requires allWriterBarrier, revisionCas, atomicReceipt, accountingFlush,
allTransportAdmission and privateStateIsolation. These claims are runtime facts,
not actor authority; the existing procedure engine must explicitly admit the new
operation. No API route or runtime capability registration is activated here.
A native wire handler is deliberately not advertised by this partial SQL patch.

Before enabling an operator action, extend existing Goal/Session UI, canonical
ActionIntent/authority/procedure admission and server-derived admin/MFA/current
Goal ownership, tenant/Project/resources, binding revision, assignment/fence/lease
and runtime-generation checks. Caller origin is not authority. No held business
request is consumed; global/business pauses and kill/budget gates stay enforced.
The native Goal remains sole continuation owner; no app dispatcher is invoked.

Every provider attempt must have a trusted native-only pre-egress permit, including
WS messages/prewarm, SSE/HTTP retries, tool rounds, compaction, fallback and native
automatic continuation. The existing opaque CONNECT proxy cannot establish this.
The first manual request waits for its generation-bound canonical start receipt.
Already-forwarded work remains uncertain until actually drained and accounted.

The migration is a new persisted native boundary. An old binary must not silently
write around it; explicit version-skew/rollback and private-state backup/restore
qualification are required before any build deployment. SDK Rust tests, bounded
public-dependency build, actual transport/isolation tests, UI guards, independent
review/current CI and authorised SAME-original-Goal following idle continuation
remain required. Source presence or a synthetic receipt is not installed consent.
