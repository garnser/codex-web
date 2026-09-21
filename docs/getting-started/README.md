# Getting Started

Codex Web has three onboarding paths. Choose the one that matches the state you actually have; do not mix legacy migration steps into a fresh first run.

## A. New user / fresh Project

Use this path when you have a clean Codex Web state and one Git repository beneath the configured workspace root.

1. [Install and start](installation.md).
2. Authenticate the Codex CLI/runtime.
3. Mount or select the workspace containing the repository.
4. [Create the first Project through guided setup](first-run.md).
5. Let fresh Project bootstrap create or reuse the canonical repository Resource and Project binding.
6. Verify `GET /api/projects/{project_id}/readiness`.
7. Resolve any blocker before execution.
8. Run the first read-only orientation turn.
9. [Complete one small verified write task](../tutorials/first-successful-task.md).

For this common path, a bootstrap manifest is optional. A new user should not need to know or edit a repository Resource ID.

## B. Existing canonical Project

Use [Existing Project reconciliation](existing-project.md) when the Project already has canonical state and you changed a repository path, mount, worker, sandbox, or topology.

The safe pattern is:

```text
inspect readiness
    -> reconcile canonical topology
    -> rerun bootstrap/reconciliation idempotently
    -> verify readiness
    -> execute
```

Do not create duplicate Resources to “fix” a stale path.

## C. Legacy / imported installation

Use [Legacy/imported installation](legacy-migration.md) when state predates the canonical Project/bootstrap model or is being recovered from an older deployment.

The operator path is:

```text
backup
    -> bootstrap dry-run
    -> semantic preflight/plan
    -> apply or resume
    -> Project readiness verification
    -> large-state review/compaction when indicated
```

## The three readiness layers

| Endpoint | What it proves | What it does **not** prove |
| --- | --- | --- |
| `GET /api/livez` | the web process is alive | runtime or a Project can execute |
| `GET /api/readyz` | application/runtime and canonical storage are ready | a specific Project has executable topology |
| `GET /api/projects/{project_id}/readiness` | Project semantic/execution prerequisites | that a future task will succeed |

`/api/healthz` and `codexReady: true` are not substitutes for Project readiness.

## Minimal safe starter configuration

Keep the first deployment local and conservative: loopback bind, SQLite canonical state, in-process event transport, `workspace-write`, `on-request`, no required external TaskSource/action provider, and no production autonomy.

## Next

Fresh users: [Install and start](installation.md).  
Operators: [Project bootstrap manifest and CLI](../operations/project-bootstrap.md).  
Blocked setup: [Project readiness troubleshooting](../troubleshooting/project-readiness.md).
