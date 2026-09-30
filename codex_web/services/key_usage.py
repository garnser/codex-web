from __future__ import annotations

from codex_web.crypto import KeyStatus, KeyVersionStatus
from codex_web.storage.recovery import RecoveryStore


class KeyUsageService:
    """Bounded metadata projection over registered canonical encrypted domains."""

    MAX_SCAN = 5000
    MAX_DISPLAY = 100

    def __init__(self, store):
        self.recovery = RecoveryStore(store)

    def snapshot(self, key, actor, *, version=None, recovery=None):
        state = recovery if recovery is not None else self.recovery.load()
        rows = []
        scanned = 0
        versions = {item.version: item.status for item in key.versions}

        def charge():
            nonlocal scanned
            scanned += 1
            if scanned > self.MAX_SCAN:
                raise ValueError('Key dependency scan exceeds its bounded limit')

        def add(kind, object_id, label, required, relationship):
            charge()
            if version is not None and version not in required:
                return
            broken = key.status == KeyStatus.REVOKED or any(
                number not in versions or versions[number] == KeyVersionStatus.REVOKED
                for number in required
            )
            rows.append({'object_type': kind, 'object_id': object_id, 'label': label,
                         'relationship': relationship, 'versions': list(required),
                         'broken': broken, 'page': 'operations', 'scope': 'shared_workspace'})

        if state.policy and state.policy.backup_key_id == key.id:
            add('recovery_policy', state.policy.id, 'Current backup policy',
                (key.current_version,), 'active_key_reference')
        for backup in state.backups.values():
            charge()
            if (backup.organization_id, backup.workspace_id) != (actor.organization_id, actor.workspace_id):
                continue
            if backup.key_id == key.id:
                add('backup', backup.id, 'Encrypted recovery backup',
                    (backup.key_version,), 'encrypted_content')
            for entry in backup.key_manifest:
                charge()
                if entry.key_id == key.id:
                    add('backup', backup.id, 'Restore key-manifest requirement',
                        entry.versions, 'restore_requirement')
        return {'schema_version': '1.0', 'available': True, 'key_id': key.id,
                'version': version, 'count': len(rows), 'blocking_count': len(rows),
                'items': rows[:self.MAX_DISPLAY], 'truncated': len(rows) > self.MAX_DISPLAY,
                'coverage': ['recovery_policy', 'backup_envelopes', 'backup_key_manifests'],
                'limitations': [
                    'The frozen backup key manifest is a restore requirement, not proof that every key encrypted snapshot content.',
                    'External copies and unregistered encrypted domains cannot be enumerated here.',
                    'Rotation preserves old decrypt-only versions; revocation is blocked while canonical consumers retain them.',
                ]}
