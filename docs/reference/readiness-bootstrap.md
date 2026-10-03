# Liveness, readiness and bootstrap reference

## Runtime endpoints

| Endpoint | Scope | Meaning |
| --- | --- | --- |
| `GET /api/livez` | process | HTTP process is alive |
| `GET /api/readyz` | application/runtime | canonical storage/runtime prerequisites for serving work are ready |
| `GET /api/healthz` | diagnostics | deeper component/daemon health; not a Project execution gate |
| `GET /api/projects/{project_id}/readiness` | Project | semantic/execution readiness for one Project |

Do not infer Project execution readiness from `codexReady: true`.

## Project readiness contract

Current readiness contract version is `1.0`.

Important fields:

- `project_id`, `organization_id`, `workspace_id`
- `semantic_ready`
- `execution_ready`
- `status`: `ready`, `warning`, `blocked`, or `not_applicable`
- `checks[]`
- `bootstrap_version` / `bootstrap_execution_id`
- `migration_version`
- `last_successful_verification_at`
- `correlation_id`
- API convenience counts: `blockerCount`, `warningCount`

Each check includes a stable ID/domain/status/code/message and can include affected identifiers, remediation, remediation route, and structured details.

## Bootstrap API

```text
POST /api/projects/{project_id}/bootstrap/preflight
POST /api/projects/{project_id}/bootstrap/plan
POST /api/projects/{project_id}/bootstrap/apply
GET  /api/projects/{project_id}/bootstrap/status
```

Apply recomputes the plan and compares it with the caller's reviewed `expected_plan_id`; stale plans fail rather than silently applying a different operation set.

The Project Setup UI clears prior readiness, topology, plan and action feedback
when Project scope changes. Read requests are aborted and generation-fenced;
late readiness or bootstrap responses cannot overwrite the new Project or
change its execution gate. Cleared scope issues no request and keeps execution
controls gated. Pending mutations retain their initiating Project; navigation
does not claim to cancel or roll back an already-submitted canonical apply.

The normalized manifest submitted for planning remains attached to the reviewed
plan in the UI. Apply captures that manifest, plan ID and the operator's explicit
authority-change approval before rendering progress, so rendering cannot reset
the submitted inputs. Canonical plan comparison and authorization remain the
enforcement boundary.

## Bootstrap CLI

The repository launcher and `python -m codex_web.cli` expose the same command contract:

```text
./codex-web bootstrap --project ID --manifest FILE --scaffold --repository PATH
./codex-web bootstrap --project ID --manifest FILE --dry-run
./codex-web bootstrap --project ID --manifest FILE --apply
```

Optional operator switches include `--migrate-legacy`, `--approve-authority-changes`, and `--output json`.

There is no separate resume flag. An interrupted apply is resumed by rerunning the same reviewed apply; stale plans require a new review.

See [Project bootstrap](../operations/project-bootstrap.md) for procedure-level guidance.

The manifest editor tracks unsaved changes in page memory. Planning, failed
validation/apply and same-Project readiness refresh preserve its raw text, including
invalid JSON being edited. Back and Reset from Project require explicit discard;
global navigation uses the shared unsaved-edit guard. An accepted apply acknowledges
only the submitted text, retaining newer edits as unsaved. Project changes clear the
previous Project draft after the shell's navigation decision. Browser storage does
not retain manifests or credential-shaped input.
