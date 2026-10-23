# Home operator conversation reference

Home can retain one administrator-selected conversation using `Project.home_operator`.
The reference contains its native thread ID, selecting identity, revision and timestamp.
It is a navigation binding on the existing Project, not a new session, execution role,
agent identity, goal owner or permission grant. Ordinary Projects have no such entry point.
Absent bindings are omitted from existing Project serialization for compatibility.

`GET /api/home/operator` reads the current tenant's Home Project and revalidates the
thread through canonical ThreadScope. An unavailable, foreign, archived or replaced
thread is displayed as unavailable. Its reference remains unchanged; lookup never
creates a replacement, resumes a runner, starts a turn or retries an external action.
Current execution settings are read from the normal thread settings service. Missing
profile selection is shown explicitly, and stored settings are not a claim that an
assignment is currently admitted or that its profile remains executable.

`PUT /api/home/operator` requires the normal canonical administrator authority. The
selected conversation must already be named, unarchived, owned by Home in the actor's
tenant and have an explicitly selected execution profile. The request carries an
`expected_revision`; changing the pointer is an atomic update of the existing Project
record. A stale revision refuses with 409. Clearing the shortcut retains a revisioned
tombstone so an old initial-selection request cannot overwrite a newer choice.
Concurrent fields and other Projects remain unchanged. This operation never modifies
a profile, worker, lease, goal, queue, credential or thread settings.

The Home card opens the existing conversation using its normal deep link and displays
its current profile, sandbox and approval policy. Failed or uncertain selection saves
are never automatically retried. Administrators select a different conversation only
after refreshing the canonical revision. Normal conversation naming, creation, profile
selection, runner inspection and goal controls remain in their existing surfaces.

Home's explicit runner/queue inspection reads the existing scoped Thread runner
and queue routes without loading the transcript or contacting a model provider.
It rechecks the Home selection before displaying results, uses the shared canonical
runner presentation and failure metadata, and never treats a queued request as
active execution. A live observation expires at the earlier lease or 60-second
freshness boundary without another request. Refresh is user initiated; it creates
no polling loop, runner, recovery request, authority grant or execution state.

This contract is the durable entry-point slice of [#1669](https://github.com/garnser/codex-web/issues/1669).
It does not establish host administration capability. The current execution profile
schema explicitly forbids host mutation and network enablement; designating a Home
operator does not override those invariants. Trusted host-access execution, same-native
Goal lifecycle, delegated completion follow-up and deployment checkpoint/resume require
their canonical execution/authority contracts and separate qualification. The feature
issue remains open until its full UI and live acceptance is satisfied.

## Explicit host metadata inspection

The assignment-bound control-plane broker recognizes `operator.host.inspect`
(`GET /api/operator/host`, no query or body). This is a fixed read projection:
current process ID, Python version/implementation, platform release/machine,
and capacity of the filesystem containing the configured Home path. It accepts
no caller paths, shell commands, environment-variable requests or file reads.
Results appear through the normal conversation tool result and broker audit;
there is no second worker, command runner, event loop or scheduler.

Activation is explicit canonical data, never bootstrap defaults:

1. An administrator publishes an exact Home-scoped execution-profile catalog
   whose existing `orchestration-only` profile declares `operator.host.inspect`.
   Scratch/no-repository execution, network-disabled and `host_mutation=False`
   remain enforced; ordinary/global declarations do not activate host inspection.
2. The administrator selects a published Agent Profile with that execution
   profile and an explicit current authority Role. That Role must grant the
   `operator.host.inspect` READ capability through normal authority evaluation.
   This change creates no new binding, wildcard grant or administrator identity.
3. The administrator uses ordinary thread creation/admission, names and selects
   the same conversation in Home. Selection must belong to the assignment's
   human requester; a shortcut selected by another identity confers no authority.
4. Human input uses the existing retained turn route:
   `POST /api/threads/{thread_id}/turns` with `defer_start=true`, the explicitly
   selected Agent Profile, and the original authenticated administrator. Existing
   queue/preflight claim, guarded Codex resume/start and reader receipts carry the
   same original; no new admission subsystem is introduced. Immediate/raw native
   transports without that retained send guard refuse for this declaration. The
   Home selection can retain an explicitly selected Agent Profile and its exact
   revision. The conversation composer reads the current Home selection and adds
   these pins and `defer_start=true` to the same ordinary turn route. It refuses
   unavailable/changed profiles and execution-setting mismatches before sending;
   lookup errors do not fall back to unguarded input. Operator replacement replies
   are never automatically followed by a second submission. The native authority
   guard independently rechecks the selected profile against fresh Home state.
   Saving a navigation-only shortcut retains the existing ordinary behavior.
   Profile pins are omitted entirely for navigation-only references. Activated
   profile-bound references require readers that understand these fields; mixed
   older Home-binding writers are unsupported. Before rolling back to the
   navigation-only engine, drain and reconcile the operator's inputs and actions,
   then explicitly save the same shortcut as navigation-only through its current
   revision. Do not drop the profile fields from live state or infer replay
   permission from a rollback. Selecting a new profile does not transfer pending
   requests or uncertain actions to that profile.
5. The worker uses its existing authenticated assignment broker. Its operation
   catalog advertises inspection only while the declaration/selection is current;
   catalog visibility is not proof of a live lease or effective permission.

Every inspection revalidates the current requester membership, Home selection,
canonical thread/bootstrap association, execution and Agent Profile revisions,
authority Role and operation, assigned registered service worker, credential
metadata when present, fence/deadline/lease, and isolated scratch workspace/lease.
These checks run again after the metadata read; revocation or reselection during
inspection suppresses the result. The configured Home path is rechecked too.
A missing/archived/replaced thread, stale definition, wrong tenant, lost worker
or expired lease refuses without silently choosing a successor or granting access.

The existing common native procedure-admission boundary also checks input to a
profile declaring this operation. The current input actor must be the assignment's
same human requester and remain an administrator with the current profile.
This slice also requires the server-generated `web`/`queued:web` ingress route.
A bot binding's resolved human owner is not evidence of a new human request;
provider/system event sources refuse even when that owner is an administrator.
The HTTP TurnCreate body cannot supply or replace this internal ingress source.
A later member prompt or generic service event cannot borrow the original
administrator's assignment. A missing ingress actor remains missing instead of
being elevated from the fallback control actor. The hook runs before discovery/history/native input
and is shared by queued/resumed turns and the existing provider-neutral session
boundary. The exact same request is revalidated after awaited context preparation
and through the existing synchronous guarded native transport immediately before
resume/input send. This does not repeat procedure discovery or consume history.
A host-enabled input refuses transports without that canonical send guard. Ordinary profiles without this declaration retain their existing path.
It creates no provenance from message text and no extra continuation owner.
Explicit, server-derived cross-project/system delegation remains separate work;
generic bot/service input is not implicitly elevated to the operator's human.

This metadata capability is a useful inspection step, not arbitrary host shell
or filesystem access. Privileged host mutation, native Goal lifecycle, deployment
checkpoint/resume and full live UI qualification remain tracked by #1669 and the
native Goal control contract. No runtime capabilities or live profiles are
activated by merely installing this source.

## Canonical Goal observations

An administrator can explicitly inspect the selected conversation's persisted
Goal bindings through `GET /api/home/operator/goals?expected_revision=...`.
The read uses the existing tenant-scoped Goal binding service, restricts bindings
to Home and the selected native thread, and verifies each canonical Session's
Project, native identity, provider and runtime. The Home selection and profile
availability are checked before and after the observation. A changed selection,
missing Goal or incompatible Session refuses visibly without creating a successor.

At most the latest 50 bindings are projected, with an explicit history indicator.
Their Goals are resolved in one validated canonical snapshot; inspection does not
reload Goal revisions and event history for every displayed binding.
The projection exposes canonical Goal and binding status/revisions, checkpoint
reference and stop reason. It does not retrieve native Goal storage, transcripts,
objective context, credentials or model output. The UI labels this as a recorded
observation and opens the existing Goal surface for supported controls. Inspection
does not claim an active runner, resume a binding, settle an unknown result, or
create a continuation owner. There is no timer or model call. Native Goal controls
remain subject to their separate capability and qualification contract.

## Explicit event follow-up in the saved conversation

Automation Definition schema `1.1` adds the opt-in `home_operator` target over the
existing canonical Automation engine. No subscription, profile, Role or worker is
created or activated by installing the source. The existing Automations editor
shows the target's native Home thread ID, Home selection revision and exact Agent
Profile revision. Its ordinary run history retains the canonical event, immutable
Definition reference, delivery result and original requested execution reference.
Manual "Run now" is unavailable for this event-only target; normal human requests
continue through the conversation composer.

An administrator explicitly publishes an enabled Automation whose owner is the
same current administrator who selected the profile-bound Home conversation.
The target ID is that exact Agent Profile. The current authority catalog must
allow `operator.event.followup` at EXECUTE in Home; publishing a task description
or receiving a provider event does not grant that capability. Normal profile
invocation rights and host operation checks remain separate. Credential-free
identity resolution rechecks membership; it does not authenticate a new human
request or manufacture MFA/session assurance. Policies requiring stronger current
assurance still refuse. Generic service or bot input cannot borrow this grant.

The Automation uses an existing canonical/provider event trigger with explicit
filters, such as CI terminal status, delegated completion or failure. Project-
scoped Home Definitions see only events in their scope; an explicitly published
workspace Definition can select other Projects' events within that same workspace.
Filters and instructions remain versioned Definition data, not another code-owned
routing catalog. Administrators should filter meaningful terminal changes rather
than intermediate status traffic. Canonical event ingress and AutomationRun
idempotency deduplicate the same event. The existing autonomy controller applies
pause, scoped/exclusive gates, cooldown and reasoning-cycle budgets; no idle model
poll, scheduler or Goal continuation owner is added.

The target requires one attempt, concurrency one, finite configured token/duration
budgets and `reuse_only` work policy. Unlike repository Automations, this input
needs no Work Item creation: its subject is the retained operator conversation.
Only bounded identifier references are appended to the published instructions;
raw event bodies, logs, transcripts and credentials are excluded. The new message
contribution is conservatively bounded by its byte length against the configured
input-token allocation. Existing native history/model limits and Automation usage
accounting still apply; this input bound is not a new whole-transcript estimator.

Before enqueueing, an atomic transition of the existing AutomationRun records its
original delivery claim and requested execution ID. At most one Home delivery may
remain unresolved in a workspace. The existing TurnService enters a deferred turn
in the **same** selected conversation without assignment replacement or renewal.
Human queue policy remains authoritative. Current publication, owner membership,
explicit capability, selection and Profile pins are rechecked at queue admission,
after awaited input preparation and immediately before the guarded native send.
That final guard additionally checks the original event message checksum and
requested execution ID, along with the existing native bootstrap/worker/fence/
lease checks. A changed pointer, disabled/superseded Definition, expired duration,
revoked authority, pause or lost custody refuses rather than retargeting.

A lost queue/native response or interrupted delivery leaves the original run and
execution reference visible as unconfirmed. Restarting, seeing the same event or
calling launch again cannot send another input. Missing receipts are not proof of
no effect. Explicit custody recovery and reconciliation use the existing retained
queue/runner mechanisms; this slice does not resurrect cancelled assignments,
resume a native Goal or duplicate an uncertain external action.

Retained threads keep their assignment alive after a turn, so assignment-terminal
Automation reconciliation alone cannot settle these runs. The existing runtime
checkpoint barrier reports the actual native terminal outcome to the Automation
ledger, without creating another execution. It validates the original run/thread/
Profile and current bootstrap/fence/lease; checkpoint failure remains failure.
A duplicate callback cannot overwrite the first terminal fact. Historical outcome
recording grants no new input and does not change pause state or provider receipts.
A missing or incompatible terminal witness remains unresolved for reconciliation.
Browser disconnection does not own delivery or settlement: both use canonical
persisted records. This implements the event-delivery engine, not the full #1669
live UI/restart/lease-expiry qualification or privileged host mutation capability.
