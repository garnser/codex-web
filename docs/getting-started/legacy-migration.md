# Legacy / imported installation

> Applies to: imported, recovered, or pre-bootstrap installations  
> Audience: administrators/operators  
> Risk: canonical migration and compatibility changes

## Goal

Convert legacy/imported state into canonical Project domains without guessing repository authority, losing auditability, or blocking the HTTP runtime on pathological historical state.

Do not use this path for a fresh installation.

## 1. Back up before mutation

Back up the application state volume/database and any compatibility files required by the release you are upgrading from. For Docker/Compose, see [Persistent data](../../DOCKER.md#persistent-data).

Record the source release/image and rollback target before applying migration.

## 2. Inspect Project readiness

```bash
curl -fsS http://127.0.0.1:8765/api/projects/<project-id>/readiness
```

A migrated process can be alive and the Codex runtime healthy while the Project is still semantically incomplete. Treat readiness blockers as the authoritative migration worklist.

## 3. Scaffold or prepare a bootstrap manifest

A manifest declares desired topology and contains references only—never raw passwords, tokens, private keys, or API keys.

For a simple repository topology:

```bash
python -m codex_web.cli bootstrap \
  --project <project-id> \
  --manifest .codex/project.yaml \
  --scaffold \
  --repository <repository-path>
```

Review the generated YAML before continuing.

## 4. Dry-run legacy materialization

```bash
python -m codex_web.cli bootstrap \
  --project <project-id> \
  --manifest .codex/project.yaml \
  --migrate-legacy \
  --dry-run
```

Dry-run validates the manifest and approved workspace paths without persistent mutation.

For richer semantic preflight/plan details, use the Project Setup UI or the Project bootstrap API endpoints described in [Project bootstrap](../operations/project-bootstrap.md).

## 5. Review authority/resource conversions

Legacy sandbox/profile values are not silently reinterpreted. A material authority change can require explicit operator approval.

Resolve ambiguous repository mappings rather than choosing the first repository. Preserve multi-repository topology where present.

## 6. Apply or resume

```bash
python -m codex_web.cli bootstrap \
  --project <project-id> \
  --manifest .codex/project.yaml \
  --migrate-legacy \
  --apply
```

If the reviewed plan requires an authority conversion, rerun only after review with:

```text
--approve-authority-changes
```

There is no separate `--resume` flag. The apply engine is resumable/idempotent; after an interruption, rerun the same reviewed apply. If the plan is stale, generate and review a new plan rather than forcing the old one.

## 7. Inspect large historical state

If preflight reports pathological journals, registries, or historical state, use the supported inspection/compaction workflow before enabling expensive background reconciliation. Destructive compaction requires its own dry-run/backup/integrity evidence.

## 8. Verify readiness

After apply:

```text
GET /api/projects/<project-id>/readiness
```

Do not proceed to executable turns until required semantic/execution checks are ready. Warnings may remain if they are explicitly non-blocking; blockers may not.

## Rollback boundary

Rollback is release/state-version dependent. Restoring an older image without restoring compatible state can reintroduce unsupported semantics. Follow the release/recovery runbook for the exact version being rolled back.

## Next

- Bootstrap CLI/API details: [Project bootstrap](../operations/project-bootstrap.md)
- Blocked readiness: [Project readiness troubleshooting](../troubleshooting/project-readiness.md)
- Recovery/release concerns: [Operations](../operations/README.md)
