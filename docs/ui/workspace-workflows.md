# Workspace workflows

This guide follows the current Codex Web workspace shell. Keep the active **Project** visible while moving between **Home**, **Work**, **Team**, **Automation**, **Operations**, and **Organization**. Use **Ctrl+K** (or **⌘K** on macOS) to jump directly to a workspace.

The UI is a view over canonical state. A button, badge, or panel does not grant authority, and provider-owned state remains authoritative in the provider that owns it.

## Project setup: become ready and start useful work

1. Confirm the active Project in the shared Project switcher. Routed pages inherit this scope; changing it is the deliberate way to move work between Projects.
2. Start at **Home** for current work and the Project readiness next step. If readiness is blocked, open **Project Setup**, inspect canonical checks, and use the offered remediation or deterministic plan.
3. Review any authority-sensitive changes and required approvals before applying a plan. A successful setup operation is not a substitute for refreshed readiness.
4. Once execution-ready, open **Threads** to begin interactive work or **Work Items** to continue a canonical task. Explicit repository targeting remains a required safety step when Project policy requires it.
5. Confirm the useful outcome from the resulting Work Item/Run and its Evidence. A Thread creation, UI click, or “ready” Project alone is not a completed outcome.

## Work Items: create or continue a canonical task

1. Open **Work Items** from the active Project's Work navigation. The routed operator inherits the shell Project; do not select the same scope again inside the page.
2. Search or select a Work Item, or use its canonical task-source flow to create/ingest work.
3. Read canonical state, owner, priority, next action, blockers, external-source state, and policy before execution.
4. Execute or continue only through the enabled canonical action. Readiness, authorization, repository targeting, approvals, and execution contracts are required governance steps, not removable UX friction.
5. Reconcile external-source state when it differs from canonical Work Item state; neither projection should be misrepresented as the other.

## Threads: start scoped interactive work

1. Open **Threads** inside the active Project. Create a Thread or select one from that Project's list.
2. Confirm the repository target when required and review the effective execution policy before sending work.
3. The routed Chat URL identifies the selected Thread within the Project. Back/Forward restores prior selections; switching Project never reuses another Project's Thread.
4. Confirm outcomes through the canonical Run/Work Item and Evidence rather than relying on a transcript assertion.

A missing Thread deep link, lost Project scope, repeated Project choice, or inability to tell which repository will be modified is accidental friction. Explicit target selection required by policy is intentional governance.

## Runs: understand status and choose the next action

1. Open **Runs / Execution** from the active Project or follow a Run link from its Work Item.
2. Confirm the Run's terminal/current status, owning Work Item, runtime/worker, repository scope, and attempt history.
3. Inspect bounded events, provider/action receipts, artifacts, verification, and Evidence as needed; do not infer success from an agent message or a running state.
4. Use the canonical retry, reconcile, approval, or remediation action shown for that outcome. Unknown external outcomes require reconciliation rather than blind replay.

## Configuration: change and verify effective state

1. Open **Configuration** for typed Project configuration or the relevant resource surface for contextual settings.
2. Inspect scope, source/inheritance, effective value, version, and policy before editing.
3. Use Edit, Attach/Detach, Reset, or lifecycle actions only where the canonical resource supports them; authorization remains server-owned.
4. Reopen or refresh the canonical view and verify the effective state and provenance after mutation. Secret/key material remains behind its protected boundary; inspect references/status, never raw values.

The number of scope re-selections and page transitions is a useful baseline for workflow review, but approvals, explicit repository choice, and verification are not accidental clicks to optimize away. A context loss, unclear next action, or repeated entry of already-known values is a candidate for remediation.

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
