# Home operator conversation reference

Home can retain one administrator-selected conversation using `Project.home_operator`.
The reference contains its native thread ID, selecting identity, revision and timestamp.
It is a navigation binding on the existing Project, not a new session, execution role,
agent identity, goal owner or permission grant. Ordinary Projects have no such entry point.
Absent bindings are omitted from existing Project serialization for compatibility.
The overview uses the explicitly present canonical Project context while the product
switcher is initializing. Explicitly cleared or unavailable context stays empty;
route or selector fallbacks never restore a cleared scope. The same resolver fences
responses so a previous Project cannot populate the current overview.

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

### Explicit trusted host action engine (schema 1.3)

The optional project-scoped execution-profile schema `1.3` adds a
`home_operator` envelope to `orchestration-only`. It names an existing host
Environment resource, explicit trusted worker IDs, an explicit `true`
`host_network_access` acknowledgment, bounded command duration/output, and an
existing Artifact-purpose `host_output_key_id`. Optional `project_targets` pin
specific Projects and child Agent Profile revisions; absent targets grant
nothing. Legacy schema readers reject this envelope. No seed profile, worker,
key, binding, Role or host backend is enabled by publication of this code.
Deploy compatible readers before publishing 1.3; drain/reconcile Home inputs
and unresolved host actions before removing or rolling back this support.

`operator.host.command` is a critical canonical ActionIntent action. Its provider
requires a separate explicitly enabled `TrustedHostActionBackend` on the
existing local worker, the configured OS UID, and that worker's explicit
`home-host-command/1.0` contract. The native Home assignment retains its isolated
scratch/Bubblewrap/network-disabled contract and its original lease. A command
uses the existing process execution engine as a nonterminal suboperation;
its ActionIntent receipt owns the command outcome, never parent completion.

The selected administrator, Home selection revision, current Profile/Definition,
Role capability, host Environment, binding/security policy, action attempt,
worker/token, assignment fence and scratch lease are checked before execution
and during execution. Commands accept bounded argv with an absolute executable;
the engine uses its minimal environment, without automatic root/sudo or credential injection.
Host filesystem, process and network access deliberately use the configured OS
principal's existing permissions. The canonical security policy must explicitly
allow host read/write roots, process/shell execution and network access. The
engine bounds duration, CPU/memory/process limits and retained output and reaps
the owned process group. Workspace disk accounting is not a whole-host disk
quota, and host network access is not egress-isolated.

This action has one attempt and advertises neither idempotency nor functional
effect verification. Exit zero is process completion, not proof that a service,
deploy or arbitrary host mutation achieved its desired state. Lost custody or
revocation after spawn retains possible effects as an unknown outcome; there
is no automatic retry. Cancellation joins the owned operation before returning.
Reconcile the original intent and artifacts rather than submitting its command
again to resolve uncertainty.

Output is bounded, encrypted with the existing canonical Artifact-purpose key,
and stored through Artifact content and DataGovernance as SECRET with
`deny_model_context=true`. Receipts contain only process metadata, command digest
and the artifact reference. Model-readable excerpts require the current
administrator's explicit output-release policy or human review through the
Home output surface. Every excerpt retains encrypted source provenance and
fresh custody/governance checks; arbitrary command output cannot classify itself.

### Trusted host command deployment

The native Home runner continues to use its isolated scratch workspace. Host
suboperations require both deployment settings
`CODEX_WEB_TRUSTED_HOME_HOST_ENABLED=true` and
`CODEX_WEB_TRUSTED_HOME_HOST_UID=<service OS uid>`. A missing or mismatched UID
refuses explicit enablement; the service does not manufacture OS privilege.
Only this opt-in advertises `home-host-command/1.0` and grants its local worker
Artifact output storage capability. Profiles, human administrator selection,
current Role authority, canonical Environment, output key and the unique enabled
`home-operator` / `trusted-host` provider binding remain separately required.

`POST /api/operator/host/commands` accepts only `parameters.argv` and an
`idempotency_key`. The broker derives identity, assignment, fence and Environment.
Admission deduplication is separate from provider effect idempotency: one original
intent has one execution attempt; an uncertain command is retained for inspection,
never automatically replayed. Its claim covers the configured process duration
and receipt recording. Encrypted governed Artifact references appear in receipts;
process completion alone does not verify a deployment or other requested effect.

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
input-token allocation. This is not a whole-transcript estimator. Settlement
uses canonical runtime usage for the exact assignment, session and native turn,
including actual input/output tokens and duration; missing usage remains pending,
not zero. The current native request API exposes no provider-side hard output-token
limit. An observed over-budget output is recorded as a failed Automation, rather
than claimed to have been prevented by the deterministic controller wrapper.

Before enqueueing, an atomic transition of the existing AutomationRun records its
original delivery claim and requested execution ID. At most one Home delivery may
remain unresolved in a workspace. Other genuine events stay in
`waiting_for_capacity`, including events from the same Automation. They keep their
original dedupe identity and are admitted in creation order after positive terminal
settlement. Up to 128 active Home events are retained per workspace; overflow is an
explicit recorded capacity refusal. Completion, real usage, startup/idle recovery
and canonical cooldown wakeups advance deferred delivery without model polling.
Unknown claimed deliveries are never requalified as no-effect.

The existing TurnService enters a deferred turn
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
checkpoint barrier joins the retained-turn outcome observer; synchronous storage
work runs in a worker thread, without blocking the shared async loop. It reports
the actual native terminal outcome to the Automation ledger, without creating another execution. It validates the original run/thread/
Profile and current bootstrap/fence/lease; checkpoint failure remains failure.
A duplicate callback cannot overwrite the first terminal fact. Historical outcome
recording grants no new input and does not change pause state or provider receipts.
A missing or incompatible terminal witness remains unresolved for reconciliation.
Browser disconnection does not own delivery or settlement: both use canonical
persisted records. Event delivery does not itself confer privileged host mutation
authority; each host action retains its independent current authorization.


Home event runs use AutomationRun state schema **1.4**, with a migration from 1.3,
a durable waiting status, immutable positive terminal witness and one interrupt
attempt marker. This is a canonical state compatibility change, including ordinary
run-store writes; a rollback requires a reader supporting 1.4 or the documented
state compatibility procedure. It must not be described as a migration-free install.

Each original claimed run creates one canonical duration deadline. At expiry the
engine may request the existing guarded `turn/interrupt` operation only for that
run's exact active native turn and current original assignment/worker/fence/lease.
A changed turn or custody refuses. The interrupt attempt is persisted before the
request; a lost response is never resent. Interruption does not settle external
mutations: original ActionIntent uncertainty and terminal/usage reconciliation
remain authoritative. Canonical timers and native recovery keep pending state
visible across restart; they do not revive cancelled custody or create idle turns.
### Governed command output and reviewed derivatives

Native source-output reads may omit a byte selection and request only a stream
and `max_bytes` (default 4096, hard maximum 65536). The server chooses a UTF-8
prefix bounded by the current exact command policy and Home envelope; the model
does not supply a classification or release override. A policy-authorized empty
stream returns explicit empty metadata without creating a derivative or claiming
host-effect success. A nonempty stream whose first UTF-8 codepoint exceeds the
requested limit is distinguished from an empty stream. Unconfigured commands
remain denied even when their output is empty.

Human preview revalidates its original source/receipt and current key/version
after decrypting, so a revocation during the read suppresses plaintext. The
release validator also parses the digest-bound original ciphertext envelope and
checks its actual key version on every future context/read edge. Encrypting a
derivative under a newer active version cannot bypass revocation of the original
source version; validation does not decrypt the original plaintext again.
The human-only no-store output list exposes at most twenty current-assignment
Artifact/receipt references and process-result metadata, never argv or output.
It reuses the existing intent catalog read and bounds matching history fanout;
it does not introduce a bounded storage index. The Home operator panel mounts
the explicit preview/review component after a human selects a listed reference,
checks the current Home selection around each request and disposes the preview
on rerender or abort. Human routes and native broker reads share the same
canonical output service and current governance checks.


Home's optional `output_release_policy` pins a published project/Home
`home-host-output-release` Definition, schema `1.0`. It is an explicit output
policy, not a host execution grant. Publication requires the current same human
administrator and selected Home origin. The policy pins that administrator,
tenant/workspace, Agent Profile revision, host Environment, trusted worker IDs,
maximum excerpt bytes and INTERNAL or CONFIDENTIAL classification. Its bounded
command rules match the entire argv and explicit process exit codes/streams;
there are no wildcard/path/shell-safety inferences or implicit default rules.
A known failing test process can expose its errors only when its configured exit
code is included. Publishing this policy once permits routine configured work
without a prompt for each output. Nothing auto-publishes or adds commands.

`HomeHostOutputService.read_source` accepts an original Artifact and bounded
selection through the current assignment-bound `operator.host.output.read`
capability. It requires the original one-attempt host ActionIntent and canonical
known process-completion receipt, exact command digest, current host key/context,
content digest, source assignment/fence, Home selection revision and explicit
current policy. Timeout, limit breach, truncation and uncertain execution cannot
release output. Process exit does not become host-effect verification.

The service creates an encrypted canonical Artifact derivative using normal
source inheritance, initially SECRET/denied. The narrow canonical DataGovernance
reviewed-release operation then records the immutable original Artifact,
governance/source IDs, encrypted content digest, exact process receipt/intent,
key context, selected byte range/digest, current Profile/Home revision and policy
reference. Only this derivative is released; original source IDs are retained
and the original remains SECRET/denied. Repeated identical release requests use
canonical Artifact idempotency and do not repeat the host command. Downstream
artifacts derived from a released excerpt default to denied model context;
classification inheritance cannot remove the release's revocation guard.

Every model read calls the canonical context filter and the current release
validator before and after decrypting. Revoked/superseded policy, changed Home
selection/Profile/Role/worker/lease, changed source/receipt/content/key, expired
retention or missing validator denies the read. Previously supplied model
context cannot be retroactively erased; revocation stops future reads.
DataGovernance state `1.1` explicitly migrates legacy `1.0` metadata and causes
old `1.0` filter engines to refuse the new state. Deploy the release-aware reader
with the host engine before publishing output policy. Rollback needs the normal
controlled compatible-state procedure; never downgrade released state by
removing provenance or policy fields.

Unconfigured commands remain denied. A separate human-only no-store preview and
review API lets the current same Home administrator inspect an output, select an
exact digest-checked UTF-8 byte range and explicitly release a derivative with a
reason and classification. This API is not a model broker operation; service
principals cannot approve their own output. `home_host_output_review.js` provides
the protected-output review control, checks Home selection before/after requests
and clears previews at the earlier of lease expiry or 60 seconds. Its host UI
must dispose the component when Home selection changes. The model cannot choose
its own classification, replace text, publish a policy or read the SECRET
original. Broker/startup/UI wiring and deployment activation remain explicit
integration steps; the standalone service does not enable privileged access.

### Cross-project coordination from Home

An explicit `project_targets` envelope pins each accessible Project and child
Agent Profile revision. `GET /api/operator/projects/threads` accepts one declared
`project_id` plus bounded pagination. `POST /api/operator/projects/thread/messages`
accepts the target Project, existing thread, bounded message and admission key.
Current membership, invocability, target authority, bootstrap ownership and full
writable/read-only repository scope are rechecked independently of the Home grant.
A declared scratch child may receive empty repository fields only when its current
resolved execution Definition exactly matches the assignment pin and specifies
`scratch` with `repository_access=none`. A repository contract with missing scope
is refused. Admission and the final native writer repeat these same checks; a
contract change cannot reinterpret an already retained input.

A canonical credential-free `home-operator-coordination` / `canonical` binding
admits one original ActionIntent and one retained input through the existing
TurnService queue. The queued dispatch pins the original Home and target custody
and message digest; the native guarded writer rechecks it immediately before
sending. Queue acceptance is neither native-start proof nor delegated completion.
Revoked authority and uncertain delivery retain the original input for normal
reconciliation rather than choosing another thread or creating a replacement key.
`HomeOperatorCoordinationDispatch` schema `1.2` requires compatible queue readers;
reconcile or drain retained dispatches before rolling back to an older reader.
## Retained Home restart input

Home startup continuation uses the existing `ActiveTurnRecoveryStore` record,
retained Turn queue and QueuedPreflight claim. An interrupted human Home turn is
retained once on the SAME conversation with its original actor, Profile, selection
revision, assignment and message checksums. The old active marker is removed only
after positive durable retention. A source string alone grants no authority: the
common native input and final-send guards require the exact canonical recovery
record, current queue attempt/claim and current Home/worker/lease authority.
Ordinary Project startup recovery is unchanged.

The existing typed `project_coordination_dispatch` queue field has a distinct
Home restart proof schema `1.3`; it conveys no project delegation. Legacy dispatch
readers reject this proof rather than silently rewriting a queue without its
witness. Absent proofs preserve ordinary queue serialization. Deploy the matching
reader/writer together; rollback must retain these rows behind the normal compatible
state boundary. A retained or unknown native start never automatically acquires a
second claim. Missing evidence is an actionable retained blocker, not an invitation
to reconstruct human origin from a source prefix.

Known NOT-SENT human input remains the original queued row while existing canonical
lease expiry, bootstrap cancellation, fresh worker custody and strong-CAS binding
recovery heal the SAME conversation. An uncertain host action does not block all
conversation inspection. Its original intent remains held; a new intent with the
same requester, host resource and exact argv cannot replay that action across
assignment incarnations. Other independently authorized operations may proceed.
Exact argv matching does not establish semantic shell equivalence or command safety.

This slice covers interrupted human turns and retained human inputs. Planned idle
restart checkpoints, completed Upgrade supervisor provenance, repeated restart
chains and claimed event custody rollover require their explicit canonical handoff;
they are not inferred from an empty active marker or a successful process exit.
Private canonical admission/custody tests do not establish live restart acceptance.

Host duplicate prevention and startup inspection use a typed, parameterized
canonical state-backend query over ORIGINAL ActionIntent and receipt rows. The
predicate pins tenant and exact native execution or Home command identity
(requester/provider/resources/argv). It selects unresolved statuses, an original
unknown/missing receipt, and terminal executions without a receipt. Positive
terminal receipts must match the original intent's identity, attempt and known
outcome. A single original receipt is required; conflicting/unknown headers
cannot establish settled history. Selected full models and original receipts are
validated again. This is a current recovery predicate, not an integrity audit of
all positively settled historical result payloads.

The query has a five-second statement budget and returns at most 256 unresolved
candidates. Settled history does not consume that limit, so 300 successful repeats
of one command or native execution cannot disable the next action. Unsupported
backend/query semantics, conversion failure or genuinely excessive unresolved
work refuse visibly. The existing owned operation joins the read worker before
retiring; it never substitutes a whole-history fallback. Legacy nonstandard JSON
which cannot be classified safely may refuse rather than be treated as absent.

The PostgreSQL query projects the original intent's query headers through a
JSON record conversion instead of repeatedly parsing its full retained payload
for each predicate. The projection is query-local, not a stored index or an
absence proof. An explicit scope CASE keeps correlated receipt checks behind
the matching tenant/execution or command predicate. Missing identifying headers
and non-object originals still reach full canonical validation; receipt identity,
unknown outcomes, candidate limits and the five-second budget are unchanged.

No derived alias proves an original absent. The abandoned Home index prototype
was never deployed; deleting any such alias cannot affect this predicate. No new
records migration is required. The reader explicitly accepts prototype normalized
metadata `1.5` without downgrading it; publication/readiness compatibility remains
intact. Ordinary canonical `1.4` writes and serialization stay unchanged.

## Planned Upgrade checkpoint and same-conversation handoff

An explicitly configured Home operator can use the existing whole-service Upgrade
supervisor. `UpgradeService.start_service_drain` has an optional before-maintenance
diagnostic checkpoint callback; `verify_post_upgrade` has an optional observer AFTER canonical
PASS evidence and COMPLETED status are committed. The application supplies the
already configured `LocalExecutionWorkerRuntime`, never constructs an alternate
worker identity. Both API operations must run through the existing owned, joined,
off-loop storage boundary. No signal, restart daemon, second controller or native
Goal mutation is introduced.

`HomeUpgradeCheckpointService.checkpoint_current` first resolves the selected
Agent Profile and its current explicit Home operator declaration. Navigation-only
and ordinary profiles are no-ops BEFORE bootstrap lookup. Configured Home resolves
the same conversation's actual bootstrap/assignment and accepts only the configured
worker's existing identity. A mismatched worker is a visible blocker. The checkpoint
is a canonical REPORT Artifact with selected Home/Profile/native-conversation and
custody references, exact new plan/release/source/target references, retained queue
IDs/hash and held intent IDs. It contains no lease tokens, transcript, argv, output
or fabricated active-turn record. This metadata checkpoint references existing
persistent state; it is not a copy of volatile native model context.

A selected **LOST** Home assignment has a separate preservation-only disposition
when the canonical current runner explicitly reports no lease, stopped process and
typed executor-work zero, with no active-turn marker, queued input or Home Goal
binding. The supervisor revalidates the exact selected Human/Admin, Profile/Role,
bootstrap, tenant, worker observations and canonical Session; it never weakens the
Host operation guard or revives custody. Executing/UNCERTAIN actions block this
classification. Terminal cancellation with a retained UNKNOWN receipt is recorded
as historical UNKNOWN, never as provider success or permission to replay.

This disposition is an idempotent, governed REPORT Artifact using distinct source
`home-upgrade-dormant-preservation`, metadata schema `1.0`, exact plan/release/source/
target and current preservation identifiers. It is **not** checkpoint `1.4`, does
not grant native input, and cannot coexist with a real planned checkpoint for the
same plan. Current metadata observations run before the atomic Artifact catalog guard and
outside Artifact/Governance locks. Canonical Session/ActionIntent readers may
initialize or migrate their own readiness records; no input or historical outcome
is changed. A checkpoint still refuses before Artifact/Governance registration,
export or upsert. This deliberately changes refusal priority: a current custody
observation can refuse before checkpoint ambiguity is inspected.
Idempotent rereads freshly revalidate dormancy; changed selection,
Session, Goal, queue or process evidence fails closed. Existing Artifact/report
inspectors expose the explicit UNKNOWN/no-continuation report name, source, plan
reference, digest and scope; detailed retained references remain in the canonical
Artifact API metadata. No new operator control or model call is introduced. The post-verification observer validates the
actual completed plan/PASS and unchanged preservation state, then returns without
enqueueing Home. An absent old process-local manager after actual service replacement
is not reported as fresh process-zero evidence; it grants no continuation.
Canonical service-wide drain counters and clean stopped-service/cgroup qualification
remain mandatory. Older readers ignore this distinct metadata source and cannot
interpret it as a checkpoint or authorization. No core schema migration or Session,
Goal, queue, lease or action-outcome mutation is introduced by this disposition.

After a service replacement, the new assignment-bound process manager may have
no registered owner for an older LOST/unleased assignment. Successful lookup of
that exact local worker/runtime/assignment, no in-flight start/stop lock, a LOST
transition predating this manager, and a fresh unchanged canonical assignment
can establish **unowned custody**, separately from an unavailable lookup. Both
runtime bindings must exist; the existing canonical identity comparison matches
provider, runtime and capability revision while tolerating routing-enriched
sandbox/authentication facts. The complete assignment snapshot still must match
the fresh canonical read. The
runner then exposes `processOwnershipObservation.state=unowned_lost`; process
running and pending executor-work fields remain omitted/UNKNOWN. A registered
owner, lifecycle in progress, current lease, stale snapshot, wrong worker/runtime
or lookup error cannot establish that observation. It authorizes preservation
only, never input, resume, teardown, provider actions or lease changes.

This case writes the same governed report source using metadata version `1.1`,
with explicit `process_ownership`, `stopped_process_observed=null` and
`pending_executor_work_observed=null`. The report name visibly says unowned LOST,
process/executor UNKNOWN, and no continuation. Version `1.0` stopped-observation
serialization is unchanged. Readers predating `1.1` reject its post-upgrade
preservation validation instead of interpreting it as a checkpoint or process
zero; a target handling such a report must include the `1.1` reader. This is a
versioned Artifact metadata boundary, not a core-store migration. Current Human,
Profile, Session, selection, Goal, queue, active-input and held-action checks all
remain mandatory; actual aggregate-zero drain and clean stopped-service/cgroup
proof still belong to the canonical Upgrade/capture supervisor. Unknown native
outcomes and old action receipts remain unchanged.

The same supervisor's post-verification callback point-reads that plan's immutable
checkpoint, verifies actual target source, SAME completed plan and current valid
PASS evidence, then retains one continuation in the SAME native conversation.
The existing typed queue dispatch union carries schema `1.4`, distinct from project
delegation and interrupted-turn `1.3`; old readers reject it visibly. Its internal
input authorizer checks the exact original preflight claim, checkpoint and resume
Evidence/message hash, current Home selection/Profile/requester and current healed
worker/fence/lease at final send. Old completed plans, stale sources, changed
selection, revoked completion evidence and replaced messages cannot authorize it.

A checkpoint failure prevents maintenance. A continuation failure after a real
successful Upgrade preserves COMPLETED/maintenance-off and records explicit WARN
Evidence (`home-upgrade-resume-blocked`); it does not repeat source installation.
An existing resume reservation is never automatically submitted again. Existing
preflight/queue evidence owns any uncertain native start. Rebinding an already
active service drain does not overwrite the pre-stop checkpoint. With configured
Home callbacks, re-reading completed verification does not replay the observer.
Unconfigured deployments retain the existing Upgrade verification behavior.

Private canonical fixtures cover the real retained admission and guarded
`thread/resume` / `turn/start` capture path with fake transport and actual expired
custody recovery. Application wiring, controlled actual restart and live Home
continuation qualification are still required; these tests do not claim live
self-restart or full native Goal lifecycle acceptance.
An interrupted Home restart continuation can create a later recovery generation
only when its exact queued input has the canonical positive native START receipt,
correlation and actual active native-turn/custody identity. An UNKNOWN original
start never authorizes a new submission. The same thread, pending human queue and
all previous recovery originals are retained. Twenty inline generations bounds
one unsettled continuation chain, never lifetime use of the conversation. A new
independent human chain requires its own positive START and the prior
continuation's actual terminal usage (plus terminal event accounting when
applicable). The complete previous chain is archived immutably through the
existing recovery store before resetting its current CAS. Original ActionIntent
and preflight UNKNOWN records are unchanged; history links require no scan.

An active Home event additionally retains the original Automation grant, event,
selection and custody evidence and revalidates current administrator, Role,
Definition and control authority. A restart message is a new inspection/
continuation input, never a replay of the event or an uncertain host action.
Its terminal callback returns to that same Automation run. Budget settlement
requires actual canonical usage for every interrupted and resumed native turn;
missing or UNKNOWN original usage stays pending instead of contributing zero.
The existing exact-turn duration interrupt also recognizes that validated chain.

### Active Goal-derived restart continuation

A positively started application-mode Home Goal can use the SAME existing
HomeRestart owner. Its strict queue proof is **1.5** and references the original
binding, delivery, Goal revision and canonical START. This is not a source-prefix
grant: current HumanAdmin, selected Home, Profile/Definition, Goal and exclusive
continuation policy are revalidated before the original retained recovery input
can cross the final native writer. Goal or binding pause, revocation and changed
custody fail closed. Ordinary human proof1.3 and planned proof1.4 stay unchanged.

Each new recovery input atomically reserves another call against that Goal's
finite budget using the existing Goal binding transaction. Expiry may renew the
same owner's lease only for the exact original retained queued snapshot with a
canonical preflight proving no claim, retirement, resume or start. A claimed or
UNKNOWN start is held, never resubmitted. Existing worker expiry, cancellation,
fresh bootstrap and strong-CAS same-thread rebind preserve the original delivery.

The original and every positively started recovery turn require actual attributed
canonical usage. Known usage is accounted once; missing original or resumed usage
keeps the delivery pending and permits no next Goal continuation. The existing
Goal runtime terminal event owner reconciles delayed original usage even after a
later recovery turn becomes the binding's latest turn. Events convey identity,
not token counts or success. Budget exhaustion blocks the binding without
claiming the ongoing Goal complete. A fresh independent positively started Goal
delivery can archive the prior bounded chain only after every original/recovery
usage has been accounted and its exact settlement witness retained in the SAME
recovery record. The witness is written before releasing the Goal delivery;
failure leaves existing terminal reconciliation retryable. Missing or UNKNOWN
usage prevents history reset, and original action/preflight records stay intact.

Goal-derived recovery records use explicit **1.1**; ordinary recovery records
retain their existing unversioned1.0 serialization. Old strict readers reject both
record1.1 and queue proof1.5. This adds a compatible-state boundary to GoalBinding
state1.2: qualify capture/isolated restore without starting the new application or
writing new records while the old service must still be restorable. After a new
writer persists these records, rollback requires compatible readers or a normal
lossless qualified migration, never deleting proof/history or clearing UNKNOWN.
Private integration fixtures prove these paths; actual deployed restart acceptance
must still use genuine canonical Goal/input and custody evidence.

### Delegated child terminal event

A positively started child input carrying the canonical Home coordination proof
emits `work.transition` with `transition=home_child_native_terminal` at the
existing owned retained-turn terminal barrier. Its source is
`home-delegated-native-terminal`. The event contains tenant, child Project,
thread/native turn, assignment/fence, original Home ActionIntent and admission
references; it contains no input text, transcripts, command output or secrets.
Exact canonical usage contributes its original usage ID, tokens and duration.
Unavailable or unfinished telemetry remains explicitly `outcome=unknown` and
`usage_available=false`; missing usage is never represented as zero or success.
The native telemetry outcome and the owned completion/checkpoint boolean
(`completion_succeeded`) are distinct fields; the latter is true only when
both the original terminal path and checkpoint succeeded.

Publication uses the original canonical event identity and is idempotent for
the same admission and native turn across duplicate terminal observations or
process reopen. A configured durable transport owns its existing outbox delivery
retries after commit. With local-only transport, a subscriber failure after
event commit has no automatic subscription replay; dedupe does not establish
delivery recovery. Abrupt process death before event commit is also outside
this observer's recovery guarantee. No unconditional crash-delivery guarantee
is introduced. It never sends another child input or replays its provider
action. Missing or inconsistent positive START/proof/intent attribution refuses
publication. The original parent action may still be `executing` because its
queue provider has not returned yet, or `succeeded`; the observed
`parent_intent_status` is explicit and is not a parent success receipt. Other
parent dispositions refuse publication. No current parent lease is required to
record this historically attributed child fact. Unrelated native turns retain their existing event behavior.
The optional active-turn admission reference is observation metadata, not an
execution grant: old readers may omit it and legacy markers produce no event.
Drain active delegated turns before rolling back if completion-event delivery
is required; an omitted reference cannot be reconstructed by guessing history.

A cross-project child completion must match an explicitly administrator-published
**workspace-scoped** Home Automation (`schema_version=1.1`), filtering
`event_type=work.transition`, `event_filter.project_id=<child project>` and
`event_filter.transition=home_child_native_terminal`. Definitions scoped to Home
are not selected for a child Project event; child-Project Definitions do not
acquire Home input authority. Existing Home selection, Profile, current Role,
controller, custody, final native-send guards and actual native usage accounting
remain required. The Automations UI and canonical event provenance expose these
existing objects; this producer adds no activation or default Automation.
Home application Goals use the canonical GoalContinuation adapter described in
[Goals](goals.md#selected-home-application-continuation). A current human-owned
ACTIVE Goal can perform bounded work in the same selected native conversation.
The generic service actor is still refused. Existing Goal pause/stop controls,
canonical input identity, final current-authority guard and actual runtime usage
control continuation; no synthetic zero-token wrapper or idle polling is used.
The Goal binding 1.2 write/rollback boundary applies globally, even before a Home
binding is created. A paused or draft Goal is not evidence of active execution.

### Resuming a control-blocked Goal startup

If Home Goal startup retention refuses specifically because GLOBAL autonomy is
paused, the existing execution owner remembers the exact interrupted active-turn
snapshot as an in-process observation. Ordinary autonomy Resume schedules one
per-thread retry through the existing stale-turn recovery coordinator. It does
not retry arbitrary live Home turns. The same-thread lock compares the current
marker with that exact snapshot before the ordinary canonical retention owner
rechecks original positive START, Human/Goal/Profile/selection authority, custody,
controls and finite Goal call reservation. A control change racing the initial
refusal is retained as one coordinator wakeup.

The observation grants no authority and adds no durable schema or controller. A
new process obtains eligibility again only through normal startup reconciliation.
A changed marker or a different authority/budget refusal does not authorize a
retry. Positive retained queue creation consumes the observation; duplicate Resume
cannot create another recovery input. Original UNKNOWN actions and missing actual
usage remain held by their existing owners. Existing Resume controls and recovery
metadata continue to provide the operator surface; no new UI action is introduced.


The PostgreSQL original-row Home recovery query runs with JIT compilation disabled only inside its existing read savepoint. Correlated receipt predicates can produce a large estimated plan cost even for a small original intent inventory; LLVM optimization must not consume the shared five-second preservation budget. The caller's JIT setting is restored on success or by savepoint rollback on refusal. The original SQL, tenant/execution/command scope, ambiguous and malformed-row handling, receipt validation, 256-row limit, and complete typed validation remain unchanged. This adds no index, persisted state, global database setting, recovery authority, or UI control.

Dormant preservation still makes three fresh snapshots within that same shared
five-second budget. Within one preservation call, its initial complete original
action-ledger observation may be reused only when the existing canonical ledger
revision is positive, finite, unchanged across the complete read, and still
unchanged at the next snapshot. The observation includes original receipt
validation, rather than only candidate selection. Missing or changed revisions
run the original query again; an unrelated ledger write does not by itself refuse
the upgrade. Storage errors still propagate. Canonical intent and receipt writes
both advance this revision. Direct database writes outside the StateStore
revision contract are not covered by this observation.

Selection, authority, assignment, process observations, session, Goals, queue and
each retained intent's current metadata are rechecked at all three points.
UNKNOWN remains UNKNOWN. The observation cannot cross an execution/actor scope,
repository change, preservation call, retry, or completed-plan resume. It creates
no persistent cache, index, schema, new input authority, or larger budget. A
successful component query or cost-sensitivity fixture remains insufficient for
live acceptance of the whole selected-Home hook.


The direct Home checkpoint owner remains strict: a storage timeout or PostgreSQL
query cancellation reports only the fixed incomplete-preservation refusal, closed
phase, and `TimeoutError` or `QueryCanceled`. Its historical outcome is UNKNOWN;
a final observation can fail after the Artifact commit. No absence or retry
permission follows from that refusal.

The whole-service Upgrade integrates that independent owner through
`checkpoint_for_service_drain`. Home preservation is a bounded best-effort
**diagnostic, never a global Upgrade gate**. Before optional Home I/O, a short
canonical plan mutation reserves `home_checkpoint_disposition=pending_unknown`.
A further short plan mutation pins the observed selection and actor-scoped Artifact
IDs before Artifact I/O. These are observed references, not proof an Artifact was
attempted: a changed selection blocks only the optional writer. An unknown pin
write also suppresses Artifact I/O. The existing drain coordinator serializes the
synchronous attempt; no canonical database callback encloses Home I/O. A settled typed Home eligibility refusal is
recorded as `blocked`; known budget/query refusal is `deferred_unknown`; a returned
Artifact is `recorded`. Unconfigured Home is `not_applicable`. No outcome grants
Home input or continuation. Mandatory Upgrade authority, immutable Release/instance
checks, global drain admission fences, and active-work preflight remain unchanged.
Unrecognized programming failures still propagate rather than becoming a warning.

A repeated same-plan drain reuses the disposition and never invokes another Home
Artifact writer. If the reservation or final plan write returned an unknown result,
the original reservation/Artifact IDs remain diagnostic references; an existing
pending reservation suppresses optional work while the ordinary global drain may
proceed under its own controls. UNKNOWN is not proof that the Artifact is absent.
The original synchronous operation must settle through the existing joined owner;
this adds no process-zero inference or background retry. A completed Upgrade with
pending, deferred or blocked Home diagnostic skips Home continuation **before**
Artifact lookup, so a late committed optional Artifact cannot acquire authority.
The shared Home completion validator also refuses these diagnostics for direct
checkpoint resume and previously queued proof validation; neither path can
promote the original Artifact or erase the retained queue.
Navigation, selection, queue, Goals, Profiles and historical native outcomes are
not changed. Separate Home continuation guards still apply to actual recorded
checkpoints. The Autonomy Control Center displays the canonical diagnostic and
original Artifact links without rendering it as a successful Home handoff.

Upgrade state format `1.1` adds the server-owned, versioned diagnostic to the
existing plan. Migration from `1.0` does not invent an attempt or authority; absent
diagnostics are omitted when serializing legacy plans, preserving their old bytes.
The public plan-creation DTO cannot inject a diagnostic. This is a forward durable
format change, with no SQL schema migration. Actual older strict UpgradePlan readers
reject populated diagnostics, so old-binary rollback after new writes is unsupported;
deploy writers/readers together or use an explicitly compatible reader. Persisting
a diagnostic uses the existing service-contract boundary to conservatively disable
binary rollback on retained and subsequently created plans; a previous compatible
rollout advertisement cannot override the new durable format. Historical outcomes
and evidence remain unchanged. This fact
does not impose a new blanket backup/restore deployment requirement.
