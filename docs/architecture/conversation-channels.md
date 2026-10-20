# ConversationChannel provider-neutral conversational intake

## Purpose

ConversationChannel is the provider-neutral inbound conversation boundary for
Slack, Telegram, Microsoft Teams and similar collaborative systems.

It is intentionally separate from TaskSource:

- TaskSource represents authoritative external work records.
- ConversationChannel represents conversational identity, messages and provider
  interaction semantics.
- AttentionItem remains the canonical attention/escalation model.
- Work/Goal/Decision state remains canonical codex-web state.

Provider message, thread or user IDs never become canonical application
identity or authority.

## Code-owned contract

Each adapter declares an immutable contract version, provider type/instance and
explicit capabilities:

- events;
- history;
- threads;
- attachments;
- references;
- mentions;
- commands;
- reactions;
- edits;
- deletes;
- cursors.

Unsupported capabilities fail explicitly. Providers do not emulate semantics
they do not support.

The built-in normalization adapters intentionally differ:

- Slack: threads, attachments, references, mentions/commands, reactions,
  edits/deletes and cursor metadata.
- Telegram: topics/replies, attachments, references, mentions/commands,
  edits and update cursors.
- Microsoft Teams: Graph-style chat/channel messages, attachments, references,
  mentions and edit/delete change notifications. Graph subscription/auth
  configuration remains an integration/extension concern rather than being
  fabricated inside the normalization contract.

## Normalized identity

ExternalConversationRef contains provider type, provider instance, conversation
ID and optional external thread ID.

ExternalMessageRef adds the provider-owned message ID.

Those references are provenance only. Canonical thread/work identity is created
by the existing routing layer.

ConversationSenderRef contains untrusted provider metadata. Provider user IDs,
tenant claims, roles or bot claims never grant codex-web authority. The
authenticated canonical actor that ingests the event determines tenant/workspace
scope.

## Canonical events and deduplication

Every normalized inbound event is committed first as
`conversation_channel.event`.

The canonical ingress idempotency key combines authenticated tenant/workspace,
provider instance, connection, Project, conversation and provider delivery ID.
Connection and Project come from canonical authenticated ingress; provider
payload claims never select them. Message/thread/sender identity, complete
substantive original input and captured authority must remain compatible on a
duplicate. Validation occurs inside the existing event/receipt transaction
before transport ACK. A changed input or provenance gets no ACK and cannot
replace the original retained input.

Production ingress captures authenticated source metadata before awaiting the
database-backed routing/Profile snapshot in the existing thread executor. Source
changes during capture fail before retention. Routing authority is revalidated
off the event loop against the immutable retained snapshot; these reads never
replace the atomic event/receipt transaction or permit an early transport ACK.

Only top-level Socket Mode `envelope_id`, `retry_attempt`, `retry_reason` and
`accepts_response_payload` are transport retry variation for input compatibility.
They remain in the first protected original payload when supplied, but are
excluded from its substantive digest. Nested message fields with those names
remain substantive. Transport tokens/credential response URLs are excluded
before retention. A retry preserves the first receipt and encrypted input;
current settings never reauthorize or rewrite its historical authority.

Providers can retry the same provider message under a different delivery ID.
ConversationChannel maintains tenant-scoped external-message position and rejects
a non-newer retry as stale before routing.

Message state keys include organization/workspace as well as provider/message
identity, so identical Slack/Teams/Telegram IDs in separate tenants cannot
collide.

## Ordering

When a provider has a monotonic sequence, a lower/equal sequence is stale.

Without a sequence, normalized occurrence time is the primary ordering position.
Provider-specific adapters must therefore normalize edit/delete occurrence time
to the provider change timestamp, not the original message timestamp.

Slack message edits use the edit timestamp. Telegram edits use edit_date.
Teams edits/deletes use Graph lastModifiedDateTime/deletedDateTime.

Opaque revisions are retained for provenance but are not treated as universal
cross-provider ordering tokens.

## Edits, deletes and reactions

A newly-created provider message can route into canonical thread/work semantics.

Later edits, deletes and reactions update provider conversation state only.
They do not replay existing canonical work automatically.

This prevents a Slack edit, Teams change notification or reaction retry from
creating duplicate Work or turns.

## Unknown routing outcomes

The canonical event and routing claim are durable before downstream routing.

If routing raises after the claim, the receipt becomes
`requires_reconciliation`. Automatic retry of the same delivery does not
blindly route again, because the downstream outcome may be unknown.

This follows the same fail-closed external-outcome principle used elsewhere in
codex-web.

## Attachments and references

Adapters normalize attachment/reference metadata only. ConversationChannel does
not download arbitrary provider content into model context.

Attachment URLs/IDs remain external references unless another governed
Artifact/Evidence flow explicitly materializes them.

## Slack and Telegram migration

The production Slack webhook/backfill, Slack socket runtime, Telegram webhook
and Telegram polling runtime receive the composed ConversationChannelService.

Their legacy direct BotInboundMessage route is retained only as a temporary
compatibility fallback while the runtime extraction/refactor completes.

Canonical Work/Thread routing semantics are unchanged.

## Microsoft Teams

TeamsConversationChannel normalizes Microsoft Graph message/change-notification
shapes without changing Work/Goal/Decision schemas.

The normalizer is deliberately read/inbound-only. A deployment may pair it with
an extension that owns Graph subscription lifecycle and SecretReferences.

No direct provider send/update/delete API exists on ConversationChannel.

## Outbound actions

Outbound provider delivery is project-bound and single-target. The canonical
thread owner, binding project, and connection project must agree; unresolved or
mismatched ownership fails closed and is recorded in runtime telemetry. For
each provider, delivery selects exactly one binding using active reply, reply,
primary, and recency precedence. A completed turn must never fan out merely
because the thread has additional non-threaded bindings.

Background work dispatch retains the newest captured human reply context rather
than constructing a channel-only target from its triggering binding. Another
channel is eligible only through a canonical binding for the same native thread,
Project, provider and nonempty connection identity. Targets must match that
binding and contain an actual external message/thread reference; delivery
receipts cannot substitute for inbound context. Equal recency prefers the primary
binding. A newer human request on a secondary channel keeps its own context.
When no valid capture exists, the triggering conversation remains the fallback.
Queued turns retain their selected target; stale-thread replacement revalidates
the retargeted canonical bindings. Identity recovery preserves original message
recency rather than marking old captures as newly received. Existing conversation/queue inspection and
single-target outbound delivery show the resulting attribution without a new
operator control or model call.

Consequential external mutations—messages, replies, reactions, subscription
changes or other provider writes—must use the existing ActionIntent /
ActionProvider authority/evidence path.

ConversationChannel does not grant outbound authority simply because an inbound
provider connection exists.

## Extensions and secrets

ConversationChannel is an extension type with its own manifest entrypoint.

Extension registration can attach an availability gate so disabled,
quarantined or incompatible installations cannot resolve their adapter.

Credential material never belongs in ConversationChannel normalized events.
Existing bot/provider integrations use SecretReferences/SecretBroker. The
capability/provenance inspector exposes no raw secret material.

## Operator inspection

The read-only API exposes:

- code-owned provider capability contracts;
- recent tenant-scoped canonical message states;
- recent tenant-scoped routing projection receipts.

The Bot Integration dialog uses this API to show provider capabilities and
routing provenance. It does not create shadow integration state.

## Failure semantics

- unsupported capability -> explicit capability error;
- incompatible contract -> fail closed;
- duplicate delivery -> duplicate receipt, no reroute;
- same message under a newer delivery but non-newer provider position -> stale;
- out-of-order edit -> stale;
- delete -> provider state marked deleted, no canonical replay;
- reaction -> recorded, no canonical replay;
- routing exception after claim -> requires_reconciliation;
- cross-tenant same external IDs -> independent state;
- provider identity claim -> provenance only, no authority.

## Durable Socket Mode ingress and state compatibility

Conversation **event/provider contract 1.0 remains unchanged**. Persisted
conversation projection state uses **3.0** and reads 1.0/2.0 through additive
migration. Existing messages and receipts retain their identity and outcome;
legacy completed receipts acquire default metadata without becoming replayable.
Legacy pending/claimed receipts without exact protection/authority snapshots
become reconciliation-required. Legacy delivery identities are checked before
new retention so the new key format cannot manufacture evidence of non-start.

The existing receipt retains normalized input before Socket Mode ACK, with the
original ingest identity and connection/project scope. A transaction-assigned
sequence preserves local retained arrival order independently of provider or
wall-clock timestamps. A PENDING receipt is claimed atomically before routing;
restart recovery selects only never-started PENDING inputs. Claimed or uncertain
outcomes preserve input for reconciliation instead of creating a second attempt.
Protected originals and identity digests remain available under canonical data
governance; pending and uncertain receipts are protected from history pruning.
The plaintext compatibility path for non-composed service consumers retains no
new Socket Mode authority. Production composition uses Project-scoped canonical
integration-payload keys and confidential governance records. This uses the
existing scoped canonical conversation store; no second inbox or task catalog
is introduced. Normalized conversation content follows its existing data policy;
interaction response URLs and transport tokens are excluded.

Deployments must fully drain old writers before the state transition. Once 3.0
state is committed, old 1.0/2.0 writers are incompatible; automatic rollback to an
older writer is unavailable without a separately qualified state restoration.
The upgrade plan must declare this state migration and its compatibility boundary.
Receipt APIs/UI show retention and provenance metadata without the stored body.

## Human queue preference

Slack live delivery and history/backfill reject bot-origin messages before the
existing routing layer assigns the `slack` turn source. Queue admission uses that
server-assigned protocol provenance, including `queued:`/`steer:` wrappers;
message text and provider identity claims cannot select a priority or grant
execution authority.

At a thread's scheduling selection, all recognized human requests share equal
priority ahead of automated origins. FIFO is preserved among human requests
regardless of Slack, authenticated web, Telegram or operator origin, and among
automated requests. This is a stable scheduling projection over
the existing durable queue, not a rewrite of physical order or immutable request
snapshots. Active and already-admitting turns are not interrupted. The preference
is scoped to the existing destination thread/agent and retains reply targets,
actor/profile pins, repository settings and execution correlation.

The executor still skips blocked, uncertain and fenced admissions and applies
normal pause/maintenance, authority and resource checks. A blocked priority request
does not prevent later eligible work from progressing. Known prestart deferrals
restore the selected request's original position and counter. Unknown native
outcomes retain their existing recovery requirement and cannot be replayed merely
because their source has scheduling preference.

Queue API/events expose the same per-request classification, total queued human
count, and the backward-compatible Slack-only count. The conversation queue notice explains this preference independently
of native activity/approval/uncertainty and automated admission pause. These UI
fields are projections and do not alter permission or admission. The fixed intake
protocol rule adds no mutable operational catalog, persisted priority schema,
Definition kind, model call, provider mutation or second scheduler.

### Retained routing compatibility

First messages still use the canonical binding lifecycle. The protected ingress
snapshot includes the original auto-binding defaults; a failure after starting
that lifecycle retains a reconciliation claim rather than retrying an uncertain
native bootstrap. Existing bound messages require their captured binding scope.
Missing capture produces a typed reconciliation reason.

Retained active-thread steer interrupts the existing turn once, then atomically
admits the original input at the front of the existing canonical queue and uses
the normal queue/preflight/worker execution boundary. It never uses the raw
resume/start compatibility branch. Captured actor, AgentProfile revision,
repository read/write selection and execution-profile Definition reference travel
with the queued turn. An empty captured selection cannot inherit a broader
thread selection. A changed effective Definition or mismatched assignment fails
closed before native input. Unknown interrupt or start retains reconciliation
and cannot automatically enqueue/start again after restart.

The details command admits `conversation.details.reply` through the existing
ActionIntent engine and `conversation-channel/slack` ActionProvider adapter.
Registration does not grant use: the operator must configure an enabled exact
Project/connection credential-reference binding and canonical `conversation.reply`
authority through existing provider/authority surfaces. Missing binding is shown
as `details_project_connection_action_binding_required` reconciliation. No grants
or provider bindings are installed automatically. Sends have one attempt and no
provider idempotency guarantee; unknown sends remain ActionIntent reconciliation.
Channel inspection links the admitted ActionIntent and distinguishes its admission
from actual delivery. Ordinary messages continue to queue while active.

QueuedTurn adds nullable `ingress_execution_profile_definition` for retained
routing; older ordinary queue entries remain readable. An old retained receipt
without the required protected authority cannot be promoted into routing. Drain
and compatibility rules above apply before downgrading to a reader that does not
enforce these ingress fields.

### Bounds and storage cost

Protected input is limited to 2 MiB per original (no truncation); protected
ciphertext is capped at 64 MiB across canonical receipts. New originals exceeding
capacity receive no ACK. Equivalent committed retries preserve the first
ciphertext/governance reference without registering another governed record.
Concurrent candidates may encrypt locally, but only the winning event/receipt
transaction registers governance metadata. Failed transactions leave no orphan
record. Governed deletion releases retained bytes while preserving the identity
tombstone and non-replay outcome.

Restart selection decrypts at most 100 originals per shard across at most 32
shards, before passing them through the existing bounded worker admission.
Admission mutates only the target thread queue row and the canonical conversation
document atomically via `record_mutate_many`; unrelated queue rows are neither
decoded nor rewritten. The conversation state remains one document, and canonical
event/governance metadata transactions retain their existing document costs.
These changes bound queue-row access, payload size and protected retrieval; they
do not claim to bound reads or rewrite cost of the entire ingress history.

External-message positions also bind connection and Project, so two authenticated
accounts sharing provider instance/message IDs cannot suppress one another's
retained originals. Existing positions without this evidence remain historical;
a new scoped event matching such a position requires reconciliation rather than
inferring non-start or replaying its native work.

Encryption retains the existing canonical key-use audit boundary. A candidate
that loses a concurrent admission race (or later fails storage) may therefore
leave an encryption-attempt audit event, but cannot leave a governed retained-input
record. Committed equivalent retries return before encryption and append neither
key-use audits nor governance records. This avoids retry write amplification for
proven retained duplicates without suppressing cryptographic operation evidence.

Repository capture invokes the existing canonical Resource Catalog selector for
both explicit thread settings and Project defaults (including coordinated
selection), using the authenticated original actor. An unused unavailable default
does not disable an existing binding's valid scratch scope. An unbound request
without a valid default retains a typed repository-selection reconciliation
reason rather than starting an uncertain bootstrap.

Receipt `source_authority_digest` distinguishes authenticated connection/account,
actor and Project provenance from the full immutable `authority_digest` routing
snapshot. Duplicate ACK compatibility validates input/source identity and the
receipt's original digests against the immutable canonical event. Creating a
binding or remembering thread settings cannot itself turn an equivalent retry
into a conflicting new original; duplicates reuse the original receipt and
protection without decryption/audit writes. This ACK is retention proof only:
pending routing still revalidates the full captured scope against current state
and reconciles drift without replay. Older protected draft receipts without the
source digest keep strict full-snapshot compatibility; no source pin is inferred.

Socket Mode pins the canonical source/credential-reference versions before
opening the authenticated transport and rechecks them before each frame is
retained or acknowledged. Rotating a credential in place cannot make an old
socket inherit a new account's canonical provenance. Source drift closes the
old socket through the existing bounded authentication/reconnect lifecycle;
it produces neither ACK nor routing under the changed source.


Native admission retains the canonical ingress service actor for Skill discovery
and attribution. The preflight engine inspects only the already selected
assignment's workspace using its existing internal control actor, then checks the
workspace tenant, execution, subject, resources and live lease. This inspection
does not grant the Slack service workspace administration or relax procedure
requirements. Project reconciliation readiness similarly validates tenant scope
before using an internal principal with the worker and secret-metadata inspection scopes required by
the readiness service; caller permissions and backfill approval remain unchanged.

### Ambiguous routing choices

Routing receipts keep the persisted scalar-map contract. Valid routing choices
are stored as an internal `availablePrefixesJson` string, containing at most 256
nonempty strings of at most 512 characters and 64 KiB of encoded JSON. The
canonical compatibility mapper returns the decoded `availablePrefixes` list and
removes the internal key. Invalid metadata or older receipts without this value
return an empty list; receipt identity, retained input and uncertain outcomes
remain unchanged, and reading a receipt never reruns routing. Provider-supplied
values cannot override the reserved encoded key.

This additive scalar remains readable by the frozen 3.0 state reader and needs
no state migration. Older binaries still omit these choices from responses;
upgrade all ingress owners to obtain the corrected behavior. Bot ingress uses
the same mapper as direct conversation ingress, including truthful pending
results. Historical backfill reports retained pending inputs as deferred rather
than dispatched; its independent polling control is unchanged.


### Selected-route authority and known pre-routing recovery

Protected Slack originals capture the existing deterministic selector's result and
only its selected binding, thread settings, published Agent Profile, execution
Profile and exact Definition reference. Selecting an existing binding does not
resolve an unused auto-binding default. A different role's replacement, settings
or Profile cannot change this snapshot. Selection changes, selected-role authority,
repository/tenant authorization and authenticated source/credential rotation still
fail closed. The source digest excludes route-selection metadata; the source is
pinned before the awaited full capture and freshly rechecked after its routing
reads, during revalidation and bounded recovery. All ingress owners must run the upgraded
selector: older readers conservatively reject the changed routing snapshot.

A failed authority check atomically compares the untouched PENDING receipt before
recording reserved scalar metadata `preRoutingAuthorityRefused`, the changed
snapshot components, `actionOwner` and `authorityRecoveryCount`. If another worker
has already claimed ROUTING or admitted a queue entry, that receipt cannot become
a known-unstarted refusal. Provider routing results cannot supply these reserved
proof fields. Historical generic refusal text is not typed non-routing proof.

The existing bounded pending-input/shard sampler may recover a typed refusal once,
only when the original input/event provenance and its entire original authority
are verified again. The original ciphertext, actor, input identity, scope and
queue admission identity remain unchanged. Fresh PENDING inputs are sampled first,
so an unrestored refusal cannot prevent a new human request from being delivered.
The retry is an atomic compare-and-set to PENDING; normal authority and routing
admission run again. A second refusal exhausts this recovery. ROUTING, unknown
native/provider outcomes and historical generic refusals remain held. Operators
inspect the exact reason and owner in the existing receipt inspector; changed
authority must be reviewed rather than restored merely to silence the refusal.
No new model calls, transport sends, permission grants or parallel queues are added.
The reserved scalar metadata remains compatible with the 3.0 state schema.


### Outbound workflow-claim read gate

Outbound delivery first applies the existing deterministic owner, current-stage
and accepted-handoff recognizers to formatted message text. With no recognized
claim, it does not load canonical WorkItems: issue-number mentions alone cannot
produce a workflow correction. Recognized claims still use the same canonical
validation, current-state corrections and read-error failure behavior. Fully
qualified references retain selected reads; bare references retain full-catalog
ambiguity checks. This gate adds no cache, index, model call, authority exception
or alternate delivery route. Public `findings` and `mentioned_states` continue to
return mentioned states even when no claim exists; only the delivery-specific
callback omits unused discovery.
