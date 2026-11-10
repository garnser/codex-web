# External operator monitoring

The optional adapter in `scripts/operator/mission_supervisor.py` keeps external
operator observation available across conversation and application interruptions.
It consumes the existing deterministic observer, systemd service state, bounded
host metrics and canonical GitHub issue/PR state. It does not replace project
orchestration, an execution worker, a Definition, a permission store, a queue or
an ActionIntent. GitHub remains delivery truth; application and provider state
remain operational truth. Home operator interaction is a separate capability.

Collection uses no model. A new open bug, a newly failed native turn, a real
service restart, or a persistent current-runner/host failure can qualify an
engineering investigation. Idle, intentionally retired runner history does not.
The first observed historical failed turn is a baseline, not a new failure.
Readiness HTTP status is recorded separately from process liveness: retained
queues may legitimately produce 503 without a crash. Maintenance suppresses
failure classification, model launch and application health probes.

Only the existing noninteractive Codex CLI performs reasoning, using the
operator's existing authentication and configured model. No credentials are
copied into configuration, prompts, issues or public evidence. One isolated
checkout and one bounded user service belong to each investigation. A completed
investigation can continue in its original checkout only after a changed CI
failure or a positive current required-check gate; the prior structured result
provides the checkpoint. There is no model polling for CI status. Merge and
production qualification remain with the established operator workflow, subject
to the existing gates. A returned `fixed` result is not issue-closure evidence.

## Bounds and reconciliation

The default limit is one automatic investigation at a time, six launches per
rolling day, at least 30 minutes between launches, and four investigation rounds
per issue. Each unit has a 20-minute lifetime, 2 GiB memory, no swap, one CPU and
bounded output files. The child CLI has an earlier 1,100-second timeout. These
output limits apply separately to supervisor-owned stdout/stderr pipes, not to
legitimate CLI state databases or files created by engineering tools. Do not
apply a blanket `LimitFSIZE` to the CLI. Timeout, overflow or incomplete pipe
cleanup signals the created process group and escalates to group SIGKILL before
reaping the leader and joining I/O. Existing unit control-group cleanup remains
the outer ownership boundary for independently detached descendants. These
are execution/resource bounds, not a claimed hard token or dollar quota; native
CLI accounting remains the usage source. The monitor uses 256 MiB, a quarter CPU
and a 90-second deadline. An unrecognized or missing unit state holds capacity.
New investigations require at least 8 GiB free disk and 3 GiB available memory.
At 500 retained jobs or 1,000 observation entries, new launches stop for audited
retention management instead of discarding uncertain receipts.
Positive inactive/failed state with PID zero and no queued systemd job releases
global capacity (including when a finished receipt already exists), but retains
the interrupted issue for reconciliation; it never replays the same operation.
Current mission, automatic-launch, coordinator-lease and maintenance controls are
rechecked before checkout and again before model startup. If startup is deferred
with a positive `model_not_started` receipt and no started marker, the original
receipt remains. After controls permit work, backoff elapses and a fresh exact
unit read proves it is reaped, that signal may qualify one new investigation
identity under the same daily/per-issue bounds. Started or unknown sessions never
use this path.

The adapter writes a launch identity before starting its exact systemd unit.
Unknown start outcomes are read back using that same unit. A retained start file
prevents rerunning a possibly effectful session. Issue creation is also recorded
before its write; an uncertain outcome permits exact-marker readback, never a
second create. A GitHub outage retains the last issue inventory and current host
observations; stale inventory inhibits automatic launches. No observation failure
is treated as proof of successful business work.

Observer timestamp freshness and inspection coverage are separate facts. The
reduced snapshot preserves only a closed `inspection_status` enum (`complete`,
`incomplete`, `unknown`), a bounded failed-thread count and a boolean inspection
result for each retained thread. A declared complete inspection cannot override
failed thread reads, malformed/truncated inventory or inspection-failure
conditions. Error messages and condition details are not retained. Older
snapshots without a boolean completeness flag have unknown project coverage;
their valid individual runner samples can still establish positive observations.

A recent incomplete or unknown inspection produces one stable coverage signal,
subject to the existing persistence, issue deduplication and launch bounds.
Successful complete inspection makes that coverage signal noncurrent. Stale
snapshots instead produce the existing stale-observer signal, and failed or stale
thread samples cannot refresh runner-failure evidence. Retained runner failures
expose `observation_status`: `observed_failure` for a fresh positive observation,
`not_observed` after a fresh successful read of that same thread without the
failure, and `unknown` when that read is unavailable. A missing thread, even in
a complete inventory, does not prove that its runner recovered. Historical
failure metadata, last-positive timestamps and existing issue/launch receipts
remain intact. None of these observation states closes an issue or authorizes
recovery, replacement, execution or replay. `summary.json` exposes current
inspection coverage separately from service liveness and readiness.

The standalone producer in `scripts/operator/veridataops_observer.py` preserves
the canonical HTTP runner projection's presence before flattening its existing
metadata. Its closed `runner_projection_status` marker is `present` for a
recognized canonical runner state, `absent` only for an explicit successful
`runner: null` response, and `invalid` for missing/malformed projections. The
canonical `waiting` and `cancelled` states are valid observations. An explicit
null or a producer-marked absence is successful inspection, while its runner
outcome remains unknown. The reducer requires the producer's exact eleven-field
all-null shape when accepting flattened absence; a legacy all-null projection
without the marker does not establish successful absence. Contradictory markers,
missing runners, malformed projections and failed reads remain incomplete.
An absent projection never settles a previously observed runner failure.

Install the reviewed producer source as well as the supervisor when updating
this boundary. The producer retains its existing optional report behavior and
command-line deployment configuration; disk observation uses the configured
state-directory filesystem instead of a host-specific home path. The default
observer invocation does not publish incidents. Its installed path/unit remain
operator-owned deployment values, not a new project dispatcher or authority.

Private JSON checkpoints hold observation metadata and launch/session/result
receipts. Message text, thread names, arbitrary runner diagnostics and secrets
are excluded from the reduced observer snapshot. Checkpoints are atomically
replaced with file and directory synchronization under a file lock. Canonical
identifiers, enum states, booleans and counts are validated by type and shape;
provider error text is omitted. Public issue metadata is filtered independently.
Retained uncertain receipts must not be deleted by
routine cleanup. Inactive observation entries without writes/jobs can be pruned;
checkouts and investigation audit files require explicit retention management.
Disk pressure inhibits useful operation through its tracked signal rather than
silently deleting evidence.

## Deployment and controls

Independent review, focused tests and an actual bounded engineering probe are
required before enabling automatic launches. The example config defaults them
off. Install a reviewed source revision at
`~/.local/lib/codex-web-mission-monitor/mission_supervisor.py`; keep private config
and checkpoints in `~/.local/state/codex-web-mission-monitor` with directory mode
0700 and files 0600. Use the absolute existing repository, observer snapshot and
authenticated CLI paths. Configuration contains deployment values, not a second
catalog of project roles, rules or authority.

The system monitor service template requires substitution of the operating
user/UID. Install its timer as a system timer and the investigation template as
a user unit. The user's manager must survive logout (systemd linger). Collection
does not depend on codex-web being active. The timer starts after boot and runs
60 seconds after each completed collection, preventing overlapping cycles.
Do not enable a second copy of the existing project observer.

The `heartbeat` operation renews the external coordinator lease for 15 minutes;
while valid, automatic launches are suppressed to avoid competing with the
interactive engineering team. Set `maintenance_until` before a qualified cold
deployment. The `disable` operation prevents new collection-driven work; to stop
an already running investigation, stop its exact user unit and retain its
receipts for reconciliation. Disabling the adapter does not pause application
work or alter provider state. Observe `summary.json`, `latest.json`, job receipts
and systemd journals to distinguish collection, launch, investigation result and
actual canonical issue/deployment completion.

Operational changes are tracked in GitHub issue #1696. This contract does not
assert that every deployment has enabled the optional adapter.
