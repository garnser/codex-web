# Isolated execution workspaces and leases

Every mutable execution owns a canonical execution workspace and resource lease. Parallel runnable work must never silently share one mutable checkout.

## Workspace identity

An execution workspace is identified deterministically from organization, tenant workspace, canonical `ExecutionSubject`, and execution ID. Repeating acquisition for the same live execution is idempotent; a terminal execution ID cannot be silently reused.

Supported subject kinds are explicit durable contract values:

- `work_item:<ref>` for canonical Work Item execution;
- `thread:<thread-id>` for an already-created Codex thread;
- `thread_bootstrap:<bootstrap-id>` for isolated thread creation before Codex has returned the canonical thread ID.

The canonical record includes:

- execution subject and execution identity;
- legacy `work_item_ref` only when the subject kind is `work_item`;
- Project and canonical Resource IDs;
- owner identity;
- workspace kind;
- lease ID/mode/expiry;
- isolated path and deterministic branch for Git work;
- exact base/head revision;
- requested/observed resource usage;
- lifecycle/error state;
- structured integration outcome.

## Thread bootstrap identity

The pinned Codex app-server generates the canonical thread ID during `thread/start`. Isolated execution therefore cannot honestly use a `thread:<thread-id>` subject before that RPC completes.

A control-plane generated immutable bootstrap ID is used as `thread_bootstrap:<bootstrap-id>` to acquire the workspace and worker assignment first. The returned Codex thread ID is then recorded in a separate immutable, tenant-scoped bootstrap binding that references the existing execution, assignment, and execution workspace.

The original assignment/workspace subject is never rewritten to the returned thread ID. This preserves deterministic IDs and historical provenance. A later lookup may map the thread ID back to the still-live bootstrap assignment/session; conflicting rebinding or cross-tenant lookup fails closed. If the isolated process/session is lost and cannot be safely resumed, recovery must mark/fail the canonical execution rather than falling back to the control-plane Codex runtime.

Bootstrap and ordinary `thread` subjects never synchronize Work Item execution state. Only `work_item` subjects populate or update legacy Work Item execution references.

The bootstrap assignment is a bounded **thread-session execution**, not a per-turn execution. The current local implementation caps its assignment deadline, workspace lease, CPU budget and wall-clock budget at 86,400 seconds (24 hours), matching the existing worker/workspace contract maxima. Its worker lease is still renewed and its fence, delegated auth, worker trust and resource bounds are revalidated before every Codex RPC.

Normal terminal turn events clear only the active-turn record for a bootstrap-bound thread; they do not complete the bootstrap assignment or stop its isolated app-server. Later turns and inactive thread RPCs resolve the returned thread ID back to that same live bootstrap assignment/session. Ordinary pre-existing threads without a bootstrap binding retain the bounded compatibility metadata path until their next independent `thread:<thread-id>` execution.

If the process/session disappears, the durable binding remains evidence that the thread belongs to an isolated bootstrap execution. Requests fail explicitly rather than starting a fresh private CODEX_HOME or falling back to the control-plane Codex runtime. Safe process restart/resume of that private CODEX_HOME is intentionally outside this contract.

## Repository target provenance and multi-repository members

The execution binding resolves a canonical repository target before workspace
acquisition. The resulting assignment records the exact
`RepositoryExecutionTarget` alongside the normal Resource IDs so audits can
distinguish Work Item, explicit, thread-profile, routing-rule, and single-repo
selection.

A filesystem ExecutionWorkspace records each repository as an
`ExecutionWorkspaceMember` with canonical Resource ID, source path, access
mode, isolated worktree path, sandbox-visible path, base/head revisions, branch
identity where mutable, and disk usage. Exactly the selected repository is
writable by default. Authorized sibling repositories are provisioned as detached
worktrees and exposed to the local worker through read-only Bubblewrap mounts
under `/mnt/codex-context/<resource-id>`; the Project root itself is never
mounted as a compatibility shortcut.

The workspace keeps one fenced canonical lease but records a per-resource mode.
Conflict detection is resource-specific: read/read overlap is permitted, while
any overlapping write lease fails closed. Existing single-repository workspaces
retain the legacy top-level path/branch/base/head projection and gain one
equivalent member record.

Repository filesystem sources come from canonical Resource filesystem/path
aliases. Relative aliases are resolved beneath the canonical Project root and
are rejected if symlink resolution escapes that root. The selected primary
repository retains the historical Project-path fallback for compatibility;
read-only sibling repositories require an explicit canonical filesystem source.

## Resource leases

Leases are reserved transactionally in SQLite before provisioning.

- read/read leases may coexist;
- any overlapping write lease conflicts;
- resource-scoped conflicts fail before a worktree is created;
- tenant and identity quotas are evaluated against active leases;
- workspace resource count and requested disk usage are bounded;
- provisioning failure marks the workspace `error` and releases the reserved lease.

This prevents two agents from accidentally receiving write authority over the same canonical Resource.

## Git isolation

Repository resources use `LocalGitWorkspaceBackend`:

1. resolve and pin an exact base revision;
2. create a deterministic branch from the execution subject ref plus execution hash;
3. create an isolated Git worktree beneath the private execution-workspace root;
4. record the exact path/base/head revision canonically.

The backend never uses shell interpolation.

Cleanup removes the worktree. Normal/expired cleanup keeps the branch so crash recovery does not destroy unmerged work; an explicit discard may delete the branch.

## Non-Git resources

Canonical resources that are not repository checkouts use `resource_lease` workspaces. They receive the same ownership, expiry, concurrency and audit semantics without inventing a filesystem checkout.

## Renewal, expiry and crash recovery

Owners or tenant administrators can renew or release a workspace. Expired leases are deterministically:

1. marked released with reason `lease-expired`;
2. associated workspace marked `abandoned`;
3. abandoned Git worktree cleaned while preserving its branch;
4. canonical Work Item execution reference updated.

Startup invokes expiry recovery, and operators can invoke the tenant-scoped recovery endpoint explicitly.

## Integration outcomes

Merge/rebase results are structured state rather than agent prose. `WorkspaceIntegrationState` records strategy, outcome, target/result revision, explicit conflicts, actor and timestamp.

A conflict requires explicit conflict paths/details and moves the workspace to `conflicted`. Successful merge/rebase/fast-forward requires a resulting revision and moves it to `integrated`. Cleanup/release remains a separate explicit action.

## Execution contract

Work Item execution lifecycle contains a compact `ExecutionWorkspaceReference`. Execution contract schema **1.3** exposes it as `target.workspace`, including workspace/lease identity, canonical Resource IDs, branch, base/head revision and lease expiry.

This makes every executing agent able to identify its isolated mutable workspace and pinned base without reconstructing it from prompts.

## API

- `GET/POST /api/execution-workspaces`
- `GET /api/execution-workspaces/{workspace_id}`
- `GET /api/execution-workspaces/{workspace_id}/events`
- `POST /api/execution-workspaces/{workspace_id}/renew`
- `POST /api/execution-workspaces/{workspace_id}/integration`
- `POST /api/execution-workspaces/{workspace_id}/release`
- `POST /api/execution-workspaces/recover`

#141 may project these lifecycle records into Operations UI. The UI must not infer workspace ownership or merge status from terminal logs.
