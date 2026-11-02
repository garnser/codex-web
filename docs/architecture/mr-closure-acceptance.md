# Exact MR acceptance manifests

An existing MR closure procedure must distinguish independent acceptance of a
specific change from a generic passing test or provider status. The
`code-host-mr-independent-acceptance.v1` verification method describes a bounded
review manifest in an existing governed managed Artifact. It creates no new
record store, reviewer identity, approval or merge permission.

The manifest contains the organization, workspace, Project, Work reference,
repository Resource, provider binding, MR number, exact head and current target
base, schema version `1.0`, and an explicit accepted/rejected/needs_input result.
The implementation producer supplies the manifest through the existing Artifact
owner. A genuinely independent eligible reviewer records the existing canonical
Verification against that Artifact with result `verified`, the method above,
and the finding `mr_review_manifest_sha256:<sha256 of exact managed bytes>`.
The immutable Verification therefore binds the accepted content even if managed
content on the Artifact is subsequently replaced. A review of a rejection still
does not accept the MR.

`check_independent_acceptance` checks those semantics on freshly loaded canonical
records. Its current implementation identities and eligible reviewer identities
must be resolved by trusted composition from canonical Work/Role/Identity and
original publication provenance. They are never procedure inputs, manifest
claims, Profile names or a service default. No eligible reviewer, expired or
invalidated records, a different binding, head or base, or generic PASS Evidence
must stop closure. Referenced managed content is limited to 16 KiB and must match
the current Artifact digest and the immutable review finding.

This predicate is one gate. Integration must load content through the existing
Artifact/Evidence owner with content verification, re-read canonical records
after content I/O, and retain actual current authority and custody checks. It
must additionally establish current exact-head CI and complete provider policy
through their canonical owners. The registered merge ActionIntent must preserve
its original identity, use a provider head comparison, and be followed by the
separate original-merge verification. This manifest does not authorize READY,
retry an uncertain merge, grant host access, or close correlated issues through
duplicate writes.

The method label alone does not qualify a reviewer or prove acceptance. The
existing Verification writer establishes the actual actor and producer
independence; closure additionally checks current eligible identity membership,
exact content and scope. No model call is required to check this protocol. A
missing or rejected independent review remains a named blocker until an actual
review event changes it.

The procedure adapter and application composition are required before this
predicate can admit live closure. The unpublished generalized readiness draft
uses existing read-only provider actions independently of this acceptance
protocol; successful observation may report pending or blocked requirements.
Neither a draft nor a passing predicate test establishes live qualification.
