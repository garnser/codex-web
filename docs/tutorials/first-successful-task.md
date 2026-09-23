# First successful task

> Applies to: current main  
> Audience: new users  
> Risk: local repository write under `workspace-write` + `on-request`

## Goal

Complete one small, reviewable repository change and verify it locally without enabling unattended production actions.

## Prerequisites

- [First run](../getting-started/first-run.md) completed;
- `GET /api/projects/{project_id}/readiness` reports the Project execution-ready;
- the Project points at the intended canonical repository target;
- the working tree is in a state you are comfortable modifying;
- repository content is treated as untrusted data and cannot grant Codex Web authority.

If readiness is blocked, stop here and follow [Project readiness troubleshooting](../troubleshooting/project-readiness.md).

## Choose a bounded task

Use a change that is easy to inspect and verify. Example:

```text
Add a short DEVELOPMENT.md file explaining how to run the existing test command.
Inspect the repository first, make only that documentation change, and show me the diff.
```

Do not use the first tutorial for infrastructure credentials, deployment, destructive migrations or production changes.

## Execute

1. Open the verified-ready Project and confirm it in the Project switcher.
2. Open **Work**. Use **Threads** for the conversational task, and use **Work Items** when you need canonical task/Run inspection.
3. Create a new Codex thread and submit the bounded task.
4. Review any approval request before accepting it.
5. If execution preflight blocks the turn, follow its remediation and use the retained turn's authorized Retry action rather than submitting duplicate work.
6. Inspect the resulting diff.
7. Run the repository's existing validation command if the task or agent did not already do so.
8. Ask Codex to summarize what changed and what verification ran.

## Expected result

A small file change exists only in the selected repository execution workspace/target, the thread records the interaction, and you can identify the verification result.

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
| Project becomes readiness-blocked | resolve the returned readiness check before retrying |
| agent touches unexpected files | reject/stop, inspect the diff, revert unwanted changes |
| command requires more privilege than expected | reject it and narrow the task/profile |
| execution preflight blocks the turn | follow the typed remediation and use retained-turn Retry after fixing the cause |
| validation fails | keep the task open; diagnose rather than declaring success |
| runtime outcome is unknown | use the relevant recovery/reconciliation UI; do not blindly replay non-idempotent actions |

## Recovery

For this starter task, repository recovery is ordinary version-control recovery: inspect and revert the local change you no longer want.

For execution/retry state, use the canonical retained-turn/recovery controls rather than manufacturing a new execution identity.

## What this proves

You have verified the supported first-user path:

```text
install/start
  -> application readiness
  -> Project creation/bootstrap
  -> Project execution readiness
  -> thread/request
  -> bounded execution
  -> repository change
  -> validation
  -> result
```

External TaskSources, ActionProviders, approvals, Evidence, Goals/Decisions and autonomy extend this path; they do not replace its canonical boundaries.

For the current shell and the handoff from conversational work into Work Item/Run inspection, see [Workspace workflows](../ui/workspace-workflows.md).
