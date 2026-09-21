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
