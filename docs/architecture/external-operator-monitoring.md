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
are execution/resource bounds, not a claimed hard token or dollar quota; native
CLI accounting remains the usage source. The monitor uses 256 MiB, a quarter CPU
and a 90-second deadline. An unrecognized or missing unit state holds capacity.
New investigations require at least 8 GiB free disk and 3 GiB available memory.
At 500 retained jobs or 1,000 observation entries, new launches stop for audited
retention management instead of discarding uncertain receipts.
Positive inactive/failed state with PID zero releases global capacity but retains
the interrupted issue for reconciliation; it never replays the same operation.

The adapter writes a launch identity before starting its exact systemd unit.
Unknown start outcomes are read back using that same unit. A retained start file
prevents rerunning a possibly effectful session. Issue creation is also recorded
before its write; an uncertain outcome permits exact-marker readback, never a
second create. A GitHub outage retains the last issue inventory and current host
observations; stale inventory inhibits automatic launches. No observation failure
is treated as proof of successful business work.

Private JSON checkpoints hold observation metadata and launch/session/result
receipts. Message text, thread names, arbitrary runner diagnostics and secrets
are excluded from the reduced observer snapshot. Checkpoints are atomically
replaced under a file lock. Retained uncertain receipts must not be deleted by
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
