# First successful task

> Applies to: current main  
> Audience: new users  
> Risk: local repository write under `workspace-write` + `on-request`

## Goal

Complete one small, reviewable repository change and verify it locally without enabling unattended production actions.

## Prerequisites

- [First run](../getting-started/first-run.md) completed;
- the project points at the intended repository;
- the working tree is in a state you are comfortable modifying;
- you understand that repository content is untrusted data and cannot grant codex-web authority.

## Choose a bounded task

Use a change that is easy to inspect and verify. Example:

```text
Add a short DEVELOPMENT.md file explaining how to run the existing test command.
Inspect the repository first, make only that documentation change, and show me the diff.
```

Do not use the first tutorial for infrastructure credentials, deployment, destructive migrations or production changes.

## Execute

1. Open the project.
2. Create a new Codex thread.
3. Submit the bounded task.
4. Review any approval request before accepting it.
5. Inspect the resulting diff.
6. Run the repository's existing validation command if the task or agent did not already do so.
7. Ask Codex to summarize what changed and what verification ran.

## Expected result

A small file change exists only in the selected workspace repository, the thread records the interaction, and you can identify the verification result.

## Verify

Use normal repository truth:

```bash
git status --short
git diff --check
git diff
```

Then run the repository-specific test/lint command documented by that repository.

A successful model response is **not** verification by itself. Repository tests, provider receipts and canonical Evidence are the preferred proof as workflows become more autonomous.

## Failure modes

| Symptom | Response |
| --- | --- |
| agent touches unexpected files | reject/stop, inspect the diff, revert unwanted changes |
| command requires more privilege than expected | reject it and narrow the task/sandbox |
| validation fails | keep the task open; diagnose rather than declaring success |
| runtime outcome is unknown | use the relevant recovery/reconciliation UI; do not blindly replay non-idempotent actions |

## Recovery

For this starter task, recovery is ordinary version-control recovery: revert the local change or reset the file after reviewing what would be lost.

## What this proves

You have verified the core path:

```text
project -> thread/request -> bounded execution -> repository change -> validation -> result
```

External task sources, ActionProviders, approvals, Evidence, Goals/Decisions and autonomy extend this path; they do not replace its canonical boundaries.
