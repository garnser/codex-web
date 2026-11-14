from __future__ import annotations

from typing import Any, Callable
import copy
import inspect
import threading
from contextvars import ContextVar

from codex_web.compatibility import MigrationRegistry
from codex_web.configuration import CONFIGURATION_CONTRACT, ConfigurationRecord
from codex_web.storage.sqlite_state import SQLiteStateStore


CONFIGURATION_MIGRATIONS = MigrationRegistry("configuration-record")
CONFIGURATION_MIGRATIONS.register(
    "0.0",
    "1.0",
    lambda payload: {
        "schema_version": CONFIGURATION_CONTRACT.current,
        "records": list(payload.get("records", [])),
    },
)


class ConfigurationRegistryStore:
    """SQLite-backed immutable-revision configuration record store."""

    namespace = "canonical_configuration_registry"

    def __init__(self, store: SQLiteStateStore) -> None:
        self.store = store
        self._read_observation = ContextVar('codex_web_configuration_read_observation', default=None)

    def _decode(self, payload: Any) -> list[ConfigurationRecord]:
        if payload is None:
            return []
        if isinstance(payload, list):
            payload = {"records": payload}
        if not isinstance(payload, dict):
            raise ValueError("canonical configuration store must be an object")
        version = str(payload.get("schema_version") or "0.0")
        if version != CONFIGURATION_CONTRACT.current:
            payload = CONFIGURATION_MIGRATIONS.migrate(
                payload,
                from_version=version,
                to_version=CONFIGURATION_CONTRACT.current,
            )
        CONFIGURATION_CONTRACT.require(payload.get("schema_version", ""))
        values = payload.get("records", [])
        if not isinstance(values, list):
            raise ValueError("canonical configuration records must be a list")
        return [ConfigurationRecord.model_validate(item) for item in values]

    @staticmethod
    def _encode(records: list[ConfigurationRecord]) -> dict[str, Any]:
        return {
            "schema_version": CONFIGURATION_CONTRACT.current,
            "records": [record.model_dump(mode="json") for record in records],
        }

    def load(self) -> list[ConfigurationRecord]:
        observation = self._read_observation.get()
        if observation is not None:
            return observation.load(self)
        return self._decode(self.store.get(self.namespace))

    def can_observe_reads(self):
        return (type(self) is ConfigurationRegistryStore and self.store is not None
            and all(getattr(getattr(self, name, None), '__self__', None) is self
                and getattr(getattr(self, name, None), '__func__', None) is method
                for name, method in _CANONICAL_CONFIGURATION_READS.items()))

    def observe_reads(self, callback):
        """One synchronous raw observation; never retain a resolved value."""
        if not _CANONICAL_CONFIGURATION_READS['can_observe_reads'](self):
            return callback(None)
        observation = _ConfigurationReadObservation(self)
        token = self._read_observation.set(observation)
        try:
            result = callback(observation)
            if inspect.isawaitable(result) or inspect.isgenerator(result) or inspect.isasyncgen(result):
                if inspect.iscoroutine(result) or inspect.isgenerator(result):
                    result.close()
                raise ValueError('Configuration observation callback must be synchronous')
            return result
        finally:
            self._read_observation.reset(token)
            observation.close()

    def replace(self, records: list[ConfigurationRecord]) -> list[ConfigurationRecord]:
        payload = self._encode(records)
        self.store.put(self.namespace, payload)
        return [record.model_copy(deep=True) for record in records]

    def update(
        self,
        updater: Callable[[list[ConfigurationRecord]], list[ConfigurationRecord]],
    ) -> list[ConfigurationRecord]:
        def apply(raw: Any) -> dict[str, Any]:
            records = self._decode(raw)
            updated = updater(records)
            return self._encode(updated)

        payload = self.store.update(
            self.namespace,
            apply,
            default={
                "schema_version": CONFIGURATION_CONTRACT.current,
                "records": [],
            },
        )
        return self._decode(payload)


class _ConfigurationReadObservation:
    def __init__(self, repository):
        self.repository = repository
        self._backend = repository.store
        self._namespace = repository.namespace
        self._thread = threading.get_ident()
        self._active = True
        self._loaded = False
        self._raw = None

    def require_owner(self, repository):
        if (not self._active or repository is not self.repository
                or repository.store is not self._backend or repository.namespace != self._namespace
                or threading.get_ident() != self._thread
                or not _CANONICAL_CONFIGURATION_READS['can_observe_reads'](repository)):
            raise ValueError('Configuration observation is foreign, closed or changed')

    def load(self, repository):
        self.require_owner(repository)
        if not self._loaded:
            self._raw = copy.deepcopy(self._backend.get(self._namespace))
            self._loaded = True
        # Resolve and validate current policy/clock on every caller invocation;
        # the view stores the exact raw envelope, not models or an effective value.
        return _CANONICAL_CONFIGURATION_READS['_decode'](repository, copy.deepcopy(self._raw))

    def require_current(self, repository, raw):
        self.require_owner(repository)
        if not self._loaded or raw != self._raw:
            raise ValueError('Configuration observation changed before guarded use')

    def close(self):
        self._active = False
        self._raw = None
        self._loaded = False


_CANONICAL_CONFIGURATION_READS = {name: getattr(ConfigurationRegistryStore, name) for name in (
    'load', '_decode', 'can_observe_reads', 'observe_reads')}
