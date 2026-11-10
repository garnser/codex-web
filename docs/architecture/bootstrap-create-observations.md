# Assignment-bound bootstrap creation observations

The Codex app-server bootstrap uses the existing assignment-session transport
capture boundary for `thread/start`. Before its actual guarded write, the worker
owner stores one `bootstrap_create_observation` in the canonical
ExecutionAssignment. The immutable correlation binds the original assignment,
execution, workspace, worker/fence, provider/runtime, process generation and RPC
request ID, plus a SHA-256 of its immutable admission (including tenant, Project,
Profile/Definition, credential custody and resource scope). Raw requests,
credentials, instructions and provider response bodies are not retained.

The trusted transport reader captures an authoritative returned native session ID
before settling the ordinary response future, including after the waiting caller
is cancelled. A native JSON-RPC error retains only its numeric code as
`error_response`; it never proves no creation or no effects. Lost, malformed,
expired-generation or unrecordable responses remain Unknown. The ordinary
post-create guard, bootstrap binding and AgentSession adoption remain mandatory.
An observation never binds a current thread, creates a turn START/ACK, changes
Work ownership, authorizes recovery, or sends another native request.

This reuses the existing transport capture table and its fixed capacity. A create
slot lasts for the original process generation (rather than expiring while a
disabled-duration create is still in flight). Captured responses remove it and
transport retirement clears it. This does not extend process lifetime or schedule
polling, retries or model calls. Unbound legacy transports and other runtimes are
unchanged; these observations concern assignment-bound Codex `thread/start`.

Late historical appends accept only the same running original fence or its exact
cancelled successor fence, matching immutable admission and worker identity.
They preserve status, leases, failure classification, keys, pins, artifacts and
all other execution state. Conflicting native identities refuse. The existing
current-work UNKNOWN recovery refusal is unchanged.

Creation and cleanup are separate facts. After actual session stop, only the
newly correlated bootstrap path joins the original session supervision,
diagnostic and native pipe executors. A cleanup observation additionally requires
the original process object to have exited, its PID and original runtime
generation to match the prepared correlation, no remaining executor work, and
the same original worker/fence. An unsettled or failed stop records no joined
claim. An absent process is not proof of cleanup; stopping a process does not
prove a persisted native session or other effects never existed. No historical
assignment lacking correlation is backfilled.

The existing worker inspector shows creation outcome, native identity, separate
cleanup outcome and original correlation/generation/fence. Missing evidence is
explicitly Unknown. These are historical observations, not operator actions or
retry/input permission.

## Compatibility

ExecutionWorker state adds `1.17` for observations. The optional nested structure
is versioned `1.0`; legacy assignments omit the field completely and retain their
previous positive/disabled-duration formats. An explicitly read `1.17` envelope
is preserved even without observations. The registered `1.16 -> 1.17` migration
changes only the domain envelope, without bulk assignment rewrites or SQL/table
changes. Old readers reject new fields; they cannot safely run concurrently or
be used for rollback after new observation writes. Deploy through the existing
canonical protocol-extension drain and compatibility Evidence. No Definition
publication or historical failure/pin/key rewrite is part of this change.

This addition cannot reconstruct a previously unrecorded create response. In
particular, historical cancelled reservations with unknown native creation remain
unknown. Any later recovery policy requires its own exact current authority and
effect/custody proof; observation persistence supplies no blanket recovery rule.
