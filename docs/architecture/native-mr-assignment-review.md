# Native MR assignment and Role review

`code-host-mr-assignment-review.v1` is a fixed assignment-bound Verification
method. It records actual review judgment for one exact repository/MR/head/base
and provider binding. It does not merge, approve a provider review, grant authority,
change Work ownership, assert test coverage or manufacture reviewer independence.

One authenticated execution-worker service may host both implementation and QA
Roles. `ArtifactEvidenceService` continues deriving `Verification.independent`
from producer/verifier principal identity. In particular, the new method's
worker-produced review Evidence verified by that same worker remains **false**.
Every existing `EvidenceRequirement.independent_verification`, autonomy/release
principal-independence policy and `check_independent_acceptance` remains unchanged.
No assignment review may satisfy one of those policies by fallback or relabeling.

## Explicit policy, not an authority grant

The existing `execution-role-catalog` Definition owner adds schema `1.1` with an
optional `mr_review_policy` semantic qualification contract. Schema `1.0` rejects
that new field; unchanged 1.0 payloads round-trip without a normalization change.
An absent policy or `basis: principal` does not enable this method. An explicit
`assignment_role` policy must name nonempty, disjoint, currently active existing
implementation and validation Role IDs. It grants no capability, credential or
repository access. Current broker, Work ownership, worker, Profile/Definition,
procedure, resource and native-custody checks still apply independently.

Coordination owner bindings accept the same catalog kinds/IDs in schemas 1.0 and
1.1 while retaining exact immutable historical references and alias restrictions.
No bootstrap automatically publishes schema 1.1, supersedes a catalog, repins a
Profile or alters a historical execution. The inactive policy example describes
only a future change. An operator must clone the complete actual current payload,
retain its scope/mappings/other Roles, select real eligible Role IDs, and use normal
Definition draft/validation/assessment/publication with actual current revision
CAS. Cross-schema drafts cannot claim a same-schema derived payload. Superseding
or revoking the policy invalidates current qualification; historical receipts
retain their exact original Definition reference. Authority approvals, if required
by the actual publication assessment, are separate normal governed operations.

The same schema 1.1 may declare `mr_ci_readiness_policy` with version 1.0 and
an explicit `execution_target_kind` of `canonical-native-assignment` or
`external-ci-job`. Absent means no CI execution-target requirement is declared;
it must not silently select native execution. Schema 1.0 rejects the new field.
The existing Role owner returns this typed current requirement and exact catalog
reference through `resolve_mr_ci_readiness_policy`; consumers fence that canonical
Definition and validate actual target proof. It grants no worker/host access and
does not override stricter CI, authority, evidence or release policy.

## Canonical receipt and original source proof

The caller selects a managed bounded review-manifest Artifact and exact original
publication intent, implementation assignment and implementation/QA preflight IDs.
They cannot supply a principal, Role, Profile, independence flag or receipt body.
The review Artifact must itself be created by the authenticated QA worker for
that exact validation execution and canonical workspace, with canonical creation
and produced timestamps after its positive QA START. Sharing a principal is not
permission to borrow an implementation/old/root-produced acceptance manifest.
The owner authenticates the actual worker and exact current QA assignment; verifies
current eligible Profile/Role, Work owner, workspace and repository; reads and
checks managed manifest bytes; and uses the existing current MR facts owner to
confirm the exact head/base/MR/provider binding.

Original implementation attribution requires a retained actual assignment/Profile
and positively correlated canonical native START, plus the exact successful
publication intent/receipt/provider verification for this head and resource. A
current mutable AgentSession or caller-supplied checkpoint provenance alone is
insufficient. Historical assignment retirement does not invalidate original START
proof or require renewing its old lease. Missing original proof returns typed
inconclusive with no acceptance record; it never becomes NOT_SENT or independent
QA. Exact canonical accepted handoff recipient/nonce and actual QA START are
required. A legitimate Orchestrator sender need not be the implementation Role.
The retained implementation START must match this exact Work. The retained QA
START must match both this Work and the handoff nonce and cannot predate that
handoff. Routed handoffs also require the original recipient assignment/fence,
thread/turn and verified delivery tuple; manual ACK does not manufacture one.
An ACK never fabricates routed delivery or replaces a missing START.

The same existing canonical transaction fences current custody/Profile/Work,
policy Definition, provider binding, original intent histories and exact preflights
while appending governed review Evidence and Verification. The versioned receipt
binds exact provenance, publication references, manifest SHA and policy reference.
Response loss uses exact method/source lookup; it must not produce a replacement
review or repeat provider effects. The closure owner must reload and revalidate
canonical referenced records through `validate_canonical_receipt`; the pure
predicate alone cannot authenticate arbitrary caller-selected metadata.

## Integration and operator visibility

Application composition must explicitly inject the existing Role Definition and
preflight owners into this adapter. No global resolver or alternate policy store
is installed. The fixed method's existing broker Verification create/read slots
must be composed alongside other fixed methods, retaining their restrictions.
The existing Definition JSON editor/schema metadata exposes the new optional
policy; existing Verification/Evidence inspection exposes method, source, actual
principal, `independent: false`, exact provenance and typed refusal. No new UI-only
truth or generic administrator endpoint is introduced. Procedure closure consumes
this explicit semantic basis only where its current policy permits it.

Qualification uses private canonical SQLite/Identity/Profile/Work/START capture,
managed content and controlled historical provider fixtures. Those tests do not
claim a new real provider publication, native model review or live Definition
publication. Full application composition, current policy opt-in and actual native
workflow qualification remain separate integration work under the same issue.
