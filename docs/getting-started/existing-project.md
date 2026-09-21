# Existing Project reconciliation

> Applies to: current main  
> Audience: administrators/operators  
> Risk: canonical Project topology changes

## Goal

Reconcile an already-canonical Project after its repository path, mount, worker, sandbox, or repository topology changes without creating duplicate Resources or bypassing readiness checks.

## 1. Inspect Project readiness

Open the Project Setup/Readiness surface or query:

```bash
curl -fsS http://127.0.0.1:8765/api/projects/<project-id>/readiness
```

**Expected result:** the response identifies the Project, `semantic_ready`, `execution_ready`, overall `status`, and stable checks/blockers.

**Verification:** do not infer readiness from `/api/healthz` or `codexReady`.

**If blocked:** follow each check's `remediation` / `remediation_route`. Do not create a second repository Resource just to make the blocker disappear.

## 2. Reconcile desired topology

For routine single-repository changes, use Project Setup and point the Project at the repository beneath the configured workspace root. The bootstrap/reconciliation engine reuses canonical Resources/bindings when their identity still matches.

For declarative or multi-repository changes, use the [Project bootstrap manifest and CLI](../operations/project-bootstrap.md).

Always run a dry-run before applying a topology change:

```bash
python -m codex_web.cli bootstrap \
  --project <project-id> \
  --manifest .codex/project.yaml \
  --dry-run
```

## 3. Apply and verify

After reviewing the plan:

```bash
python -m codex_web.cli bootstrap \
  --project <project-id> \
  --manifest .codex/project.yaml \
  --apply
```

Rerunning the same apply is supported and should not create duplicate canonical Resources or bindings.

Then re-check:

```text
GET /api/projects/<project-id>/readiness
```

Continue only when the Project is execution-ready for the work you intend to run.

## Common failures

| Blocker | Meaning | Recovery |
| --- | --- | --- |
| repository target missing/ambiguous | Project has no deterministic mutable target | fix repository bindings/default/explicit target selection |
| worker capability missing | no eligible worker proves the required execution capability | repair/reconcile the worker; do not edit persisted capabilities manually |
| sandbox/profile unsupported | requested authority cannot be represented safely | select a supported execution profile or review an explicit authority conversion |
| credential reference missing | runtime/task-source reference cannot be resolved | restore the canonical SecretReference; never put raw secret material in the manifest |
| bootstrap plan stale | state changed after review | generate a new plan/dry-run and review it again |

## Next

After readiness is restored, resume normal work or run [First successful task](../tutorials/first-successful-task.md) if this is the first post-reconciliation execution.
