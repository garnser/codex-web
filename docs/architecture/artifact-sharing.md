# Governed cross-Work source delivery

Issue #2008 uses the existing Artifact, Evidence, DataGovernance, managed content,
Work, identity, execution custody, authority and Definition owners. It adds no
sharing ledger, ACL store, repository lease or execution permission. No model
call is required to evaluate a sharing request.

The broker-only operation `verification.artifact.share` accepts
`POST /api/artifacts/shares` with the closed `governed-artifact-sharing.v1`
request: original request ID, expected current fence, exact source Evidence ID
and exact target Work reference. It accepts neither content nor actor, resource,
classification, approval, authority, result or Profile overrides. The one source
repository query selector is checked against the capture, not used as a target
execution override. Application
composition registers its operation/resolver/adapter and canonical policy owner.
Actual grants/policy and live qualification are still required before operational use. The existing operator `/api/artifacts/derived`
route and its ADMIN/MFA requirement are unchanged.

## Explicit policy and authority

The canonical Registry kind `artifact-sharing-policy`, schema `1.0`, stable ID
`artifact.sharing.default` is strictly Project-scoped. Its payload identifies the
organization, workspace and Project and bounded rules naming exact source and
target Resource IDs and a content byte ceiling. The only supported source
contract is `repository-source-capture.v1`; each limit is at most the existing
consumer's 256 KiB bound. There are no default rules or seeded permissions.
New/broader rules require the existing Registry independent publication approval
bound to the candidate checksum and exact active record/revision. The existing
approver must differ from the publisher; approval availability and publication
remain owned by that Registry, not a new sharing approval endpoint.
removal/narrowing remains the same canonical publication path. Policy is managed
and inspected in the existing Definition Registry surface, not a new permission
store.

The current authenticated worker and original requester each need canonical
`api.artifacts.share` at EXECUTE on the entire source/target Resource union in
this Project. The atomic Role evaluator cannot combine incomplete grants. A
policy match is additional to those decisions and grants no repository access.
The source assignment's admitted repository set is unchanged; the receiver does
not receive source repository execution access. Missing policy, unavailable
owners, unfulfilled approval requirements, current authority denial and stale
creation references refuse visibly. Actual application registration, Project policy
publication and grants must be explicitly configured through their existing
canonical owners before live qualification; Profile instructions are not grants.

## Source and target truth

The source must be the current owning worker's own canonical #2006 capture,
with the exact source Work, assignment, execution, workspace, worker, Profile,
fence and lease. Artifact and Evidence deterministic IDs must identify the same
capture request, and the immutable INFO Evidence digest must equal the managed
Artifact bytes' digest. This excludes arbitrary public Artifact metadata that
merely claims to be a source capture. Only the capture's report plus single
`artifact_verification` INFO Evidence pair is supported. This slice does not
forward arbitrary other workers' records or privileged host-output releases.

The target Work is resolved canonically in the same tenant/Project; its current
Resource set, not a caller selector, must fit a published directional rule.
Current source ownership and both Work original indexes are checked; an
unresolved native original or an unavailable/incomplete index refuses. Pending
handoffs, START/ACK and their existing histories are not changed by delivery.

The owner reads bounded managed content, verifies its digest and closed source
contract, then revalidates current custody, identities, Profile, complete Work
scope, source provenance, authority and exact policy references after I/O and
under the existing canonical writer. Revocation, expiration, lifecycle changes,
context denial or changed bytes are never interpreted as an eligible source.

## Canonical derivation and partial outcomes

The existing governed writer creates the target-scoped derived Artifact,
immutable managed content and INFO Evidence in one shared SQLite transaction.
The derivative references both source governance records, inheriting maximum
classification, earliest retention, residency and context denial. No policy can
weaken those hard constraints. The narrow shared-provider branch in the existing worker reader reloads the
source Artifact/INFO receipt, current parent governance/retention, original
producer identity/custody, original principals' current sharing authority and
current directional sharing policy before returning metadata/content and after
content I/O. Creation references remain immutable provenance. Compatible current
policy revisions keep existing inputs readable; removed directions, tighter byte
bounds and revoked sources deny them. Normal producer completion or fenced idle
bootstrap cancellation does not revoke an otherwise valid durable input. Legitimate source-assignment retirement does not itself erase
immutable captured input; it never becomes new execution authority.

The assignment/fence/request ID owns stable Artifact/Evidence IDs. An identical
request returns its original pair only while all current guards remain valid.
Changed requests, source digests or policy references cannot overwrite or replace
an original delivery. A refusal before commit leaves no accepted partial rows;
content-addressed storage may retain unreferenced immutable bytes. After response
loss the caller must preserve the original request identity and inspect/settle
that exact request, not invent another derivative. An incomplete or changed
original pair refuses for explicit reconciliation, never fake success.

The response and existing Artifact/Evidence inspectors expose source/derived IDs,
source and target Work, producer/custody, content digest, exact sharing and
operational authority Definition references, selected rule and INFO-only result.
The receiver uses the existing assignment-bound exact-ID content read with its
own current target Work/resource scope. Data delivery is neither independent QA,
validation, handoff acceptance, merge permission nor procedure completion.

## Qualification boundary

Private tests use actual SQLite/Registry/authority/governance/content and Work
owners. Native/workspace transport edges are controlled and source capture
provenance is constructed through its canonical writer contract; that is not
live capture qualification. A receiving worker fixture exercises the existing
exact-ID content reader on its own target scope. Full operational qualification
still requires the registered current runtime, explicit canonical policy/grants,
real source-owner capture and sharing, and an actual receiving-owner content read.
No Root byte upload or operator-generated derivative counts as autonomous delivery.


## Registered transport and execution scope

`POST /api/artifacts/shares?repository_resource_id=<exact admitted source>` uses
one explicit source selector, which must equal the capture Artifact's sole
repository. No other query overrides are accepted. Preliminary authority checks
use only this source, not unrelated admitted READ context; final decisions require
the complete source/target data Resource union. The target is canonical Work
scope, never an execution admission. The exact INFO-sharing operation may run
under admitted procedure selection; it neither creates a Verification nor marks
the selected procedure complete. Other Verification operations retain their
existing unmediated/writable scope policy.

Application composition instantiates the policy service after Resources and
Registry, and injects that same owner into the broker. Registration adds only the
Definition kind/schema entry; no existing document format, SQL migration, Profile
permission or published policy is changed. Current-schema runtime metadata will
therefore include the additional `artifact-sharing-policy: 1.0` entry. Existing
Artifact/Evidence rows show bounded provenance with canonical source links and
INFO limits; metadata does not enable an operator mutation or confer authority.

### Receiving Work discovery

A current owning WRITE worker may call the native broker's
`GET /api/artifacts/shared-inputs?work_item_ref=<exact Work>&limit=8`.
This reads target-Work shared-input IDs, digest and size, not source content or
repository access. Both the authenticated worker and original requester need
current `api.artifacts.read` and `api.evidence.read` on the target Resources.
The normal current Profile, native custody, Work ownership and canonical
workspace/recovery checks apply. This does not expand READ execution Profiles.
Each returned report/INFO pair passes the same current directional policy,
parent governance/lifecycle and visibility checks as exact-ID consumption.
A receiver can then use existing exact Artifact/Evidence metadata and bounded
Artifact content reads. INFO is neither independent QA nor acceptance.

The canonical backend filters tenant, Project, exact Work, sharing provider and
source before limiting to 32 candidates, newest recorded first. Default result
limit is eight, maximum sixteen; metadata is capped at 32 KiB. SQLite queries the
existing canonical JSON, while PostgreSQL uses its existing current v2 array
projection and refuses unavailable/stale projections. Neither path loads a
whole tenant history into Python. These are returned-data bounds, not database
CPU or scanned-row guarantees. No index, schema or persisted format is added.

Invisible/revoked candidates do not expose IDs. `complete=false` and `truncated`
mean the caller has not established absence and must retain the provided
same-scope `next_cursor` if it needs another bounded page. Cursors describe an
observation position, confer no permission and revalidate current authority and
policy on every request; they do not promise a snapshot across requests or
include later insertions. New material Work inputs warrant a fresh first read,
not unchanged polling. Concurrent changes during a read refuse rather than
return stale authorization. Duplicated selected identities, malformed scoped
metadata and incompatible schemas refuse; no unbounded fallback is used.
