# Codex Web documentation

This is the user/operator documentation entry point. Architecture documents describe durable technical contracts; these guides explain how to install, operate and adopt the product safely.

## Start here

- [Getting Started](getting-started/README.md) — install, start, verify health, create the first project and run the first task.
- [Core Concepts](core-concepts/README.md) — the product mental model, canonical state, authority and execution boundaries.
- [Tutorials](tutorials/README.md) — complete end-to-end learning paths.
- [How-to](how-to/README.md) — focused task-oriented procedures.
- [Administration](administration/README.md) — identity, secrets, resources, providers, policy and privileged configuration.
- [Operations](operations/README.md) — health, backup/recovery, incidents, capacity and upgrades.
- [Reference](reference/README.md) — API/configuration/version documentation.
- [Troubleshooting](troubleshooting/README.md) — common failure modes and recovery.
- [Extension developer guide](extensions/developer-guide.md) — manifest, compatibility, permissions, lifecycle, migrations and publication.
- [Advanced Adoption](advanced-adoption/README.md) — maturity-based rollout from interactive use to controlled autonomy.
- [Worked Examples](examples/README.md) — success, refusal, blocker, approval and recovery scenarios.
- [Reproducible Screenshots](screenshots/README.md) — sanitized fixture-based UI capture and refresh workflow.
- [Workspace navigation](ui/workspace-navigation.md) — current Home/Work/Team/Automation/Operations shell, Project switching and Run inspection.
- [Workspace workflows](ui/workspace-workflows.md) — practical Work Item/Run, Agent/Team/Skill, Automation, Attention and Operations walkthroughs.

## Choose a path

| You are… | Recommended path |
| --- | --- |
| Trying codex-web for the first time | [Getting Started](getting-started/README.md) → [First successful task](tutorials/first-successful-task.md) |
| A developer integrating a provider | [Core Concepts](core-concepts/README.md) → [Architecture index](architecture/README.md) |
| An administrator | [Trust and credentials before privileged actions](administration/trust-and-credentials.md) → Administration |
| An operator | Operations → Troubleshooting → [Autonomy architecture](architecture/bounded-autonomy.md) |
| Rolling out autonomy gradually | [Advanced Adoption](advanced-adoption/README.md) → Autonomy Control Center |
| Maintaining the documentation | [Guide template](guide-template.md) → [Versioning and review](reference/documentation-versioning.md) |

## Documentation contract

User-facing guides should state:

1. prerequisites;
2. exact steps;
3. expected result;
4. verification;
5. common failure modes;
6. recovery/rollback;
7. related concepts and deeper reference.

Use [guide-template.md](guide-template.md) for new procedural documentation.

## Canonical truth

Documentation must distinguish three layers:

- **Canonical codex-web state** — identity, policy, work, approvals, ActionIntents, Evidence, lifecycle and other control-plane truth.
- **External provider state** — GitLab, Slack, model/action providers, workers and other systems remain authoritative for the state they own.
- **Advisory model output** — model text can recommend or interpret; it does not grant authority or become canonical state merely by being generated.

When a guide conflicts with the application contract, tests and versioned architecture documentation are authoritative. Open an issue to correct the guide.
