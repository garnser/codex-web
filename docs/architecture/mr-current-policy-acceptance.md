# Current-policy MR acceptance owner

`CurrentPolicyMRAcceptanceOwner.acceptance(scope, artifact_id, verification_id,
actor=...)` is the bounded production composition seam for the existing closure
gate. Application composition supplies the actual execution Role Definition owner
and native assignment-review adapter, which already owns the canonical Identity,
Artifact/Evidence, worker, positive START, Work and publication provenance checks.
The wrapper makes no provider call, approval, persistence, model invocation or
identity claim. It returns only the already validated manifest.

Only the current published catalog's explicit schema1.1 `assignment_role` policy
selects assignment review. The exact current reference is checked again after the
canonical receipt/content observation. Historical policy receipts cannot silently
become acceptance under another revision. `Verification.independent` stays false
when principals are shared. A stricter server-owned principal-independence
requirement is passed through and still refuses that receipt. No stored semantic
Role policy grants authority or overrides a stricter Evidence, release or autonomy
requirement.

An absent policy or principal basis fails with the typed
`mr_acceptance_principal_eligibility_owner_unavailable` dependency. It never falls
back to assignment review. The existing strict `check_independent_acceptance`
predicate remains unchanged and is not operationally enabled by this wrapper.
Execution Role contracts map semantic Roles, not authenticated principals;
AgentProfile revisions explicitly exclude worker/provider identities; active
Identity membership alone does not establish reviewer eligibility. The strict
Verification record has no fixed retained reviewer START/Profile/handoff receipt
from which that eligibility could safely be recovered. Its missing production
prerequisite is a canonical owner supplying eligible reviewer principals and
actual implementation principal provenance from authenticated, scoped records.
Neither manifests, caller arrays, display names, Artifact producer identity alone,
nor a constant callback may substitute for that owner. No new binding or policy
catalog is introduced here.

The application must inject this callable explicitly after the existing owners
are constructed. Current policy adoption remains a separate normal governed
Definition publication; schema1.0/default principal workflows are not implicitly
converted. Tests use the real private canonical native-review and Definition
owners, with synthetic historical publication/provider boundaries, and do not
claim live native QA or production policy publication.
