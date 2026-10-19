# Full-shape preservation qualification

The preservation/recovery maintainer owns the serialized operator command
`scripts/ci/run_full_shape_preservation.py`. Run it after changes to preservation
streaming, storage adapters, encrypted capture/restore or their scale fixture.
A green default CI run does not prove this boundary. Its distributed summary and
the default unit runner explicitly report **NOT EXECUTED** and link here.

## Inventory and ownership

The command selects exactly these three methods, in order:

| Module / class | Method |
| --- | --- |
| `tests.test_storage_preservation.SQLiteStoragePreservationTests` | `test_full_current_shape_crosses_old_row_and_value_limits` |
| `tests.test_storage_preservation.PostgresStoragePreservationTests` | `test_full_current_shape_crosses_old_row_and_value_limits` |
| `tests.test_recovery_storage_operator.FullShapeEncryptedStorageTests` | `test_full_current_shape_governed_encrypted_capture_and_isolated_verify` |

The raw cases independently check 118,389 rows, 294,508,748 logical payload
bytes, a 12,373,762-byte largest value and 6,906 malformed legacy values against
literal SQL. The encrypted case adds governed capture and isolated restore,
independent hash/SQL verification and unchanged-source assertions. Its existing
8 GiB disk reservation, 512 MiB operation memory budget and 85-second operation
wall budget remain intact. Semantic-invalid legacy content must remain invalid;
byte preservation is not a health claim.

Normal distributed discovery reports 29 scale-related skips: two raw methods,
one encrypted method and 26 inherited methods on the encrypted subclass. Those
26 are ordinary cases already owned by `PostgresStorageOperatorTests` in the
same distributed job. They are not 26 additional scale targets. The operator
command deliberately does not select the whole subclass. Default CI still runs
all existing backend assertions and explicitly sets the scale flag to `0`.
The broad unit runner rejects flag `1` and directs operators to this owner.

## Bounded invocation

Use Linux, Python with the repository requirements installed, Git, Docker or
Podman, `mkfs.ext4`, loop/mount tools and noninteractive sudo for allocation.
Use a clean committed checkout and an existing caller-owned absolute workspace
path without symlinks. Keep all concurrent qualifications on that same path so
the nonblocking workspace lock serializes them.

```sh
mkdir -p "$HOME/.cache/codex-web-full-shape"
python scripts/ci/run_full_shape_preservation.py \
  --workspace-parent "$HOME/.cache/codex-web-full-shape"
```

For rootful Podman append `--engine podman --sudo-engine`. Only the allocation,
mount and selected engine commands use sudo; the test client uses the caller.
`--status` is read-only and reports ownership without admitting any tests.

The command requires 22 GiB free and physically allocates a 20 GiB loop-backed
ext4 filesystem before starting PostgreSQL or importing application code. It
stages the exact committed source with `git archive`, excludes the deployment
`data` symlink and creates a fresh real directory. SQLite files, PostgreSQL
files, application data, temporary files and operation reservations all reside
inside this allocation. The real PGDATA and reservation directories share a
filesystem and the server reports the same absolute PGDATA path. PGDATA retains
its server-managed permissions. No production DSN or runtime data is accepted;
the command generates a private loopback DSN for a new synthetic-only database.

The pinned PostgreSQL image is recorded in each report. Its container has two
CPUs, 2 GiB memory, 256 PIDs, a read-only root filesystem, bounded temporary
mounts and a 10 MiB log cap. The test client has 4 GiB virtual memory and a
1,200-second CPU ceiling. The whole command has a 1,200-second wall budget,
including setup; teardown commands each have a 30-second timeout. These are
qualification infrastructure bounds, not changes to preservation limits.

## Evidence and cleanup

Each invocation creates `full-shape-*` under the workspace and prints its path.
Retain `report.json`, `tests.json` and `tests.log`: source revision, pinned image,
PostgreSQL/Python versions, exact test IDs, elapsed seconds and phase timings
are evidence. `qualified: true` requires all three exact methods to succeed,
zero skips/errors/failures and successful cleanup. Import failures, missing
backends, partial selections and skipped cases cannot qualify. Attach the source
revision, durations and evidence to the implementation PR or issue; do not
claim scale qualification from an ordinary green distributed job.

On success or failure the owner terminates its client process group, removes
only its named container, unmounts/detaches its filesystem and deletes the disk
allocation. Evidence remains. If cleanup fails, the command reports failure and
retains the occupied allocation and resource identifiers for operator cleanup.
After a host crash, inspect the recorded container, mount and loop device before
removing that job's allocation; never prune unrelated containers or mounts.
