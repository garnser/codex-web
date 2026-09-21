# Project bootstrap manifest and CLI

Codex Web supports a versioned `ProjectBootstrap` manifest for declaring the intended Project topology without embedding credentials. The manifest is an operator input; it does not grant authority.

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

Repository IDs must be unique. At most one repository may be marked `default`. `repositorySelection: single` requires exactly one repository; `repositorySelection: default` requires exactly one default repository. Multi-repository `explicit` manifests remain valid without choosing a repository implicitly.

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

## Apply the supported legacy materialization slice

The current apply bridge deliberately reuses the canonical materializer rather than introducing a parallel migration engine:

```bash
./codex-web bootstrap \
  --project my-project \
  --manifest .codex/project.yaml \
  --migrate-legacy \
  --apply
```

Apply resolves the local administrator in the manifest Organization/Workspace and therefore requires an existing authorized membership for that scope. It refuses to fabricate tenant authority.

Before mutation, the command verifies that the manifest Project name, sandbox, repository topology, and TaskSource intent align with the deterministic canonical materialization plan. Any mismatch is returned as a typed `blocked` result instead of silently rewriting the manifest or choosing a repository.

The underlying canonical materialization execution is durable, idempotent, and resumable. Its execution ID is returned as `bootstrapExecutionId`. Re-running a converged materialization does not duplicate Resources, Project bindings, SecretReferences, or Work Item associations.

Desired-state changes that require the broader reconciliation planner—such as changing canonical Project fields, non-GitLab TaskSource conversion, or Slack integration reconciliation—fail closed rather than being silently ignored.

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
