# Canonical ApprovalRequest

Issue: #338.

ApprovalRequest is the reusable approval primitive for Codex command/file execution, sensitive Definition publication, Goals, Decisions, ActionIntents, releases, recovery and later production controls. Feature-specific approval state machines are not authoritative.

## Invariants

- Approval eligibility is derived from canonical identity, membership, Role authority and live session assurance. Model output and untrusted content cannot grant approver authority.
- An approval is bound to the exact tenant/workspace, requested operation, target object, target version/digest and resources reviewed by the approver.
- Replacing or materially changing the target invalidates reuse of an earlier approval.
- Quorum is deterministic. Distinct-human and self-approval constraints fail closed when required.
- MFA/step-up requirements are evaluated through canonical identity/session assurance. Authentication secrets are never stored in ApprovalRequest state.
- Decision submission is idempotent. Duplicate provider/browser retries do not create duplicate decisions.
- Expiry uses the durable scheduler; process-local sleeps are not authoritative.
- Approval consumption is attributable and, where required, atomic with the guarded mutation.
- Approval lifecycle transitions emit canonical events.
- Browser/operator UI is a projection over the canonical API. It does not maintain independent lifecycle, quorum or authorization truth.
- Human-attention routing and notification delivery belong to #160. AttentionItems may point to an ApprovalRequest but may not duplicate its approval/quorum state.

## Lifecycle

Canonical statuses are:

- `pending`
- `partially_approved`
- `approved`
- `rejected`
- `expired`
- `cancelled`
- `superseded`
- `consumed`
- `invalidated`

Approved is not equivalent to consumed. A guarded operation must consume the exact approved request against the same target binding before treating approval as authorization evidence.

## Target binding

The reviewed target includes:

- operation
- object type and stable object ID
- optional target version
- optional target digest
- related canonical resource IDs

The target fingerprint is deterministic. Consumption with a different fingerprint fails closed and records invalidation when appropriate.

## Quorum and separation of duties

ApprovalRequirement defines:

- quorum
- required authentication assurance
- eligible membership roles
- optional team IDs
- optional canonical authority Role IDs
- distinct-human requirement
- self-approval policy

Eligibility is revalidated from live canonical identity/authority state before approval and consumption. Revoked/expired sessions, revoked memberships or no-longer-eligible approvers cannot silently preserve authority.

## Expiry and scheduling

Approval expiry is represented by a durable scheduler timer. Timer firing produces deterministic canonical events and transitions; it never invokes a model directly. Restart recovery and duplicate firing use the scheduler/event idempotency boundaries.

## Native compatibility

Legacy/native Codex approval prompts are projected into ApprovalRequest before a decision is sent back to the app-server. The native response is sent only after the canonical request reaches an allowed terminal/consumed state. Native transport state is compatibility state, not the approval source of truth.

Sensitive Definition publication uses the same canonical ApprovalRequest boundary rather than a separate publication-specific approval lifecycle.

## API and operator UI

The canonical API supports list/get/create/decision/cancel/supersede/consume operations under `/api/approval-requests`.

The product UI exposes ApprovalRequests from the main operator surface, including scope, requester, target version/digest, policy/authority reason, quorum, required assurance, decisions, expiry and result/invalidation state. Approve/reject actions call the canonical decision API and surface deterministic backend denial reasons.

The UI performs deterministic API refresh only and must not trigger model reasoning.

## Audit and evidence

Approval records retain requester/approver identity, session/assurance metadata, policy/authority source, decisions, timestamps, resulting operation reference and audit/evidence references. Secret or authentication material is never embedded in the record.
