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


## Local isolated execution backend

The built-in local worker no longer advertises `command_execution` merely because
codex-web is running on the same host. At startup the control plane probes the
Bubblewrap sandbox and reconciles the canonical worker capabilities with the
actual result.

If Bubblewrap is missing, user namespaces are disabled, or the probe otherwise
fails, the local worker remains registered for metadata-safe capabilities such
as Git/artifact handling but **does not** advertise command execution. Pending
command assignments therefore remain ineligible instead of falling back to an
unsandboxed subprocess.

A local command execution requires all of the following:

- a canonical execution assignment already authorized by the control plane;
- the canonical local worker identity and active fenced lease;
- an active #135 filesystem execution workspace matching the execution,
  Work Item, project/resource set and base revision;
- `command_execution` in the assignment capability set;
- a sandbox other than `danger-full-access`;
- a network-disabled policy that Bubblewrap can actually enforce;
- bounded CPU, address-space, process-count, file-size/disk and wall-clock
  limits.

The local backend builds a minimal filesystem namespace rather than exposing
the host root. It mounts the runtime toolchain from `/usr` read-only, provides
`/proc`, `/dev`, private `/tmp` and a private HOME, and mounts only the
assigned execution workspace writable for `workspace-write` assignments
(read-only otherwise). Git worktrees may additionally receive the canonical
target repository's shared Git metadata directory required by that worktree;
unrelated control-plane data, application state, key/secret directories and
the operator's home are absent. The backend unshares process/user/IPC/UTS/network
namespaces and starts the command in a new process session. POSIX rlimits constrain CPU time, address space,
processes and individual file size. The parent worker monitors total workspace
disk usage and wall time and kills the complete process group on breach.

### Network policy

Bubblewrap can reliably provide a private network namespace for network-disabled
assignments. It does not by itself provide hostname/DNS allowlist enforcement.
Consequently the local worker does not advertise the canonical `network`
capability and rejects network-enabled assignments, including allowlisted ones.
A future remote/container transport may advertise that capability only after it
can enforce the requested egress policy.

This is deliberately fail-closed: codex-web never converts
`allowed_hosts=(...)` into unrestricted network access.

### Environment and secrets

The execution process receives a minimal environment containing only ordinary
locale/terminal/timezone values and `PATH`. Ambient control-plane environment
variables such as provider tokens, cloud credentials and application secrets
are not inherited.

Secret references on the canonical assignment remain references. The local
backend does not resolve them automatically. Any future credential grant must be
purpose-specific, time-bounded and mediated by the canonical SecretBroker rather
than copying the control-plane environment into the worker.

Command argv is ephemeral execution input. Worker results and limit-breach
Evidence store only the executable basename and a SHA-256 argv digest, never the
raw argv, stdout or stderr.

### Lease and failure behavior

The local runtime claims the exact assignment through the existing worker state
machine, starts it with the current fence/token, heartbeats the worker, and
renews the same fenced lease while the sandboxed process runs. Completion still
goes through the canonical worker service.

CPU/file/disk/wall/resource failures produce a structured failed assignment.
When Artifact/Evidence storage is available, limit breaches also produce
metadata-only `policy_evaluation` failure Evidence tied to the canonical
execution/workspace. If the completion fence changes before the assignment can
commit, that Evidence is invalidated rather than being left as accepted proof.

### Worker-scoped Codex authentication delegation

The worker boundary now includes an explicit Codex authentication delegation
primitive. It does **not** copy or mount the control-plane `CODEX_HOME`, keyring,
`auth.json`, or renewable login cache.

A delegated Codex launch is available only when all of these checks succeed:

- the canonical assignment is already `claimed` or `running`;
- the exact assigned worker ID and monotonic fence still match its live lease;
- the authenticated caller is the worker service principal with both
  `execution-worker:run` and `secret:use`;
- the assignment contains exactly one SecretBroker reference whose provider is
  `codex`/ `openai` and whose purpose is `codex_access_token`;
- that secret reference explicitly permits the worker identity, is active, has
  an expiry, and expires inside both the configured delegation TTL and the
  assignment deadline;
- the secret rotation still matches the issued delegation when it is reused.

The credential value exists only inside `SecretBroker.use(...)` while trusted
worker code launches Codex. The canonical assignment, events, artifacts,
evidence, logs, UI and delegation metadata contain only the secret reference,
rotation, expiry, worker ID and fence. SecretBroker's boundary scrubber still
rejects/scrubs accidental credential escape from the trusted launch callback.

The delegated Codex command forces:

- `cli_auth_credentials_store="ephemeral"`, so the delegated credential stays
  in the Codex process rather than becoming `auth.json`;
- the private worker `CODEX_HOME=/tmp/codex-worker-home`;
- `shell_environment_policy.inherit="none"`;
- automatic secret-name filtering and explicit exclusion of
  `CODEX_ACCESS_TOKEN`, `CODEX_API_KEY` and `OPENAI_API_KEY` from repository
  child commands.

Generic worker command execution does not resolve assignment secret references
and does not receive this environment. This is a Codex-specific trusted launch
contract, not a general-purpose way for repository commands to request secrets.

Rotation, expiry, tenant mismatch, missing worker ACL, stale/reassigned fences,
expired leases and assignment deadline expiry all fail closed before launch or
when a delegation is revalidated.

### Current migration boundary

This backend is the concrete execution primitive for local worker command
execution. The worker-scoped Codex authentication blocker is now separated from
the remaining transport migration: the existing long-lived control-plane Codex
app-server/thread compatibility transport has **not** silently become an
assignment-bound worker process.

The delegation primitive is the credential/state seam required by that move.
#166 still owns migrating the long-lived compatibility transport so each worker
Codex process is associated with canonical assignment/workspace/lease state and
uses this delegation contract. Until that migration is complete, new untrusted
command/tool execution paths must use the isolated worker backend rather than
introducing direct `subprocess` execution in the control plane.
