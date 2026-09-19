# Configure the first authoritative task source

> Applies to: current main  
> Audience: administrators adopting external work intake  
> Risk: reads/synchronizes an external task system; credentials are provider-scoped

## Goal

Bind one project to an authoritative external task source and verify that codex-web can inspect/synchronize it without creating a second source of work-item truth.

## Prerequisites

- [First run](../getting-started/first-run.md) is complete;
- the target project already exists;
- the external provider is reachable;
- required provider credentials are configured through the supported secret/configuration boundary;
- the current identity may administer the project.

GitLab is the currently operational built-in task-source path. The TaskSource contract is provider-neutral.

## Understand the boundary first

A project can have exactly one authoritative task source. The external system remains authoritative for its provider-owned issue/task record. Codex-web deterministically projects that record into canonical Work Item state and records provenance/reconciliation information.

Do not put a raw API token in a task-source payload. Where the provider contract supports a canonical SecretReference, store only that reference.

## Inspect available task sources

In the operator UI, open the project/task-source administration view. The canonical catalog is also exposed by:

```text
GET /api/task-sources
```

Confirm the intended provider/source instance is present before binding it.

## Bind the project

Use the project administration UI, or the canonical endpoint:

```text
PUT /api/projects/{project_id}/task-source
```

The provider-neutral binding identifies:

- `source_type`;
- `source_instance`;
- provider scope;
- credential secret reference when supported/required;
- typed non-secret provider settings where applicable.

For GitLab, use the source instance/scope that matches the configured GitLab integration and the project/group you intend to treat as authoritative.

## Synchronize

Trigger synchronization from the Work Items / Task Source operator UI, or:

```text
POST /api/work-items/sync/{project_id}
```

Synchronization is a reconciliation operation. It should project external records into canonical Work Items rather than mutating lifecycle state through an integration-specific shortcut.

## Expected result

- the Project shows one authoritative task-source binding;
- `GET /api/task-sources` shows the configured source;
- synchronization reports discovered/projected work;
- resulting Work Items retain external source identity/provenance.

## Verify

Inspect:

```text
GET /api/projects
GET /api/task-sources
GET /api/work-items?project_id={project_id}
```

Open one projected Work Item and confirm its canonical lifecycle/provenance matches the external record you selected.

## Failure modes

| Symptom | Likely cause | Recovery |
| --- | --- | --- |
| provider missing from catalog | provider/integration is not configured | configure the integration before binding |
| authentication failure | credential missing/revoked/wrong scope | repair the credential in its authoritative secret/provider system |
| project/source not found | wrong external scope or project binding | correct the binding; do not invent a local duplicate |
| drift/conflict | provider and canonical state disagree | use reconciliation/operator state; do not overwrite blindly |

## Recovery

To stop using the authoritative source, remove the project binding with the UI or:

```text
DELETE /api/projects/{project_id}/task-source
```

Removing the binding does not delete the external provider's issues/tasks.

## Related concepts

- [TaskSource contract](../architecture/task-source-contract.md)
- [GitLab task-source adapter](../architecture/gitlab-task-source-adapter.md)
- [Canonical Work Item lifecycle](../architecture/work-item-lifecycle.md)
