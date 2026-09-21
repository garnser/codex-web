# First run

> Applies to: current main  
> Audience: new users  
> Risk: local workspace mutation only after Project readiness succeeds

## Goal

Create one Project from a repository beneath the approved workspace root, let Codex Web establish the canonical repository topology, verify Project execution readiness, and only then start the first thread.

## Prerequisites

- [Install and start](installation.md) completed;
- `GET /api/livez` succeeds;
- `GET /api/readyz` succeeds;
- a Git repository exists beneath the configured workspace root;
- the current identity may create/manage Projects.

## 1. Create the first Project

Use **Project Setup** in the browser.

For the starter path choose:

- **Name:** a human-readable Project name;
- **Path:** the Git repository beneath the configured workspace root;
- **Sandbox:** `workspace-write`;
- **Approval policy:** `on-request`.

The manifest/CLI is **not required** for this simple first run.

Fresh Project creation creates or reuses the canonical repository Resource, binds it to the Project and runs the supported fresh bootstrap/readiness evaluation. You do not need to invent or edit Resource IDs.

The equivalent API is `POST /api/projects`; its response can include `freshBootstrap` details alongside the Project.

## 2. Verify Project readiness

Find the Project ID in the UI or `GET /api/projects`, then query:

```bash
curl -fsS http://127.0.0.1:8765/api/projects/<project-id>/readiness
```

**Expected result:** `semantic_ready` and `execution_ready` are true and the overall status is ready (or otherwise explicitly non-blocking for the selected topology).

Do not substitute `/api/healthz`, `codexReady: true`, or a successful browser load for this check.

### If readiness is blocked

Read the returned `checks[]`, especially `code`, `message`, `remediation`, and `remediation_route`.

Common starter blockers include:

- repository path/target missing or ambiguous;
- worker capability missing;
- sandbox/profile incompatibility;
- workspace path outside the approved root.

Use [Project readiness troubleshooting](../troubleshooting/project-readiness.md). Do not edit SQLite, compatibility JSON, worker capabilities, or Resource IDs by hand.

## 3. Run a read-only orientation turn

Only after Project readiness succeeds, create a thread and ask:

```text
Inspect this repository and summarize its structure. Do not change files.
```

**Expected result:** the thread executes against the selected canonical repository target and does not require unrelated external integrations.

## 4. Verify the thread

Confirm that:

- the selected Project/repository shown in the UI is the one you intended;
- no unexpected repository target or host path was selected;
- a blocked execution appears with a structured preflight blocker/remediation rather than silently doing nothing;
- the thread can be reopened/resumed.

## Optional: authoritative task source

A local first task does **not** require GitLab/Jira/ServiceNow or another TaskSource.

When you later bind an authoritative TaskSource, use a canonical SecretReference rather than raw credential material. Read [Task-source contract](../architecture/task-source-contract.md) before enabling synchronization.

## Recovery

If Project creation succeeded but readiness is blocked, keep the Project and remediate the blocker; do not delete/recreate it to bypass canonical state.

Fresh bootstrap is idempotent and can be rerun through the supported Project setup/bootstrap surfaces.

## Next

Complete [First successful task](../tutorials/first-successful-task.md).
