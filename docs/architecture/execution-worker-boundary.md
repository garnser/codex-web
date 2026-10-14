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
Those still flow through ActionProvider/ActionIntent. A repository-write
assignment receives an assignment-bound repository-action surface on the same
Unix-socket broker. It can publish only the assignment's single writable
repository and active execution-workspace branch; create or reconcile a pull
request; create, comment on, or close an issue; and merge a pull or merge request
only when the canonical code-host provider reports it clean and mergeable. The
broker derives tenant, Project, repository, execution, requester, the unique
enabled GitHub or GitLab provider binding, and credential references from
canonical assignment state. The worker cannot select the provider, override
those values, or receive the provider credential. Zero or multiple matching
bindings fail closed.

Storage-backed broker identity, target-scope and authority checks run in one
awaited worker-thread boundary, preserving their original order and the caller's
ContextVars. Paginated Work Item reads and exact-item reads likewise execute
existing synchronous state/index construction off the shared event loop. Slow
canonical storage must not prevent the loop from receiving worker completion
notifications. This scheduling boundary does not alter tenant/resource checks,
cursor revision/filter validation, response schemas or side-effect authority.

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

### Explicit repository read scope

A public turn request that explicitly supplies `read_only_repository_resource_ids`
uses that exact set, including an empty set. Omission preserves remembered defaults
for ordinary unscoped turns. Admission persists explicit scope before bootstrap or
replacement, and queued requests retain the resolved set. An unrelated remembered
repository must not be restored as a read dependency during recovery. Existing
scope inspectors show the requested and admitted repository sets; authority,
profile revision, lease fencing and active-assignment guards remain enforced.

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

The built-in process-owned local worker is reconciled after persisted stale
workers and expired leases during application composition. Runtime supervision
then emits authenticated heartbeats while the worker is idle as well as during
assignments. One heartbeat completes before startup recovery is scheduled, so
slow startup cannot mark the newly ensured worker offline and queued recovery
does not require manual worker activation.

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

Worker readiness reads fresh validated worker records without loading assignment,
event or enrollment catalogs. It retains tenant scope, lifecycle, capability,
execution-contract and sandbox checks. Admission and concurrency decisions still
consult canonical assignments; readiness does not reserve worker capacity.

Worker loss, lease expiry, capability mismatch, stale completion and resource
limit breaches are explicit events. Limit breaches and execution failures
should produce canonical Evidence and Verification records where applicable.

Execution-workspace expiry recovery first reads and validates fresh canonical
lease records for the requested tenant scope and recovery timestamp. If no
unreleased lease has expired, recovery returns without loading or rewriting
workspace or event history. Negative probes are not cached. When expiry is
present, the existing recovery transition rechecks canonical state and retains
the same cleanup and durable repository-checkpoint safeguards. This projection
does not change persisted schemas, public APIs, lease authority or fencing.

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
the operator's home are absent. Code-owned public system CA trust-store
directories are mounted read-only so the trusted Codex process can authenticate
the exact HTTPS model-provider endpoints admitted by its assignment-bound
CONNECT broker; the host `/etc` directory remains absent. The backend unshares process/user/IPC/UTS/network
namespaces and starts the command in a new process session. POSIX rlimits
constrain CPU time, address space, processes and individual file size. The
parent worker monitors total workspace
disk usage and wall time and kills the complete process group on breach.

Linux charges `RLIMIT_NPROC` to the host UID rather than the Bubblewrap PID
namespace. The worker snapshots existing tasks for its real UID before launch
and adds the assignment's requested process headroom to that baseline, capped
by the inherited hard limit. Trusted interactive Codex sessions additionally
retain their finite local minimum for Chromium helper threads. This is shared
UID accounting, not an independent cgroup PID quota; concurrent host tasks can
consume that headroom. One-shot CPU, address-space and file-size limits retain
the assignment's exact values. Operator limits inspection reports the requested
headroom, while execution output records launch failures at the effective
kernel ceiling.

Filesystem executions also receive a writable, workspace-local Python virtual
environment. If the canonical Project has an operator-maintained `.venv`, only
that exact environment is additionally mounted read-only as a baseline package
layer. The execution-local environment remains first on `PATH`, so installs do
not mutate a shared Project environment. This targeted mount does not expose the
Project root and does not imply package-registry or generic network access.

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

An `orchestration-only` or `repository-write` assignment may receive a separate
brokered control-plane channel when its execution profile declares control-plane
operations. Repository access does not widen this channel. This is not generic
worker networking and it is not an HTTP proxy to localhost.

The local implementation uses a private host-side Unix socket mounted read-only
into the worker namespace plus a fixed loopback relay at
`CODEX_WEB_CONTROL_PLANE_URL=http://127.0.0.1:8788`. The relay can reach only
that Unix socket. The trusted Codex child-shell policy sets this non-secret
loopback locator explicitly while retaining `inherit="none"`, so repository
commands can discover governed operations without inheriting the app-server
environment or credential material. Assignment-bound Codex commands allow
socket access inside the worker namespace so they can reach these loopback
relays. The outer worker namespace remains the network boundary and has no
external interface, so this does not grant repository commands general network
access. The broker parses each request itself and dispatches only a
small code-owned operation catalog to canonical codex-web services.
Workers discover that assignment-scoped catalog through
`GET /api/control-plane-broker/operations`; the response contains operation
metadata and scope, never credentials or lease material. Catalog discovery and
repository actions evaluate the assignment requester through canonical Role
authority; the fenced worker service identity remains the executing principal.

The caller's catalog includes an additive `assignment_context` with only
assignment/execution/workspace IDs, the execution profile, pinned agent profile
ID/revision/record/role and declared writable/read-only repository IDs. This is
immutable assignment provenance, not effective authority or live-lease proof;
every operation still performs its own scope/authority/fencing checks. Disabled
broker profiles redact that context. No lease material, credentials, secret
references or filesystem paths are included.

Both worker and operator catalogs share repository-action request metadata.
Only `parameters` (a JSON object) and `idempotency_key` are accepted at the top
level; action fields belong inside `parameters`. Branch publication derives
`execution_workspace_id` from the current assignment and overrides caller
values. Malformed flat envelopes fail before intent creation, with a bounded
shape hint; metadata never changes action validation or publication authority.

The assignment-scoped workspace refresh operation resolves an exact revision in
the existing checkout first. If, and only if, a full 40-character commit is
absent locally, the broker may materialize the current default-branch tip that
the canonical code-host provider independently reports for the assignment's
single writable repository Resource. The broker derives the HTTPS remote from
that verified provider binding and repository fact; callers cannot provide a
URL, repository name, branch or credential. Fetch runs through the existing
credential-reference boundary with terminal prompting, hooks, recursive
submodules, redirects and non-HTTPS protocols disabled. Authenticated transport
runs in a freshly initialized temporary bare repository with no checkout-local
configuration, includes, URL rewrites or object alternates. The provider-attested
repository URL retains the GitLab path prefix and exact configured HTTPS port;
foreign authorities, userinfo and unsafe paths fail closed. Only after verifying
the fetched commit against the attested revision does the service import its
object pack into the checkout, without credentials, transport, reference updates
or working-tree writes. It revalidates the assignment lease/fence before the existing clean-workspace
refresh mutates HEAD. Dirty workspaces, abbreviated revisions, missing
credentials, foreign provider/resource facts, moving default branches and stale
assignments fail visibly. Concurrent recovery is serialized per repository
checkout and reuses an already materialized exact object without a second
fetch.

This is an additive compatibility change: the refresh request remains
`{"revision": <revision>}` and the existing `item` response remains unchanged;
the response adds bounded `materialization` outcome metadata. No persisted
schema, Definition, role, scheduler or repository primitive is added. The
existing workspace event history records safe provider/resource/revision
provenance, while the broker audit records denials and correlation. Existing
workspace and broker inspectors already render those canonical details, so no
separate UI state or linked UI implementation is required.

An active repository-write owner whose canonical execution subject is `thread`
or `thread_bootstrap` may request a bounded issue-scoped publication recovery
through the assignment broker. The broker accepts only a qualified Work Item in
the same tenant, workspace and Project, whose current owner matches the pinned
Agent Profile and whose resources include the assignment's single writable
repository. The request records the original execution subject and execution,
assignment ID and fence, requester identity, workspace lease, repository and
expiry. It does not rewrite the subject, create a Work Item execution, transfer
historical authority or extend either lease.

That provenance permits the existing GitLab-MR recovery contract to consume the
Work Item's independently verified issue relation while preserving all existing
exact-local-head, observed-remote-head, source-branch, provider-project and MR
state checks. A live workspace can hold only one active recovery scope; a
different issue, repository, assignment or fence is refused until the old scope
expires with its original assignment/workspace bound. Released, expired,
cross-Project, cross-repository, unowned and concurrent replacement requests
fail before ActionIntent creation. The eventual branch ActionIntent is attributed
to the recovery Work Item and original execution. The workspace inspector shows
both the immutable execution subject and recovery provenance, while the existing
ActionIntent inspector shows the Work Item/execution attribution. This adds no
provider credential exposure, Orchestrator authority, Operator provisioning
requirement or automatic intent replay.

The execution-workspace persisted contract is version `1.6`. Version `1.5`
migrates additively with no recovery scope. Older readers that reject unknown
workspace fields cannot read newly scoped records, so rollback requires the
canonical pre-transition snapshot or expiry/removal of those records; source-only
rollback is not advertised after a recovery scope is written.

Repository fact reads accept an optional single, nonempty
`repository_resource_id` query parameter. The default remains the assignment's
singular writable repository. Explicit selectors may choose only that repository
or an existing declared read-only repository in the same canonical assignment
scope; the selected resource receives tenant/project, Role authority, provider
binding and secret-boundary checks. This permits immutable dependency artifacts
to be retrieved without write authority. Orchestration-only profiles remain
ineligible for repository facts. The selector is rejected on mutations and does
not change publication targets, workspace leases, artifact byte limits or member
path validation. The operation catalog advertises this optional query parameter.

`repository.read.artifact-download` also advertises optional `member_path`:
`GET /api/repository-facts/artifacts/{artifact_id}/download?max_bytes={max_bytes}&member_path={url_encoded_relative_path}`.
This selects one member of the original job artifact, with the same repository,
tenant, job provenance and response-byte bounds as whole-archive reads. Omit it
for the original archive. Single relative member paths are validated against
traversal and absolute paths; duplicate or empty selectors are rejected. Use
original artifact members for bounded preview review rather than rebuilding or
mistaking a truncated ZIP prefix for the full artifact.

The channel is HTTP and does not require MCP tool registration. Task instructions
include an explicit direct-loopback discovery example with a bounded client
connection/read deadline. Proxy bypass is local to that broker request; it must
not alter model egress or global network policy. Preserve the returned HTTP
status, denial and correlation evidence. A client timeout is distinct from an
authority denial, and an ambiguous mutation must reconcile its existing
ActionIntent before retrying. Client deadlines never extend server-side
assignment leases, operation deadlines or authorization.

Initial reachability is limited to scoped Work Item operations:

- list/read;
- progress;
- handoff;
- acknowledgement;
- exact-owner steering;
- retry;
- reconciliation.

`work_item.steer` is not a general Thread API. Its request carries only the
expected canonical owner, the exact current owner-binding Thread, and a bounded
idempotency key. Tenant/workspace/Project and Work Item scope are checked before
Role authority. Continuity then revalidates actionable state and the same-Project
owner binding, refuses unrelated or stale Threads, and reports active, queued,
duplicate, or dispatched outcomes without launching duplicate work. The stable
canonical dispatch key remains the deduplication authority; changing a caller
key cannot bypass it. Dispatch uses the existing event path, which resolves the
recipient's published Agent/Execution Profile and strips repository scope from
profiles that do not permit it. A singular canonical Work Item repository is
attached as execution context, not as a grant.

`work_item.read` returns both canonical state and the configured TaskSource read
projection. Its `assigned_scope` selects canonical `next_action` when present;
otherwise it exposes the authoritative title, body/acceptance criteria, and
source identity already available through that credential-free boundary. Raw
provider credentials never enter the assignment.

The same read capability supports `?view=coordination`, retaining current
ownership, live handoff, actual acknowledgement timestamps, assigned scope and
exact `owner_dispatch` preconditions without replaying notes and checkpoint
contexts. Oversized Work Item reads and mutation receipts automatically use
this projection. `_projection.omitted_fields` explicitly identifies omitted
history, and `recent_handoffs` retains the last three recorded outcomes.
Canonical storage and the operator API remain unchanged. Successful committed
mutations retain their success and broker correlation receipt; accumulated
history alone must not turn an accepted acknowledgement into an apparent 502.
The response byte limit still applies to current facts and scope.

Repository-write assignments may also reconcile only their own branch-publish
and pull-request-merge ActionIntents. The broker requires the same tenant,
Project, requester and single writable repository. An exact originating
execution may request only retries that remain eligible under the canonical
ActionIntent policy; a recovered assignment may perform verification-only
reconciliation but cannot retry a historical mutation. Merge verification
reads provider state and never replays the merge. This boundary does not expose
a tenant-wide ActionIntent administration surface.

`GET /api/action-intents/{intent_id}/delivery-evidence` is a separate
`repository.read`/READ operation for inspecting branch-publish, pull-request upsert and merge
receipts, including from a superseded execution. Existing assignment, worker,
fence, requester, tenant/workspace, Project, admitted repository and Role checks
remain mandatory. It exposes only selected intent scope/destination metadata
and the latest eight receipt/verification metadata rows, with omission counts.
Canonical execution-start and completion timestamps are included with status
and attempt count. Missing timestamps or receipts alone never prove an unknown
provider outcome. The operation catalog advertises supported Action IDs from
the dispatch allowlist and marks this inspection read-only. For an upsert use
`/api/action-intents/{intent_id}/delivery-evidence?repository_resource_id={repository_id}`;
the raw `/api/action-intents/{intent_id}` administration path remains denied to
assignment-bound callers.
Arbitrary request/provider/inbox bodies and credential references are excluded.
It performs no reconciliation, retry or provider action. Unset historical Work
Item attribution remains unset; receipt inspection does not grant publication,
acknowledge intake, establish independent acceptance or complete an issue.

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
- the broker owns each accepted CONNECT handler for its complete lifetime and,
  on session shutdown, stops accepting, cancels and awaits every active handler
  before removing the private socket;
- assignment and fence validation runs outside the control-plane event loop;
  authenticated CONNECT bursts share a short validation result while the
  independent one-second session watchdog bounds revocation latency and owns
  worker heartbeat/lease renewal, preventing connection admission from
  amplifying durable writes or starving unrelated health, routing, and API work;
- workspace/bootstrap preparation, assignment preparation, credential-bound
  process launch, watchdog validation, and runtime request admission run
  blocking catalog/filesystem work outside the control-plane event loop,
  including assignment-session resolution and thread-list session adoption;
- stale-thread replacement is serialized and coalesced per recovery service so
  concurrent watchdogs cannot amplify workspace/assignment catalog mutations;
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
- checkpoints every writable Git member before terminal completion and retains
  the canonical branch unless an explicit discard was authorized;
- namespaces interactive approval request IDs by assignment and routes approval
  responses to the runtime that owns them, so concurrent isolated sessions
  cannot collide on raw app-server request IDs;
- keeps `turn/start` timeouts attached to the already-active execution rather
  than silently queueing a duplicate turn.

Missing/stale sessions, changed fences, quarantined/revoked workers, expired
delegations and other canonical mismatches fail closed. There is no fallback
from an active assignment to the long-lived control-plane CodexRuntime.

### Provider-neutral CLI thread transcript projection

CLI runtimes do not necessarily expose native historical turns after their
process exits or before they have acquired a provider-native session ID.
Codex-web therefore persists the user message before starting a CLI turn and
projects provider-neutral assistant message events into a bounded canonical
per-thread transcript. The projection contains user-visible messages only; it
does not copy credentials, raw command output, reasoning, or provider-specific
session state.

The projection is a code-owned persisted contract with schema version `1.0`.
Unknown versions fail visibly instead of being guessed. Each thread retains at
most 1,000 projected turns, dropping the oldest first; no raw provider errors,
credentials, reasoning, command output, or tool payloads are stored. Older
codex-web versions ignore the new state namespace, so rollback does not rewrite
or reinterpret provider-native history.

`thread/read` prefers a provider-native response when it contains native turns.
When a CLI runtime returns metadata without turns or is inactive, degraded, or
session-less, the API returns the canonical transcript instead. This is a
read-availability projection, not a second execution authority: provider-native
session identity, assignment/fence state, execution controls, and native Codex
history pagination retain their existing owners.

A CLI start that exits or times out before acquiring a provider-native session
ID is terminal for that process: codex-web marks the projected turn failed,
clears the active marker, and fails the exact fenced assignment so its lease
cannot continue renewing behind a permanently blocked thread queue. Native
Codex start timeouts keep their existing recoverable in-flight semantics.

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
   Bubblewrap namespace and minimal environment as other local worker commands.
   CPU, process, file-size, wall-clock and workspace-disk limits remain
   assignment-bound. The trusted app-server alone receives a 2 TiB sparse
   virtual-address ceiling so V8 can reserve its sandbox; one-shot repository
   commands and non-Codex interactive runtimes retain the assignment's strict
   address-space limit;
5. hand only the already-running subprocess to the existing `CodexRuntime`,
   which remains the single JSON-RPC protocol implementation.

The raw access token is therefore present only while trusted worker code creates
the Codex process. The Python session retains the process handle and
metadata-only delegation (secret reference, rotation, expiry, assignment,
worker and fence), never the credential value.

The assignment-bound process is intentionally non-restartable because a restart
requires fresh lease and credential validation. A bounded metadata/read RPC
timeout therefore fails that request without killing an otherwise healthy
one-shot process. The global control-plane Codex runtime retains its existing
restart-on-timeout recovery behavior.

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

### Trusted operator authentication evidence and transport health

The trusted-local credential-availability probe uses an actual successful
`account/read` response from the canonical operator Codex runtime, retained for
at most 120 monotonic seconds. Readiness alone never establishes authenticated
account evidence. A transient shared RPC subprocess restart does not log the
operator out: proof remains bounded to the same host process, effective UID,
Codex command, working directory, auth-source location and environment, and
credential/config file metadata. Credential contents and account email are not
stored in this proof or exposed in diagnostics.

Logout/login requests, account-change/login/refresh notifications, explicit
account-read RPC errors, logged-out/malformed account responses, expiry, and
operator/auth-context changes invalidate the evidence. An account-read started
before an invalidation cannot restore old evidence. The existing bounded quota
probe verifies account metadata once before reading quota; no model sampling or
additional polling loop is introduced.

Authentication evidence remains separate from RPC transport health and execution
preflight. A runtime with cleared readiness is unavailable even when its recent
authenticated-account evidence is still fresh. Worker-scoped delegation, actor,
policy, credential rotation/expiry, lease/fence and assignment checks remain
authoritative and unchanged.

Positive Codex `account/updated` or successful login-completed notifications
invalidate prior account evidence and schedule a real `account/read` outside the
stdout reader. One in-flight task coalesces bursts; it performs at most two
metadata reads, including one follow-up when account changes race the first
response. Negative auth modes/login results do not request a refresh. Logout or
a newer account change still prevents older responses from publishing proof.
Transport stop retires the refresh task, without weakening readiness or execution
authority checks. Continuous changes remain unavailable until a later ordinary
quota refresh or notification; no polling loop or model call is introduced.

Assignment-bound thread startup and turn continuation include the shared HTTP
control-plane broker discovery guidance, even when a Slack or bootstrap turn has
no work-item reference. Work-item contracts reuse that same guidance; an existing
copy is delivered once. This explains the current assignment channel and its
catalogued operations, without changing actor, repository, route, or policy
permissions. Trusted local execution without a worker assignment does not receive
an assignment-channel claim. A mutation timeout still requires reconciliation of
its existing ActionIntent before a retry.

Worker disk accounting iterates directory entries and reads each regular file's
size without repeating path metadata lookups. Nested symlink entries are skipped;
hidden files, hardlinks by path, shared Git metadata, and configured read-only
resources retain their existing accounting. Vanished or inaccessible entries keep
the existing filesystem-error behavior. This traversal optimization adds no cross-scan cache
and does not change disk limits, validation intervals, or assignment enforcement.

Bootstrap recovery retains the historical assignment's immutable agent profile
even when restarting its native session fails. A requested profile that conflicts
with that binding fails before replacement. When a generic bootstrap has no
profile, scope supersession or failed-session healing resolves the explicit
requested profile through the normal actor-aware runtime router. Recovery retains
that canonical runtime binding and refuses an implicit change from the inherited
or explicitly requested runtime. The actual turn repository and writable scope
remain subject to normal resource, assignment and workspace validation.

### Retained queue replacement routing

Queue admission restores replacement routing only from the canonical durable
replacement graph, verifying every edge against the retained Project and tenant.
Only the scheduling thread and its reply-target thread may differ; original
messages, destinations, actor/profile revisions and repository scope remain exact.
Active admission/retry claims fence route changes. The original snapshot, claim,
blockers and native outcome remain immutable; an uncertain original stays
nonretryable while later ready requests may start. An actual late original native
receipt still records that original start. Existing preflight inspectors expose
replacement routing separately from native outcome, with an attributed route
restoration event. This adds no persisted field, model call or execution grant.

### Fresh bootstrap recovery for a retained pre-start refusal

The preflight inspector can explicitly request a fresh bootstrap when the
canonical original thread is archived and a complete historical replacement
chain is unavailable. Administrator authority and MFA for a human are required.
The caller supplies the exact original-snapshot SHA-256 and a bounded reason;
the retained original supplies the actor, immutable profile revision, execution
profile, sandbox, approval policy and repository scopes. Missing provenance,
active work, unknown original starts and existing verified routes refuse creation.

The existing preflight record retains `bootstrap_recovery` version `1.0`, with
states `pending`, `refused` and `created`. Legacy records omit this field and
empty recovery history, preserving their previous serialization. Older readers
refuse records containing the new field; deployment must declare that persisted
contract boundary rather than imply source-only rollback. Original messages,
request scopes, blockers, attempt counters and native outcome remain unchanged.

Reservation commits before runtime creation and supplies correlated bootstrap
and execution IDs to the existing assignment-bound ThreadService. A pending
reservation cannot expire into another creation: subsequent requests reconcile
only canonical bootstrap binding and assignment receipts. A returned thread ID
alone cannot authorize routing. Verification requires the exact tenant, Project,
bootstrap execution, pending initial Turn, profile revision and granted repository
scope. The verified fresh edge and retained preflight transition commit together
through the canonical store transaction, fencing concurrent original receipts.
Inspection indexes can be rebuilt after that authoritative commit.

ThreadService distinguishes its own preparation refusals from provider errors
using a server-owned boundary immediately before provider thread creation.
Definite pre-start refusals retain attributed proof and permit another explicit
request; eight prior refused attempts are retained before further attempts are
blocked. Errors after that boundary and cancellation remain unresolved, never
inferred unstarted from missing receipts, queue absence or claim expiration.
Fresh bootstrap recovery does not submit the original Turn. Existing Retry
restores its exact queue copy after routing is verified, and ordinary admission
still rechecks actor/profile authority and records actual native start receipts.
Viewing or inspecting this workflow invokes no model and submits no native Turn.


### Project-scoped administrative target declarations

Execution-profile catalog schema `1.1` supports bounded `administrative_repository_ids`
for scratch execution profiles that declare explicit control-plane operations. These
are canonical resource references for governed provider administration, distinct from
Git workspace write/read scopes. They grant no filesystem, Git, network, host-mutation,
credential-reading, or automatic provider-action authority. The declaration must
resolve from the exact Project-scoped catalog; a global, workspace, or organization
catalog cannot supply effective administrative targets. Existing schema `1.0` rejects
administrative declarations, and ordinary serialized profile snapshots omit the empty
field so historical checksums and pinned definitions remain unchanged.

Administrative handoff and progress actor fields use the acting published Agent Role.
Administrative access cannot impersonate an implementer or independent validator in
a recipient acknowledgement; authenticated requester/worker identities remain distinct.

Administrative operations must validate the selected canonical target, current actor
and operational authority, live fenced assignment, effective published definition and
operation permission before creating a canonical ActionIntent. Historical profile or
requester identity alone cannot grant administrative permission. Administrative target
configuration does not replace independent review, required CI, provider receipts,
reconciliation of unknown outcomes, or release/deployment requirements.

Old readers do not understand catalog schema `1.1`; publishing that schema requires
an explicitly qualified code upgrade before configuring the administrative profile.
Do not publish a new administrative definition during mixed-version operation or
assume source-only rollback after such publication. The target declaration schema
alone does not enable administrative broker execution. Delivery and live acceptance
are tracked in GitHub issue #1351.

The existing worker/broker inspector renders declared administrative targets and
operations separately from Git scope. It shows the pinned and current execution
Definition and Agent Profile, Agent Role, operational authority Role, assignment
requester, and the authenticated broker actor recorded for an operation. A bounded
declaration observation invokes the same administrative guard for one explicitly
identified operation/target pair; it creates no authority decision, action or audit
event and grants no execution permission. Every real operation still rechecks its
complete live scope, Role, profile, lease/fence and policy requirements.

Broker audit loads on explicit request, reports loading/denied/unavailable/empty
states, and labels its latest twenty events as a bounded sample. Project changes
discard old observations and fence late responses. Broker admission and queue/batch
acceptance do not attest provider execution or recipient acknowledgement. Existing
ActionIntent links open the existing Automations workspace, select the assignment
execution and expose its canonical target
resources, unknown/reconciliation state, provider receipts and verification history
without rendering request bodies, provider output bodies or credential material.
Operators use the existing Agent Profile/Definition and resource links to repair a
missing, disabled or stale configuration, the worker inspector for lease/fence
refusals, and the existing Work Items workspace dependency graph for source/target Work
Item context. Graph links navigate to that Project workspace rather than scrolling
to a card hidden in another workspace.

Worker redraws reattach an existing broker inspector only when its complete public
assignment snapshot is unchanged. Its pending or loaded audit read then survives
unrelated worker refreshes. Changed configuration, pins, lease/fence or assignment
state replaces the inspector and fences the old response. A removed/filtered row
or Project change cancels its read and discards the panel; it cannot publish a
previous scope's result into a later view.

Reattaching an unchanged inspector also preserves the expanded assignment row so
its pending result remains visible. Worker administration initializes after its
imports complete even when DOM readiness has already fired; delayed module loads
must still bind refresh controls and load an already-open inspector.

## Automated native turn admission

The turn execution boundary reads the existing RuntimePolicy disable control
and canonical AutonomyControl mode. Automated origins require runtime autonomy
to be enabled and mode `active`. Human web, Slack and Telegram input and explicit
operator UI/retry controls remain admissible under a global autonomy pause;
source provenance comes from server-side producers, never message contents.
Unknown sources remain subject to the automated gate.

Queue drain checks this gate before retention, claims, removal or retry-counter
changes. A mixed queue may admit a human request behind waiting automation
without moving the waiting requests. Admission rechecks after provider/bootstrap
awaits before native activity/start. A proven refusal at this boundary restores
the original request and queue position without charging an attempt. Existing
unknown outcomes and immutable native receipts retain their reconciliation
requirements. Pause never authorizes replay or changes ownership of active turns.

Queue API and events expose the waiting count and reason from these same controls;
the conversation UI shows that reason in a separate queue notice, including
while native activity, uncertainty or approval status occupies the turn indicator. Canonical control changes wake the existing
queue drainer, which rechecks both controls. A runtime disable marker still blocks
automation after canonical resume; removing it requires the usual runtime/restart
or explicit queue wakeup. Waiting drains invoke no model and do not spin retries.

Already-open routed worker and ActionIntent workspaces load their canonical state
after controller imports finish, even when the legacy Developer panel is closed.
A visible ActionIntent workspace clears the old actor and results on Project
changes, then reads the new canonical scope with the existing response fences.

### Deferred queue position

A queued admission captures its physical queue order inside the same canonical
per-thread mutation that removes the request. A later refusal or cancellation
restores the complete original scheduling snapshot against surviving preceding
or following peers. Concurrent explicit retries retain their priority, and new
ordinary arrivals stay behind the deferred original. If no original peer remains,
the existing retained preflight intent's explicit retry count supplies priority
provenance; queue message text, caller labels and creation time cannot grant it.
The priority lookup verifies the canonical Project and current verified thread
route. It changes no request field or native outcome and creates no new registry.

Cancellation can leave an unknown start in the retained recovery domain even
when its original queue copy is restored. Restored position, missing receipts,
queue absence and a stale maintenance blocker do not prove a negative native
outcome. Existing recovery authorization and actual provider receipts remain
required before an unknown original may be retried or treated as started.

### Historical scheduling-order repair

The existing Thread queue exposes `GET /api/threads/{id}/queue/order-recovery`
for a read-only proposal and `POST` on the same path for an explicit guarded
repair. Apply requires administrator authority and human MFA-equivalent
assurance, canonical Thread/Project/tenant scope, an inactive original/current
Thread, and no active admission or retry claim. The request names the exact
digest of both the queue and its retained intents plus a bounded operator reason.
The backend derives the order; callers cannot submit replacement bodies, native
outcomes or their own ordering list.

Every queued entry must match its complete retained snapshot, with only verified
replacement routing and diagnostic retry counters projected separately. Explicit
retry priority keeps its current relative order; ordinary requests use original
creation provenance with stable ties. The queue compare-and-swap rechecks scope,
provenance, digest and the existing admission-ownership fence. It modifies only
list order. A metadata-only recovery event identifies actor, scope, reason,
original/resulting order and hashes. An audit write failure reports a committed
order without inventing a receipt or retrying the operation. Preflight/native
outcomes, original bodies, scope, profiles and reply destinations remain exact.
The inspector distinguishes scheduling repair from outcome reconciliation and
native retry, preserving denied/stale states and fencing late Project responses.
