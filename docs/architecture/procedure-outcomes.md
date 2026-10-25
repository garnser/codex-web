# Verified operational outcomes and reviewed procedure drafts

Outcome conversion extends the existing verified-execution Skill promotion and
Organizational Memory lifecycle. It adds no learning catalog, scheduler, worker,
job store or model call. Meaningful completed/recovered procedure advance
requests and explicitly linked Incident postmortems activate conversion. An
operator can also request it directly. An idle application consumes zero tokens.

The source is a canonical ExecutionAssignment's exact ProcedureCheckpoint:
Definition record/revision/checksum, immutable input/output Artifact pins,
Resource context checksum, step results, Evidence and authorized deviations.
Conversion rechecks proof provenance, successful effects and their declared
verification, source lifecycle/retention, scope and freshness. Recovery-only,
failed, uncertain or stale outcomes produce review needs, never proven fixes.
Resource facts are included only with an explicit checksum comparison against
the execution's pinned context; changes require review.

Extraction is deterministic and bounded to 24 KiB. It copies IDs, typed
preconditions, applicability, service-version limits and observed step proofs.
Structured diagnostic signatures are retained as SHA-256 values with exact
Evidence links; arbitrary signature text never becomes instructions.
It does not copy arbitrary logs, output values, conversations, postmortem prose
or free-form deviation reasons. Operators follow the exact source links for
authorized detail. Causation is explicitly an unconfirmed hypothesis; successful
restoration proves only the observed outcome. Untested recovery branches,
deviations and other versions require independent review. Existing procedure
review/expiry limits are retained, never extended automatically.

## Atomic derivation and conflicts

The canonical shared store's `update_many` transaction covers Definition,
Memory, Data Governance and Artifact/Evidence documents. Private service/store
copies operate on a view of these locked transaction documents, using the
existing engines and validators. No shared service is temporarily rebound.
Source proof/governance is rechecked within the transaction. A draft, its
governed Memory report and its exact `procedure_skill` dependency edge become
visible together, or all roll back. Retrieval indexing is marked dirty only
after commit and remains rebuildable derived infrastructure.

Assignment plus exact source Definition and, for checkpoint 1.1, immutable run
ID determines delivery identity. Legacy outcome identity stays unchanged. The
report records run ID and source validation resolves that retained checkpoint,
so a later current run cannot rewrite the old Knowledge or Skill provenance. Changing
an event ID cannot bypass deduplication. The existing Memory logical-key gate
and deterministic Definition record ID reserve the result. Retries return the
same revision and exactly one derived governance record. Content drift or a
concurrent local/new published revision raises a review conflict. There is no
last-writer replacement. A new target Skill may be explicitly requested; its
slot must be absent. The conversion does not publish, enable, assign or replace
the source execution pin.

Source erasure requests serialize with derivation through the same store. A
pending/blocked erasure or inactive source aborts the entire conversion. A later
erasure discovers the committed dependency through the existing governed
research cascade, prechecks dependent legal holds, erases Memory content and
quarantines/tombstones the derived Skill and all edited/published descendants.
Tombstones retain canonical record identity/lineage and a governance marker;
payload and checksum change as an explicit governance exception to immutable
content retention. Old checksums no longer resolve. Descendant authoring cannot
resurrect an erased ancestor. Source classification, retention, residency and
model-context denial inherit through canonical governance, never caller claims.

## Review and publication

The inactive revision contains a reviewable instructions diff and the original
validated operational contract. Tested behavior does not automatically rewrite
steps, loosen applicability or assert a cause. Owners edit the draft explicitly
after reviewing deviations and evidence. Existing Skill schema/reference
validation, revision-bound scans and security policy still apply. Generic
Definition publication/rollback and Skill assignment recheck source freshness
and governance; provenance removal in an edited descendant cannot shed its
ancestry gates. Newer publication/local revisions require conflict review.
Publication remains distinct from assignment/enabling future use.

The operator API exposes `GET /api/execution-workers/assignments/{id}/procedure/outcome`
and `POST .../procedure/outcome-draft` with optional `incident_id` and `skill_id`.
The response carries the report, canonical draft/Memory references, diff,
publication requirement and unchanged assignment state. Tenant/source mismatch
fails visibly; conflicting mutation returns 409. Existing Skill mutation
assurance/authority and Memory write authority remain required. Automatic
delivery uses its authenticated caller, never manufactured MFA or a worker's
operational grant. Insufficient authority or review conflicts become canonical
Attention needs; explicit operator conversion can resolve them.

Incident postmortem requests may supply at most sixteen exact
`procedure_assignment_ids`. The Incident must be resolved and share canonical
restoration Evidence, Project and Resource scope with each execution. The report
retains Incident, postmortem and exact postmortem Memory version/digest links.
A later postmortem does not silently rewrite an earlier execution-derived draft;
changed reports or Memory revisions produce a review conflict. Logical-key matches
are accepted only when the original deterministic content and exact source
attribution still match; manually preseeded claims cannot become observations.

This is an additive API/composition change; existing Definition, Memory and
procedure payload versions are unchanged. No data migration or automatic
publication occurs. An older engine cannot provide these source gates and must
not be used to publish/reuse outcome-derived records. Drain before downgrade and
restore a compatible verified snapshot if rollback is required.

Assignment, ActionIntent and Incident inspectors expose outcome extraction,
evidence/diff review and explicit inactive-draft conversion. Exact draft links open
the existing Skill/Definition validation, scanning and publication controls;
Memory, Evidence and Attention remain canonical source inspection surfaces.
Independent editor groups acknowledge only values submitted by that operation;
inspection calls never acknowledge edits. See the [operator guide](../operations/procedure-outcomes.md).
