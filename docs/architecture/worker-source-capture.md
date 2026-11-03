# Governed source capture from an admitted checkout

`repository.source.capture` is an assignment-bound broker operation, not a
general HTTP filesystem service. Its fixed `POST /api/repository-source-captures`
route requires one `repository_resource_id` query parameter and the versioned
`repository-source-capture.v1` request advertised by the broker descriptor.
The request identifies the original request ID, exact assignment fence, current
Work, repository, immutable commit, and each relative path, Git blob ID and
SHA-256. It contains no producer, authority override, host path, command,
credentials or provider URL.

The existing broker and worker owners revalidate the authenticated service
worker, requester, current Profile and Definition, Project/tenant, live lease,
fence, current Work ownership and admitted repository scope. The existing
workspace member and its READ or WRITE lease must agree exactly. Administrative
scratch scope cannot manufacture an admitted checkout. A custom execution
contract must explicitly declare this operation; capture adds no grant or
Profile binding.

The local worker uses its existing sandbox backend with a fixed program,
read-only mounts, disabled network, cleared Git configuration and no native
runtime HOME, credentials, broker socket or extra runtime mounts. Git plumbing
reads immutable objects without hooks, checkout filters, replace refs or lazy
fetch. The commit must be the checkout's HEAD. Each selected path must be one
tracked regular file, and the actual file opened through directory descriptors
with `NOFOLLOW` must equal the verified immutable blob. Traversal, symbolic
links, dirty substitution, changed HEAD/blob/digest, unsupported isolation and
truncation refuse capture. Limits are eight files and 65,536 raw bytes; the
encoded governed JSON bundle remains below the existing 262,144-byte consumer
record bound. No repository code is executed.

The existing ArtifactContent and Artifact/Evidence owners persist the bounded
bundle and its provenance under their canonical transaction with current
identity, Profile, Work, worker, assignment, workspace, lease and governance
guards. Artifact producer and Evidence producer are the actual worker. Both
retain the exact Project, Work, execution, workspace and repository plus
assignment ID/fence, worker, requester, request digest and immutable commit.
The immutable Evidence digest also binds the exact Artifact content digest.
The Evidence is `artifact_verification` with result `info`; it establishes a
source observation, not CI success, QA, independence, acceptance or merge
permission. Canonical governance assigns retention and controls later use.

The original request ID is scoped to the assignment incarnation. Exact repeats
return the same governed Artifact/Evidence IDs without another source read;
changed content under that identity is refused. Artifact, content pointer,
Evidence and governance metadata publish together. A failed content/governance
append may leave an unreferenced immutable content object, never accepted
source proof. Lost responses require inspection/settlement of the same original
identity, not a new ID or claims of rollback.

Capture does not authorize a different Resource owner to read the result.
Existing governed derivative sharing remains separately authorized; its
administrator/MFA boundary is unchanged. Cross-resource autonomous sharing
requires its own canonical policy and owner. Existing Artifact/content/Evidence
inspectors show the resulting records and governance. The existing broker
operation inspector exposes the typed request, bounds and refusal context; no
new UI action or source store is introduced (#2006).
