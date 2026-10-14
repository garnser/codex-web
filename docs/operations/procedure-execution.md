# Execute and recover a pinned procedure

A procedure describes requirements; current canonical policy grants authority.
Use an existing qualified assignment and workspace. Starting a procedure does not
install a provider, start another worker, enable host mutation/network access or
satisfy a missing prerequisite. Native operational tools and direct adapter paths
remain unsupported. Do not use a prompt reminder as enforcement.

Publish/review the Skill through ordinary Definition/Skill security controls.
Provide immutable managed input and executable diagnostic Artifacts with their
checksums. The input snapshot contains only the declared typed values and
canonical references. Credentials stay behind SecretBroker; assignment secret
references must already be authorized.

Start with the exact DefinitionReference and input Artifact ID through
`POST /api/execution-workers/assignments/{id}/procedure/start`. Advance through the
same assignment's `/procedure/advance` endpoint. Each request handles at most one
step. Read `/procedure` or the existing assignment inspector to see exact pins,
state, blockers, attempts, approval, Evidence and ActionIntent references. A
persisted backoff means wait until `not_before`; refreshing status does not run
models or authorize work. The remaining integrated UI is tracked in
[#1428](https://github.com/garnser/codex-web/issues/1428).

For a protected step, confirm the current target/scope and existing canonical
approval state. Only policy-required requests are created. Decisions use ordinary
ApprovalRequest APIs, including live eligibility, step-up/quorum and exact target
consumption. Resuming an approved step reuses its existing bound request; do not
ask the user to grant the same authority again.

The controlled qualification workflow in `tests/test_procedure_execution.py`
backs up a temporary fixture configuration as an actual managed Artifact, checks
disk through an actual isolated worker, applies one bounded change through a
registered provider/ActionIntent, then observes every expected worker and HTTP
readiness through a separate scoped read-provider ActionIntent. Arbitrary diagnostic
Artifacts never inherit parent network permission, container/broker channels or
SecretReferences. A real network-enabled-parent regression proves that an Artifact
cannot POST to a reachable test-owned endpoint while the governed read-provider
path remains supported. It records canonical
Evidence and receipts. These are controlled qualification effects, not delivered
GitLab operations or permission to mutate GitLab/VeridataOps. The fixture's
conditional rollback invokes the original provider result through the canonical
rollback engine and verifies restoration separately.

When interrupted, inspect the durable state before advancing:

- **Before provider execution:** existing claim recovery may return an expired,
  never-started claim to pending. Keep the same intent and idempotency key.
- **After a possible effect without receipt:** `reconciliation_required` blocks
  every subsequent mutation, including rollback. Inspect ActionIntent history and
  use canonical provider observation/reconciliation. Never replay configuration,
  restart a service, or synthesize success to find out what happened.
- **Awaiting approval:** decide the existing exact ApprovalRequest; restarting the
  interpreter retains its ID. Rejected/expired/revoked approvals stop visibly.
- **During verification:** the durable forward receipt is retained. A declared
  network-disabled/read-only Artifact diagnostic may repeat only within its
  existing attempt/time budget. HTTP/service observations use their existing
  registered read provider and durable ActionIntent receipts/reconciliation.
  Readiness requires all declared Evidence; one worker passing is insufficient.
- **Known failure/retry exhaustion:** inspect the canonical failure classification.
  Only transient, idempotent, bounded retries are automatic. Otherwise address
  Attention and create a newly admitted recovery when policy permits.
- **Revocation, changed scope/facts, expired lease or incompatible definition:**
  restore the required canonical prerequisites through their owning services.
  Historical lease observations and exact content pins do not restore authority.
  Do not edit the checkpoint to bypass a denial.

For provider rollback the published conditional recovery step must name its
originating mutation and the same provider/action reference. The interpreter
requires its actual successful receipt, compatible current provider contract and
ActionDefinition, current authority/Resource/secret references and live fence.
It calls the original intent's rollback engine once. Separate inverse-action
procedures remain distinct from this mechanism.

An interrupted rollback with no receipt remains unresolved. Do not invoke it a
second time or retry the original forward action, even when the provider normally
supports forward idempotency. If a rollback receipt was durably recorded before a
local interruption, canonical reconciliation can repair status from that exact
receipt without any effect replay. A provider without trustworthy observation or
a durable receipt remains an explicit operator recovery requirement. Unavailable,
incompatible or failed rollback leaves canonical Attention. Even a `rolled_back`
intent does not complete recovery until the declared restoration checks publish
valid Evidence; the procedure then reports `recovered`, not original success.

Procedure failures upsert canonical Attention with assignment, action and owner
references. Use the related Incident's existing timeline/recovery controls when
this procedure belongs to an Incident. A successful declared restoration resolves
the procedure Attention; it does not close an Incident or assert business recovery
beyond the recorded checks.

For deployment rollback, worker state 1.11 and ActionIntent document 1.5/keyed
metadata 1.2 require compatible readers/writers. Drain before upgrades; take and
verify a canonical backup with content/secret/key dependencies. Do not run mixed
writers or downgrade a database containing new checkpoints. A restored snapshot
requires explicit lease/authority checks and action reconciliation; it never
implies permission to replay pending effects.
