from __future__ import annotations

import hashlib
import json
import uuid
from urllib.parse import urlsplit

from typing import Any, Callable
from urllib.parse import quote

from codex_web.action_intents import ActionIntent, ActionIntentState, ActionIntentStatus, ActionIntentReceipt
from codex_web.compatibility import ContractSpec, MigrationRegistry
from codex_web.storage.state_store import StateStore


_ORIGIN_INDEX_EPOCH = uuid.uuid4().hex

ACTION_INTENT_STATE_CONTRACT = ContractSpec("action-intent-state", "1.6", ("1.6",))
ACTION_INTENT_STATE_MIGRATIONS = MigrationRegistry("action-intent-state")
ACTION_INTENT_STATE_MIGRATIONS.register("1.5", "1.6", lambda payload: {**payload, "schema_version": "1.6"})
ACTION_INTENT_STATE_MIGRATIONS.register(
    "0.0",
    "1.0",
    lambda payload: {
        "schema_version": "1.0",
        **{key: value for key, value in payload.items() if key != "schema_version"},
    },
)
ACTION_INTENT_STATE_MIGRATIONS.register(
    "1.2", "1.3", lambda payload: {**payload, "schema_version": "1.3"},
)
ACTION_INTENT_STATE_MIGRATIONS.register("1.3", "1.4", lambda payload: {**payload, "schema_version": "1.4"})


def _security_migration(payload: dict[str, Any]) -> dict[str, Any]:
    migrated = dict(payload)
    intents: list[dict[str, Any]] = []
    for raw in migrated.get("intents", []) or []:
        item = dict(raw)
        definition = dict(item.get("action_definition") or {})
        authority = dict(item.get("authority_decision") or {})
        policy = dict(item.get("policy_decision") or {})
        resource_ids = list(item.get("resource_ids") or [])
        item.setdefault(
            "security_policy",
            {
                "sandbox": "workspace-write",
                "network": {
                    "enabled": False,
                    "allowed_schemes": ["https"],
                    "allowed_hosts": [],
                    "allowed_ports": [443],
                    "allowed_private_cidrs": [],
                    "max_redirects": 0,
                },
                "filesystem": {
                    "allowed_read_roots": [],
                    "allowed_write_roots": [],
                    "allow_symlink_escape": False,
                },
                "process": {
                    "allow_process_execution": True,
                    "allowed_executables": [],
                    "allow_shell": False,
                },
                "require_digest_for_executable_artifacts": True,
            },
        )
        item.setdefault(
            "security_decision",
            {
                "outcome": "deny",
                "source": "security:migration",
                "risk_class": str(definition.get("risk_class") or "medium"),
                "resource_ids": resource_ids,
                "sandbox": "workspace-write",
                "network_enabled": False,
                "authority_source": str(authority.get("source") or "unknown"),
                "policy_source": str(policy.get("source") or "unknown"),
                "reasons": [
                    "migrated pre-security-boundary intent requires re-evaluation"
                ],
            },
        )
        intents.append(item)
    migrated["intents"] = intents
    migrated["schema_version"] = "1.1"
    return migrated


ACTION_INTENT_STATE_MIGRATIONS.register("1.0", "1.1", _security_migration)
ACTION_INTENT_STATE_MIGRATIONS.register(
    "1.1",
    "1.2",
    lambda payload: {
        **payload,
        "schema_version": "1.2",
        "intents": [
            {**dict(item), "failure": dict(item).get("failure")}
            for item in payload.get("intents", [])
        ],
    },
)


class ReadinessOriginIndexProgressing(ValueError):
    """A bounded preparation advanced but has not reached the catalog end."""

    def __init__(self, cursor: str, epoch: str):
        self.cursor = cursor
        self.epoch = epoch
        super().__init__("readiness origin index backfill is progressing")


class ActionIntentStore:
    namespace = "action_intents"

    def __init__(self, store: StateStore) -> None:
        self.store = store

    def _decode(self, payload: Any) -> ActionIntentState:
        if payload is None:
            return ActionIntentState()
        if not isinstance(payload, dict):
            raise ValueError("action intent state must be an object")
        version = str(payload.get("schema_version") or "0.0")
        if version != ACTION_INTENT_STATE_CONTRACT.current:
            payload = ACTION_INTENT_STATE_MIGRATIONS.migrate(
                payload,
                from_version=version,
                to_version=ACTION_INTENT_STATE_CONTRACT.current,
            )
        ACTION_INTENT_STATE_CONTRACT.require(payload.get("schema_version", ""))
        return ActionIntentState.model_validate(payload)

    records_namespace = "action_intents.records.v1"
    _meta_key = "meta"
    _fields = ("intents", "receipts", "verifications", "inbox")
    _publication_marker = "publication-index:1.0"

    @staticmethod
    def publication_instance(value: str) -> str:
        """Code-host bindings use host[:port], webhook configuration uses URLs."""
        return urlsplit(value if '://' in value else '//' + value).netloc.casefold()

    @classmethod
    def publication_prefix(cls, kind: str, *, organization_id: str, workspace_id: str,
                           project_id: str, resource_id: str, provider_instance: str,
                           value: str) -> str:
        scope = [organization_id, workspace_id, project_id, resource_id,
                 cls.publication_instance(provider_instance), value]
        return 'publication-' + kind + ':' + hashlib.sha256(
            json.dumps(scope, separators=(',', ':')).encode()).hexdigest() + ':'

    @classmethod
    def _publication_indexes(cls, item: ActionIntent) -> dict[str, Any]:
        if (item.provider_type != 'gitlab' or not item.project_id or not item.work_item_ref
                or len(item.resource_ids) != 1
                or item.action_id not in {'code-host.branch.publish', 'code-host.pull-request.upsert'}):
            return {}
        branch = item.request.parameters.get('branch' if item.action_id == 'code-host.branch.publish' else 'head')
        if not isinstance(branch, str) or not branch:
            return {}
        scope = dict(organization_id=item.organization_id, workspace_id=item.workspace_id,
            project_id=item.project_id, resource_id=item.resource_ids[0], provider_instance=item.provider_instance)
        indexes = {cls.publication_prefix(kind, value=value, **scope) + item.id: {'id': item.id}
                   for kind, value in (('branch', branch), ('work', item.work_item_ref))}
        indexes[cls.publication_prefix('project-branch', value=branch,
            **{**scope, 'resource_id':'', 'provider_instance':''}) + item.id] = {'id':item.id}
        return indexes

    @staticmethod
    def _scope(organization_id: str, workspace_id: str) -> str:
        return f"{quote(organization_id, safe='')}:{quote(workspace_id, safe='')}:"

    @classmethod
    def _idempotency_key(cls, organization_id, workspace_id, binding_id, action_id, key):
        identity = json.dumps([organization_id, workspace_id, binding_id, action_id, key])
        return "idempotency:" + hashlib.sha256(identity.encode()).hexdigest()

    def find_idempotent(self, organization_id, workspace_id, binding_id, action_id, key):
        self._ensure_records()
        alias = self.store.record_get(self.records_namespace, self._idempotency_key(
            organization_id, workspace_id, binding_id, action_id, key,
        ))
        return self.get(alias["id"]) if alias is not None else None

    def insert(self, intent: ActionIntent, *, guard_rows=None, guard=None) -> ActionIntent:
        self._ensure_records()
        alias_key = self._idempotency_key(intent.organization_id, intent.workspace_id,
                                          intent.binding_id, intent.action_id, intent.idempotency_key)
        result = []
        def apply(current):
            alias = current[alias_key]
            if alias is not None and intent.status != ActionIntentStatus.CANCELLED:
                result.append(alias["id"])
                return {}
            result.append(intent.id)
            return self._records(ActionIntentState(intents=[intent]))
        if guard is None:
            self.store.record_mutate(self.records_namespace, (alias_key,), apply)
        else:
            def guarded(documents):
                guard(documents)
                records = documents[self.records_namespace]
                return {self.records_namespace: apply({alias_key: records.get(alias_key)})}
            self.store.record_mutate_many({**guard_rows, self.records_namespace: (alias_key,)}, guarded)
        if result[0] == intent.id:
            return intent
        existing = self.get(result[0])
        if existing is None:
            raise ValueError("action idempotency alias points to missing intent")
        return existing

    @classmethod
    def _indexes(cls, item: ActionIntent) -> dict[str, Any]:
        scope = cls._scope(item.organization_id, item.workspace_id)
        indexes = {**cls._publication_indexes(item), **cls._ready_origin_indexes(item)}
        if item.status != ActionIntentStatus.CANCELLED:
            indexes[cls._idempotency_key(item.organization_id, item.workspace_id,
                                        item.binding_id, item.action_id, item.idempotency_key)] = {"id": item.id}
        if item.status == ActionIntentStatus.PENDING:
            indexes[f"pending:{scope}{item.created_at:024.9f}:{item.id}"] = {"id": item.id}
        if item.status in {ActionIntentStatus.CLAIMED, ActionIntentStatus.EXECUTING} and item.lease:
            indexes[f"lease:{scope}{item.lease.expires_at:020.6f}:{item.id}"] = {
                "id": item.id, "expires_at": item.lease.expires_at,
            }
        return indexes

    @classmethod
    def _records(cls, state: ActionIntentState) -> dict[str, Any]:
        records = {}
        for field in cls._fields:
            for item in getattr(state, field):
                # Histories are ordered and scoped by their parent intent.
                if field == "intents":
                    key = f"intents:{item.id}"
                    records.update(cls._indexes(item))
                else:
                    timestamp = getattr(item, "received_at", getattr(item, "verified_at", 0.0))
                    key = f"{field}:{quote(item.intent_id or '', safe='')}:{timestamp:020.6f}:{item.id}"
                records[key] = item.model_dump(mode="json")
        return records

    def _ensure_records(self) -> None:
        if not self.store.record_collection_exists(self.records_namespace):
            self.store.record_mutate(self.records_namespace, (), lambda _current: {})
        meta = self.store.record_get(self.records_namespace, self._meta_key)
        if meta is not None:
            if meta in ({"schema_version": "1.0"}, {"schema_version": "1.1"}, {"schema_version": "1.2"}):
                def upgrade(current):
                    if current[self._meta_key] not in ({"schema_version": "1.0"}, {"schema_version": "1.1"}, {"schema_version": "1.2"}, {"schema_version": "1.3"}):
                        raise ValueError("unsupported action intent records schema")
                    return {self._meta_key: {"schema_version": "1.3"}}
                self.store.record_mutate(self.records_namespace, (self._meta_key,), upgrade)
            elif meta not in ({"schema_version": "1.3"}, {"schema_version": "1.4"}, {"schema_version": "1.5"}):
                raise ValueError("unsupported action intent records schema")
            return
        # Upgrade both namespaces under the shared transaction lock. The old
        # document is a rollback checkpoint, never an active second authority.
        def migrate(documents):
            records = documents[self.records_namespace]
            if self._meta_key not in records:
                fresh = documents[self.namespace] is None and not records
                records = self._records(self._decode(documents[self.namespace]))
                records[self._meta_key] = {"schema_version": "1.4" if fresh else "1.3"}
                if fresh:
                    records[self._publication_marker] = {'schema_version': '1.0', 'after': None, 'ready': True}
            return {self.records_namespace: records, self.namespace: documents[self.namespace]}
        self.store.update_many(
            {self.namespace: None, self.records_namespace: {}},
            migrate,
        )

    def prepare_publication_index(self) -> None:
        """Explicit pre-admission migration after older writers are drained.

        The metadata barrier fences 1.3 writers before resumable page backfill.
        Webhook lookups never call this method or initialize missing collections.
        """
        self._ensure_records()
        progress = self.store.record_get(self.records_namespace, self._publication_marker)
        if progress is not None and not self._valid_publication_progress(progress):
            raise ValueError('publication_index_metadata_invalid')
        def fence(current):
            if current[self._meta_key] not in ({'schema_version': '1.3'}, {'schema_version': '1.4'}, {'schema_version': '1.5'}):
                raise ValueError('unsupported action intent records schema')
            return {self._meta_key: {'schema_version': '1.5' if current[self._meta_key] == {'schema_version':'1.5'} else '1.4'}}
        self.store.record_mutate(self.records_namespace, (self._meta_key,), fence)
        while True:
            progress = self.store.record_get(self.records_namespace, self._publication_marker)
            if progress is not None and not self._valid_publication_progress(progress):
                raise ValueError('publication_index_metadata_invalid')
            if progress and progress['ready']:
                return
            page, after = self.store.record_page(self.records_namespace,
                key_prefix='intents:', after=(progress or {}).get('after'), limit=100)
            def apply(current):
                # Re-read every candidate under the canonical transaction lock.
                if current.get(self._publication_marker) != progress:
                    return {}
                changes = {}
                for key in page:
                    raw = current.get(key)
                    if raw is not None:
                        changes.update(self._publication_indexes(ActionIntent.model_validate(raw)))
                changes[self._publication_marker] = {'schema_version': '1.0', 'after': after, 'ready': after is None}
                return changes
            self.store.record_mutate(self.records_namespace,
                (*page, self._publication_marker), apply)

    def publication_index_ready(self) -> bool:
        if not self.store.record_collection_exists(self.records_namespace):
            return False
        marker = self.store.record_get(self.records_namespace, self._publication_marker)
        return (self.store.record_get(self.records_namespace, self._meta_key) in ({'schema_version': '1.4'}, {'schema_version': '1.5'})
            and self._valid_publication_progress(marker) and marker['ready'] is True)

    @staticmethod
    def _valid_publication_progress(marker) -> bool:
        return (isinstance(marker, dict) and set(marker) == {'schema_version', 'after', 'ready'}
            and marker['schema_version'] == '1.0' and type(marker['ready']) is bool
            and (marker['after'] is None or (isinstance(marker['after'], str)
                 and marker['after'].startswith('intents:')))
            and (not marker['ready'] or marker['after'] is None))

    def publication_candidates(self, kind: str, *, limit: int = 100, **scope) -> list[ActionIntent]:
        # No _ensure_records: an ingress read cannot upgrade or backfill state.
        if not self.publication_index_ready():
            raise ValueError('publication_index_not_ready')
        prefix = self.publication_prefix(kind, **scope)
        rows, after = self.store.record_page(self.records_namespace, key_prefix=prefix, limit=limit)
        if after is not None:
            raise ValueError('publication_candidates_exceed_bound')
        result = []
        for key, alias in rows.items():
            raw = self.store.record_get(self.records_namespace, 'intents:' + alias['id'])
            if raw is None:
                raise ValueError('publication_index_target_missing')
            item = ActionIntent.model_validate(raw)
            if key not in self._publication_indexes(item):
                raise ValueError('publication_index_scope_changed')
            result.append(item)
        return result

    def _state(self, records: dict[str, Any]) -> ActionIntentState:
        return self._decode({
            "schema_version": ACTION_INTENT_STATE_CONTRACT.current,
            **{field: [raw for key, raw in sorted(records.items())
                       if key.startswith(field + ":") and raw is not None]
               for field in self._fields},
        })

    @staticmethod
    def _ready_origin_indexes(item):
        if (item.action_id == 'code-host.pull-request.ready' and item.readiness_origin is not None
                and item.status in {'executing', 'uncertain', 'requires_reconciliation'}
                and item.attempt > 0 and item.execution_started_at is not None):
            return {'ready-origin:' + quote(item.readiness_origin.assignment_id, safe='') + ':' + item.id:
                {'id':item.id}}
        return {}

    def unsettled_readiness_origin(self, assignment_id: str, *, prepare=True) -> tuple[ActionIntent, ...]:
        """Current origin projection; rebuild once per reader process after old writers.

        Pages advance durably for this process epoch. A new process deliberately
        starts a fresh backfill, so a previous older writer cannot leave a false
        complete index. Ordinary updates maintain aliases in the same commit.
        """
        if prepare:
            self._ensure_records()
        elif (not self.store.record_collection_exists(self.records_namespace)
                or self.store.record_get(self.records_namespace, self._meta_key)
                    not in ({'schema_version':'1.3'}, {'schema_version':'1.4'}, {'schema_version':'1.5'})):
            raise ValueError('readiness origin records require canonical preparation')
        # Separate cursors keep current replicated readers from restarting
        # each other's bounded migration. Aliases remain shared canonical data.
        marker = 'ready-origin-index:1.0:' + _ORIGIN_INDEX_EPOCH
        for _ in range(8):
            progress = self.store.record_get(self.records_namespace, marker)
            if progress is not None and (not isinstance(progress,dict)
                    or set(progress) != {'schema_version','epoch','after','ready'}
                    or progress['schema_version'] != '1.0'
                    or not isinstance(progress['epoch'],str) or len(progress['epoch']) != 32
                    or any(char not in '0123456789abcdef' for char in progress['epoch'])
                    or not isinstance(progress['ready'],bool)
                    or progress['after'] is not None and not isinstance(progress['after'],str)
                    or progress['ready'] and progress['after'] is not None):
                raise ValueError('readiness origin index metadata is invalid')
            if progress and progress.get('epoch') == _ORIGIN_INDEX_EPOCH and progress.get('ready') is True:
                break
            if not prepare:
                raise ValueError('readiness origin index requires canonical preparation')
            previous = progress
            after = progress.get('after') if progress and progress.get('epoch') == _ORIGIN_INDEX_EPOCH else None
            page, following = self.store.record_page(self.records_namespace,
                key_prefix='intents:', after=after, limit=128)
            def apply(current):
                if current.get(marker) != previous:
                    return {}
                changes = {}
                for key in page:
                    if current.get(key) is not None:
                        changes.update(self._ready_origin_indexes(ActionIntent.model_validate(current[key])))
                changes[marker] = {'schema_version':'1.0','epoch':_ORIGIN_INDEX_EPOCH,
                    'after':following,'ready':following is None}
                return changes
            self.store.record_mutate(self.records_namespace,(*page,marker),apply)
        progress = self.store.record_get(self.records_namespace,marker)
        if not progress or progress.get('epoch') != _ORIGIN_INDEX_EPOCH or progress.get('ready') is not True:
            if (prepare and isinstance(progress, dict)
                    and progress.get('epoch') == _ORIGIN_INDEX_EPOCH
                    and isinstance(progress.get('after'), str)
                    and progress.get('ready') is False):
                raise ReadinessOriginIndexProgressing(progress['after'], _ORIGIN_INDEX_EPOCH)
            raise ValueError('readiness origin index preparation is unavailable')
        prefix = 'ready-origin:' + quote(assignment_id,safe='') + ':'
        rows, after = self.store.record_page(self.records_namespace,key_prefix=prefix,limit=64)
        if after is not None:
            raise ValueError('unsettled readiness origin exceeds the bounded active-action inventory')
        found = []
        for key, alias in rows.items():
            if (not isinstance(alias, dict) or set(alias) != {'id'}
                    or not isinstance(alias['id'], str)):
                raise ValueError('readiness origin alias is invalid')
            item = self.get(alias['id'])
            if item is None and not prepare:
                raise ValueError('readiness origin target is unavailable')
            if item is not None and key in self._ready_origin_indexes(item):
                found.append(item)
        return tuple(found)

    def get(self, intent_id: str) -> ActionIntent | None:
        self._ensure_records()
        raw = self.store.record_get(self.records_namespace, f"intents:{intent_id}")
        return ActionIntent.model_validate(raw) if raw is not None else None

    def _pages(self, prefix: str):
        after = None
        while True:
            page, after = self.store.record_page(
                self.records_namespace, key_prefix=prefix, after=after, limit=100,
            )
            yield from page.items()
            if after is None:
                return

    def indexed_intents(self, kind: str, *, organization_id: str | None = None,
                        workspace_id: str | None = None, now: float | None = None):
        self._ensure_records()
        prefix = kind + ":"
        if organization_id is not None and workspace_id is not None:
            prefix += self._scope(organization_id, workspace_id)
        for _, alias in self._pages(prefix):
            if kind == "lease" and now is not None and alias["expires_at"] > now:
                continue
            item = self.get(alias["id"])
            if item is not None:
                yield item

    def home_recovery_candidates(self, query):
        """Original-row predicate, never derived alias completeness."""
        from codex_web.storage.home_intent_query import HomeIntentRecoveryQuery
        if not isinstance(query, HomeIntentRecoveryQuery):
            raise ValueError('Typed Home recovery query required')
        self._ensure_records()
        reader = getattr(self.store, 'home_intent_recovery_rows', None)
        if not callable(reader):
            raise ValueError('Canonical Home recovery query unsupported')
        rows = reader(self.records_namespace, query)
        result = []
        for raw in rows:
            item = ActionIntent.model_validate(raw)
            if (item.organization_id,item.workspace_id) != (query.organization_id,query.workspace_id):
                raise ValueError('Home recovery original scope indeterminate')
            if query.execution_id is not None:
                if item.execution_id != query.execution_id:
                    raise ValueError('Home recovery original execution indeterminate')
            else:
                command = query.command
                if (item.project_id != 'home' or item.action_id != 'operator.host.command'
                        or item.provider_type != command.provider_type or item.provider_instance != command.provider_instance
                        or item.requested_by != command.requested_by or item.resource_ids != command.resource_ids
                        or tuple(item.request.parameters.get('argv', ())) != command.argv):
                    raise ValueError('Home recovery original command indeterminate')
            result.append(item)
        return tuple(result)

    def recovery_last_receipt(self, intent):
        if not intent.last_receipt_id:
            return None
        rows, after = self.store.record_page(self.records_namespace,
            key_prefix='receipts:' + quote(intent.id, safe='') + ':', limit=128)
        if after is not None:
            raise ValueError('Home recovery original receipt inventory exceeded')
        found = []
        for raw in rows.values():
            receipt = ActionIntentReceipt.model_validate(raw)
            if receipt.intent_id != intent.id:
                raise ValueError('Home recovery receipt origin invalid')
            if receipt.id == intent.last_receipt_id:
                if any(getattr(receipt,field) != getattr(intent,field) for field in
                        ('attempt','provider_type','provider_instance','action_id','idempotency_key','correlation_id')):
                    raise ValueError('Home recovery receipt identity invalid')
                found.append(receipt)
        if len(found) != 1:
            raise ValueError('Home recovery original receipt unavailable')
        return found[0]

    def intent_page(self, *, after: str | None = None, limit: int = 100):
        """Read complete validated intents without receipt/verification history.

        Canonical intent rows, including terminal rows, are still fully validated.
        No pending/lease alias can stand in for unsettled or uncertain intents.
        """
        self._ensure_records()
        rows, cursor = self.store.record_page(self.records_namespace,
            key_prefix="intents:", after=after, limit=limit)
        return [ActionIntent.model_validate(raw) for raw in rows.values() if raw is not None], cursor

    def load(self, *, intent_id: str | None = None) -> ActionIntentState:
        self._ensure_records()
        if intent_id is None:
            return self._state(self.store.record_items(self.records_namespace))
        records = {f"intents:{intent_id}": self.store.record_get(
            self.records_namespace, f"intents:{intent_id}",
        )}
        for field in self._fields[1:]:
            records.update(self._pages(f"{field}:{quote(intent_id, safe='')}:"))
        return self._state(records)

    def flush_legacy_mirror(self) -> None:
        self.store.put(self.namespace, self.load().model_dump(mode="json"))

    def update(self, updater: Callable[[ActionIntentState], ActionIntentState], *,
               intent_ids: tuple[str, ...] | None = None,
               guard_rows=None, guard=None) -> ActionIntentState:
        self._ensure_records()
        # Compatibility/bulk callers can still transform a complete snapshot;
        # execution hot paths explicitly select only their target intent rows.
        keys = (tuple(self.store.record_items(self.records_namespace)) if intent_ids is None
                else tuple(f"intents:{item_id}" for item_id in intent_ids))
        result = []
        def apply(current):
            state = self._state(current)
            before = self._records(state)
            updated = updater(state)
            after = self._records(updated)
            changes = {key: raw for key, raw in after.items() if before.get(key) != raw}
            changes.update({key: None for key in before.keys() - after.keys()})
            result.append(updated)
            return changes
        if guard is None:
            self.store.record_mutate(self.records_namespace, keys, apply)
        else:
            def guarded(documents):
                guard(documents)
                records = documents[self.records_namespace]
                changes = apply({key: records.get(key) for key in keys})
                return {self.records_namespace: changes}
            self.store.record_mutate_many({**guard_rows, self.records_namespace: keys}, guarded)
        return result[0]

ACTION_INTENT_STATE_MIGRATIONS.register(
    "1.4", "1.5", lambda payload: {**payload, "schema_version": "1.5"},
)
