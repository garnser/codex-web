# Execution worker trust boundary

## Status

Canonical execution-worker trust-boundary foundation.

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

- canonical execution subject, execution, project and resource identity;
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

### Execution subject and v1.1 compatibility

Worker assignments and execution workspaces use one shared typed execution
subject:

- `work_item:<ref>` for canonical Work Item execution;
- `thread:<thread-id>` for interactive/web/bot thread execution.

The subject kind is explicit structured state. Thread executions must not be
encoded as fake `work_item_ref` values. `work_item_ref` remains a
backward-compatible projection only for `work_item` subjects so existing
Work Item APIs and execution-contract attribution remain stable.

The persisted execution-worker and execution-workspace state contracts are
version `1.1`. Loading `1.0` deterministically migrates each legacy
`work_item_ref` to the equivalent `{kind: "work_item", ref: ...}` subject.
Conflicting subject/work-item projections fail validation rather than guessing.
Workspace identity, branch derivation and assignment/workspace matching use the
canonical subject. Work Item synchronization is skipped entirely for non-Work
Item subjects.

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
Canonical Work Item state is mutated by the control plane only when the
execution subject is a real Work Item and after verifying the assignment lease
and result; workers do not receive direct database access.

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
should produce canonical Evidence and Verification records where applicable.

## UI impact

The platform administration UI and operator workspaces should expose worker pools, identity, capability set, version,
heartbeat/health, concurrency, execution subject, assignment/fence/lease status,
drain/quarantine/revocation, sandbox/network/resource limits, and failure
evidence. Raw lease tokens and secret material must never be rendered.


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
- an active canonical filesystem execution workspace matching the execution,
  execution subject, project/resource set and base revision;
- `command_execution` in the assignment capability set;
- an explicit sandbox mode of `read-only`, `workspace-write`, or `danger-full-access`;
- a network-disabled policy that Bubblewrap can actually enforce;
- bounded CPU, address-space, process-count, file-size/disk and wall-clock
  limits.

The local backend builds a minimal filesystem namespace rather than exposing
the host root. It mounts the runtime toolchain from `/usr` read-only, provides
`/proc`, `/dev`, private `/tmp` and a private HOME, and mounts only the
assigned execution workspace writable for `workspace-write` and
`danger-full-access` assignments (read-only for `read-only`). Git worktrees may
additionally receive the canonical
target repository's shared Git metadata directory required by that worktree;
unrelated control-plane data, application state, key/secret directories and
the operator's home are absent. The backend unshares process/user/IPC/UTS/network
namespaces and starts the command in a new process session. POSIX rlimits constrain CPU time, address space,
processes and individual file size. The parent worker monitors total workspace
disk usage and wall time and kills the complete process group on breach.

`danger-full-access` is deliberately scoped to the assigned worker environment.
The sandbox value is passed through to Codex so its inner command sandbox is
disabled, while the outer Bubblewrap worker boundary remains authoritative. The
assigned execution workspace and canonical Git metadata are writable, but the
host root, control-plane database/state, unrelated tenant workspaces, operator
home, and secret/key directories remain absent. The local worker also keeps its
private network namespace; selecting `danger-full-access` does not implicitly
grant the canonical `network` capability or unrestricted repository egress.

### Orchestration-only scratch execution

Coordination work does not need a fake repository checkout. The canonical
`orchestration-only` execution profile resolves to a scratch
`ExecutionWorkspace`:

- the workspace has no canonical repository resource IDs and no Git worktree;
- the worker assignment requires `command_execution` but not `git`;
- the workspace is still tenant/project scoped, assignment-bound, deadline
  bounded and fenced through the normal worker state machine;
- the scratch directory is created underneath the configured execution-workspace
  backend root with the same root-escape checks and deterministic cleanup/
  recovery as repository workspaces;
- Bubblewrap remains the outer execution boundary and generic networking remains
  disabled;
- repository mutation, broad host filesystem access and ad-hoc localhost access
  are not implied by the profile;
- the exact execution-profile Definition revision is persisted on the
  assignment and emitted in assignment audit metadata.

The profile may declare canonical control-plane operations such as work-item
read/handoff/reconcile for explainability and future capability negotiation.
Those declarations do not expose the control plane to the worker. Governed
agent-to-control-plane API access is a separate brokered capability and must
authorize the exact operation, identity, tenant/project scope and live
assignment/fence before use.

### Brokered control-plane access

An `orchestration-only` assignment may receive a separate brokered control-plane
channel. This is not generic worker networking and it is not an HTTP proxy to
localhost.

The local implementation uses a private host-side Unix socket mounted read-only
into the worker namespace plus a fixed loopback relay at
`CODEX_WEB_CONTROL_PLANE_URL=http://127.0.0.1:8788`. The relay can reach only
that Unix socket. The broker parses each request itself and dispatches only a
small code-owned operation catalog to canonical codex-web services.

Initial reachability is limited to scoped Work Item operations:

- list/read;
- progress;
- handoff;
- acknowledgement;
- retry;
- reconciliation.

Reachability is distinct from authority. Every request re-resolves the current
worker service identity and evaluates the requested capability through the
canonical Role authority service for the assignment's tenant/workspace/project
and target resources. A profile declaration or reachable broker path never
grants authority by itself.

Every broker request also revalidates the live assignment, worker, fence, lease
and deadline. Revoked/disabled service identities, stale fences, changed worker
bindings, expired assignments and canonical Role denials fail closed. No admin,
service-token, lease-token or reusable broker credential is copied into the
workspace, command environment, assignment state, response payload or audit
record.

The broker additionally enforces deterministic header/request/response byte
limits, concurrency and requests-per-minute limits. Absolute-form URLs,
unapproved methods, unapproved paths, transfer-encoded requests and arbitrary
localhost services are denied. Correlation/causation IDs are bounded and
recorded; the correlation ID is returned to the caller.

Metadata-only audit records retain the assignment/execution/worker/fence,
canonical service identity, operation/capability, target, authority decision and
Definition revision, response class and denial reason. Request bodies and secret
material are never stored in broker audit state. Operators inspect the effective
broker scope and audit linkage through the existing execution-worker surface.

### Network policy

Bubblewrap can reliably provide a private network namespace for network-disabled
assignments. It does not by itself provide hostname/DNS allowlist enforcement.
Consequently the local worker does not advertise the canonical `network`
capability and rejects network-enabled assignments, including allowlisted ones.
A future remote/container transport may advertise that capability only after it
can enforce the requested egress policy.

This is deliberately fail-closed: codex-web never converts
`allowed_hosts=(...)` into unrestricted network access.

#### Trusted Codex model-provider egress

Assignment-bound Codex app-server sessions need model-provider transport, but
that transport is **not** the worker's generic `network` capability and is not
shared with repository commands.

The local implementation keeps Bubblewrap under `--unshare-net` and adds a
separate assignment-bound broker:

- a host-side Unix-socket CONNECT broker accepts only HTTPS destinations derived
  from active canonical Model Gateway provider metadata plus the exact
  code-owned first-party Codex/OpenAI endpoints when applicable;
- every CONNECT requires a random per-session proxy capability and revalidates
  the current assignment, worker, fence, deadline and delegated credential
  authority before opening provider transport;
- a tiny loopback relay inside the private network namespace bridges only the
  trusted Codex parent process to that Unix socket; the broker directory is
  mounted read-only and outside the repository workspace;
- the proxy capability exists only in the trusted Codex launch environment.
  Codex child-command policy uses `inherit="none"` and explicitly excludes
  upper- and lower-case HTTP(S)/ALL/NO proxy variables;
- repository/tool execution still receives `networkAccess: false` and generic
  Bubblewrap assignments still reject `network.enabled=true`;
- non-HTTPS custom model-provider endpoints do not receive a worker egress
  exception.

This means model transport does not imply repository network authority. There is
no host-network fallback, wildcard provider domain, or second model-routing
authority store. Stale worker/fence/delegation state denies new broker
connections and the session watchdog terminates the bound app-server.

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

### Canonical thread-turn execution binding

Before any production thread turn can start model RPCs, the control plane now has
a deterministic binding stage that prepares the canonical execution state for
that explicit execution ID. It does not launch Codex and it does not resolve a
credential value.

The binding stage:

- resolves the canonical Project in the authenticated tenant/workspace;
- resolves all active project Resource bindings and requires exactly one active
  repository Resource for the local Git execution workspace;
- creates a typed `thread:<thread-id>` ExecutionSubject rather than fabricating
  a Work Item reference;
- resolves `codex.worker.access_token_secret` through the typed Configuration
  Registry using normal organization → workspace → project precedence;
- accepts only the `SECRET_REF` value and persists only its SecretBroker
  reference ID;
- acquires the canonical ExecutionWorkspace for the explicit execution ID,
  project/resource set, sandbox-derived lease mode and bounded deadline;
- creates one ExecutionAssignment with the exact subject, project/resources,
  provisioned base revision, workspace ID, sandbox, approval policy, resource
  limits, deadline, command/Git capabilities and configured secret reference;
- returns canonical IDs/metadata only.

Preparation is idempotent for the same explicit execution ID. An existing
assignment must still match the thread subject, project, execution controls and
workspace correlation; conflicting state fails closed. Missing credential
configuration, no eligible repository, multiple active repositories, invalid
deadlines or divergent canonical state also fail closed.

Configuration selects which secret reference is attached to the assignment but
does **not** authorize its use. SecretBroker ACL/expiry plus the worker-scoped
delegation contract remain the authority boundary that can turn that reference
into an ephemeral Codex credential. There is no arbitrary-secret scan,
`CODEX_HOME`/auth.json fallback or control-plane environment fallback.

This stage intentionally stops before `thread/resume`, `thread/start` or
`turn/start`. Production RPC routing and assignment/session completion belong
to the worker-transport migration; the planner only guarantees that those operations can
begin from one canonical, isolated and auditable execution binding.

### Production turn routing through isolated sessions

Production `thread/resume` and `turn/start` traffic for an executing turn is
routed through the AssignmentBoundCodexSession that owns the canonical
ExecutionAssignment. The control-plane `host.codex` process is not a fallback
for an active assignment.

The turn orchestrator now:

- creates or reuses one explicit thread-turn execution ID;
- obtains the canonical thread execution binding before any model RPC;
- starts the matching assignment-bound Codex session;
- uses the isolated ExecutionWorkspace path for `cwd` and sandbox policy;
- persists execution ID, assignment ID, workspace ID, worker ID and fence on
  ActiveThreadTurn state;
- routes active thread reads/lifecycle requests to the same isolated session;
- completes the exact fenced assignment on terminal turn completion/failure;
- namespaces interactive approval request IDs by assignment and routes approval
  responses to the runtime that owns them, so concurrent isolated sessions
  cannot collide on raw app-server request IDs;
- keeps `turn/start` timeouts attached to the already-active execution rather
  than silently queueing a duplicate turn.

Missing/stale sessions, changed fences, quarantined/revoked workers, expired
delegations and other canonical mismatches fail closed. There is no fallback
from an active assignment to the long-lived control-plane CodexRuntime.

This migration intentionally leaves metadata-oriented compatibility operations
such as initial `thread/start` creation and inactive-thread listing on the
control-plane runtime for the worker-transport migration. That compatibility path does not
execute a production turn. Removing the remaining global app-server requirement
and completing restart/session-loss coverage is required before the migration is considered complete.

### Assignment-bound Codex app-server session

The local worker now has a concrete interactive Codex session primitive. One
session belongs to exactly one canonical ExecutionAssignment, local worker ID
and monotonic fence. It is not a reusable global app-server and cannot silently
restart after the bound process exits.

Launch proceeds in this order:

1. validate the canonical execution workspace and Bubblewrap assignment policy;
2. heartbeat/claim/start the exact assignment through the worker state machine;
3. resolve one short-lived Codex credential through the worker-scoped delegation
   contract above;
4. inside that SecretBroker callback, start `codex app-server` through the same
   Bubblewrap namespace, minimal environment and POSIX resource limits as other
   local worker commands;
5. hand only the already-running subprocess to the existing `CodexRuntime`,
   which remains the single JSON-RPC protocol implementation.

The raw access token is therefore present only while trusted worker code creates
the Codex process. The Python session retains the process handle and
metadata-only delegation (secret reference, rotation, expiry, assignment,
worker and fence), never the credential value.

While the process is alive, a deterministic watchdog:

- requires the worker to remain active/draining and the assignment to remain
  running on the exact worker/fence;
- revalidates delegated secret rotation/expiry and assignment deadline;
- heartbeats the worker and renews the same canonical fenced lease before
  expiry;
- enforces the assignment wall-clock and total workspace disk limits in
  addition to the Bubblewrap process rlimits;
- terminates the app-server if the worker is quarantined/revoked/offline, the
  lease/fence changes, the assignment deadline passes, delegated authentication
  rotates/expires, or resource bounds are exceeded.

Shutdown uses the normal runtime supervisor to stop any assignment-bound
sessions. A session intentionally does not complete the canonical assignment on
its own; later production transport orchestration owns the semantic completion
point and must submit artifacts/evidence through the existing worker boundary.

### Current migration boundary

This backend is the concrete execution primitive for local worker command
execution. The worker-scoped Codex authentication blocker is now separated from
the remaining transport migration: the existing long-lived control-plane Codex
app-server/thread compatibility transport has **not** silently become an
assignment-bound worker process.

The delegation primitive is the credential/state seam required by that move.
The long-lived compatibility transport must be migrated so each worker Codex
process is associated with canonical assignment/workspace/lease state and uses
this delegation contract. Until that migration is complete, new untrusted
command/tool execution paths must use the isolated worker backend rather than
introducing direct `subprocess` execution in the control plane.
