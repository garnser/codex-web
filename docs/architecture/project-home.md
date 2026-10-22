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
