# Workspace workflows

This guide follows the current Codex Web workspace shell. Keep the active **Project** visible while moving between **Home**, **Work**, **Team**, **Automation**, **Operations**, and **Organization**. Use **Ctrl+K** (or **⌘K** on macOS) to jump directly to a workspace.

The UI is a view over canonical state. A button, badge, or panel does not grant authority, and provider-owned state remains authoritative in the provider that owns it.

## Work: inspect and operate on a Work Item

Open **Work** when you need to understand what is happening with a task or execution.

1. Open the Work workspace.
2. Use **Work Items** to open the canonical Work Item operator.
3. Select the relevant Project and Work Item.
4. Read the summary first: current state, owner, priority, next action, blocker, and routing lanes.
5. Inspect the **Run timeline** for execution state, repository attribution, usage, Evidence, verification, retries, and external actions.
6. Use **Retry** or **Reconcile** only when the canonical action is enabled and its remediation applies.

A blocked or partial Run is not success. Repository tests, provider receipts, and canonical Evidence are stronger proof than an agent message saying the task is complete.

Screenshot fixture: **work-items-and-handoffs**.

## Team: understand Agents, Teams, and Skills

Open **Team** when the question is who performs work and which reusable capability is attached to that work.

- **Agent** identifies the stable collaborator. Provider, model, and runtime are execution metadata rather than the Agent's identity.
- **Team** shows bounded membership and delegation relationships.
- **Skill** shows the reusable, versioned procedure and the exact revision available to execution.

Use these views to understand ownership and provenance. Do not treat Team membership or Skill text as a way to expand canonical authority.

Screenshot fixture: **agent-team-skill**.

## Automation: inspect scheduled or event-driven work

Open **Automation** for governed scheduled/event-driven behavior and autonomy controls.

1. Identify the trigger and enabled/paused state.
2. Confirm the Agent or Team that will execute the work.
3. Review authority, approval requirements, budgets, retries, and concurrency.
4. Follow linked Work Items/Runs for actual execution history.
5. For external side effects, inspect the ActionIntent, provider receipt, and Evidence rather than assuming a trigger means the action completed.

Paused automation should start no new Runs. Unknown external outcomes require reconciliation rather than blind replay.

Screenshot fixtures: **orchestration-and-actions** and **autonomy-control-center**.

## Attention: process work that requires a person

Open **Attention** for items that genuinely require human intervention, such as approval, missing information, policy denial, runtime remediation, repeated failure, or incident response.

1. Read the reason, severity, source, and requested human action.
2. Follow the source link to the related Work Item, Run, Approval, Incident, or diagnostic context when needed.
3. Use only the actions offered by the canonical source flow.
4. Resolve the underlying condition; do not dismiss a blocker merely to make the Inbox quiet.

Routine successful progress belongs in Work/Run history, not in the Attention Inbox.

Screenshot fixture: **attention-inbox**.

## Operations: diagnose runtime, provider, and worker health

Open **Operations** when execution cannot start, is degraded, or needs operator remediation.

The overview summarizes canonical runtime readiness, provider delivery state, worker lifecycle/capability, failures, and remediation. From there you can navigate to Runs/Work, Attention, Incidents, Workers, Evidence, or runtime enrollment.

Use the canonical reason/remediation fields rather than interpreting raw error strings. Credentials and bearer-token material are intentionally not rendered.

For a new runtime, use the existing **Add runtime** / enrollment flow. Do not manually manufacture worker state.

Screenshot fixture: **runtime-operations**.

## Home: orient before drilling down

**Home** is the bounded orientation view for the active Project. It summarizes current work, Attention, agent activity, Goals, and automation without replacing the detailed canonical surfaces.

Use Home to decide where to go next:

- active or blocked task → **Work**;
- human intervention → **Attention**;
- Agent/Team/Skill ownership → **Team**;
- scheduled/event-driven behavior → **Automation**;
- runtime/provider/worker problem → **Operations**.

Screenshot fixture: **home-overview**.

## Mobile and narrow screens

The same workflow concepts remain available on narrow screens. Shared cards collapse to a single column, controls remain reachable, and navigation keeps the active Project context.

Screenshot fixture: **mobile-navigation**.

## Reproducible screenshots

All screenshot names above come from sanitized deterministic browser fixtures. Generate the current set with:

```bash
npm ci
npx playwright install chromium
npm run docs:screenshots
```

See [Reproducible documentation screenshots](../screenshots/README.md) and the [screenshot catalog](../screenshots/catalog.md).

## Related

- [Workspace navigation](workspace-navigation.md)
- [Getting started](../getting-started/README.md)
- [First successful task](../tutorials/first-successful-task.md)
- [Operations](../operations/README.md)
- [Core concepts](../core-concepts/README.md)
