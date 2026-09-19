# Tamper-evident autonomy audit and reliability

## Status

**Controlled-production-autonomy audit contract.** This boundary turns
autonomy-cycle attribution into append-only canonical accountability records and
derives reliability/efficiency qualification from those records. Runtime logs,
traces and in-process metrics remain telemetry; they are not the audit source of
truth.

## Audit boundary

Every completed autonomy-cycle decision path, including deterministic, skipped,
recommended, prepared, blocked, failed and completed outcomes, is appended to a
tenant/workspace audit partition after the canonical cycle record is persisted.

Audit payloads intentionally contain identifiers and bounded metadata rather than
model prompts, provider payloads, credentials, secrets or raw exception text.
The record can retain canonical event/correlation/causation IDs; acting identity
kind and authentication assurance; effective autonomy level and policy
fingerprint; ApprovalRequest and break-glass references; Goal, Decision,
Resource and ActionIntent references; model/provider/revision/prompt-template
and routing identifiers; provider receipts, Evidence and Verification
references; token/cost and configured impact budgets; and outcome reason codes.

Sensitive exception or governed-redaction reason text is represented by SHA-256
proof metadata rather than copied into immutable history.

## Hash chain and partitioning

Audit records are partitioned by a deterministic hash of organization/workspace.
Each partition begins at a fixed genesis hash. A record hashes the versioned
algorithm, partition ID, monotonically increasing sequence, previous record hash,
and SHA-256 of canonical JSON payload.

The transactional AutonomyAuditStore append operation reads the current
partition tail and appends the next record under the same SQLite writer
transaction. Concurrent writers cannot independently claim the same next
sequence.

Verification recomputes payload hashes, record hashes, previous-hash links and
sequences. Missing, reordered or rewritten records fail integrity verification
deterministically.

## Checkpoints and external roots

A checkpoint anchors an exact partition sequence and record root. Checkpoints
form their own append-only hash chain. The service supports pluggable
AuditCheckpointSigner and AuditCheckpointExporter contracts.

A signer supplies signature algorithm, key reference and signature while key
material remains behind the signing implementation. This avoids inventing
signing semantics inside the existing encryption-key API.

Exporters receive an immutable checkpoint. The built-in filesystem exporter is
create-only and fsyncs the checkpoint; deployments requiring WORM or object-lock
guarantees should use an appropriately protected mount or an object-store
exporter. External roots are integrity anchors, not canonical business state,
and cannot authorize actions.

## Periodic verification

Configuring reliability policy for a tenant/workspace ensures one recurring
canonical scheduler entry for audit verification. The scheduler emits the normal
canonical schedule.due event; the audit service subscribes to that trigger.
There is no private timer loop.

A successful scheduled verification creates a checkpoint. A failed verification
creates an active audit-integrity safety signal. Operators can also run
verification explicitly and publish the result as canonical policy-evaluation
Evidence for production-readiness and recovery qualification.

## Reliability and efficiency

Reliability metrics are derived from canonical audit-cycle rows, not dashboard
state. The deterministic summary includes sample/completed/failed/blocked
counts, canonical human-approval intervention rate, consecutive failures,
rollback/recovery/incident attribution where represented by canonical metadata,
total model tokens/cost, tokens and cost per successful completed outcome, and
current audit-integrity status.

Policy can define minimum sample size, maximum failure/consecutive-failure and
human-intervention rates, and maximum tokens/cost per successful outcome.

Safety signals provide a provider-neutral bridge for canonical evaluation
regression, SLO/error-budget exhaustion, critical incidents, audit-integrity
failure, release readiness and recovery readiness. They reference Evidence IDs
rather than copying evidence payloads.

When auto_suspend is configured, failed thresholds or configured active safety
signals deterministically set the existing autonomy control to paused and append
an auto_suspension audit record. This reduces autonomy only; it cannot grant
authority or perform provider actions.

Reliability summaries can be published as canonical policy-evaluation Evidence.
The effective autonomy policy includes reliability and audit_integrity as
production-qualification gate types, so broader production autonomy can require
fresh PASS Evidence instead of operator intuition.

## Governed redaction

Historical audit records are not rewritten for redaction. A governed redaction
appends a new audit row referencing the target and contains only a digest of the
redaction reason. Read surfaces minimize the target identity/details while
leaving original hashes and the redaction proof visible.

This preserves evidence that history existed and was later governed without
silently mutating the chain, while avoiding use of the audit service as a store
for prohibited personal or sensitive content.

## API surface

The authenticated surface under /api/autonomy/audit supports record inspection,
integrity verification, checkpoint inspection/creation, reliability metrics and
Evidence publication, reliability-policy configuration, safety-signal
inspection/mutation, and integrity-preserving redaction. Human mutations require
administrator authority and MFA. Service principals require explicit autonomy
audit/admin scope.

## UI and related production domains

Issue #121 owns the Autonomy Control Center and explain-action UI over this
canonical audit. It should trace event -> policy/authority -> approval ->
ActionIntent -> receipt/evidence/verification -> outcome and show integrity,
reliability, error-budget and safety-signal state without reimplementing audit
logic in the browser.

Release (#161), Incident (#162), recovery (#163), capacity (#165), upgrade
(#168) and replicated ownership (#142) domains should publish canonical Evidence
and/or safety signals into this boundary rather than introducing local
production-autonomy readiness flags.
