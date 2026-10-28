# Safe upgrades, migration orchestration, version skew and rollback

## Status

Canonical production-upgrade contract. Application upgrades are represented
as canonical Upgrade Plans with an explicit source/target compatibility profile,
ordered migration phases, durable step progress, preflight Evidence, and a
truthful rollback boundary.

## Cancelling an unexecuted local plan

An administrator with MFA and complete hosted-scope coverage, a trusted local
administrator, or a correctly scoped `upgrade:admin` service may request
`POST /api/upgrades/{id}/cancel-unexecuted`. The request names the current
service instance, expected running revision, plan admission epoch and bounded
reason. Under the same host lock used by the qualified installer and the
canonical admission coordinator, the service verifies checkout/build agreement
and rejects an active installer, unknown or changed build, installed target,
active operations, arbitrary attempted steps, definition migrations or irreversible state.
An absent host guard fails closed. This local operation does not extend to
replicated or shared deployments.

An unexecuted, nonempty contract-only plan may release maintenance with its
target binary installed when a distinct completed Upgrade plan predates its
creation and carries valid, passing canonical post-verification Evidence for
the same immutable build digest and source revision. That earlier plan and its
Release/Evidence are rechecked under the shared canonical transaction before
cancellation commits. An empty plan or a plan containing rollout steps cannot
use this exception. Missing, revoked, changed, foreign or nonpassing baseline
records refuse cancellation. Cancellation Evidence links the previous verified
deployment and continues to claim neither installation nor rollback. The existing
Upgrade cancellation control and refusal/Evidence surfaces apply unchanged.

A single failed `skill-scan-contract/1.0` activation may also release its hold
when its sole attempt has exact, valid engine-owned failure Evidence for the
worker-assignment refusal before enablement. The handler, phase, kind, attempt,
error, empty result and mandatory approval/backup/drain references must match.
Any enabled scanner marker or another running/succeeded scanner contract refuses
this disposition. Unknown failures, additional attempts and arbitrary handlers
remain ineligible. This proof relies on scanner admission requiring the persisted
enablement marker before any incompatible scanner record can be written.

The original target must have a verified completed binary baseline predating the
failed activation. The current running revision may be its subsequently installed
source fix, as proven independently by the host checkout/build guard; cancellation
does not assert installation of either revision. Failed-step Evidence, baseline
Release/Evidence and the absence of enablement are rechecked in the canonical
transaction. The failed attempt, error, Evidence and consumed approval remain
unchanged. Cancellation Evidence links the original failure. The inspector labels
this action **Cancel blocked scanner activation** and shows its failed-step
Evidence. A fresh immutable Release/plan and approval are required for activation.

Cancellation records separate attributed `upgrade-unexecuted-cancellation`
Evidence with a failure result, invalidates the admission epoch and clears only
that plan's maintenance hold. It claims neither installation nor rollback and
does not alter queued requests, checkpoints, other plans, worker lifecycle or
autonomy controls. The existing `failed` status with `completed_at` marks this
terminal disposition without adding a persisted field or enum. Such a plan
cannot be reopened, verified, retried or marked rolled back; create a fresh
plan for the current immutable Release instead. Older readers retain the failed
state and false maintenance hold, although they lack the new cancellation API;
do not use an older writer to reopen a cancelled plan.

The existing Upgrade installation inspector shows the request, exact source and
target, reason, canonical denial and Cancellation Evidence separately from
post-installation verification. Its confirmation does not grant authority:
the backend independently validates every request. A cancellation request can
fail after a UI snapshot becomes stale; no automatic retry changes its epoch.

Upgrades are deterministic orchestration. LLMs are not used to decide whether
versions, schemas, workers, definitions or extensions are compatible.

## Explicit compatibility profile

Each Upgrade Plan freezes the exact supported release pair and mixed-version
window:

- source and target application versions;
- deployment mode (local/shared/replicated);
- control-plane versions allowed during rollout;
- execution-worker versions allowed during rollout;
- execution-contract versions workers may consume;
- source and target StateStore schema versions;
- supported API and canonical-event contract versions;
- target Definition engine version;
- exact Definition kind/schema versions supported by that engine;
- target extension-host compatibility version;
- optional source version to which rollback remains supported.

No component guesses forward/backward compatibility from a higher/lower version
number.

## Target release binding

An Upgrade Plan is bound to one immutable Release from the release/supply-chain
domain. The Release version must equal the target application version and its
immutable artifact digest is reused when irreversible ApprovalRequests are
created.

Blocked, superseded or rolled-back Releases cannot be upgrade targets.

## Preflight

Preflight checks canonical runtime state before upgrade execution:

- current StateStore schema equals the declared source schema;
- target schema is not an unsupported downgrade;
- API/event contracts are inside the declared target window;
- observed control-plane versions fit the mixed-version rollout window;
- immutable target Release remains eligible;
- Recovery health is qualified when required;
- every effective published Definition is supported by the target engine and
  target Definition schema list;
- active/draining workers use declared rollout versions;
- nonterminal assignments use supported execution-contract versions;
- enabled/configured/upgrading extensions are compatible with the target
  extension-host version;
- active ActionIntent and worker execution is drained when the plan requires
  maintenance.

Preflight emits canonical POLICY_EVALUATION Evidence with source
upgrade-preflight. A failed preflight remains visible as explicit blockers rather
than silently being bypassed.

## Definition behavior preservation

Preflight freezes every effective published Definition by:

- record ID;
- stable Definition ID/kind;
- revision;
- definition schema version;
- checksum.

A target engine must explicitly advertise support for that kind/schema and must
also satisfy the Definition record's min/max engine version.

If a Definition must change, the upgrade records a DefinitionMigrationRecord
from the frozen source record/checksum to a published target record/checksum.
The migration must preserve the same canonical Definition slot and carries an
operator reason and actor provenance.

Post-upgrade verification rejects a changed/incompatible Definition unless that
migration was explicitly recorded. Stored payloads therefore cannot silently
acquire new meaning solely because a new engine was deployed.

## Worker mixed-version safety

ExecutionWorker now declares the exact execution-contract versions it supports.
Worker state contract 1.3 migrates existing workers to the historical 1.0
execution contract by default.

Assignment claim eligibility rejects
execution_contract_version_mismatch deterministically. A mixed-version worker
can only claim work whose execution contract it explicitly advertised.

This runtime boundary exists independently of upgrade preflight, so an
out-of-window worker cannot process incompatible canonical work even if an
operator bypasses an external rollout tool.

## Drain and maintenance

Upgrade maintenance is canonical plan state. While maintenance is active:

- ordinary ActionIntent claims are not admitted;
- new execution-worker assignments are rejected;
- execution-worker assignment claims are paused;
- native turns on existing bound sessions resolve their canonical assignment
  scope before admission; scope cannot be replaced by the caller or Project;
- queued native turns rejected by maintenance retain FIFO position and failure
  attempts, with delayed deterministic admission retries and no model call;
- incident/recovery/rollback/reconciliation ActionIntents remain eligible.

Already-running operations are not killed by entering maintenance. Preflight
reports their count and only passes the drain gate after claimed/running/
uncertain/reconciliation activity has reached a safe state.

This avoids a race where preflight succeeds and new ordinary work starts while a
migration begins.

Scanner activation can retain a pending assignment only when it is provably
before its first claim: fence zero, no worker or lease, no start/completion,
failure or output references, and an execution contract explicitly supported by
the plan. Whole-service maintenance continues to fence its claim. Retaining that
input does not settle an original provider effect, classify an unknown outcome,
replay input or authorize unrelated work. Ambiguous/requeued pending custody and
active assignments still refuse activation; old-worker and writer-version guards
are unchanged.

Native Thread turns use the same boundary. Admission owns a short-lived fenced
lease from before bootstrap/session resolution until the provider turn is
accepted. A drain activated while that code awaits preserves the already
admitted operation, while later web, queue, steering and recovery starts fail
with a typed maintenance result. Queue drains restore the same FIFO item without
consuming an execution retry.

Local whole-service rollouts are distinct from tenant-scoped maintenance. A
local Upgrade Plan may enter `service` maintenance only with the existing
upgrade administrator authority and explicit instance-local/cross-scope
coverage. The plan records the service instance, exact source and immutable
Release target revisions, admission epoch, complete hosted-scope inventory and
its fingerprint. Service maintenance gates every hosted scope; it does not make
the requesting principal a tenant administrator. Service-principal use requires
an existing `upgrade:admin` grant and canonical membership in every covered
scope. It also fences workspace creation and every new ActionIntent claim while
the whole process is being replaced; tenant-scoped incident/recovery exceptions
remain unchanged. Unknown coverage fails closed.

The supported local installer accepts only a fresh passing preflight from that
service drain. It checks active native admissions, ActionIntents and worker
assignments across all scopes. Before consuming the installation guard, it
matches both checkout origins to the exact running build's repository identity,
requires an unchanged clean live checkout, and verifies the complete candidate
archive against the source checkout. Commit-object presence alone is not
sufficient. An incomplete live object database receives one bounded exact-commit
refetch with hooks and recursive submodules disabled; received objects remain
packed so materialization need not modify historical loose-object directories.
Origin changes, incomplete objects, fetch failures, archive mismatch and dirty
or changed HEAD fail before the guard or service restart. Already-complete
candidates do not fetch into the live repository. The helper emits a structured
`candidate_objects_verified` result with the exact revision, archive digest and
fetch disposition into its existing deployment log; this is diagnostic evidence,
never an installation grant. After checkout it requires exact HEAD, a clean
tree and the same archive digest before restarting. Object materialization
changes no API or persisted schema and retains canonical authority and rollback
gates.

Git subprocesses use a child-only `0022` umask so privileged fetches and
checkouts leave tracked code, traversed directories and revision metadata
readable by the unprivileged service account. The operator's own restrictive
umask remains unchanged for journals and runtime data. The installer does not
recursively change checkout permissions or ownership: ignored DATA, credentials
and other untracked paths retain their existing protection. This access contract
does not relax the clean-tree, exact-source, canonical qualification or restart
verification gates.

The existing Control Center includes recent completed Plans and a scoped local
installation inspector. Its admin/MFA (or `upgrade:admin` service) read joins the
canonical Plan, Release, post-upgrade Evidence and up to eight matching installer
reports in the existing Evidence domain. Helper reports use `runtime_result`
with informational results, fixed diagnostic fields and producer attribution;
they are untrusted claims and cannot satisfy qualification or completed
verification. Credential-bearing URLs, arbitrary log/exception bodies and
unrecognized fields are not returned. A completed Plan is displayed as verified
only with valid matching canonical post-upgrade Evidence. A process is reported
running/exited only from the same-instance scoped live `Popen` handle; missing,
restarted or mismatched handles remain unknown. Stored PIDs or helper reports
never establish process liveness. Reads add no model calls or mutations.

Installer diagnostic writes are best-effort, use existing Evidence authorization,
and do not run between consuming the one-shot guard and checkout. Enforced
deployments without authenticated diagnostic reporting show an unreported state
while retaining every installation gate. The API projection is additive; no new
persisted Plan field, schema migration, privilege, or parallel deployment state
is introduced.

Under the deployment lock, immediately before
switching the checkout, an inherited one-shot channel asks the still-running
control plane to revalidate the exact plan, Release, evidence, instance,
admission epoch, scope fingerprint and live/source revisions. Missing or stale
revalidation aborts without restarting. A receipt file is not authoritative.
Root-capable tools can still bypass the helper; that explicit host-operator
boundary remains visible and must not be described as universal protection.

After the qualified switch, the local installer checks liveness, exact running
revision metadata and the same source/target service-maintenance plan. Readiness
remains deliberately false while workers and autonomy are drained; the operator
must still verify the plan and restore those controls before claiming readiness.
The fixed service revision override follows the installed checkout and any
compatible rollback. Local overlays cannot change the immutable qualified
candidate. Automatic rollback is allowed only when the canonical qualification
and current plan both retain rollback availability; otherwise maintenance stays
in place for operator recovery.
Host operators can obtain the same fresh canonical check from
`POST /api/upgrades/{plan_id}/qualify-local-deployment`, with the exact service
instance, source/target and preflight evidence. It retains administrator+MFA or
scoped upgrade service authority and the complete-hosted-scope checks. A CLI
guard must perform this request under the deployment lock at the one-shot
verification boundary; a previously saved response is not an install grant.

## Bounded durable drain reads

Whole-service drain counts and hosted-scope enumeration page the canonical
`intents:` records and assignment records instead of decoding retained receipt,
verification, inbox, event and enrollment catalogs. Each visited ActionIntent and
Assignment is still fully validated, including terminal rows; current Worker
records are validated too. Every page is read fresh from the existing StateStore.
No cached count, lease-only alias or pending-only index can establish zero work:
claimed, executing, uncertain and reconciliation-required actions all remain
active, across every hosted tenant/workspace. Legacy backend/service interfaces
retain their existing full validated reader fallback.

The ordinary Upgrade backup endpoint maps expected Recovery conflicts, including
cooperative memory refusal, to the existing JSON 409 response contract (other
Recovery errors remain JSON 400). Unexpected errors remain failures. A refusal
response is not proof of a missing manifest: inspect the same canonical plan,
catalog and retained journal. It never permits automatic capture replay.
These read changes do not change memory estimates, cgroup limits, source pins,
authority, maintenance/drain fencing, crypto or isolated restore requirements.
Actual production capture and restore must still pass their unchanged guards.

## Offline pre-install backup capture

When the installed backup implementation needs a reviewed source correction
before installation can qualify, `python -m codex_web.preinstall_backup_cli`
provides a narrowly scoped, single-capture path. It composes canonical Identity,
Crypto, Evidence, Audit, Recovery and Upgrade services without application startup,
identity/definition bootstrap, worker admission, provider actions or migration of
missing worker/action/audit records. It consumes an **existing** authorized MFA
session or service credential through a file descriptor; credentials and exception
bodies are never written to the journal. Both `upgrade:admin` and `recovery:admin`
apply to service credentials, in addition to existing key and hosted-scope guards.

The operator first completes a reviewed whole-service drain and preserves all
unknown inputs and origins. The helper runs under the existing deploy lock and
the operator qualification lock, with exact clean installed/producer revisions,
service instance, PID and invocation pins. A separately supervised cgroup must
actually cap this helper at no more than its explicit operation budget with zero
swap. `--memory-budget-bytes` defaults to 6 GiB; a separately reviewed operation
may select a different finite byte ceiling through the existing authenticated
operator procedure. This configures only the supervised capture/restore helper,
never the production service or an ordinary execution profile. The existing
upgrade/recovery administrator authority, protected operation configuration and
supervisor own this choice; repository/model content cannot grant or select it.
The helper does not set a cgroup limit. It observes the actual finite current
cgroup at entry and at both capture boundaries, refusing an unknown/unlimited
cap, a cap above the selected ceiling or nonzero swap. Claimed arguments are
insufficient. Canonical Recovery host and every visible ancestor headroom check,
and its conservative allocation estimates, remain unchanged and can refuse even
under the selected cap.

`pre-stop-qualified.json` records `memory_budget_bytes` separately from its actual
`memory_cap` observation; capture completion and refusal retain the selected
budget. A separate cold restore verifier must bind that exact same operation
budget to the immutable capture proof and observe its own real cap and current
headroom. A budget mismatch refuses verification. Raising a number neither
reserves memory nor establishes backup/restore qualification. Capacity planning
must account for the stopped service, helper, host and ancestor limits, while
retaining the original finite offline deadline and positive-writer-exit rule.
The runtime data directory and its database, key and backup paths are frozen
from the pinned installation's `data` layout and rechecked at both boundaries.
An alternate destination cannot register a `local` manifest which only the
helper, rather than the installed service, can read. Retargeted data/backend
symlinks refuse capture or association.

The helper obtains authenticated observations from the **running** installed API:
its version, actual Recovery executor idle status, and a fresh canonical Upgrade
preflight with zero action/assignment/native/fenced counts. Capture alone may
proceed despite the exact stale RPO/restore-freshness/RTO qualification blockers;
other compatibility, authority, scheduled-owner or recovery failures still refuse.
A settled scheduled UNKNOWN can leave the reconciliation health blocker present
when a newer captured backup has not yet earned a matching restore PASS. Capture
alone may proceed in that case only after rereading canonical Recovery and
independent Evidence/Verification: every unknown custody is explicitly released,
all retained fencing/owner/effect proofs remain valid and unexpired, and no
scoped scheduled firing is running. Missing, revoked, non-independent or invalid
proof still refuses; local executor idleness never proves a foreign owner idle.
The same check repeats at both capture boundaries. This neither changes UNKNOWN
to success nor qualifies deployment: ordinary Recovery health still requires a
newer matching backup and isolated restore PASS, current policy and RPO/RTO.
It durably writes `pre-stop-qualified.json`. Only then may the reviewed operator
procedure stop the positively drained service. The helper waits at most 30
seconds for that same invocation to exit cleanly, and refuses a replacement,
crash, failed stop or expired pre-stop observation. A successful systemd stop
accepts either ordinary exit code 0 or the expected SIGTERM signal after Uvicorn
shutdown, with the same process/start/exit-time and PID-absence checks. Forced
kill, segmentation fault and non-success service results remain ineligible.
The helper itself never stops,
starts, installs or rolls back a service. The operator procedure must independently
own service restoration, including helper interruption or an unknown result.

The separate offline capture window defaults to 600 seconds. An explicitly
reviewed `--capture-duration-seconds` selects an integer from 1 through 900;
the selection and absolute deadline are journaled before stop and on completion.
The deadline is fixed at the original pre-stop observation and is never reset
after manifest creation. Existing four-argument `OfflineCaptureProof` callers
retain their 600-second bound. The operator must independently review matching
supervisor, restoration, verification and credential limits before selecting a
longer duration; the option does not extend those limits or authorize a retry.
During that window, the pre-stop preflight
is explicitly **historical admission evidence**, not a newly refreshed observation.
Before capture and before association the helper checks the current cleanly
stopped service, sources, locks, canonical hosted scopes, durable zero-work counts,
Release, Recovery policy/scheduled custody, Evidence validity and current actor.
Credential/Evidence expiry is checked against actual current time. Capture uses
the existing owned Recovery executor; cancellation waits for its real writer.
The same finite window supplies cooperative storage deadlines. Expiry refuses
further guarded writes or association; it does not claim that a synchronous
writer has stopped or detach an in-flight operation. Restoration must inspect
the actual helper/writer outcome rather than infer completion from elapsed time.
The created manifest is journaled before post-capture checks. Association uses
canonical Upgrade storage with identity-locked reauthentication and plan-context
CAS; a changed plan or actor retains the backup without falsely associating it.

A disconnected, timed-out, crashed or refused capture must be inspected through
its journal, canonical Recovery manifests and destination state. It is not
permission to invoke capture again, change an unknown outcome, or reuse another
plan's backup. A successful capture does not qualify restore or deployment.
The normal production-sized isolated restore, current service-instance drain
rebinding after restart, fresh full preflight and qualified installer remain
mandatory. Existing Recovery/Upgrade inspectors display the canonical results;
no parallel recovery domain or separate success receipt is introduced.
Capture duration metadata belongs to the operator proof, not BackupSnapshot or
BackupManifest: it does not change the encrypted backup wire, stored schemas,
or OLD-source restore semantics. Recovery RPO still measures actual backup age
from its canonical `created_at`; a longer capture window does not refresh that
timestamp or relax current restore, Evidence, credential or installation guards.
The CLI help and closed operation journal expose this operator-only setting;
it introduces no runtime Configuration record or additional UI authority.

## Migration phases

Steps are declared before execution and must be ordered:

1. **expand** — backward-compatible schema/data/Definition preparation;
2. **rollout** — control-plane/worker/extension deployment under the declared
   mixed-version window;
3. **verify** — health, compatibility and behavior checks;
4. **contract** — cleanup/removal after rollback compatibility is intentionally
   abandoned.

Each step declares kind, handler ID, idempotency, reversibility,
irreversibility, drain requirement and backup requirement.

A failed idempotent step retains attempts/error/Evidence and may be resumed.
A failed non-idempotent step is not automatically replayable.

Every attempt produces upgrade-step-verification Evidence.

## Irreversible boundaries

An irreversible step must be declared as such in the plan. Contract/cleanup
steps that are irreversible must require a pre-upgrade backup.

Before execution:

- a fresh encrypted backup is captured through RecoveryService when requested;
- a canonical ApprovalRequest is created against the exact Upgrade Plan/Step,
  target application version and immutable Release digest;
- the ApprovalRequest references the successful preflight Evidence;
- approval consumption is idempotent and bound to the step execution key.

Once an irreversible step succeeds, irreversible_boundary_crossed becomes true
and rollback_available becomes false permanently for that plan.

For an agent-originated request, the assignment-bound broker can attach the
canonical ApprovalRequest under the actual registered worker service identity.
The original human authorizer is retained in its reason and audit references;
no caller-supplied requester identity is accepted. Request approval while that
bounded assignment is live, after capturing the required backup and before
activating service-wide drain. Then release the assignment and refresh drain,
backup, restore verification and preflight as required for execution. Human
decision and exact-target consumption use the existing canonical APIs; this
request-only bridge does not waive any execution gate or change an existing
human request's requester.

A process interruption after approval consumption can resume only by using the
same canonical ApprovalRequest consumption/idempotency identity.

## Rollback truth

Rollback is only advertised when the compatibility profile explicitly permits
return to the source application version and no irreversible boundary has been
crossed.

A rollback claim is rejected after an irreversible schema/Definition contract
boundary. A successful rollback record produces
upgrade-rollback-verification Evidence and exits maintenance mode.

Actual package/process rollback remains a deployment operation; the Upgrade
domain owns whether that rollback is compatible and truthful.

## Post-upgrade verification

Final verification requires:

- all declared steps completed/skipped;
- target StateStore schema active;
- frozen Definition checksums still valid or explicit Definition migrations;
- active workers inside the declared rollout window;
- enabled extensions compatible with the target host.

The result is canonical POLICY_EVALUATION Evidence with source
upgrade-post-verification. PASS completes the Upgrade and exits maintenance;
FAIL keeps the upgrade in a failed state for recovery/rollback decisions.

## API and UI

/api/upgrades exposes plan creation/list/read, preflight, tenant drain,
whole-service local drain, backup capture,
irreversible-step approval, step execution, Definition migration recording,
post-upgrade verification and rollback recording.

Human mutation requires administrator authority + MFA; service principals
require upgrade:admin.

The Autonomy Control Center and operator workspaces display source/target
versions, mixed-version warnings, Definition incompatibilities, tenant/service
drain scope, service instance, covered-scope and native/fenced-admission state,
exact deployment source/target revisions and preflight Evidence ID, step
progress/Evidence, irreversible boundaries and rollback availability. The
operator upgrade/runbook documentation describes the guarded local path and its
external bypass limit.


The local Skill scanner persisted-contract transition uses the registered
`skill-scan-contract/1.0` handler and canonical irreversible-step workflow. See
[Skill security admission](skill-security.md#enforced-scanner-admission-and-policy)
for its service drain, verified snapshot, old-worker exclusion and write gate.
It disables rollback on earlier plans atomically and on later plans conservatively;
a new plan cannot make retained incompatible rows safe for an old binary.

The `action-publication-index/1.0` contract follows the same canonical approval,
backup, service-drain and irreversible-boundary workflow. Install its compatible
source first while original routing remains unavailable for unprepared indexes.
On that installed source create the contract plan, capture/verify the fresh
backup, request/consume exact approval and execute the contract step. The handler
does not mutate keyed metadata before the engine records the boundary. Existing
post-upgrade verification activates and resumes the 1.4 index backfill only under
the succeeded boundary/current service instance, drain and epoch; unready or
partial activation cannot release maintenance or produce PASS verification.
Startup and webhook inspection do not perform this irreversible transition.
See [ActionIntent publication lookup](action-intents.md#verified-publication-notification-lookup).

## Patched native Goal control dependency

`patches/codex/goal-control-v1/UPSTREAM.json` is a public-source build qualification
input, not an enabled runtime or a Release. A runtime implementing native lifecycle
control must enter the existing Release/UpgradePlan and compatibility flow with
exact upstream/patch/lock/binary digests, independent build/race/transport/isolation
Evidence and explicit native Goal database migration/rollback rules. Docker's
existing CLI version arguments are not changed by this prerequisite. No Profile,
Definition or grant may infer qualified support from a version string or adapter
method. Preserve original native Goal/objective/accounting through drain, migration
and post-verification before any separately authorised installation.

Fresh authentication observations are compared by canonical actor authority,
excluding only `authenticated_at`. The stopped-service capture still reauthenticates
before capture and at the identity-locked association writer; changed identity,
tenant, roles, teams, assurance, session/token reference or scopes refuses. A
revoked credential cannot associate a captured manifest. The observation-time
comparison does not grant authority or qualify a backup or deployment.


### Cold writer source and upgrade target

A cold pre-install helper can publish canonical Recovery verification Evidence
before the original service is restored. Read-only decoding of a newer domain
model is not evidence that its later writes remain source compatible. In
particular, ArtifactEvidence 1.3 reads may migrate in memory under a 1.4 tool,
while publishing restore-verification Evidence persists 1.4 and makes the 1.3
source reader refuse. That is an installation migration boundary, not an
ordinary verification side effect that may be ignored.

`preinstall_backup_cli` therefore distinguishes `--expected-producer-revision`
(the complete executable tool checkout) from
`--expected-upgrade-target-revision` (the exact Release/Upgrade target).
By default the separate offline verifier must use the installed source revision; the
separate target remains subject to all existing plan, Release, authority,
preflight, lock, PID, storage-path, memory and deadline checks. The verifier must check its source/tool pin before credential use and at each
guarded boundary. A target-executable verifier refuses visibly. Capture keeps
its existing default target binding and separately audited write set; this does
not authorize target-schema Evidence publication during verification.
The pre-stop receipt retains both revisions. An offline verifier must make this
same distinction, pin all imports to one source-compatible checkout, and bind
the separate target against the original capture plan rather than overwrite
historical producer facts. It must still perform actual isolated restore
verification and publish its genuine canonical Evidence under the source
contract. Neither service memory limits nor helper capacity are inferred from
this source choice; supervised finite limits and real capacity admission remain
required.

A recovery-only backport can be admitted through an explicit qualified-tool
proof. This is a separate immutable tool revision derived from the exact
installed base, never the upgrade target relabeled as the base. The default
source-equality rule remains unchanged without that proof. Before credentials
are used, static validation pins the complete clean base/tool/target trees,
private proof identity and exact reviewed diff. Only the bounded Recovery/crypto
algorithm and its proof/CLI seams are eligible; domain models, storage writers,
migrations, dependencies and other executable files remain byte-identical to
the base. All loaded application-package modules must belong to that one tool
tree. A path allowlist or a local compatibility assertion alone is insufficient.

After canonical authentication, the existing scoped Artifact/Evidence services
must confirm the actual tool archive digest and valid independent compatibility,
CI and review evidence for those exact bindings. Revalidate that qualification
at the existing guarded publication boundaries; invalidated, expired, changed
or foreign-scope proof cannot authorize a write. Journals retain the actual
installed base, executable tool, upgrade target and qualification references.
The tool preserves the base's actual persisted contracts, including its
ArtifactEvidence writer. The historical 1.3-to-1.4 writer transition above is
still incompatible; newer-schema publication is never repaired by downgrading
an envelope. This exception grants no capture, migration, provider or native
execution authority and changes no actor, lock, custody, deadline, freshness,
memory or semantic restore check.

No live-state envelope downgrade, skipped verification, mixed source imports,
legacy provenance relabeling or rollback promise follows from this distinction.
The later target installation still requires its declared forward domain
migration, compatibility policy Evidence and fresh recovery qualification.
