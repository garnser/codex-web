# Project readiness troubleshooting

Use this guide when Codex Web is running but a Project cannot safely start executable work.

## Identify the failing layer

```text
GET /api/livez
GET /api/readyz
GET /api/projects/{project_id}/readiness
```

- `/api/livez` proves process liveness only.
- `/api/readyz` proves application/runtime readiness.
- Project readiness proves semantic/execution prerequisites for one Project.
- `/api/healthz` and `codexReady` are diagnostics, not Project execution gates.

## Read the Project readiness response

A readiness snapshot includes:

- `semantic_ready`
- `execution_ready`
- overall `status`
- `checks[]` with stable `id`, `domain`, `status`, `code`, and `message`
- optional `remediation` and `remediation_route`
- `blockerCount` and `warningCount`
- bootstrap/migration verification metadata when present

Example shape:

```json
{
  "project_id": "example",
  "semantic_ready": false,
  "execution_ready": false,
  "status": "blocked",
  "checks": [
    {
      "id": "repository-target",
      "domain": "resources",
      "status": "blocked",
      "code": "repository_target_ambiguous",
      "message": "No deterministic repository target is available.",
      "remediation": "Select or bind the intended repository target."
    }
  ],
  "blockerCount": 1,
  "warningCount": 0
}
```

The exact checks depend on Project topology. Use returned codes/messages rather than matching the example literally.

## Common blocker families

| Blocker family | Recovery |
| --- | --- |
| repository missing/ambiguous/unauthorized | fix the canonical Resource binding/default/explicit target selection |
| worker unavailable/capability missing | repair or enroll a qualified worker and let its probe advertise capabilities |
| sandbox/profile incompatible | select a supported profile or review the explicit migration conversion |
| credential/SecretReference missing | restore the canonical reference; never paste raw secrets into Project state |
| bootstrap/migration incomplete | run Project Setup or the reviewed bootstrap plan/apply flow |
| TaskSource unavailable | repair the authoritative source binding when that source is required |
| workspace/path outside approved root | fix the configured workspace mount/root; do not mount the whole host |
| lease/capacity blocked | release/expire conflicting execution state or wait for eligible capacity |

## Do not repair canonical state by hand

Do not edit SQLite, compatibility JSON, Resource IDs, worker capability rows, or bootstrap checkpoint records manually. Use Project Setup, bootstrap/reconciliation APIs, or the supported CLI.

## After remediation

Re-run:

```text
GET /api/projects/{project_id}/readiness
```

Only then retry executable work. If a submitted turn was retained by execution preflight, use its authorized Retry action rather than submitting a duplicate side effect.
