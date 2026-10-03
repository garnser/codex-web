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
shortcuts and Ctrl/Cmd+K keyboard access. Every Project workspace has a stable
`/projects/{project_id}/{page}` route, also supported beneath the deployment mount
prefix. The server serves the shell only for supported page names; unknown pages
remain 404. Legacy hash entries such as `#workspace/resources` migrate to their
corresponding page. Workers have a distinct route from Operations, so reload and
browser history preserve the selected surface.

A Project route supplies navigation context, not resource ownership or authority.
Skills, Integrations, Workers, Resources and Organization retain their canonical
organization/workspace scope and label it explicitly. Definitions display selected
Project and inherited shared records. Domain APIs remain responsible for actor,
tenant and resource authorization; route names never grant access.

Projects and Threads remain directly accessible in the sidebar rather than being
forced through management screens.

Company Operations and Memory expose organization/workspace projections, including
facts or links from multiple Projects in that tenant. Their scope labels and
navigation commands must state that shared scope even when reached from a Project
route. Changing the selected Project does not turn these canonical shared APIs
into Project-filtered APIs or change the actor's tenant authority.

## Unsaved editor state

Substantive editors use the main-content page rather than a modal window.
`page_editor.js` moves the live editor DOM into the active workspace, temporarily
hides its originating cards, and restores those cards, scroll position and focus
on Back. It preserves canonical API handlers and form ownership. Project/page
navigation closes the presentation after the shell's dirty-state guard; it does
not transfer unsaved values to another Project. The standalone fallback is also
a full-width, non-modal page. This is presentation state, not a second resource
or authority store.

Resource create/edit/relationship and Definition/Configuration forms share
sticky primary-action bars. Resource records containing editors are not bounded
by the log viewer's height or scrolling rules. Long text fields can grow and
resize vertically; the page is the enclosing scroll container. Agent Profile,
Team, Skill-assignment, membership, consumer and revision views reuse the same
page presentation. Executive consultations, the company operating view and Bot
Integration also use this surface. Bot credential inputs are write-only and clear
on close; they never enter dirty-editor snapshots or browser draft storage.

Routed Goals, Decisions, Metrics, Company Operations, Memory, Attention and
Project Setup already adopt their canonical domain DOM non-modally.
Direct Work Item commands preserve their requested target while entering the
same routed Work Items page; post-creation and programmatic Project Setup
launches also enter that routed page instead of bypassing it with a modal. Mature
administration editors remain in their adopted workspace cards. Lightweight
Project creation, command selection and deliberate lifecycle/approval
confirmations remain transient dialogs. Those dialogs must not become containers
for unrelated substantial editing workflows.

Substantive editors can register with `dirty_editor.js`. The shared guard compares
current field values with the editor's saved baseline, exposes a live unsaved
status, and protects workspace/Project navigation, history navigation, and browser
unload. Cancel keeps the form and its values; explicit discard resets the baseline
and lets the editor close. Successful persistence clears the guard, while failed
validation or saving leaves entered values available for correction. Browser
unload warnings remain subject to browser support and user activation.

Automation, Agent Profile/Team, Resource create/edit/relationship forms, and generic
Definition/Configuration draft editors use this contract. Resource catalog display
updates and filters defer while a row has unsaved changes, with an explicit status
and discard action. Read failures retain drafts with an unavailable-state message;
create/relationship selections survive catalog hydration. A successful save clears
only the submitted snapshot, so typing during an outstanding request remains dirty.
Resource metadata remains in page memory and canonical mutation APIs still enforce
authority; deferred display state is not an authorization or concurrency guarantee. Definition and Configuration drafts preserve edited inputs
across same-Project refreshes. Automation retains
the active form across background renders, suppresses same-Project refresh while
editing, and prevents a discarded/detached draft response from continuing into
publication. An authoritative Project change still clears the old Project's
editor; the guard is a user navigation safeguard, never an authorization boundary.

Draft values and baselines remain in page memory only. They are not written to
local/session storage, telemetry, or logs; this also applies to arbitrary structured
payloads that could contain sensitive values. Durable draft recovery requires an
explicit classification/retention contract before an editor opts into it. Other
editors must integrate the shared contract explicitly; ordinary filters and
transient search controls do not register a blocking guard.

## Form validation

`form_validation.js` supplies inline errors, `aria-invalid` and description
associations, an alert summary with keyboard-operable field links, and focus on
the first invalid control (or the summary for a general failure). Clearing a
validation result restores any pre-existing descriptions and validity attributes.
Error text uses text nodes; structured server input values are not rendered.

Automation and typed Configuration editors use the primitive. Predictable native
constraints and Automation trigger dependencies are checked before submission.
Configuration types, ranges, choices and supported scopes come from canonical
configuration specifications. API schema paths are mapped explicitly to controls;
unmapped errors remain visible in the summary. Structured backend errors remain
authoritative, including policy and cross-field incompatibilities. Client checks
neither authorize a mutation nor replace canonical validation.

Rejected submissions retain all entered values. A successful save clears errors;
an error code without useful text receives actionable general guidance instead of
being the sole explanation. Permission/read-only controls keep their existing
canonical availability and explanation paths. This shared contract is qualified
on these representative editors; new substantive forms should use it instead of
adding another validation mechanism.

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
Secrets/Credentials occupy the Project Secrets page with explicit shared
workspace ownership and Project consumer context. Encryption Key controls remain
in Configuration. Both expose reference/metadata only.

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

### Project Overview projection

Overview resolves the requested Project through canonical tenant authorization
before reading summaries. Its work, attention, approvals, incidents, agent
sessions, Goals and schedules are Project projections: records belonging to a
different Project or having no Project association are not counted as this
Project's work. Workspace-wide records remain available through their canonical
domain surfaces, such as Operations and the workspace Inbox. This projection
does not change their ownership, visibility rules or lifecycle.

Project changes invalidate pending Overview responses and clear the previous
rendered state, including when the panel is hidden. No selected Project means
an explicit selection state and no Overview request; it must not fall back to
a synthetic `home` Project. Returned Project identity must match the request
before any summaries or onboarding state are rendered.

## Accessibility and responsive behavior

- all workspace navigation is native button/dialog UI;
- Ctrl/Cmd+K or **All workspaces** opens command search and focuses its input;
- Up/Down selects results, Enter opens a destination, Escape dismisses and restores focus;
- explicit workspace navigation moves focus to the destination heading; the next Tab reaches its controls;
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

Production form controls must retain a programmatic accessible name when
placeholder and tooltip text are absent. Icon-only actions use an explicit
accessible label describing the action. The static markup qualification opens
all forms/dialogs and checks every control's computed name, while browser
interaction qualification covers 320 CSS pixel reflow, enlarged text, reduced
motion and keyboard navigation. These targeted checks cover their stated
behaviors; they do not establish full WCAG conformance.

### Chat and realtime view continuations

Chat actions capture the selected Project visit and Thread selection generation.
Accepted server mutations retain their canonical outcome, but a late create,
rename or archive response cannot change the current conversation after navigation,
even when the user returns to the same Project and Thread. Sending a prompt that
first needs a Thread stops its UI continuation if that creation's view is stale;
it cannot select a different Project's Thread and send the original prompt there.

Narrow realtime Thread/binding refreshes capture Project visit, full-refresh
generation and search context when scheduled. They check that context before the
request and before applying results. A return visit has independent coalescing
keys, so its fresh request is not blocked by an older outstanding response. Global
stream sequence/reconnect reconciliation remains independent of Project visits.
These are display fences; existing canonical APIs retain authorization and action
ownership, and no model calls or durable execution records are added.

The context-compaction panel uses the same Project visit fence plus its Thread
selection generation. Delayed status, success/failure messages and post-compaction
refresh timers cannot change a later conversation. Context/compaction requests
carry the captured Project query context and use the canonical Thread ownership
checks described in the identity/tenancy contract.

Explicit history and preflight refreshes request a scoped conversation reload,
while duplicate route navigation remains a no-op. History controls preserve
keyboard focus when live message maintenance rebuilds their display.

Project Secrets is a stable `/projects/{id}/secrets` page in Project navigation,
with a Secrets command and full-page content. It presents workspace-owned secret
references in a selected-Project consumer context, rather than implying Project
ownership or automatic use authority. Configuration and TaskSource credential
selectors link to this surface; readiness secret remediation opens it directly.
Reference URLs carry only the non-secret reference ID. The canonical lifecycle,
redaction and impact boundaries are described in [secrets.md](secrets.md).

## Contextual object references

`reference_navigation.js` owns typed UI destinations and escaped View links.
It preserves the deployment prefix and selected Project; a link carries no tenant
switch or authority. The canonical target API still decides visibility and
mutation permissions. A known object with no supported editor displays inspection
or ownership guidance instead of an invented Edit operation. Runtime registrations
remain adapter/deployment-owned; immutable invocation/evidence records are
inspection surfaces, not editable execution history.

The reference inventory includes Agent Profile/Team membership and Skill pins,
profile Execution Profile/provider/owner references, Run provider/worker/assignment
and repository references, resource relationships, provider consumers, Secret/key
usage, Definition and Configuration revision chains, and Work Item Goal/Decision
references. Secret, key, Execution Profile and provider helpers share the same
Project path builder. Logical routing-definition/configuration references carry
an explicit revision; record references select their exact record. Skill links
preserve historical pins rather than silently opening the latest revision.

Targets select or focus the canonical object after hydration. A missing or denied
requested detail remains visibly unavailable rather than substituting another
object. Lists stay bounded; an absent reference may require changing filters or
loading more canonical records. Normal anchors preserve browser navigation and
the shared unsaved-change guard. Source object identifiers can be written into
the current history entry so Back restores that object; editor values, credentials,
and arbitrary return URLs are never captured by this mechanism.

### Consequential action review

`action_confirmation.js` provides the shared transient review dialog for consequential
operator actions. This is a presentation contract, not an approval, policy decision,
or ActionIntent. Existing domain APIs retain authorization, step-up, independent
approval, dependency validation, revision checks, durable receipts and reconciliation.
The browser does not infer that a displayed acknowledgement grants permission.

Callers supply an explicit action label, target identity/scope, consequence and a
truthful recovery path. Known canonical impact is included with its visibility and
completeness limits. A failed required impact read blocks submission rather than
presenting zero dependencies. Secret values, token material and key material must
never enter the confirmation; reference IDs and non-sensitive metadata are sufficient.

Presentation risk has two levels. Bounded changes use one review with Cancel as the
initial focus; high-impact changes also require explicit acknowledgement and use
visually distinct action styling. Revocation, destructive lifecycle changes, live
publication/routing changes, authority grants and recovery with irreversible effects
use the high-impact review. Inactive draft creation and read-only validation use their
normal form feedback without another confirmation. Synchronous unsaved-edit navigation
protection remains a separate contract in `dirty_editor.js`.

The same review pattern covers identity/session/token and membership controls, Secret
and Key lifecycle, Resource changes, Agent Profile/Team lifecycle, Execution Profiles,
Definitions/Configuration, provider binding, model routing, entitlements, Work Graph,
extensions, workers/workspaces, artifacts/evidence and recovery operations. Existing
specialized lifecycle forms can use `protectActionDialog` while retaining their own
required reason, dependency and revision controls. These action labels and structural
UI routes are code-owned presentation of existing API contracts, not a second mutable
authority or Definition catalog.

Dialogs render supplied labels as text, trap keyboard focus, support Escape/Cancel,
and return focus to the initiating control. Project/route navigation invalidates the
review, including an A-to-B-to-A visit; callers also fence mutable selection/revision
state where applicable. Dialogs fit narrow viewports and enlarged text. Only a
currently valid accepted review proceeds to the existing canonical API. Recovery text
must distinguish a later authorized replacement/revision from Undo; irreversible
revocation, discarded data and already completed external effects have no invented
Undo path. Uncertain provider results still require canonical reconciliation.

Thread archive offers a direct **Unarchive Thread** recovery control through the
existing canonical restore API, scoped to the Project visit. Knowledge deletion
explains its lack of Undo. Skill publish/archive and pin changes review exact
revisions and available usage. Autonomy resume, scoped unpause and leaving dry-run
or simulation require review. Emergency pause/kill controls remain immediate so
stopping work is not delayed by a modal. Automation publication reviews its Project,
prior revision and resulting lifecycle before creating/publishing the candidate;
ordinary manual run admission still uses its existing canonical gates.

Goal activation/resume, terminal cancellation/completion and execution-binding
resume/cancel/outcome reconciliation use the same review, identifying the Goal
revision, known Project bindings and selected evaluation/binding. Goal terminal
transitions have no Undo and do not rewrite bound Work Items. The Orchestration
Inspector reviews schedule resume/cancel and live autonomy changes; pause/kill stay
immediate. Confirmation never substitutes for provider evidence when reconciling an
unknown runtime outcome.

Skill editing, portable bundle import and verified-procedure promotion use the same
unsaved-edit contract. Invalid payloads and failed saves retain page input; a late
successful save acknowledges only the submitted snapshot and does not erase newer
edits. Selection/Cancel and global navigation require explicit discard. An edited
bundle invalidates its previous preview before submission. Saved inactive Skill
revisions can be reopened from the canonical Skill list after a reload; unsaved
instructions, assets, procedures and imported bundles remain page memory only.

Model Gateway provider/model, prompt-version and routing-policy editors also register
independent dirty state. Source selectors are navigation controls: they are excluded
from value snapshots but must confirm discarding an edited form before loading a
new source. Background catalog hydration defers while any managed form is dirty,
and successful mutations acknowledge only their submitted snapshot. Explicit discard
restores the baseline; failed requests keep the entered values. Late module loading
retains the latest read projection until controls are ready. These browser snapshots
are temporary presentation state, never a second registry or authorization source.

Extension configuration-reference and resource-grant selections use the same
page-memory dirty-state contract. Refresh defers while selections are unsaved;
explicit discard restores their baseline. Saves retain failed input and acknowledge
only submitted selections, so a late success cannot erase newer edits. Only reference
IDs enter these forms; no secret material or browser draft storage is introduced.
