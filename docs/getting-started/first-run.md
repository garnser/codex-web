# First run

> Applies to: current main  
> Audience: new users  
> Risk: local workspace mutation under the selected sandbox/approval policy

## Goal

Create a project that points at an allowed workspace repository and confirm codex-web can create/resume a native Codex thread without enabling external automation.

## Prerequisites

- codex-web is healthy; see [Install and start](installation.md);
- a repository exists below the configured workspace root;
- the current identity is allowed to create/manage projects.

## Create the first project

Use the Projects UI and create a project with:

- **Name:** a human-readable project name;
- **Path:** the repository path below the configured workspace root;
- **Sandbox:** keep `workspace-write` for the starter path;
- **Approval policy:** keep `on-request`.

The canonical API is `POST /api/projects`; the UI uses the same project model.

## Start a thread

Select the new project, create a thread and ask for a read-only orientation first, for example:

```text
Inspect this repository and summarize its structure. Do not change files.
```

Approve nothing you do not understand.

## Expected result

- the project appears in `GET /api/projects`;
- a native Codex thread is created for that project;
- the response refers to the selected repository rather than another host path.

## Verify

Check:

```bash
curl -fsS http://127.0.0.1:8765/api/projects
curl -fsS http://127.0.0.1:8765/api/healthz
```

Then continue in the browser and confirm the thread can be resumed.

## Optional: configure an authoritative task source

A project can have exactly one authoritative external task source. This is **not required** for the first successful local task.

When you later configure one, the binding is canonical project state and contains provider identity/scope plus a secret **reference**, never raw credential material. Use the project/task-source administration UI or `PUT /api/projects/{project_id}/task-source`.

Read [Task-source contract](../architecture/task-source-contract.md) before enabling synchronization.

## Failure modes and recovery

If the repository path is rejected, fix the workspace root/mount; do not work around it by mounting the entire host filesystem. If a thread cannot start, re-check `/api/healthz` and Codex authentication.

## Next

Complete [First successful task](../tutorials/first-successful-task.md).
