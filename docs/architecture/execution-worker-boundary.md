# Execution worker trust boundary

## Status

Milestone 3 foundation for issue #166.

The control plane owns canonical identity, policy, approvals, secrets, resources,
work state, evidence requirements and side-effect intent state. Execution
workers are treated as bounded, potentially compromised executors.

## Worker identity

Every worker is a tenant/workspace-scoped service identity with:

- stable worker ID;
- service identity reference;
- pool and software version;
- advertised capabilities;
- maximum concurrency;
- lifecycle: active, draining, quarantined, revoked or offline;
- heartbeat timestamp.

Registration is a control-plane administrative action. A worker cannot add its
own capabilities, increase concurrency, unquarantine itself, or change tenant
scope.

## Assignment contract

Assignments contain bounded inputs only:

- canonical work/execution/project/resource identity;
- base revision and execution-contract version;
- required worker capabilities;
- sandbox and approval policy;
- network policy and explicit host allowlist;
- CPU, memory, disk, process and wall-clock limits;
- secret references, never secret values;
- deadline;
- expected artifact/evidence types;
- optional canonical execution-workspace reference.

The assignment is not authority to perform arbitrary external side effects.
Those still flow through ActionProvider/ActionIntent.

## Fenced leases

Workers claim pending assignments using their authenticated service identity.
Every successful claim increments a monotonic fence and returns an opaque
lease token.

Renew/start/complete require the exact worker ID, fence and token. Expired,
reassigned, revoked or stale workers cannot complete an assignment. When a
claim expires, recovery marks the assignment lost and makes it eligible for an
explicit control-plane retry/reassignment rather than accepting late output.

This makes duplicate/stale completion deterministic and replay-resistant at the
application boundary.

## Capability and concurrency matching

The control plane only offers an assignment to an active worker when:

- tenant/workspace match;
- required capabilities are a subset of worker capabilities;
- the worker is not draining/quarantined/revoked/offline;
- its active claimed/running count is below max concurrency;
- assignment deadline has not expired.

Network access additionally requires the network capability. Workers cannot
self-grant capabilities from assignment input.

## Result boundary

Workers submit only bounded completion metadata and canonical artifact/evidence
IDs. Produced files/results must be uploaded through Artifact/Evidence APIs.
Canonical Work Item state is mutated by the control plane after verifying the
assignment lease and result; workers do not receive direct database access.

## Local mode

Single-node deployments use the same worker/assignment model. A local worker is
simply a worker with pool=local and the same capability, lease, fencing and
result rules. This keeps the development path simple without creating a
trusted bypass that would differ from future remote/ephemeral workers.

## Isolation expectations

The worker contract is provider-neutral. Local process/container/VM backends
must enforce the assignment's sandbox, network policy, limits and execution
workspace ownership. Higher-risk work should use disposable filesystems and
stronger isolation.

The control plane must not mount or expose unrelated:

- SQLite/control-plane state;
- key/secret material directories;
- other tenant workspaces;
- broad provider credentials.

Secret references are resolved only through a narrowly scoped execution secret
grant when a concrete worker backend needs them.

## Recovery and evidence

Worker loss, lease expiry, capability mismatch, stale completion and resource
limit breaches are explicit events. Limit breaches and execution failures
should produce canonical evidence through #136 where applicable.

## UI impact

#141/#125 should expose worker pools, identity, capability set, version,
heartbeat/health, concurrency, assignment/fence/lease status, drain/quarantine/
revocation, sandbox/network/resource limits, and failure evidence. Raw lease
tokens and secret material must never be rendered.
