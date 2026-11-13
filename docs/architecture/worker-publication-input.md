# Governed initial-publication input collection

The fixed `workspace-initial-publication-input.v1` method on the existing
assignment-bound `POST /api/verifications` creates an input Artifact and INFO
Evidence. It does not create a QA Verification, publish a branch, invoke a model,
or grant an operation. The current pinned execution Profile must already declare
`verification.create` and the required repository reads. Both current principals
need their own authority; credentials remain inside the existing CodeHost reader.
For Resource-context admission, the original context requester remains the
requester for every repository read; an allocation custodian cannot lend its
permissions. The canonical Work source API base must match the selected GitLab
binding's API base using the TaskSource owner's existing trailing-slash
normalization, not a hostname/display-label guess.

The strict request binds the exact published procedure, repository, expected
assignment/fence, expected local HEAD and original request ID. These are assertions
against authenticated custody, never selectors for another worker. Work, producer,
workspace, branch and remote revision cannot be supplied as caller facts. Current
Work comes from the canonical Work workspace or its exact live publication-recovery
binding; a bootstrap assignment's immutable subject and absent Work attribution
are not rewritten. Only a current owned same-repository open GitLab MR is supported.

The canonical worker inspects actual clean Git metadata in its isolated member.
The existing CodeHost facade reads the exact repository, MR and source branch;
forks, protected/default source refs, changed branch/head and conflicting repository
identity refuse collection. Canonical publication-recovery scope is required when
the bootstrap workspace has no Work subject. Exact typed published inputs choose the supported
five-field legacy shape or six-field shape including the same existing MR number.
A five-field contract cannot express a different recovery publication branch.
No mutable Skill catalog or new procedure `input_protocol` is introduced.

Canonical Artifact, managed content, INFO Evidence and governance publish in the
existing shared transaction after current custody, Work, Definition, provider and
authority guards. Metadata pins the original request, exact Definition, actual
worker/assignment/fence and provider observation. This is an observation, never
permission to use stale facts: the original publication owner still performs its
normal current-authority, workspace, provider, expected-remote CAS and verification
checks at effect time, including ancestry and recovery branch mapping. The
collector never invokes the publisher's host-Git attestor; all of its Git reads
stay in the actual worker's isolated inspection backend. Input collection does not settle another pending or UNKNOWN
publication or replace its key.

Exact repeated requests return the original governed IDs before another Git or
provider read. Changed request content under the original identity refuses.
Response loss uses `GET /api/verifications?method=workspace-initial-publication-input.v1&source=<original-source>`;
absence is UNKNOWN, not permission to issue a new key or move the request to a new
assignment. The original source is computable even when the POST response was
lost: SHA256 of UTF-8 JSON with keys `assignment_id`, `fence`, `method`, `request_id`,
sorted keys, compact comma/colon separators and no NaN, prefixed `sha256:`. The
values are the original request's expected assignment/fence, fixed method and
request ID; the server independently checks that asserted origin is actual.
Existing broker operation inspectors expose the actual fixed-method
schema; Artifact/Evidence inspectors expose canonical scope, producer and governance.
No new first-class store or separate UI truth is introduced.

This additive fixed-method protocol changes no persisted state schema, Definition
map or existing procedure input contract. Older servers refuse the unsupported
method; existing Artifact/Evidence readers retain ordinary governance and content
integrity checks. The declared operation and current published procedure remain
necessary; installing this method does not publish guidance or add grants.
