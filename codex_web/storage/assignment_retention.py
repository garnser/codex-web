"""Read-only roots for optional canonical assignment garbage collection.

These references prevent deletion; they never authorize custody or recovery.
Call only while the existing StateStore namespace guards are held. Unknown or
incomplete inventory suppresses pruning, not the worker mutation.
"""
from __future__ import annotations

import json
from pydantic import ValidationError

# Existing canonical namespaces only; no retention index or second history.
REFERENCE_NAMESPACES = (
    'agent_sessions', 'agent_sessions.records.v1', 'thread_bootstrap_bindings',
    'goal_execution_bindings', 'active_turns', 'active_turn_recovery_records',
    'active_turn_recovery_history', 'execution_preflight_attempts', 'turn_queues',
    'artifact_evidence', 'upgrades',
)
MAX_REFERENCE_ROWS = 16384
MAX_REFERENCE_NODES = 131072


class ReferenceInventoryUnavailable(ValueError):
    """Optional GC has no complete interpretation of its retained roots."""


def referenced_assignments(store):
    from codex_web.models import ActiveThreadTurn, ExecutionPreflightAttempt, QueuedTurn
    from codex_web.storage.agent_sessions import AgentSessionStore
    from codex_web.artifact_evidence import ArtifactEvidenceState
    from codex_web.storage.artifact_evidence import ARTIFACT_EVIDENCE_MIGRATIONS, ARTIFACT_EVIDENCE_CONTRACT
    from codex_web.storage.upgrades import UpgradeStore
    from codex_web.compatibility import ContractCompatibilityError
    from codex_web.storage.goal_execution_bindings import GoalExecutionBindingStore
    from codex_web.thread_bootstrap import ThreadBootstrapBindingState, THREAD_BOOTSTRAP_BINDING_CONTRACT

    refs = set()
    remaining = MAX_REFERENCE_NODES

    def walk(value, depth=0):
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > 32:
            raise ReferenceInventoryUnavailable('reference inventory limit')
        if isinstance(value, dict):
            if value.get('target_type') == 'execution_assignment':
                target = value.get('target_id')
                if not isinstance(target, str) or not target:
                    raise ReferenceInventoryUnavailable('invalid typed assignment target')
                refs.add(target)
            for name, item in value.items():
                if name in {'assignment_id', 'previous_assignment_id', 'predecessor_assignment_id',
                            'original_assignment_id', 'failed_assignment_id', 'target_assignment_id'}:
                    if item is not None:
                        if not isinstance(item, str) or not item:
                            raise ReferenceInventoryUnavailable('invalid assignment reference')
                        refs.add(item)
                elif name == 'assignment' and isinstance(item, dict) and 'id' in item:
                    if not isinstance(item['id'], str) or not item['id']:
                        raise ReferenceInventoryUnavailable('invalid assignment reference')
                    refs.add(item['id'])
                walk(item, depth + 1)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item, depth + 1)

    def read(namespace):
        # No lazy initialization or fallback to a filesystem/rollback mirror.
        if store.record_collection_exists(namespace):
            rows = store.record_items(namespace)
        else:
            rows = store.get(namespace)
        if isinstance(rows, (dict, list)) and len(rows) > MAX_REFERENCE_ROWS:
            raise ReferenceInventoryUnavailable('reference inventory limit')
        return rows

    def mapping(value):
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ReferenceInventoryUnavailable('invalid reference collection')
        return value

    try:
        session_namespace = AgentSessionStore.records_namespace
        session_ids = set()
        if store.record_collection_exists(session_namespace):
            if store.record_get(session_namespace, 'meta') != {'schema_version': '1.0'}:
                raise ReferenceInventoryUnavailable('incomplete session records')
            page = getattr(store, 'record_page_projected', None)
            if not callable(page):
                raise ReferenceInventoryUnavailable('session projection unavailable')
            cursor = None
            rows_seen = 0
            while True:
                rows, cursor = page(session_namespace, fields=('id', 'assignment_id'),
                    key_prefix='sessions:', after=cursor, limit=100)
                rows_seen += len(rows)
                if rows_seen > MAX_REFERENCE_ROWS:
                    raise ReferenceInventoryUnavailable('session inventory limit')
                for key, item in rows.items():
                    if (not isinstance(item, dict) or not isinstance(item.get('id'), str)
                            or 'assignment_id' not in item
                            or key != AgentSessionStore._session_key(item['id'])):
                        raise ReferenceInventoryUnavailable('invalid session reference projection')
                    session_ids.add(item['id'])
                    walk(item)
                if cursor is None:
                    break
            cursor = None
            while True:
                rows, cursor = page(session_namespace, fields=('session_id',),
                    key_prefix='native:', after=cursor, limit=100)
                rows_seen += len(rows)
                if rows_seen > MAX_REFERENCE_ROWS:
                    raise ReferenceInventoryUnavailable('session inventory limit')
                if any(not isinstance(item, dict) or not isinstance(item.get('session_id'), str)
                       or item.get('session_id') not in session_ids
                       for item in rows.values()):
                    raise ReferenceInventoryUnavailable('incomplete session alias')
                if cursor is None:
                    break
            count = getattr(store, 'record_count', None)
            if not callable(count) or count(session_namespace) != rows_seen + 1:
                raise ReferenceInventoryUnavailable('unknown session record shape')
        else:
            legacy = read('agent_sessions')
            if legacy is not None:
                if (not isinstance(legacy, dict) or not isinstance(legacy.get('sessions'), list)
                        or not isinstance(legacy.get('schema_version', '0.0'), str)
                        or legacy.get('schema_version', '0.0') not in {'0.0', '1.0', '1.1'}):
                    raise ReferenceInventoryUnavailable('invalid legacy session roots')
                sessions = AgentSessionStore(store)._decode(legacy)
                session_ids = {s.id for s in sessions.sessions}
                walk(sessions.model_dump(mode='json'))

        bootstrap = read('thread_bootstrap_bindings')
        if bootstrap is not None:
            if (not isinstance(bootstrap, dict)
                    or bootstrap.get('schema_version') not in THREAD_BOOTSTRAP_BINDING_CONTRACT.supported):
                raise ReferenceInventoryUnavailable('unknown bootstrap reference schema')
            THREAD_BOOTSTRAP_BINDING_CONTRACT.require(bootstrap['schema_version'])
            walk(ThreadBootstrapBindingState.model_validate(bootstrap).model_dump(mode='json'))
        goals = read('goal_execution_bindings')
        if goals is not None:
            if (not isinstance(goals, dict) or not isinstance(goals.get('schema_version', '1.0'), str)
                    or goals.get('schema_version', '1.0') not in {'1.0', '1.1', '1.2'}
                    or not isinstance(goals.get('bindings'), list)):
                raise ReferenceInventoryUnavailable('invalid Goal roots')
            goal_state = GoalExecutionBindingStore._decode(goals)
            if any(b.agent_session_id not in session_ids
                   for b in goal_state.bindings):
                raise ReferenceInventoryUnavailable('missing Goal session root')
            # Includes the exact retained HomeGoalDelivery, not just session IDs.
            walk(goal_state.model_dump(mode='json'))
        for item in mapping(read('active_turns')).values():
            walk(ActiveThreadTurn.model_validate(item).model_dump(mode='json'))
        for namespace in ('active_turn_recovery_records', 'active_turn_recovery_history'):
            for item in mapping(read(namespace)).values():
                if (not isinstance(item, dict) or not isinstance(item.get('original'), dict)
                        or not isinstance(item.get('recovery_id'), str)):
                    raise ReferenceInventoryUnavailable('invalid recovery root')
                ActiveThreadTurn.model_validate(item['original'])
                walk(item)
        for key, item in mapping(read('execution_preflight_attempts')).items():
            if key.startswith('a/'):
                walk(ExecutionPreflightAttempt.model_validate(item).model_dump(mode='json'))
            elif key.startswith('b/'):
                if not isinstance(item, dict):
                    raise ReferenceInventoryUnavailable('invalid retained batch')
                walk(item)
            elif not key.startswith(('t/', 'wi/', 'q/', 'bn/', 'bt/', 'metadata/')):
                raise ReferenceInventoryUnavailable('unknown preflight record')
        for values in mapping(read('turn_queues')).values():
            if not isinstance(values, list):
                raise ReferenceInventoryUnavailable('invalid retained queue')
            for item in values:
                walk(QueuedTurn.model_validate(item).model_dump(mode='json'))
        # Planned Home checkpoints are named by the existing Upgrade owner.
        # Point-read only those exact metadata Artifacts; do not scan/decrypt
        # model/provider Artifact content or the entire Evidence catalog.
        upgrades = read('upgrades')
        if upgrades is not None:
            if (not isinstance(upgrades, dict) or not isinstance(upgrades.get('schema_version', '1.0'), str)
                    or upgrades.get('schema_version', '1.0') not in {'1.0', '1.1'}
                    or not isinstance(upgrades.get('plans'), dict)):
                raise ReferenceInventoryUnavailable('invalid Upgrade roots')
            plans = UpgradeStore._decode(upgrades).plans.values()
            if len(upgrades.get('plans', {})) > MAX_REFERENCE_ROWS:
                raise ReferenceInventoryUnavailable('Upgrade inventory limit')
            for plan in plans:
                disposition = plan.home_checkpoint_disposition
                if disposition is None:
                    continue
                for aid in {disposition.recorded_artifact_id, disposition.checkpoint_artifact_id,
                            disposition.preservation_artifact_id} - {None}:
                    reader = getattr(store, 'document_array_item_envelope', None)
                    if not callable(reader):
                        raise ReferenceInventoryUnavailable('checkpoint metadata reader unavailable')
                    envelope = reader('artifact_evidence', 'artifacts', aid,
                        organization_id=plan.organization_id, workspace_id=plan.workspace_id,
                        collection_names=('artifacts', 'evidence', 'verifications'))
                    if envelope is None:
                        raise ReferenceInventoryUnavailable('checkpoint outcome unavailable')
                    if not isinstance(envelope, dict):
                        raise ReferenceInventoryUnavailable('invalid checkpoint envelope')
                    version = envelope.get('schema_version', '0.0')
                    if not isinstance(version, str) or version not in {'0.0', *ARTIFACT_EVIDENCE_CONTRACT.supported}:
                        raise ReferenceInventoryUnavailable('unknown checkpoint envelope')
                    if version != ARTIFACT_EVIDENCE_CONTRACT.current:
                        envelope = ARTIFACT_EVIDENCE_MIGRATIONS.migrate(envelope,
                            from_version=version, to_version=ARTIFACT_EVIDENCE_CONTRACT.current)
                    ARTIFACT_EVIDENCE_CONTRACT.require(envelope.get('schema_version', ''))
                    state = ArtifactEvidenceState.model_validate(envelope)
                    matches = [a for a in state.artifacts if a.id == aid
                        and a.organization_id == plan.organization_id and a.workspace_id == plan.workspace_id]
                    if len(matches) != 1:
                        raise ReferenceInventoryUnavailable('checkpoint metadata unavailable')
                    artifact = matches[0]
                    field = {'home-upgrade-checkpoint': 'checkpoint',
                        'home-upgrade-dormant-preservation': 'dormant_preservation'}.get(artifact.source)
                    if field is None or not isinstance(artifact.metadata.get(field), str):
                        raise ReferenceInventoryUnavailable('checkpoint metadata changed')
                    checkpoint = json.loads(artifact.metadata[field])
                    if not isinstance(checkpoint, dict):
                        raise ReferenceInventoryUnavailable('invalid checkpoint reference')
                    if field == 'checkpoint' and not checkpoint.get('assignment_id'):
                        raise ReferenceInventoryUnavailable('missing checkpoint assignment')
                    if field == 'dormant_preservation' and not isinstance(checkpoint.get('assignment'), dict):
                        raise ReferenceInventoryUnavailable('missing dormant assignment')
                    walk(checkpoint)
    except (ValidationError, ContractCompatibilityError, json.JSONDecodeError) as exc:
        raise ReferenceInventoryUnavailable('malformed reference inventory') from exc
    return refs
