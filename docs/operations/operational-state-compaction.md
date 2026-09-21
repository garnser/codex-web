# Operational-state inspection and compaction

Large migrated installations can accumulate operational registries that are useful for runtime compatibility but expensive to inspect or maintain. Codex Web treats cleanup as an explicit operator action; bootstrap never deletes operational history automatically.

## Bounded inspection

Use:

```text
GET /api/operational-state/inspection
```

The inspection path is admin-authorized and runs filesystem work away from the main asyncio event loop. It reports bounded pathology indicators for:

- bot event journal size without scanning the entire journal;
- delivery-target record count;
- reply-target record count;
- queued-turn thread count.

The bootstrap preflight consumes the same bounded inspection summary. A warning means the store merits operator review; it does not trigger compaction.

Current warning thresholds are:

- bot event journal: 128 MiB;
- delivery targets: 25,000 records;
- reply targets: 25,000 records;
- queued-turn registry: 5,000 thread queues.

The event journal intentionally reports `count_exact: false`: an exact line count would require a full scan of a potentially very large file. Journal retention/rotation is owned by the dedicated event-journal lifecycle.

## Delivery-target compaction

The first supported destructive compaction is limited to `bot_delivery_targets`.

Create an exact dry-run plan:

```text
POST /api/operational-state/delivery-targets/plan
```

Planning scans the delivery-target registry in bounded pages on a worker thread and returns exact:

- source record count;
- retained record count;
- redundant aliases to remove;
- canonical keys that need repair/upsert;
- records preserved because they cannot be attributed safely;
- estimated serialized bytes removed;
- source/result checksums and source revision.

### Retention contract

For each active canonical bot binding, Codex Web identifies all delivery targets with the same provider, conversation, and thread identity.

It retains the newest target and ensures the canonical binding keys point to it:

- `<provider>:<conversation>:<thread-id>`
- `thread:<thread-id>:<provider>:<conversation>`

It also retains the external alias for the newest target. Older `external:*` aliases for that same active binding are reconstructible/superseded and may be removed.

Records that cannot be attributed to an active canonical binding are **never deleted by this compactor**. Bare legacy aliases and other non-proven-redundant records are also preserved.

## Apply safeguards

Apply the exact reviewed plan with:

```text
POST /api/operational-state/delivery-targets/apply
```

Apply requires admin authority and rejects a stale plan if the delivery-target namespace changed since planning.

Before mutation the API quiesces bot runtime so Slack/Telegram background activity cannot race the registry. Once the guarded mutation starts, request cancellation does not abandon it between the backup and recovery-metadata boundaries; runtime is restarted afterward.

The service then:

1. revalidates source revision and checksum;
2. writes a complete JSON backup under `data/compaction-backups/`;
3. restricts the backup directory to mode `0700` and backup file to `0600`;
4. fsyncs the backup and directory before mutation;
5. applies deletes/upserts atomically through the canonical StateStore;
6. verifies the exact resulting checksum;
7. atomically refreshes the rollback-compatible JSON mirror;
8. records durable execution/audit metadata including the backup reference.

No automatic compaction runs merely because a threshold is exceeded.

## Recovery

Each execution returns a `backup_ref` and exact rollback instructions. To restore:

```text
POST /api/operational-state/delivery-targets/restore
{
  "backup_ref": "file:///.../delivery-targets-....json"
}
```

Restore is admin-authorized, quiesces bot runtime, verifies the backup checksum, atomically replaces the canonical delivery-target record collection, refreshes the compatibility mirror, and verifies the restored checksum.

The backup reference is restricted to the configured compaction backup directory; arbitrary filesystem paths are rejected.

## Crash boundaries

A failure before canonical replacement leaves the original registry authoritative and the verified backup available. A failure after canonical replacement leaves the compacted canonical registry plus the backup. A compatibility-mirror failure does not invalidate canonical StateStore state; the mirror can be regenerated after recovery.

Compaction deliberately does not delete non-reconstructible state. Any future compactor that does so must define an explicit retention contract and require separate operator confirmation.
