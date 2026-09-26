# Shared workspace components

`static/workspace_components.js` and `static/workspace_components.css` define the small shared presentation layer for human/Agent workspaces. The module consumes already-authorized canonical/view-model state and returns DOM nodes; it does not fetch data, make policy decisions, infer readiness, or own lifecycle state.

The stylesheet consumes the `--cw-*` design tokens documented in [design-tokens.md](design-tokens.md). Product surfaces should use these primitives when the same interaction or semantic treatment appears in more than one domain, and keep genuinely domain-specific visualization local.

## Component set

| Primitive | Purpose |
| --- | --- |
| `statusBadge(status, label?, options?)` | Semantic health/lifecycle state with text plus structural/icon cues. |
| `identityChip(identity, options?)` | Consistent person, Agent, Team, or service identity treatment; may include canonical status. |
| `objectHeader(viewModel)` | Page/object heading with description, identity, status, and caller-supplied actions. |
| `metadataGrid(entries)` | Responsive definition-list metadata for provider/model/runtime, Work Item, Run, evidence, and similar facts. |
| `statePanel(viewModel)` | Shared loading, empty, degraded, error, and offline state surface. |
| `actionFeedback(viewModel)` | Accessible acknowledged, in-progress, succeeded, failed, and needs-attention action result. |
| `skeleton(options?)` | `aria-busy` loading placeholder that does not expose decorative rows to assistive technology. |
| `timeline(entries)` | Ordered activity/history sequence. |
| `provenanceDisclosure(viewModel)` | Keyboard-native `details/summary` disclosure containing a shared timeline. |
| `surfaceCard(viewModel)` | Base Work/Run/Approval/Attention/Evidence style card accepting caller-supplied body/footer nodes. |

`statusFamily(status)` is exported for surfaces that need to align an existing legacy class with the shared positive/warning/negative/neutral mapping during migration.

## View-model contract

Callers pass display-ready values and optional DOM actions. Components never decide whether an action is authorized or whether a canonical transition is valid. A disabled or omitted action therefore comes from the surface/controller after it receives canonical authorization/capability state.

Identity input supports `id`, `label`/`name`/`display_name`, `kind`/`type`, and optional `status`. A component may display a canonical status but must not derive one from unrelated client observations.

## Accessibility and responsive behavior

Status meaning is not encoded only by color: status badges retain text and a symbol. Failed action feedback uses an assertive alert; other action feedback uses a polite status announcement. Error/offline state panels use `role="alert"`; normal state panels use `role="status"`; loading/skeleton surfaces use `aria-busy`. Native buttons and `details/summary` preserve keyboard semantics, and shared interactive surfaces use the design-token focus ring.

At the documented 640px phone breakpoint, headers/cards stack, metadata grids collapse to one column, and actions remain visible rather than being silently removed.

## Current adoption and migration

The product workspace shell consumes the shared status primitive. Agent Providers/Sessions consumes shared identity and status primitives. Existing Attention, Approval, Work Item, Artifact/Evidence, and other page-local card/pill/state implementations remain valid during migration but are identified for replacement under the UI migration cleanup work rather than being removed in one risky change.

Representative browser coverage lives in `tests/browser/workspace_components.spec.js`. It covers semantic state families, human/Agent/Team identity rendering, loading/error/offline semantics, keyboard focus order, and phone-width layout.
