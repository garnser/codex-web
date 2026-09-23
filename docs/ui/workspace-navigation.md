# Workspace navigation

Codex Web groups day-to-day work into a small set of workflow-oriented workspaces. The active **Project** remains visible while you move between them.

## Primary workspaces

- **Home** — a bounded overview of current work, Attention, approvals, incidents, Agents, Goals and recent automation activity.
- **Work** — Work Items, Threads, execution state, Runs, blockers, handoffs and verification.
- **Team** — Agents, Teams and Skills used to carry out work.
- **Automation** — scheduled/event-driven automation and autonomy controls.
- **Operations** — runtime, provider, worker, incident and health surfaces.
- **Organization** — policy, identity, Resources, configuration and administrative controls.

Use the Project switcher to change canonical Project context. Project switching does not create a second client-side source of truth; the application reloads the relevant canonical view for the selected Project.

## Fast navigation

Press **Ctrl+K** (or **⌘K** on macOS) to open workspace search. You can also use the workspace launcher in the top bar.

Deep links use the current shell and preserve Project context. Browser Back/Forward should return you through workspace navigation rather than forcing a full application reload.

## Work Item and Run inspection

Open **Work** when you need to understand or operate on a task. Use the **Work Items** action in that workspace to open the canonical Work Item operator. A Work Item shows canonical work state, authoritative external-source state, execution policy/contract information, diagnostics and history. Its Run timeline shows bounded execution records and provenance rather than replaying unbounded history in the browser.

When a Run is active, live updates refresh the affected Run/Work Item state incrementally. Missing or unavailable auxiliary data should degrade the relevant panel rather than make the entire workspace unusable.

For end-to-end walkthroughs of Work, Team, Automation, Attention and Operations, see [Workspace workflows](workspace-workflows.md).

## State and authority

The workspace UI presents canonical state; it does not grant authority itself. Disabled or absent actions reflect canonical authorization, readiness or capability state. Provider-owned facts remain provider-owned and are reconciled into Codex Web rather than inferred from UI state.

## Narrow screens

At phone widths, navigation and shared cards collapse to a single-column layout. Actions remain reachable rather than being silently removed. Use the same workflow terminology on desktop and mobile.

## Related

- [Core concepts](../core-concepts/README.md)
- [Getting started](../getting-started/README.md)
- [Workspace workflows](workspace-workflows.md)
- [Shared workspace components](components.md)
- [Reproducible screenshots](../screenshots/README.md)
