# Project bootstrap manifest and CLI

Codex Web supports a versioned `ProjectBootstrap` manifest for declaring the intended Project topology without embedding credentials. The manifest is an operator input; it does not grant authority.

For the common fresh single-repository first run, the manifest is optional: Project Setup and fresh Project creation perform the supported canonical bootstrap automatically. Use the manifest/CLI when you need declarative setup, reconciliation, multi-repository topology, or legacy migration.

The current contract version is `codex-web/v1`.

## Supported command

From a source checkout, use the executable launcher:

```bash
./codex-web bootstrap --project <project-id> --manifest .codex/project.yaml --dry-run
```

The container image installs the same launcher on `PATH`, so Compose/container operators can use `codex-web bootstrap ...`. `python server.py bootstrap ...` is also supported for source-checkout compatibility.

Dry-run and scaffold dispatch before FastAPI/runtime composition. They validate the manifest and approved filesystem paths without starting providers or mutating canonical state.

## Minimal manifest

```yaml
apiVersion: codex-web/v1
kind: ProjectBootstrap

project:
  name: Veridataops
  organization: veridataops
  workspace: engineering

repositories:
  - id: saas-app
    path: /workspace/veridataops/saas-app
    default: true

taskSource:
  type: gitlab
  authoritative: true
  secretRef: gitlab-primary

execution:
  repositorySelection: single
  requiredCapabilities:
    - command_execution
  sandbox: workspace-write
```

The manifest accepts references only. Fields shaped like raw tokens, passwords, API keys, credentials, private keys, cookies, or authorization values are rejected before schema validation, and validation errors do not echo their values.

`CODEX_WEB_WORKSPACE_ROOT` must be configured for bootstrap manifest path validation. Every declared repository must resolve to an existing Git repository beneath that approved root. Path traversal and symlink escapes are rejected.

Repository IDs must be unique. At most one repository may be marked `default`. `repositorySelection: single` requires exactly one repository; `repositorySelection: default` requires exactly one default repository. Multi-repository `explicit` manifests remain valid without choosing a repository implicitly. Use `repositorySelection: coordinated` to opt the Project into its complete active bound repository set as the writable scope for every repository execution.

## Scaffold a starter manifest

For a deterministic single-repository starter topology:

```bash
./codex-web bootstrap \
  --project my-project \
  --manifest .codex/project.yaml \
  --scaffold \
  --repository /workspace/my-project \
  --project-name "My Project"
```

The starter manifest uses `workspace-write`, requires `command_execution`, and uses a single explicit repository marked as the default. Existing files are not overwritten unless `--force` is supplied.

## Dry run

```bash
./codex-web bootstrap \
  --project my-project \
  --manifest .codex/project.yaml \
  --migrate-legacy \
  --dry-run
```

Use `--output json` for stable machine-readable output. A successful current dry-run proves the v1 manifest and approved repository paths are valid; Project-wide reconciliation/preflight is a separate bootstrap phase and is not inferred from this validation result.

## Full preflight and reconciliation

The CLI `--dry-run` remains the zero-runtime-composition manifest/path validation edge. Full Project preflight runs through the canonical bootstrap service on a running Codex Web instance:

- `POST /api/projects/{project_id}/bootstrap/preflight` validates current database/schema state, tenant ownership, repository topology, execution-target policy, SecretReference authorization, TaskSource health, worker capabilities, sandbox/isolation support, operational-state size, and canonical/legacy migration blockers.
- `POST /api/projects/{project_id}/bootstrap/plan` returns the deterministic desired/current reconciliation plan.
- `POST /api/projects/{project_id}/bootstrap/apply` applies a reviewed plan identity.
- `GET /api/projects/{project_id}/bootstrap/status` returns durable execution/checkpoint/audit state.

Plan operations use `ready`, `create`, `migrate`, `update`, `skip`, `warning`, or `blocked`, with stable reason codes, dependencies, approval requirements, and rollback classifications. An ambiguous tenant, repository, TaskSource, or credential mapping is blocked rather than guessed.

A partial TaskSource/provider outage can be reported as a warning while local reconciliation remains possible. This does not make the Project execution-ready; the post-apply readiness handoff remains authoritative.

## Apply

```bash
./codex-web bootstrap \
  --project my-project \
  --manifest .codex/project.yaml \
  --migrate-legacy \
  --apply
```

Apply resolves the local administrator in the manifest Organization/Workspace and therefore requires an existing authorized membership for that scope. It refuses to fabricate tenant authority and re-evaluates authorization at execution/checkpoint boundaries.

The bootstrap engine delegates canonical Project/Resource/Work Item/Secret migration to the canonical materializer and delegates legacy thread/profile conversion to the legacy migration service. It does not maintain a second domain-specific migration implementation.

Bootstrap execution state is durable. Stable operation IDs and checkpoints allow interrupted runs to resume without duplicating bootstrap audit records or canonical resources/bindings. A Project-scoped lease prevents simultaneous applies; lease-owner fencing prevents a stale executor from overwriting a replacement executor after lease expiry.

There is no separate `--resume` flag. To resume after interruption, rerun the same reviewed apply. If the plan is stale, generate and review a new plan instead of forcing the old plan.

The execution ID is returned as `bootstrapExecutionId`. Re-running a converged plan returns the existing successful execution.

Material execution-authority changes require explicit approval. For example:

```bash
./codex-web bootstrap \
  --project my-project \
  --manifest .codex/project.yaml \
  --apply \
  --approve-authority-changes
```

Use that flag only after reviewing the plan. It is required for changes such as adopting `danger-full-access`.

When a valid canonical GitLab `secretRef` is supplied, bootstrap can use that reference directly instead of copying a legacy token. The provider instance/scope must still be deterministically derivable; conflicting authoritative providers or ambiguous scopes remain blocked.

Slack desired state is preserved in the plan as explicitly delegated/skipped until the dedicated integration reconciliation lifecycle owns that mutation. It is never silently applied by bootstrap.

## Exit codes

| Code | Meaning |
| ---: | --- |
| `0` | valid/ready for the requested supported phase |
| `2` | invalid manifest, path, or scaffold request |
| `10` | completed with warnings |
| `20` | blocked; operator/reconciliation action is required |
| `30` | apply failed |

Machine-readable results include the bootstrap contract version, manifest API version, Project ID, stable manifest digest, source/target version labels, and typed blockers when applicable.

## Secret handling

Raw credentials must never be placed in a bootstrap manifest. Use `secretRef` or another documented reference field. The CLI does not print backend exception text during apply because provider/backend exceptions may contain credential-bearing request details.

Canonical materialization uses the SecretBroker/SecretReference boundary. Secret values are never copied into the manifest, bootstrap output, Project state, Work Items, or ordinary logs.

## Relationship to semantic readiness

A successful manifest validation or materialization execution is not equivalent to Project semantic/execution readiness. Readiness additionally depends on canonical repository bindings, TaskSource and SecretReference state, worker capabilities, sandbox/profile compatibility, unresolved Work Item associations, and other bootstrap checks. Operators should use the Project readiness/bootstrap status surfaces as those phases are enabled.


## Readiness after bootstrap

Bootstrap completion is not the same as Project execution readiness. Operators should distinguish three levels:

- `GET /api/livez` confirms only that the web process is alive.
- `GET /api/readyz` verifies application/runtime readiness and StateStore health.
- `GET /api/projects/<project-id>/readiness` evaluates Project semantic and execution readiness.

Project readiness is derived from canonical state. It checks repository Resources/bindings and deterministic execution targeting, required TaskSource and SecretReference state, worker capability, sandbox compatibility, unresolved bootstrap state, and open Work Item Resource associations. Optional domains are reported as `not_applicable` when the Project does not use them.

Executable thread turns are rejected with a structured `project_readiness_blocked` preflight blocker when Project readiness is unresolved. The response includes the readiness correlation ID, failing check ID/code, remediation route, and the Project readiness URL. Thread-bootstrap execution is intentionally exempt so bootstrap can repair an unready Project.

Readiness diagnostics expose SecretReference identifiers only; raw credential values are never included.


## Fresh Project creation

Creating a Project through `POST /api/projects` also runs the supported fresh-topology bootstrap when the deployment has the canonical bootstrap services composed.

For the common single-repository case, Codex Web discovers the Git repository beneath the approved Project path, creates or reuses its canonical repository Resource, binds it to the Project, and then evaluates Project readiness. The response retains the normal Project fields and adds `freshBootstrap` with materialization and readiness details.

This setup is idempotent. Re-run it explicitly with:

```
POST /api/projects/<project-id>/fresh-bootstrap
```

A missing execution worker does not roll back repository topology: the Project remains configured with its canonical Resource binding and readiness reports the worker blocker before a normal turn can start.

For a Project root containing multiple independent Git repositories, all repositories are materialized and preserved. Codex Web does not pick the first repository as mutable execution authority; readiness remains blocked until deterministic selection/routing is configured.

A non-Git directory remains a configured Project but reports the canonical `repository_missing` materialization/readiness blocker unless a supported orchestration-only topology is configured separately.


## First-user and troubleshooting links

- [Fresh first run](../getting-started/first-run.md)
- [Existing Project reconciliation](../getting-started/existing-project.md)
- [Legacy/imported installation](../getting-started/legacy-migration.md)
- [Project readiness troubleshooting](../troubleshooting/project-readiness.md)
- [Liveness/readiness/bootstrap reference](../reference/readiness-bootstrap.md)
