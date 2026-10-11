from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any, Callable

from codex_web.autonomy import (
    AUTONOMY_STATE_CONTRACT,
    AutonomyControl,
    AutonomyCycleRecord,
    AutonomyDeadLetter,
    AutonomyState,
)
from codex_web.autonomy_policy import AutonomyBreakGlassGrant
from codex_web.compatibility import ContractSpec, MigrationRegistry
from codex_web.storage.state_store import StateStore


AUTONOMY_STATE_MIGRATIONS = MigrationRegistry("autonomy-state")
AUTONOMY_STATE_MIGRATIONS.register(
    "0.0",
    "1.0",
    lambda payload: {
        "schema_version": "1.0",
        "control": payload.get("control") or {},
        "cycles": list(payload.get("cycles") or []),
        "dead_letters": list(payload.get("dead_letters") or []),
        "updated_at": float(payload.get("updated_at") or time.time()),
        "updated_by": str(payload.get("updated_by") or "migration"),
    },
)
AUTONOMY_STATE_MIGRATIONS.register(
    "1.0",
    "2.0",
    lambda payload: {
        **payload,
        "schema_version": "2.0",
        "break_glass_grants": list(payload.get("break_glass_grants") or []),
    },
)


CYCLE_RECORDS_CONTRACT = ContractSpec("autonomy-cycle-records", "1.0", ("1.0",))


class AutonomyStateStore:
    namespace = "autonomy"
    max_cycles = 5000
    cycle_meta_key = "_cycle_records_meta"
    max_dead_letters = 1000

    def __init__(self, store: StateStore) -> None:
        self.store = store

    def _decode(self, payload: Any) -> AutonomyState:
        if payload is None:
            return AutonomyState()
        if not isinstance(payload, dict):
            raise ValueError("autonomy state must be an object")
        version = str(payload.get("schema_version") or "0.0")
        if version != AUTONOMY_STATE_CONTRACT.current:
            payload = AUTONOMY_STATE_MIGRATIONS.migrate(
                payload,
                from_version=version,
                to_version=AUTONOMY_STATE_CONTRACT.current,
            )
        AUTONOMY_STATE_CONTRACT.require(payload.get("schema_version", ""))
        if self.cycle_meta_key in payload:
            self._validate_cycle_meta(payload[self.cycle_meta_key])
            payload = {
                **{key: payload[key] for key in AutonomyState.model_fields
                   if key != "cycles" and key in payload},
                "cycles": [value for key, value in sorted(payload.items()) if key.startswith("cycle:")],
            }
        return AutonomyState.model_validate(payload)

    def _ensure_records(self) -> None:
        if self.store.record_collection_exists(self.namespace):
            return

        def migrate(current):
            # A concurrent initializer may already have normalized the cycle
            # ledger while this caller waited for the namespace lock.
            if current[self.cycle_meta_key] is not None:
                self._validate_cycle_meta(current[self.cycle_meta_key])
                return {}
            payload = {key: value for key, value in current.items()
                       if key != self.cycle_meta_key and value is not None}
            return self._decode(payload).model_dump(mode="json")

        # record_mutate converts the legacy mapping and migrates its fields
        # under the same namespace transaction lock, preserving concurrent writes.
        self.store.record_mutate(self.namespace, (self.cycle_meta_key, *AutonomyState.model_fields), migrate)

    @staticmethod
    def _cycle_key(sequence: int) -> str:
        return f"cycle:{sequence:020d}"

    @staticmethod
    def _lookup_key(cycle_key: str, event_id: str) -> str:
        digest = hashlib.sha256(json.dumps([cycle_key, event_id], separators=(",", ":")).encode()).hexdigest()
        return f"cycle-lookup:{digest}"

    def _validate_cycle_meta(self, meta: Any) -> None:
        if not isinstance(meta, dict):
            raise ValueError("cycle records metadata must be an object")
        CYCLE_RECORDS_CONTRACT.require(str(meta.get("schema_version") or ""))
        if meta.get("max_cycles") != self.max_cycles:
            raise ValueError("cycle retention change requires migration")
        if not isinstance(meta.get("next_sequence"), int) or meta["next_sequence"] < 0:
            raise ValueError("invalid cycle records sequence")

    def _cycle_records(self, cycles: list[AutonomyCycleRecord]) -> dict[str, Any]:
        records = {self.cycle_meta_key: {
            "schema_version": CYCLE_RECORDS_CONTRACT.current,
            "max_cycles": self.max_cycles, "next_sequence": len(cycles),
            "generation": uuid.uuid4().hex,
        }}
        for sequence, cycle in enumerate(cycles):
            key = self._cycle_key(sequence)
            records[key] = cycle.model_dump(mode="json")
            records[self._lookup_key(cycle.cycle_key, cycle.event_id)] = {"record_key": key}
        return records

    def _encode(self, state: AutonomyState) -> dict[str, Any]:
        raw = state.model_dump(mode="json")
        raw.pop("cycles")
        raw.update(self._cycle_records(state.cycles[-self.max_cycles:]))
        return raw

    def _ensure_cycle_records(self) -> None:
        self._ensure_records()
        meta = self.store.record_get(self.namespace, self.cycle_meta_key)
        if meta is not None:
            self._validate_cycle_meta(meta)
            return

        def migrate(current):
            if current[self.cycle_meta_key] is not None:
                self._validate_cycle_meta(current[self.cycle_meta_key])
                return {}
            state = self._decode({"schema_version": current["schema_version"],
                                  "cycles": current["cycles"] or []})
            return {**self._cycle_records(state.cycles[-self.max_cycles:]),
                    "schema_version": AUTONOMY_STATE_CONTRACT.current, "cycles": None}

        self.store.record_mutate(
            self.namespace, (self.cycle_meta_key, "schema_version", "cycles"), migrate,
        )

    def find_cycle(self, cycle_key: str, event_id: str) -> AutonomyCycleRecord | None:
        self._ensure_cycle_records()
        lookup = self.store.record_get(self.namespace, self._lookup_key(cycle_key, event_id))
        if lookup is None:
            return None
        raw = self.store.record_get(self.namespace, lookup["record_key"])
        if raw is None:
            return None
        cycle = AutonomyCycleRecord.model_validate(raw)
        if cycle.cycle_key != cycle_key or cycle.event_id != event_id:
            raise ValueError("cycle lookup does not match its record")
        return cycle

    def recent_cycles(self, limit: int = 500) -> list[AutonomyCycleRecord]:
        self._ensure_cycle_records()
        if limit < 1 or limit > 1000:
            raise ValueError("cycle page limit must be between 1 and 1000")
        meta = self.store.record_get(self.namespace, self.cycle_meta_key)
        self._validate_cycle_meta(meta)
        start = max(0, meta["next_sequence"] - limit)
        rows, _ = self.store.record_page(
            self.namespace, key_prefix="cycle:",
            after=self._cycle_key(start - 1) if start else None, limit=limit,
        )
        return [AutonomyCycleRecord.model_validate(raw) for raw in rows.values()]

    def control(self) -> AutonomyControl:
        self._ensure_records()
        version = self.store.record_get(self.namespace, "schema_version")
        if version != AUTONOMY_STATE_CONTRACT.current:
            return self.load().control
        return AutonomyControl.model_validate(self.store.record_get(self.namespace, "control"))

    def load(self) -> AutonomyState:
        self._ensure_records()
        return self._decode(self.store.get(self.namespace))

    def update(self, updater: Callable[[AutonomyState], AutonomyState]) -> AutonomyState:
        self._ensure_records()
        def apply(raw: Any) -> dict[str, Any]:
            state = updater(self._decode(raw))
            state.cycles = state.cycles[-self.max_cycles :]
            state.dead_letters = state.dead_letters[-self.max_dead_letters :]
            state.updated_at = time.time()
            return self._encode(state)

        payload = self.store.update(
            self.namespace,
            apply,
            default=AutonomyState().model_dump(mode="json"),
        )
        return self._decode(payload)

    def set_control(self, control: AutonomyControl, *, actor_id: str) -> AutonomyState:
        return self.update(
            lambda state: state.model_copy(
                update={"control": control, "updated_by": actor_id}
            )
        )

    def append_cycle(self, cycle: AutonomyCycleRecord) -> AutonomyCycleRecord:
        self._ensure_cycle_records()
        row = cycle.model_dump(mode="json")
        lookup_key = self._lookup_key(cycle.cycle_key, cycle.event_id)
        while True:
            expected = self.store.record_get(self.namespace, self.cycle_meta_key)
            self._validate_cycle_meta(expected)
            sequence = expected["next_sequence"]
            expired_key = self._cycle_key(sequence - self.max_cycles)
            expired = (self.store.record_get(self.namespace, expired_key)
                       if sequence >= self.max_cycles else None)
            expired_lookup = (self._lookup_key(expired["cycle_key"], expired["event_id"])
                              if expired is not None else None)
            keys = (self.cycle_meta_key, expired_key, *([expired_lookup] if expired_lookup else []))

            def apply(current):
                # Another writer may have advanced the ring after our point
                # reads. Retry rather than pruning that writer's retained row.
                if current[self.cycle_meta_key] != expected or current[expired_key] != expired:
                    return {}
                meta = dict(expected)
                meta["next_sequence"] = sequence + 1
                key = self._cycle_key(sequence)
                changes = {self.cycle_meta_key: meta, key: row,
                           lookup_key: {"record_key": key}, "updated_at": time.time()}
                if expired is not None:
                    changes[expired_key] = None
                    if expired_lookup != lookup_key and current[expired_lookup] == {"record_key": expired_key}:
                        changes[expired_lookup] = None
                return changes

            if self.store.record_mutate(self.namespace, keys, apply):
                return cycle

    def record_dead_letter(self, item: AutonomyDeadLetter) -> AutonomyDeadLetter:
        """Append to the bounded dead-letter field without reading cycle history."""
        self._ensure_records()
        item = AutonomyDeadLetter.model_validate(item.model_dump(mode="json"))
        row = item.model_dump(mode="json")

        def apply(current):
            values = current["dead_letters"]
            if values is None:
                values = []
            if not isinstance(values, list):
                raise ValueError("autonomy dead letters must be a list")
            # Validate retained state as before; never return an unchecked
            # record merely because the full cycle ledger is no longer read.
            retained = [AutonomyDeadLetter.model_validate(value).model_dump(mode="json")
                        for value in values]
            return {"dead_letters": (retained + [row])[-self.max_dead_letters:],
                    "updated_at": time.time()}

        self.store.record_mutate(self.namespace, ("dead_letters", "updated_at"), apply)
        return item

    def append_dead_letter(self, item: AutonomyDeadLetter) -> AutonomyState:
        # Preserve the historical full-state return contract for explicit callers.
        self.record_dead_letter(item)
        return self.load()

    def add_break_glass_grant(
        self,
        grant: AutonomyBreakGlassGrant,
    ) -> AutonomyState:
        def apply(state: AutonomyState) -> AutonomyState:
            state.break_glass_grants = [
                item
                for item in state.break_glass_grants
                if item.id != grant.id
            ]
            state.break_glass_grants.append(grant)
            return state

        return self.update(apply)

    def active_break_glass_grants(
        self,
        *,
        organization_id: str,
        workspace_id: str,
        now: float,
        project_id: str | None = None,
    ) -> tuple[AutonomyBreakGlassGrant, ...]:
        self._ensure_records()
        if self.store.record_get(self.namespace, "schema_version") != AUTONOMY_STATE_CONTRACT.current:
            grants = self.load().break_glass_grants
        else:
            raw = self.store.record_get(self.namespace, "break_glass_grants")
            grants = [AutonomyBreakGlassGrant.model_validate(item) for item in (raw or [])]
        return tuple(
            item
            for item in grants
            if item.organization_id == organization_id
            and item.workspace_id == workspace_id
            and item.expires_at > now
            and (item.project_id is None or item.project_id == project_id)
        )
