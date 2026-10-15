# TaskSource writeback coordination

Canonical Work Item state remains authoritative. External task systems receive a
projection of selected canonical fields through one keyed writeback coordinator
per authoritative source identity.

## Coalescing

Asynchronous writeback requests are keyed by tenant/workspace plus
TaskSource type, source instance, and external item ID. Repeated updates replace
the pending desired snapshot instead of creating another provider task.

One event-loop turn is intentionally yielded before the first provider read so
synchronous bursts collapse to the latest desired owner/stage. A generation
counter is checked after the provider read; a superseded generation is not
mutated externally and the coordinator immediately processes the newer desired
state.

## Provider-efficient projection

Adapters may implement the optional combined-write contract. The coordinator
then performs:

1. one provider read
2. a generation check
3. one combined projection of owner + canonical stage
4. zero provider writes when the provider already matches
5. at most one provider mutation when either/both fields differ

The GitLab adapter implements this contract by editing owner/status labels and
open/closed state in one issue update. For a merge-request identity, combined
projection edits only owner/status labels through the merge-request endpoint.
The adapter validates the exact positive MR identity and provider response,
preserves unrelated labels, and skips unchanged labels. Canonical progress
never merges, closes, reopens, or changes the draft or branch of an MR; its
provider lifecycle remains controlled by governed merge-request actions.
The same-number issue is never used as a substitute.

Adapters without the combined capability continue through the provider-neutral
owner/state write contracts while still benefiting from keyed task coalescing.

Adapters may advertise the additive `delivery_state_write` capability and
implement `write_delivery_projection`. It receives the canonical owner/business
stage plus an independent `delivery_pending` flag. The GitLab adapter and its
secret-bound facade support this extension through the same combined mutation
and per-call secret-use boundary. Pending handoffs project awaiting-confirmation
status even when the business stage remains failed; after acknowledgement, status
reflects the business stage. Receipt delivery does not constitute validation.
Incoming awaiting-confirmation feedback cannot promote a pending handoff's
business stage. Legacy adapters retain their existing owner/state contracts.

Nonterminal external workflow labels cannot change the business stage, cancel
the pending recipient intake, or reassign its canonical sender/action owner.
The original handoff remains pending with its original request time and
awaiting-confirmation status until canonical ACK, progress, expiry or resolution.
Provider snapshots/labels remain observable as source data; they do not fabricate
recipient acceptance. Authoritative external closure retains its existing terminal
reconciliation path. Preserved source stage changes emit a canonical work-item
event so operators can distinguish retained delivery from provider projection.

Delivery-pending state participates in coalescing and applied fingerprints, so
creating or acknowledging a handoff cannot be suppressed merely because owner,
business stage and provider revision are unchanged. Operations diagnostics expose
this state alongside the existing owner/stage/revision fields.

## Canonical-state safety

A successful atomic SLA handoff expiry schedules the same coordinator using
the current canonical record. Replaced, acknowledged or closed handoffs that
fail the expiry comparison do not schedule expiry writeback. This removes stale
awaiting-confirmation provider labels without creating recipient acceptance or
extending handoff deadlines; timeout policy remains unchanged.

Provider response metadata is merged into the latest canonical Work Item record.
An older scheduled copy is never saved wholesale over newer canonical
owner/stage state.

Provider revision participates in the applied fingerprint. This prevents
feedback loops for unchanged provider revisions without suppressing
reconciliation after an external provider change.

## Failure and retry

Each keyed coordinator task owns bounded exponential retry. HTTP 429
`Retry-After` is honored up to the configured safety cap. Provider failure does
not roll back canonical Work Item progress. The latest desired canonical state
remains queued until success or the bounded retry budget is exhausted; a later
canonical update creates a fresh reconciliation opportunity.

## Observability

`GET /api/operations` exposes TaskSource writeback state including:

- scheduled/coalesced/skipped/applied/retried/failed counts
- provider read/write counts
- pending queue depth and oldest pending age
- active coordinator task count
- current backoff count
- per-item desired/applied owner, stage, and provider revision
- bounded last-error summaries

The coordinator is lifecycle-owned and cancelled during controlled application
shutdown.

GitLab MR owner and status projection supports both combined adapters and the
individual `write_owner`/`write_state` routes used by `SecretBoundTaskSource`.
Each individual operation obtains a fresh credential through the existing
secret-use boundary, reads the exact MR, preserves the partner and unrelated
labels, and sends a labels-only PUT only when its projected value differs.
The private MR label helper shares exact identity/current-snapshot/provider
response validation across all three routes. MR lifecycle, draft and branches
remain governed separately. The secret-bound facade does not acquire a new
combined capability; rotation/revocation and existing issue behavior remain
unchanged. Operation counters on the individual writeback path are not proof
of actual HTTP mutations; verification checks the exact provider calls.

## Durable canonical success and provider projection (1.0)

Canonical Work Item saves create or supersede `source_projection` in the same
record write. Its SHA-256 `desired_revision` binds tenant, Project, source item,
operational owner/stage and the meaningful-progress revision. Provider receipt
and activity clocks do not generate a new desired revision or owner dispatch.
The existing Work Item record is the durable reconciliation source; coordinator
queues remain execution aids, not a second outbox or scheduler.

`TaskSourceProjectionState` schema `1.0` records pending, failed, conflict or
succeeded independently of committed domain progress, with exact target identity
and revision, desired role/provider account fields, attempted actor/time/count, mapping checksum,
observed provider revision/state and protected source-state baseline. Reasons
are bounded code-owned identifiers and HTTP status only: raw response bodies,
exception strings, tokens and request URLs are never diagnostic evidence.
Attempt and outcome evidence is appended to the existing canonical Work Item event
history, retaining exact desired/mapping revision attribution after supersession.
These evidence events do not replay domain progress or dispatch an owner.
Storage/programming failures are not provider success. A cancelled/lost response
may leave pending evidence; GET readback remains authoritative. Progress never
needs replay to recover a projection.

`POST /api/work-items/{ref}/projection-retry` requires the exact current desired
revision and existing `work-item.operate` authority, tenant and requested Project
checks. The assignment broker uses existing `work_item.reconcile` capability and
its assignment, resource and ownership checks; adding the operation grants no
role/profile authority. Retry resolves the current credential-bound adapter and
configuration, reads current canonical and provider state, and only projects the
provider. It creates no progress, handoff, Attention, dispatch or execution retry.
Explicit source reconciliation supersedes an existing outbound desired revision
when it changes canonical state; read-only discovery creates no new outbound work.
The canonical repository's transactional update checks desired revision before
merging receipt/evidence fields, so stale results cannot replace newer ownership.
A restart requires no background model call: the operator can retry a persisted
pending/failed record through this same endpoint. The existing coalescer retains
bounded retries for transient transport failures; conflicts, mapping failures and
permanent provider denials require explicit operator reconciliation.

### GitHub account mapping and concurrency

Project `TaskSourceConfiguration.provider_settings` accepts
`GitHubTaskSourceSettings` schema `1.0`, including `assignee_mapping` from exact
operational role identifiers to GitHub logins. These are deployment-specific
adapter configuration values, like existing Jira/ServiceNow settings, rather than
reusable execution definitions or authority grants. No new Definition Registry
catalog is introduced. The exact configuration checksum is attributed to each
attempt. Existing Projects migrate to an empty mapping: there is no role-name
fallback or guessed human account. Syntax and unambiguous reverse mapping are
validated on configuration save; GitHub repository assignability is validated
before adding a mapped account. Missing mapping is a visible failed projection.
Unknown/manual provider accounts do not become operational roles on ingestion.

Assignee projection uses GitHub's additive assignee endpoint, never replacement
of the assignee array. Null canonical owner does not clear human assignees.
Manual accounts, including accounts added during awaited requests, are retained.
Reads and verification surround the additive mutation; response loss is recovered
by readback before another write. A changed external revision conflicts unless
readback verifies the complete desired projection. The original protected issue
state persists across retries and is checked after assignability and assignee
awaits; a human close/reopen yields conflict and never a reverse state PATCH.

[GitHub conditional-request guidance](https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api#use-conditional-requests-if-appropriate)
does not support unsafe conditional requests unless an endpoint explicitly
supports them. The [issue update contract](https://docs.github.com/en/rest/issues/issues#update-an-issue)
documents no revision compare-and-swap. Accordingly guarded writeback reports
`github_state_transition_requires_reconciliation` when a close/reopen is needed,
and sends no state PATCH. Operators inspect canonical versus observed state and
use the existing governed `code-host.issue.update` ActionIntent path for an
intentional provider issue-state action, then retry only the projection to verify.
Read/verify evidence (`read_verify_no_state_cas`) is an observation, not atomic
provider synchronization or an enduring claim against subsequent human edits.

The existing Work Item operator view exposes the two outcomes, bounded failure
reason, desired/observed/protected revision provenance and a projection-only retry
button. The Project TaskSource editor manages the explicit mapping. This extends
the Work Item UI tracked by #102 and #127; it adds no separate dashboard.

### Compatibility and drain

The optional Work Item field is additive; old records load without it and acquire
pending evidence on their next canonical save. Unknown nested schema versions
fail validation. Mapping publication uses the existing admin-controlled Project
binding API, with current credential use checks on every attempt. There is no
new destructive storage migration, secret material or autonomous scheduler.
Drain old API/writeback processes before enabling these settings: an old writer
can drop unknown evidence and retains unsafe role-to-account behavior. Do not run
mixed old/new writers. Rollback requires draining writeback and removing the new
GitHub settings under the same Project admin boundary; retain the canonical
records/evidence for readback and reconciliation, and never replay progress.
