# Legacy multi-repository Project migration

Use the legacy migration workflow when an existing codex-web Project points at a
directory that contains one or more Git repositories and persistent native Codex
threads predate canonical repository/execution-profile binding.

The workflow migrates **codex-web execution authority and topology metadata**. It
does not rewrite native Codex thread IDs or thread history.

## Dry-run first

Open **Developer → Legacy Project Migration**, choose the Project, and select
**Dry run**. The corresponding API is:

`POST /api/projects/{project_id}/legacy-migration/dry-run`

Dry-run is read-only. It:

- walks the Project root without following symlinked directories;
- discovers independent Git roots from `.git` directory/file markers;
- resolves existing canonical repository Resources by their `filesystem` alias;
- inventories Project-associated persistent threads from the thread index and
  bot bindings;
- proposes one repository target or an orchestration-only profile per thread;
- reports `converted`, `blocked`, `unchanged`, and
  `operator_action_required` counts;
- reports the effective authority difference for material sandbox/profile
  changes;
- creates no Resource, Project binding, thread setting, compatibility mapping,
  external-provider mutation, or migration-status record.

A thread with multiple possible repositories and no deterministic cwd/path,
existing canonical target, or orchestration role is
`repository_target_ambiguous` and remains blocked. Migration never asks a model
to choose execution authority.

## Thread preservation

Migration keeps the native thread identity. It does not archive, fork, replace,
rollback, compact, rename, or recreate the Codex thread and does not rewrite the
thread index.

Only canonical run settings required for safe isolated execution are changed:

- sandbox, when an explicit conversion requires it;
- repository Resource target;
- read-only repository context;
- execution profile.

Existing model, reasoning-effort, developer-instruction, native history,
project/thread association, bot binding IDs, external conversations, delivery
routing and thread names remain intact unless a separate operator workflow
changes them.

## Sandbox and execution-profile conversion

`workspace-write` and `read-only` retain their sandbox mode when a repository
target can be selected deterministically.

For legacy `danger-full-access`, migration does **not** assume that old
effective authority is equivalent to the current contained
`danger-full-access` implementation. When the legacy thread lacks already
canonical repository/profile scope, dry-run reports a material authority
difference:

- legacy effective host/filesystem authority cannot be proven equivalent;
- current `danger-full-access` remains inside the assigned worker/workspace
  boundary;
- repository mutation scope becomes explicit.

That conversion is `operator_action_required` and apply returns
`migration_authority_approval_required` until an administrator explicitly
checks **Approve material authority changes** (or sends
`approve_material_authority_changes=true`).

Master/orchestrator bindings map structurally to the canonical
`orchestration-only` execution profile. They receive no mutable repository
target.

## Apply, interruption and idempotence

Apply uses the exact dry-run plan:

`POST /api/projects/{project_id}/legacy-migration/apply`

The plan has a stable plan ID. Apply persists a migration execution before
mutating canonical state and records stable operation checkpoints:

- `repository:{repository-key}`;
- `thread:{thread-id}`.

Repository creation is idempotent through the canonical filesystem alias, and
Project binding is already idempotent. If the process stops after a Resource is
created but before later thread conversions, rerunning the **same plan** resumes
the persisted execution rather than creating another Resource.

An already-applied plan returns the existing applied execution. A plan that has
not started and no longer matches current discovery/settings fails with
`migration_plan_stale` and must be dry-run again.

## Compatibility window

Apply may create an explicit, time-bounded mapping from each legacy absolute
repository path to its canonical Resource. The default UI window is seven days;
operators may select none, one day, seven days, or thirty days.

Mappings are metadata, not authority grants. They:

- are tenant/workspace scoped;
- point only to the canonical Resource materialized by migration;
- expire at a concrete timestamp;
- do not bypass repository selection, Project binding, worker capabilities,
  sandbox/network policy, or leases.

The diagnostic endpoint is:

`GET /api/legacy-migration/path-compatibility?path=...`

An expired mapping resolves as unavailable.

## Rollback boundary

Dry-run is fully reversible because it performs no mutation.

After apply has created canonical Resources/Project bindings or changed thread
run settings, rollback is **compensating-only**. The migration does not promise
transactional restoration of all pre-migration topology. The persisted report
therefore records this boundary explicitly.

Native Codex thread IDs/history remain untouched, so a topology rollback never
requires inventing or restoring a different thread history.

## Status and troubleshooting

Use:

`GET /api/projects/{project_id}/legacy-migration/status`

Each execution reports:

- plan/version;
- status (`planned`, `applying`, `partial`, `applied`, `blocked`);
- operator approval identity/time when required;
- completed operation IDs;
- active compatibility mappings;
- last apply error;
- the original dry-run plan and rollback boundary.

For a blocked thread, correct the canonical topology or make repository/profile
scope explicit, then create a fresh dry-run. Do not bypass the blocker by
editing migration state or by relying on legacy absolute paths as authority.
