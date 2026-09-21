# Canonical legacy-state materialization

This runbook covers the cross-domain materialization step for imported or
pre-Docker codex-web installations. It is distinct from the multi-repository
thread/profile conversion described in
[multi-repository Projects](multi-repository-projects.md): this workflow
creates the canonical Project-domain records that current runtimes require.

The current materializer contract is version **1.0** and supports legacy
layouts compatible with pre-Docker / v0.1-era state. Unsupported or ambiguous
records are reported rather than guessed.

## What it materializes

For one Project, the workflow inventories and, where deterministic, creates or
updates:

- explicit Organization/Workspace ownership;
- repository Resources and Project/Resource bindings;
- Work Item repository `resource_ids` and tenant scope;
- the authoritative Project GitLab TaskSource binding;
- a canonical GitLab SecretReference backed by SecretBroker storage.

Historical Attention and Approval state is **not** inferred from incomplete
legacy history. Those domains are reported as skipped with explicit reason
codes so the migration cannot fabricate an approval decision, authority grant,
or historical user action.

## Security boundary

Dry-run and reports contain references and metadata only. Raw credentials are
never serialized into the plan, report, Project, Work Item, logs, or ordinary
configuration.

When a supported legacy GitLab token exists, apply moves it behind
SecretBroker and stores only the resulting SecretReference ID in the Project
TaskSource configuration. The TaskSource runtime then resolves that reference
through `SecretBroker.use_async` for each provider call. Once a canonical
credential reference exists, the GitLab runtime does not consult the legacy
raw-token path.

Repository discovery does not follow symlinked directories. Existing
non-generic tenant ownership is never silently reassigned. `Local / Default`
may be migrated only into the explicitly authenticated target tenant; choosing
to keep `Local / Default` itself requires explicit confirmation.

## Dry run

Use the Project materialization plan endpoint:

```http
POST /api/projects/<project-id>/canonical-materialization/plan
Content-Type: application/json

{
  "confirm_generic_target": false
}
```

The response contains:

- the versioned machine-readable plan;
- counts for `migrated`, `unchanged`, `skipped`, `unresolved`, and
  `operator_action_required`;
- a human-readable report;
- `blocked: true` when unresolved/operator-action records exist.

Planning is read-only: it does not create Resources, Secrets, bindings, Work
Item associations, migration executions, or provider-side mutations.

Review every `unresolved` and `operator_action_required` record before
apply. Common blockers include ambiguous multi-repository Work Items, missing
GitLab credentials, conflicting TaskSource bindings, and pre-existing
non-generic tenant ownership.

## Apply

Submit the exact plan returned by dry-run:

```http
POST /api/projects/<project-id>/canonical-materialization/apply
Content-Type: application/json

{
  "plan": { "...": "the complete dry-run plan" }
}
```

Apply requires administrator authority through the normal Projects API policy.
On first apply the service recomputes the plan and rejects a stale plan before
mutation. Each operation has a stable ID and checkpoints after completion.

The materializers are idempotent:

- existing repository Resources are reused by canonical filesystem alias;
- Project/Resource bindings are reused;
- migration-created GitLab SecretReferences are reused by provider/purpose;
- Work Item associations converge on the same Resource;
- an already-applied plan returns the existing execution.

## Resume after interruption

Retrieve status:

```http
GET /api/projects/<project-id>/canonical-materialization/status
```

A partially applied execution retains its original plan and completed
operation IDs. Resubmit the **same plan** to the apply endpoint. Resume skips
checkpointed operations and continues from the remaining operation boundary.

Do not generate a new plan merely to conceal an interrupted execution. Generate
a new plan when the source/canonical topology intentionally changed and the
previous plan is no longer the desired migration.

## Multi-repository mapping

The materializer preserves every discovered Git repository. GitLab routing
paths are associated with repositories only when deterministic:

- one repository plus one GitLab project path; or
- an exact repository-directory / GitLab project-leaf match.

A Work Item is mapped from, in order:

1. its `project_path` when it falls inside one discovered repository;
2. GitLab source identity or issue-ref prefix matching a canonical GitLab
   repository alias;
3. the sole Project repository, when exactly one exists.

If multiple repositories remain possible, the Work Item is reported as
`work_item_repository_ambiguous` and is left unchanged.

## Credentials and TaskSource recovery

If GitLab routing exists but no canonical or supported legacy credential can
be found, the report contains `gitlab_credential_missing`. Provide a
canonical GitLab SecretReference before retrying.

If multiple source groups are observed, the report contains
`task_source_scope_ambiguous`. Select the authoritative GitLab scope
explicitly rather than allowing migration to choose one.

An existing conflicting authoritative TaskSource is
`operator_action_required`; migration never overwrites it silently.

## Tenant recovery

A Project already owned by a non-generic Organization/Workspace cannot be
moved by this workflow. Resolve ownership explicitly first.

If a `local/default` Project already has canonical Resources in that legacy
tenant, the workflow also refuses to move those Resource identities
implicitly. Reconcile those Resources explicitly before applying the new
tenant scope.

## Rollback boundary

Materialization is forward/idempotent rather than a destructive history
rewrite. It does not delete legacy source state automatically. This provides a
recovery window while the canonical state is qualified.

Secret material moved into SecretBroker should not be copied back into Project
files or ordinary configuration. If rollback to an older release is required,
use the normal verified backup/rollback procedure in
[upgrade and rollback](upgrade-and-rollback.md) and treat any credentials
created after the backup as credentials that may require rotation.

The higher-level bootstrap planner, manifest CLI, stale-plan orchestration, and
cross-domain audit execution are owned by the bootstrap framework. This
materializer supplies its deterministic domain operations to that framework.
