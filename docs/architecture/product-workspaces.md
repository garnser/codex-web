# Product workspaces and shared explainability UI

## Purpose

The frontend has two equally important interaction modes:

- fast conversational Threads for direct work;
- first-class product workspaces for canonical state, administration, operations,
  explainability and governed actions.

The workspace shell is a composition layer over existing domain APIs and UI
modules. It does not own a second copy of domain state.

## Composition rule

A workspace must use one of two patterns:

1. **Delegate** to an existing canonical domain dialog/launcher, such as Inbox,
   Goals, Decisions, Metrics, Company Operations or Memory.
2. **Adopt** an existing mature administration card by moving the live DOM node
   from the Developer surface into its product workspace.

Adoption preserves the original IDs, event listeners, API requests and mutation
controls. The workspace shell does not clone forms or reconstruct their state.

Debug-only tools may remain in Developer. Mature canonical domain controls
should not.

## Workspace information architecture

The current shell exposes:

- Overview;
- Inbox / Attention;
- Projects;
- Threads;
- Work;
- Goals;
- Decisions;
- Metrics / KPIs;
- Company Operations;
- Organization / Roles;
- Definitions / Contracts;
- Resources;
- Integrations / Extensions;
- Agent Providers / Sessions;
- Workers / Execution;
- Operations / Observability;
- Memory;
- Autonomy;
- Settings / Security.

The shell supports direct navigation, a workspace switcher, responsive sidebar
shortcuts, Ctrl/Cmd+K keyboard access and hash deep links such as
`#workspace/resources`.

Projects and Threads remain directly accessible in the sidebar rather than being
forced through management screens.

## Canonical UI vocabulary

Shared UI primitives distinguish the reason a capability is unavailable or
constrained:

- **Definition** — the contract/behavior revision that exists;
- **Authorization / policy** — whether an identity/role may act;
- **Configuration / rollout** — whether the feature is configured/enabled here;
- **Entitlement** — whether the tenant may use it;
- **Budget / quota** — whether bounded capacity remains;
- **Compatibility / version skew** — whether versions can interoperate;
- **Health / capability** — whether the provider/worker/extension can serve it;
- **Evidence / verification** — whether required proof exists.

These concepts must not be collapsed into generic “settings” or “not allowed”
messages. Visual treatment includes text/shape differences rather than relying
on color alone.

The shared `CodexProductUI` browser API exposes concept badges, canonical
status badges, provenance trails and workspace navigation for domain modules
that need the same vocabulary.

## Explainability path

The Overview workspace documents the shared stored-provenance path:

1. trigger / source;
2. identity / authentication assurance;
3. owner / role;
4. exact definition revision;
5. model / execution-agent routing;
6. effective policy / configuration / entitlement;
7. target resource;
8. execution contract / worker;
9. ApprovalRequest / quorum;
10. ActionIntent / provider receipt;
11. Evidence / verification;
12. result.

“Explain Action” delegates to the canonical Autonomy Control Center and its
stored ActionIntent provenance. Navigation and refresh do not invoke a model.

## Sensitive references

The workspace shell never renders secret values or cryptographic key material.
Existing Secrets/Credentials and Encryption Key controls are adopted intact and
continue to expose reference/metadata only.

Tenant/workspace scope, identity assurance, canonical IDs and lifecycle/status
metadata remain visible so operators can understand why an action is permitted,
blocked or unavailable.

## Dynamic modules

Some domains install cards after initial page load. A MutationObserver adopts
matching mature cards as they appear, including Agent Providers/Sessions,
Autonomy Control Center and Orchestration Inspector.

The observer only reparents DOM nodes. It does not fetch domain data or create
backend state.

## Refresh semantics

Opening an adopted workspace may invoke the existing card's deterministic
Refresh control. This uses the same canonical APIs as before and must not trigger
model/provider reasoning merely to make UI state fresh.

Domain event/data-driven refresh remains owned by each domain module.

## Accessibility and responsive behavior

- all workspace navigation is native button/dialog UI;
- Ctrl/Cmd+K or **All workspaces** opens command search and focuses its input;
- Up/Down selects results, Enter opens a destination, Escape dismisses and restores focus;
- dialogs retain normal Escape behavior;
- workspace selection has `aria-current`;
- mobile layouts collapse to one-column navigation/content;
- status/concept meaning is conveyed in text and shape, not color alone.

## Developer surface

Developer remains available for diagnostics such as daemon information, route
tests and raw communication/audit debugging. Product domains are progressively
removed from Developer as their canonical UI matures.

This keeps Developer useful without making it the permanent information
architecture for production features.

## Command palette

The existing workspace switcher uses a shared, code-owned UI command model
(`command_palette.js`). Sources register a stable ID and a synchronous projection
of existing controls or already loaded canonical data. Commands describe a label,
scope, availability predicate and navigation callback. This is UI plumbing, not an
operational Definition catalog or a new authority system; stored definitions and
plugins cannot inject executable callbacks.

Project-scoped results must match the current Project. Navigation and Project
switching reuse the shell's existing handlers; cross-Project switches are labeled
explicitly. Loaded Thread and Work Item sources retain their originating scope;
Thread indexing is invalidated when the Project changes, and Work Item opening
refreshes canonical state and revalidates the requested Project and item. The
palette does not fetch a global object index. Recent command IDs are bounded to
eight and kept only in memory; labels and authorization are resolved afresh.

Availability is checked when listing and again before execution. Administration
uses the existing canonical identity-context decision and starts hidden until
that decision is loaded. Navigation is not authorization: destination API checks
remain authoritative. Only navigation, Project switching and Thread search are
common commands; mutations retain their existing review/approval paths. Another
modal blocks palette opening so confirmations cannot be obscured.
