# Inspecting procedure admission

A procedure is published Skill guidance; canonical policy and authority still
decide which actions are permitted. Discovery runs without a model. The same
selection is checked again immediately before an ActionIntent provider mutation.

Inspect the ActionIntent's side-effect timeline for the actor, tenant/Project,
Resource targets, operation, exact selected record/revision/checksum, consulted
source references, runtime/worker capability evidence and candidate explanations. Earlier admissions stay visible.
The assignment API also retains its fenced discovery history. Instruction bodies
and secret values are not copied into the admission evidence.

`matched` identifies consulted, pinned instructions. `no_match` means a successful
catalog lookup found no applicable procedure; existing policy still decides
whether the requested action may continue. `failed`, `inaccessible`, `ambiguous`
and `unsupported_enforcement` are blockers, not alternative spellings of no-match.
Use the authenticated `/api/skills/procedure-applicability` preview with an existing
provider binding and canonical action request to inspect applicability without
executing the operation. A preview grants no permission.

Set environment, tags and service versions through the existing Resource API's
`operational_context` field when those facts are required. Omitted facts are
unknown. Review stale/quarantined/incompatible Skills through existing Definition
and Skill review/security controls. Resolve equal-precedence conflicts through a
new reviewed Definition revision. Do not weaken policy or copy instructions into
another catalog to clear a blocker.

A scope/operation change causes new discovery. A new published procedure revision
within the same admitted context requires a fresh explicitly admitted intent;
retain the old trace and its exact revisions. Record any deviation rationale on
the existing ActionIntent. Rationale grants no authority and cannot clear a gate.
A matching procedure alone does not require another user confirmation; approval
is requested only by existing canonical policy/approval controls.

Native providers currently cannot intercept every operational tool mutation.
A successful no-match preserves policy-authorized local repository or scratch
preparation, including ordinary repository-writing Thread/WorkItem/delegated/resumed
turns. Admission verifies the exact effective profile pin, live assignment fence,
registered runtime/worker evidence, isolated workspace lease, target scope and
disabled network. Read-only exploration uses that same canonical envelope.
A matching required procedure on native tools, an unknown/changed envelope or a
broader operational/host scope returns typed `procedure_unsupported_enforcement`. Arbitrary
native tool mutations, trusted-local host execution and unsupported procedure
rollback paths must not be described as covered. Use a supported canonical
ActionIntent operation for protected mutations. This source change does not
supply a native operational tool runner.

In **Skills**, open a revision and choose **Edit as new draft**. Enable
**Structured operational playbook** to author metadata, selectors, freshness,
capability requirements and ordered steps using the canonical procedure schema.
Adding/removing/reordering steps changes only the draft form. The server validates
references, recovery ordering, bounds and compatibility when saving. Schema loading
and failures preserve entered edits; retry schema loading before saving. Unsaved
edits use the shared navigation guard and never enter browser storage.

Review the effective Definition scope, owner, source, exact record/checksum,
revision history and canonical diff. Validate the exact revision and run its
security scan. Publication explicitly supersedes the eligible prior revision;
existing runs retain their pins. Rollback creates a new revision through current
gates. If a scan or publication gate refuses rollback after creating an inactive
revision, follow the retained revision link and review it instead of repeating
the rollback. Generic **Definitions** inspection provides the same lifecycle and
independent publication-approval controls.

**Playbook applicability preview** accepts a task trigger, registered provider
binding and operation, Resource IDs, optional expected environment and execution
ID. The task is a preview filter, not permission. Environment is always read
from canonical Resource facts; disagreement produces a conflict, never a simulated
grant. **Resources → Edit canonical resource** owns environment, operational tags
and service versions. Review matched/rejected/conflicting candidates, the exact
selected revision, policy reasons and capability sources. Missing facts/capabilities
remain unknown and blocked. Instructions cannot supply current facts or authority.

**Workers / Execution** exposes discovery and durable checkpoints on each
assignment. **Advance one step / resume** invokes the canonical interpreter once;
it can wait for an exact approval, lease, persisted retry time or reconciliation.
Inspect **Attention** and exact **ActionIntent** links for blockers. A lost response
is an unknown outcome, not a failed change: retain the originating intent and
receipt, reconcile before retry, and verify declared recovery before claiming
restoration. **ActionIntent** timelines and **Incident** traces expose linked
canonical runs. Diagnostic deviations require evidence and current policy; they
cannot bypass mandatory verification. Start, deviation and outcome-draft inputs
have independent unsaved-edit ownership so one operation cannot acknowledge
another form's unsubmitted edits. Inspection never marks edits saved.

Capability requirements use the assigned worker's registered capabilities and the
exact pinned runtime registration intersected with its actual adapter capabilities
and current canonical provider grants. Assignment minima are not a capability
attestation. Missing facts remain unknown; revocation or revision skew blocks a
required procedure. These reads do not grant capabilities or enable network.
The read-only preview accepts optional `execution_id` for scoped assignment
capability inspection; without attribution, capability-bearing candidates remain
unresolved.

Direct provider-neutral AgentSession starts/resumes/objectives lack attested worker
transport and remain unsupported, including Goal continuation through that path.
Use the canonical assignment-bound Thread/WorkItem turn boundary for supported
isolated preparation. Operational step execution remains the separate
[#1431](https://github.com/garnser/codex-web/issues/1431) contract; this admission
slice supplies no new provider/tool enforcement capability.
